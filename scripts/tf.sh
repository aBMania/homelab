#!/usr/bin/env bash
# Run terraform with SOPS-encrypted state committed in git.
# Usage: scripts/tf.sh <dir> <terraform args...>   e.g. scripts/tf.sh terraform/cloudflare plan
# The plaintext terraform.tfstate only exists while terraform runs. It is re-encrypted
# to terraform.tfstate.sops.json afterwards, even if terraform fails.
set -euo pipefail
root=$(git rev-parse --show-toplevel)
dir=${1:?usage: tf.sh <dir> <terraform args...>}; shift
cd "$root"
plain="$dir/terraform.tfstate"
enc="$dir/terraform.tfstate.sops.json"
export SOPS_AGE_KEY_FILE=${SOPS_AGE_KEY_FILE:-$root/age.agekey}

if [ -f "$enc" ]; then
  sops decrypt --input-type json --output-type json "$enc" > "$plain"
fi

finish() {
  if [ -s "$plain" ]; then
    if [ ! -f "$enc" ] || ! cmp -s "$plain" <(sops decrypt --input-type json --output-type json "$enc"); then
      sops encrypt --input-type json --output-type json --filename-override "$enc" "$plain" > "$enc.tmp"
      mv "$enc.tmp" "$enc"
      echo "tf.sh: state changed, re-encrypted $enc (commit it)" >&2
    fi
  fi
  rm -f "$plain" "$plain.backup"
}
trap finish EXIT

# Fresh checkout: install providers first (pinned by .terraform.lock.hcl)
if [ ! -d "$dir/.terraform" ] && [ "${1:-}" != "init" ]; then
  terraform -chdir="$dir" init -input=false >/dev/null
fi

terraform -chdir="$dir" "$@"
