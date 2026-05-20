#!/usr/bin/env python3
"""
EBS Right-Sizer
===============
Closes the loop between AWS Compute Optimizer EBS findings and actual
CloudWatch usage history, then optionally applies new IOPS / throughput
values via ec2:ModifyVolume.

Capabilities
------------
1. Compute-Optimizer-driven right-sizing report (default).
2. Orphan-volume report (unattached `available` volumes).
3. Targeted apply: --volume-ids, --volume-ids-file, or --apply-all.

Read-only by default. The --apply flag must be combined with an explicit
volume scope and (in interactive sessions) confirmed at the prompt.

Required IAM
------------
  compute-optimizer:GetEBSVolumeRecommendations
  ec2:DescribeVolumes
  ec2:DescribeVolumesModifications
  ec2:ModifyVolume                      (only when --apply is set)
  cloudwatch:GetMetricData
  sts:GetCallerIdentity
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GP3_BASELINE_IOPS = 3000
GP3_BASELINE_THROUGHPUT = 125  # MiB/s

# Per-volume-type service ceilings.
TYPE_LIMITS: Dict[str, Dict[str, int]] = {
    "gp3": {"max_iops": 16000, "max_throughput": 1000},
    "io1": {"max_iops": 64000, "max_throughput": 1000},
    "io2": {"max_iops": 256000, "max_throughput": 4000},
}
TUNABLE_TYPES = frozenset(TYPE_LIMITS)

# gp3 pricing model (USD per month). Defaults match AWS list prices in
# us-east-1 as of late 2025. Storage = $/GiB-month; IOPS over 3000 =
# $/provisioned-IOPS-month; throughput over 125 = $/MiB/s-month.
# Prices vary by region; override via --pricing-file or --price-* flags.
DEFAULT_PRICING_GP3: Dict[str, float] = {
    "storage_per_gib_month": 0.08,
    "iops_per_iop_month_over_3000": 0.005,
    "throughput_per_mibps_month_over_125": 0.04,
}

# CloudWatch retains 5-minute resolution metrics for 63 days.
MAX_LOOKBACK_DAYS = 63

VOLUME_ID_RE = re.compile(r"^vol-[0-9a-f]{8,17}$")
REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d$")

# Characters that trigger formula execution in spreadsheets (CSV injection).
CSV_INJECTION_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

# Bound the parallel CloudWatch fan-out. CW account-level RPS is ~400/s.
DEFAULT_MAX_WORKERS = 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ebs-rightsizer")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class VolumeReport:
    volume_id: str
    volume_type: str
    size_gib: int
    state: str
    attached_instances: str
    create_time: str
    current_iops: int
    current_throughput_mibps: int
    peak_iops: float
    peak_throughput_mibps: float
    target_iops: Optional[int]
    target_throughput_mibps: Optional[int]
    direction: str  # UPSIZE | DOWNSIZE | NONE
    action: str
    monthly_cost_current_usd: float = 0.0
    monthly_cost_target_usd: float = 0.0
    monthly_delta_usd: float = 0.0   # target - current; negative = savings
    annual_delta_usd: float = 0.0
    modification_state: str = ""
    co_finding_reasons: str = ""
    category: str = "rightsizing"  # or "orphan"
    notes: str = ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--region", required=True, help="AWS region, e.g. us-east-1")
    p.add_argument("--profile", default=None, help="AWS CLI profile (optional)")
    p.add_argument("--days", type=int, default=30,
                   help=f"CloudWatch lookback in days (1-{MAX_LOOKBACK_DAYS}, default 30)")
    p.add_argument("--buffer", type=float, default=20.0,
                   help="Safety buffer percent above observed peak (default 20)")
    p.add_argument("--min-iops", type=int, default=GP3_BASELINE_IOPS,
                   help=f"Floor for IOPS (default {GP3_BASELINE_IOPS})")
    p.add_argument("--min-throughput", type=int, default=GP3_BASELINE_THROUGHPUT,
                   help=f"Floor for throughput MiB/s (default {GP3_BASELINE_THROUGHPUT})")
    p.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS,
                   help=f"Parallel CloudWatch workers (default {DEFAULT_MAX_WORKERS})")
    p.add_argument("--output", default="ebs_rightsizing_report.csv",
                   help="CSV report output path")

    # Pricing (gp3)
    p.add_argument("--pricing-file",
                   help="JSON file with keys: storage_per_gib_month, "
                        "iops_per_iop_month_over_3000, "
                        "throughput_per_mibps_month_over_125")
    p.add_argument("--price-storage", type=float, default=None,
                   help="Override gp3 storage price ($/GiB-month)")
    p.add_argument("--price-iops", type=float, default=None,
                   help="Override gp3 IOPS price ($/IOP-month above 3000)")
    p.add_argument("--price-throughput", type=float, default=None,
                   help="Override gp3 throughput price ($/MiB/s-month above 125)")

    # Orphan options
    p.add_argument("--include-orphans", action="store_true",
                   help="Append unattached `available` volumes to the report")
    p.add_argument("--orphans-only", action="store_true",
                   help="Only scan orphan volumes; skip Compute Optimizer findings")

    # Scoping
    p.add_argument("--volume-ids", nargs="+",
                   help="Limit analysis (and apply) to these volume IDs")
    p.add_argument("--volume-ids-file",
                   help="File with one volume ID per line; same effect as --volume-ids")

    # Direction / type filtering
    p.add_argument("--gp3-only", dest="gp3_only", action="store_true", default=True,
                   help="Only analyze gp3 volumes (default ON)")
    p.add_argument("--all-types", dest="gp3_only", action="store_false",
                   help="Include io1/io2/gp2 volumes in the report")
    p.add_argument("--direction", choices=["both", "upsize", "downsize"], default="both",
                   help="Filter MODIFY rows by direction (default both)")

    # Apply
    p.add_argument("--apply", action="store_true",
                   help="Call ec2:ModifyVolume on in-scope volumes (off by default)")
    p.add_argument("--apply-all", action="store_true",
                   help="Required if --apply is used WITHOUT --volume-ids/--volume-ids-file. "
                        "Explicit opt-in to modify every volume in scope.")
    p.add_argument("--yes", action="store_true",
                   help="Skip interactive confirmation prompt before applying")

    return p.parse_args(argv)


def validate_args(args: argparse.Namespace) -> List[str]:
    """Return a list of validation error messages (empty if OK)."""
    errors: List[str] = []
    if not REGION_RE.match(args.region):
        errors.append(f"--region '{args.region}' does not look like a valid AWS region.")
    if not 1 <= args.days <= MAX_LOOKBACK_DAYS:
        errors.append(f"--days must be 1..{MAX_LOOKBACK_DAYS}.")
    if args.buffer < 0 or args.buffer > 500:
        errors.append("--buffer must be between 0 and 500.")
    if args.min_iops < 100:
        errors.append("--min-iops must be at least 100.")
    if args.min_throughput < 25:
        errors.append("--min-throughput must be at least 25.")
    if args.max_workers < 1 or args.max_workers > 32:
        errors.append("--max-workers must be 1..32.")
    if args.orphans_only and args.include_orphans:
        # not an error, but redundant
        pass
    if args.apply:
        scoped = bool(args.volume_ids) or bool(args.volume_ids_file)
        if not scoped and not args.apply_all:
            errors.append(
                "--apply requires either --volume-ids / --volume-ids-file, "
                "or --apply-all to confirm operating on the full scope."
            )
    # Output path safety: refuse weird paths
    out_dir = os.path.dirname(os.path.abspath(args.output)) or "."
    if not os.path.isdir(out_dir):
        errors.append(f"Output directory '{out_dir}' does not exist.")
    return errors


def load_volume_id_scope(args: argparse.Namespace) -> Optional[List[str]]:
    """Return the deduped list of explicitly scoped volume IDs, or None."""
    ids: List[str] = []
    if args.volume_ids:
        ids.extend(args.volume_ids)
    if args.volume_ids_file:
        try:
            with open(args.volume_ids_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        ids.append(line)
        except OSError as exc:
            log.error("Cannot read --volume-ids-file: %s", exc)
            sys.exit(2)

    if not ids:
        return None

    cleaned: List[str] = []
    seen = set()
    for v in ids:
        v = v.strip()
        if not VOLUME_ID_RE.match(v):
            log.error("Invalid volume ID format: %r (expected vol-xxxxxxxx)", v)
            sys.exit(2)
        if v not in seen:
            seen.add(v)
            cleaned.append(v)
    return cleaned


# ---------------------------------------------------------------------------
# AWS client setup
# ---------------------------------------------------------------------------

def build_session(profile: Optional[str], region: str) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


def boto_config() -> Config:
    # Adaptive retries handle Compute Optimizer + CloudWatch throttling cleanly.
    return Config(
        retries={"max_attempts": 10, "mode": "adaptive"},
        user_agent_extra="ebs-rightsizer/1.0",
    )


# ---------------------------------------------------------------------------
# Compute Optimizer
# ---------------------------------------------------------------------------

def get_compute_optimizer_findings(co_client, account_id: str) -> Dict[str, dict]:
    """Manual nextToken pagination - older boto3 builds don't register a paginator
    for get_ebs_volume_recommendations (e.g. the python3-boto3 RPM on AL2023)."""
    findings: Dict[str, dict] = {}
    next_token: Optional[str] = None
    page_size = 1000  # API max per call
    try:
        while True:
            kwargs: Dict[str, object] = {
                "accountIds": [account_id],
                "maxResults": page_size,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            resp = co_client.get_ebs_volume_recommendations(**kwargs)
            for rec in resp.get("volumeRecommendations", []):
                if rec.get("finding") != "NotOptimized":
                    continue
                vol_arn = rec.get("volumeArn", "")
                vol_id = vol_arn.rsplit("/", 1)[-1] if vol_arn else ""
                if VOLUME_ID_RE.match(vol_id):
                    findings[vol_id] = rec
            next_token = resp.get("nextToken")
            if not next_token:
                break
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("OptInRequiredException", "AccessDeniedException"):
            log.error("Compute Optimizer is not opted in for account %s, or you lack "
                      "compute-optimizer:GetEBSVolumeRecommendations permission.",
                      account_id)
        log.error("Compute Optimizer call failed: %s", exc)
        sys.exit(1)
    return findings


# ---------------------------------------------------------------------------
# EC2 / Volume helpers
# ---------------------------------------------------------------------------

def describe_volumes_by_id(ec2_client, volume_ids: List[str]) -> Dict[str, dict]:
    """Describe specific volumes; tolerates partial NotFound errors.

    Note: EC2 DescribeVolumes rejects MaxResults when VolumeIds is set, so we
    cannot use the boto3 paginator (which always injects MaxResults via
    PaginationConfig). We chunk manually instead. Per-chunk size of 200 is
    well under any practical response limit.
    """
    out: Dict[str, dict] = {}
    if not volume_ids:
        return out
    for i in range(0, len(volume_ids), 200):
        chunk = volume_ids[i:i + 200]
        try:
            resp = ec2_client.describe_volumes(VolumeIds=chunk)
            for v in resp.get("Volumes", []):
                out[v["VolumeId"]] = v
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "InvalidVolume.NotFound":
                log.warning("One or more volumes in chunk no longer exist: %s", exc)
            else:
                log.warning("DescribeVolumes failed for chunk: %s", exc)
    return out


def describe_orphan_volumes(ec2_client) -> Dict[str, dict]:
    """All volumes in `available` state (unattached)."""
    out: Dict[str, dict] = {}
    paginator = ec2_client.get_paginator("describe_volumes")
    try:
        for page in paginator.paginate(
            Filters=[{"Name": "status", "Values": ["available"]}],
            PaginationConfig={"PageSize": 500},
        ):
            for v in page.get("Volumes", []):
                out[v["VolumeId"]] = v
    except ClientError as exc:
        log.error("DescribeVolumes (orphans) failed: %s", exc)
    return out


def get_in_progress_modifications(ec2_client, volume_ids: List[str]) -> Dict[str, str]:
    """Return {volume_id: state} for modifications still running. Used to skip cooldown collisions."""
    busy: Dict[str, str] = {}
    if not volume_ids:
        return busy
    paginator = ec2_client.get_paginator("describe_volumes_modifications")
    for i in range(0, len(volume_ids), 200):
        chunk = volume_ids[i:i + 200]
        try:
            for page in paginator.paginate(VolumeIds=chunk):
                for mod in page.get("VolumesModifications", []):
                    state = mod.get("ModificationState", "")
                    if state in ("modifying", "optimizing"):
                        busy[mod["VolumeId"]] = state
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "InvalidVolumeModification.NotFound":
                continue
            log.warning("DescribeVolumesModifications failed for chunk: %s", exc)
    return busy


# ---------------------------------------------------------------------------
# CloudWatch
# ---------------------------------------------------------------------------

# GetMetricStatistics caps at 1440 datapoints per call. GetMetricData allows
# up to 100,800 datapoints per request total, supports pagination via
# NextToken, and lets us pull all 4 EBS metrics in a single round-trip per
# volume. Far fewer API calls, no datapoint-cap math.

EBS_METRICS = ("VolumeReadOps", "VolumeWriteOps", "VolumeReadBytes", "VolumeWriteBytes")


def collect_peak_usage(cw_client, volume_id: str,
                       days: int) -> Tuple[float, float, int]:
    """Return (peak_iops, peak_throughput_MiBps, total_datapoints).

    Uses GetMetricData to fetch all 4 EBS metrics in one paginated call.
    `total_datapoints` lets the caller distinguish "genuinely idle volume"
    from "no metric data available".
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    period = 300  # native EBS metric resolution

    queries = []
    for idx, metric_name in enumerate(EBS_METRICS):
        queries.append({
            "Id": f"m{idx}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EBS",
                    "MetricName": metric_name,
                    "Dimensions": [{"Name": "VolumeId", "Value": volume_id}],
                },
                "Period": period,
                "Stat": "Sum",
            },
            "ReturnData": True,
        })

    peaks: Dict[str, float] = {q["Id"]: 0.0 for q in queries}
    total_points = 0
    next_token: Optional[str] = None

    try:
        while True:
            kwargs: Dict[str, object] = {
                "MetricDataQueries": queries,
                "StartTime": start,
                "EndTime": end,
                "ScanBy": "TimestampDescending",
            }
            if next_token:
                kwargs["NextToken"] = next_token
            resp = cw_client.get_metric_data(**kwargs)
            for r in resp.get("MetricDataResults", []):
                values = r.get("Values", [])
                total_points += len(values)
                if values:
                    local_max = max(values)
                    if local_max > peaks[r["Id"]]:
                        peaks[r["Id"]] = local_max
            next_token = resp.get("NextToken")
            if not next_token:
                break
    except (ClientError, BotoCoreError) as exc:
        log.warning("CloudWatch GetMetricData failed for %s: %s", volume_id, exc)
        return 0.0, 0.0, 0

    # Sum-over-period -> per-second rate
    peak_iops = (peaks["m0"] + peaks["m1"]) / period
    peak_throughput_mibps = (peaks["m2"] + peaks["m3"]) / period / (1024 * 1024)
    return peak_iops, peak_throughput_mibps, total_points


