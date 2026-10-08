# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this repo is

A GitOps homelab: a single-node [Talos](https://www.talos.dev/) Kubernetes cluster (`k8s-control-1`, a VM on TrueNAS with an NVIDIA GPU), managed with [FluxCD](https://fluxcd.io/) bootstrapped originally with TrueCharts' clustertool, which has since been replaced by mise tasks. Everything merged to `main` gets reconciled into the live cluster, so **every change to `clusters/` or `repositories/` is a production deploy**.

The two root Kustomizations, `flux-entry` (`clusters/main/kubernetes/flux-entry.yaml`) and `flux-entry-repos` (`repositories/flux-entry.yaml`), are **not applied by Flux itself**: `mise run cluster:bootstrap` creates them. If you change either file, also run `kubectl apply -f clusters/main/kubernetes/flux-entry.yaml -f repositories/flux-entry.yaml` after merging. Otherwise the live objects keep the old spec, and Flux can get stuck.

The Flux `GitRepository` (`repositories/git/this-repo.yaml`) only watches `/clusters` and `/repositories`. Changes anywhere else never reach the cluster.

## Layout

```
clusters/main/
  clusterenv.yaml            # SOPS-encrypted cluster variables (IPs, domains, tokens…)
  talos/
    talconfig.yaml           # talhelper config for the node (Talos + k8s versions, extensions)
    patches/                 # Talos machine-config patches
    patches/                 # Talos machine-config patches (strategic merge)
    backup/                  # SOPS-encrypted copy of the live machine config (mise run talos:backup)
    generated/               # talhelper output; only talsecret.yaml (encrypted) is tracked
  kubernetes/
    flux-entry.yaml          # root Flux Kustomization -> ./clusters/main/kubernetes
    kustomization.yaml       # lists the top-level groups below
    flux-system/             # Flux install + encrypted bootstrap secrets (*.secret.yaml)
    kube-system/             # cilium, metrics-server, descheduler, nfd, kubelet-csr-approver
    system/                  # cert-manager, longhorn, openebs, metallb, kyverno, volsync, cnpg, …
    core/                    # cluster-level config: issuers, metallb pools, blocky, kyverno policies, upgrade plans
    network/                 # traefik, cloudflare DDNS
    auth/                    # lldap + authelia
    media/                   # *arr stack, qbittorrent, emby
    apps/                    # personal / self-built apps (panio, veggie-planner, suivi-saeg, whosaid, …)
repositories/
  flux-entry.yaml            # Flux Kustomization -> ./repositories
  helm/                      # HelmRepository sources (truecharts, bjw-s, fait-maison, …)
  git/, oci/                 # other Flux sources
scripts/kubeconform.sh       # validation used by CI
.sops.yaml                   # encryption rules
```

`age.agekey` is a gitignored local file (backed up off-machine by the user). Never commit, print, or copy the contents of `age.agekey`.

## Adding or changing an app

Every app follows the same pattern:

```
<group>/<app>/
  ks.yaml                    # Flux Kustomization, namespace flux-system
  app/
    helm-release.yaml        # HelmRelease
    namespace.yaml
    kustomization.yaml       # lists the two files above (optional but preferred)
```

To add one:

1. Create the folder under the right group (`apps/` for personal apps, `media/` for the media stack, etc.).
2. `ks.yaml`: `kustomize.toolkit.fluxcd.io/v1` Kustomization named after the app, `namespace: flux-system`, `path: clusters/main/kubernetes/<group>/<app>/app`, `prune: true`, `sourceRef: {kind: GitRepository, name: cluster}`. Copy a sibling such as `apps/suivi-saeg/ks.yaml`.
3. Register it in the group's `kustomization.yaml` (`- <app>/ks.yaml`). If you forget this step, Flux never deploys the app.
4. If the chart comes from a new Helm repo, add a `HelmRepository` in `repositories/helm/` and list it in `repositories/helm/kustomization.yaml`.

HelmRelease conventions:

- `apiVersion: helm.toolkit.fluxcd.io/v2`, `chart.spec.sourceRef` points to a HelmRepository in `flux-system`.
- Keep the `# renovate: registryUrl=...` comment above `chart:` so Renovate can bump the version.
- Most charts are **TrueCharts** (`truecharts` repo). Use TrueCharts' values schema: `ingress.main.integrations.traefik` / `certManager`, plus `persistence.<name>` with `type: nfs`, `server: ${NFS_HOST}`, `path: /mnt/tank/...`. See `media/sonarr/app/helm-release.yaml`.
- Self-built apps come from the `fait-maison` OCI repo (`oci://ghcr.io/fait-maison/helm`) and use a plain `ingress:` block with cert-manager and Traefik annotations. See `apps/panio/app/helm-release.yaml`.
- Ingress hosts are `<name>.${DOMAIN_0}`, and the TLS issuer is `domain-0-le-prod`.
- To put an app behind Authelia SSO, add the Traefik middleware `auth` (namespace `traefik`). Middlewares `local` and `traefik-dash` also exist.

## Variables and secrets

- `${VAR}` placeholders are substituted by Flux `postBuild.substituteFrom` from the `cluster-config` ConfigMap, which is generated from `clusters/main/clusterenv.yaml`. Available keys include `DOMAIN_0`, `NFS_HOST`, `VIP`, `TRAEFIK_IP`, `BLOCKY_IP`, `PODNET`, `SVCNET`, plus a variety of credentials. To use a new value, add it to `clusterenv.yaml` (that requires decrypting it, so ask the user).
- `cluster-config` is a **Secret** (it holds passwords and tokens), referenced with `substituteFrom: kind: Secret` in both `flux-entry.yaml` files.
- Substitution applies to every Flux Kustomization. To opt one out, label it `substitution.flux.home.arpa/disabled: "true"`. If a manifest needs a literal `${...}`, escape it as `$${...}`.
- SOPS (age) encrypts:
  - any `*values.yaml` under `clusters/**/kubernetes/`
  - any `*.secret.yaml` under `clusters/**/kubernetes/`
  - `clusterenv.yaml` and `talsecret.yaml`
  
  Only keys that match the `encrypted_regex` in `.sops.yaml` get encrypted (`pass`, `secret`, `key`, `token`, `email`, `data`, `stringData`, …).
- **Never commit plaintext secrets** and never hand-edit `ENC[...]` blobs. Put secrets in a `*.secret.yaml` file and encrypt it with `sops -e -i <file>`, or reference a `clusterenv.yaml` variable. If you can't encrypt, stop and ask.
- `mise run sops:check` (also the pre-commit hook) fails if a file matching `.sops.yaml` is not encrypted.

## Single-node gotchas

The cluster has exactly one node. Before bumping a chart, check its new defaults with `helm template`. Charts that ship HA defaults can't become ready here, and their upgrades time out and roll back:

- More than one replica of a pod that uses `hostNetwork`/`hostPort`. This is why the cilium-operator is pinned to `replicas: 1`.
- Pods that must run on separate nodes (`podAntiAffinity` on `kubernetes.io/hostname`). For example, OpenEBS ≥ 4.3 ships Loki, which is disabled here for that reason.

If a HelmRelease shows `Stalled` / `RetriesExceeded`, Flux has stopped retrying it. Once the fix is merged, it needs `flux reconcile hr -n <ns> <name> --force`.

## Talos / node

- Node config lives in `clusters/main/talos/talconfig.yaml` and `patches/` (strategic-merge patches; Talos 1.13 configs are multi-document, so JSON6902 `op/path` patches no longer work). Variables come from `clusters/main/clusterenv.yaml`.
- `mise run talos:genconfig` generates the machine config + `talosconfig` with talhelper (secrets decrypted in memory). `mise run talos:diff` dry-runs it against the live node; **always diff before `mise run talos:apply`**, which may reboot the node. The network is kept in the legacy v1alpha1 format via `patches/network.yaml`. Moving to the Talos 1.13+ multi-document network config (`networkInterfaces` in talconfig) is a deliberate change: it needs a reboot, so do it with console access.
- `mise run talos:backup` refreshes the encrypted live-config copy in `talos/backup/`. Run it after any change to the node.
- Talos and Kubernetes versions are pinned in `talconfig.yaml` and `flux-system/flux/upgradesettings.yaml`. Upgrades run through system-upgrade-controller plans in `core/system-upgrade-controller-plans/`. Major bumps are deliberately not automerged.
- The admin kubeconfig client certificate expires after a year: `mise run talos:kubeconfig`.

## Disaster recovery

Rebuilding the node from scratch (needs `age.agekey` from its backup):

1. Boot the Talos ISO for the version in `talconfig.yaml` (factory.talos.dev, same schematic).
2. `mise run talos:genconfig`, then `talosctl apply-config --insecure -n <node-ip> --file clusters/main/talos/generated/main-k8s-control-1.yaml`.
3. `talosctl --talosconfig clusters/main/talos/generated/talosconfig bootstrap`, then `mise run talos:kubeconfig`.
4. `mise run cluster:bootstrap` installs Cilium and Flux and applies the secrets and the root Kustomizations. Flux rebuilds everything else. `--dry-run` tests it against a running cluster without changing anything.

## Validation

CLI tools are pinned in `mise.toml`. Run `mise install` once; inside the repo, mise puts them on PATH. `talosctl`, `kubectl` and `flux` match the cluster versions. Bump them together with the cluster, never ahead of it (see the GPU note under Operations notes). Tasks: `mise run validate` (same checks as CI) and `mise run tf <args>` (Terraform, see below).

Before proposing a change to `clusters/main/kubernetes/`, run the same checks as CI:

```bash
bash ./scripts/kubeconform.sh ./clusters/main/kubernetes   # needs kustomize + kubeconform
kustomize build clusters/main/kubernetes/<group>/<app>/app  # quick check for one app
```

A local git pre-commit hook (installed with `mise run hooks:install`) runs `mise run sops:check --staged`, which refuses to commit unencrypted secrets. Don't bypass it with `--no-verify`. If it fails, fix the cause.

CI (`.github/workflows/Tests.yaml`) runs kubeconform plus a `flux-local` diff on PRs that touch `clusters/main/kubernetes/**`. The automerge jobs (`pascalgn/automerge-action`) only squash-merge PRs that pass **and** carry the `automerge` label. Never add that label without the user's approval, because merging to `main` deploys.

The `.devcontainer` (TrueCharts devcontainer) ships with flux, kubectl, talosctl, sops and the other needed tools.

## DNS (Terraform)

Public DNS for `DOMAIN_0` lives in Cloudflare and is managed by Terraform in `terraform/cloudflare/`. A wildcard `*.<domain>` CNAME points every app name at the apex, so **adding an ingress needs no DNS change**. The apex `A` record's IP is owned by the cloudflareddns app; Terraform ignores its content.

- Secrets go in the gitignored `.env`, loaded by mise; see `.env.example`. It must set `CLOUDFLARE_API_TOKEN` and `TF_VAR_zone_name`.
- **The domain name is private.** Never commit it in Terraform, manifests, commit messages or PR text. Use `var.zone_name` / `${DOMAIN_0}`.
- State is committed **SOPS-encrypted** (`terraform.tfstate.sops.json`). Always run Terraform through the wrapper: `mise run tf plan` / `mise run tf apply`. It decrypts, runs and re-encrypts. Commit the updated `.sops.json` after an apply.
- `.claude/settings.json` denies agents read access to `.env`, `*.tfvars` and `age.agekey`. Don't work around it.

## Operations notes (lessons from the 2026-10 upgrades)

**Talos upgrades**
- The BOOT partition is only 1000 MB (old 1.9-era layout) and can't hold two NVIDIA-sized images. A plain upgrade fails with `write /boot/B/vmlinuz: no space left on device`. Use the **two-hop** trick:
  1. Delete the stale files of the inactive boot slot.
  2. Upgrade to the *current* version with a slim schematic (no NVIDIA).
  3. Upgrade to the target with the NVIDIA schematic.
- The node boots with GRUB and `grubUseUKICmdline: false`, so the SUC talos plan passes `--legacy` (Talos ≥ 1.13 would otherwise drop `net.ifnames=0`). Moving to the UKI cmdline is a deliberate, separate change.
- **The GTX 960 (Maxwell) needs the NVIDIA 580 driver branch**, the last one supporting it. NVIDIA supports 580 as a long-term branch until 2028-08. Talos ships it as the `nonfree-kmod-nvidia-lts` / `nvidia-container-toolkit-lts` extensions (580.x on Talos 1.13, 1.14 and `main`, see `nvidia_driver_lts_version` in siderolabs/pkgs `Pkgfile`). **Before every Talos upgrade, check that the target version's `-lts` extensions are still 580.x** (factory.talos.dev/version/<v>/extensions/official). If Sidero moves `lts` to a newer branch, the GPU stops working. The fallback is the CPU's Intel UHD 730 (Quick Sync) passed through instead. `mise.toml` pins talosctl/kubectl/flux to the cluster versions, so they move together with the cluster.
- Take an etcd snapshot first (`talosctl etcd snapshot`) and a ZFS snapshot of the VM disk on TrueNAS.
- **Manual upgrades (e.g. the two-hop): merge the `TALOS_VERSION` bump in `upgradesettings.yaml` *before* resuming Flux/`node:up`.** The SUC talos plan selects every node whose version is *not* `TALOS_VERSION`. With git still on the old version, it immediately "upgrades" the node back. That happened on 2026-10-08: the downgrade failed for lack of BOOT space and left a half-written slot, which had to be cleaned.
- `talconfig.yaml`'s `talosVersion` is the talhelper *config contract* (still 1.13-style), not the installed version.

**GPU**
- NVIDIA runs through Talos extensions (`nonfree-kmod-nvidia-lts`, `nvidia-container-toolkit-lts`) plus **gpu-operator** in `core/gpu-operator`. The driver and toolkit are disabled there, and CDI is on.
- **Time-slicing gives 5 `nvidia.com/gpu` slots.** Pods request `nvidia.com/gpu: 1` with `runtimeClassName: nvidia`. The CUDA validation workload is off because CUDA 13 dropped Maxwell.

**Shutting the node down safely (TrueNAS reboot, maintenance, `talos:apply`)**
- `talosctl shutdown` stops pods in parallel. Longhorn can go away while Postgres is still writing: I/O errors, then a crash-recovery.
- Use `mise run node:down [shutdown|reboot]` (`--dry-run` shows the plan). It auto-discovers everything mounting Longhorn volumes:
  - hibernates the CNPG clusters
  - scales Deployments/StatefulSets to 0
  - suspends CronJobs
  - pauses Flux
  - waits until every volume is detached, aborting and restoring on timeout
  - only then optionally shuts down or reboots
- Without an argument it only quiesces, e.g. before a TrueNAS reboot or `mise run talos:apply`.
- Afterwards `mise run node:up` undoes it from `.state/quiesce.json` and runs the health checks.

**Longhorn**
- `concurrentAutomaticEngineUpgradePerNodeLimit` must stay > 0, so volume engines follow the manager. Before a minor Longhorn upgrade, check that every volume runs the current engine image.

**TrueCharts charts**
- Newer `common` versions set `hostUsers: false` on k8s ≥ 1.33. Talos has user namespaces disabled, so pods fail with ENOSPC on `unshare`. Set `podOptions.hostUsers: true`.
- Some newer charts run as root, but NFS squashes root, so file ownership and plugin updates break. Set `securityContext.container.runAsUser/runAsGroup: 568` (the TrueNAS `apps` user).
- Several TrueCharts system charts are archived (cert-manager, cloudnative-pg, traefik). cert-manager and CNPG now use the upstream charts.

**TrueNAS host**
- The node is the `talos` VM (UEFI, autostart, zvol `tank/talos`). The GPU is isolated and passed through with vfio-pci. TrueNAS 25.10 dropped host NVIDIA support for Maxwell, which doesn't matter for passthrough.
- After a TrueNAS major upgrade, wait about a week before "Upgrade pool" (ZFS feature flags). Boot-environment rollback depends on it.
- Download the config backup (with the secret seed) before upgrading.

## Automation

- Renovate (`.github/renovate.json5` + `custom.json5`, extending the TrueCharts preset) opens `chore(flux): update ...` PRs every day before 06:00. Don't fight it: bump versions in the same places it does, and keep the `# renovate:` comments intact.
- PRs get labelled `area/kubernetes` / `area/github` automatically.

## Conventions

- Commit messages mix gitmoji (`⬆️ update panio to 0.0.10`, `:bug: ...`) and conventional commits (`feat(app): ...`). Either style is fine, but keep the subject short and scoped to the app.
- YAML uses 2-space indentation (a few older TrueCharts releases use 4-space). Match the file you're editing.
- Use lowercase-kebab-case for app and namespace names, and each app gets its own namespace (the media stack shares `media`).

## Don'ts

- Don't run `kubectl apply`, `flux reconcile`, `mise run talos:apply`, or `talosctl` against the live cluster unless the user explicitly asks. Git is the source of truth.
- Don't delete an app folder or a `ks.yaml` entry casually. `prune: true` removes the workload, and possibly its PVCs, from the cluster.
- Don't touch `flux-system/flux/*.secret.yaml`, or `talos/generated/` without being asked.
