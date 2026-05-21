# Amazon EBS Right-Sizer

> **Operationalize AWS Compute Optimizer EBS recommendations with real
> CloudWatch usage data, dollar-denominated impact, and safe automated
> remediation.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)

## Overview

AWS Compute Optimizer surfaces over- and under-provisioned EBS volumes, but
acting on those findings at scale is a manual, per-volume process today.
This utility closes that loop end-to-end:

1. Pulls every Compute Optimizer EBS recommendation flagged `NotOptimized`.
2. Validates each recommendation against **30 days of actual workload data**
   from CloudWatch (peak IOPS and throughput).
3. Calculates a right-sized target with a configurable safety buffer,
   floored at the gp3 baseline (3000 IOPS / 125 MiB/s) and capped at the
   volume type's service ceiling.
4. Estimates **monthly and annual cost impact in USD** using a configurable
   pricing model (defaults to AWS list prices).
5. Produces a CSV report sorted by attached EC2 instance with per-instance
   subtotals, an orphan-volume section, and a grand total.
6. With explicit opt-in, applies the changes via `ec2:ModifyVolume` (online,
   no detach, no I/O pause).

## Why this is helpful

Compute Optimizer dashboards routinely show tens of thousands of dollars in
flagged EBS savings, but customers struggle to act on them because:

- **Recommendations are heuristic.** Customers want to verify against real
  workload peaks before reducing provisioned IOPS or throughput.
- **Per-volume console actions don't scale.** Modifying 300 volumes
  manually is a multi-day exercise prone to errors.
- **Financial impact isn't visible.** The dashboard shows aggregate dollars
  but not per-volume or per-instance numbers stakeholders can act on.
- **Orphan volumes are an adjacent problem.** Unattached `available`
  volumes are pure waste and rarely get cleaned up.

This tool addresses all four in a single, auditable run.

## Use cases

### Cost optimization (downsize)

Identify gp3 volumes provisioned with IOPS or throughput well above their
30-day peak, generate a per-volume savings estimate, and apply right-sized
values during a controlled change window.

```bash
python3 ebs_rightsizer.py --region us-east-1 --direction downsize
```

The CSV gives you `monthly_delta_usd` per volume, a `SUBTOTAL` per EC2
instance, and a `GRAND_TOTAL` row at the bottom. Hand it to FinOps for
sign-off, then:

```bash
python3 ebs_rightsizer.py --region us-east-1 --direction downsize \
    --volume-ids-file approved.txt --apply
```

### Performance remediation (upsize)

Surface volumes whose workload is being throttled by a low IOPS or
throughput ceiling. The script flags these as `MODIFY UPSIZE` rows so app
owners can react before users notice.

```bash
python3 ebs_rightsizer.py --region us-east-1 --direction upsize
```

### Idle resource cleanup (orphans)

Inventory unattached `available` volumes with their age, encryption status,
and the monthly cost they represent. The tool reports only — deletion stays
a manual step, gated by your snapshot and change-management process.

```bash
python3 ebs_rightsizer.py --region us-east-1 --orphans-only --output orphans.csv
```

### Multi-account governance

Wrap the script in an STS `AssumeRole` loop across organization member
accounts to produce a consolidated report from the management account or a
delegated admin. The script is per-account by design so the apply path stays
auditable per account boundary.

## How it works

### Data flow

```
┌────────────────────────┐
│ AWS Compute Optimizer  │  GetEBSVolumeRecommendations (paginated)
└──────────┬─────────────┘
           │  NotOptimized findings
           ▼
┌────────────────────────┐
│ Amazon CloudWatch      │  GetMetricData (single call per volume,
└──────────┬─────────────┘    all 4 EBS metrics, 5-minute resolution)
           │  peak IOPS + throughput
           ▼
┌────────────────────────┐
│ Amazon EC2             │  DescribeVolumes, DescribeVolumesModifications
└──────────┬─────────────┘
           │  current Iops/Throughput, cooldown state
           ▼
┌────────────────────────┐
│ Right-sizing engine    │  target = max(floor, peak × (1 + buffer))
│ Cost engine            │           capped at type service limit
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────┐
│ CSV report             │  sorted by attached_instances,
└──────────┬─────────────┘    subtotals + grand total
           │
           ▼  (with --apply)
┌────────────────────────┐
│ ec2:ModifyVolume       │  online change, no detach
└────────────────────────┘
```

### Volume types

By default, the script focuses on **gp3** since IOPS and throughput are
independently tunable for cost optimization. Use `--all-types` to widen.

| Type    | IOPS tunable     | Throughput tunable | Default behavior |
|---------|------------------|---------------------|------------------|
| gp3     | yes              | yes                 | full right-size  |
| io1     | yes              | no                  | filtered out     |
| io2     | yes              | no                  | filtered out     |
| gp2     | no (size-bound)  | no                  | filtered out     |
| st1/sc1 | no               | no                  | filtered out     |

## Prerequisites