# ---------------------------------------------------------------------------
# Sizing math
# ---------------------------------------------------------------------------

def round_up_to_step(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step) if value > 0 else 0


def compute_target(volume_type: str,
                   peak_iops: float,
                   peak_throughput: float,
                   buffer_pct: float,
                   min_iops: int,
                   min_throughput: int) -> Tuple[int, int]:
    limits = TYPE_LIMITS.get(volume_type, TYPE_LIMITS["gp3"])
    multiplier = 1 + (buffer_pct / 100.0)

    target_iops = max(min_iops, round_up_to_step(peak_iops * multiplier, 100))
    target_iops = min(target_iops, limits["max_iops"])

    target_tput = max(min_throughput, round_up_to_step(peak_throughput * multiplier, 25))
    target_tput = min(target_tput, limits["max_throughput"])

    return target_iops, target_tput


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def load_pricing(args: argparse.Namespace) -> Dict[str, float]:
    """Resolve gp3 pricing in this order: defaults <- pricing file <- CLI overrides."""
    pricing = dict(DEFAULT_PRICING_GP3)
    if args.pricing_file:
        try:
            with open(args.pricing_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Cannot read --pricing-file: %s", exc)
            sys.exit(2)
        for key in pricing:
            if key in data:
                try:
                    pricing[key] = float(data[key])
                except (TypeError, ValueError):
                    log.error("Pricing file key %r must be numeric", key)
                    sys.exit(2)
    if args.price_storage is not None:
        pricing["storage_per_gib_month"] = args.price_storage
    if args.price_iops is not None:
        pricing["iops_per_iop_month_over_3000"] = args.price_iops
    if args.price_throughput is not None:
        pricing["throughput_per_mibps_month_over_125"] = args.price_throughput
    return pricing


def gp3_monthly_cost(size_gib: int, iops: int, throughput_mibps: int,
                     pricing: Dict[str, float]) -> float:
    """Compute monthly USD cost for a gp3 volume. AWS only bills for IOPS over
    3000 and throughput over 125 MiB/s; baseline is included in storage."""
    storage = max(0, size_gib) * pricing["storage_per_gib_month"]
    extra_iops = max(0, iops - 3000) * pricing["iops_per_iop_month_over_3000"]
    extra_tput = max(0, throughput_mibps - 125) * pricing["throughput_per_mibps_month_over_125"]
    return round(storage + extra_iops + extra_tput, 2)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def modify_volume(ec2_client, volume_id: str, new_iops: int,
                  new_throughput: int, volume_type: str) -> Optional[str]:
    kwargs = {"VolumeId": volume_id, "Iops": new_iops}
    if volume_type == "gp3":
        kwargs["Throughput"] = new_throughput
    try:
        resp = ec2_client.modify_volume(**kwargs)
        return resp.get("VolumeModification", {}).get("ModificationState")
    except ClientError as exc:
        log.error("ModifyVolume failed for %s: %s", volume_id, exc)
        return None


def confirm_apply(scope_count: int, applying_to_all: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        log.error("Refusing to --apply without --yes in a non-interactive session.")
        return False
    scope_label = "ALL Compute-Optimizer-flagged volumes" if applying_to_all else "the listed volumes"
    answer = input(
        f"\nAbout to call ec2:ModifyVolume on {scope_count} volume(s) ({scope_label}).\n"
        "Type 'yes' to proceed: "
    ).strip().lower()
    return answer == "yes"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def sanitize_for_csv(value) -> str:
    """Mitigate CSV/spreadsheet formula injection.

    Pure numeric values (int/float) are emitted as-is; the formula-injection
    risk only applies to string content. For string values, prefix any cell
    starting with =, +, -, @, tab, or CR with a single quote so spreadsheet
    apps don't treat it as a formula.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if s and s[0] in CSV_INJECTION_TRIGGERS:
        return "'" + s
    return s


def write_report(rows: List[VolumeReport], path: str) -> None:
    if not rows:
        log.info("No rows to write.")
        return
    fieldnames = list(rows[0].__dict__.keys())
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: sanitize_for_csv(v) for k, v in row.__dict__.items()})
    os.replace(tmp_path, path)
    log.info("Report written to %s (%d rows)", path, len(rows))


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def analyze_rightsizing(args: argparse.Namespace,
                        ec2, cw, co,
                        account_id: str,
                        scope_ids: Optional[List[str]],
                        pricing: Dict[str, float]) -> List[VolumeReport]:
    log.info("Fetching Compute Optimizer EBS recommendations...")
    findings = get_compute_optimizer_findings(co, account_id)
    log.info("Compute Optimizer flagged %d volumes as NotOptimized.", len(findings))

    if scope_ids is not None:
        missing = [v for v in scope_ids if v not in findings]
        if missing:
            log.warning("These scoped IDs are not in CO findings (still analyzed): %s",
                        ", ".join(missing))
        findings = {v: findings.get(v, {}) for v in scope_ids}

    if not findings:
        return []

    volume_details = describe_volumes_by_id(ec2, list(findings.keys()))

    # gp3-only filter (default). Drop non-gp3 volumes from analysis entirely
    # rather than producing SKIP rows the user doesn't care about.
    if args.gp3_only:
        before = len(findings)
        kept = {vid: rec for vid, rec in findings.items()
                if volume_details.get(vid, {}).get("VolumeType") == "gp3"}
        dropped = before - len(kept)
        if dropped:
            log.info("--gp3-only: filtered out %d non-gp3 volume(s).", dropped)
        findings = kept
        if not findings:
            log.info("No gp3 volumes in scope after filtering.")
            return []

    rows: List[VolumeReport] = []
    # Parallel CloudWatch fan-out
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        future_map = {
            pool.submit(collect_peak_usage, cw, vid, args.days): vid
            for vid in findings
        }
        peaks: Dict[str, Tuple[float, float, int]] = {}
        for fut in as_completed(future_map):
            vid = future_map[fut]
            try:
                peaks[vid] = fut.result()
            except Exception as exc:  # noqa: BLE001 - log and continue
                log.warning("Metric collection failed for %s: %s", vid, exc)
                peaks[vid] = (0.0, 0.0, 0)

    for vol_id, rec in findings.items():
        vol = volume_details.get(vol_id)
        peak_iops, peak_tput, n_points = peaks.get(vol_id, (0.0, 0.0, 0))

        if not vol:
            missing_row = VolumeReport(
                volume_id=vol_id,
                volume_type="?",
                size_gib=0,
                state="missing",
                attached_instances="",
                create_time="",
                current_iops=0,
                current_throughput_mibps=0,
                peak_iops=round(peak_iops, 2),
                peak_throughput_mibps=round(peak_tput, 2),
                target_iops=None,
                target_throughput_mibps=None,
                direction="NONE",
                action="SKIP - volume not found (deleted?)",
                co_finding_reasons=";".join(rec.get("findingReasonCodes", [])) if rec else "",
                notes=f"datapoints={n_points}",
            )
            _populate_cost_columns(missing_row, pricing)
            rows.append(missing_row)
            continue

        rows.append(_build_rightsizing_row(vol, rec, peak_iops, peak_tput, n_points, args, pricing))

    # Direction filter applies after rows are built so SKIP/NO_CHANGE are kept
    # for visibility but MODIFY rows can be narrowed.
    if args.direction != "both":
        wanted = args.direction.upper()
        rows = [r for r in rows
                if not r.action.startswith("MODIFY")
                or r.direction == wanted]
    return rows


def _populate_cost_columns(row: VolumeReport, pricing: Dict[str, float]) -> None:
    """Fill monthly_cost_* columns. Only gp3 has a pricing model here; for
    other types we leave the columns at 0.0 to avoid misleading the reader."""
    if row.volume_type != "gp3":
        return
    cur_cost = gp3_monthly_cost(row.size_gib, row.current_iops,
                                row.current_throughput_mibps, pricing)
    row.monthly_cost_current_usd = cur_cost
    if row.target_iops is not None and row.target_throughput_mibps is not None:
        tgt_cost = gp3_monthly_cost(row.size_gib, row.target_iops,
                                    row.target_throughput_mibps, pricing)
        row.monthly_cost_target_usd = tgt_cost
        row.monthly_delta_usd = round(tgt_cost - cur_cost, 2)
        row.annual_delta_usd = round(row.monthly_delta_usd * 12, 2)
    else:
        # No target (orphan or skipped). Leave delta at 0.
        row.monthly_cost_target_usd = cur_cost
        row.monthly_delta_usd = 0.0
        row.annual_delta_usd = 0.0


def _build_rightsizing_row(vol: dict, rec: dict,
                           peak_iops: float, peak_tput: float,
                           n_points: int,
                           args: argparse.Namespace,
                           pricing: Dict[str, float]) -> VolumeReport:
    vol_type = vol["VolumeType"]
    cur_iops = vol.get("Iops", 0)
    cur_tput = vol.get("Throughput", 0)
    attached = ",".join(a.get("InstanceId", "") for a in vol.get("Attachments", []))
    create_time = vol.get("CreateTime")
    create_time_str = create_time.isoformat() if isinstance(create_time, datetime) else str(create_time or "")

    base = VolumeReport(
        volume_id=vol["VolumeId"],
        volume_type=vol_type,
        size_gib=vol["Size"],
        state=vol["State"],
        attached_instances=attached,
        create_time=create_time_str,
        current_iops=cur_iops,
        current_throughput_mibps=cur_tput,
        peak_iops=round(peak_iops, 2),
        peak_throughput_mibps=round(peak_tput, 2),
        target_iops=None,
        target_throughput_mibps=None,
        direction="NONE",
        action="",
        co_finding_reasons=";".join(rec.get("findingReasonCodes", [])) if rec else "",
        notes=f"datapoints={n_points}",
    )

    if vol_type not in TUNABLE_TYPES:
        base.action = f"SKIP - {vol_type} not directly tunable; migrate to gp3 first"
        _populate_cost_columns(base, pricing)
        return base

    if n_points == 0:
        base.action = "SKIP - no CloudWatch data in lookback window"
        _populate_cost_columns(base, pricing)
        return base

    target_iops, target_tput = compute_target(
        vol_type, peak_iops, peak_tput,
        args.buffer, args.min_iops, args.min_throughput,
    )
    base.target_iops = target_iops
    base.target_throughput_mibps = target_tput

    iops_changed = target_iops != cur_iops
    tput_changed = (vol_type == "gp3") and (target_tput != cur_tput)
    if not (iops_changed or tput_changed):
        base.action = "NO_CHANGE - already matches target"
        _populate_cost_columns(base, pricing)
        return base

    delta_iops = target_iops - cur_iops
    delta_tput = (target_tput - cur_tput) if vol_type == "gp3" else 0
    if delta_iops > 0 or delta_tput > 0:
        direction = "UPSIZE"
    elif delta_iops < 0 or delta_tput < 0:
        direction = "DOWNSIZE"
    else:
        direction = "NONE"
    base.direction = direction
    base.action = f"MODIFY {direction} (Δ {delta_iops:+d} IOPS, {delta_tput:+d} MiB/s)"
    _populate_cost_columns(base, pricing)
    return base


def analyze_orphans(ec2, args: argparse.Namespace,
                    scope_ids: Optional[List[str]],
                    pricing: Dict[str, float]) -> List[VolumeReport]:
    log.info("Scanning for orphan (unattached) EBS volumes...")
    orphans = describe_orphan_volumes(ec2)
    if scope_ids is not None:
        orphans = {vid: orphans[vid] for vid in scope_ids if vid in orphans}
    if args.gp3_only:
        before = len(orphans)
        orphans = {vid: v for vid, v in orphans.items() if v.get("VolumeType") == "gp3"}
        dropped = before - len(orphans)
        if dropped:
            log.info("--gp3-only: filtered out %d non-gp3 orphan(s).", dropped)
    log.info("Found %d orphan volume(s).", len(orphans))

    rows: List[VolumeReport] = []
    now = datetime.now(timezone.utc)
    for vid, vol in orphans.items():
        create_time = vol.get("CreateTime")
        age_days: Optional[int] = None
        create_time_str = ""
        if isinstance(create_time, datetime):
            age_days = (now - create_time).days
            create_time_str = create_time.isoformat()

        size = vol.get("Size", 0)
        encrypted = "encrypted" if vol.get("Encrypted") else "unencrypted"
        notes = f"age_days={age_days};{encrypted};type={vol.get('VolumeType')}"

        rows.append(VolumeReport(
            volume_id=vid,
            volume_type=vol.get("VolumeType", "?"),
            size_gib=size,
            state=vol.get("State", "available"),
            attached_instances="",
            create_time=create_time_str,
            current_iops=vol.get("Iops", 0),
            current_throughput_mibps=vol.get("Throughput", 0),
            peak_iops=0.0,
            peak_throughput_mibps=0.0,
            target_iops=None,
            target_throughput_mibps=None,
            direction="NONE",
            action="ORPHAN - unattached; consider snapshot+delete",
            category="orphan",
            notes=notes,
        ))
    # For orphans, "delta" represents potential savings if deleted: target=0
    for r in rows:
        if r.volume_type == "gp3":
            r.monthly_cost_current_usd = gp3_monthly_cost(
                r.size_gib, r.current_iops, r.current_throughput_mibps, pricing)
            r.monthly_cost_target_usd = 0.0
            r.monthly_delta_usd = round(-r.monthly_cost_current_usd, 2)
            r.annual_delta_usd = round(r.monthly_delta_usd * 12, 2)
    return rows


def apply_changes(ec2, rows: List[VolumeReport],
                  scope_ids: Optional[List[str]]) -> None:
    """Apply ModifyVolume to qualifying rows. Mutates row.modification_state."""
    candidates = [r for r in rows if r.action.startswith("MODIFY")]
    if scope_ids is not None:
        scope_set = set(scope_ids)
        candidates = [r for r in candidates if r.volume_id in scope_set]

    if not candidates:
        log.info("Nothing to apply after scoping.")
        return

    busy = get_in_progress_modifications(ec2, [r.volume_id for r in candidates])
    if busy:
        log.warning("%d volume(s) currently mid-modification; will be skipped: %s",
                    len(busy), ", ".join(busy))

    for row in candidates:
        if row.volume_id in busy:
            row.modification_state = f"SKIPPED_BUSY({busy[row.volume_id]})"
            continue
        if row.target_iops is None:
            row.modification_state = "SKIPPED_NO_TARGET"
            continue
        state = modify_volume(
            ec2,
            row.volume_id,
            row.target_iops,
            row.target_throughput_mibps or 0,
            row.volume_type,
        )
        row.modification_state = state or "FAILED"
        log.info("ModifyVolume %s -> %s", row.volume_id, row.modification_state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    errs = validate_args(args)
    if errs:
        for e in errs:
            log.error(e)
        return 2

    scope_ids = load_volume_id_scope(args)
    if scope_ids is not None:
        log.info("Scoped to %d volume ID(s).", len(scope_ids))

    cfg = boto_config()
    session = build_session(args.profile, args.region)
    try:
        sts = session.client("sts", config=cfg)
        account_id = sts.get_caller_identity()["Account"]
    except (ClientError, BotoCoreError) as exc:
        log.error("Could not resolve AWS identity: %s", exc)
        return 1

    log.info(
        "Account=%s region=%s lookback=%dd buffer=%.1f%% apply=%s orphans=%s",
        account_id, args.region, args.days, args.buffer, args.apply,
        "only" if args.orphans_only else ("yes" if args.include_orphans else "no"),
    )

    ec2 = session.client("ec2", config=cfg)
    cw = session.client("cloudwatch", config=cfg)
    co = session.client("compute-optimizer", config=cfg)

    pricing = load_pricing(args)
    log.info("Pricing model (gp3, USD): storage=$%.4f/GiB-mo, "
             "iops=$%.4f/IOP-mo over 3000, throughput=$%.4f/MiB/s-mo over 125",
             pricing["storage_per_gib_month"],
             pricing["iops_per_iop_month_over_3000"],
             pricing["throughput_per_mibps_month_over_125"])

    rows: List[VolumeReport] = []

    if not args.orphans_only:
        rows.extend(analyze_rightsizing(args, ec2, cw, co, account_id, scope_ids, pricing))

    if args.include_orphans or args.orphans_only:
        orphan_rows = analyze_orphans(ec2, args, scope_ids, pricing)
        # Dedup: if a volume appears in both rightsizing and orphan paths
        # (rare but possible), keep the orphan row since deletion is the
        # more impactful recommendation.
        orphan_ids = {o.volume_id for o in orphan_rows}
        rows = [r for r in rows if r.volume_id not in orphan_ids]
        rows.extend(orphan_rows)

    if not rows:
        log.info("No volumes in scope. Nothing to do.")
        return 0

    if args.apply:
        applying_to_all = scope_ids is None and args.apply_all
        modify_count = sum(1 for r in rows if r.action.startswith("MODIFY"))
        if modify_count == 0:
            log.info("No MODIFY rows; nothing to apply.")
        elif not confirm_apply(modify_count, applying_to_all, args.yes):
            log.warning("Apply cancelled by user.")
        else:
            apply_changes(ec2, rows, scope_ids)
    else:
        for r in rows:
            if r.action.startswith("MODIFY"):
                r.modification_state = "DRY_RUN"

    write_report(_sort_rows(rows), args.output)

    # Summary
    counts = {"MODIFY": 0, "NO_CHANGE": 0, "SKIP": 0, "ORPHAN": 0}
    monthly_savings = 0.0
    monthly_added = 0.0
    orphan_savings = 0.0
    for r in rows:
        for key in counts:
            if r.action.startswith(key):
                counts[key] += 1
                break
        if r.category == "orphan":
            orphan_savings += -r.monthly_delta_usd
        elif r.action.startswith("MODIFY"):
            if r.monthly_delta_usd < 0:
                monthly_savings += -r.monthly_delta_usd
            elif r.monthly_delta_usd > 0:
                monthly_added += r.monthly_delta_usd

    log.info("Summary: modify=%d, no_change=%d, skip=%d, orphan=%d",
             counts["MODIFY"], counts["NO_CHANGE"], counts["SKIP"], counts["ORPHAN"])
    log.info("Estimated monthly impact: savings=$%.2f, added=$%.2f, "
             "net=$%+.2f (annualized $%+.2f). Orphan deletion savings=$%.2f/mo.",
             monthly_savings, monthly_added,
             monthly_added - monthly_savings,
             (monthly_added - monthly_savings) * 12,
             orphan_savings)

    if not args.apply and counts["MODIFY"]:
        log.info("Re-run with --apply (and --volume-ids / --apply-all) to push changes.")

    failed = sum(1 for r in rows if r.modification_state == "FAILED")
    return 1 if failed else 0


def _sort_rows(rows: List[VolumeReport]) -> List[VolumeReport]:
    """Sort by attached_instances so volumes on the same EC2 group together.
    Orphans (empty attached_instances) sink to the bottom. Within an instance,
    sort by volume_id for deterministic output."""
    return sorted(
        rows,
        key=lambda r: (
            r.category == "orphan",                # rightsizing first, orphans last
            r.attached_instances == "",            # attached first, unattached after
            r.attached_instances.lower(),
            r.volume_id,
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
