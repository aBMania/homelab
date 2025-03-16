This is a kubernetes cluster Powered by TrueCharts ClusterTool

## Additional steps after flux setup

```shell
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/crd/bases/metallb.io_ipaddresspools.yaml
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/crd/bases/metallb.io_l2advertisements.yaml
```
