#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# setup.sh — One-shot publish of EBS Right-Sizer to GitHub
# ----------------------------------------------------------------------------
# What this script does:
#   1. Verifies you're inside the repo directory.
#   2. Installs the GitHub CLI (gh) if it's missing.
#   3. Authenticates gh via your browser (one-time).
#   4. Creates a private repo at github.com/<user>/ebs-rightsizer.
#   5. Pushes main + all tags.
#
# It is idempotent: rerunning after success only pushes new commits.
# ----------------------------------------------------------------------------

set -euo pipefail

REPO_NAME="${REPO_NAME:-ebs-rightsizer}"
REPO_DESC="${REPO_DESC:-Operationalize AWS Compute Optimizer EBS recommendations with real CloudWatch usage and dollar-denominated impact}"
VISIBILITY="${VISIBILITY:-private}"   # set to public after review

# ---- helpers ---------------------------------------------------------------
log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

require_repo_root() {
  if [[ ! -d .git ]]; then
    err "Run this from the repo root (must contain .git/)."
    err "Try: cd /Users/jlonapp/Downloads/EBS_Costsavings && ./setup.sh"
    exit 1
  fi
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    err "No commits found. Run 'git log' to investigate."
    exit 1
  fi
}

ensure_gh() {
  if command -v gh >/dev/null 2>&1; then
    log "GitHub CLI already installed: $(gh --version | head -1)"
    return
  fi
  log "GitHub CLI not found. Installing via Homebrew..."
  if ! command -v brew >/dev/null 2>&1; then
    err "Homebrew is required to auto-install gh. Install from https://brew.sh"
    err "Or install gh manually from https://cli.github.com and re-run."
    exit 1
  fi
  brew install gh
}

ensure_gh_auth() {
  if gh auth status >/dev/null 2>&1; then
    local who
    who="$(gh api user --jq .login 2>/dev/null || echo unknown)"
    log "Already authenticated to GitHub as: $who"
    return
  fi
  log "Launching browser-based GitHub login (one-time)..."
  log "Choose: GitHub.com -> HTTPS -> 'Login with a web browser'"
  gh auth login --hostname github.com --git-protocol https --web
}

current_user() {
  gh api user --jq .login
}

ensure_remote_repo() {
  local user owner_repo
  user="$(current_user)"
  owner_repo="${user}/${REPO_NAME}"

  if gh repo view "$owner_repo" >/dev/null 2>&1; then
    log "Remote repo already exists: https://github.com/${owner_repo}"
  else
    log "Creating ${VISIBILITY} repo: ${owner_repo}"
    gh repo create "$owner_repo" \
      --"${VISIBILITY}" \
      --description "$REPO_DESC" \
      --disable-wiki
  fi

  # Wire the local origin remote
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "https://github.com/${owner_repo}.git"
  else
    git remote add origin "https://github.com/${owner_repo}.git"
  fi
  log "origin -> $(git remote get-url origin)"
}

push_main_and_tags() {
  log "Pushing main branch..."
  git push -u origin main

  if git tag -l | grep -q .; then
    log "Pushing tags..."
    git push origin --tags
  else
    warn "No local tags to push."
  fi
}

print_summary() {
  local user owner_repo
  user="$(current_user)"
  owner_repo="${user}/${REPO_NAME}"
  cat <<EOF

============================================================
SUCCESS

Repository: https://github.com/${owner_repo}
Visibility: ${VISIBILITY}

Commits pushed:
$(git log --oneline -5)

Tags pushed:
$(git tag -l | sed 's/^/  /')

Next steps:
  1. Review the repo in your browser.
  2. Share the URL with internal reviewers.
  3. When ready to publish:
       - Make public: Settings -> General -> Change visibility
       - Transfer to aws-samples: Settings -> General -> Transfer ownership
============================================================
EOF
}

# ---- main ------------------------------------------------------------------
main() {
  require_repo_root
  ensure_gh
  ensure_gh_auth
  ensure_remote_repo
  push_main_and_tags
  print_summary
}

main "$@"
