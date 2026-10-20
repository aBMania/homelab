# Push Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The admin's Android phone gets an ntfy message when storage is in trouble or something people use is broken (Emby, requests/login, downloads, the homelab itself), and nothing else.

**Architecture:** Gatus in a new `monitoring` namespace runs the in-cluster checks and sends ntfy alerts after 1 h of failure; a Python probe CronJob computes the checks Gatus can't express and pushes them to Gatus external endpoints; a second CronJob reads Gatus's history and sends "flaky" messages. Outside the cluster, healthchecks.io watches three heartbeats (cluster, TrueNAS, Emby on the LAN), and TrueNAS posts its own alerts to ntfy through its Slack alert service. Emby's public URL is checked by Gatus through public DNS, so the request goes out to the public IP and back in through the router's port forward (NAT loopback, verified working). Gatus's read-only page is the status page at `status.${DOMAIN_0}`: open on the LAN, any logged-in user from outside (Authelia).

**Tech Stack:** Gatus v5.37.0 (bjw-s app-template 3.7.3), Python 3 stdlib (probe, flaky), curl, ntfy.sh, healthchecks.io, TrueNAS 25.10 alert services and cron tasks, Flux + SOPS.

**Spec:** `docs/superpowers/specs/2026-10-20-push-notifications-design.md`

## Global Constraints

