"""
Customer Support AI Agent — Completed Implementation
====================================================

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore configure --entrypoint main.py --name csai_agent
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
app = BedrockAgentCoreApp()

# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────
GATEWAY_URL = "https://customersupportgateway-k1n2iynlsq.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "VDKAVHWW4F"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-6ZPBfN8QiH"


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
model_id = "global.amazon.nova-2-lite-v1:0"

model = BedrockModel(model_id=model_id)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    return {s["type"]: s["namespaces"][0] for s in strategies}


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)

    # ---------------------------------------------------------------- retrieve
    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return

        last = messages[-1]
        # Strands delivers tool RESULTS with role "user" as well, so a plain
        # role check is not enough — it would re-fire on every tool return and
        # corrupt the tool-use loop. Require a text block AND reject any message
        # carrying a toolResult / toolUse block.
        if last.get("role") != "user":
            return
        content = last.get("content") or []
        if not content or "text" not in content[0]:
            return
        if any("toolResult" in blk or "toolUse" in blk for blk in content):
            return

        user_query = content[0]["text"]

        try:
            all_context = []
            for strategy_type, namespace in self.namespaces.items():
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace.format(actorId=self.actor_id),
                    query=user_query,
                    top_k=5,
                )
                for memory in memories or []:
                    if isinstance(memory, dict):
                        text = (memory.get("content") or {}).get("text", "").strip()
                        if text:
                            all_context.append(f"[{strategy_type}] {text}")

            if all_context:
                joined = "\n".join(all_context)
                messages[-1]["content"][0]["text"] = (
                    f"Customer Context:\n{joined}\n\n{user_query}"
                )
                logger.info("Injected %d memories", len(all_context))
        except Exception as e:
            logger.warning("Memory retrieval failed: %s", e)

    # -------------------------------------------------------------------- save
    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        try:
            messages = event.agent.messages
            customer_query = None
            agent_response = None

            for msg in reversed(messages):
                content = msg.get("content") or []
                if not content or "text" not in content[0]:
                    continue
                if any("toolResult" in blk or "toolUse" in blk for blk in content):
                    continue
                if agent_response is None and msg.get("role") == "assistant":
                    agent_response = content[0]["text"]
                elif customer_query is None and msg.get("role") == "user":
                    customer_query = content[0]["text"]
                if customer_query and agent_response:
                    break

            if customer_query and agent_response:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (customer_query, "USER"),
                        (agent_response, "ASSISTANT"),
                    ],
                )
                logger.info("Saved interaction to memory")
        except Exception as e:
            logger.warning("Memory save failed: %s", e)

    # ---------------------------------------------------------------- register
    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID or KB_ID.startswith("<"):
        return "Knowledge base not configured."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
        results = resp.get("retrievalResults", [])
        if not results:
            return f"No knowledge base results found for: {query}"

        chunks = [r["content"]["text"] for r in results if r.get("content", {}).get("text")]
        return "\n---\n".join(chunks)
    except Exception as e:
        logger.error("Knowledge base search failed: %s", e)
        return f"Knowledge base search failed: {e}"


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
import json

loyalty_points   = {int(loyalty_points)}
tier             = "{tier}".capitalize()
order_total      = float({float(order_total)})
product_category = "{product_category}".lower()

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

# --- Redeem points: 100 points = $1, minimum 500, floor to nearest 500,
#     capped at 50% of the order total.
max_points_by_cap = int((order_total * 0.50) * 100)
usable = min(loyalty_points, max_points_by_cap)
points_redeemed = (usable // 500) * 500
if points_redeemed < 500:
    points_redeemed = 0
points_value = round(points_redeemed / 100.0, 2)

# --- Tier discount applies to the subtotal AFTER points are applied.
subtotal_after_points = round(order_total - points_value, 2)
tier_rate     = tier_rates.get(tier, 0.00)
tier_discount = round(subtotal_after_points * tier_rate, 2)

final_total      = round(subtotal_after_points - tier_discount, 2)
total_savings    = round(points_value + tier_discount, 2)
points_earned    = int(final_total * earn_rates.get(product_category, 1))
remaining_points = loyalty_points - points_redeemed

result = {{
    "order_total": round(order_total, 2),
    "tier": tier,
    "tier_discount_rate": tier_rate,
    "points_redeemed": points_redeemed,
    "points_value_usd": points_value,
    "tier_discount": tier_discount,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}}
print(json.dumps(result))
"""

    try:
        with code_session(REGION) as cs:
            response = cs.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": True},
            )
            for event in response["stream"]:
                return json.dumps(event["result"])
        return "Code Interpreter returned no result."

    except Exception as e:
        # Fallback: tier discount only, no sandbox.
        logger.warning("Code Interpreter unavailable, using fallback: %s", e)
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_rate = tier_rates.get(str(tier).capitalize(), 0.00)
        tier_discount = round(float(order_total) * tier_rate, 2)
        final_total = round(float(order_total) - tier_discount, 2)
        return json.dumps({
            "order_total": round(float(order_total), 2),
            "tier": str(tier).capitalize(),
            "tier_discount_rate": tier_rate,
            "points_redeemed": 0,
            "points_value_usd": 0.0,
            "tier_discount": tier_discount,
            "final_total": final_total,
            "total_savings": tier_discount,
            "points_earned": int(final_total),
            "remaining_points": int(loyalty_points),
            "note": f"Fallback calculation (tier discount only): {e}",
        })


SYSTEM_PROMPT = """You are a helpful Amazon customer support agent.

Tool usage rules:
- Order status, tracking, shipping questions -> use the Gateway order tracking tools.
- Refunds and returns -> first look up the order with the order tracking tool
  to get its total, then call the refund tool passing that total as the amount.
  Never submit a refund without an amount.
- Product specs, return/warranty policy, loyalty program rules -> use search_knowledge_base.
- Any discount, points or price arithmetic -> use calculate_loyalty_discount.
  Report the returned fields VERBATIM: final_total, tier_discount, points_redeemed,
  points_earned, remaining_points. Never recompute, re-derive or round them, and
  never state a total that differs from the tool's final_total.
- Live web pages -> use the browser tool. It is STATEFUL and must be used in
  this exact order, always with session_name "support_browser":
    1. action type "init_session" with session_name "support_browser"
    2. action type "navigate" with the full url and the same session_name
    3. action type "get_text" with the same session_name to read the page
    4. action type "close" with the same session_name when finished
  Never call navigate before init_session.

Always ground your answer in tool output. If a tool fails, say so plainly.
Be concise, friendly, and professional. Remember details the customer shares about
themselves and use them in later turns."""


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    user_input = payload.get("prompt", "Hello")
    session_id = payload.get("session_id") or str(uuid.uuid4())

    # Long-term memory is keyed to actor_id. A shared literal default
    # ("default_customer") pools every caller's memories into one namespace —
    # a data-isolation bug, and the reason stale test context leaks into new
    # sessions. Fall back to a per-session anonymous actor instead.
    actor_id = payload.get("customer_id")
    if not actor_id:
        actor_id = f"anonymous-{session_id}"
        logger.warning("No customer_id in payload; using isolated actor %s", actor_id)

    try:
        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))

        with mcp_client:
            gateway_tools = mcp_client.list_tools_sync()
            tools.extend(gateway_tools)

            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                system_prompt=SYSTEM_PROMPT,
            )

            response = await agent.invoke_async(user_input)

        return response.message["content"][0]["text"]

    except Exception as e:
        logger.error("Agent invocation failed: %s", e)
        return f"Sorry, I hit an error handling that request: {e}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()