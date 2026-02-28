# MCP Travel Helper server — Streamable HTTP
# Build from repo root: docker build -f mcp_travel_helper/ci/Dockerfile .
# Run: docker run -p 8000:8000 [-v /path/to/data/travel_helper.json:/app/data/travel_helper.json] <image>

FROM python:3.12-slim

WORKDIR /app

# MCP server only needs mcp (FastMCP + Streamable HTTP)
RUN pip install --no-cache-dir "mcp[cli]"

# Copy package; use sample data so image builds without repo data/travel_helper.json (override at run with -v)
COPY mcp_travel_helper/ /app/mcp_travel_helper/
RUN mkdir -p /app/data
COPY mcp_travel_helper/ci/travel_helper.json.sample /app/data/travel_helper.json

ENV TRAVEL_HELPER_MCP_TRANSPORT=streamable-http \
    TRAVEL_HELPER_MCP_HOST=0.0.0.0 \
    TRAVEL_HELPER_MCP_PORT=8000

EXPOSE 8000

# Streamable HTTP: /mcp and /docs
CMD ["python", "-m", "mcp_travel_helper", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
