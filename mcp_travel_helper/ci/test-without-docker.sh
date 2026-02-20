#!/usr/bin/env sh
# Test the MCP server the same way the Docker image runs it (no Docker required).
# From repo root: sh mcp_travel_helper/ci/test-without-docker.sh

set -e
cd "$(dirname "$0")/../.."
PORT=8010
export TRAVEL_HELPER_JSON=mcp_travel_helper/ci/travel_helper.json.sample

echo "Starting MCP server on port $PORT (sample data)..."
.venv-travel/bin/python -m mcp_travel_helper --transport streamable-http --port "$PORT" &
PID=$!
trap "kill $PID 2>/dev/null || true" EXIT
sleep 3

echo "Calling travel_deals_cheapest..."
.venv-travel/bin/python -m mcp_travel_helper.test_client --url "http://127.0.0.1:$PORT/mcp" --tool travel_deals_cheapest --top 2

echo "Checking /docs..."
code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/docs")
if [ "$code" = "200" ]; then
  echo "OK: /docs returned 200"
else
  echo "FAIL: /docs returned $code"
  exit 1
fi
echo "Done."
