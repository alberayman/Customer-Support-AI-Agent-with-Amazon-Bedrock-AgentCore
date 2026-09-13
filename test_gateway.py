#!/usr/bin/env python3
"""
test_gateway.py — talk to the Gateway with nothing in the way.

No model, no agent, no memory. Opens an MCP session straight to the Gateway
URL, lists the tools it advertises, and calls each one with a known-good and a
known-bad ID.

This is the single most useful diagnostic in the project: it answers
"is the Gateway broken, or is something above it broken?" in one run.
It works because the Gateway uses the NONE authorizer.

Usage:
  uv run python test_gateway.py                       # reads phase2_outputs.json
  uv run python test_gateway.py <gateway-url>
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

OUTPUTS = Path(__file__).parent / "phase2_outputs.json"

# Seeded data in order_tracker.py — these are the IDs that actually exist.
GOOD_ORDER, BAD_ORDER = "ORD-001", "ORD-999"
GOOD_CUSTOMER = "CUST-123"


def gateway_url():
    if len(sys.argv) > 1:
        return sys.argv[1]
    if OUTPUTS.exists():
        url = json.loads(OUTPUTS.read_text()).get("gateway_url")
        if url:
            return url
    sys.exit("No gateway URL. Pass it as an argument or run phase2_setup.py first.")


def preview(result, limit=400):
    try:
        parts = [c.text for c in result.content if hasattr(c, "text")]
        return (" ".join(parts))[:limit]
    except Exception:
        return str(result)[:limit]


async def main():
    url = gateway_url()
    print(f"Connecting to {url}\n")

    async with streamable_http_client(url) as conn:
        read, write = conn[0], conn[1]
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            print(f"Gateway advertises {len(tools)} tools:")
            for t in tools:
                print(f"  - {t.name}")
            if len(tools) < 4:
                print("\n>>> Fewer than 4 tools. A target is FAILED. Check:")
                print("    aws bedrock-agentcore-control list-gateway-targets \\")
                print("      --gateway-identifier <id>")
                print("    then get-gateway-target and read statusReasons.")
            print()

            # Tool names are <target-name>___<operation>, so match on the suffix.
            def find(suffix):
                return next((t.name for t in tools
                             if t.name.endswith(suffix)), None)

            checks = [
                (find("get_order"), {"order_id": GOOD_ORDER}, "valid order"),
                (find("get_order"), {"order_id": BAD_ORDER}, "missing order"),
                (find("get_customer"), {"customer_id": GOOD_CUSTOMER}, "valid customer"),
                (find("get_customer_orders"), {"customer_id": GOOD_CUSTOMER}, "customer orders"),
                (find("initiate_refund"),
                 {"order_id": "ORD-002", "reason": "defective", "amount": 139.99},
                 "refund"),
            ]

            for name, args, label in checks:
                if not name:
                    print(f"SKIP  {label}: tool not advertised")
                    continue
                print(f"CALL  {name}  {args}   ({label})")
                try:
                    res = await session.call_tool(name, args)
                    print(f"  -> {preview(res)}\n")
                except Exception as e:
                    print(f"  !! {type(e).__name__}: {e}\n")

    print("Reading the results:")
    print("  valid IDs return data      -> Gateway and backend are fine;")
    print("                                any agent failure is above this layer.")
    print("  'An internal error occurred' on BOTH valid and invalid IDs")
    print("                             -> target config or IAM, not the data.")
    print("  0 or <4 tools              -> a FAILED target (E6/E7).")


if __name__ == "__main__":
    asyncio.run(main())
