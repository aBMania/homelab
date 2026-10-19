# Push notifications: tell me when something is really broken

## Intent

The admin wants their Android phone to say when something **people would notice** is broken: a disk failing, storage filling up, Emby unreachable, requests not working, downloads not happening. Today nothing tells them: a VPN blackout loop (ProtonVPN's cap on new connections) left qBittorrent "up" but not downloading, and nobody knew until someone looked.

It is not pod monitoring. Kubernetes restarts pods on its own; an alert per restart would teach the admin to ignore the phone.

## Goals

1. **Storage health and capacity**: a disk failing or in bad shape (SMART errors, pool degraded or faulted), and pools filling up.
2. **"Something is broken" checks**, testing the outcome like a person would:
   - **Emby** answers from the internet (public URL) and on the LAN (`192.168.1.112:8096`).
   - **Requests (Jellyseerr)** loads, and can still reach Emby, Radarr and Sonarr.
   - **Downloading works** (details below).
3. **The homelab is offline**: node down, power cut, TrueNAS down, home internet down. The alert must still reach the phone.
4. One message when something breaks, one when it recovers. Each message says plainly what is wrong and links to the page to look at.

## Non-goals

- Alerts for pod restarts, single app crashes that recover, CPU or memory.
- Dashboards, metrics history, a Prometheus/Grafana stack.
- Alerts to anyone but the admin.
- Network-policy drop alerts (containment E2): later, on the same ntfy setup, once A4 is in place. (Claude's call: the admin's list didn't include them.)

## Anti-goals (failure even if it works)

- **Noise.** Any alert that fires for a restart, an update or a blip. Rule: **a check alerts only after 1 hour of continuous failure** (the admin's rule), or when it is **flaky** (next point).
- **Missing flakiness.** Degraded over time is broken too, even with no 1-hour outage (the admin's rule): e.g. ProtonVPN killing the tunnel for a few minutes again and again. Each check's failures are also averaged over a rolling window, and a flaky check alerts.
- **Being woken up.** Nothing may ring at night or break through Do Not Disturb, **not even a dead disk** (the admin's rule). No message uses ntfy priority 5 (urgent).
- **Leaking private things.** The repo is public, and ntfy.sh / the external check service are third parties:
  - the ntfy topic name or access token and the healthchecks ping URL are secrets (SOPS);
  - no domain name, public IP or user name in git or in commits/PRs (use `${DOMAIN_0}`). **The domain may appear in alert messages and links, and in the URL an external checker probes** (the admin's choice: it is public in DNS anyway; only the repo must not carry it). Configs therefore take URLs from Flux substitution or secrets, not literal text;
  - **no media titles** in alert text (torrent, film or episode names): say "3 downloads stalled", never which ones.
- A monitor that silently stops monitoring. The dead man's switch covers the cluster side; the external checks cover the rest.

## Constraints

- GitOps: everything in `clusters/`, reconciled by Flux; secrets via SOPS or `clusterenv.yaml` variables. Single node, Talos, no Prometheus server (only the prometheus-operator CRDs).
- Network containment is live: pods can't reach private ranges (A2) or TrueNAS (except Traefik on :444); app pods can't reach the API server (A3); per-namespace ingress rules (A4) are coming. A new checker namespace needs allow rules into each namespace it checks once A4 lands; it reaches Emby's LAN IP fine (LoadBalancer IPs are translated to the pod before policy).
- Phone: Android, ntfy app.
- qBittorrent runs behind gluetun (ProtonVPN WireGuard, NAT-PMP port forwarding); gluetun's control server exposes VPN and forwarded-port state; qBittorrent has a Web API.

## How it works (decided)

**Delivery: ntfy.sh** (public server), not self-hosted, so alerts still arrive when the house is offline. Access protected by a free ntfy.sh account with an access token (or, failing that, a long random topic name). The admin subscribes in the Android app. One topic is enough to start. (ntfy.sh and the token: Claude's call.)

**Priorities** (Claude's call, within the admin's rule): TrueNAS Critical and above → priority 4 (high), TrueNAS Warning → 3; app checks → 3 (default); recoveries → 2 (low). Never 5. The phone's Do Not Disturb silences them all at night.

**Four parts:**

1. **In-cluster checker** for the app checks, with ntfy alerting, a per-check failure threshold and "resolved" messages, configured from YAML in git. **Gatus** is Claude's recommendation (single small container, YAML config, native ntfy alerts with `failure-threshold`/`send-on-resolved`, HTTP checks with JSON conditions). Uptime Kuma is configured through its UI (not GitOps); Prometheus + Alertmanager is far heavier and has no native ntfy.
2. **Dead man's switch** outside the house: the cluster pings a **healthchecks.io** check every 5 minutes (a CronJob or the checker itself); healthchecks.io alerts ntfy when pings stop for 1 hour (period 5 min, grace ~55 min). It covers node, cluster, power and home-internet outages, and a dead checker. healthchecks.io has a native ntfy integration. (Claude's call.)
3. **TrueNAS's own alerts**: **everything TrueNAS raises at Warning and above** goes to ntfy (the admin's choice: "we'll handle noise if it's too much"): disks, pools, capacity, failed snapshot/replication tasks, updates, certificates… No filtering bridge; TrueNAS alert classes stay as they are (so the existing emails don't change). Add ntfy as a TrueNAS alert service, natively if 25.10 supports ntfy, else its generic webhook, else a minimal forwarder. **Sent as TrueNAS raises them, not after 1 hour** (Claude's call: a failing disk or a full pool doesn't heal with a restart, and TrueNAS raises each alert once). If it gets noisy, the fix is a per-class policy change in TrueNAS or a filter, decided then.
4. **A TrueNAS cron job** (the admin agreed; set up in the TrueNAS UI), every 5 minutes, pinging two healthchecks.io checks:
   - **"TrueNAS alive"**: pinged on every run. Covers TrueNAS down while the cluster is up (the cluster's own dead man's switch keeps pinging then).
   - **"Emby on the LAN"**: pinged only when `http://192.168.1.112:8096/emby/System/Info/Public` answers 200 from TrueNAS. A real LAN machine, so it tests the whole LAN path (MetalLB's announcement included), and the cluster checker needs **no exception** to A4's "Emby :8096 only from Traefik and the node".
   Both use healthchecks.io's period 5 min / grace ~55 min, i.e. the same 1-hour rule.

## The checks

All app checks run every few minutes (the VPN ones every minute, so short blackouts are seen) and alert in two ways:

- **Down**: **1 hour of continuous failure** (e.g. interval 5 min, failure threshold 12). One message, then one when it recovers.
- **Flaky**: over the last **6 hours**, the check failed **more than 5% of the time** (about 18 minutes in total), or had **3 or more separate outages**, without ever being down for an hour. One message ("Downloads: VPN flaky, down 9 times / 34 min in the last 6 h"), at most once a day per check, and a "back to stable" message once the 6-hour window is clean again. (Thresholds: Claude's call, easy to tune.)
  - An **outage** counts only if it lasts **2 or more consecutive failed checks** (a single missed probe is ignored).
  - **Planned downtime doesn't count**: while Flux is suspended or `node:down` has quiesced the cluster (its `.state/quiesce.json` / suspended Kustomizations), failures are not recorded. A Renovate update restarting an app once a day stays below the thresholds.
  - **Rollout: the flaky rule runs silently for the first week** (it logs what it would have sent); the thresholds are then set against the real background rate before it is allowed to notify.

| Check | What "broken" means | Alert text (example) | Link |
|---|---|---|---|
| Emby, outside | the public URL doesn't answer (`/emby/System/Info/Public` not 200), seen from outside the house (see "Left to the builder") | "Emby unreachable from the internet for 1 h" | Emby |
| Emby, LAN | from the **TrueNAS cron job** (part 4): no successful `http://192.168.1.112:8096/emby/System/Info/Public` for 1 h → healthchecks.io alerts | "Emby not answering on the LAN for 1 h" | Emby |
| TrueNAS alive | the TrueNAS cron job stopped pinging for 1 h | "TrueNAS not checking in for 1 h" | TrueNAS |
| Requests | the public URL doesn't answer as a visitor would see it (through Traefik and Authelia: a redirect to the login is "up"; no monitoring account in LLDAP, Claude's call), or Jellyseerr's status/settings API says Emby, Radarr or Sonarr is unreachable | "Requests: can't reach Radarr for 1 h" | Jellyseerr |
| Login (Authelia) | the login portal's health endpoint (`/api/health`) fails: every app behind Authelia is unusable | "Login portal down for 1 h" | Authelia |
| Downloads: VPN | gluetun not connected, or no forwarded port, or qBittorrent's listen port ≠ the forwarded port | "Downloads: VPN down for 1 h" | qBittorrent |
| Downloads: VPN flapping | covered by the **flaky** rule on "Downloads: VPN" (checked every minute through gluetun's control server). No pod restart counter: it needs the Kubernetes API, which containment denies to app pods (Claude's call) | "Downloads: VPN flaky, down 9 times / 34 min in the last 6 h" | qBittorrent |
| Downloads: stalled | there are active (non-paused, non-queued) downloads but total download progress hasn't moved for 1 h | "Downloads: nothing has moved for 1 h (N active)" | qBittorrent |
| Downloads: hand-off | Radarr/Sonarr health reports the download client unavailable, or queue items in an error/warning state for 1 h+ | "Radarr can't reach qBittorrent" | Radarr/Sonarr |
| Homelab offline | no ping to healthchecks.io for 1 h | "Homelab not checking in for 1 h" | — |
| TrueNAS alerts | anything TrueNAS raises at Warning or above (SMART, pool degraded/faulted, capacity, snapshot/replication failures, …) | TrueNAS's alert text | TrueNAS |

Not alerted: one slow or seedless torrent, a single short VPN hiccup (repeated ones are the **flaky** rule's job), items Decluttarr already handles (stalled/failed downloads it removes).

Storage capacity: TrueNAS's own pool-capacity alerts with its default thresholds (80% warning, 90% critical, 95%), on `tank` (HDD) and `tank2` (SSD); no separate thresholds.

## Left to the builder

- **Gatus vs another checker**, if Gatus can't express a check. Gatus alerts on consecutive failures only, so the **flaky** rule needs something on top (e.g. a small CronJob reading each check's history or uptime from Gatus's API and sending its own ntfy message, remembering what it already sent). The "stalled" check needs state across runs too (a script that computes yes/no and pushes to Gatus's external-endpoint API is fine).
- **Outside view of Emby**: the in-cluster check of the public URL goes out and back through the router (NAT loopback), not truly from the internet. Options: an external uptime service with ntfy or webhook support, or accept the NAT-loopback check plus the dead man's switch. Pick the simplest that really tests from outside; say which.
- **API credentials** the checks need (qBittorrent, gluetun control server — it already warns about missing auth —, Jellyseerr, Radarr/Sonarr keys): read-only where possible, stored as SOPS secrets, never printed.
- **TrueNAS → ntfy** method (native, webhook, or minimal forwarder) after checking TrueNAS 25.10's alert services; the cron script's exact form (TrueNAS cron task running `curl`), given to the admin to paste with secrets filled in by them.
- How the checker and pinger fit the containment policies (namespace, PSA `restricted`, no API token, A4 allow rules). Decided: the cluster checker doesn't call Emby's port 8096 directly (the LAN check runs on TrueNAS), so A4's Emby rule keeps no exception.
- Testing: each check proven by breaking it on purpose (or simulating it) and seeing one alert after the threshold and one recovery message; the flaky rule proven with a simulated on/off pattern (e.g. a test check failing a few minutes every half hour); then a quiet 24 h under normal use.

## What the admin needs to do

- Create the ntfy.sh account and token (or choose a topic), and a healthchecks.io account; hand the secrets over through SOPS-encrypted files (Claude never prints them).
- Install the ntfy app, subscribe, allow notifications.
- In TrueNAS: add the ntfy alert service (UI), and the cron job (UI), with the healthchecks.io ping URLs.
