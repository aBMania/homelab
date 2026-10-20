#!/bin/bash
# Sourced after scripts/arr.sh by media tasks that read Emby: port-forwards Emby to
# localhost:18096 and exports EMBY_API_KEY from the media/emby-maintenance secret (never printed).
_arr_forward emby 18096 10079
EMBY_API_KEY=$(_arr_key emby-maintenance api-key)
export EMBY_API_KEY
for _ in $(seq 1 20); do
  (exec 3<>"/dev/tcp/127.0.0.1/18096") 2>/dev/null && break
  sleep 0.5
done
