# EBS Right-Sizer

Closes the loop between **AWS Compute Optimizer** EBS findings and actual
**CloudWatch** usage history, then optionally applies new IOPS / throughput
values via `ec2:ModifyVolume`. Also reports orphan (unattached) volumes that
typically dominate idle-resource spend.

## What it does

1. Pulls Compute Optimizer `GetEBSVolumeRecommendations` for every volume
   flagged `NotOptimized`.
2. Queries CloudWatch `AWS/EBS` metrics
   (`VolumeReadOps`, `VolumeWriteOps`, `VolumeReadBytes`, `VolumeWriteBytes`)
   over the last N days at native 5-minute resolution.
3. Computes peak IOPS and peak throughput (MiB/s).
4. Compares against current `Iops` / `Throughput` from `DescribeVolumes`.
5. Calculates target = `peak * (1 + buffer%)`, floored at gp3 baseline,
   capped at the volume type's service limit.
6. Optionally lists orphan volumes (state `available`).
7. Writes a CSV report. With `--apply` plus an explicit scope, calls
   `ec2:ModifyVolume`.

## Volume types

By default, the script focuses on **gp3 only** since that's where IOPS and
throughput are independently tunable for cost optimization. Use `--all-types`
to widen the scan.

| Type    | IOPS tunable     | Throughput tunable | Default behavior |
|---------|------------------|---------------------|------------------|
| gp3     | yes              | yes                 | full right-size  |
| io1     | yes              | no                  | filtered out (use `--all-types`) |
| io2     | yes              | no                  | filtered out (use `--all-types`) |
| gp2     | no (size-bound)  | no                  | filtered out (use `--all-types`) |
| st1/sc1 | no               | no                  | filtered out (use `--all-types`) |

## Install

```bash
pip install -r requirements.txt
```

## Common workflows

Read-only report (gp3 only, both directions):

```bash
python ebs_rightsizer.py --region us-east-1
```

Cost-savings only (drop upsize recommendations):

```bash
python ebs_rightsizer.py --region us-east-1 --direction downsize
```

Performance fixes only (drop downsize recommendations):

```bash
python ebs_rightsizer.py --region us-east-1 --direction upsize
```

Include orphan volumes in the same report:

```bash
python ebs_rightsizer.py --region us-east-1 --include-orphans
```

Orphan-only report:

```bash
python ebs_rightsizer.py --region us-east-1 --orphans-only \
    --output orphans.csv
```

Widen scan to io1 / io2 / gp2:

```bash
python ebs_rightsizer.py --region us-east-1 --all-types
```

Apply downsize-only to a vetted list (preferred apply path):

```bash
python ebs_rightsizer.py --region us-east-1 \
    --direction downsize \
    --volume-ids-file ./approved_volumes.txt \
    --apply
```

`approved_volumes.txt` is one volume ID per line; `#` lines are comments.

Apply to every Compute-Optimizer-flagged volume (must opt in explicitly):

```bash
python ebs_rightsizer.py --region us-east-1 --apply --apply-all
```

Tune lookback and buffer:

```bash
python ebs_rightsizer.py --region us-east-1 --days 40 --buffer 30
```

## Apply safety model

- `--apply` alone is rejected. You must combine it with **either**
  `--volume-ids` / `--volume-ids-file` (a specific scope) **or** `--apply-all`
  (explicit opt-in to act on every flagged volume).
- Interactive confirmation is required unless `--yes` is passed.
- In non-interactive sessions `--yes` is mandatory.
- Dry-run is the default in every other case.
- The script checks `DescribeVolumesModifications` and skips volumes that are
  already mid-modification (avoids the 6-hour cooldown error).

## CLI reference

| Flag | Default | Purpose |
|------|---------|---------|
| `--region` | required | AWS region |
| `--profile` | none | AWS CLI profile |
| `--days` | 30 | CloudWatch lookback (1-63) |
| `--buffer` | 20 | Headroom percentage above peak |
| `--min-iops` | 3000 | Floor for IOPS (gp3 baseline; never goes below) |
| `--min-throughput` | 125 | Floor for throughput MiB/s (never goes below) |
| `--max-workers` | 8 | Parallel CloudWatch threads |
| `--output` | `ebs_rightsizing_report.csv` | Report path |
| `--gp3-only` | **on** | Focus only on gp3 volumes |
| `--all-types` | off | Override `--gp3-only` to include io1/io2/gp2 |
| `--direction` | `both` | `both` / `upsize` / `downsize` filter for MODIFY rows |
| `--include-orphans` | off | Append unattached volumes to report |
| `--orphans-only` | off | Skip CO findings, just list orphans |
| `--volume-ids` | none | Limit scope to these IDs |
| `--volume-ids-file` | none | Same, but read from file |
| `--apply` | off | Call `ec2:ModifyVolume` on in-scope MODIFY rows |
| `--apply-all` | off | Required for `--apply` without an ID list |
| `--yes` | off | Skip interactive confirm prompt |

