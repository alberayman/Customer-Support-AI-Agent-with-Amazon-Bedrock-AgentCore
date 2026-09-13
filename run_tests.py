#!/usr/bin/env python3
"""
run_tests.py — run the six rubric scenarios and capture the evidence.

Invokes the deployed runtime for each scenario, prints the full prompt and
full reply to the terminal, and writes test-transcript.md.

It exists to enforce three things that are easy to forget by hand:
  * every call passes customer_id (so memory is keyed to a real actor, not
    the shared default — see E9)
  * every scenario gets a FRESH session id (the CLI otherwise reuses a
    cached session and you debug a stale conversation — see E10)
  * the memory test deliberately uses TWO sessions for the SAME customer,
    with a wait between them for asynchronous extraction

Usage:
  uv run python run_tests.py            # all six
  uv run python run_tests.py 5          # just scenario 5
  uv run python run_tests.py 1 2 3      # a subset
  uv run python run_tests.py | tee test-run-output.txt
"""

import json
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

CUSTOMER = "CUST-902"          # Jane Smith, Gold tier, 4250 points
TRANSCRIPT = Path(__file__).parent / "test-transcript.md"
MEMORY_WAIT_SECONDS = 180       # extraction is async; 30s is the floor, 90 is safe

SCENARIOS = {
    1: ("Order Tracking",
        "Rubric: Gateway tool invocation returns a well-formed response.",
        [("What is the status of order ORD-001?", None)]),

    2: ("Refund Processing",
        "Rubric: second Gateway tool, Lambda target.",
        [("I want to return my Kindle Paperwhite from order ORD-002. "
          "It arrived with a cracked screen. Please process the refund.", None)]),

    3: ("Knowledge Base (RAG)",
        "Rubric: search_knowledge_base calls the Retrieve API and grounds the answer.",
        [("What are the benefits of the Platinum loyalty tier?", None)]),

    4: ("Long-Term Memory (two sessions)",
        "Rubric: cross-session recall — two separate sessions, same customer.",
        [("Hi, I am Jane. I prefer concise responses.", "A"),
         ("__WAIT__", None),
         ("Do you remember my name and my communication preference?", "B")]),

    5: ("Loyalty Discount (Code Interpreter)",
        "Rubric: sandboxed arithmetic, structured breakdown.",
        [("I am a Gold member with 4250 points. My order total is $450 for "
          "standard items. Calculate my discount and final price.", None)]),

    6: ("Browser Tool",
        "Rubric: agent retrieves content from a live web page.",
        [("Go to https://www.udacity.com and tell me the page title.", None)]),

    7: ("Gateway Failure Handling (controlled failure)",
        "Rubric: a failing tool produces a clear message naming the failed "
        "operation and a next step, and the agent stays responsive.",
        [("What is the status of order ORD-999?", None),
         ("Thanks. While I have you — what is the return window for "
          "standard items?", None)]),
}


def invoke(prompt, session_id, actor):
    payload = json.dumps({
        "prompt": prompt,
        "customer_id": actor,
        "session_id": session_id,
    })
    cmd = ["uv", "run", "agentcore", "invoke", "--session-id", session_id, payload]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    elapsed = time.time() - started
    out = (proc.stdout or "") + (proc.stderr or "")
    return out.strip(), elapsed


def run(num, lines):
    title, rubric, turns = SCENARIOS[num]
    header = f"\n{'=' * 72}\nTEST {num} — {title}\n{rubric}\n{'=' * 72}"
    print(header, flush=True)
    lines.append(f"\n## Test {num} — {title}\n\n_{rubric}_\n")

    # Each scenario gets its own memory actor so no test inherits another's
    # memories. Test 4's two sessions still share this scenario's actor.
    actor = f"{CUSTOMER}-T{num}"
    session_ids = {}
    for prompt, tag in turns:
        if prompt == "__WAIT__":
            print(f"\n  ... waiting {MEMORY_WAIT_SECONDS}s for memory extraction ...\n",
                  flush=True)
            lines.append(f"\n*Waited {MEMORY_WAIT_SECONDS}s between sessions "
                         f"for asynchronous memory extraction.*\n")
            time.sleep(MEMORY_WAIT_SECONDS)
            continue

        if tag not in session_ids:
            session_ids[tag] = str(uuid.uuid4())
        sid = session_ids[tag]

        label = f"session {tag}" if tag else "session"
        print(f"\n[{label}] {sid}")
        print(f"customer_id: {actor}")
        print(f"PROMPT: {prompt}\n", flush=True)

        reply, elapsed = invoke(prompt, sid, actor)
        print(f"REPLY ({elapsed:.1f}s):\n{reply}", flush=True)

        lines.append(f"**{label}:** `{sid}`  \n**customer_id:** `{actor}`\n")
        lines.append(f"**Prompt**\n\n```\n{prompt}\n```\n")
        lines.append(f"**Response** ({elapsed:.1f}s)\n\n```\n{reply}\n```\n")


def main():
    wanted = [int(a) for a in sys.argv[1:]] or sorted(SCENARIOS)
    bad = [n for n in wanted if n not in SCENARIOS]
    if bad:
        sys.exit(f"Unknown scenario(s): {bad}. Valid: {sorted(SCENARIOS)}")

    lines = [
        "# Customer Support Agent — Test Transcript\n",
        f"Run: {datetime.now().isoformat(timespec='seconds')}  ",
        f"Customer: `{CUSTOMER}`  ",
        "Agent: `customer_support_agent` on Amazon Bedrock AgentCore Runtime\n",
        "\nEvery scenario uses a fresh session id. Test 4 deliberately uses two "
        "separate sessions for the same customer to demonstrate cross-session recall.\n",
    ]

    for n in wanted:
        try:
            run(n, lines)
        except subprocess.TimeoutExpired:
            msg = f"TEST {n} TIMED OUT after 600s"
            print(msg); lines.append(f"\n> {msg}\n")
        except Exception as e:
            msg = f"TEST {n} FAILED: {type(e).__name__}: {e}"
            print(msg); lines.append(f"\n> {msg}\n")

    TRANSCRIPT.write_text("\n".join(lines))
    print(f"\n{'=' * 72}\nWrote {TRANSCRIPT.name}\n{'=' * 72}")


if __name__ == "__main__":
    main()
