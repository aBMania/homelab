#!/usr/bin/env bash
# Quiesce the cluster before a node shutdown/reboot: every app stopped cleanly first (Talos stops
# pods in parallel, and its drain stalls on some pods), Postgres hibernated, Flux paused.
# Stops: workloads mounting any PVC, plus every Deployment/StatefulSet outside the infra
# namespaces ($KEEP_NS). Refuses to start while someone plays something in Emby.
#
# Usage: scripts/node-down.sh [--dry-run] [shutdown|reboot]
#   no action   quiesce only (e.g. before a TrueNAS reboot or `mise run talos:apply`)
#   shutdown    quiesce, then `talosctl shutdown`
#   reboot      quiesce, then `talosctl reboot`
# Undo with scripts/node-up.sh (reads the state file written here).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
STATE=${NODE_DOWN_STATE:-.state/quiesce.json}
POLL=${NODE_DOWN_POLL:-10}
EMBY_IDLE_CMD=${EMBY_IDLE_CMD:-scripts/emby-idle.sh --wait 30}
# infra: left running until the power action (DNS, ingress, operators, storage, Flux)
KEEP_NS=${NODE_DOWN_KEEP_NS:-kube-system kube-public kube-node-lease flux-system cert-manager cloudnative-pg gpu-operator kyverno metallb metallb-config openebs prometheus-operator snapshot-controller system system-upgrade traefik volsync kubernetes-reflector blocky}
TALOSCONFIG=clusters/main/talos/generated/talosconfig
DRY=0; ACTION=""
for a in "$@"; do case "$a" in --dry-run) DRY=1;; shutdown|reboot) ACTION=$a;; *) echo "unknown arg: $a" >&2; exit 2;; esac; done
log() { echo "node-down: $*" >&2; }
for t in kubectl jq talosctl; do command -v $t >/dev/null || { log "missing $t (run via mise)"; exit 2; }; done
[ -f "$STATE" ] && [ $DRY = 0 ] && { log "$STATE exists: a previous quiesce was not undone. Run scripts/node-up.sh first."; exit 2; }

# 0. Nobody watching?
if [ $DRY = 0 ] && ! $EMBY_IDLE_CMD; then log "ABORT: someone is playing in Emby (or Emby unreachable); nothing changed"; exit 1; fi

# 1. Discover what to stop: users of any PVC, and every app workload
keep=$(printf '%s\n' $KEEP_NS | jq -R . | jq -sc .)
lh_pvcs=$(kubectl get pvc -A -o json | jq -c '[.items[] | {ns: .metadata.namespace, name: .metadata.name}]')
plan=$(kubectl get pods -A -o json | jq -c --argjson pvcs "$lh_pvcs" '
  [ .items[] | . as $p
    | select([.spec.volumes[]? | .persistentVolumeClaim.claimName // empty] as $c
             | any($pvcs[]; .ns == $p.metadata.namespace and (.name as $n | $c | index($n))))
    | if .metadata.labels["cnpg.io/cluster"] then {kind: "cnpg", ns: .metadata.namespace, name: .metadata.labels["cnpg.io/cluster"]}
      else (.metadata.ownerReferences[0] // {}) as $o | {kind: $o.kind, ns: .metadata.namespace, name: $o.name} end
  ] | unique')
# ReplicaSet -> Deployment
targets=$(echo "$plan" | jq -c '.[]' | while read -r t; do
  kind=$(echo "$t" | jq -r .kind); ns=$(echo "$t" | jq -r .ns); name=$(echo "$t" | jq -r .name)
  case "$kind" in
    ReplicaSet) d=$(kubectl -n "$ns" get rs "$name" -o jsonpath='{.metadata.ownerReferences[0].name}'); r=$(kubectl -n "$ns" get deploy "$d" -o jsonpath='{.spec.replicas}'); echo "{\"kind\":\"Deployment\",\"ns\":\"$ns\",\"name\":\"$d\",\"replicas\":$r}" </dev/null;;
    StatefulSet) r=$(kubectl -n "$ns" get sts "$name" -o jsonpath='{.spec.replicas}'); echo "{\"kind\":\"StatefulSet\",\"ns\":\"$ns\",\"name\":\"$name\",\"replicas\":$r}" </dev/null;;
    cnpg) echo "{\"kind\":\"cnpg\",\"ns\":\"$ns\",\"name\":\"$name\"}";;
    Job) cj=$(kubectl -n "$ns" get job "$name" -o jsonpath='{.metadata.ownerReferences[0].name}' </dev/null 2>/dev/null)
         if [ -n "$cj" ]; then s=$(kubectl -n "$ns" get cronjob "$cj" -o jsonpath='{.spec.suspend}' </dev/null); echo "{\"kind\":\"CronJob\",\"ns\":\"$ns\",\"name\":\"$cj\",\"was_suspended\":${s:-false}}"; else log "WARNING: standalone Job $ns/$name (left to finish)"; fi;;
    *) log "WARNING: unhandled owner $kind $ns/$name (left running)";;
  esac
