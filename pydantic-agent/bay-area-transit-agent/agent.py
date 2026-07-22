"""uAgent entry point for the Bay Area Transit & Fare Concierge.

Wires the ASI:One Agent Chat Protocol (the trip-planning state machine) and the
Agent Payment Protocol (the Stripe unlock gate) into a single uAgent. On startup
it refuses to run unless the Stripe keys are test keys - this agent never touches
a live Stripe account.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context  # noqa: E402

import payment  # noqa: E402
from chat_proto import chat_proto  # noqa: E402
from payment import payment_proto  # noqa: E402

agent = Agent(
    name=os.getenv("AGENT_NAME", "bay-area-transit-agent"),
    seed=os.environ.get("AGENT_SEED", "bay-area-transit-agent-dev-seed"),
    port=int(os.getenv("AGENT_PORT", "8090")),
    mailbox=True,
    publish_agent_details=True,
    description=(
        "Plan a multi-modal Bay Area transit trip and see the cheapest way to pay "
        "for it (Clipper vs. cash vs. day pass). A one-time $5 Stripe unlock is "
        "required before any trip planning - test mode only, no real charge."
    ),
    readme_path=os.path.join(os.path.dirname(__file__), "README.md"),
)

# ``ctx.storage`` persists to a JSON file across restarts. For an example agent,
# each fresh ``python agent.py`` run should behave like a brand-new demo (pay
# again from message one). Default true; set to "false" to keep state across
# restarts if you want to test persistence deliberately.
_RESET_STORAGE_ON_START = (os.getenv("RESET_STORAGE_ON_START", "true").strip().lower()) not in {
    "0",
    "false",
    "no",
}


@agent.on_event("startup")
async def startup(ctx: Context) -> None:
    payment.assert_stripe_test_keys()
    if _RESET_STORAGE_ON_START:
        ctx.storage.clear()
        ctx.logger.info(
            "[agent] RESET_STORAGE_ON_START=true - wiped session state; "
            "this run starts as a brand-new demo (pay again from message one)."
        )
    ctx.logger.info(f"[agent] {agent.name} | {agent.address}")
    ctx.logger.info("[agent] TEST MODE only - Stripe test keys. No real money moves.")
    port = os.getenv("AGENT_PORT", "8090")
    ctx.logger.info(
        f"[agent] Inspector: https://agentverse.ai/inspect/"
        f"?uri=http://127.0.0.1:{port}&address={agent.address}"
    )


agent.include(chat_proto, publish_manifest=True)
agent.include(payment_proto, publish_manifest=True)


if __name__ == "__main__":
    payment.assert_stripe_test_keys()  # fail fast before the server starts
    agent.run()
