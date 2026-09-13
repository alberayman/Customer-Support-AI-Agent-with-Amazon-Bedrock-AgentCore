#!/usr/bin/env python3
"""
discount_tool_evidence.py — capture the loyalty discount tool's raw result.

The AgentCore runtime logs record tool NAMES but not tool RESULTS, so the raw
structured payload cannot be recovered from CloudWatch. This calls the tool
directly and records:

  1. the SUCCESS path  — computed in the AgentCore Code Interpreter sandbox
  2. the FALLBACK path — forced by making code_session raise, to prove both
     paths return the same key set

Writes discount-tool-result.md.

Usage:
  uv run python discount_tool_evidence.py | tee discount-tool-result.txt
"""

import json
from datetime import datetime
from pathlib import Path

import main as agent_module
from main import calculate_loyalty_discount

REPORT = Path(__file__).parent / "discount-tool-result.md"

REQUIRED_FIELDS = [
    "points_redeemed",
    "tier_discount_pct",
    "final_total",
    "remaining_points",
]

CASE = dict(loyalty_points=4250, tier="Gold", order_total=450.0,
            product_category="standard")


def call_tool(**kwargs):
    """Call the @tool-decorated function, unwrapping the decorator if needed."""
    fn = calculate_loyalty_discount
    try:
        return fn(**kwargs)
    except TypeError:
        for attr in ("_func", "func", "original_function", "__wrapped__"):
            inner = getattr(fn, attr, None)
            if callable(inner):
                return inner(**kwargs)
        raise


def parse(raw):
    """The tool returns a JSON string; the sandbox path wraps it one level."""
    try:
        data = json.loads(raw)
    except Exception:
        return {"_unparsed": raw}
    # Sandbox results arrive as {"content":[{"text":"{...}"}]} or similar.
    if isinstance(data, dict) and "points_redeemed" not in data:
        for key in ("content", "structuredContent", "result"):
            blob = data.get(key)
            if isinstance(blob, list) and blob:
                inner = blob[0].get("text") if isinstance(blob[0], dict) else None
                if inner:
                    try:
                        return json.loads(inner)
                    except Exception:
                        pass
            if isinstance(blob, dict):
                return blob
    return data


def report(title, note, result):
    parsed = parse(result)
    missing = [f for f in REQUIRED_FIELDS if f not in parsed]
    print(f"\n{'=' * 68}\n{title}\n{note}\n{'=' * 68}")
    print(json.dumps(parsed, indent=2))
    print(f"\nRequired fields present: {sorted(set(REQUIRED_FIELDS) - set(missing))}")
    if missing:
        print(f"MISSING: {missing}")
    else:
        print("All required fields present.")
    return parsed, missing


def main():
    lines = [
        "# Loyalty Discount Tool — raw result payloads\n",
        f"Captured: {datetime.now().isoformat(timespec='seconds')}\n",
        "The AgentCore runtime logs record tool names but not tool results, so "
        "these payloads were captured by invoking the tool directly. Input for "
        f"both runs:\n\n```json\n{json.dumps(CASE, indent=2)}\n```\n",
    ]

    # ── 1. Success path: real Code Interpreter sandbox ───────────────────────
    ok_raw = call_tool(**CASE)
    ok, ok_missing = report(
        "SUCCESS PATH — AgentCore Code Interpreter",
        "Arithmetic executed in the sandbox via code_session().invoke('executeCode').",
        ok_raw,
    )
    lines.append("\n## Success path — AgentCore Code Interpreter\n")
    lines.append(f"```json\n{json.dumps(ok, indent=2)}\n```\n")

    # ── 2. Fallback path: force code_session to fail ─────────────────────────
    original = agent_module.code_session

    def broken(*a, **k):
        raise RuntimeError("Simulated Code Interpreter outage")

    agent_module.code_session = broken
    try:
        fb_raw = call_tool(**CASE)
    finally:
        agent_module.code_session = original

    fb, fb_missing = report(
        "FALLBACK PATH — Code Interpreter unavailable",
        "code_session patched to raise, so the tool's except branch runs.",
        fb_raw,
    )
    lines.append("\n## Fallback path — Code Interpreter unavailable\n")
    lines.append("`code_session` was patched to raise, forcing the tool's "
                 "fallback branch.\n")
    lines.append(f"```json\n{json.dumps(fb, indent=2)}\n```\n")

    # ── 3. Key-set comparison ────────────────────────────────────────────────
    shared = sorted(set(ok) & set(fb))
    only_ok = sorted(set(ok) - set(fb))
    only_fb = sorted(set(fb) - set(ok))

    print(f"\n{'=' * 68}\nKEY SET COMPARISON\n{'=' * 68}")
    print(f"Required fields in BOTH paths: "
          f"{all(f in ok and f in fb for f in REQUIRED_FIELDS)}")
    print(f"Keys in both ({len(shared)}): {shared}")
    if only_ok:
        print(f"Success-only keys: {only_ok}")
    if only_fb:
        print(f"Fallback-only keys: {only_fb}")

    lines.append("\n## Key-set comparison\n")
    lines.append("| Required field | Success path | Fallback path |")
    lines.append("|---|---|---|")
    for f in REQUIRED_FIELDS:
        lines.append(f"| `{f}` | {'yes' if f in ok else 'NO'} "
                     f"| {'yes' if f in fb else 'NO'} |")
    lines.append(f"\nKeys present in both paths: "
                 f"{', '.join('`%s`' % k for k in shared)}\n")
    if only_fb:
        lines.append(f"Fallback adds: {', '.join('`%s`' % k for k in only_fb)} "
                     "(a `note` field labelling the degraded result).\n")

    REPORT.write_text("\n".join(lines))
    print(f"\nWrote {REPORT.name}")


if __name__ == "__main__":
    main()
