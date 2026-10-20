# Push notifications (ntfy)

The admin's Android phone gets an ntfy message when storage is in trouble or something people use is broken. Nothing is sent for a restart, an update or a blip. Design: `docs/superpowers/specs/2026-10-20-push-notifications-design.md`.

Nothing uses priority 5, so the phone's Do Not Disturb silences everything at night, a dead disk included.

## Where alerts come from

| Source | Checks | Sends when |
|---|---|---|
| Gatus (`monitoring` namespace, `core/gatus/`) | Requests page, login portal, Emby's public address, VPN, stalled downloads, arr download clients, Requests backends | 1 h of continuous failure; one recovery message |
| Flaky detector (CronJob `gatus-flaky`) | same checks, last 6 h | failed > 5 % of the time (18 min) or ≥ 3 outages of ≥ 2 checks; at most once per check per day; "stable again" when the window is clean |
| healthchecks.io | `cluster` (CronJob `gatus-heartbeat`, only while Gatus is healthy), `truenas` and `emby-lan` (TrueNAS cron job) | no ping for 1 h (period 5 min, grace 55 min) |
| TrueNAS alert service | anything TrueNAS raises at Warning or above (SMART, pool, capacity, snapshots, replication, updates…) | as TrueNAS raises it |

Status page: `https://status.<domain>`. No login on the LAN, any user's password from outside (Authelia).

## What each message means and where to look

| Message | Meaning | Look at |
|---|---|---|
| Requests: unreachable through Traefik/Authelia | the public Requests page doesn't answer | Traefik, Authelia, Jellyseerr pods |
| Login portal down | Authelia's health endpoint fails: every app behind it is unusable | `kubectl -n authelia get pods` |
| Emby unreachable at its public address | public DNS → router port forward → Traefik → Emby fails, or the certificate expires within 3 days | DDNS (`cloudflareddns`), router port forward, Traefik, Emby, Certificate |
| Downloads: VPN down | qBittorrent's connection status isn't `connected` (tunnel down, port not forwarded or firewalled) for 1 h | gluetun logs, `qbittorrent` pod |
| Downloads: nothing has moved | active downloads but 0 B/s for 1 h | qBittorrent, VPN |
| Radarr / Radarr 4K / Sonarr can't reach qBittorrent | the arr's health reports the download client unavailable | the arr's System → Status |
| Requests: can't reach Emby, Radarr or Sonarr | the probe can't reach Jellyseerr, Radarr or Sonarr | those pods |
| `<check>` flaky | repeated short outages over 6 h (e.g. ProtonVPN blackouts) | the check's history on the status page |
| Homelab not checking in (healthchecks.io `cluster`) | no heartbeat for 1 h: node, power, home internet, or Gatus down | TrueNAS VM, internet |
| TrueNAS not checking in | the TrueNAS cron job stopped for 1 h | TrueNAS |
| Emby not answering on the LAN | TrueNAS couldn't reach `192.168.1.112:8096` for 1 h | MetalLB, Emby |
| TrueNAS: … | TrueNAS's own alert text | TrueNAS UI → Alerts |

## Maintenance

- `mise run node:down` scales `monitoring` down with the apps: Gatus records nothing, so no app alert fires. The healthchecks.io `cluster` heartbeat stops too: **before a `node:down` that may last more than 1 h, pause the `cluster` check in healthchecks.io** (and `emby-lan`), and resume it after `node:up`.
- A TrueNAS reboot stops the `truenas` and `emby-lan` pings; under 1 h, nothing is sent.
- A Flux suspension without `node:down` isn't detected: a deliberate long outage can send a "down" message.

## Secrets

`core/gatus/app/alerting.secret.yaml` (SOPS): `NTFY_TOPIC`, `NTFY_TOKEN`, `GATUS_PUSH_TOKEN`, `HC_CLUSTER_PING_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD`, `RADARR_API_KEY`, `RADARR_4K_API_KEY`, `SONARR_API_KEY`. Edit with `sops core/gatus/app/alerting.secret.yaml`; never print the values.

## TrueNAS setup (UI, done by the admin)

1. **Alert service.** TrueNAS 25.10 has no ntfy or generic webhook service, but its **Slack** service posts `{"text": "…"}`, which ntfy turns into a message with templating. System → Alert Settings → Add: type **Slack**, level **Warning**, webhook URL:

   ```
   https://ntfy.sh/<topic>?tpl=yes&t=TrueNAS&m={{.text}}&auth=<AUTH>
   ```

   where `<AUTH>` is computed on a trusted machine (not printed into any shared log):

   ```bash
   printf 'Bearer %s' "<ntfy token>" | base64 | tr '+/' '-_' | tr -d '='
   ```

   "Send Test Alert" → a message on the phone. All TrueNAS alerts arrive at priority 3: the Slack service can't vary the request per alert level.
2. **Cron job.** System → Advanced → Cron Jobs → Add, every 5 minutes (`*/5`), any user with network access, command:

   ```
   curl -fsS -m 10 --retry 3 '<HC truenas URL>' >/dev/null; curl -fsS -m 10 http://192.168.1.112:8096/emby/System/Info/Public >/dev/null && curl -fsS -m 10 --retry 3 '<HC emby-lan URL>' >/dev/null
   ```

   healthchecks.io `truenas` and `emby-lan` turn "up" within 10 minutes.

## Tuning

- Flaky thresholds: `WINDOW`, `MAX_RATIO`, `MAX_OUTAGES` in `core/gatus/app/flaky/flaky.py` (tests in `scripts/tests/test_gatus_flaky.py`). `FLAKY_NOTIFY` in `flaky-cronjob.yaml` switches messages on (log-only for the first week).
- Down threshold: `failure-threshold` in the Gatus config (`helm-release.yaml`): 12 for 5-min checks, 60 for the 1-min VPN check.
- Too many TrueNAS messages: change the alert class's level in TrueNAS (Alert Settings), not the service.
