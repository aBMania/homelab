#!/usr/bin/env bash
# Disaster recovery: hand a fresh Kubernetes API (Talos already bootstrapped) over to Flux.
# Replaces `clustertool talos apply` / `clustertool flux bootstrap`. See AGENTS.md "Disaster recovery".
#
# Usage: scripts/bootstrap.sh [--dry-run]
#   --dry-run  render and server-side dry-run everything against the current cluster, change nothing.
#
# Order matters: Cilium first (no pod networking without it), then Flux, then the secrets and the
# root Kustomizations. Flux reconciles everything else from git.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="--dry-run=server"
K=clusters/main/kubernetes
FLUX_DIR=$K/flux-system/flux
log() { echo "bootstrap: $*" >&2; }
for t in kubectl helm flux sops yq envsubst; do command -v $t >/dev/null || { log "missing $t (run via mise)"; exit 2; }; done
[ -f age.agekey ] || { log "age.agekey missing at repo root (restore it from your backup)"; exit 2; }

# Variables for ${VAR} substitution, decrypted in memory only (never written to disk)
set -a; eval "$(sops decrypt $K/../clusterenv.yaml | yq -r 'to_entries[] | "\(.key)=\(.value | @sh)"')"; CLUSTERNAME=main; set +a

# 1. Cilium, same chart version and values as its HelmRelease
HR=$K/kube-system/cilium/app/helm-release.yaml
CILIUM_VERSION=$(yq -r '.spec.chart.spec.version' $HR)
values=$(mktemp); trap 'rm -f "$values"' EXIT
yq '.spec.values' $HR | envsubst > "$values"
helm repo add cilium https://helm.cilium.io --force-update >/dev/null
if [ -n "$DRY" ]; then
  helm template cilium cilium/cilium --version "$CILIUM_VERSION" -n kube-system -f "$values" >/dev/null
  log "1/4 cilium $CILIUM_VERSION: chart renders with the HelmRelease values (dry-run)"
else
  helm upgrade --install cilium cilium/cilium --version "$CILIUM_VERSION" -n kube-system -f "$values" --wait --timeout 10m
  log "1/4 cilium $CILIUM_VERSION installed"
fi

# 2. Flux controllers + CRDs, same version as repositories/oci/flux-manifests.yaml (that Kustomization then takes over)
FLUX_VERSION=$(yq -r '.spec.ref.tag' repositories/oci/flux-manifests.yaml)
if [ -n "$DRY" ]; then
  flux install --version "$FLUX_VERSION" --export | kubectl apply --server-side --force-conflicts $DRY -f - >/dev/null
  log "2/4 flux $FLUX_VERSION: install manifests accepted (dry-run)"
else
  flux install --version "$FLUX_VERSION"
  log "2/4 flux $FLUX_VERSION installed"
fi

# 3. Namespace, secrets (decrypted in a pipe) and settings
kubectl apply --server-side --force-conflicts $DRY -f $FLUX_DIR/namespace.yaml >/dev/null
for s in sopssecret deploykey clustersettings; do
  sops decrypt $FLUX_DIR/$s.secret.yaml | kubectl apply --server-side --force-conflicts $DRY -f - >/dev/null
done
kubectl apply --server-side --force-conflicts $DRY -f $FLUX_DIR/upgradesettings.yaml >/dev/null
log "3/4 sops-age, deploy-key, cluster-config, upgrade-settings applied${DRY:+ (dry-run)}"

# 4. Sources and the two root Kustomizations (not self-managed by Flux, see AGENTS.md)
kubectl apply --server-side --force-conflicts $DRY -f repositories/oci/flux-manifests.yaml -f repositories/git/this-repo.yaml >/dev/null
kubectl apply --server-side --force-conflicts $DRY -f repositories/flux-entry.yaml -f $K/flux-entry.yaml >/dev/null
log "4/4 sources + flux-entry/flux-entry-repos applied${DRY:+ (dry-run)}"
[ -n "$DRY" ] || log "done. Watch: flux get ks -A --watch"
