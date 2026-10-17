#!/bin/bash
# Sourced by the media:* mise tasks: port-forwards the *arr APIs to localhost and exports
# their API keys from the cluster secrets (never printed). The forwards stop on EXIT.
#   radarr -> localhost:17878, sonarr -> localhost:18989, radarr-4k -> localhost:17879 (if present)
_arr_pids=()
_arr_forward() { # <svc> <local-port> <svc-port>
  kubectl -n media port-forward "svc/$1" "$2:$3" >/dev/null 2>&1 &
  _arr_pids+=($!)
}
_arr_key() { # <secret> <key>
  kubectl -n media get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null | base64 -d
}
trap 'kill "${_arr_pids[@]}" 2>/dev/null' EXIT

_arr_forward radarr 17878 7878
_arr_forward sonarr 18989 8989
RADARR_API_KEY=$(_arr_key decluttarr-secret RADARR_API_KEY)
SONARR_API_KEY=$(_arr_key decluttarr-secret SONARR_API_KEY)
export RADARR_API_KEY SONARR_API_KEY
if kubectl -n media get svc radarr-4k >/dev/null 2>&1; then
  _arr_forward radarr-4k 17879 7878
  RADARR_4K_API_KEY=$(_arr_key radarr-4k-secret RADARR_4K_API_KEY)
  export RADARR_4K_API_KEY
fi

# Wait until every forward answers (max ~10 s)
for _port in 17878 18989 ${RADARR_4K_API_KEY:+17879}; do
  for _ in $(seq 1 20); do
    (exec 3<>"/dev/tcp/127.0.0.1/$_port") 2>/dev/null && break
    sleep 0.5
  done
done
