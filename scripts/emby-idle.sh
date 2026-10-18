#!/usr/bin/env bash
# Is anyone playing something in Emby? Gate for maintenance (node:down, volume moves).
# Usage: scripts/emby-idle.sh [--wait MINUTES]
#   exit 0  nobody is playing
#   exit 1  someone is playing (after MINUTES of polling with --wait)
#   exit 2  error (Emby unreachable, key missing or rejected)
# Prints counts only, never what is playing. Key: Secret media/emby-maintenance (api-key).
# Test overrides: EMBY_URL (skip the port-forward), EMBY_KEY_OVERRIDE, EMBY_IDLE_INTERVAL
# (seconds between polls, default 60), EMBY_IDLE_MINUTE (seconds per minute, default 60).
set -euo pipefail
WAIT=0
case "${1:-}" in --wait) WAIT=${2:?--wait needs minutes};; "") ;; *) echo "unknown arg: $1" >&2; exit 2;; esac
log() { echo "emby-idle: $*" >&2; }

key=${EMBY_KEY_OVERRIDE:-$(kubectl -n media get secret emby-maintenance -o jsonpath='{.data.api-key}' 2>/dev/null | base64 -d)}
[ -n "$key" ] || { log "no key (secret media/emby-maintenance)"; exit 2; }
url=${EMBY_URL:-}
if [ -z "$url" ]; then
  port=$((20000 + RANDOM % 10000))
  kubectl -n media port-forward svc/emby "$port:10079" >/dev/null 2>&1 & pf=$!
  trap 'kill $pf 2>/dev/null' EXIT
  url="http://127.0.0.1:$port"
  for _ in $(seq 20); do curl -s -o /dev/null -m 1 "$url" && break; sleep 0.5; done
fi

playing() {  # prints the number of sessions playing something; fails on error
  curl -sf -m 15 -H "X-Emby-Token: $key" "$url/emby/Sessions" | jq '[.[] | select(.NowPlayingItem)] | length'
}

deadline=$(( $(date +%s) + WAIT * ${EMBY_IDLE_MINUTE:-60} ))
while :; do
  n=$(playing) || { log "cannot read Emby sessions"; exit 2; }
  log "playing: $n"
  [ "$n" = 0 ] && exit 0
  [ "$(date +%s)" -ge "$deadline" ] && exit 1
  sleep "${EMBY_IDLE_INTERVAL:-60}"
done
