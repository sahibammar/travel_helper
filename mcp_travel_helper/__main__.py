"""Run the Travel Helper MCP server (STDIO or Streamable HTTP)."""
import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Travel Helper MCP server — reads travel_helper.json, exposes deal tools.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("TRAVEL_HELPER_MCP_TRANSPORT", "stdio"),
        help="Transport: stdio (for Cursor/Claude Desktop) or streamable-http (HTTP server).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("TRAVEL_HELPER_MCP_HOST", "127.0.0.1"),
        help="Bind host for streamable-http (use 0.0.0.0 to accept external connections).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TRAVEL_HELPER_MCP_PORT", "8000")),
        help="Bind port for streamable-http.",
    )
    args = parser.parse_args()

    from mcp_travel_helper.server import mcp

    # Apply host/port for Streamable HTTP (FastMCP uses __init__ defaults otherwise)
    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(f"Travel Helper MCP: Streamable HTTP at http://{args.host}:{args.port}/mcp", file=sys.stderr)
        print(f"Documentation: http://{args.host}:{args.port}/docs", file=sys.stderr)
        _run_streamable_http_with_docs(mcp, args.host, args.port)
    else:
        mcp.run(transport="stdio")


def _run_streamable_http_with_docs(mcp, host: str, port: int) -> None:
    """Run Streamable HTTP with /docs served first (wrapper app) so /docs never 404s."""
    import anyio
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Mount, Route

    _docs_path = Path(__file__).resolve().parent / "docs.html"

    async def _docs_handler(request):
        if _docs_path.exists():
            return HTMLResponse(_docs_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Docs</h1><p>docs.html not found.</p>", status_code=404)

    mcp_app = mcp.streamable_http_app()

    async def _lifespan(app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    wrapper_app = Starlette(
        routes=[
            Route("/docs", _docs_handler, methods=["GET"]),
            Mount("/", mcp_app),
        ],
        lifespan=_lifespan,
    )

    import uvicorn
    config = uvicorn.Config(
        wrapper_app,
        host=host,
        port=port,
        log_level=mcp.settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    async def _serve():
        await server.serve()

    anyio.run(_serve)


if __name__ == "__main__":
    main()
