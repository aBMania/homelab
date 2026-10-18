#!/usr/bin/env bash
# Blocked network flows (Cilium/Hubble), e.g. after adding a network policy.
#   mise run net:drops [namespace]            drops logged by the Cilium agent (hours of history)
#   mise run net:drops --follow [namespace]   live, through Hubble Relay
# Extra hubble flags go after `--`, e.g. `mise run net:drops traefik -- --to-ip 192.168.1.73`.
# Hubble's in-memory buffer only holds seconds of flows on this node, so past drops come from the
# file the agent exports (DROPPED only, see the cilium HelmRelease); --follow uses the relay.
set -euo pipefail

follow=""
ns=""
extra=()
while [ $# -gt 0 ]; do
  case "$1" in
    -f|--follow) follow=1 ;;
    --) shift; extra=("$@"); break ;;
    *) ns="$1" ;;
  esac
  shift
done
filters=(--verdict DROPPED ${ns:+--namespace "$ns"} "${extra[@]}")

if [ -n "$follow" ]; then
  exec hubble observe -P --follow -o compact "${filters[@]}"
fi

# Oldest first: rotated backups (events-<time>.log), then the current file
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
kubectl -n kube-system exec ds/cilium -c cilium-agent -- sh -c \
  'cd /var/run/cilium/hubble 2>/dev/null || exit 0; f=$(ls -tr events*.log 2>/dev/null); [ -z "$f" ] || cat $f' \
  > "$tmp"
if [ ! -s "$tmp" ]; then
  echo "no drops logged yet (the agent's export file is empty or missing; live view: --follow)"
  exit 0
fi
hubble observe --input-file "$tmp" -o compact "${filters[@]}"
