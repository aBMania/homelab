# Homelab

GitOps for a single-node [Talos](https://www.talos.dev/) Kubernetes cluster running as a VM on my [TrueNAS](https://www.truenas.com/) NAS (with an NVIDIA GPU passed through).

- Talos node config: `clusters/main/talos` (talhelper, see `mise run talos:genconfig`)
- Kubernetes and all apps: `clusters/main/kubernetes`, deployed by [FluxCD](https://fluxcd.io/)
- Public DNS: `terraform/cloudflare` (Terraform, SOPS-encrypted state)
- Updates: [Renovate](https://docs.renovatebot.com/) opens PRs, reviewed and merged by hand for anything risky
- Tools: `mise install`, then `mise tasks` lists the helpers (validate, talos:*, tf, cluster:bootstrap, sops:check)

[AGENTS.md](AGENTS.md) is the detailed guide: layout, conventions, secrets, operations notes and disaster recovery.
