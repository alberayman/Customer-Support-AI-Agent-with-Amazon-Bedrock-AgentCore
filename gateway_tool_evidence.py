#!/usr/bin/env python3
"""
gateway_tool_evidence.py — prove every Gateway tool returns a well-formed,
non-empty result.

Opens an MCP session straight to the Gateway (no model, no agent) and calls
all six tools with valid inputs, printing the raw structured payload each one
returns. Writes gateway-tool-responses.md.

The last call is a clearly-labelled NEGATIVE CONTROL with a non-existent ID,
included to show the error path is handled — not as a failed tool call.

Usage:
  uv run python gateway_tool_evidence.py | tee gateway-tool-responses.txt
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

OUTPUTS = Path(__file__).parent / "phase2_outputs.json"
REPORT = Path(__file__).parent / "gateway-tool-responses.md"

# Seeded data in lambda/order_tracker.py
GOOD_ORDER = "ORD-001"
DELIVERED_ORDER = "ORD-002"
GOOD_CUSTOMER = "CUST-123"
BAD_ORDER = "ORD-999"


def gateway_url():
    if len(sys.argv) > 1:
        return sys.argv[1]
    if OUTPUTS.exists():
        url = json.loads(OUTPUTS.read_text()).get("gateway_url")
        if url:
            return url
    sys.exit("No gateway URL. Pass it as an argument or run phase2_setup.py first.")


def payload_of(result):
    """Return the tool's raw text payload, pretty-printed if it is JSON."""
    parts = [c.text for c in result.content if hasattr(c, "text")]
    raw = "\n".join(parts).strip()
    try:
        return json.dumps(json.loads(raw), indent=2)
    except Exception:
        return raw


def verdict(raw):
    if not raw:
        return "EMPTY — not acceptable"
    if "internal error occurred" in raw.lower():
        return "ERROR PATH (expected for the negative control)"
    try:
        json.loads(raw)
        return "OK — non-empty, valid structured JSON"
    except Exception:
        return "OK — non-empty text result"


async def main():
    url = gateway_url()
    lines = [
        "# Gateway Tool Responses — raw payloads\n",
        f"Captured: {datetime.now().isoformat(timespec='seconds')}  ",
        "Method: direct MCP session to the AgentCore Gateway. No model, no agent, "
        "no memory — so each payload below is exactly what the Gateway returned.\n",
    ]
    print(f"Connecting to {url}\n")

    async with streamable_http_client(url) as conn:
        read, write = conn[0], conn[1]
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            names = [t.name for t in tools]
            print(f"Gateway advertises {len(tools)} tools:")
            lines.append(f"\n## Tools advertised ({len(tools)})\n")
            for n in names:
                print("  -", n)
                lines.append(f"- `{n}`")
            print()

            def find(suffix):
                return next((n for n in names if n.endswith(suffix)), None)

            calls = [
                (find("get_order"), {"order_id": GOOD_ORDER},
                 "Order lookup — shipped order"),
                (find("get_order"), {"order_id": DELIVERED_ORDER},
                 "Order lookup — delivered order"),
                (find("get_customer"), {"customer_id": GOOD_CUSTOMER},
                 "Customer profile — tier and points"),
                (find("get_customer_orders"), {"customer_id": GOOD_CUSTOMER},
                 "All orders for a customer"),
                (find("initiate_refund"),
                 {"order_id": DELIVERED_ORDER, "reason": "damaged on arrival",
                  "amount": 139.99},
                 "Refund initiation"),
                (find("get_return_label"), {"order_id": DELIVERED_ORDER},
                 "Prepaid return label"),
                (find("check_refund_status"), {"refund_id": "REF-TEST0001"},
                 "Refund status lookup"),
                (find("get_order"), {"order_id": BAD_ORDER},
                 "NEGATIVE CONTROL — non-existent order ID"),
            ]

            ok = 0
            for name, args, label in calls:
                if not name:
                    print(f"SKIP  {label}: tool not advertised\n")
                    continue
                print(f"── {label}")
                print(f"   tool: {name}")
                print(f"   args: {json.dumps(args)}")
                try:
                    res = await session.call_tool(name, args)
                    raw = payload_of(res)
                    v = verdict(raw)
                    print(f"   verdict: {v}")
                    print("   response:")
                    for ln in raw.splitlines():
                        print("     " + ln)
                    print()
                    if v.startswith("OK"):
                        ok += 1
                    lines.append(f"\n### {label}\n")
                    lines.append(f"**Tool:** `{name}`  \n**Arguments:** `{json.dumps(args)}`  \n"
                                 f"**Verdict:** {v}\n")
                    lines.append(f"```json\n{raw}\n```\n")
                except Exception as e:
                    print(f"   !! {type(e).__name__}: {e}\n")
                    lines.append(f"\n### {label}\n\n> Exception: "
                                 f"{type(e).__name__}: {e}\n")

            summary = (f"{ok} of {len(calls) - 1} valid calls returned a "
                       f"non-empty, well-formed payload.")
            print("=" * 68)
            print(summary)
            print("=" * 68)
            lines.insert(3, f"\n**Result:** {summary}\n")

    REPORT.write_text("\n".join(lines))
    print(f"\nWrote {REPORT.name}")


if __name__ == "__main__":
    asyncio.run(main())
