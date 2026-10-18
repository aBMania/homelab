#!/usr/bin/env bash
# Move an app's data to a new PVC (SSD Stage 2): two-pass rsync, the app stopped only for pass 2.
# Usage: scripts/move-volume.sh <ns> <helmrelease> <deployment> <source> <dest-pvc> <pr> [--exclude DIR]... [--dry-run]
#   <source>  pvc:<name> (copied as root, owners kept) or nfs:<path> (as 568:568; NFS squashes root)
#   <pr>      the PR that adds <dest-pvc> and points the HelmRelease at it; merged here once green
# Flow: suspend HR → merge PR → reconcile → pass 1 (app running) → Emby idle? → scale to 0 →
#       pass 2 --delete → file counts equal? → resume HR → rollout → deployment uses <dest-pvc>.
# Stops without resuming if anything is off after the app was stopped (rollback = revert the PR).
# Test overrides: EMBY_IDLE_CMD, MOVE_POLL (seconds between polls), MOVE_STATE_DIR,
# MOVE_SAVE_MANIFESTS (append the rsync pod manifests to this file).
set -euo pipefail
ns=$1 hr=$2 deploy=$3 src=$4 dst=$5 pr=$6; shift 6
DRY=0; excludes=()
while [ $# -gt 0 ]; do
  case "$1" in --dry-run) DRY=1;; --exclude) excludes+=("$2"); shift;; *) echo "unknown arg: $1" >&2; exit 2;; esac
  shift
done
cd "$(dirname "$0")/.."
STATE=${MOVE_STATE_DIR:-.state}; mkdir -p "$STATE"
POLL=${MOVE_POLL:-5}
EMBY_IDLE_CMD=${EMBY_IDLE_CMD:-scripts/emby-idle.sh --wait 60}
IMG="instrumentisto/rsync-ssh:alpine3.23-r3@sha256:6cbad37c2fbdca4ac7ad9d1c1bb8990af9efd4dc76321b349935876cbb1e9e4a"
log() { echo "$(date +%T) [$ns/$deploy] $*" | tee -a "$STATE/move-volume.log" >&2; }
run() { if [ $DRY = 1 ]; then log "DRY: $*"; else "$@"; fi; }
wait_for() {  # wait_for <seconds> <command...>: poll until the command succeeds
  local t=$1; shift; local end=$(( $(date +%s) + t ))
  until "$@"; do [ "$(date +%s)" -ge "$end" ] && return 1; sleep "$POLL"; done
}

case "$src" in
  pvc:*) src_vol="persistentVolumeClaim: {claimName: ${src#pvc:}, readOnly: true}"; uid=0 ;;
  nfs:*) server=$(kubectl -n flux-system get secret cluster-config -o jsonpath='{.data.NFS_HOST}' | base64 -d)
         src_vol="nfs: {server: ${server}, path: ${src#nfs:}, readOnly: true}"; uid=568 ;;
  *) echo "source must be pvc:<name> or nfs:<path>" >&2; exit 2 ;;
esac

# rsync_pass <pass> <delete:yes|no>: prints "files src=N dst=M"
rsync_pass() {
  local pass=$1 del=$2 pod="${dst}-rsync-$1" opts="-a --stats"
  [ "$del" = yes ] && opts="$opts --delete"
  [ "$uid" = 0 ] || opts="$opts --no-owner --no-group"
  for d in "${excludes[@]}"; do opts="$opts --exclude=/$d/"; done
  local manifest
  manifest=$(cat <<EOF
apiVersion: v1
kind: Pod
metadata: {name: ${pod}, namespace: ${ns}}
spec:
  restartPolicy: Never
  securityContext: {runAsUser: ${uid}, runAsGroup: ${uid}}
  initContainers:  # the new volume's root is root-owned; hand it to the copier (568 for NFS)
    - name: own
      image: ${IMG}
      securityContext: {runAsUser: 0, runAsGroup: 0}
      command: [sh, -c, 'chown ${uid}:${uid} /dst && chmod 775 /dst']
      volumeMounts: [{name: dst, mountPath: /dst}]
  containers:
    - name: rsync
      image: ${IMG}
      command: [sh, -c, 'rsync ${opts} /src/ /dst/ >/dev/null && echo "files src=\$(find /src -type f | wc -l) dst=\$(find /dst -type f | wc -l)"']
      volumeMounts: [{name: src, mountPath: /src, readOnly: true}, {name: dst, mountPath: /dst}]
  volumes:
    - name: src
      ${src_vol}
    - name: dst
      persistentVolumeClaim: {claimName: ${dst}}
EOF
)
  [ -n "${MOVE_SAVE_MANIFESTS:-}" ] && echo "$manifest" >> "$MOVE_SAVE_MANIFESTS"
  if [ $DRY = 1 ]; then log "DRY: rsync $pass ($opts)"; echo "files src=0 dst=0"; return; fi
  kubectl -n "$ns" delete pod "$pod" --ignore-not-found --wait=true >/dev/null
  echo "$manifest" | kubectl apply -f - >/dev/null
  wait_for 3600 bash -c "[ \"\$(kubectl -n $ns get pod $pod -o jsonpath='{.status.phase}')\" = Succeeded ] || [ \"\$(kubectl -n $ns get pod $pod -o jsonpath='{.status.phase}')\" = Failed ]" || true
  kubectl -n "$ns" logs "$pod" | tail -1
  kubectl -n "$ns" delete pod "$pod" --ignore-not-found >/dev/null
}

