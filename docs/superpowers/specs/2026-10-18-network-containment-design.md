# Containment: a compromised app stays where it is

Date: 2026-10-18. Status: draft for review.

## Intent

If one app in the cluster is compromised, the attacker shouldn't be able to use it as a stepping stone. What the user most wants protected, in order:

1. **TrueNAS**: the zvols (including the Talos VM's own disk), the NFS shares (media, app configs), its admin UI.
2. **The other machines at home**: laptop, phones, the projector.

Network policies are the main tool, but this design covers every layer that limits how far a compromise can spread: network, what a pod may do on the node, what it can do with the Kubernetes API, what's exposed to the internet, the one entry point that bypasses the cluster (TrueNAS SSH), and how the user finds out something is wrong.

## Threat model

- **In scope:** one app pod is compromised: a bug in an internet-facing app (Emby, panio, suivi-saeg, the arr stack and Jupyter behind Authelia), a malicious image update, or code run through Jupyter. Also: the internet-facing entry points themselves (Traefik, TrueNAS SSH).
- **Out of scope:** a compromised node or TrueNAS host, a stolen admin account, attacks on the router itself.

## Goals

- Pods can't reach TrueNAS (done) or anything else on the home LAN.
- App pods can't use the Kubernetes API or the node's own services unless they need to, and don't carry API credentials they don't use.
- App namespaces only accept traffic from Traefik, their own namespace, and a short explicit list.
- App pods run with the least privilege their namespace allows (Pod Security levels), so a compromised process can't easily escape to the node.
- Internet exposure is deliberate: each internet-facing app is either behind Authelia or has a written reason not to be.
- Every change is visible before and after it applies (Hubble, drop logs), and any step can be undone with one revert PR.

## Non-goals

- Network policies inside system namespaces (kube-system, flux-system, cert-manager, kyverno, cloudnative-pg, metallb, openebs, volsync, gpu-operator, system-upgrade, snapshot-controller, prometheus-operator, kubernetes-reflector). They serve admission webhooks and the control plane; a mistake there can stop the whole cluster.
- Restricting which internet sites apps can reach (FQDN / L7 policies).
- Covering host-network pods (Cilium agent, MetalLB speaker, Emby's discovery relay): that needs Cilium's host firewall, a separate and riskier change.
- Supply-chain controls (image signing, Kyverno verifyImages). Renovate + PR review + digest pinning stay the defence.

## Anti-goals

- A setup where every Renovate bump or new app breaks in a way that's hard to debug. Rules should be few, generic and readable ("deny LAN", "allow from Traefik"), not a per-app port matrix.
- Losing existing protection during a rollout (Authelia/LLDAP policies, TrueNAS deny). This rules out Cilium's audit mode, global or per pod: on an audited pod *every* policy only logs, including the TrueNAS deny (found in review, 2026-10-19).
- Locking the user out of their own admin paths (laptop → TrueNAS UI, kubectl, talosctl).

## Current state (2026-10-18)

| Already in place | Where |
|---|---|
| Pods can't reach TrueNAS; Traefik only on 444 | `core/network-policies/truenas.yaml` (#1046) |
| Default-deny ingress + allows in `authelia`, `lldap`; PSA `baseline` there | app folders (#955) |
| qBittorrent reaches the internet only through the VPN | gluetun firewall, `media/qbittorrent` |
| Authelia: default deny, admins 2FA from outside, `requests.` any LLDAP user, `support` group for Radarr/Sonarr | `auth/authelia` (#948, #1007, #1008) |
| NFS exports limited to the node IP; TrueNAS SSH key-only, modern ciphers | TrueNAS (2026-10-08 review) |
| Hubble + Relay + UI (`hubble.<domain>`, admins), dropped flows exported to a file (~8–10 h) | cilium HelmRelease (#1044, #1047, #1049) |
| `mise run net:drops [ns] [--follow]` | `scripts/net-drops.sh` (#1047, #1050) |

### Measured network traffic (90 s Hubble sample of new connections)

- To the LAN: only `traefik → 192.168.1.73:444` (TrueNAS UI).
- To the internet: `media` (HTTPS, WireGuard 51820/udp), `flux-system` (HTTPS), `kube-system` (CoreDNS → 1.1.1.1 / 8.8.8.8 on 53/udp).
- Between namespaces: `authelia → lldap:3890`, `traefik → authelia:9091`, every namespace → `kube-system` DNS.
- From `world`: only `→ traefik:443` (Blocky's LAN DNS wasn't hit during the sample, but is used).
- `host → pods`: kubelet health probes (always allowed).

### Workloads (app namespaces)

| Finding | Pods |
|---|---|
| PSA `privileged` on an app namespace | `media` (only because of `qbittorrent-gluetun`: root, `NET_ADMIN`, `NET_RAW`, `MKNOD`) |
| No PSA label (cluster default applies: Talos enforces `baseline`, warns `restricted`) | `blocky`, `cloudflareddns`, `jupyter`, `panio`, `suivi-saeg`, `traefik`, `whosaid` |
| Run-as user not set (may run as root) | `panio`, `suivi-saeg`, `media/decluttarr` |
| API token mounted but unused (default SA, no RBAC) | `media/decluttarr`, `media/pinchflat`, `panio`, `suivi-saeg` |
| Need the Kubernetes API | `blocky` (k8s_gateway, ClusterRole), `traefik` (ClusterRole), CNPG instance pods (`cnpg.io/cluster`) |
| Root with extra capabilities | `blocky-k8sgateway` (uid 0, NET_BIND_SERVICE + file caps), `qbittorrent-gluetun` |

### Internet exposure

| Entry | Login |
|---|---|
| Router TCP 443/80 → Traefik (.110) | per app |
| Router TCP 2222 → TrueNAS SSH :22 | key-only (rbossut, 6 keys, FULL_ADMIN) |
| `emby.` | Emby's own login (clients can't do Authelia) |
| `panio.`, `suivi-saeg.` | none / own login (no Authelia middleware) |
| `auth.` | Authelia itself |
| everything else | Authelia (`lldap.`, `torrent.`, `traefik.` LAN-only via the `local` middleware) |

`whosaid` has no ingress (internal only).

## Constraints

- Single node (Talos VM), Cilium 1.20.3, tunnel mode (VXLAN), kube-proxy replacement, IPv4 only. Node 192.168.1.101, VIP .100, MetalLB .110 (Traefik) and .111 (Blocky DNS), TrueNAS .73. LAN 192.168.1.0/24 behind an SFR Neufbox NB6VAC (firmware R4.0.47h5): no VLANs; guest Wi-Fi isolated from the main LAN.
- CoreDNS forwards to 1.1.1.1 / 8.8.8.8 (`forwardKubeDNSToHost: false`), not to the router. Blocky uses the chart's public upstreams; its only conditional forward is to its own k8s_gateway sidecar on localhost:5353; no client lookup against the router. A private-range deny touches neither.
- NFS volumes are mounted by the node: pod egress policies never affect volumes.
- Cilium semantics relied on (the last three are verified in the plan before relying on them):
  - a deny always wins over an allow;
  - `enableDefaultDeny: {egress: false, ingress: false}` makes a policy deny-only;
  - `toCIDR`/`toCIDRSet` only match `world`: they never select `host`, `kube-apiserver` or pods;
  - Service / LoadBalancer IPs are translated to the backend before policy;
  - local-node traffic (kubelet probes) reaches pods regardless of policy.
- Hubble's in-memory buffer holds seconds; history comes from the export file (DROPPED only).
- Any Cilium values change restarts the agent (network blip) and can crash it without `helm template` noticing (#1047): check the rendered agent config for values the agent parses itself, and watch `rollout status ds/cilium` right after deploy.
- Flux substitution provides `${NFS_HOST}`, `${DOMAIN_0}`, …; the domain is private and never written in files.
- `main` is protected: every change is a PR; merging deploys.

## Design

Ranked by risk reduction per effort. Each track is independent unless noted.

### Track A: network

All new cluster-wide and app-namespace policies live in `core/network-policies/`, one file per concern, so the whole set reads in one folder. Existing `authelia`/`lldap` policies stay where they are.

"App namespaces" = `media`, `panio`, `suivi-saeg`, `whosaid`, `jupyter`, `blocky`, `cloudflareddns`, `authelia`, `lldap`.

- **A1 pods → TrueNAS** (done, #1046).
- **A2 pods → private ranges.** Cluster-wide deny-only policy: every pod except Traefik loses egress to `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`. Traefik's allow-list changes from `0.0.0.0/0 except TrueNAS` to `0.0.0.0/0 except` those three ranges, plus TrueNAS:444. Pod/service CIDRs are cluster identities and unaffected. No exception is needed today (measured).
- **A3 app pods → API server and node.** Cluster-wide deny-only policy for pods in app namespaces, excluding CNPG instance pods (`cnpg.io/cluster` exists) and `blocky` (k8s_gateway): deny egress to entities `kube-apiserver` and `host`. Blocks the API, kubelet 10250, Talos API 50000/50001. Health probes (host → pod) are unaffected.
- **A4 ingress into app namespaces.** One CiliumNetworkPolicy per app namespace without one today (`media`, `panio`, `suivi-saeg`, `whosaid`, `jupyter`, `blocky`, `cloudflareddns`): allow from the `traefik` namespace, from the same namespace, and:
  - `blocky`: from `world` and `host` on 53/udp+tcp (LAN clients through MetalLB);
  - `whosaid`: from the same namespace only (no ingress);
  - `cloudflareddns`: nothing (it only calls out).
  Everything else is dropped. The projector reaches Emby through Traefik or the host-network discovery relay (local traffic, allowed).
- **A5 (deferred) egress allow-lists for app namespaces**: same namespace, kube-dns, world minus private ranges, explicit cross-namespace. Done after weeks of quiet drop logs, if wanted.

### Track B: workloads

- **B1 drop unused API tokens.** `automountServiceAccountToken: false` for `media/decluttarr`, `media/pinchflat`, `panio`, `suivi-saeg` (their charts/values). Cheap, no behaviour change.
- **B2 run as non-root.** Set `runAsNonRoot` / `runAsUser: 568` (or the image's non-root user) for `panio`, `suivi-saeg`, `media/decluttarr` after checking each image works that way (panio and suivi-saeg are self-built: fix in the image/chart if needed).
- **B3 Pod Security levels.** Label each app namespace with the strictest level its pods pass, `enforce` plus `warn`/`audit` at the next level up:
  - `restricted` candidates: `cloudflareddns`, `whosaid`, `authelia`, `lldap`, `panio`, `suivi-saeg`, `jupyter` (after B2 / small securityContext additions: `seccompProfile: RuntimeDefault`, `capabilities.drop: [ALL]`, `runAsNonRoot`);
  - `baseline`: `blocky` (root + allowed caps in k8sgateway), `traefik`;
  - `media`: blocked at `privileged` by gluetun. **User's choice (2026-10-19): a Kyverno policy with an exception for qBittorrent** (moving it to its own namespace means more config changes). The namespace label stays `privileged` (built-in PSA can't exempt one pod); a Kyverno policy enforces the `baseline` checks on every `media` pod except qBittorrent's, matched by its labels. Kyverno is 1.19 with `--enablePolicyException=false` and v1 `ClusterPolicy` deprecated: write the exclusion into the policy itself (a CEL `ValidatingPolicy` match condition, or a `ClusterPolicy` `podSecurity` rule with `exclude` if CEL can't express the baseline checks), not a `PolicyException`. Audit mode first is fine here: unlike Cilium's, it only affects this policy. Other options considered: a separate `torrent` namespace; leaving `media` privileged.
  Check with `kubectl label --dry-run=server` (reports violations) before each label PR.

### Track C: internet exposure

- **C1 panio and suivi-saeg**: today public without Authelia.
  - `panio`: **public without auth, the user's choice (2026-10-19).** No change; the ingress isolation (A4) and the B2/B3 hardening still apply.
  - `suivi-saeg`: **decision for the user**: behind Authelia (who are the users?), LAN-only (`local` middleware), or public with a written reason.
- **C2 Emby**: stays public (TV/phone clients can't do Authelia). Mitigations without breaking clients: keep Emby updated (Renovate), no admin account reachable from outside if Emby allows (`Allow remote connections` per user / admin login only from LAN), strong passwords.
- **C2b real client IPs in Emby and Jellyseerr (user's choice, 2026-10-19).** Behind Traefik both apps see every client as Traefik's pod IP (172.16.x). That's a private address, so Emby takes internet clients for LAN ones: per-user "allow remote connections", LAN-only admin login and its logs all rely on the client IP. Traefik already sets `X-Forwarded-For` from the real source (`externalTrafficPolicy: Local`, client-supplied headers not trusted). Make each app trust that header from Traefik only:
  - Jellyseerr: "Enable Proxy Support" (`trustProxy` in its settings).
  - Emby: its network settings' trusted/known proxy option if this version has one, scoped to the pod CIDR 172.16.0.0/16 (or Traefik's address); if it has none, record that and keep C2's mitigations in mind as weaker.
  - Check after: the app's log or session view shows a LAN client as 192.168.1.x and a phone on mobile data as a public IP (not printed); a request with a forged `X-Forwarded-For` from outside isn't trusted.
- **C3 Traefik hardening**: a rate-limit middleware on the Authelia login and on public apps; optional geo/IP reputation filtering (e.g. a CrowdSec bouncer) as a later option.
- **C4 TrueNAS SSH (2222 → :22)**: well configured; residual risk is rbossut's 6 FULL_ADMIN keys and direct exposure. rbossut connects from anywhere so far; **the user is asking him** (2026-10-19). Options: keep as is (documented risk), limit the source IPs (the Neufbox port-forward UI may not support source filtering), or move access to a VPN (WireGuard/Tailscale) and remove the forward.

### Track D: home network

- **D1 phones on the Neufbox guest Wi-Fi** (optional): isolates them from a compromised server; they lose local access (Emby, Blocky DNS, TrueNAS UI) and use public URLs only. Not the laptop (admin access) or the projector (local Emby).

### Track E: finding out

- **E1 rollout tooling (evidence first)**: a second Hubble flow log (`hubble.export.dynamic`, file `inbound.log`, reloaded without an agent restart) of new connections into app namespaces, excluding the node and LAN DNS to Blocky; `mise run net:inbound [namespace]` summarises it per source → workload:port with counts. Drops stay in the static export (`net:drops`).
- **E2 (separate project) phone push notifications** for drops and other alerts; then alert on new drops after each rollout.
- **E3 (if needed) longer drop history**: compress / field-mask the export file.

### Rollout procedure (every policy step)

Evidence first, then enforce: no step weakens the policies already live.

1. Before writing an allow-list policy (A4, A5): a few days of `mise run net:inbound <ns>`, covering normal use of each app (a film on the projector, a request, a download). Every source in it gets an allow rule or a written reason to stay blocked.
2. Merge; watch `rollout status` if Cilium values changed.
3. Right after: `mise run net:drops <ns>` and the step's acceptance test. A missed flow shows as a drop: add the rule in git, or revert the PR (rollback).

Deny-only steps (A2, A3) use a short Hubble sample instead of days of `net:inbound`, and ship directly when it is clean.

### Acceptance tests

A throwaway probe pod (busybox, temporary namespace, deleted at the end, prints only connected/blocked):

- **A2:** router 192.168.1.1:80 and another LAN host blocked; 1.1.1.1:443 reachable; `kubernetes.default` reachable from the probe namespace; TrueNAS UI through Traefik still works (user loads it); Blocky still answers LAN DNS.
- **A3:** probe pod in an app namespace: `kubernetes.default:443`, 192.168.1.101:10250 and :50000 blocked; CNPG clusters stay healthy; Blocky still resolves cluster names (k8s_gateway).
- **A4:** probe in `jupyter` → `media` radarr:7878 blocked; every app URL still loads; Blocky DNS from the LAN still works.
- **B3:** `kubectl label --dry-run=server` shows no violation before the label PR; pods still start after a rollout restart.

## Order

1. E1 (tooling: the `inbound` flow log + `net:inbound`), so evidence for A4 builds up early.
2. A2 (private ranges), A3 (API/node), B1 (tokens): cheap, measured, high value.
3. C1 decision + change; C4 decision.
4. A4 (ingress per namespace), one namespace at a time, internet-facing first: `media`, `panio`, `suivi-saeg`, `jupyter`, then `blocky`, `whosaid`, `cloudflareddns`.
5. B2, B3 (non-root, PSA labels), `media` per the user's decision.
6. C2, C3, D1, A5, E2, E3 as time allows.

## Questions only the user can answer

1. ~~panio~~: public, no auth (answered 2026-10-19). **suivi-saeg**: who uses it? Behind Authelia, LAN-only, or public on purpose?
2. ~~media PSA~~: Kyverno policy with an exception for qBittorrent (answered 2026-10-19).
3. **TrueNAS SSH**: from anywhere so far; the user is asking rbossut (keep / source-limit / VPN).
5. ~~Emby/Jellyseerr trusted proxy~~: yes, added as C2b (answered 2026-10-19).
4. ~~Audit window for A2, A3~~: moot, audit mode dropped (it lifts every policy on the pod).

## Calls made by Claude (to check)

- The private-range deny covers all three RFC 1918 ranges, not only 192.168.0.0/16.
- `blocky` is excluded from A3 (k8s_gateway reads Services/Ingresses); `traefik` isn't an "app namespace" for A3 (needs the API).
- `kubernetes-reflector` is treated as a system namespace.
- App-namespace policies are centralised in `core/network-policies/`.
- A5, C3's CrowdSec, D1, E2, E3 are optional / later.
