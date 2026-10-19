#!/usr/bin/env bash
# Who opens connections into the app namespaces: the evidence for their ingress policies.
#   mise run net:inbound [namespace]
# One line per source → destination workload and port, with a count and when it was last seen.
# Sources: <namespace>/<workload>, LAN:<ip>, or internet (public addresses are not printed).
# Data: the agent's `inbound` flow log (see the cilium HelmRelease, hubble.export.dynamic): new
# TCP connections and UDP packets into app namespaces, excluding the node (always allowed) and
# LAN DNS queries to Blocky.
set -euo pipefail

ns="${1:-}"

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
kubectl -n kube-system exec ds/cilium -c cilium-agent -- sh -c \
  'cd /var/run/cilium/hubble 2>/dev/null || exit 0; f=$(ls -tr inbound*.log 2>/dev/null); [ -z "$f" ] || cat $f' \
  > "$tmp"
if [ ! -s "$tmp" ]; then
  echo "nothing logged yet (the agent's inbound flow log is empty or missing)"
  exit 0
fi

jq -r --arg ns "$ns" '
  def workload: sub("-[a-z0-9]{8,10}-[a-z0-9]{5}$"; "") | sub("-[0-9]+$"; "");
  def private: test("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)");
  select(.flow) | .flow
  | select($ns == "" or .destination.namespace == $ns)
  | (if (.source.namespace // "") != "" then "\(.source.namespace)/\(.source.pod_name // "-" | workload)"
     elif (.IP.source // "" | private) then "LAN:\(.IP.source)"
     else "internet" end) as $src
  | "\(.destination.namespace)/\(.destination.pod_name // "-" | workload)" as $dst
  | (if .l4.TCP then "tcp/\(.l4.TCP.destination_port)"
     elif .l4.UDP then "udp/\(.l4.UDP.destination_port)" else "other" end) as $port
  | [$src, $dst, $port, .time] | @tsv' "$tmp" |
  awk -F'\t' '{ k = $1 "\t" $2 "\t" $3; n[k]++; if ($4 > last[k]) last[k] = $4 }
    END { for (k in n) print k "\t" n[k] "\t" substr(last[k], 1, 16) }' |
  sort -t$'\t' -k2,2 -k1,1 |
  { printf 'SOURCE\tDESTINATION\tPORT\tCOUNT\tLAST SEEN (UTC)\n'; cat; } | column -t -s$'\t'