done | jq -sc 'unique')
# CronJobs that mount PVCs, even if no Job runs right now (one could start mid-maintenance)
cronjobs=$(kubectl get cronjobs -A -o json | jq -c --argjson pvcs "$lh_pvcs" '
  [ .items[] | . as $c
    | select([.spec.jobTemplate.spec.template.spec.volumes[]? | .persistentVolumeClaim.claimName // empty] as $v
             | any($pvcs[]; .ns == $c.metadata.namespace and (.name as $n | $v | index($n))))
    | {kind: "CronJob", ns: .metadata.namespace, name: .metadata.name, was_suspended: (.spec.suspend // false)} ]')
# every Deployment/StatefulSet outside the infra namespaces, with or without volumes
apps=$( { kubectl get deploy -A -o json | jq -c --argjson keep "$keep" '[.items[] | select((.metadata.namespace as $n | $keep | index($n)) | not) | select((.spec.replicas // 0) > 0) | {kind: "Deployment", ns: .metadata.namespace, name: .metadata.name, replicas: .spec.replicas}]'
          kubectl get sts -A -o json | jq -c --argjson keep "$keep" '[.items[] | select((.metadata.namespace as $n | $keep | index($n)) | not) | select((.spec.replicas // 0) > 0) | select(.metadata.ownerReferences // [] | map(.kind) | index("Cluster") | not) | {kind: "StatefulSet", ns: .metadata.namespace, name: .metadata.name, replicas: .spec.replicas}]'; } | jq -sc 'add')
targets=$(jq -nc --argjson a "$targets" --argjson b "$cronjobs" --argjson c "$apps" '$a + $b + $c | unique_by([.kind,.ns,.name])')
suspended_ks=$(kubectl -n flux-system get ks -o json | jq -c '[.items[] | select(.spec.suspend == true) | .metadata.name]')
suspended_hr=$(kubectl get hr -A -o json | jq -c '[.items[] | select(.spec.suspend == true) | {ns: .metadata.namespace, name: .metadata.name}]')
state=$(jq -n --argjson t "$targets" --argjson sk "$suspended_ks" --argjson sh "$suspended_hr" '{targets: $t, already_suspended_ks: $sk, already_suspended_hr: $sh}')

log "PVCs: $(echo "$lh_pvcs" | jq length). Will stop:"
echo "$targets" | jq -r '.[] | "  \(.kind) \(.ns)/\(.name)\(if .replicas then " (replicas \(.replicas))" else "" end)\(if .kind == "CronJob" then " (suspended; running Job finishes)" else "" end)"' >&2
if [ $DRY = 1 ]; then log "dry-run: nothing changed${ACTION:+ (would then run talosctl $ACTION)}"; exit 0; fi

mkdir -p "$(dirname "$STATE")"; echo "$state" > "$STATE"

# 2. Pause Flux so it doesn't scale things back up
kubectl -n flux-system get ks -o name | xargs -r -n1 kubectl -n flux-system patch --type merge -p '{"spec":{"suspend":true}}' >/dev/null
kubectl get hr -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name}{"\n"}{end}' | while read -r ns n; do kubectl -n "$ns" patch hr "$n" --type merge -p '{"spec":{"suspend":true}}' >/dev/null; done
log "Flux suspended"

# 3. Stop the consumers
echo "$targets" | jq -c '.[]' | while read -r t; do
  kind=$(echo "$t" | jq -r .kind); ns=$(echo "$t" | jq -r .ns); name=$(echo "$t" | jq -r .name)
  case "$kind" in
    cnpg) kubectl -n "$ns" annotate cluster.postgresql.cnpg.io "$name" cnpg.io/hibernation=on --overwrite >/dev/null 2>&1 </dev/null;;
    Deployment) kubectl -n "$ns" scale deploy "$name" --replicas=0 >/dev/null </dev/null;;
    StatefulSet) kubectl -n "$ns" scale sts "$name" --replicas=0 >/dev/null </dev/null;;
    CronJob) kubectl -n "$ns" patch cronjob "$name" --type merge -p '{"spec":{"suspend":true}}' >/dev/null </dev/null;;
  esac
done
log "consumers stopped, waiting for their pods to be gone"

# 4. Wait until no pod of a stopped workload (or mounting a PVC) is left. Pods node-down doesn't
#    manage (DaemonSets, bare pods) don't count. Abort and restore on timeout.
left() {  # prints ns/name of every pod still running that belongs to a target or mounts a PVC
  kubectl get pods -A -o json | jq -r --argjson t "$targets" --argjson pvcs "$lh_pvcs" '.items[] | . as $p
    | select(.status.phase == "Running" or .status.phase == "Pending")
    | (.metadata.ownerReferences[0] // {}) as $o | .metadata.namespace as $ns
    | select(
        any($t[]; . as $w | $w.ns == $ns and (
             ($w.kind == "Deployment" and $o.kind == "ReplicaSet" and ($o.name | startswith($w.name + "-")) and ($o.name | ltrimstr($w.name + "-") | test("^[a-z0-9]+$")))
          or ($w.kind == "StatefulSet" and $o.kind == "StatefulSet" and $o.name == $w.name)
          or ($w.kind == "cnpg" and $p.metadata.labels["cnpg.io/cluster"] == $w.name)))
        or ([.spec.volumes[]? | .persistentVolumeClaim.claimName // empty] as $c
            | any($pvcs[]; .ns == $ns and (.name as $n | $c | index($n)))))
    | "\(.metadata.namespace)/\(.metadata.name)"'
}
for i in $(seq 1 90); do
  pods=$(left </dev/null)
  [ -z "$pods" ] && break
  sleep "$POLL"
done
if [ -n "$pods" ]; then
  log "TIMEOUT: still running: $(echo $pods); restoring everything"
  scripts/node-up.sh --no-wait-node
  exit 1
fi
log "all app pods gone"

# 5. Optional power action
if [ -n "$ACTION" ]; then
  log "talosctl $ACTION"
  talosctl --talosconfig "$TALOSCONFIG" "$ACTION" --wait=false
fi
log "done. Afterwards: mise run node:up"
