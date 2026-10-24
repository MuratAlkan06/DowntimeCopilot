#!/usr/bin/env bash
# Secret / probe guard. Run before every commit and in CI:  bash tools/guard.sh
# Exits nonzero with a clear message if any check fails.
set -u

cd "$(git rev-parse --show-toplevel)" || { echo "guard: not inside a git repo" >&2; exit 2; }

fail=0

# 1. The Bedrock probe is distributed by DM and must never be tracked.
if git ls-files --error-unmatch tools/bedrock_probe.py >/dev/null 2>&1; then
  echo "guard FAIL [probe-tracked]: tools/bedrock_probe.py is tracked; run 'git rm --cached tools/bedrock_probe.py'" >&2
  fail=1
fi

# 2. No AWS credential material in the index (staged or committed).
#    Key IDs are matched case-sensitively; the secret-key assignment case-insensitively
#    so a pasted .env line (AWS_SECRET_ACCESS_KEY=...) is caught too.
key_hits=$(git grep --cached -nIE 'AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}' -- . ':!tools/guard.sh' 2>/dev/null)
secret_hits=$(git grep --cached -nIiE 'aws_secret_access_key *= *[A-Za-z0-9/+=]{40}' -- . ':!tools/guard.sh' 2>/dev/null)
if [ -n "$key_hits$secret_hits" ]; then
  echo "guard FAIL [credentials]: AWS credential material found in the index at:" >&2
  # Print file:line only, never the matched value.
  printf '%s\n%s\n' "$key_hits" "$secret_hits" | sed -E 's/^([^:]+:[0-9]+):.*/  \1/' | grep -v '^$' >&2
  fail=1
fi

# 3. Probe results must be redacted (no account IDs, no ARNs with account segments).
#    Portable form of 'arn:aws:[^ ]*:[0-9]{12}|\b[0-9]{12}\b' (works on BSD and GNU grep).
if [ -f docs/probe-results.md ]; then
  if grep -nE 'arn:aws:[^ ]*:[0-9]{12}|(^|[^0-9])[0-9]{12}([^0-9]|$)' docs/probe-results.md >/dev/null; then
    echo "guard FAIL [unredacted]: docs/probe-results.md contains an AWS account ID or ARN account segment; replace with [REDACTED]" >&2
    fail=1
  fi
fi

# 4. Committed eval bundles (evals/baseline/) get the same redaction check as the probe results.
if [ -d evals/baseline ]; then
  if grep -rnE 'arn:aws:[^ ]*:[0-9]{12}|(^|[^0-9])[0-9]{12}([^0-9]|$)' evals/baseline >/dev/null; then
    echo "guard FAIL [unredacted]: evals/baseline/ contains an AWS account ID or ARN account segment; re-scrub the bundle" >&2
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi
echo "guard OK: probe untracked, no credentials in index, probe results and eval baseline redacted"
