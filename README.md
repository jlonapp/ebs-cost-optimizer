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
6. Estimates monthly and annual cost impact for each row using a
   configurable gp3 pricing model.
7. Optionally lists orphan volumes (state `available`) with monthly cost as
   the savings if deleted.
8. Sorts the report by attached EC2 instance so volumes on the same host
   group together; orphans appear last.
9. Writes a CSV report. With `--apply` plus an explicit scope, calls
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

## Pricing model

Each row carries four cost columns so the financial impact is visible per
volume and at the run-level summary:

| Column | Meaning |
|--------|---------|
| `monthly_cost_current_usd` | Estimated monthly cost at current provisioning |
| `monthly_cost_target_usd` | Estimated monthly cost at recommended target |
| `monthly_delta_usd` | `target - current`. Negative = savings. |
| `annual_delta_usd` | `monthly_delta * 12` |

For orphans, `target_usd` is `0` and the delta represents savings if the
volume is deleted.

The default gp3 prices are AWS list prices for `us-east-1` as of late 2025:

| Component | Default | Source |
|-----------|---------|--------|
| Storage | $0.08 / GiB-month | AWS list price |
| IOPS over 3000 baseline | $0.005 / IOP-month | AWS list price |
| Throughput over 125 baseline | $0.04 / MiB/s-month | AWS list price |

Override defaults to match your customer's billing rate via CLI flags:

```bash
python ebs_rightsizer.py --region us-east-1 \
    --price-storage 0.075 \
    --price-iops 0.0045 \
    --price-throughput 0.036
```

Or via JSON file:

```json
{
  "storage_per_gib_month": 0.075,
  "iops_per_iop_month_over_3000": 0.0045,
  "throughput_per_mibps_month_over_125": 0.036
}
```

```bash
python ebs_rightsizer.py --region us-east-1 \
    --pricing-file ./customer_prices.json
```

The summary line at the end of every run reports total monthly savings,
total monthly added cost (for upsize recommendations), and the annualized
net impact:

```
Estimated monthly impact: savings=$65.00, added=$0.00,
net=$-65.00 (annualized $-780.00). Orphan deletion savings=$4.00/mo.
```

## CSV row order

Rows are sorted by `attached_instances` so all volumes on the same EC2
instance group together. Within an instance, rows are sorted by `volume_id`
for deterministic output. Orphan (unattached) volumes appear at the bottom.

### Per-instance subtotals (default ON)

After each instance's volume group, a `SUBTOTAL` row rolls up:

- `monthly_cost_current_usd` (sum)
- `monthly_cost_target_usd` (sum)
- `monthly_delta_usd` (sum, negative = savings)
- `annual_delta_usd` (sum × 12)

Orphan volumes get a single combined `ORPHAN_SUBTOTAL` row, and a final
`GRAND_TOTAL` row sums the entire report. The `volume_id` column carries
the literal string `SUBTOTAL`, `ORPHAN_SUBTOTAL`, or `GRAND_TOTAL` so they
are trivial to filter or pivot in Excel.

To disable totals (e.g., for downstream tooling that only wants leaf rows):

```bash
python ebs_rightsizer.py --region us-east-1 --no-subtotals
```

Sample with subtotals:

```
volume_id,...,attached_instances,...,monthly_delta_usd,annual_delta_usd,...
vol-bbbb...,...,i-aaaa,...,-15.00,-180.00,...
vol-cccc...,...,i-aaaa,...,0.00,0.00,...
SUBTOTAL,...,i-aaaa,...,-15.00,-180.00,...
vol-aaaa...,...,i-bbbb,...,-50.00,-600.00,...
SUBTOTAL,...,i-bbbb,...,-50.00,-600.00,...
vol-dddd...,...,,...,-4.00,-48.00,...
ORPHAN_SUBTOTAL,...,(orphan),...,-4.00,-48.00,...
GRAND_TOTAL,...,,...,-69.00,-828.00,...
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
| `--no-subtotals` | off (subtotals on) | Disable per-instance subtotal rows |
| `--pricing-file` | none | JSON file with gp3 prices (see Pricing) |
| `--price-storage` | 0.08 | Override $/GiB-month |
| `--price-iops` | 0.005 | Override $/IOP-month above 3000 |
| `--price-throughput` | 0.04 | Override $/MiB/s-month above 125 |
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
volume_id,volume_type,size_gib,state,attached_instances,create_time,current_iops,current_throughput_mibps,peak_iops,peak_throughput_mibps,target_iops,target_throughput_mibps,direction,action,monthly_cost_current_usd,monthly_cost_target_usd,monthly_delta_usd,annual_delta_usd,modification_state,co_finding_reasons,category,notes
vol-bbbb...,gp3,200,in-use,i-aaaa,...,6000,250,50,10,3000,125,DOWNSIZE,"MODIFY DOWNSIZE (Δ -3000 IOPS, -125 MiB/s)",36.0,16.0,-20.0,-240.0,DRY_RUN,,rightsizing,datapoints=8640
vol-cccc...,gp3,100,in-use,i-aaaa,...,3000,125,0,0,3000,125,NONE,NO_CHANGE - already matches target,8.0,8.0,0.0,0.0,,,rightsizing,datapoints=8640
vol-aaaa...,gp3,500,in-use,i-bbbb,...,9000,500,50,10,3000,125,DOWNSIZE,"MODIFY DOWNSIZE (Δ -6000 IOPS, -375 MiB/s)",85.0,40.0,-45.0,-540.0,DRY_RUN,,rightsizing,datapoints=8640
vol-dddd...,gp3,50,available,,...,3000,125,0,0,,,NONE,ORPHAN - unattached; consider snapshot+delete,4.0,0.0,-4.0,-48.0,,,orphan,age_days=400;encrypted;type=gp3
```

The `direction` column lets you filter UPSIZE / DOWNSIZE in Excel. The
`monthly_delta_usd` column lets you sum savings across the report or sort
by financial impact.

## Extending

- **Multi-account / org-wide:** wrap the boto3 session in an STS `AssumeRole`
  loop over all accounts in the org and aggregate the CSVs.
- **Slack / SNS notifications:** hook after each `MODIFY` log line.
- **Lambda + EventBridge:** schedule weekly to keep volumes right-sized as
  workloads evolve.
- **Tag-based exclusions:** filter `findings` by a `do-not-modify` tag.
