# Customer Support AI Agent — Amazon Bedrock AgentCore + Strands SDK

A production-shaped customer support agent deployed to Amazon Bedrock AgentCore
Runtime. It answers order and refund questions by calling Lambda-backed tools
through an AgentCore Gateway over MCP, grounds policy answers in a Bedrock
Knowledge Base, remembers customers across sessions, runs pricing arithmetic in a
sandboxed code interpreter, and can read live web pages.

Built as the capstone for the Udacity *Future AWS Agent Engineer* program. The
infrastructure is scripted rather than clicked, because most of the failure modes
in this stack are configuration, not code.

---

## It looks like this

```bash
$ agentcore invoke '{"prompt": "I am a Gold member with 4250 points. My order total is $450 for standard items. Calculate my discount and final price.", "customer_id": "CUST-901"}'
```

> **Order total:** $450.00
> **Tier discount (10%):** $41.00
> **Points redeemed (4,000):** $40.00
> **Final price:** **$369.00**
> **Points earned:** 369 · **Remaining:** 250

The arithmetic is not the model's. It runs in the AgentCore Code Interpreter and
returns a structured payload the model is instructed to quote verbatim:

```json
{
  "order_total": 450.0, "tier": "Gold",
  "tier_discount_pct": 10.0, "tier_discount": 41.0,
  "points_redeemed": 4000, "points_value_usd": 40.0,
  "final_total": 369.0, "total_savings": 81.0,
  "points_earned": 369, "remaining_points": 250
}
```

Full evidence: [`test-transcript.md`](test-transcript.md) (seven scenarios),
[`gateway-tool-responses.md`](gateway-tool-responses.md) (raw Gateway payloads),
[`discount-tool-result.md`](discount-tool-result.md) (sandbox vs fallback).

---

## Architecture

```
  ┌─ AGENT ───────────────────────────────────────────┐
  │  main.py on AgentCore Runtime (Direct Code Deploy) │
  │  Strands Agent · Nova 2 Lite                       │
  │  + Memory hook  + Code Interpreter  + Browser      │
  └───────────────┬───────────────────────────────────┘
                  │ MCPClient, connected per request
  ┌───────────────▼── TOOL EXPOSURE ──────────────────┐
  │  AgentCore Gateway (MCP)                          │
  │  target A → REST API    target B → refund Lambda  │
  └───────────────┬───────────────────────────────────┘
  ┌───────────────▼── BACKEND ────────────────────────┐
  │  API Gateway (3 GET ops) → order-tracker Lambda   │
  │  refund-processor Lambda                          │
  └───────────────────────────────────────────────────┘
  ┌─ KNOWLEDGE ───────────────────────────────────────┐
  │  S3 → Bedrock Knowledge Base → S3 Vectors         │
  └───────────────────────────────────────────────────┘
```

Six MCP tools are exposed by the Gateway: `get_order`, `get_customer_orders`,
`get_customer`, `initiate_refund`, `check_refund_status`, `get_return_label`.

---

## Repository

| File | Purpose |
|---|---|
| `main.py` | The agent. Entrypoint, memory hook, KB search tool, loyalty discount tool. |
| `phase2_setup.py` | Builds IAM roles, Lambdas, REST API, Gateway, targets and Memory in dependency order. Idempotent. Also attaches the runtime execution policy. |
| `test_gateway.py` | Opens an MCP session straight to the Gateway — no model, no agent. The fastest way to tell whether a failure is above or below the Gateway. |
| `gateway_tool_evidence.py` | Calls all six Gateway tools over a direct MCP session and records the raw payloads. Proves the integration independently of the model. |
| `discount_tool_evidence.py` | Captures the discount tool's result from both the Code Interpreter and the fallback path, and compares key sets. |
| `run_tests.py` | Runs the seven scenarios and writes `test-transcript.md`. Gives each scenario its own memory actor. |
| `lambda/` | Backend handlers and the refund tool schema. |
| `product_catalog.txt` | Knowledge Base source data. |

Seven scenarios cover order tracking, refunds, RAG, cross-session memory,
sandboxed calculation, live browsing, and a controlled Gateway failure.

---

## Running it

```bash
uv sync
aws configure                       # region us-east-1

uv run python phase2_setup.py       # build everything except the KB
uv run python test_gateway.py       # gate: 6 tools, real data

# create the Knowledge Base in the console, then:
#   set GATEWAY_URL / KB_ID / REGION / MEMORY_ID in main.py

uv run agentcore configure --entrypoint main.py --name customer_support_agent
uv run agentcore deploy
uv run python phase2_setup.py --fix-runtime-role <execution-role-name>

uv run python run_tests.py
uv run python gateway_tool_evidence.py
uv run python discount_tool_evidence.py
```

The Knowledge Base is created last and deleted first — it is the only resource
with meaningful standing cost.

---

## Four things worth reading the code for

**The memory hook filters on content, not just role.** Strands delivers tool
results with `role: "user"`, so a naive role check re-fires the hook on every
tool return and corrupts the tool-use loop. The hook requires a text block and
rejects any message carrying a `toolResult`.

**The Gateway client stays open for the whole invocation.** The agent is created
*and* invoked while the MCP session is held open. Creating it inside and invoking
outside yields tools whose connection has already closed.

**Gateway failures are handled in three distinct classes.** Connection failure,
tool-discovery failure, and a Gateway that advertises zero tools — the last being
the dangerous one, because a FAILED target returns an empty list rather than
raising. Each names the failed operation, confirms nothing was changed, and
suggests a next step, while the agent degrades to its local tools instead of
failing the turn.

**`actor_id` never falls back to a shared default.** The starter's
`"default_customer"` pools every caller's long-term memory into one namespace.
That is a data-isolation bug: one customer's history surfaces in another's
conversation. This version falls back to a per-session anonymous actor.

---

## Findings from building it

- **Long-term memory extraction is asynchronous.** A cross-session recall test run
  90 seconds after the first session failed; the record was written ~10 seconds
  later. Test waits are 180 seconds.
- **Memory can launder an error into a fact.** A mis-narrated discount was saved to
  the customer's namespace and replayed as truth in a later run. Isolating each
  test scenario to its own actor made the tests deterministic.
- **A tool result and the model's prose can diverge.** The sandbox returned a $41
  tier discount; the model's summary said $45. The system prompt now requires the
  returned fields to be quoted verbatim.
- **A conversation transcript is not tool evidence.** The agent's prose can be
  correct while saying nothing about what the tool actually returned — and the
  runtime logs record tool *names*, not results. Verifying an integration means
  capturing the payload at the boundary, which is what the two evidence scripts do.
- **The Gateway maps any non-2xx to a generic error.** A legitimate 404 looks
  identical to a permissions failure, which sends you hunting through IAM for a
  problem that was never there.

---

## Not production-ready, deliberately

The Gateway uses the `NONE` authorizer and the runtime policy grants its actions on
`Resource: "*"`. Both are sandbox expedients. Production would need a JWT
authorizer with a real issuer, per-resource ARN scoping, and a value threshold on
the refund tool so no agent approves an unbounded return without a human.
