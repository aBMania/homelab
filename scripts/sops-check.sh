#!/usr/bin/env bash
# Fail if any file that .sops.yaml says must be encrypted is not (replaces `clustertool adv precommit`).
# Usage: scripts/sops-check.sh [--staged]   (--staged: only files staged for commit, for the git hook)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
if [ "${1:-}" = "--staged" ]; then
  files=$(git diff --cached --name-only --diff-filter=ACMR)
else
  files=$(git ls-files)
fi
command -v yq >/dev/null && command -v sops >/dev/null || { echo 'sops-check: yq and sops are required (run via mise)' >&2; exit 2; }
mapfile -t regexes < <(yq -r '.creation_rules[].path_regex' .sops.yaml)
bad=0; checked=0
while IFS= read -r f; do
  [ -f "$f" ] || continue
  for re in "${regexes[@]}"; do
    if [[ "$f" =~ $re ]]; then
      checked=$((checked+1))
      if ! sops filestatus "$f" 2>/dev/null | grep -q '"encrypted":true'; then
        echo "NOT ENCRYPTED: $f" >&2; bad=1
      fi
      break
    fi
  done
done <<< "$files"
[ "${#regexes[@]}" -gt 0 ] || { echo "sops-check: no creation_rules found in .sops.yaml" >&2; exit 2; }
[ "$bad" = 0 ] && echo "sops-check: $checked file(s) matching .sops.yaml are encrypted" || { echo "sops-check: refusing, encrypt with: sops encrypt -i <file>" >&2; exit 1; }