- One PR per task; the user merges; merging deploys. Live cluster writes (kubectl apply/patch/delete, probe pods) only with the user's explicit OK per action.
- Down = **1 hour of continuous failure** (5-min checks: `failure-threshold: 12`; 1-min checks: `60`). One message, one recovery (`send-on-resolved: true`).
- Flaky = over the last **6 h**, failed **> 5 %** of the time or **≥ 3 outages**; an outage = **≥ 2 consecutive failed checks**; at most **one message per check per day**; a "back to stable" message when the 6-h window is clean; **silent (log-only) for the first week**.
- Priorities: never 5. App checks 3; TrueNAS alerts 3 (see Task 4 step 1); recoveries same as their alert (Gatus limitation, see Task 1).
- The repo is public: no domain, public IP or user name in git, commits or PR text (`${DOMAIN_0}` via Flux substitution; secrets via SOPS). The domain may appear in ntfy messages and links. No GitHub Actions check of Emby (the admin doesn't want a public workflow pointing at it).
- **No media titles** in any message or log line the probe/flaky scripts print: counts only.
- Gatus's own `${VAR}` env expansion collides with Flux substitution: write Gatus env references as `$${VAR}` in git.
- New namespace `monitoring`: PSA `enforce/warn/audit: restricted`, `automountServiceAccountToken: false`, non-root, no Kubernetes API access (A3 already denies it).
- Status page `status.${DOMAIN_0}`: **no login on the LAN, Authelia `one_factor` from outside for any user (no group restriction)**. Gatus's UI is read-only; its push API stays protected by `GATUS_PUSH_TOKEN`.
- Secrets are never printed in terminal output, commits or PRs; values copied between SOPS files go through pipes, not the screen.

## Rulings in this plan (deviations from the spec's examples)

- **VPN check = qBittorrent's `connection_status == "connected"`**, not gluetun's control server: that server (port 8000) isn't in any Service and can change the VPN; exposing it cluster-wide would be a new risk. "connected" already means tunnel up, port forwarded and reachable. Cost if wrong: a VPN that is up but whose forwarded port is broken shows as down (that is still "downloads broken").
- **"Requests can reach Emby/Radarr/Sonarr" = the probe reaches them** from `monitoring`, plus Jellyseerr's own status endpoint; Jellyseerr's per-server test endpoints need full server settings and its admin key. Cost: a Jellyseerr-only misconfiguration (wrong URL in its settings) isn't caught.
- **Hand-off = arr health only** (download-client checks), not queue items in error: long-lived warning items (blocked imports) would alert forever, and Decluttarr already removes failed ones. Cost: an import failure that isn't a client outage isn't alerted.
- **Recoveries are sent at the alert's priority** (Gatus's ntfy provider has one priority per provider); the flaky job's "stable" messages use 2.
- **TrueNAS alerts are all priority 3** (Task 4 step 1).
- **Planned downtime** is excluded by `monitoring` scaling down with `node:down` (no results recorded); a Flux suspension without `node:down` isn't detected. Cost: a deliberate long manual outage can produce a "down" alert.
- **Status page through Authelia rules, not a Traefik IP allow-list**: one ingress with the `auth` middleware; Authelia `bypass` for the `lan` network and `one_factor` with no subject otherwise, so access control stays in one place. Cost: a LAN device (guest Wi-Fi on the same subnet) sees the page without logging in; it shows check names and app URLs, no secrets.
- **Emby "from outside" = Gatus via public DNS + NAT loopback**, not a third-party or GitHub Actions probe (the admin's choice): it covers stale DDNS, a broken port forward, Traefik, the certificate and Emby. Cost: an ISP-side block of inbound traffic, or a router WAN fault that leaves loopback working, isn't caught (home internet down is caught by the heartbeat).

## Review Focus

1. A check that fails during **planned maintenance** (`mise run node:down`, Renovate restarts) must not alert: `monitoring` is not in `NODE_DOWN_KEEP_NS`, so Gatus and the CronJobs scale down with the apps and record nothing (Task 1 step pins it; Task 3 test `test_gap_in_results_is_not_an_outage`).
2. **Gatus restarts** lose nothing the flaky rule needs: results live in SQLite on a PVC with enough results kept for 6 h of 1-min checks (Task 1 config `maximum-number-of-results: 500`; Task 3 test `test_window_uses_timestamps_not_count`).
3. **The probe can't log in** (qBittorrent password changed, arr key rotated): the check must fail visibly ("can't log in"), not pass silently or crash without pushing (Task 2 test `test_login_failure_reports_failure`).
4. **The flaky state file is missing or corrupt** (first run, PVC wiped): the job starts fresh, doesn't crash, doesn't resend everything (Task 3 test `test_missing_state_starts_fresh`).
5. **A message would contain a torrent or film name**: the probe only reports counts (Task 2 test `test_messages_have_no_titles`).

---

### Task 1: Accounts, Gatus, HTTP checks, status page, cluster heartbeat

**Files:**
- Create: `clusters/main/kubernetes/core/gatus/ks.yaml` (Kustomization `gatus`, path `clusters/main/kubernetes/core/gatus/app`, copy `core/blocky/ks.yaml`'s shape)
- Create: `clusters/main/kubernetes/core/gatus/app/{kustomization.yaml,namespace.yaml,helm-release.yaml,gatus-data-pvc.yaml,alerting.secret.yaml,config/gatus.yaml,heartbeat-cronjob.yaml}`
- Modify: `clusters/main/kubernetes/core/kustomization.yaml` (add `- gatus/ks.yaml`)
- Modify: `clusters/main/kubernetes/auth/authelia/app/helm-release.yaml` (two `access_control` rules for `status.${DOMAIN_0}`, step 4b)
- Modify: `docs/superpowers/plans/2026-10-18-network-containment.md` Task 6 (A4): add "`monitoring` → media qbittorrent:10095, radarr:7878, radarr-4k:7878, sonarr:8989, jellyseerr:10241; → authelia:9091; nothing to Emby :8096; Traefik → `monitoring` gatus:8080 (status page)"

**Interfaces:**
- Produces: Service `gatus.monitoring.svc:8080`; external endpoints (pushed by Task 2) with keys `downloads_vpn-connected`, `downloads_stalled`, `downloads_radarr-download-client`, `downloads_radarr-4k-download-client`, `downloads_sonarr-download-client`, `requests_backends`, accepting `POST /api/v1/endpoints/<key>/external?success=<bool>&error=<text>` with `Authorization: Bearer $GATUS_PUSH_TOKEN`; history at `GET /api/v1/endpoints/<key>/statuses?page=1&pageSize=500` (Task 3).
- Produces: Secret `monitoring/alerting` keys `NTFY_TOPIC`, `NTFY_TOKEN`, `GATUS_PUSH_TOKEN`, `HC_CLUSTER_PING_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD`, `RADARR_API_KEY`, `RADARR_4K_API_KEY`, `SONARR_API_KEY`.

- [ ] **Step 1 (user):** create a free ntfy.sh account, an access token, and a reserved topic; subscribe in the Android app. Create a healthchecks.io account with an **ntfy integration** (server `https://ntfy.sh`, the topic, the token, priority 3) and three checks, period 5 min, grace 55 min: `cluster`, `truenas`, `emby-lan`. Send one test notification from healthchecks.io; it arrives on the phone.
- [ ] **Step 2:** write `alerting.secret.yaml` (namespace `monitoring`, `stringData`) and encrypt it with `sops -e -i`. The user types the ntfy/healthchecks values into the file themselves (or pipes them in); the qBittorrent/arr values are piped from `media/decluttarr/app/decluttarr.secret.yaml` with `sops -d … | yq … | ` into the new file, never printed. `GATUS_PUSH_TOKEN`: 32 random bytes (`openssl rand -hex 32`, piped). Verify: `mise run sops:check` passes; `sops -d alerting.secret.yaml | yq '.stringData | keys'` lists exactly the 9 keys.
- [ ] **Step 3:** `config/gatus.yaml`, turned into ConfigMap `gatus-config` by `configMapGenerator` (`disableNameSuffixHash: false`, so a config change rolls the pod). Pinned values:
  - `storage: {type: sqlite, path: /data/data.db, maximum-number-of-results: 500, maximum-number-of-events: 100}`
  - `alerting.ntfy: {url: https://ntfy.sh, topic: $${NTFY_TOPIC}, token: $${NTFY_TOKEN}, priority: 3, default-alert: {failure-threshold: 12, success-threshold: 1, send-on-resolved: true}}`
  - endpoints (group / name, interval, URL, conditions, alert description):
    - `requests` / `public`, 5m, `https://requests.${DOMAIN_0}/`, `[STATUS] < 400` (Authelia's 302 counts), `client: {ignore-redirect: true}`, "Requests: unreachable through Traefik/Authelia for 1 h"
    - `login` / `authelia`, 5m, `http://authelia.authelia.svc:9091/api/health`, `[STATUS] == 200`, "Login portal down for 1 h"
    - `emby` / `public`, 5m, `https://emby.${DOMAIN_0}/emby/System/Info/Public`, `client: {dns-resolver: "tcp://1.1.1.1:53"}` (public DNS, so the request goes to the public IP and back through the router's port forward instead of Blocky's LAN answer), conditions `[STATUS] == 200` and `[CERTIFICATE_EXPIRATION] > 72h`, "Emby unreachable at its public address for 1 h (DNS, router port forward, Traefik or Emby)"
  - `external-endpoints`: the six keys from Interfaces, `token: $${GATUS_PUSH_TOKEN}`; `downloads_vpn-connected` alert `failure-threshold: 60` (pushed every minute), the others 12.
  - Each alert's `description` is the message text; ntfy click links come from Gatus's endpoint URL.
- [ ] **Step 4:** `helm-release.yaml` (app-template 3.7.3, like `media/decluttarr`): image `ghcr.io/twin/gatus:v5.37.0`, env from Secret `alerting`, `GATUS_CONFIG_PATH=/config/gatus.yaml`, config mounted from `gatus-config`, `/data` from PVC `gatus-data` (`local-data`, 1Gi, `kustomize.toolkit.fluxcd.io/prune: disabled`), port 8080, Service `gatus`, ingress `status.${DOMAIN_0}` shaped like `media/pinchflat`'s (annotations `cert-manager.io/cluster-issuer: domain-0-le-prod`, `router.entrypoints: websecure`, `router.tls: "true"`, `router.middlewares: traefik-auth@kubernetescrd`; TLS secret `gatus-tls`). Gatus `ui: {title: Status, header: Status}`. Pod: `automountServiceAccountToken: false`, `runAsNonRoot`, uid/gid/fsGroup 65534, seccomp RuntimeDefault; container: no privilege escalation, drop ALL, `readOnlyRootFilesystem: true`. Probes on `/health`.
- [ ] **Step 4b:** Authelia rules, inserted **before** the "Admins at home" rules (first match wins; the wildcard admin rules would otherwise demand a login on the LAN and refuse non-admins):
  ```yaml
          # Status page: open at home, any user from outside
          - domain:
              - "status.${DOMAIN_0}"
            policy: bypass
            networks:
              - lan
          - domain:
              - "status.${DOMAIN_0}"
            policy: one_factor
  ```
  Verify: `helm template` of the authelia chart with these values renders both rules in the configuration, in that order.
- [ ] **Step 5:** `heartbeat-cronjob.yaml`: every 5 min, `curlimages/curl` (exact tag), `curl -fsS -m 10 http://gatus.monitoring.svc:8080/health && curl -fsS -m 10 --retry 3 "$HC_CLUSTER_PING_URL"` (ping only while Gatus is healthy: a dead checker stops the heartbeat). Same securityContext; `concurrencyPolicy: Forbid`, `successfulJobsHistoryLimit: 1`.
- [ ] **Step 6:** `namespace.yaml`: `monitoring`, PSA labels `restricted` ×3. Do **not** add `monitoring` to `NODE_DOWN_KEEP_NS` (Review Focus 1); say so in a comment in `namespace.yaml`.
- [ ] **Step 7: Validate locally.** `kustomize build clusters/main/kubernetes/core/gatus/app`, `bash scripts/kubeconform.sh ./clusters/main/kubernetes` → exit 0. Gatus parses the config: render the ConfigMap, replace `$${` with `${`, run `docker run --rm -e NTFY_TOPIC=x -e NTFY_TOKEN=x -e GATUS_PUSH_TOKEN=x -e GATUS_CONFIG_PATH=/c/gatus.yaml -v <dir>:/c ghcr.io/twin/gatus:v5.37.0` → it starts and logs the endpoints (Ctrl-C). Server dry-run of the rendered Deployment pod spec in a `restricted` namespace (as in #1069) → accepted.
- [ ] **Step 8: Commit, PR.** `feat(monitoring): Gatus checks with ntfy alerts, status page, cluster heartbeat`.
- [ ] **Step 9 (after merge):** pods Running; `kubectl -n monitoring port-forward svc/gatus 8080` → `/api/v1/endpoints/statuses` shows the 3 HTTP endpoints succeeding and the 6 external ones with no results yet; healthchecks.io `cluster` check is "up". Status page: the Certificate `gatus-tls` is Ready before anyone opens it (Let's Encrypt limit); on the LAN `https://status.${DOMAIN_0}` opens without a login; from mobile data it redirects to Authelia, and a non-admin account (the user tries one) gets the page after its password. Alert path proven once (needs the user's OK): add a temporary endpoint `test/always-fails` (`http://127.0.0.1:1/`, 1m, `failure-threshold: 2`) in a PR, see one ntfy message and, after removing it, nothing more.

### Task 2: Probe CronJob (downloads, arrs, requests backends)

**Files:**
- Create: `clusters/main/kubernetes/core/gatus/app/probe/probe.py`, `probe-cronjob.yaml`; ConfigMap `gatus-probe` from `configMapGenerator`
- Test: `scripts/tests/test_gatus_probe.py` (unittest, `sys.path` insert of the probe folder, like `test_media_stats.py`)

**Interfaces:**
- Consumes: Task 1's push API and Secret keys.
- Produces (pure functions, unit-tested):
  - `vpn_connected(transfer_info: dict) -> tuple[bool, str]`: `transfer_info` is qBittorrent `GET /api/v2/transfer/info`; success iff `connection_status == "connected"` (port forwarded and reachable through the VPN); message e.g. `"qBittorrent connection: firewalled"`.
  - `stalled(torrents: list[dict], transfer_info: dict) -> tuple[bool, str]`: success unless there is ≥ 1 torrent in state `downloading`/`stalledDL`/`metaDL`/`forcedDL` **and** `dl_info_speed == 0`; message `"N active downloads, 0 B/s"` (count only).
  - `download_client_ok(health: list[dict], app: str) -> tuple[bool, str]`: arr `GET /api/v3/health`; failure iff an entry has `source` in `{"DownloadClientCheck", "DownloadClientStatusCheck"}`; message `"<app>: download client unavailable"`.
  - `backends_ok(results: dict[str, bool]) -> tuple[bool, str]`: failure iff any of `emby`, `radarr`, `sonarr` is false; message lists the failing names.
  - `push(key: str, ok: bool, error: str) -> None`: POST to Gatus; retries 3×; never raises out of `main()`.
  - `main(mode: str) -> int`: `mode` `vpn` (every minute: only `downloads_vpn-connected`) or `all` (every 5 min: the other five keys).

- [ ] **Step 1: Write failing tests** in `test_gatus_probe.py`:
  - `test_vpn_connected_ok` (`{"connection_status": "connected"}` → `(True, …)`), `test_vpn_firewalled_fails`, `test_vpn_disconnected_fails`
  - `test_stalled_when_active_and_zero_speed` (2 torrents `downloading`, speed 0 → `(False, "2 active downloads, 0 B/s")`), `test_not_stalled_when_moving`, `test_not_stalled_when_nothing_active` (only `pausedDL`/`queuedDL`/`uploading`)
  - `test_download_client_unavailable` (health with `source: "DownloadClientStatusCheck"` → False), `test_other_health_issues_ignored` (`IndexerStatusCheck` → True)
  - `test_backends_lists_failures` (`{"emby": True, "radarr": False, "sonarr": False}` → `(False, "radarr, sonarr unreachable")`)
  - `test_login_failure_reports_failure` (Review Focus 3): with a fake HTTP layer whose qBittorrent login returns `Fails.`, `main("vpn")` pushes `downloads_vpn-connected` with `success=false` and `error` containing `"login"`, and returns 0
  - `test_messages_have_no_titles` (Review Focus 5): torrents with `name` fields set to sentinel strings; no sentinel appears in any returned message
- [ ] **Step 2: Run** `python3 -m unittest scripts/tests/test_gatus_probe.py -v` → fails (module missing).
- [ ] **Step 3: Implement `probe.py`** (stdlib only: `urllib.request`, `http.cookiejar`, `json`). Base URLs: `http://qbittorrent.media.svc:10095`, `http://radarr.media.svc:7878`, `http://radarr-4k.media.svc:7878`, `http://sonarr.media.svc:8989`, `http://jellyseerr.media.svc:10241/api/v1/status`, `http://emby.media.svc:10079/emby/System/Info/Public`. `backends_ok` inputs: HTTP 200 from Emby's public info, Radarr's and Sonarr's `/api/v3/system/status` (with key). qBittorrent: `POST /api/v2/auth/login` (cookie jar), then `transfer/info`, `torrents/info?filter=all`. Any exception for one check → push that key as failed with a short reason; other checks still run. Logs: one line per key, `key ok|fail reason` (no titles).
- [ ] **Step 4: Run tests** → all pass.
- [ ] **Step 5:** `probe-cronjob.yaml`: two CronJobs from one script, `gatus-probe-vpn` (`* * * * *`, `python /probe/probe.py vpn`) and `gatus-probe` (`*/5 * * * *`, `all`); image `python:3.13-alpine` pinned to an exact tag; env from Secret `alerting`; securityContext as Task 1; `concurrencyPolicy: Forbid`; `activeDeadlineSeconds: 50` (vpn) / `240`; history limits 1.
- [ ] **Step 6: Validate:** kustomize build, kubeconform, server dry-run of a Job pod spec in a `restricted` namespace. Commit; PR `feat(monitoring): probe for downloads, arrs and requests`.
- [ ] **Step 7 (after merge):** within 10 min all six external endpoints show results in Gatus; `downloads_vpn-connected` succeeding; job logs show no titles.

### Task 3: Flaky detector (silent first)

**Files:**
- Create: `clusters/main/kubernetes/core/gatus/app/flaky/flaky.py`, `flaky-cronjob.yaml`, `flaky-state-pvc.yaml` (`local-data`, 64Mi, prune disabled); ConfigMap `gatus-flaky` from `configMapGenerator`
- Test: `scripts/tests/test_gatus_flaky.py`

**Interfaces:**
- Consumes: Gatus `GET /api/v1/endpoints/statuses` (keys) and `GET /api/v1/endpoints/<key>/statuses?page=1&pageSize=500` (`results[].success`, `results[].timestamp` RFC 3339).
- Produces:
  - `outages(results: list[dict], now: datetime, window: timedelta, interval: timedelta) -> list[tuple[datetime, datetime]]`: runs of ≥ 2 consecutive failures inside the window; a gap > 3× interval between results ends a run and is not a failure.
  - `assess(results, now, interval) -> Verdict` with `Verdict(flaky: bool, down_now: bool, outage_count: int, failed_minutes: int, failed_ratio: float)`: window 6 h; `flaky = (failed_ratio > 0.05 or outage_count >= 3) and not down_now`; `down_now` = currently ≥ 1 h of continuous failure (Gatus already alerts that).
  - `decide(key: str, verdict: Verdict, state: dict, today: date) -> tuple[str | None, dict]`: returns `"flaky"`, `"stable"` or None plus new state; `"flaky"` at most once per `today` per key; `"stable"` only after a `"flaky"` and when the window has 0 outages.
  - `message(key: str, verdict: Verdict) -> str`: e.g. `"downloads/vpn-connected flaky: down 9 times / 34 min in the last 6 h"`.
  - `main() -> int`: env `FLAKY_NOTIFY` (`"false"` → log what it would send, update state anyway), `NTFY_TOPIC`, `NTFY_TOKEN`; state JSON at `/state/flaky.json`.

- [ ] **Step 1: Write failing tests**:
  - `test_single_failure_is_not_an_outage`; `test_two_consecutive_failures_is_one_outage`
  - `test_three_outages_is_flaky` (3 separate 2-min outages in 6 h of 1-min results → `flaky=True`); `test_ratio_over_5_percent_is_flaky` (one 20-min outage, 360 results → ratio 0.055 → flaky); `test_under_thresholds_not_flaky` (2 outages, 10 min)
  - `test_down_now_is_not_flaky` (last 70 min all failing → `down_now=True`, `flaky=False`)
  - `test_gap_in_results_is_not_an_outage` (Review Focus 1: 2-h gap with no results between successes → 0 outages)
  - `test_window_uses_timestamps_not_count` (Review Focus 2: results older than 6 h ignored even when fewer than 500 are returned)
  - `test_flaky_sent_once_per_day`; `test_stable_after_flaky_when_window_clean`; `test_no_stable_without_prior_flaky`
  - `test_missing_state_starts_fresh` (Review Focus 4: missing and invalid JSON state → `{}`, no exception)
  - `test_silent_mode_logs_but_does_not_send` (fake sender not called when `FLAKY_NOTIFY=false`; state still updated)
- [ ] **Step 2: Run** `python3 -m unittest scripts/tests/test_gatus_flaky.py -v` → fails.
- [ ] **Step 3: Implement `flaky.py`** (stdlib). Interval per key: 1 min for `downloads_vpn-connected`, 5 min for the rest. ntfy send: `POST https://ntfy.sh/<topic>` with `Authorization: Bearer`, `Title`, `Priority: 3` (`2` for "stable"), `Tags: warning` / `white_check_mark`.
- [ ] **Step 4: Run tests** → pass.
- [ ] **Step 5:** `flaky-cronjob.yaml`: `*/15 * * * *`, same pinned python image, env `FLAKY_NOTIFY: "false"`, `/state` from PVC `flaky-state`, securityContext as Task 1 (fsGroup 65534 so the PVC is writable).
- [ ] **Step 6: Validate, commit, PR** `feat(monitoring): flaky detector (log-only for a week)`.
- [ ] **Step 7 (after merge):** job logs show one line per key with its verdict; `/state/flaky.json` written; no ntfy message sent.

### Task 4: TrueNAS alerts, TrueNAS cron, runbook (user UI steps)

**Files:**
- Create: `docs/push-notifications.md` (runbook: what alerts exist, what each means, where to look, how to pause during long maintenance, the TrueNAS steps below)
- Modify: `AGENTS.md` (Operations notes: 3-line "Alerts" pointer to the runbook; mention pausing healthchecks.io checks before a long `node:down`)

**Interfaces:**
- Consumes: healthchecks.io checks `truenas`, `emby-lan` (Task 1 step 1).

- [ ] **Step 1: TrueNAS alert service (user, UI).** TrueNAS 25.10 has no ntfy or generic webhook service (checked: `middlewared/alert/service/` has aws_sns, influxdb, mail, mattermost, opsgenie, pagerduty, slack, snmp_trap, telegram, victorops). The **Slack** service posts `{"text": "…"}`; ntfy turns it into a message with templating (verified on a throwaway topic). Add a Slack alert service, level **Warning**, URL `https://ntfy.sh/<topic>?tpl=yes&t=TrueNAS&m={{.text}}&auth=<base64url("Bearer <token>") without padding>`; press "Send Test Alert" → message on the phone. Ruling: TrueNAS can't vary the request per alert level, so all TrueNAS alerts use priority 3 (the spec's "Critical → 4" isn't possible with this path; acceptable since nothing may ring anyway).
- [ ] **Step 2: TrueNAS cron task (user, UI):** every 5 min, user `root` not required (any user with network access), command:
  `curl -fsS -m 10 --retry 3 '<HC truenas URL>' >/dev/null; curl -fsS -m 10 http://192.168.1.112:8096/emby/System/Info/Public >/dev/null && curl -fsS -m 10 --retry 3 '<HC emby-lan URL>' >/dev/null`
  healthchecks.io `truenas` and `emby-lan` turn "up" within 10 min.
- [ ] **Step 3:** runbook + AGENTS.md pointer; commit; PR `docs(monitoring): alerts runbook, TrueNAS steps`.
- [ ] **Step 4 (after merge, with the user):** all three healthchecks.io checks "up"; Gatus all green; a 24 h period with normal use produces no message.

### Task 5: Turn on flaky notifications (after one silent week)

**Files:**
- Modify: `clusters/main/kubernetes/core/gatus/app/flaky-cronjob.yaml` (`FLAKY_NOTIFY: "true"`), and thresholds in `flaky.py` only if the week's logs justify it
- Modify: `docs/push-notifications.md` (record the week's numbers and the thresholds kept)

- [ ] **Step 1:** read the week of flaky job logs (`kubectl -n monitoring logs` of recent jobs, or the state file): per key, how many days would have sent "flaky". Expected: only keys with real trouble (the VPN, if Proton blackouts continue).
- [ ] **Step 2:** if a key would have fired on most days without a real problem, adjust its thresholds with a test first (`test_<key>_threshold`), else keep the spec's values.
- [ ] **Step 3:** flip `FLAKY_NOTIFY`, commit, PR `feat(monitoring): flaky notifications on`.
- [ ] **Step 4 (after merge):** next real flaky episode → one message; or, with the user's OK, simulate one by stopping qBittorrent's deployment for 3 × 3 min within an hour (Emby idle check first) and see one "flaky" message, then "back to stable" 6 h later.
