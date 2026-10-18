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
- **Pods restarted during an audit window** silently enforce: `net:audit status <ns>` must show it, so the operator can re-run `on` — test in Task 1.
- **`${NFS_HOST}` left unsubstituted** in a new policy would match nothing: check the live object's CIDR after each deploy — step in Task 2.

---

### Task 1: Rollout tooling: audit verdicts in the export, `net:audit`, `net:drops --audit` (E1)

**Files:**
- Modify: `clusters/main/kubernetes/kube-system/cilium/app/helm-release.yaml` (`hubble.export.static.allowList`)
- Create: `scripts/net-audit.sh`
- Modify: `scripts/net-drops.sh` (add `--audit`)
- Modify: `mise.toml` (task `net:audit`)

**Interfaces:**
- Produces: `mise run net:audit on|off|status <namespace>`; `mise run net:drops --audit [namespace]` (filters `--verdict AUDIT` instead of `DROPPED`, in both file and `--follow` modes).

- [ ] **Step 1:** Change the export allow-list to `- '{"verdict":["DROPPED","AUDIT"]}'` (one JSON string). Render with `helm template` (as in #1044) and check `hubble-export-allowlist: "{\"verdict\":[\"DROPPED\",\"AUDIT\"]}"` in the output.
- [ ] **Step 2:** `scripts/net-audit.sh <on|off|status> <namespace>`: inside the agent (`kubectl -n kube-system exec ds/cilium -c cilium-agent --`), list endpoints with `cilium-dbg endpoint list -o json`, keep those whose labels contain `k8s:io.kubernetes.pod.namespace=<namespace>`, then `cilium-dbg endpoint config <id> PolicyAuditMode=Enabled|Disabled`. `status` prints one line per endpoint: pod name and `PolicyAuditMode` (from `cilium-dbg endpoint get <id>`), and a final count `enabled N / total M`. Exit non-zero on an unknown verb or a namespace with no endpoints.
- [ ] **Step 3:** `net-drops.sh`: `--audit` switches the verdict filter to `AUDIT`; keep the ICMPv6 exclusion in `--follow`.
- [ ] **Step 4:** `mise.toml`: `[tasks."net:audit"]`, `run = "bash scripts/net-audit.sh"`, description "Per-pod Cilium policy audit mode for a namespace (on|off|status): policies log AUDIT instead of dropping. Lost when a pod restarts".
- [ ] **Step 5:** Commit, PR (warn: agent restart). After merge: `rollout status ds/cilium`; `kubectl -n kube-system get cm cilium-config -o jsonpath='{.data.hubble-export-allowlist}'` shows both verdicts.
- [ ] **Step 6 (live, with the user's OK):** `mise run net:audit on whosaid` then `status whosaid` → `enabled N / total N`; delete one whosaid pod, wait for it, `status` → `enabled N-1 / total N` (the Review Focus case); `off whosaid` → `enabled 0`.

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
- [ ] **Step 4:** kubeconform + server dry-run; commit; PR. The user decides: audit window first (Task 1 tooling on the namespaces) or direct.
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

Depends on the user's answers (design "Questions" 1 and 3).

**Files (C1, if Authelia is chosen):**
- Modify: `clusters/main/kubernetes/apps/panio/app/helm-release.yaml`, `clusters/main/kubernetes/apps/suivi-saeg/app/helm-release.yaml` (ingress annotation `traefik.ingress.kubernetes.io/router.middlewares: traefik-auth@kubernetescrd`, or `traefik-local@kubernetescrd` for LAN-only)
- Modify: `clusters/main/kubernetes/auth/authelia/app/helm-release.yaml` (a rule for `panio.${DOMAIN_0}` / `suivi-saeg.${DOMAIN_0}` with the group the user names; admins are covered by the wildcard rules)

- [ ] **Step 1:** Apply the user's choice per app; render; kubeconform; PR.
- [ ] **Step 2 (after merge):** unauthenticated request through Traefik (`curl --resolve <host>:443:192.168.1.110`, host masked in output) returns 302 to Authelia (or 403 from outside for `local`); the user logs in and the app works.
- [ ] **Step 3 (C4, outside git):** per the user's choice: nothing (record the accepted risk in the design), router source restriction (check the Neufbox UI supports it), or a VPN plan (separate design). No cluster change.

### Task 6: Ingress isolation per app namespace (A4)

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
- [ ] **Step 2 (live, user's OK):** `mise run net:audit on <namespace>`.
- [ ] **Step 3:** kubeconform + server dry-run; commit; PR; merge.
- [ ] **Step 4:** a few days of `mise run net:drops --audit <namespace>`: expected empty. Each AUDIT line: add a rule in git with a comment, or explain why it must stay blocked.
- [ ] **Step 5 (live, user's OK):** `mise run net:audit off <namespace>`; acceptance: probe pod in a temporary namespace → a pod of `<namespace>` on its service port: blocked; every app URL of the namespace loads; `media`: the user checks the projector discovers and plays Emby; `blocky`: `dig @192.168.1.111` from the laptop answers.

### Task 7: Pod Security levels (B3)

Depends on the user's answer to design question 2 for `media`.

**Files:**
- Modify: the `namespace.yaml` of each app namespace (labels `pod-security.kubernetes.io/enforce`, `.../warn`, `.../audit`)
- (Option 1 for `media` only, its own PR first, after a short sub-plan agreed with the user) move qBittorrent into a new `torrent` namespace kept `enforce: privileged`: the HelmRelease folder stays `media/qbittorrent` but targets `namespace: torrent`; its config volume is migrated per the sub-plan; Radarr/Sonarr/Radarr 4K download clients and decluttarr point to `qbittorrent.torrent.svc.cluster.local:10095`; the `torrent` namespace gets its own `ingress-torrent.yaml` (from Traefik and from `media` on 10095) in Task 6's style, and `deny-apps-to-api-and-node` (Task 3) gains `torrent` in its `In` list.

- [ ] **Step 1:** For each namespace, `kubectl label --dry-run=server --overwrite ns <ns> pod-security.kubernetes.io/enforce=<level>` and read the warnings. Target levels: `restricted` for `cloudflareddns`, `whosaid`, `authelia`, `lldap`, `panio`, `suivi-saeg`, `jupyter`; `baseline` for `blocky`, `traefik`; `media` per the decision. A namespace with violations: add the missing securityContext fields (`seccompProfile: RuntimeDefault`, `capabilities.drop: [ALL]`, `runAsNonRoot: true`, `allowPrivilegeEscalation: false`) in its chart values in the same PR, or settle one level lower.
- [ ] **Step 2:** Set `enforce=<level>`, `warn` and `audit` one level stricter (`restricted` when enforcing `baseline`).
- [ ] **Step 3:** kubeconform; commit; PR (one per few namespaces).
- [ ] **Step 4 (after merge):** `kubectl -n <ns> rollout restart` each Deployment/StatefulSet; all pods Running; no new warnings in `kubectl get events -n <ns>`.

### Task 8: Emby and Traefik hardening (C2, C3)

**Files:**
- Create: `clusters/main/kubernetes/network/traefik/app/` rate-limit middleware (a `Middleware` named `ratelimit` in `traefik`: `rateLimit: {average: 20, burst: 50}`), via the Traefik chart's `middlewares` values like `auth`/`local`
- Modify: `clusters/main/kubernetes/auth/authelia/app/helm-release.yaml` ingress middlewares (add `traefik-ratelimit@kubernetescrd`), and the public apps' ingresses (`emby`, `panio`, `suivi-saeg` if they stay public)

- [ ] **Step 1 (outside git, user):** in Emby, admin accounts can't sign in remotely (per-user "Allow remote connections" off for admins), strong passwords; record what was set in the design.
- [ ] **Step 2:** middleware + annotations; render; kubeconform; PR.
- [ ] **Step 3 (after merge):** each URL loads; a burst of 100 requests in a second to `auth.` gets HTTP 429 after the burst (`for i in $(seq 100); do curl -s -o /dev/null -w '%{http_code}\n' --resolve ...; done | sort | uniq -c`, host masked).

### Task 9 (optional): phones on the guest Wi-Fi (D1)

Outside git. The user moves phones to the Neufbox guest Wi-Fi if wanted; check from a phone: public app URLs load; the laptop and projector stay on the main network. Record the choice in the design.

### Deferred (no task until the user asks)

A5 egress allow-lists, E2 phone notifications (separate design), E3 longer drop history, CrowdSec.
