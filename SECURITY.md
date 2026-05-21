# Security Posture

This document maps the EBS Cost Optimizer's controls to the **AWS
Well-Architected Framework — Security Pillar** and standard secure-coding
practice for AWS sample utilities.

## Reporting a vulnerability

Do **not** open a public GitHub issue for security problems. Follow the
[AWS Vulnerability Reporting](https://aws.amazon.com/security/vulnerability-reporting/)
process.

## Identity and Access Management (SEC-01, SEC-02, SEC-03)

| Control | Implementation |
|---------|----------------|
| Least privilege | Sample IAM policy in `README.md` grants only the actions the script invokes (CO read, EC2 describe, CW GetMetricData, STS GetCallerIdentity, optional EC2 ModifyVolume on `volume/*`). |
| Credential precedence | Standard boto3 chain only (env vars, shared credentials file, EC2/ECS role, SSO). |
| No CLI credentials | The script never accepts AWS access keys via flags or env-injection so they can't leak into shell history or process listings. |
| Recommended posture | Run from an EC2 instance role or SSO-issued credentials; long-lived IAM user keys are not required. |
| Audit trail | The calling principal ARN is logged at startup so runs can be correlated with CloudTrail. |
| MFA | Inherited from the calling identity. The script does not bypass MFA, IP restrictions, or session-policy boundaries. |

## Detection and audit (SEC-04)

- Calling principal ARN logged at INFO at startup.
- Account ID, region, lookback, buffer, floors, gp3-only mode, direction
  filter, apply flag, and pricing model logged on every run.
- Run manifest embedded as a banner in every output report (CSV and
  XLSX) so reports are self-describing for ticketing and email
  forwarding.
- All AWS API actions are visible in CloudTrail (Compute Optimizer,
  EC2, CloudWatch, STS) under the calling principal.

## Network protection (SEC-05)

- All API traffic uses TLS via the AWS SDK; the script never disables
  certificate validation.
- Connection and read timeouts (`connect_timeout=10`, `read_timeout=60`)
  prevent hung endpoints from indefinitely blocking the run.
- No outbound network calls other than the AWS service endpoints
  required to fulfil the documented IAM scope.

## Compute protection (SEC-06)

- Pure-Python implementation. No native code, no shell-outs, no
  subprocesses.
- No use of `eval`, `exec`, `os.system`, `subprocess.*`, or `pickle`.
- Bandit static-security scanner reports zero findings.

## Data protection (SEC-07, SEC-08, SEC-09)

| Control | Implementation |
|---------|----------------|
| Output file permissions | All report files written with `0o600` (user-only read/write). |
| Atomic writes | Reports are written to `<path>.tmp` then `os.replace` so a crash never leaves a half-written file. |
| Input file size caps | `--pricing-file` and `--volume-ids-file` are capped at 1 MiB each to prevent trivial DoS via giant inputs. |
| Symlink rejection | Both `--pricing-file` and `--volume-ids-file` reject symlinks to avoid following a maliciously placed link to a file the user couldn't otherwise read. |
| CSV/spreadsheet injection guard | Cells starting with `=`, `+`, `-`, `@`, `\t`, or `\r` are prefixed with `'` so Excel/Sheets do not evaluate them as formulas. Numeric cells are exempt to preserve currency formatting. |
| No secrets in logs | The script logs volume IDs, instance IDs, sizes, IOPS, throughput, and cost numbers — no AWS credentials, tokens, or session keys are ever logged or written to disk. |
| No customer data persistence | Reports stay on the local filesystem; the script never uploads, e-mails, or transmits them. |

## Input validation

- `--region` is matched against `^[a-z]{2}-[a-z]+-\d$`.
- `--volume-ids` and entries from `--volume-ids-file` are matched
  against `^vol-[0-9a-f]{8,17}$`.
- `--days` constrained to 1..63 (CloudWatch 5-minute datapoint
  retention limit).
- `--buffer` constrained to 0..500.
- `--min-iops` ≥ 100, `--min-throughput` ≥ 25.
- `--max-workers` constrained to 1..32.
- All pricing values must be non-negative numerics.
- A 5,000-volume scope cap prevents accidental account-wide mass changes
  per run.

## Apply path (defensive defaults)

- **Read-only by default.** `--apply` is rejected unless paired with
  either `--volume-ids` / `--volume-ids-file` (explicit scope) or
  `--apply-all` (explicit opt-in to act on every flagged volume).
- **Interactive confirmation** required before any modification;
  non-interactive sessions require `--yes`.
- **Cooldown awareness.** `DescribeVolumesModifications` is checked
  first; volumes in `modifying` or `optimizing` state are skipped to
  avoid the 6-hour cooldown error.

## Reliability protections (operational safety)

- Adaptive retries with capped attempts (10) so a misbehaving service
  doesn't generate unbounded API calls.
- Bounded parallelism (`--max-workers`, default 8) to stay under
  CloudWatch account-level RPS limits.
- All API calls use boto3's official endpoint resolution (regional, not
  global).

## Static analysis posture

| Tool | Result |
|------|--------|
| `pyflakes` | clean |
| `bandit -ll` | 0 findings |
| `pip-audit -r requirements.txt` | direct dependencies clean; transitive `urllib3` < 2.0 carries known CVEs that are patched only by upgrading to urllib3 2.x. Mitigation: ensure your environment runs `botocore` ≥ 1.36 which pulls urllib3 ≥ 2.0. The repo pins `boto3 >= 1.26` for compatibility; for security-sensitive deployments, install with `pip install --upgrade boto3 urllib3`. |

## What this tool deliberately does **not** do

- Does not write to AWS Systems Manager Parameter Store / AWS Secrets
  Manager.
- Does not call any IAM, KMS, S3, or CloudTrail APIs.
- Does not delete any resource. Orphan volumes are reported only;
  deletion stays a manual step.
- Does not require any third-party SaaS, partner tool, or non-AWS
  network endpoint.
- Does not bypass cooldowns, MFA requirements, IP restrictions, or
  session-policy boundaries inherited from the calling identity.

## Recommended deployment hardening

1. **Run from an EC2 instance role**, not from a developer laptop with
   long-lived IAM user keys.
2. **Scope the IAM policy to the regions you operate in** by adding a
   `Condition` block on `aws:RequestedRegion`.
3. **Restrict `ec2:ModifyVolume` to specific tagged volumes** in
   production (e.g., `aws:ResourceTag/managed-by: ebs-cost-optimizer`).
4. **Pipe CSV reports into a controlled S3 bucket** with bucket
   encryption and explicit tagging if you need to persist them
   centrally — the script does not do this for you.
5. **Pin a known-good `boto3` version** in your environment (e.g.,
   `boto3==1.36.x`) to ensure `urllib3 >= 2.0` is pulled in.
