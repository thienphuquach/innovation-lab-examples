from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context

import payment
from chat_proto import chat_proto, store
from payment import payment_proto

# CMS publishes NADAC on the first Monday on or after the 15th of each month.
# Checking daily is nearly free because the refresh compares the metastore's
# `modified` timestamp first and skips the download when nothing has changed.
REFRESH_INTERVAL_SECONDS = int(os.getenv("NADAC_REFRESH_SECONDS", str(24 * 3600)))

agent = Agent(
    name=os.getenv("AGENT_NAME", "pill-price-agent"),
    seed=os.environ.get("AGENT_SEED", "pill-price-agent-dev-seed"),
    port=int(os.getenv("AGENT_PORT", "8091")),
    mailbox=True,
    publish_agent_details=True,
)

# Like the shipping-label example, each fresh `python agent.py` starts as a new
# demo instance rather than resuming a half-finished conversation from a
# previous run. Set to "false" to test persistence deliberately.
_RESET_STORAGE_ON_START = (os.getenv("RESET_STORAGE_ON_START", "true").strip().lower()) not in {
    "0",
    "false",
    "no",
}


def next_cms_release(today: date | None = None) -> date:
    """The next first-Monday-on-or-after-the-15th, for logging the cadence."""
    today = today or datetime.now(UTC).date()
    candidate = today.replace(day=15)
    if candidate <= today:
        month = candidate.month % 12 + 1
        year = candidate.year + (1 if month == 1 else 0)
        candidate = date(year, month, 15)
    return candidate + timedelta(days=(7 - candidate.weekday()) % 7)


@agent.on_event("startup")
async def startup(ctx: Context) -> None:
    payment.assert_stripe_test_keys()
    if _RESET_STORAGE_ON_START:
        ctx.storage.clear()
        ctx.logger.info("[agent] RESET_STORAGE_ON_START=true - wiped session state.")

    ctx.logger.info(f"[agent] {agent.name} | {agent.address}")
    ctx.logger.info("[agent] TEST MODE only - Stripe test keys. No real money moves.")
    port = os.getenv("AGENT_PORT", "8091")
    ctx.logger.info(
        f"[agent] Inspector: https://agentverse.ai/inspect/"
        f"?uri=http://127.0.0.1:{port}&address={agent.address}"
    )

    cache = store()
    if cache.row_count() == 0:
        ctx.logger.info("[nadac] cache empty - loading the current CMS release now.")
        await _refresh(ctx)
    else:
        ctx.logger.info(
            f"[nadac] cache ready: {cache.row_count():,} rows from release "
            f"{cache.loaded_release()}. Next CMS release ~{next_cms_release()}."
        )


@agent.on_interval(period=REFRESH_INTERVAL_SECONDS)
async def scheduled_refresh(ctx: Context) -> None:
    await _refresh(ctx)


async def _refresh(ctx: Context) -> None:
    """Resolve the current distribution and reload the cache if it has moved.

    The CSV filename embeds a release date that rotates on CMS's schedule, so it
    is always read from the metastore rather than assembled from a template.
    """
    cache = store()
    try:
        reloaded, release = await asyncio.to_thread(cache.refresh)
    except Exception as exc:  # noqa: BLE001 - a failed refresh must not kill the agent
        ctx.logger.error(f"[nadac] refresh failed, serving the existing cache: {exc}")
        return
    if reloaded:
        ctx.logger.info(
            f"[nadac] loaded {cache.row_count():,} rows from {release.download_url} "
            f"(modified {release.modified})"
        )
    else:
        ctx.logger.info(f"[nadac] already current at release {release.modified}")


agent.include(chat_proto, publish_manifest=True)
agent.include(payment_proto, publish_manifest=True)


if __name__ == "__main__":
    payment.assert_stripe_test_keys()
    agent.run()