checks_pending() { gh pr view "$pr" --json statusCheckRollup --jq '[.statusCheckRollup[] | select(.status != "COMPLETED")] | length'; }
checks_bad() { gh pr view "$pr" --json statusCheckRollup --jq '[.statusCheckRollup[] | select(.conclusion != "SUCCESS" and .conclusion != "SKIPPED" and .conclusion != "NEUTRAL")] | length'; }

log "start: $src -> pvc $dst (PR #$pr)"
run flux suspend hr -n "$ns" "$hr" >/dev/null
merged=$(gh pr view "$pr" --json mergedAt --jq '.mergedAt // ""' 2>/dev/null || true)
[ -n "$merged" ] && log "PR #$pr already merged (resuming)"
if [ $DRY = 0 ] && [ -z "$merged" ]; then
  # mergeStateStatus CLEAN = required checks passed and nothing blocks the merge. Checks register
  # one by one on a fresh PR, so "none pending" alone can be true too early.
  if ! wait_for "${MOVE_CHECKS_TIMEOUT:-900}" bash -c "[ \"\$(gh pr view $pr --json mergeStateStatus --jq .mergeStateStatus)\" = CLEAN ]"; then
    log "ABORT: PR #$pr not mergeable (checks pending or failing)"; flux resume hr -n "$ns" "$hr" >/dev/null; exit 1
  fi
  wait_for 900 bash -c "[ \"\$(gh pr view $pr --json statusCheckRollup --jq '[.statusCheckRollup[] | select(.status != \"COMPLETED\")] | length')\" = 0 ]" || true
  if [ "$(checks_bad)" != 0 ]; then log "ABORT: PR #$pr checks not green"; flux resume hr -n "$ns" "$hr" >/dev/null; exit 1; fi
fi
[ -n "$merged" ] || run gh pr merge "$pr" --squash --delete-branch >/dev/null 2>&1 || true  # checked just below
if [ $DRY = 0 ] && [ "$(gh pr view "$pr" --json state --jq .state)" != MERGED ]; then
  log "ABORT: PR #$pr not merged"; flux resume hr -n "$ns" "$hr" >/dev/null; exit 1
fi
# Flux may fetch git before GitHub serves the merge: reconcile again until the PVC shows up
reconcile() { flux reconcile source git cluster -n flux-system >/dev/null 2>&1; flux reconcile ks "$hr" -n flux-system >/dev/null 2>&1; kubectl -n "$ns" get pvc "$dst" -o name >/dev/null 2>&1; }
if [ $DRY = 1 ]; then log "DRY: flux reconcile source + ks $hr"
elif ! wait_for 300 reconcile; then
  log "ABORT: pvc $dst not created (app unchanged, HR suspended)"; exit 1
fi

out=$(rsync_pass pass1 no); log "pass1: $out"
if ! $EMBY_IDLE_CMD >/dev/null 2>&1; then
  log "ABORT: Emby busy (or unreachable); app left running on its old volume"
  run flux resume hr -n "$ns" "$hr" >/dev/null; exit 1
fi
run kubectl -n "$ns" scale "deploy/$deploy" --replicas=0 >/dev/null
[ $DRY = 1 ] || wait_for 300 bash -c "[ -z \"\$(kubectl -n $ns get pods -l app.kubernetes.io/instance=$hr -o name 2>/dev/null)\" ]" || true
log "app stopped"
out=$(rsync_pass pass2 yes); log "pass2: $out"
s=$(sed -n 's/.*src=\([0-9]*\).*/\1/p' <<<"$out"); d=$(sed -n 's/.*dst=\([0-9]*\).*/\1/p' <<<"$out")
if [ -z "$s" ] || [ "$s" != "$d" ]; then log "STOP: counts differ (src=$s dst=$d); NOT resuming"; exit 1; fi
run flux resume hr -n "$ns" "$hr" >/dev/null
run kubectl -n "$ns" rollout status "deploy/$deploy" --timeout=600s >/dev/null
claims=$(kubectl -n "$ns" get deploy "$deploy" -o jsonpath='{range .spec.template.spec.volumes[*]}{.persistentVolumeClaim.claimName} {end}')
if [ $DRY = 0 ] && ! grep -qw "$dst" <<<"$claims"; then log "STOP: deployment volumes are '$claims', not $dst"; exit 1; fi
log "OK: running on $dst"
