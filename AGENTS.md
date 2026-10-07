# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this repo is

A GitOps homelab: a single-node [Talos](https://www.talos.dev/) Kubernetes cluster (`k8s-control-1`, a VM on TrueNAS with an NVIDIA GPU), managed with [FluxCD](https://fluxcd.io/) and bootstrapped with TrueCharts' `clustertool`. Everything merged to `main` gets reconciled into the live cluster, so **every change to `clusters/` or `repositories/` is a production deploy**.

The Flux `GitRepository` (`repositories/git/this-repo.yaml`) only watches `/clusters` and `/repositories`. Changes anywhere else never reach the cluster.

## Layout

```
clusters/main/
  clusterenv.yaml            # SOPS-encrypted cluster variables (IPs, domains, tokens…)
  talos/
    talconfig.yaml           # talhelper config for the node (Talos + k8s versions, extensions)
    patches/                 # Talos machine-config patches
    generated/               # clustertool output; only talsecret.yaml (encrypted) is tracked
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
.sops.yaml                   # encryption rules (managed by clustertool, see below)
```

`clustertool` (the binary at the repo root) and `age.agekey` are gitignored local files. Never commit, print, or copy the contents of `age.agekey`.

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
- Substitution applies to every Flux Kustomization. To opt one out, label it `substitution.flux.home.arpa/disabled: "true"`. If a manifest needs a literal `${...}`, escape it as `$${...}`.
- SOPS (age) encrypts:
  - any `*values.yaml` under `clusters/**/kubernetes/`
  - any `*.secret.yaml` under `clusters/**/kubernetes/`
  - `clusterenv.yaml` and `talsecret.yaml`
  
  Only keys that match the `encrypted_regex` in `.sops.yaml` get encrypted (`pass`, `secret`, `key`, `token`, `email`, `data`, `stringData`, …).
- **Never commit plaintext secrets** and never hand-edit `ENC[...]` blobs. Put secrets in a `*.secret.yaml` file and encrypt it with `sops -e -i <file>`, or reference a `clusterenv.yaml` variable. If you can't encrypt, stop and ask.
- Don't edit `.sops.yaml` above the `## DO NOT REMOVE` line, because clustertool manages that section.
- `*.yaml.ct` files (for example the Cilium bootstrap values) are clustertool templates rendered from `clusterenv.yaml`. Don't hand-edit them.

## Single-node gotchas

The cluster has exactly one node. Before bumping a chart, check its new defaults with `helm template`. Charts that ship HA defaults can't become ready here, and their upgrades time out and roll back:

- More than one replica of a pod that uses `hostNetwork`/`hostPort`. This is why the cilium-operator is pinned to `replicas: 1`.
- Pods that must run on separate nodes (`podAntiAffinity` on `kubernetes.io/hostname`). For example, OpenEBS ≥ 4.3 ships Loki, which is disabled here for that reason.

If a HelmRelease shows `Stalled` / `RetriesExceeded`, Flux has stopped retrying it. Once the fix is merged, it needs `flux reconcile hr -n <ns> <name> --force`.

## Talos / node

- Node config lives in `clusters/main/talos/talconfig.yaml` and `patches/`. Changes there are applied manually with clustertool (`clustertool genconfig`, `clustertool apply`), not by Flux.
- Talos and Kubernetes versions are pinned in `talconfig.yaml` with Renovate comments. Upgrades run through system-upgrade-controller plans in `core/system-upgrade-controller-plans/`. Major bumps are deliberately not automerged.
- `talconfig.json` is only a JSON schema for editor support.
- The admin kubeconfig client certificate expires after a year. If `kubectl` says "the server has asked for the client to provide credentials", generate a new one with `talosctl --talosconfig clusters/main/talos/generated/talosconfig -n <node-ip> kubeconfig <path>`. Talos itself is reachable with that talosconfig.

## Validation

CLI tools are pinned in `mise.toml`. Run `mise install` once; inside the repo, mise puts them on PATH. `talosctl`, `kubectl` and `flux` match the cluster versions. Bump them together with the cluster, never ahead of it: Talos must stay on 1.13 because of the GTX 960. Tasks: `mise run validate` (same checks as CI) and `mise run tf <args>` (Terraform, see below).

Before proposing a change to `clusters/main/kubernetes/`, run the same checks as CI:

```bash
bash ./scripts/kubeconform.sh ./clusters/main/kubernetes   # needs kustomize + kubeconform
kustomize build clusters/main/kubernetes/<group>/<app>/app  # quick check for one app
```

A local git pre-commit hook (`.git/hooks/pre-commit`) runs `./clustertool adv precommit`, which refuses to commit unencrypted secrets. Don't bypass it with `--no-verify`. If it fails, fix the cause.

CI (`.github/workflows/Tests.yaml`) runs kubeconform plus a `flux-local` diff on PRs that touch `clusters/main/kubernetes/**`. The automerge jobs (`pascalgn/automerge-action`) only squash-merge PRs that pass **and** carry the `automerge` label. Never add that label without the user's approval, because merging to `main` deploys.

The `.devcontainer` (TrueCharts devcontainer) ships with flux, kubectl, talosctl, sops and the other needed tools.

## DNS (Terraform)

Public DNS for `DOMAIN_0` lives in Cloudflare and is managed by Terraform in `terraform/cloudflare/`. A wildcard `*.<domain>` CNAME points every app name at the apex, so **adding an ingress needs no DNS change**. The apex `A` record's IP is owned by the cloudflareddns app; Terraform ignores its content.

- Secrets go in the gitignored `.env`, loaded by mise; see `.env.example`. It must set `CLOUDFLARE_API_TOKEN` and `TF_VAR_zone_name`.
- **The domain name is private.** Never commit it in Terraform, manifests, commit messages or PR text. Use `var.zone_name` / `${DOMAIN_0}`.
- State is committed **SOPS-encrypted** (`terraform.tfstate.sops.json`). Always run Terraform through the wrapper: `mise run tf plan` / `mise run tf apply`. It decrypts, runs and re-encrypts. Commit the updated `.sops.json` after an apply.
- `.claude/settings.json` denies agents read access to `.env`, `*.tfvars` and `age.agekey`. Don't work around it.

## Automation

- Renovate (`.github/renovate.json5` + `custom.json5`, extending the TrueCharts preset) opens `chore(flux): update ...` PRs every day before 06:00. Don't fight it: bump versions in the same places it does, and keep the `# renovate:` comments intact.
- PRs get labelled `area/kubernetes` / `area/github` automatically.

## Conventions

- Commit messages mix gitmoji (`⬆️ update panio to 0.0.10`, `:bug: ...`) and conventional commits (`feat(app): ...`). Either style is fine, but keep the subject short and scoped to the app.
- YAML uses 2-space indentation (a few older TrueCharts releases use 4-space). Match the file you're editing.
- Use lowercase-kebab-case for app and namespace names, and each app gets its own namespace (the media stack shares `media`).

## Don'ts

- Don't run `kubectl apply`, `flux reconcile`, `clustertool apply`, or `talosctl` against the live cluster unless the user explicitly asks. Git is the source of truth.
- Don't delete an app folder or a `ks.yaml` entry casually. `prune: true` removes the workload, and possibly its PVCs, from the cluster.
- Don't touch `flux-system/flux/*.secret.yaml`, `talos/generated/`, or the bootstrap `.ct` files without being asked.