## Required IAM

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

For read-only operation, drop the `ApplyOnly` statement.

## Operational notes

- **Compute Optimizer must be opted in.** Enable in the account (or org via
  the management account); allow ~24h after opt-in for findings to populate.
- **EBS metric resolution is 5 minutes** unless detailed monitoring is on.
  The script uses `Period=300` and converts `Sum` to a per-second rate, which
  matches how Compute Optimizer normalizes peak utilization.
- **CloudWatch retention.** 5-minute datapoints are kept for 63 days. The
  script enforces `--days <= 63`.
- **ModifyVolume cooldown.** AWS only allows another modification on the same
  volume every 6 hours. The script proactively skips volumes whose previous
  modification is still `modifying` or `optimizing`.
- **Throughput parameter is gp3-only.** For io1/io2 the script only adjusts
  IOPS.
- **gp2 volumes** are reported but skipped for modification because IOPS on
  gp2 is bound to volume size. Migrate to gp3 first.
- **Orphan volumes.** The script flags them with age and encryption status.
  Deletion is not automated. Snapshot first, then delete via your normal
  change process.

## Security and best-practice posture

- **Read-only by default.** Apply requires both `--apply` and an explicit
  scope flag.
- **Interactive confirmation** before any `ModifyVolume` call.
- **Adaptive retries** via `botocore.config.Config(retries='adaptive')` to
  handle Compute Optimizer and CloudWatch throttling cleanly.
- **CSV injection guard.** Cells starting with `=`, `+`, `-`, `@`, `\t`, `\r`
  are prefixed with `'` so spreadsheet apps don't execute formulas.
- **Atomic file writes** (write to `.tmp`, then `os.replace`) so a crash
  mid-run doesn't leave a corrupt CSV.
- **Strict input validation** for region, volume IDs, lookback bounds, and
  numeric ranges.
- **No secrets in logs.** Only volume IDs, sizes, and metric values are
  emitted.
- **Bounded parallelism** (`--max-workers`, default 8) to stay well under
  CloudWatch account RPS limits.
- **Least-privilege IAM** sample provided above.
- **No outbound network calls** other than AWS APIs.

## Output sample

```
volume_id,volume_type,size_gib,state,attached_instances,create_time,current_iops,current_throughput_mibps,peak_iops,peak_throughput_mibps,target_iops,target_throughput_mibps,direction,action,modification_state,co_finding_reasons,category,notes
vol-0abc...,gp3,500,in-use,i-0123,2024-08-12T03:11:00+00:00,9000,500,1240.5,180.2,3000,250,DOWNSIZE,"MODIFY DOWNSIZE (Δ -6000 IOPS, -250 MiB/s)",DRY_RUN,VolumeIOPSOverProvisioned,rightsizing,datapoints=8640
vol-0xyz...,gp3,75,in-use,i-0456,2026-05-14T02:48:00+00:00,3000,125,857.5,159.07,3000,200,UPSIZE,"MODIFY UPSIZE (Δ +0 IOPS, +75 MiB/s)",DRY_RUN,VolumeThroughputUnderProvisioned,rightsizing,datapoints=6548
```

The `direction` column makes it trivial to pivot the CSV in Excel: filter on
`UPSIZE` for performance fixes, `DOWNSIZE` for cost savings.

## Extending

- **Multi-account / org-wide:** wrap the boto3 session in an STS `AssumeRole`
  loop over all accounts in the org and aggregate the CSVs.
- **Slack / SNS notifications:** hook after each `MODIFY` log line.
- **Lambda + EventBridge:** schedule weekly to keep volumes right-sized as
  workloads evolve.
- **Tag-based exclusions:** filter `findings` by a `do-not-modify` tag.
