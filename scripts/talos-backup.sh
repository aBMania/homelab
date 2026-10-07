#!/usr/bin/env bash
# Export the node's live machine config and store it SOPS-encrypted (safety net for rebuilding the node).
# The plaintext only exists in a pipe; nothing is printed.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
node=${1:-k8s-control-1}  # file name only; the node itself comes from the talosconfig
out=clusters/main/talos/backup/$node.machineconfig.sops.yaml
mkdir -p "$(dirname "$out")"
talosctl --talosconfig clusters/main/talos/generated/talosconfig get machineconfig v1alpha1 -o jsonpath='{.spec}' \
  | sops encrypt --filename-override "$out" --input-type yaml --output-type yaml /dev/stdin > "$out.tmp"
mv "$out.tmp" "$out"
echo "talos-backup: wrote $out ($(grep -c 'ENC\[' "$out") encrypted values)"
