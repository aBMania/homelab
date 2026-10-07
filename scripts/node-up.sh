#!/usr/bin/env bash
# Undo scripts/node-down.sh after the node is back: uncordon, wake CNPG, restore replicas,
# resume Flux, then health-check the cluster.
# Usage: scripts/node-up.sh [--no-wait-node]
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
STATE=.state/quiesce.json
log() { echo "node-up: $*" >&2; }
for t in kubectl jq flux; do command -v $t >/dev/null || { log "missing $t (run via mise)"; exit 2; }; done
[ -f "$STATE" ] || { log "no $STATE: nothing to restore"; exit 2; }
state=$(cat "$STATE")

if [ "${1:-}" != "--no-wait-node" ]; then
  log "waiting for the API and the node"
  until kubectl get nodes >/dev/null 2>&1; do sleep 10; done
  until [ "$(kubectl get nodes -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}')" = "True" ]; do sleep 10; done
fi
kubectl get nodes -o name | xargs -r kubectl uncordon >/dev/null
log "node ready and uncordoned"

# Wake CNPG explicitly (removing the annotation is not enough) and restore replicas
echo "$state" | jq -c '.targets[]' | while read -r t; do
  kind=$(echo "$t" | jq -r .kind); ns=$(echo "$t" | jq -r .ns); name=$(echo "$t" | jq -r .name); r=$(echo "$t" | jq -r '.replicas // empty')
  case "$kind" in
    cnpg) kubectl -n "$ns" annotate cluster.postgresql.cnpg.io "$name" cnpg.io/hibernation=off --overwrite >/dev/null 2>&1 </dev/null;;
    Deployment) kubectl -n "$ns" scale deploy "$name" --replicas="$r" >/dev/null </dev/null;;
    StatefulSet) kubectl -n "$ns" scale sts "$name" --replicas="$r" >/dev/null </dev/null;;
    CronJob) [ "$(echo "$t" | jq -r .was_suspended)" = true ] || kubectl -n "$ns" patch cronjob "$name" --type merge -p '{"spec":{"suspend":false}}' >/dev/null </dev/null;;
  esac
done
log "databases woken, replicas restored"

# Resume Flux, except what was already suspended before node-down
skip_ks=$(echo "$state" | jq -r '.already_suspended_ks[]')
for k in $(kubectl -n flux-system get ks -o jsonpath='{.items[*].metadata.name}'); do
  echo "$skip_ks" | grep -qx "$k" || kubectl -n flux-system patch ks "$k" --type merge -p '{"spec":{"suspend":false}}' >/dev/null
done
skip_hr=$(echo "$state" | jq -r '.already_suspended_hr[] | "\(.ns)/\(.name)"')
kubectl get hr -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name}{"\n"}{end}' | while read -r ns n; do
  echo "$skip_hr" | grep -qx "$ns/$n" || kubectl -n "$ns" patch hr "$n" --type merge -p '{"spec":{"suspend":false}}' >/dev/null </dev/null
done
flux reconcile ks flux-entry -n flux-system >/dev/null 2>&1 || true
log "Flux resumed"
mv "$STATE" "$STATE.done-$(date +%Y%m%dT%H%M)"

# Health checks
log "waiting for pods (up to 15 min)"
for i in $(seq 1 90); do
  bad=$(kubectl get pods -A --no-headers 2>/dev/null | awk '$4!="Running" && $4!="Completed" && $4!="Succeeded"' | wc -l)
  [ "$bad" -eq 0 ] && break
  # Longhorn CSI pods left in Error by the reboot are stale leftovers
  kubectl get pods -n longhorn-system --no-headers 2>/dev/null | awk '$3=="Error" && $1 ~ /^csi-/{print $1}' | xargs -r kubectl -n longhorn-system delete pod --wait=false >/dev/null 2>&1
  sleep 10
done
fail=0
chk() { if [ "$2" = ok ]; then log "  OK   $1"; else log "  FAIL $1: $2"; fail=1; fi; }
np=$(kubectl get pods -A --no-headers | awk '$4!="Running" && $4!="Completed" && $4!="Succeeded"{printf "%s/%s ",$1,$2}'); chk pods "${np:-ok}"
cn=$(kubectl get clusters.postgresql.cnpg.io -A -o json | jq -r '[.items[] | select(.status.readyInstances != .spec.instances) | .metadata.name] | join(" ")'); chk postgres "${cn:-ok}"
lh=$(kubectl -n longhorn-system get volumes.longhorn.io -o json | jq -r '[.items[] | select(.status.robustness != "healthy") | .metadata.name] | join(" ")'); chk longhorn "${lh:-ok}"
hr=$(kubectl get hr -A -o json | jq -r '[.items[] | select(([.status.conditions[]? | select(.type=="Ready")][0].status) != "True") | "\(.metadata.namespace)/\(.metadata.name)"] | join(" ")'); chk helmreleases "${hr:-ok}"
gpu=$(kubectl -n media exec deploy/emby -- nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1); chk "gpu in emby" "$([ -n "$gpu" ] && echo ok || echo 'nvidia-smi failed')"
vpn=$(kubectl -n media exec deploy/qbittorrent -c qbittorrent-gluetun -- wget -qO- http://127.0.0.1:8000/v1/portforward 2>/dev/null | grep -c '"port":[1-9]' || true); chk "vpn port forward" "$([ "$vpn" = 1 ] && echo ok || echo 'no forwarded port yet')"
[ $fail = 0 ] && log "all checks passed" || { log "some checks failed (see above)"; exit 1; }
