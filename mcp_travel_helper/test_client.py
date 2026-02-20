#!/usr/bin/env python3
"""
Test client: send a query to the Travel Helper MCP server (Streamable HTTP).

Usage:
  # Start the server first: python -m mcp_travel_helper --transport streamable-http --port 8080
  python -m mcp_travel_helper.test_client [--url http://127.0.0.1:8080/mcp] [--tool travel_deals_cheapest] [--top 3]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def run(url: str, tool: str, list_tools: bool = False, **kwargs: object) -> None:
    async with streamable_http_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if list_tools:
                tools = await session.list_tools()
                for t in (tools.tools or []):
                    print(t.name)
                return
            result = await session.call_tool(tool, kwargs or {})
            # Print structured content if present, else first text block
            if getattr(result, "structuredContent", None) is not None:
                print(json.dumps(result.structuredContent, indent=2))
            elif result.content:
                for block in result.content:
                    text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None)
                    if text:
                        try:
                            data = json.loads(text)
                            print(json.dumps(data, indent=2))
                        except json.JSONDecodeError:
                            print(text)
                        break
            else:
                print(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Travel Helper MCP server (Streamable HTTP)")
    parser.add_argument("--url", default="http://127.0.0.1:8080/mcp", help="MCP endpoint URL")
    parser.add_argument("--tool", default="travel_deals_cheapest", help="Tool to call")
    parser.add_argument("--top", type=int, default=3, help="For travel_deals_cheapest: top_n")
    parser.add_argument("--limit", type=int, default=5, help="For travel_deals_list: limit")
    parser.add_argument("--query", default="", help="For travel_deals_search: query")
    parser.add_argument("--list-tools", action="store_true", help="List tools the server exposes (to verify server version)")
    args = parser.parse_args()

    if args.list_tools:
        kwargs = {}
    elif args.tool == "travel_deals_cheapest":
        kwargs = {"top_n": args.top}
    elif args.tool == "travel_deals_list":
        kwargs = {"limit": args.limit}
    elif args.tool == "travel_deals_search":
        kwargs = {"query": args.query or "Barcelona", "limit": args.limit}
    elif args.tool == "travel_deals_data_status":
        kwargs = {}
    else:
        kwargs = {}

    try:
        asyncio.run(run(args.url, args.tool, args.list_tools, **kwargs))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
