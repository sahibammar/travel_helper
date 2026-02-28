# Docker — MCP Travel Helper server

Build and run the **Travel Helper MCP server** (Streamable HTTP) in a container.

## Build

From the **repository root**:

```bash
docker build -f mcp_travel_helper/ci/Dockerfile -t travel-helper-mcp .
```

To bake in your own `data/travel_helper.json`, ensure it exists in the repo and add to the Dockerfile before the final `COPY`:

```dockerfile
COPY data/travel_helper.json /app/data/travel_helper.json
```

(or use a multi-stage build that generates it).

## Run

```bash
# Default: server on http://0.0.0.0:8000 (MCP at /mcp, docs at /docs)
docker run -p 8000:8000 travel-helper-mcp
```

With your own data file:

```bash
docker run -p 8000:8000 -v /path/to/data/travel_helper.json:/app/data/travel_helper.json travel-helper-mcp
```

## Use

- **MCP endpoint**: http://localhost:8000/mcp  
- **Docs**: http://localhost:8000/docs  

Test with the project test client:

```bash
python -m mcp_travel_helper.test_client --url http://localhost:8000/mcp --tool travel_deals_cheapest --top 3
```

## Kubernetes

Manifests in **ci/kubernetes/** deploy the server to a cluster:

```bash
kubectl apply -f mcp_travel_helper/ci/kubernetes/
```

See [ci/kubernetes/README.md](kubernetes/README.md) for image build, port-forward, and mounting `data/travel_helper.json`.

## Image

- **Base**: `python:3.12-slim`
- **Install**: `mcp[cli]` only (no Ryanair/Trivago/GeoTemp deps)
- **Default data**: empty deal list (`mcp_travel_helper/ci/travel_helper.json.sample`); mount real JSON at `/app/data/travel_helper.json` at runtime if needed.
