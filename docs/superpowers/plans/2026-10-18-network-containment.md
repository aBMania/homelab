# Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Limit how far a compromised app can spread: no LAN, no API/node access for app pods, ingress isolation per app namespace, least-privilege pods, deliberate internet exposure.

**Architecture:** Cilium policies in `core/network-policies/` (cluster-wide deny-only rules plus one CiliumNetworkPolicy per app namespace), workload hardening through chart values or Flux `postRenderers`, PSA namespace labels, Authelia/Traefik changes for exposure. Each task is one PR; merging deploys.

**Tech Stack:** Flux (Kustomization + HelmRelease), Cilium 1.20.3 (CiliumClusterwideNetworkPolicy, CiliumNetworkPolicy, Hubble), Kubernetes Pod Security Admission, Authelia, Traefik, mise tasks (bash).

**Spec:** `docs/superpowers/specs/2026-10-18-network-containment-design.md`

## Global Constraints

- Every change goes through a PR to `main` (protected); merging deploys. Never add the `automerge` label.
- Never write the domain in files, commits or PR text: use `${DOMAIN_0}`. TrueNAS is `${NFS_HOST}` in manifests.
- No `Co-Authored-By` trailer in commits.
- Policies live in `clusters/main/kubernetes/core/network-policies/app/`, one file per concern, listed in its `kustomization.yaml`.
- App namespaces: `media`, `panio`, `suivi-saeg`, `whosaid`, `jupyter`, `blocky`, `cloudflareddns`, `authelia`, `lldap`. Never add policies to system namespaces.
- Private ranges: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`.
- Deny-only cluster-wide policies set `enableDefaultDeny: {egress: false, ingress: false}`.
- Any Cilium values change: check the rendered `cilium-config` values the agent parses itself (filters are JSON strings), then watch `kubectl -n kube-system rollout status ds/cilium` right after deploy; warn the user of the agent restart (network blip) before merging.
- Before each PR: `bash ./scripts/kubeconform.sh ./clusters/main/kubernetes` passes; new Cilium objects pass `kubectl apply --dry-run=server` with variables substituted by hand (`sed 's#\${NFS_HOST}#192.168.1.73#g'`).
- Probe pods: busybox, temporary namespace, print only connected/blocked, deleted at the end. Never print media titles or file names.
- Live cluster writes outside git (kubectl patch/label, cilium-dbg endpoint config) only with the user's explicit OK, per action.

## Review Focus

- **LAN clients resolving through Blocky** (192.168.1.111:53) after A2 and A4: `dig @192.168.1.111 example.com` from the laptop must still answer — test in Task 2 and Task 6 (blocky).
- **The projector finding and playing Emby** after A4 on `media`: discovery (UDP 7359 via the host-network relay) and playback must keep working — user check in Task 6 (media).
- **CNPG Postgres pods after A3**: they talk to the API (instance manager); clusters must stay `Cluster in healthy state` — test in Task 3.
- **A source missing from `net:inbound`** because the app wasn't used during the evidence window (e.g. the projector): the user exercises each app before A4 enforces; `net:drops <ns>` right after merge — Task 6.
- **`${NFS_HOST}` left unsubstituted** in a new policy would match nothing: check the live object's CIDR after each deploy — step in Task 2.

---

### Task 1: Rollout tooling: the `inbound` flow log and `net:inbound` (E1)

**Files:**
- Modify: `clusters/main/kubernetes/kube-system/cilium/app/helm-release.yaml` (`hubble.export.dynamic`)
- Create: `scripts/net-inbound.sh`
- Modify: `mise.toml` (task `net:inbound`)

**Interfaces:**
- Produces: `mise run net:inbound [namespace]`: one line per source (`<ns>/<workload>`, `LAN:<ip>` or `internet`) → `<ns>/<workload>` and port, with count and last seen.

- [ ] **Step 1:** Dynamic flow log `inbound` → `/var/run/cilium/hubble/inbound.log`: include TCP SYNs (`reply: [false]`, `tcp_flags: [{SYN: true}]`) and UDP (`protocol: [udp]`) to `destination_pod` in the app namespaces; exclude `source_label: ["reserved:host"]` and Blocky port 53; field mask to the fields the summary needs. Render with `helm template`: `hubble-flowlogs-config-path` set, ConfigMap `cilium-flowlog-config` as intended, static export unchanged.
- [ ] **Step 2:** `scripts/net-inbound.sh [namespace]`: cat `inbound*.log` from the agent (oldest first), aggregate with jq/awk; public source IPs print as `internet`. Test with a fake `kubectl` serving fixture flows: grouping by workload, LAN vs internet, namespace filter, empty file message.
- [ ] **Step 3:** Commit, PR (warn: agent restart). After merge: `rollout status ds/cilium`; agent log has no `Failed to parse dynamic exporter config`; after an hour `mise run net:inbound` shows Traefik → app lines; check the file's growth rate (10 MB × 6 files).

### Task 2: Pods → private ranges (A2)

**Files:**
- Create: `clusters/main/kubernetes/core/network-policies/app/private-ranges.yaml`
- Modify: `clusters/main/kubernetes/core/network-policies/app/truenas.yaml` (`traefik-egress`)
- Modify: `clusters/main/kubernetes/core/network-policies/app/kustomization.yaml`

**Interfaces:**
- Produces: CCNP `deny-pods-to-private-ranges`; `traefik-egress` with private ranges excluded.

- [ ] **Step 1:** Before writing: sample 5 minutes of flows to private ranges (`hubble observe -P --follow --to-ip 192.168.0.0/16` etc. aggregated per namespace as in the design's sample). Expected: only `traefik → ${NFS_HOST}:444`. Anything else: stop and report.
- [ ] **Step 2:** `private-ranges.yaml`: CCNP `deny-pods-to-private-ranges`, `endpointSelector` = namespace `NotIn [traefik]`, deny-only, `egressDeny: toCIDR: [10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16]`. Header comment: why, and "pod/service CIDRs are cluster identities, not matched".
- [ ] **Step 3:** `traefik-egress`: the `toCIDRSet` rule becomes `cidr: 0.0.0.0/0`, `except: [10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16]`; keep `toEntities: [cluster]` and `${NFS_HOST}:444`.
- [ ] **Step 4:** kubeconform + server dry-run; commit; PR. Ships directly when the sample is clean.
- [ ] **Step 5 (after merge):** live objects show `192.168.1.73/32` substituted; probe pod: `192.168.1.1:80` blocked, `1.1.1.1:443` connected, `kubernetes.default.svc:443` connected; `dig @192.168.1.111` from the laptop answers; the user loads the TrueNAS UI; `mise run net:drops` shows only the probe's drops.

### Task 3: App pods → API server and node (A3)

**Files:**
- Create: `clusters/main/kubernetes/core/network-policies/app/api-and-node.yaml`
- Modify: `clusters/main/kubernetes/core/network-policies/app/kustomization.yaml`

**Interfaces:**
- Produces: CCNP `deny-apps-to-api-and-node`.

- [ ] **Step 1:** Confirm API users in app namespaces: pods whose service account has RBAC (design table: blocky, CNPG instances) and a 5-minute sample `hubble observe -P --follow --to-identity kube-apiserver` / `--to-identity host` aggregated per namespace. Expected: no app namespace besides `blocky` and CNPG pods. Anything else: add a labelled exclusion with a comment, or stop and report.
- [ ] **Step 2:** `api-and-node.yaml`: CCNP `deny-apps-to-api-and-node`, `endpointSelector.matchExpressions`: namespace `In [media, panio, suivi-saeg, whosaid, jupyter, cloudflareddns, authelia, lldap]` and `cnpg.io/cluster DoesNotExist`; deny-only; `egressDeny: toEntities: [kube-apiserver, host]`.
- [ ] **Step 3:** kubeconform + server dry-run; commit; PR.
- [ ] **Step 4 (after merge, user's OK to run a probe pod in `whosaid`, an app namespace with no external users):** probe pod `tcp-probe` in `whosaid`, deleted at the end: `kubernetes.default.svc:443`, `192.168.1.101:10250`, `192.168.1.101:50000` blocked; `1.1.1.1:443` connected. `kubectl get clusters.postgresql.cnpg.io -A` all healthy after 10 minutes; Blocky resolves an app name (`dig @192.168.1.111 radarr.<domain>`, masked in output).

### Task 4: Drop unused API tokens, run as non-root (B1, B2)

**Files:**
- Modify: `clusters/main/kubernetes/media/decluttarr/app/helm-release.yaml`, `clusters/main/kubernetes/media/pinchflat/app/helm-release.yaml` (app-template: `defaultPodOptions.automountServiceAccountToken: false`; decluttarr also `defaultPodOptions.securityContext` `runAsUser/runAsGroup/fsGroup: 568`, `runAsNonRoot: true`)
- Modify: `clusters/main/kubernetes/apps/panio/app/helm-release.yaml`, `clusters/main/kubernetes/apps/suivi-saeg/app/helm-release.yaml` (Flux `spec.postRenderers[].kustomize.patches` on the Deployment: `automountServiceAccountToken: false`, pod `securityContext` `runAsNonRoot: true`, `runAsUser: <image user>`, `seccompProfile: RuntimeDefault`; container `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`)

- [ ] **Step 1:** For panio and suivi-saeg, find the image's user (`docker image inspect` / the Dockerfile in the fait-maison repo); if it's root, run as 568 only if the app writes nowhere it can't (check its volumes), otherwise stop and report (fix belongs in the image).
- [ ] **Step 2:** Edit the four HelmReleases; render each (`helm template` with the values, plus `kustomize build` for the postRenderer apps through `flux build` if available, else check after deploy).
- [ ] **Step 3:** kubeconform; commit; PR.
- [ ] **Step 4 (after merge):** each pod Running; `kubectl get pod -o jsonpath='{.spec.automountServiceAccountToken}'` = `false`; no `/var/run/secrets/kubernetes.io/serviceaccount` in the containers; panio/suivi-saeg URLs load; decluttarr logs show a normal check cycle.

### Task 5: Internet exposure decisions (C1, C4)

Depends on the user's answers (design "Questions" 1 and 3). `panio`: public without auth, no change (answered 2026-10-19). `suivi-saeg` and C4 still open.

**Files (C1, if Authelia is chosen):**
- Modify: `clusters/main/kubernetes/apps/panio/app/helm-release.yaml`, `clusters/main/kubernetes/apps/suivi-saeg/app/helm-release.yaml` (ingress annotation `traefik.ingress.kubernetes.io/router.middlewares: traefik-auth@kubernetescrd`, or `traefik-local@kubernetescrd` for LAN-only)
- Modify: `clusters/main/kubernetes/auth/authelia/app/helm-release.yaml` (a rule for `panio.${DOMAIN_0}` / `suivi-saeg.${DOMAIN_0}` with the group the user names; admins are covered by the wildcard rules)

- [ ] **Step 1:** Apply the user's choice per app; render; kubeconform; PR.
- [ ] **Step 2 (after merge):** unauthenticated request through Traefik (`curl --resolve <host>:443:192.168.1.110`, host masked in output) returns 302 to Authelia (or 403 from outside for `local`); the user logs in and the app works.
- [ ] **Step 3 (C4, outside git):** per the user's choice: nothing (record the accepted risk in the design), router source restriction (check the Neufbox UI supports it), or a VPN plan (separate design). No cluster change.

### Task 6: Ingress isolation per app namespace (A4)

`media`: Emby :8096 must only be reachable from Traefik (and the node). Emby trusts `X-Forwarded-For` from any address (design C2b), so this rule is what stops a pod from posing as a LAN client.

One PR per namespace, in this order: `media`, `panio`, `suivi-saeg`, `jupyter`, `blocky`, `whosaid`, `cloudflareddns`.

**Files:**
- Create: `clusters/main/kubernetes/core/network-policies/app/ingress-<namespace>.yaml` (one per PR)
- Modify: `clusters/main/kubernetes/core/network-policies/app/kustomization.yaml`

**Interfaces:**
- Produces: CNP `ingress` in each namespace.

- [ ] **Step 1:** Policy `ingress` (CiliumNetworkPolicy, namespace = the app namespace, `endpointSelector: {}`), ingress rules:
  - `fromEndpoints: [{matchLabels: {k8s:io.kubernetes.pod.namespace: traefik}}]`
  - `fromEndpoints: [{}]` (same namespace)
  - `blocky` only: `fromEntities: [world, host]` with `toPorts: 53/UDP, 53/TCP`
  - `whosaid`, `cloudflareddns`: only the same-namespace rule (no Traefik)
  - from `monitoring` (push notifications, Gatus + probe): `media` qbittorrent:10095, radarr:7878, radarr-4k:7878, sonarr:8989, jellyseerr:10241; `authelia` :9091; nothing to Emby :8096 (the LAN check runs on TrueNAS)
  - `monitoring` itself: from Traefik to gatus:8080 (status page) and same namespace (probe and heartbeat → Gatus)
- [ ] **Step 2:** `mise run net:inbound <namespace>` covering a few days of normal use (the user exercises each app). Each source: a rule in Step 1's policy with a comment, or a written reason to stay blocked.
- [ ] **Step 3:** kubeconform + server dry-run; commit; PR; merge.
- [ ] **Step 4:** right after merge: `mise run net:drops <namespace>` (expected: nothing legitimate); acceptance: probe pod in a temporary namespace → a pod of `<namespace>` on its service port: blocked; every app URL of the namespace loads; `media`: the user checks the projector discovers and plays Emby; `blocky`: `dig @192.168.1.111` from the laptop answers. Anything legitimate dropped: fix in git or revert.

### Task 7: Pod Security levels (B3)

App namespaces other than `media`: done in #1059 (enforced, pods restarted 2026-10-19). `media`: the user chose a Kyverno policy with an exception for qBittorrent (design B3).

**Files:**
- Modify: the `namespace.yaml` of each app namespace (labels `pod-security.kubernetes.io/enforce`, `.../warn`, `.../audit`)
- (`media`) Create: `clusters/main/kubernetes/core/kyverno-policies/app/media-baseline.yaml`, listed in that folder's `kustomization.yaml`: a policy enforcing the PSA `baseline` checks on Pods in `media`, skipping qBittorrent's pod by its labels (read them from the live pod). Kyverno 1.19: prefer a CEL `ValidatingPolicy`; fall back to a `ClusterPolicy` `validate.podSecurity: {level: baseline}` rule with `exclude` if CEL can't express it. No `PolicyException` (`--enablePolicyException=false`). The `media` namespace label stays `privileged`.

- [ ] **Step 1:** For each namespace, `kubectl label --dry-run=server --overwrite ns <ns> pod-security.kubernetes.io/enforce=<level>` and read the warnings. Target levels: `restricted` for `cloudflareddns`, `whosaid`, `authelia`, `lldap`, `panio`, `suivi-saeg`, `jupyter`; `baseline` for `blocky`, `traefik`; `media` per the decision. A namespace with violations: add the missing securityContext fields (`seccompProfile: RuntimeDefault`, `capabilities.drop: [ALL]`, `runAsNonRoot: true`, `allowPrivilegeEscalation: false`) in its chart values in the same PR, or settle one level lower.
- [ ] **Step 2:** Set `enforce=<level>`, `warn` and `audit` one level stricter (`restricted` when enforcing `baseline`).
- [ ] **Step 3:** kubeconform; commit; PR (one per few namespaces).
- [ ] **Step 4 (after merge):** `kubectl -n <ns> rollout restart` each Deployment/StatefulSet; all pods Running; no new warnings in `kubectl get events -n <ns>`.
- [ ] **Step 5 (`media`):** first ship the policy in Audit (`validationActions: [Audit]` / `validationFailureAction: Audit`); read its policy reports for `media` (only qBittorrent may fail, and it's skipped); then switch to Deny in a second PR. Check: a test pod in `media` with `privileged: true` (server dry-run, `kubectl run --dry-run=server`) is refused; restarting the qBittorrent deployment still works (Emby idle check first: `mise run emby:idle`).

### Task 8: Emby and Traefik hardening (C2, C3)

**Files:**
- Create: `clusters/main/kubernetes/network/traefik/app/` rate-limit middleware (a `Middleware` named `ratelimit` in `traefik`: `rateLimit: {average: 20, burst: 50}`), via the Traefik chart's `middlewares` values like `auth`/`local`
- Modify: `clusters/main/kubernetes/auth/authelia/app/helm-release.yaml` ingress middlewares (add `traefik-ratelimit@kubernetescrd`), and the public apps' ingresses (`emby`, `panio`, `suivi-saeg` if they stay public)

- [ ] **Step 0 (C2b, first: Step 1 relies on Emby seeing real client IPs):** already configured (design C2b, 2026-10-19); only run the check below. Originally: Jellyseerr "Enable Proxy Support"; Emby's trusted/known proxy setting for 172.16.0.0/16 if it has one (in the app's settings or its config values; ask the user before changing a setting through the UI). Check: a LAN client shows as 192.168.1.x and a phone on mobile data as a public IP (not printed).
- [ ] **Step 1 (outside git, user):** in Emby, admin accounts can't sign in remotely (per-user "Allow remote connections" off for admins), strong passwords; record what was set in the design.
- [ ] **Step 2:** middleware + annotations; render; kubeconform; PR.
- [ ] **Step 3 (after merge):** each URL loads; a burst of 100 requests in a second to `auth.` gets HTTP 429 after the burst (`for i in $(seq 100); do curl -s -o /dev/null -w '%{http_code}\n' --resolve ...; done | sort | uniq -c`, host masked).

### Task 9 (optional): phones on the guest Wi-Fi (D1)

Outside git. The user moves phones to the Neufbox guest Wi-Fi if wanted; check from a phone: public app URLs load; the laptop and projector stay on the main network. Record the choice in the design.

### Deferred (no task until the user asks)

A5 egress allow-lists, E2 phone notifications (separate design), E3 longer drop history, CrowdSec.
