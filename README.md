# OpenShift Install

The OpenShift installer `openshift-install` makes it easy to get a cluster
running on the public cloud or your local infrastructure.

To learn more about installing OpenShift, visit [docs.openshift.com](https://docs.openshift.com)
and select the version of OpenShift you are using.

## Installing the tools

After extracting this archive, you can move the `openshift-install` binary
to a location on your PATH such as `/usr/local/bin`, or keep it in a temporary
directory and reference it via `./openshift-install`.

## License

OpenShift is licensed under the Apache Public License 2.0. The source code for this
program is [located on github](https://github.com/openshift/installer).
# hello-chris-ai-homelab

## Kustomize bundles

This repository contains OpenShift-ready kustomize bundles under `hello-chris-ai-homelab/.k8s/`.

### Home Assistant
- Manifests: `hello-chris-ai-homelab/.k8s/`
- Includes: Namespace, ServiceAccount, PVC, Deployment, Service, Route (edge TLS), Kustomization
- Deployment strategy: Recreate (avoids two pods mounting the same PVC)
- SCC: uses `anyuid` via ServiceAccount; the pod runs as UID 0 to satisfy the container’s init

Deploy:
```bash
oc apply -k hello-chris-ai-homelab/.k8s
oc -n home-assistant rollout status deploy/home-assistant
oc -n home-assistant get route home-assistant -o jsonpath='https://{.spec.host}\n'
```
Notes:
- If you get 400 Bad Request via the Route, add reverse-proxy settings inside Home Assistant’s `/config/configuration.yaml`:
  ```yaml
  http:
    use_x_forwarded_for: true
    trusted_proxies:
      - 10.128.0.0/14
  homeassistant:
    external_url: "https://<your-route-host>"
  ```

### n8n (exported from existing namespace)
- Directory: `hello-chris-ai-homelab/.k8s/n8n`
- Resources exported from current `n8n` namespace with cluster-specific fields removed
- Secrets: generated via `secretGenerator`; populate placeholders under `hello-chris-ai-homelab/.k8s/n8n/secrets/n8n-secrets/`

Deploy:
```bash
oc apply -k hello-chris-ai-homelab/.k8s/n8n
oc -n n8n rollout status deploy/n8n
oc -n n8n get route n8n -o jsonpath='https://{.spec.host}\n'
```
Adjustments for new clusters:
- Update Route hosts to the new router domain if needed.
- Set a `storageClassName` in the PVC JSON if your cluster lacks a default.
- For reproducibility, pin the container images to a digest instead of tags like `latest`.
