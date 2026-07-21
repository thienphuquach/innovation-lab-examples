# Bay Area Transit & Fare Concierge

A payment-gated Fetch.ai uAgent, reachable through ASI:One, that plans multi-modal
San Francisco Bay Area trips (walk + bus + train) and computes the cheapest way to
pay for each one — Clipper vs. cash vs. day pass — from live schedule and fare data.

Everything happens inside ASI:One chat: the agent talks through **interactive
cards** and plain **free-text chat**, interchangeably. A single one-time Stripe
charge at first contact unlocks the agent for the rest of that chat window; after
that there is no metered billing.

This agent is a reference fusion of two Innovation Lab patterns: the
[`stripe-horoscope-agent`](../../stripe-horoscope-agent) payment gate and the
card-driven flow of [`news-card-agent`](../../news-card-agent). See
[`research-notes.md`](./research-notes.md) for the wire-protocol details and every
place the live docs differed from the original brief.

> **Test mode only.** The agent refuses to start unless `STRIPE_SECRET_KEY` /
> `STRIPE_PUBLISHABLE_KEY` are Stripe **test** keys. No real money ever moves.

## What it does (the flow)

| Stage | What happens | Surface |
|---|---|---|
| 0 — Payment gate | Any message from an unpaid sender gets a Stripe checkout and nothing else | Payment Protocol (native Stripe UI) |
| 1 — Verification | `CommitPayment` → verify `payment_status == "paid"` with retries → unlock | Payment Protocol (`seller`) |
| 2 — Intake | Collect origin / destination / depart time / priority | `form` card **or** free text |
| 2.5 — Geocoding | Resolve text to coordinates (Transitland stops → Nominatim fallback) | `carousel` for disambiguation |
| 3 — Routes | Transitland itineraries with Fastest / Fewest-transfers / Long-wait badges | `carousel` |
| 4 — Route + fares | Fare-by-payment-method + live 511 GTFS-RT alerts for the chosen route | `detail` card |
| 5 — Review & confirm | A Pydantic AI `requires_approval=True` deferred tool gates the finalize step | `review` card |
| 6 — Repeat use | A finished session keeps the paid unlock and re-enters intake on the next message | — |
| Cross-cutting | An interrupt classifier lets a user type past any card at any stage (override / escalate / side-question / clarify) | — |

## Prerequisites

- Python 3.10+
- API credentials (all free — see below)

## Getting the API credentials

| Variable | Where to get it |
|---|---|
| `ASI_ONE_API_KEY` | https://asi1.ai/dashboard/api-keys |
| `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY` | https://dashboard.stripe.com/test/apikeys (the **test** keys) |
| `Transitland_APIs` | Free REST key: https://www.transit.land/documentation (used for stop/station geocoding) |
| `Transitland_Routing_API` | A **separate** subscription on the same account — order the "Transitland Routing API - Beta" plan (1,000 free queries/month) |
| `sf_bay_511_api` | Free self-serve token: https://511.org/open-data/token (covers all Bay Area operators + Fares v2, `operator_id=RG`) |

Nominatim (the geocoding fallback) needs no key; the client sends a descriptive
`User-Agent` and rate-limits itself per the OSM usage policy.

## Local setup

```bash
cd innovation-lab-examples/pydantic-agent/bay-area-transit-agent

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and fill in the credentials above
```

## Run it

```bash
python agent.py
```

On startup the agent prints its address and an Agentverse **Inspector** link. Open
that link, connect the agent to Agentverse via its mailbox, and chat with it
through ASI:One. Every fresh `python agent.py` starts as a brand-new demo (pay
again from message one) unless you set `RESET_STORAGE_ON_START=false`.

## Testing the payment flow

1. Send the agent any message in ASI:One — you'll get the Stripe checkout.
2. Pay with a Stripe **test card**: `4242 4242 4242 4242`, any future expiry, any
   CVC, any ZIP. (More test cards: https://docs.stripe.com/testing.)
3. On success the agent unlocks and immediately sends the trip-intake form. From
   there, either fill the form or just type a trip like
   *"Berkeley to the Mission at 6pm, cheapest."*

If the automatic `CommitPayment` is slow to arrive, typing `paid` nudges the agent
to re-check Stripe.

## Running the tests

```bash
pytest -q
```

All tests run fully offline — no ASI:One, Stripe, 511, Transitland, or Nominatim
calls. Pydantic AI agents are driven by `TestModel`/`FunctionModel`, network
clients are monkeypatched, and `ctx` is an in-memory fake. Coverage spans the
payment gate, intake + geocoding (resolved / ambiguous / not-found), the routing
carousel, the fare engine (Clipper transfer discounts, day passes, estimated
zone fares), the `requires_approval` finalize gate (defer / approve / deny), and
the interrupt classifier (fast-path + all four intents + graceful degradation).

## Project layout

```
agent.py            uAgent entry point (chat + payment protocols, test-key guard)
chat_proto.py       Session/stage dispatcher + interrupt classifier wiring
payment.py          Agent Payment Protocol seller role + Stripe checkout/verify
ai.py               Pydantic AI: trip extraction, intent classifier, finalize gate
cards.py            Interactive-card builders + shared send/parse helpers
fares.py            GTFS-Fares v2 fare engine (per payment method)
models.py           Trip models + time/format helpers
session_state.py    Per-sender session schema in ctx.storage
clients/
  geocode.py        Nominatim geocoder (rate-limited, Bay-Area biased)
  transitland.py    Transitland routing client (cached)
  five11.py         511 regional GTFS + Fares v2 + GTFS-RT alerts
research-notes.md   Mandatory-research findings + doc discrepancies
tests/              Offline test suite
```
