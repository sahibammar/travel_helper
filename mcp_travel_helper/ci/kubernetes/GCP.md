# Run MCP Travel Helper on Google Cloud (GCP)

Two options: **Cloud Run** (simplest, serverless) or **GKE** (Kubernetes).

---

## Option 1: Cloud Run (recommended for a single service)

No cluster to manage. You get a HTTPS URL (e.g. `https://travel-helper-mcp-xxx.run.app`).

### Prerequisites

- [Google Cloud SDK (gcloud)](https://cloud.google.com/sdk/docs/install) installed and logged in
- A GCP project: `gcloud config set project YOUR_PROJECT_ID`

### Build and deploy

From the **repository root**:

```bash
# 1. Configure Docker to use GCP Artifact Registry (or gcr.io)
gcloud auth configure-docker

# 2. Set your project and region
export PROJECT_ID=$(gcloud config get-value project)
export REGION=europe-west4

# 3. Create Artifact Registry repo (one-time)
gcloud artifacts repositories create travel-helper-mcp \
  --repository-format=docker --location=$REGION 2>/dev/null || true

# 4. Build and push with Cloud Build (no local Docker needed)
gcloud builds submit --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/travel-helper-mcp/server:latest \
  -f mcp_travel_helper/ci/Dockerfile .

# 5. Deploy to Cloud Run (port 8000, allow unauthenticated for MCP/docs)
gcloud run deploy travel-helper-mcp \
  --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/travel-helper-mcp/server:latest \
  --platform managed --region $REGION \
  --port 8000 \
  --allow-unauthenticated \
  --set-env-vars "TRAVEL_HELPER_MCP_TRANSPORT=streamable-http,TRAVEL_HELPER_MCP_HOST=0.0.0.0,TRAVEL_HELPER_MCP_PORT=8000"
```

### Use your data file (optional)

Upload `travel_helper.json` to Cloud Storage and mount it, or bake it into the image before step 4. Easiest: add a step before the build to copy the file into the build context and add a line in the Dockerfile to COPY it, then run the build again.

Alternatively, use a **Secret** or **Cloud Storage Fuse** (more setup). For small JSON you can pass as env (base64) or use a startup script that fetches from GCS.

### Get the URL

After deploy, the CLI prints the service URL, or:

```bash
gcloud run services describe travel-helper-mcp --region $REGION --format 'value(status.url)'
```

- **Docs:** `https://YOUR_SERVICE_URL/docs`
- **MCP endpoint:** `https://YOUR_SERVICE_URL/mcp`

Test:

```bash
python -m mcp_travel_helper.test_client --url https://YOUR_SERVICE_URL/mcp --tool travel_deals_cheapest --top 3
```

---

## Option 2: GKE (Kubernetes)

Use the existing manifests in `ci/kubernetes/` and push the image to GCP so the cluster can pull it.

### Prerequisites

- GKE cluster created and `kubectl` connected:  
  `gcloud container clusters get-credentials CLUSTER_NAME --region REGION`
- Docker (or Cloud Build) to build and push the image

### Build and push image

From the **repository root**:

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION=europe-west4

# Build and push with Cloud Build
gcloud builds submit --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/travel-helper-mcp/server:latest \
  -f mcp_travel_helper/ci/Dockerfile .
```

If your cluster uses **gcr.io** instead of Artifact Registry:

```bash
gcloud builds submit --tag gcr.io/${PROJECT_ID}/travel-helper-mcp:latest \
  -f mcp_travel_helper/ci/Dockerfile .
```

### Update deployment image

Edit `mcp_travel_helper/ci/kubernetes/deployment.yaml` and set the container image:

- Artifact Registry: `image: europe-west4-docker.pkg.dev/YOUR_PROJECT_ID/travel-helper-mcp/server:latest`
- GCR: `image: gcr.io/YOUR_PROJECT_ID/travel-helper-mcp:latest`

Set `imagePullPolicy: Always` (or leave as `IfNotPresent` after first pull).

If the cluster is in a different project or needs pull secrets, create an imagePullSecret for the GCP registry.

### Deploy

```bash
kubectl apply -f mcp_travel_helper/ci/kubernetes/
```

### Expose and access

- **Port-forward:** `kubectl port-forward svc/travel-helper-mcp 8000:8000` then use http://localhost:8000/docs and /mcp.
- **LoadBalancer:** change the Service to `type: LoadBalancer` and use the external IP.
- **Ingress:** add an Ingress resource pointing to `travel-helper-mcp:8000` and (optionally) a TLS cert.

### Mount real data

Create a ConfigMap from your `travel_helper.json`, uncomment the volume/volumeMount in `deployment.yaml`, then re-apply. See the main [ci/kubernetes/README.md](README.md).

---

## Summary

| Option     | Best for              | URL / access                          |
|-----------|------------------------|----------------------------------------|
| **Cloud Run** | Single service, no K8s | HTTPS URL from `gcloud run deploy`     |
| **GKE**       | Already using GKE      | Port-forward, LoadBalancer, or Ingress |

For most cases, **Cloud Run** is the fastest way to run the MCP server in a GCP project.
