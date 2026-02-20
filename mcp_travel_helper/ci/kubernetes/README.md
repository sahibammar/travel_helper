# Kubernetes — MCP Travel Helper server

Deploy the Travel Helper MCP server (Streamable HTTP) on Kubernetes.

**Google Cloud (GCP):** See [GCP.md](GCP.md) for **Cloud Run** (recommended) and **GKE** steps (build with Cloud Build, push to Artifact Registry, deploy).

## Prerequisites

- Cluster access and `kubectl` configured
- Docker image available to the cluster:
  - **Minikube / kind / local:** build locally and load (see below)
  - **Remote cluster:** build, tag, and push to a registry the cluster can pull from

## Build image for local clusters (Minikube / kind / Docker Desktop K8s)

From the **repository root**:

```bash
# Minikube: use the Docker daemon inside Minikube
eval $(minikube docker-env)
docker build -f mcp_travel_helper/ci/Dockerfile -t travel-helper-mcp:latest .

# kind: load after build
docker build -f mcp_travel_helper/ci/Dockerfile -t travel-helper-mcp:latest .
kind load docker-image travel-helper-mcp:latest
```

For a **remote registry** (GCR, ECR, GHCR, etc.):

```bash
docker build -f mcp_travel_helper/ci/Dockerfile -t <registry>/travel-helper-mcp:latest .
docker push <registry>/travel-helper-mcp:latest
```

Then set that image in `deployment.yaml` (e.g. `image: ghcr.io/myuser/travel-helper-mcp:latest`).

## Deploy

```bash
kubectl apply -f mcp_travel_helper/ci/kubernetes/
```

Or apply files individually:

```bash
kubectl apply -f mcp_travel_helper/ci/kubernetes/configmap.yaml   # optional
kubectl apply -f mcp_travel_helper/ci/kubernetes/deployment.yaml
kubectl apply -f mcp_travel_helper/ci/kubernetes/service.yaml
```

## Access

- **Inside the cluster:** `http://travel-helper-mcp:8000` (Service name, port 8000).
- **From your machine (port-forward):**
  ```bash
  kubectl port-forward svc/travel-helper-mcp 8000:8000
  ```
  Then open http://localhost:8000/docs and use the MCP endpoint at http://localhost:8000/mcp.

## Expose externally (optional)

To expose the service outside the cluster, either:

1. **Change Service type to LoadBalancer** (edit `service.yaml`, set `type: LoadBalancer`), or
2. **Add an Ingress** pointing to `travel-helper-mcp:8000`.

## Mount real data (travel_helper.json)

1. Create a ConfigMap from your JSON file:
   ```bash
   kubectl create configmap travel-helper-mcp-data --from-file=travel_helper.json=/path/to/travel_helper.json
   ```
2. In `deployment.yaml`, uncomment the `volumeMounts` under the container and the `volumes` section at the bottom.
3. Apply: `kubectl apply -f mcp_travel_helper/ci/kubernetes/deployment.yaml`

If the JSON is large, consider a PersistentVolumeClaim and mount it at `/app/travel_helper.json` instead of a ConfigMap.

## Remove

```bash
kubectl delete -f mcp_travel_helper/ci/kubernetes/
```