- **Python** 3.8 or later
- **boto3** 1.26+ (AWS SDK)
- **openpyxl** 3.0+ — required only when generating `.xlsx` reports
- **AWS credentials** with the IAM permissions listed below
- **AWS Compute Optimizer opted in** for the target account
  ([documentation](https://docs.aws.amazon.com/compute-optimizer/latest/ug/getting-started.html))

### Install the dependencies

Pick whichever fits your environment.

**Amazon Linux 2023** (common on EC2):
```bash
sudo dnf install -y python3-boto3 python3-openpyxl
```

**Amazon Linux 2:**
```bash
sudo yum install -y python3-boto3 python3-openpyxl
```

**Debian / Ubuntu:**
```bash
sudo apt-get install -y python3-boto3 python3-openpyxl
```

**RHEL / CentOS / Rocky:**
```bash
sudo yum install -y python3-boto3 python3-openpyxl
```

**macOS / generic Linux with pip:**
```bash
pip3 install --user -r requirements.txt
```

If `pip3` itself is missing on Amazon Linux:
```bash
sudo dnf install -y python3-pip   # AL2023
sudo yum install -y python3-pip   # AL2
```

`openpyxl` is optional. Skip it if you only want CSV output. The script
will detect it's missing and emit a platform-specific install hint if you
ask for `.xlsx` without it installed.

### Required IAM permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "compute-optimizer:GetEBSVolumeRecommendations",
        "ec2:DescribeVolumes",
        "ec2:DescribeVolumesModifications",
        "cloudwatch:GetMetricData",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ApplyOnly",
      "Effect": "Allow",
      "Action": ["ec2:ModifyVolume"],
      "Resource": "arn:aws:ec2:*:*:volume/*"
    }
  ]
}
```

For read-only operation, omit the `ApplyOnly` statement.

## Quickstart

```bash
git clone https://github.com/aws-samples/ebs-rightsizer.git
cd ebs-rightsizer
pip install -r requirements.txt

# Read-only report
python3 ebs_rightsizer.py --region us-east-1 --include-orphans
```

## Common workflows

```bash
# Cost-savings only (drop upsize recommendations)
python3 ebs_rightsizer.py --region us-east-1 --direction downsize

# Performance fixes only
python3 ebs_rightsizer.py --region us-east-1 --direction upsize

# Override default pricing with customer billing rates
python3 ebs_rightsizer.py --region us-east-1 \
    --price-storage 0.075 --price-iops 0.0045 --price-throughput 0.036

# Apply to a vetted list (preferred apply path)
python3 ebs_rightsizer.py --region us-east-1 \
    --volume-ids-file approved.txt --apply

# Apply to every flagged volume in scope (must opt in)
python3 ebs_rightsizer.py --region us-east-1 --apply --apply-all
```

## CLI reference

| Flag | Default | Purpose |
|------|---------|---------|
| `--region` | required | AWS region |
| `--profile` | none | AWS CLI profile |
| `--days` | 30 | CloudWatch lookback (1-63) |
| `--buffer` | 20 | Headroom percentage above peak |
| `--min-iops` | 3000 | Floor for IOPS (gp3 baseline) |
| `--min-throughput` | 125 | Floor for throughput MiB/s |
| `--max-workers` | 8 | Parallel CloudWatch threads |
| `--output` | `ebs_rightsizing_report.csv` | Report path. Use `.xlsx` for styled Excel |
| `--no-timestamp` | off (timestamps on) | Disable UTC timestamp suffix in filename |
| `--no-subtotals` | off (subtotals on) | Disable per-instance subtotal rows |
| `--pricing-file` | none | JSON file with gp3 prices |
| `--price-storage` | 0.08 | Override $/GiB-month |
| `--price-iops` | 0.005 | Override $/IOP-month above 3000 |
| `--price-throughput` | 0.04 | Override $/MiB/s-month above 125 |
| `--gp3-only` | **on** | Focus only on gp3 volumes |
| `--all-types` | off | Include io1/io2/gp2 |
| `--direction` | `both` | `both` / `upsize` / `downsize` |
| `--include-orphans` | off | Append unattached volumes |
| `--orphans-only` | off | Skip CO findings, just list orphans |
| `--volume-ids` | none | Limit scope to these IDs |
| `--volume-ids-file` | none | Same, read from file |
| `--apply` | off | Call `ec2:ModifyVolume` |
| `--apply-all` | off | Required for `--apply` without an ID list |
| `--yes` | off | Skip interactive confirm prompt |

## Output formats

The script produces either a CSV (default) or a styled Excel workbook
based on the file extension passed to `--output`.

### Run manifest header

Every report (CSV and XLSX) carries a manifest banner at the top with full
run provenance:

```
Generated 2026-05-21T18:23:00Z by ebs-rightsizer v1.5.0 |
Account 711457211352 | Region us-east-1
Parameters: lookback=30d, buffer=20.0%, floors=3000 IOPS / 125 MiB/s,
gp3-only=True, direction=both, applied=False
Pricing (gp3 USD): storage=$0.08/GiB-mo, iops=$0.005/IOP-mo over 3000,
throughput=$0.04/MiB/s-mo over 125
```

This makes a report self-describing: anyone receiving the file by email or
in a ticket can see exactly which account, region, parameters, and pricing
model produced it. In CSV, banner lines start with `#` so most tools ignore
them by default. In XLSX, the banner sits in merged rows above the column
headers.

