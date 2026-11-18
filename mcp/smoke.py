# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2"]
# ///
"""Smoke test: act as an MCP host against server.py over stdio.

Does what a host does on connect — launch (or connect), initialize, tools/list — then
calls one tool.
    stdio: uv run --directory mcp smoke.py [tool] [json_args]
    http:  uv run --directory mcp smoke.py --url http://localhost:8000/mcp [tool] [json_args]
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


async def main(tool: str, args: dict, url: str | None) -> None:
    if url:
        transport = streamable_http_client(url)  # talk to an already-running HTTP server
    else:
        transport = stdio_client(StdioServerParameters(command="uv", args=["run", "server.py"]))
    async with transport as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected to server: {init.server_info.name}")
            print("instructions (first line):", (init.instructions or "").splitlines()[0])

            tools = await session.list_tools()
            print(f"\ntools/list -> {len(tools.tools)} tools")
            for t in tools.tools:
                first_line = (t.description or "").strip().splitlines()[0]
                print(f"  - {t.name}({', '.join(t.input_schema.get('properties', {}))}): {first_line}")

            print(f"\ntools/call {tool} {json.dumps(args)}")
            result = await session.call_tool(tool, args)
            for block in result.content:
                text = getattr(block, "text", str(block))
                print(text[:6000] + ("…" if len(text) > 6000 else ""))


if __name__ == "__main__":
    argv = sys.argv[1:]
    url = None
    if argv and argv[0] == "--url":  # smoke.py --url http://localhost:8000/mcp [tool] [json]
        url, argv = argv[1], argv[2:]
    tool = argv[0] if argv else "list_tables"
    args = json.loads(argv[1]) if len(argv) > 1 else {}
    asyncio.run(main(tool, args, url))
