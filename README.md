# Homelab

This repository manage a [TalosOS](https://www.talos.dev/) VM that runs in my [TrueNAS](https://www.truenas.com/) Home NAS.

Key informations:
- Setup of the TalosOS VM lives in `cluster/main/talos`
- Setup of Kubernetes and all applications lives in `cluster/main/kubernetes`
- Everything is automatically deployed using [FluxCD](https://fluxcd.io/)
- Everything is automatically kept up to date using [Renovate](https://docs.renovatebot.com/)

### Side notes

Due to problems at setup time, I had to run these additional steps after flux setup:

```shell
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/crd/bases/metallb.io_ipaddresspools.yaml
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/crd/bases/metallb.io_l2advertisements.yaml
```