### Output filename and timestamps

Every report filename is automatically suffixed with a UTC timestamp so
runs never overwrite each other and chronological sorting works
correctly:

```
report.xlsx              ->  report_20260521T182300Z.xlsx
ebs_rightsizing_report.csv -> ebs_rightsizing_report_20260521T182300Z.csv
reports/q2-audit.csv     ->  reports/q2-audit_20260521T182300Z.csv
```

For explicit positioning, use the `{ts}` placeholder:
```bash
python3 ebs_rightsizer.py --region us-east-1 --output snapshot_{ts}.xlsx
# -> snapshot_20260521T182300Z.xlsx
```

To disable timestamping (overwrite same filename each run):
```bash
python3 ebs_rightsizer.py --region us-east-1 --no-timestamp --output report.csv
```

### CSV (default)

Plain text, suitable for downstream tooling, scripting, and version control.

```bash
python3 ebs_rightsizer.py --region us-east-1 --output report.csv
```

### Styled Excel workbook (.xlsx)

Professional-grade workbook with frozen header, AutoFilter on every column,
USD currency formatting (negative values in red), color-coded direction
(green=downsize, yellow=upsize, peach=orphan, grey=no-change/skip), and
visually distinct SUBTOTAL (pale blue) and GRAND_TOTAL (navy) rows.

```bash
python3 ebs_rightsizer.py --region us-east-1 --output report.xlsx
```

Requires `openpyxl` (see Prerequisites above for install commands).

## Output schema

| Column | Description |
|--------|-------------|
| `volume_id` | EBS volume ID, or `SUBTOTAL` / `ORPHAN_SUBTOTAL` / `GRAND_TOTAL` |
| `volume_type` | `gp3`, `io1`, etc. |
| `size_gib` | Volume size |
| `state` | `in-use`, `available`, etc. |
| `attached_instances` | Comma-separated EC2 instance IDs |
| `create_time` | ISO 8601 |
| `current_iops` / `current_throughput_mibps` | Current provisioning |
| `peak_iops` / `peak_throughput_mibps` | Observed peak over `--days` window |
| `target_iops` / `target_throughput_mibps` | Recommended target |
| `direction` | `UPSIZE`, `DOWNSIZE`, or `NONE` |
| `action` | Human-readable summary |
| `monthly_cost_current_usd` | Estimated monthly cost at current settings |
| `monthly_cost_target_usd` | Estimated monthly cost at target |
| `monthly_delta_usd` | `target − current`. Negative = savings |
| `annual_delta_usd` | `monthly_delta × 12` |
| `modification_state` | Result of `ModifyVolume` (or `DRY_RUN`) |
| `co_finding_reasons` | Compute Optimizer reason codes |
| `category` | `rightsizing`, `orphan`, `subtotal`, or `total` |
| `notes` | Datapoint count, orphan age, encryption status |

## Safety controls

- **Read-only by default.** `--apply` requires either `--volume-ids` /
  `--volume-ids-file` or explicit `--apply-all`.
- **Interactive confirmation** before any modification. Non-interactive
  sessions require `--yes`.
- **Cooldown awareness.** Skips volumes already mid-modification (the AWS
  6-hour cooldown).
- **Adaptive retries** for Compute Optimizer and CloudWatch throttling.
- **CSV injection guard** for spreadsheet safety.
- **Atomic file writes** so a crash never produces a half-written CSV.
- **Strict input validation** for region, volume IDs, and numeric ranges.
- **Bounded parallelism** to stay under CloudWatch account RPS limits.

## Limitations

- AWS Compute Optimizer takes ~24h after opt-in to populate findings, and
  ~14d to populate `findingReasonCodes` for new volumes.
- CloudWatch retains 5-minute datapoints for **63 days**; lookback is
  capped at 63.
- The pricing model covers gp3 only. For io1/io2/gp2/st1/sc1, the cost
  columns remain at 0; widen the model in `DEFAULT_PRICING_*` if needed.
- AWS only allows one `ModifyVolume` per volume every 6 hours. Re-runs
  within that window will skip cooldown-bound volumes.
- Per-account by design. Multi-account scans require an STS `AssumeRole`
  wrapper.

## Roadmap

- Multi-account / Organizations-wide aggregator with `AssumeRole` loop.
- Tag-based exclusion (`do-not-modify=true`).
- AWS Lambda packaging with EventBridge schedule and SNS notifications.
- Slack / Microsoft Teams notifier hook.
- Optional Cost Explorer integration to pull actual blended rates per
  account.

## Security

See [CONTRIBUTING.md#security-issue-notifications](CONTRIBUTING.md) for
information about reporting vulnerabilities.

## License

This project is licensed under the Apache-2.0 License. See [LICENSE](LICENSE).

## Disclaimer

This is sample code intended to demonstrate AWS service capabilities. Test
in a non-production environment before applying changes to production
volumes. The pricing defaults are illustrative; verify against your AWS
billing rates before relying on the financial impact figures.
