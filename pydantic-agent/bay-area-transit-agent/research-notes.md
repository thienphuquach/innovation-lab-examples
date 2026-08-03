# Research Notes — Bay Area Transit & Fare Concierge

Written **before** any implementation, per the mandatory research phase. Everything
below is summarized in my own words from the live docs fetched on 2026-07-21 plus a
close reading of the two reference agents that already live in this repo
(`../shipping-label-agent/` and `../../news-card-agent/`). Where the live docs
contradict the project brief, the docs win and the discrepancy is flagged in
**§7 Discrepancies**.

Sources read in full:
- Fetch.ai Innovation Lab: interactive-cards, card-playground, agent-chat-protocol,
  agent-payment-protocol, stripe-horoscope-payment-protocol, uagent-creation.
- Pydantic AI (`pydantic.dev/docs/ai/`): message-history, deferred-tools, models/openai, agent.
- Reference source, read directly (not the docs summaries): `shipping-label-agent/`
  (`payment.py`, `chat_proto.py`, `pydantic_agent.py`, `session_state.py`, `agent.py`)
  and `news-card-agent/` (`cards.py`, `chat_proto.py`, `agent.py`).
- Data APIs: Transitland Routing API (OTP-compat), Transitland REST v2, 511.org Regional,
  GTFS-Fares v2 intro, Stripe testing.

---

## 1. Card wire protocol — metadata keys

A card is **not** a separate message type. It is a normal Agent Chat Protocol
`ChatMessage` whose `content` list holds:
- one (or more) `TextContent` block(s) — the narration bubble shown above the drawer, and
- exactly **one** `MetadataContent` block — the card declaration.

`MetadataContent.metadata` is a **flat `dict[str, str]`** (wire type is string→string;
nested structures must be `json.dumps`'d first). Keys:

**Required (every card):**
| Key | Value |
|---|---|
| `card_protocol_version` | the literal string `"1"` — **omitting this silently drops the card** (falls back to plain text, zero error surfaced) |
| `requires_card_interaction` | `"true"` |
| `card_kind` | one of `"carousel"`, `"detail"`, `"form"`, `"review"`, `"custom"` |
| `card_payload` | the payload dict **JSON-stringified** (`json.dumps(...)`), never a nested dict |

**Optional:**
| Key | Value | Effect |
|---|---|---|
| `is_terminal` | `"true"` | informational-only card; planner won't wait for input. Do **not** set on a card that needs input or the drawer won't open. |
| `preferred_drawer_width_px` | integer string e.g. `"540"` | drawer width hint, clamped to `[320, 800]` |

**Validation limits:** `card_payload` ≤ 64 KB; custom element-tree ≤ 8 levels deep
(root = level 1); unknown `card_kind` rejected; payload not matching the declared
`card_kind`'s schema is rejected. On any validation failure ASI:One silently degrades
to showing just the `TextContent` — so **every narration must be self-contained**
enough to stand alone.

Both reference agents wrap this in one shared helper (`shipping-label-agent`'s `_wrap()`
+ `send_card()`, `news-card-agent`'s `_card_metadata()`). We will do the same: a single
`build_card_metadata()` that always sets `card_protocol_version="1"` so it can't be
forgotten anywhere (master edge-case #1).

### Response format (what comes back when a user interacts)
The selection returns as a follow-up `ChatMessage` whose `TextContent` is **either**:
1. **Selection JSON** (direct `@mention`) — the CTA's `selection` block + captured
   input values, serialized to a JSON string. Try `json.loads()` first.
2. **Natural-language prose** (planner-mediated) — the planner narrates the selection
   ("the user picked offer off_123"). Every identifier under `selection` is mentioned
   somewhere in the sentence; parse with regex/keywords as a fallback.

Rule enforced everywhere: **try `json.loads()` first, fall back to prose parsing.**
(This is master edge-case #2 and is exactly what `shipping-label-agent.parse_selection`
and `news-card-agent.parse_card_selection` do.)

---

## 2. The five card kinds and their payload shapes

### `carousel` — pick-one horizontally scrollable list
```json
{ "title": "...", "subtitle": "...",
  "items": [
    { "id": "off_123", "image": "https://...", "title": "...", "subtitle": "...",
      "badges": [{"label": "Direct", "variant": "info"}],
      "secondary_text": "USD 88.16",
      "primary_cta": {"label": "Select", "selection": {"offer_id": "off_123"}} }
  ] }
```
Selection = whatever is under `primary_cta.selection`. Badge `variant` ∈
`info|success|warning`. (We'll use `warning` for the long-wait badge in Stage 3.)

### `detail` — one item, summary rows, optional radio sub-options, CTAs
```json
{ "title": "...", "hero_image": "https://...",
  "summary_rows": [{"label": "Departure", "value": "21:50 · LHR"}],
  "sub_options": {"name": "cabin", "kind": "radio", "label": "Cabin class",
    "choices": [{"value": "economy", "label": "Economy", "secondary_text": "USD 88"}]},
  "ctas": [{"label": "Continue", "selection": {"action": "continue"}, "primary": true},
           {"label": "Back", "selection": {"action": "back"}}] }
```
Selection = chosen sub-option value **merged with** the clicked CTA's `selection`,
e.g. `{"cabin": "economy", "action": "continue"}`. This is exactly the Stage 4 shape
(fare radio = `sub_options`, Continue/Back = `ctas`).

### `form` — labeled inputs + one submit
```json
{ "title": "...",
  "fields": [
    {"name": "origin", "kind": "text", "label": "Origin", "required": true},
    {"name": "priority", "kind": "select", "label": "Priority",
      "options": [{"value": "fastest", "label": "Fastest"}]} ],
  "submit_cta": {"label": "Continue", "selection": {"action": "submit"}} }
```
Field kinds: `text|number|email|select|checkbox`; `select` needs a non-empty `options`.
Selection = every field value **merged with** `submit_cta.selection`.
Note (learned from `shipping-label-agent`): the `form` schema is exactly
`{title, fields, submit_cta}` — it has **no** top-level `subtitle` key (unlike carousel).
Any framing copy goes in the narration text, not the payload, or the card gets dropped.

### `review` — read-only summary + approve/reject
```json
{ "title": "...",
  "summary_rows": [{"label": "Total", "value": "USD 88.16"}],
  "approve_cta": {"label": "Confirm", "selection": {"action": "confirm"}, "primary": true},
  "reject_cta": {"label": "Cancel", "selection": {"action": "cancel"}} }
```
Selection = `approve_cta.selection` **or** `reject_cta.selection`. This is the Stage 5
gate and maps 1:1 onto Pydantic AI approve/deny (§5).

### `custom` — element tree (`card_kind: "custom"`, root `{"root": <node>}`)
Layout: `section{title?,subtitle?,children}`, `group{direction:row|column,gap?,children}`,
`divider`. Content: `text{value,style?:body|muted|emphasis}`, `heading{value,level:1|2|3}`,
`image{src,alt?,aspect_ratio?}`, `badge{label,variant?}`. Interactive:
`button{label,primary?,action:{selection}}`, `input{name,kind,label,required?,options?,placeholder?}`,
`list{items:[{children,action?:{selection}}]}`, `choice_grid{name,choices:[{value,label,image?}],multi?}`.
For custom cards the drawer tracks every `input`/`choice_grid` value by `name` and merges
them with the clicked button's `action.selection`. `news-card-agent` builds its list→detail
flow entirely from `section`/`list`/`image`/`group`/`heading`/`text`/`badge`/`button`.

**Card-kind choices for this project:** Stage 2 intake = `form`; Stage 2.5 disambiguation
= `carousel`; Stage 3 routes = `carousel`; Stage 4 route+fare = `detail` (with fare radio
as `sub_options`); Stage 5 confirm = `review`; terminal/error cards = `detail` or `custom`
with `is_terminal:"true"`. None of these need the custom element tree, so we stick to the
four predefined kinds except possibly a terminal info card.

---

## 3. Agent Chat Protocol carrier types

Imported from `uagents_core.contrib.protocols.chat`:
- `ChatMessage{timestamp: datetime, msg_id: UUID4, content: list[AgentContent]}`
- `ChatAcknowledgement{timestamp, acknowledged_msg_id: UUID4, metadata?: dict}`
- content union: `TextContent{type:"text",text}`, `MetadataContent{type:"metadata",metadata}`,
  `ResourceContent`, `StartSessionContent`, `EndSessionContent`, stream types.
- `chat_protocol_spec` — pass to `Protocol(spec=chat_protocol_spec)`, register
  `@chat_proto.on_message(ChatMessage)` / `(ChatAcknowledgement)`, then
  `agent.include(chat_proto, publish_manifest=True)`.

Rhythm: on **every** inbound `ChatMessage`, immediately send a `ChatAcknowledgement`
(both reference agents do this first thing), then process. Session identity: `sender` is
stable per user and can be **reused across windows**, so a new window must reset state.
`StartSessionContent` is the protocol's own "this message begins a new session" signal
(see `session_state.reset_on_new_window`) — a brand-new conversation carries it, so that's
what triggers the reset + re-charge. An earlier version compared `ctx.session` ids instead;
that fired spuriously on structured card-submission turns (`ctx.session` is not stable
across every turn), incorrectly re-charging an already-paid, mid-flow sender.

---

## 4. Payment protocol — message types, roles, Stripe wiring

Imported from `uagents_core.contrib.protocols.payment`. Models:
- `Funds{amount: str, currency: str, payment_method: str = "fet_direct"}`
- `RequestPayment{accepted_funds: list[Funds], recipient: str, deadline_seconds: int,
  reference?: str, description?: str, metadata?: dict[str, str|dict[str,str]]}`
- `CommitPayment{funds: Funds, recipient: str, transaction_id: str, reference?, description?, metadata?}`
- `RejectPayment{reason?: str}`
- `CancelPayment{transaction_id?, reason?}`
- `CompletePayment{transaction_id?}`

Spec/roles (`payment_protocol_spec`):
```
RequestPayment -> {CommitPayment, RejectPayment}
CommitPayment  -> {CompletePayment, CancelPayment}
roles: seller = {RequestPayment, CancelPayment, CompletePayment}
       buyer  = {CommitPayment, RejectPayment}
```
**We are the seller.** Instantiate `Protocol(spec=payment_protocol_spec, role="seller")`
(must pass `role` or you get locked-spec errors). ASI:One / the Agentverse UI acts as the
buyer: it renders the checkout, and sends `CommitPayment` back on its own once the user pays.

### Flow (seller side)
1. Seller sends `RequestPayment` (with `Funds(payment_method="stripe")` + Stripe details in
   `metadata["stripe"]`). **Send only `RequestPayment` — no text in the same handler call**,
   or ASI:One swallows the payment card and shows only the text (confirmed in
   `shipping-label-agent/payment.py` docstring).
2. Buyer/UI → `CommitPayment(funds.payment_method="stripe", transaction_id="<checkout_session_id>")`.
   Here `transaction_id` is the Stripe **Checkout Session ID** (`cs_test_...`).
3. Seller verifies with Stripe that `payment_status == "paid"`; on success sends
   `CompletePayment(transaction_id=...)` then delivers content; on failure `RejectPayment(reason=...)`.

### Stripe embedded checkout → `RequestPayment.metadata["stripe"]`
`stripe.checkout.Session.create(ui_mode="embedded", mode="payment",
payment_method_types=["card"], line_items=[{price_data:{currency, product_data, unit_amount}, quantity:1}],
return_url=..., redirect_on_completion="if_required")` returns `client_secret` + `id`.
Put a **string→string** dict under `metadata["stripe"]`:
```json
{"ui_mode": "embedded", "publishable_key": "pk_test_...", "client_secret": "cs_test_...",
 "checkout_session_id": "cs_test_...", "currency": "usd", "amount_cents": 100}
```
Verify later with `stripe.checkout.Session.retrieve(id).payment_status == "paid"`.

**Two `ui_mode` spellings seen:** the horoscope docs use `ui_mode="embedded"`; the in-repo
`shipping-label-agent/payment.py` uses `ui_mode="embedded_page"` and comments that this is
what ASI:One's native renderer expects. Since the strict requirement is "behave exactly
like shipping-label-agent's payment," **we mirror `shipping-label-agent` verbatim** —
`embedded_page`, `redirect_on_completion="if_required"`, `expires_at` clamped to Stripe's
30 min–24 h window, `metadata={"stripe": checkout, "service": ..., "purpose": ...}`. Flagged
in §7.

### Test-mode safety (mirrored from shipping-label-agent)
Refuse to start unless `STRIPE_SECRET_KEY` starts `sk_test_` and `STRIPE_PUBLISHABLE_KEY`
(if set) starts `pk_test_`. Test cards (Stripe): success `4242 4242 4242 4242`,
decline `4000 0000 0000 0002`, any future expiry / any CVC / any ZIP.

### How this maps to Stage 0/1 of the brief
- Stage 0: first message from an unpaid sender → build a fresh checkout session, store its
  id, send a bare `RequestPayment`. Every unpaid message (greeting, trip request, pricing
  question) gets the identical gate, but first re-asks Stripe about the stored session: if
  it already reads `paid` the sender is unlocked, and if it is still `open` that same
  session is re-sent rather than a second one being minted. Only an expired/absent session
  creates a new one.
- Card metadata: `metadata["stripe"]` mirrors `shipping-label-agent/payment.py` field for
  field, `ui_mode="embedded_page"` included. (Some other agents in the org map this to
  Agentverse's older `embedded` spelling; the merged shipping-label reference does not, and
  it is the contract this agent follows.)
- Delivery: `CommitPayment` does **not** reliably follow ASI:One's "Confirm Payment" tap, so
  the unlock cannot depend on it. An `on_interval` polls Stripe for outstanding checkouts.
  A tick's `Context` carries a fresh random `session`, and ASI:One routes chat by session,
  so the poller must re-stamp the sender's recorded chat session before replying or the
  unlock message is sent but never appears in the conversation.
- Ordering: a new chat window clears the poller's watch list (`clear_watch`) before minting
  a checkout, and every gate/poll path holds a per-sender `asyncio.Lock` while
  read-modify-writing `session:{sender}`. Without both, a previous window's paid checkout
  (or a poller unlock racing `request_payment`'s Stripe await) could push the trip form
  ahead of the payment card. The ASI:One line *"No completed checkout is on record yet…"*
  is **not** agent code — it is ASI:One's own pre-check copy when Confirm Payment finds no
  completed record on their side.
- Stage 1: `on_commit` (seller) → reject if `payment_method != "stripe"` or no
  `transaction_id`; else verify with Stripe (retry 2–3× with short backoff because the UI
  can `CommitPayment` a beat before Stripe settles); on paid set `paid=True/paid_at/stage`,
  send `CompletePayment`, then **immediately** send the Stage 2 form in the same turn.
  Idempotency: if already `paid`, ignore a duplicate `CommitPayment` (double-click), never
  re-grant. Prefer `msg.transaction_id` over the stored session id, since a restart with
  `RESET_STORAGE_ON_START` wipes the latter.

**This is a single unlock fee — one charge only.** Unlike `shipping-label-agent` (which
charges a second time for the label), this agent has exactly one `RequestPayment` per
session, at first contact. No per-query billing.

---

## 5. Pydantic AI — message history + deferred-tool approval

### Model wiring to ASI:One (confirmed in `shipping-label-agent/pydantic_agent.py`)
```python
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
OpenAIChatModel("asi1-mini", provider=OpenAIProvider(
    base_url="https://api.asi1.ai/v1", api_key=os.environ["ASI_ONE_API_KEY"]))
```
ASI:One is OpenAI-compatible, so this is the whole integration. Model configurable via
`ASI_ONE_MODEL` (default `asi1-mini` for the cheap, fast classifier calls).

### message_history serialization (critical — plain json.dumps does NOT work)
Pydantic AI messages are `ModelMessage` objects, serialized with a **type adapter**, not
`json.dumps`:
```python
from pydantic_ai.messages import ModelMessagesTypeAdapter
dump: str = ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")   # to store
msgs = list(ModelMessagesTypeAdapter.validate_json(dump))                  # to restore
```
- `result.all_messages()` = full history incl. prior runs; `result.new_messages()` = just
  this run. Pass a restored list as `message_history=` to continue a conversation.
- If `message_history` is set and non-empty, **no new system prompt is generated** (the
  history is assumed to include one). For our per-turn classifier we either keep a single
  long-lived history or re-attach instructions carefully.
- The library auto-repairs provider-invalid histories (dangling tool calls etc.) before each
  request, so a crash mid-run won't wedge the next call.
- `conversation_id` is auto-propagated across runs that share a history — useful for tracing,
  nothing we must manage.

This is exactly what the session-state schema's `message_history` field needs: store the
`ModelMessagesTypeAdapter.dump_json(...)` string in `ctx.storage`, restore on the next turn.
So a follow-up like "what was that fare again" (Stage 6) resolves from context.

### Deferred tools / human-in-the-loop approval (Stage 5)
Two flows exist; we use the **stop-the-world** flow (same as `shipping-label-agent`), which
maps cleanly onto the Review card:
1. Declare the finalize tool with `@agent.tool(requires_approval=True)` and put
   `DeferredToolRequests` in the agent's `output_type` (`output_type=[str, DeferredToolRequests]`).
2. First run: the model calls the tool; because it requires approval the run **ends** with a
   `DeferredToolRequests` output (`.approvals` holds `ToolCallPart`s with `tool_call_id`).
   The tool body does **not** run. Serialize `result.all_messages()` + stash the `tool_call_id`.
3. Send the Review card. Approve/Reject resolve the deferral:
   ```python
   results = DeferredToolResults()
   results.approvals[tool_call_id] = True            # approve  (or ToolApproved())
   # results.approvals[tool_call_id] = ToolDenied("...")   # reject
   agent.run(message_history=restored, deferred_tool_results=results, deps=..., model=...)
   ```
   Only on approval does the tool body execute. `shipping-label-agent` uses
   `results.approvals[id] = True` / `ToolDenied(...)`; the brief mentions `ToolApproved()`.
   Docs confirm `approvals[id]` accepts a bool, `ToolApproved`, **or** `ToolDenied` — all
   equivalent; we'll use the boolean/`ToolDenied` form to match the in-repo reference.
- (There is also a newer inline `HandleDeferredToolCalls` capability that resolves without
  ending the run; not needed here since the approval physically comes from a separate chat
  turn, i.e. a different process boundary — stop-the-world is the correct fit.)

Note: for this transit agent the "finalize itinerary" tool doesn't touch money or an
external side-effecting API (payment already happened at Stage 0). The `requires_approval`
gate is used purely as the structural confirm/deny mechanism the brief asks for, so Approve
just emits the final itinerary. This keeps the deferred-tool machinery honest to the docs
while not over-engineering a fake external call.

---

## 6. Data APIs

### Transitland Routing API (`GET/POST https://transit.land/api/v2/routing/otp/plan`)
- Auth: `api_key` **or** `apikey` query param, or `apikey` header (docs inconsistent; both
  query forms confirmed working per brief). **Separate** paid/beta subscription from the REST
  key — "Transitland Routing API - Beta" plan, 1,000 free queries/month.
- Required params: `fromPlace`, `toPlace` (coordinate strings), `time` (HH:MM:SS local),
  `date` (YYYY-MM-DD local). Useful optional: `arriveBy`, `maxItineraries`/`numItineraries`
  (default 10), `maxWalkingDistance`, `fallbackWalkingItinerary` (walking-only if no transit),
  `includeWalkingItinerary`, `useFallbackDates`, `useTargetStopPruning` (default **true** —
  leave it; it's what correctly returns walking-only for short hops).
- **Schedule-only — no GTFS-Realtime** (explicit "known limitation"). We overlay 511 live
  delays ourselves at Stage 4, keyed on the `tripId`/`routeId` the router returns.
- Response: `plan.itineraries[]`, each with `duration`, `distance`, `startTime`/`endTime`
  (ms epoch), `walkTime`, `transitTime`, `waitingTime`, `transfers`, and `legs[]`. Each leg:
  `mode` (`WALK|BUS|RAIL`), `transitLeg` (bool), `from`/`to` (`lat`,`lon`,`name`,`stopId`,
  `stopCode`,`stopOnestopId`,`departure`), and for transit legs `agencyId`, `agencyName`,
  `routeShortName`, `routeLongName`, `routeType`, `routeId` (e.g. `"BA:Green-N"`, `"SF:31"`,
  `"AC:51A"`), `routeColor`, `tripId` (e.g. `"BA:1508761"`), `headsign`, `feedId`,
  plus `intermediateStops[]` and `legGeometry` (encoded polyline). **No populated fare field**
  → fares computed from 511 (§Fares).
- **⚠ Coordinate order — see §7.** Use **`lat,lon`** (e.g. `"37.7757,-122.47996"`).

### Transitland REST v2 (`https://transit.land/api/v2/rest`) — geocoding tier 1
- Auth: `apikey` query/header. Stops search: `GET /api/v2/rest/stops?lat=..&lon=..&radius=..&apikey=..`
  (nearby), or free-text `?query=<name>`. Filter with `served_by_route_type[]=`. Returns
  `stops[]` with `stop_name`, `geometry` (coords), `onestop_id`, `stop_id`, admin names.
  We use this first for stop/station-name inputs ("Powell St", "Downtown Berkeley").
- This is the **free** production tier (rate-limited) — a different product from the routing key.

### Geocoding — OSM Nominatim (now the primary tier; live-tested)
Free `https://nominatim.openstreetmap.org/search?q=<addr>&format=json&countrycodes=us&limit=5&viewbox=<bay>&bounded=1`.
Usage policy: ≤1 req/sec, descriptive `User-Agent`, no heavy batch (implemented: async throttle +
5-min in-process cache). Bay-Area viewbox + `bounded=1` keeps results local.

**Live test results (2026-07-21, real Nominatim calls):**
| Query | status | n | top hit (lat,lon) |
|---|---|---|---|
| "Downtown Berkeley" | ambiguous | 2 | Downtown Berkeley, Shattuck Ave (37.8701, -122.2681) |
| "Powell St San Francisco" | ambiguous | 4 | Powell Street, Union Square (37.7847, -122.4072) |
| "Fruitvale BART" | ambiguous | 3 | Fruitvale BART (37.7752, -122.2249) |
| "the Mission San Francisco" | ambiguous | 5 | (imprecise: CCA, 7th/Mission) (37.7679, -122.4006) |
| "Berkeley" | resolved | 1 | Berkeley, Alameda County (37.8708, -122.2729) |
| "1600 Amphitheatre Parkway" | ambiguous | 2 | Google Bldg 41 (37.4225, -122.0856) |
| "asdkfjqwoeixyz nonsense place" | not_found | 0 | — |

Takeaways baked into the implementation: coords come back correct lat/lon; vague neighborhood
names ("the Mission") are imperfect but correctly land in the **disambiguation carousel** path
rather than being silently accepted; a garbage string correctly returns **not_found** →
terminal card + re-show form. This validated the two-tier resolver before Stage 3 was built.

### 511.org Regional API (free token, no card)
- Base `http://api.511.org/transit/`. `api_key` mandatory. Default rate limit **60 req / 3600 s**
  per token — tight, so cache aggressively (master edge-case: rate-limit discipline).
- `datafeeds?operator_id=RG` → consolidated **Regional GTFS zip** (all ~30+ operators),
  and it bundles the GTFS+ and **GTFS-Fares v2** files — no separate fares key. Download once,
  cache/parse locally, refresh infrequently (schedules change slowly).
- `servicealerts?agency=RG` → GTFS-RT Service Alerts (also available as JSON via `format=json`).
- `tripupdates?agency=RG` → GTFS-RT Trip Updates (Protocol Buffer). `vehiclepositions` similar.
  These are the live-delay overlay for Stage 4, keyed on `tripId`/`routeId`.

### GTFS-Fares v2 (parsed from the 511 regional zip) — for Stage 4 fares
Files: `fare_products.txt` (fare types + amounts + `fare_media_id` + `rider_category_id`),
`fare_media.txt` (cash / clipper / contactless etc.), `fare_leg_rules.txt` (which product
applies to a leg, filtered by network/area/timeframe), `fare_transfer_rules.txt` (free/
discounted transfers between legs — this is why the **leg sequence matters**, not a flat
per-agency lookup), plus supporting `networks.txt`/`route_networks.txt`, `areas.txt`/
`stop_areas.txt`, `timeframes.txt`, `rider_categories.txt`. Concepts: a *leg* is one
continuous ride; fare_leg_rules match legs to products via filter conditions; fare_transfer_rules
compute discounts across consecutive legs. Clipper-vs-cash-vs-pass differences live in
`fare_media` + distinct `fare_products`. **Exact column names for these files will be
re-verified against the actual 511 regional zip at Stage 4** (per "never fabricate an API
shape") — where an operator has incomplete Fares v2 data, the computed cost is labeled
"Estimated", never shown as false precision.

### Stripe testing
Success `4242 4242 4242 4242`; decline `4000 0000 0000 0002` (`card_declined`). Any future
expiry, any CVC, any ZIP. Test/live separated by key prefix (`sk_test_`/`pk_test_`).

---

## 7. Discrepancies between the brief and the live docs

1. **Transitland coordinate order.** The routing-API docs *table* labels `fromPlace`/`toPlace`
   as `"lon,lat"`, but the doc's own example value `"37.7757,-122.47996"` and the response
   `from`/`to` objects (`lat: 37.7757, lon: -122.47996`) are unambiguously **lat,lon**
   (37.77 is a latitude for SF; -122 is a longitude). The docs table label is a typo. **The
   brief is correct: send `lat,lon`.** We will build the coord string as `f"{lat},{lon}"`
   and (defensively) log the parsed `plan.from.lat/lon` on the first live call to confirm the
   router echoed our origin back in the right place.

2. **Stripe `ui_mode` value.** Horoscope docs use `ui_mode="embedded"`; the in-repo
   `shipping-label-agent/payment.py` uses `ui_mode="embedded_page"` and explicitly comments
   that this is what ASI:One's native payment renderer expects. The strict product requirement
   is "behave like shipping-label-agent's payment," so **we mirror `embedded_page`** (and its
   `return_url` + `expires_at` + `metadata` shape) rather than the docs' `embedded`.

3. **Deferred-tool approval value.** Brief says resolve with `ToolApproved()`/`ToolDenied()`;
   in-repo reference uses `results.approvals[id] = True` / `ToolDenied(...)`. Docs confirm both
   are accepted. We use the boolean/`ToolDenied` form to match the in-repo reference agent.

4. **message_history serialization.** The brief's schema comment says "verify serialization
   rather than assuming plain `json.dumps` works" — confirmed: plain `json.dumps` does **not**
   work on `ModelMessage`s; must use `ModelMessagesTypeAdapter.dump_json/validate_json`.

5. **Fares not in routing response.** Confirmed the Transitland OTP response carries no usable
   fare data, so all fare computation is ours from the 511 GTFS-Fares v2 files (as the brief
   assumed) — noted so we don't waste time looking for a `fare` field.

6. **Geocoding is genuinely unvalidated.** The brief flags Stage 2.5 as the one path not
   live-tested. Confirmed nothing in either reference agent does geocoding; this is net-new and
   got its own early test pass (recorded in §6) before the pipeline depended on it.

7. **Transitland REST stops has no free-text name search.** The brief's Stage 2.5 tier 1 was
   "match the input against Transitland's own stop/station search." Verified against the live
   REST docs: the `search` (full-text) param exists only on `agencies`/`operators`/`routes`,
   **not** on `stops`. The stops endpoint filters by `lat`/`lon`/`radius`, `served_by_onestop_ids`,
   `served_by_route_type`, `stop_id`. (The `query` field some MCP wrappers expose is a wrapper
   convenience, not a native param.) So there's no chicken-and-egg-free way to name-search stops.
   **Resolution:** make Nominatim the primary geocoder (it covers stop names *and* addresses/
   landmarks, returns multiple candidates for disambiguation, and biases to the Bay Area), rather
   than layering it under a stops search that can't take free text. The routing endpoint only
   needs `lat,lon`, which Nominatim supplies directly, so no stop snapping is required.

---

## 8. Open product decision (must be answered before Stage 0 ships)
The unlock **price** and whether the unlock is **indefinite or time-boxed** are product
decisions, not engineering defaults — raised explicitly with the requester rather than picked
silently. Placeholder until answered: `$1.00` one-time, indefinite unlock per `sender`
(mirrors the horoscope reference's $1; shipping-label uses $5). `STRIPE_AMOUNT_CENTS` and an
optional `UNLOCK_TTL_SECONDS` will be env-driven so the final number is a config change, not a
code change.

---

## 9. Clipper guidance + Stage 5 content gap (fetched live, not from memory)

**Physical/mobile card link.** `https://www.clippercard.com/get` was fetched directly and
matches what it's expected to say: Apple/Google Wallet setup, buy online (mailed), buy in
person, plus a callout for youth/senior/disability discount cards. It's the one canonical URL
Clipper wants riders sent to, so the fare-choice narration (`_show_detail` in `chat_proto.py`)
now includes it as a plain link — the same pattern already proven by the "Open in Google Maps"
link on the trip map (`_send_trip_map`), which already renders clickable in ASI:One.

**Tap-on/tap-off is a real per-agency policy fact, not derivable from GTFS-Fares v2.** Verified
against Clipper's own rider FAQ plus BART's, AC Transit's, and Golden Gate's fare pages (all
fetched live):
- **Dual-tap (charges by distance/zone, tap off or get charged the maximum):** BART, Caltrain,
  Golden Gate Transit **(bus only)**, San Francisco Bay Ferry (WETA), SMART, Sonoma County Transit.
- **Single-tap (flat fare):** Muni, AC Transit, VTA, Golden Gate **Ferry** — note this is the
  *same district* as Golden Gate Transit but a different, single-tap brand; matching must not
  collapse to a bare "golden gate" substring.
- BART states a concrete **$7.55** penalty fare for a missing/mismatched tap, which is why this
  is worth surfacing rather than treating tap-off as a minor nicety.

GTFS-Fares v2 has no explicit "requires exit tap" field, so this can't be computed from the fare
feed the way `fares.py`'s existing `distance_based`/`estimated` flag is (that flag is a
*fare-computation* heuristic — "too many station-pair products to join" — not a policy fact, and
doesn't reliably cover a flat-per-route agency like Golden Gate Transit bus). `cards.py` now
carries a small, explicitly-documented, hand-verified marker list
(`_DUAL_TAP_AGENCY_MARKERS`) matched against `agencyName`, confirmed exact for BART and Golden
Gate Transit against real Transitland output (both appear in this codebase's existing
`_AGENCY_SHORT_NAMES` map / a live screenshot); Caltrain/SMART/SF Bay Ferry markers are hedged
with both common and formal agency-name forms pending the same direct confirmation (flagged in
code as a documented, not silent, assumption).

**"Trip confirmed" content gap.** `alerts_for_routes` was called exactly once, in `_show_detail`
(Stage 4), and never written to session state — so an active service alert visible on the Stage 4
walkthrough card silently disappeared by the time the rider reached the Stage 5 confirmation.
Fixed by storing `state["alerts"]` in `_show_detail` and reading it back in `_handle_confirm` to
pass into `final_itinerary_card`, which now accepts and renders an `alerts` param the same way
`route_walkthrough_card` already did.

Separately: `cards.py`'s own documented rule is that ASI:One silently drops a card on any
schema/size validation failure, falling back to showing only the narration text — so "every
narration must be self-contained enough to stand alone." The narration sent alongside the final
confirmation card was just "You're all set - confirmed!", which violates that rule (no route,
time, or fare if the card itself ever failed to render). `chat_proto.py` now builds this
narration from a shared `_trip_recap_line` helper (route, duration, transfers, times, fare),
used at both `_handle_confirm` and the `_handle_escalate` fast path — not a replacement for the
structured `custom` card (which has typed headings/badges/styled text a plain-text message can't
reproduce), but a durable fallback consistent with the codebase's existing standard elsewhere.

---

## 10. Payment-race hardening, card-copy density, and pre-payment Q&A

**`clear_watch` was the one mutator of `_pending`/`session:{sender}` state not holding
`_lock_for(sender)`**, unlike `request_payment`, `settle_or_request_payment`, `on_commit`,
`on_reject`, and `_grant_access`. `poll_pending` holds that lock across its own Stripe network
call while validating a watch; because `clear_watch` never asked for it, a new window's clear
could interleave with an in-flight poller cycle validating an *old* window's watch under the same
`sender` (ASI:One reuses `sender` across separate chat windows), letting that old watch's
`_grant_access` (which sends the trip form) land close enough in time to a fresh window's own
payment card to appear grouped together in ASI:One's UI. `clear_watch` is now `async` and
lock-protected like every other mutator; the one call site (`chat_proto.py`'s new-window branch)
now `await`s it. This is the most plausible mechanism for the "form + payment card together, then
'No completed checkout...'" report — confirmed as *a* real race by code inspection, not
confirmed as *the exact* sequence for that report without agent-side logs from the moment it
happened.

**"No completed checkout is on record yet..." cannot be suppressed by us.** It's ASI:One's own
client-side pre-check when "Confirm Payment" is tapped before ASI:One's own backend has
independently recorded the checkout as complete — a race entirely on ASI:One's side, timing
depending on how fast their backend polls Stripe relative to the click. **A proposed fix to add a
reassuring line near the payment card was rejected before implementation**: `_send_request_payment`
must send `RequestPayment` **with no accompanying text before or after** (`payment.py`'s own
docstring, mirroring `shipping-label-agent`) or ASI:One drops the native payment sheet and shows
only the text. Any narration adjacent to that send is unsafe. The existing `poll_pending`
(~3s interval) already unlocks independently of whether this message appears or whether
`CommitPayment` ever arrives, so the message is cosmetic, not a stuck state.

**Card-copy density (ux follow-up).** The Stage 4 fare narration briefly grew into a four-idea
paragraph (estimated-fare caveat + Clipper card-ordering link + "ask me anything" line) after the
§9 pass. Reworked so the narration is one line and the per-trip specifics (which legs need a tap
off, the card-ordering link) live in `route_detail_card`'s own `summary_rows` instead — scannable
rows, not prose, and computed per-itinerary (`_tap_off_agencies`) rather than a generic disclaimer.

**Pre-payment questions now get answered without weakening the gate.** `_intent_agent` (used for
mid-flow interrupts) gained a `_FARE_KNOWLEDGE` block — Clipper 2.0 facts, the dual-tap vs
single-tap agency list, BART's $7.55 tap penalty, the card-ordering link — verified live in §9, so
`side_question` replies are grounded in what was actually checked rather than the model's own
(likely pre-Dec-2025) training data. Separately, `chat_proto._handle_unpaid` recognizes a
fare/Clipper-topic question (a cheap local heuristic, not an LLM call, for the common non-question
case) and answers it with a new, narrowly-scoped `answer_fare_question` (deliberately not the
mid-flow classifier, since there's no route/fare context yet and its other intents don't apply
pre-payment) — sent as its own text-only reply, never bundled with a re-sent `RequestPayment` in
the same turn, per the constraint above. Anything that isn't recognizably a fare question still
falls straight through to the gate, unchanged.

Three edge cases found by stress-testing this before calling it done, all fixed:
- **Scope leak.** The first cut matched *any* question-shaped text, not just fare ones - "How do I
  get from Berkeley to the Mission?" would also have been routed to an LLM with no route data,
  risking a fabricated-sounding trip answer (a real "no functionality before payment" violation,
  and simply wrong). `_looks_like_a_fare_question` now also requires a fare/Clipper/pay keyword.
- **Stale-state delay.** `_handle_unpaid` used to act on the `state` its caller had already read.
  Answering a question involves an `await` (the LLM call), during which the background poller can
  grant access - a rider who paid moments before asking a question would otherwise not get
  unlocked until their *next* message. It now re-reads state itself and, if already paid, hands
  straight off to the normal paid dispatch instead of answering (or re-gating).
- **Empty-reply gap.** A blank/whitespace-only answer (a possible, if rare, LLM output) is treated
  as a Q&A failure and falls through to the gate rather than sending nothing useful.

---

## 11. Filling the actual gaps in the Clipper knowledge base

§10's `_FARE_KNOWLEDGE` covered how Clipper/contactless payment *works* (tap-on/off, which
agencies) but not the questions a genuine first-timer asks *before* that - "do I even need a
Clipper card", "how much does one cost", "what if I lose it". Fetched live and added, not from
memory:

- **A Clipper card is not required at all** - this is the single most load-bearing fact for a
  first-timer, since the fare card previously led with a bare `clippercard.com/get` link with no
  context, which reads as "you must get this" rather than "this is optional." The card's row now
  leads with "Not required" before the link (`cards.py`'s `route_detail_card`).
- **Cost** (clippercard.com/get, bart.gov, Wikipedia's Clipper card article, cross-checked): a
  physical plastic card is a one-time **$3** fee, waived by signing up for Autoload at purchase;
  adding Clipper to Apple Wallet/Google Wallet is **free**. Cards are sold online (mailed), at
  station ticket machines, and at retailers.
- **Reloading**: online, in the Clipper app, at ticket machines, or at retailers; added value can
  take minutes to a few days to become active depending on the method (some require "tagging"
  a reader to pick up pending value).
- **Discounts**: youth, senior, disability, and income-qualified riders (Clipper START) get 50%+
  off via a discount Clipper card, arranged directly through clippercard.com - not something this
  agent issues, so the reply is scoped to pointing there rather than claiming to process one.
- **Lost/stolen card**: replaced for a **$5** fee with balance restored, reported at
  clippercard.com or (877) 878-8883. A lost *contactless bank card* is the rider's own bank's
  problem, not Clipper's - worth stating explicitly so a rider doesn't call the wrong number.

`_FARE_KNOWLEDGE` now carries all of the above (still capped at "answer in 1-2 short sentences" for
the model's actual replies - only the *reference block* got longer, not what a rider sees). A new
guard test (`test_fare_knowledge_covers_the_questions_a_first_time_rider_actually_asks`) asserts
each verified fact stays present, so a future edit can't silently drop one.

---

## 12. Tap-off/Clipper detail moved back out of the card, into short narration lines

§10 moved this detail from a narration paragraph *into* `route_detail_card`'s `summary_rows` to
stop it reading as a wall of text. In practice a link plus a full sentence doesn't fit a table
cell without wrapping across 3+ lines (the "New to Clipper?" row), which just relocated the wall
of text from the narration into the card itself rather than removing it.

Moved it back to the narration - but as short, standalone lines (`cards.fare_narration_lines`,
joined with `\n`, the same pattern already used for the trip-map caption in `_send_trip_map`),
not the single run-on paragraph from before §10. Each fact is its own sentence:

```
How would you like to pay? Same price either way.
Tap off when you exit Golden Gate Transit bus - it charges by distance.
New to Clipper? Not required - a contactless bank card/phone works just as well. Want one anyway? clippercard.com/get
Just ask if anything's unclear.
```

`route_detail_card` no longer carries the `Tap off at` / `New to Clipper?` rows at all - fare_notes
(why a trip can't be tapped through) is the only conditional row left on that card.

---

## 13. A Clipper/tap-off question mid-flow could get misclassified as "override", and a plain "A to B" destination could get dropped

Two live, reproduced failures reported against the deployed agent:

1. At the "Confirm your trip" card, the user typed *"I still don't know anything about clipper can
   you explain"*. Instead of an answer, the agent responded "I need both a starting point and a
   destination. Let's try again" and re-showed the empty intake form - the trip in progress was
   wiped.
2. On a fresh trip request, *"SFO to Golden Gate Bridge"* got "I didn't catch where you're heading
   to" - twice in a row, for the identical input.

**Root cause 1** - `_dispatch_paid` routes any free text at a card stage through the LLM interrupt
classifier (`ai.classify_intent`), which returns exactly one of
`override | escalate | side_question | clarify | accept_default`. Anything the classifier doesn't
confidently recognize falls to the `else` branch, which is `override` - a deliberate "never hang"
fallback for when the *classifier call itself* fails (`except Exception`), but it also silently
absorbs a *wrong* classification from the model. "I still don't know anything about X, can you
explain" doesn't read as a question to a keyword/prefix check (`_looks_like_a_fare_question`,
already used at the pre-payment gate, only recognized `?`-ending or `what/how/why/...`-*starting*
text) and apparently didn't read as `side_question` to asi1-mini either - it was classified as
`override`, which clears the whole trip and re-runs intake on the literal text (which has no
place names in it, hence the empty-fields error).

**First fix attempt (reverted - see below)**: `_dispatch_paid` was given a deterministic
fare-keyword short-circuit before the classifier, and `extract_trip` was given a regex "A to B"
backstop to fill whatever the model left blank. Both are exactly the kind of hardcoded,
non-LLM pattern matching this agent is supposed to avoid, and the regex proved it: the very next
real user message, *"does I need to tab in and tab off when I travel I don't understand please
give me full detail"*, contains the literal word "to" ("need **to** tab"), so the regex split it
into a fabricated origin `"does I need"` and destination `"tab in and tab off when I travel..."`
- confidently inventing a trip out of a question that named no places at all
(`tests/test_intake.py` at the time confirmed this reproduces exactly:
`_regex_origin_destination("does I need to tab in ...")` returns that fabricated pair). The
keyword short-circuit didn't catch this one either, because the user wrote "tab" instead of "tap"
and it isn't in the keyword list - a second illustration of why a fixed keyword list can't cover
real phrasing/typos.

**Reverted both.** The actual root cause both incidents share is that `classify_intent` (the real
LLM call, which already has `_FARE_KNOWLEDGE` in its prompt) was classifying a confused question
as `override` instead of `side_question` - and no amount of keyword-list patching in front of it
fixes that, it just relocates where the next unseen phrasing slips through. The fix that actually
generalizes is strengthening the classifier's own prompt: `IntentClassification.intent`'s
description and `_intent_agent`'s system prompt now spell out that `override` requires a
*concrete new place name*, and that confusion, questions, typos, or broken grammar are
`side_question`/`clarify` regardless of wording - explicitly calling out that wiping the user's
trip to answer a question would be a serious mistake. `extract_trip`'s prompt was similarly
strengthened (an explicit "the most common phrasing is `<origin> to <destination>`, extract both
even when one is a landmark" instruction) instead of a regex patch, so the SFO/Golden Gate Bridge
case is handled by the same LLM call actually understanding the sentence, not a second system
guessing at it from the outside.

This can't be fully verified by a mocked-model unit test (a `TestModel`/`FunctionModel` doesn't
exercise real classification judgment) - `tests/test_classifier.py` only confirms the *dispatch
wiring* correctly preserves trip state when the classifier does return `side_question`. Whether
`asi1-mini` reliably classifies a given real message this way needs live/manual testing, and
remains an inherent tradeoff of using a small, cheap LLM for this - not something further code can
fully guarantee.

---

## 14. The actual root cause of all three "the LLM keeps getting this wrong" reports: `ASI_ONE_MODEL=asi1` in `.env`, not `asi1-mini`

After the §13 prompt fix, "SFO to Golden Gate"/"SFO to Golden Gate Bridge" *still* failed
identically live (destination empty), which shouldn't happen if the issue were prompt wording.
Traced with live calls against the real ASI:One API (`ASI_ONE_API_KEY` is configured in this
project's `.env`, so this was verified directly rather than guessed at):

- `extract_trip("Berkeley to the Mission")` - the product's own canonical example - **also**
  returned `destination=''` on the code as it stood at HEAD (before *any* of this session's
  prompt edits). So this was never a prompt-wording problem, and the §13 prompt changes, while
  reasonable prompt-engineering in their own right, could not have fixed the live symptom either.
- A minimal 2-field Pydantic AI agent (no trip/transit semantics at all) reproduced the same
  bug: `destination` empty regardless of input, field order, or field name (`end_place`,
  `start_place` - renaming didn't help, so it isn't about the literal word "destination").
- A **plain-text** (non-tool-calling) chat completion against the same model correctly answered
  `origin=Berkeley, destination=the Mission` - so the model can do the task; something specific to
  *structured/tool-call output* was failing.
- Capturing the actual outgoing HTTP request (`httpx.AsyncClient.send` monkeypatch) showed
  Pydantic AI was sending `"model": "asi1"` - not `"asi1-mini"`. `.env` had
  `ASI_ONE_MODEL=asi1` (missing `-mini`), silently overriding the code's own
  `os.getenv("ASI_ONE_MODEL", "asi1-mini")` default (which `.env.example` correctly documents).
- Confirmed directly: the identical raw tool-call request sent to `asi1` vs `asi1-mini` -
  `asi1` returns `{"origin": "Berkeley, CA, USA"}` (drops `destination` silently); `asi1-mini`
  correctly returns both fields. `asi1` has a real, reproducible defect in multi-argument
  tool-calling; `asi1-mini` (the documented default) does not.

**Fix**: `.env`'s `ASI_ONE_MODEL` corrected from `asi1` to `asi1-mini` - a one-line config fix,
no code change. Re-verified live against all three previously-broken cases after the fix:
`extract_trip` now correctly fills both fields for "SFO to Golden Gate", "SFO to Golden Gate
Bridge", and "Berkeley to the Mission"; `classify_intent` now correctly returns `side_question`
(not `override`) for both live-reported confused messages ("does I need to tab in and tab off...
I don't understand", "I still don't know anything about clipper can you explain"), each with a
grounded reply from `_FARE_KNOWLEDGE`.

This means the §13 misclassifications were very likely *also* downstream of this same
misconfiguration, not something the §13 prompt strengthening alone fixed - `asi1`'s tool-calling
defect plausibly degrades `_intent_agent`'s structured output the same way it did
`_trip_extract_agent`'s. The §13 prompt changes are still worth keeping (clearer instructions are
good practice, and reduce reliance on any one model behaving perfectly), but this `.env` fix is
what actually resolves the reported symptoms. Nothing in `ai.py`/`chat_proto.py` needed to change
for this - a reminder to check configuration against a live call before continuing to patch code
for a symptom that isn't actually in the code.

---

## 15. A second, independent cause of the same "question at review wipes the trip" symptom: two unrelated Pydantic AI agents sharing one state key

After §14's `.env` fix, the exact same *symptom* (a question at the "Confirm your trip" card
wipes the trip and re-shows the intake form) still reproduced live, with a real Python exception
this time, not a misclassification:

```
ERROR: [classifier] failed, defaulting to override: Cannot provide a new user prompt when the
message history contains unprocessed tool calls.
```

Root cause: `state["message_history"]` was being used for **two different, unrelated Pydantic AI
agents**:

- `_show_review` (Stage 5) stores `_finalize_agent`'s history there when the review card opens -
  and that history *always* ends with an unresolved tool call (`finalize_itinerary`,
  `requires_approval=True`), since the whole point of the deferred-tool gate is that it's waiting
  on the user's Confirm/Cancel before that tool call resolves.
- `classify_intent` (the cross-cutting interrupt classifier, `_intent_agent`) reads and writes the
  *same* key for its own, unrelated conversation.

So asking any question while the review card is open (`AWAITING_CONFIRM`, `pending_approval` set)
made `_dispatch_paid` call `classify_intent(text, history_json=state["message_history"], ...)`
using `_finalize_agent`'s dangling tool-call history - which Pydantic AI correctly refuses to
continue with a new user prompt, since `_intent_agent` has no idea what to do with an unresolved
`finalize_itinerary` tool call it never issued. The resulting exception hit the
`except Exception -> _handle_override` fallback (added originally as a "classifier API down,
never hang" safety net - research-notes.md §13), which reads as a plausible explanation for why
this kept resembling a classifier *misjudgment* even after the model was fixed: it's the same
code path, just reached by a different failure now.

Reproduced directly: calling `start_finalize(...)` then feeding its `history_json` straight into
`classify_intent(...)` raises `Cannot provide a new user prompt when the message history contains
unprocessed tool calls.` - the identical message from the live log.

**Fix**: split the one overloaded key into two - `state["message_history"]` stays exclusively
`_intent_agent`'s own conversation, and a new `state["finalize_history"]` holds
`_finalize_agent`'s pending-tool-call history (`_show_review` writes it, `_handle_confirm` reads
it for `resume_finalize`). `clear_trip_state` now also resets `finalize_history` alongside
`pending_approval`, since both belong to the same Stage 5 gate. `message_history` is untouched by
`clear_trip_state` as before (Stage 6's "keep paid + message_history" comment is why - a rider's
classifier context should survive into their next trip in the same session).

Test: `tests/test_classifier.py::test_side_question_at_review_does_not_collide_with_finalize_history`
asserts `classify_intent` is only ever given `message_history`, never `finalize_history`, and that
asking a question at the review card leaves both the review gate (`pending_approval`,
`finalize_history`) and the trip itself intact.

---

## 16. A third recurrence of the same symptom, with no exception logged this time - `_handle_override` now independently verifies the text before destroying anything

After §14 and §15, the *identical* symptom reproduced live a third time, at `SHOWING_DETAIL`
(right after the walkthrough/fare-detail cards were sent): a rider asked *"can you tell me more
about clipper? and are there anything I need to pay attention when traveling?"*, got wiped to the
empty intake form, then the same thing happened again on the next message (which is fully
explained by the *first* wipe already having reset the stage to `INTAKE` - once there, `_dispatch_
paid`'s fast path sends every message straight to `_handle_intake` with no classifier call at all,
so the second failure isn't a second bug).

This time the terminal log for that exact run showed **no** `ERROR: [classifier] failed...` line -
ruling out both prior root causes (§13's model bug and §15's history collision, both of which
always throw). Direct, repeated live calls (5x) to `classify_intent` with this exact text and stage
all correctly returned `side_question`, and a full end-to-end simulation of `_dispatch_paid` with a
realistic `SHOWING_DETAIL` state also produced the correct grounded reply - the bug did not
reproduce isolated, which is consistent with LLM output being probabilistic rather than a
deterministic code defect: the *same* prompt will not return the identical output on every
sampling run, even with temperature effectively low. Chasing the exact production sample that
misfired further wasn't going to be conclusive or fixable at the prompt-engineering level alone (§13
already showed that patching prompts for one observed phrasing just relocates the failure to the
next unseen one).

**Fix - defense in depth, independent of classifier accuracy**: `_handle_override` (the function
`_dispatch_paid` calls whenever the classifier says `override`, for *any* reason - correct
classification, hallucinated classification, or the `except Exception` fallback) now runs
`extract_trip` on the text itself *before* wiping anything. An `override` is, by definition, "the
user names a concrete new origin and/or destination" - so if the identical text the classifier
labeled `override` doesn't extract to *either* field even here, that label is self-evidently wrong
and is no longer trusted: the trip in progress is left untouched and the current card is re-shown
with a plain "I didn't catch a new starting point or destination there" note, instead of being
destroyed. A real, partial-or-full override (e.g. "actually take me from Oakland instead", which
extracts `origin="Oakland"`) is unaffected and proceeds exactly as before.

This does not depend on identifying why the classifier said `override` this particular time - it
holds regardless of whether the cause is a genuine model misjudgment, non-determinism, or a future
bug in `classify_intent` itself, because it double-checks the *outcome* (does this text name a
place?) rather than trying to prevent every possible way the classifier could be wrong. This also
strictly improves the pre-existing `except Exception -> _handle_override` "classifier is down"
fallback: previously a total classifier outage would wipe *any* in-flight trip on pure guesswork the
moment a user typed anything; now it only does so if the text itself contains a real place name,
otherwise it degrades to "I didn't catch a new starting point..." while preserving the trip - a
strictly safer default than guessing.

Tests: `tests/test_classifier.py::test_override_never_wipes_a_trip_when_the_text_names_no_place`
reproduces the exact live failure text with a mocked classifier forced to return `override`, and
asserts the trip and stage survive. `test_classifier_failure_without_a_place_preserves_the_trip`
covers the same guard on the pre-existing "classifier raised an exception" fallback path.
`test_classifier_failure_with_a_real_place_still_overrides` and the existing
`test_override_clears_trip_and_reintakes` confirm a real override (text that does name a place)
is untouched by this change.

---

## 17. A fourth recurrence with the §16 fix already live - narrowed down to `stage`, not the classifier

After §16 shipped, the identical-looking symptom reproduced live a fourth time, at `SHOWING_DETAIL`,
with a brand-new message ("how can I register for clipper and is that cheaper than credit card?").
Confirmed the running process had the §16 fix (`chat_proto.py` mtime predates this run's restart in
the terminal log).

This time, every angle was tested directly against the exact reported text and the live model,
repeatedly:
- `classify_intent(text, stage=SHOWING_DETAIL)` - 3x live calls, always `side_question` with a
  correct, grounded reply.
- `extract_trip(text)` - correctly empty on both fields (it is not a trip request).
- The same text with a literal `@bay-area-transit-age ` prefix prepended (in case ASI:One relays
  the mention to the agent) - same correct results. (Moot regardless: `cards.extract_text` already
  strips a leading `@mention` before any of this code sees the text.)
- A full, faithful reconstruction of `_dispatch_paid` with realistic `SHOWING_DETAIL` state (trip,
  itineraries, fare options all populated, exactly as `_show_detail` leaves them) and the *real*
  (non-mocked) `classify_intent` - produces the correct answer and leaves `stage`/`trip` untouched.

Every one of these succeeded. This is strong evidence the bug is not in classifier judgment,
`extract_trip`, mention-handling, or the `_dispatch_paid`/`_handle_override` wiring itself - all of
which were re-verified against this exact input and found correct. The one thing that could not be
verified offline is the actual value of `state["stage"]` in the live process at the moment this
specific message was dispatched: the observed reply ("I need both a starting point and a
destination... fill in the form or described your trip") can *only* come from `_handle_intake`
failing `validate_trip_texts`, which is only reached (a) via `_handle_override`, ruled out by the
§16 guard and confirmed live-testable to not fire on this text, or (b) via `_dispatch_paid`'s own
fast path, taken whenever `state.get("stage")` is not one of `SHOWING_ROUTES` / `SHOWING_DETAIL` /
`AWAITING_CONFIRM` - which would also fully explain why no classifier call, and therefore no
`[classifier] failed...` error, appears in the log for this turn (the fast path never calls the
classifier at all).

**Added (not yet root-caused) diagnostic logging** rather than another speculative prompt/guard
change, since three consecutive fixes (§13, §15, §16) each correctly fixed a real, reproduced cause
and yet the symptom re-appeared from a fourth, distinct trigger every time - guessing a fifth fix
blind is not a good use of a real user's time. `_dispatch_paid` now logs, on every call:
- the fast-path branch: resolved `stage`, whether a trip/itineraries are present in state, and the
  first 60 chars of the text - `[dispatch] fast-path to _handle_intake | stage='intake' ...` would
  immediately confirm/deny the theory above the next time this happens.
- the classifier branch: the stage and the returned intent - `[dispatch] classifier at
  stage='showing_detail' -> 'side_question'`.

Next occurrence: capture the `[dispatch]` log line for that exact turn. If it reads
`fast-path to _handle_intake | stage='intake'` (or any non-card stage) with `has_trip=True`, that
conclusively confirms the state's `stage` field itself is the thing going wrong between `_show_detail`
saving it and the next message being dispatched - at which point the investigation moves to *what
else* writes `session:{sender}` in that window (the Stripe poller and `reset_on_new_window` are the
only other writers, both re-checked in §16 but not yet caught in the act with direct evidence).

---

## 18. Root cause found via the §17 logging: `DONE` was deliberately excluded from the classifier, and that's exactly the stage every report happened at

The `[dispatch]` logging added in §17 caught the next occurrence outright - no more guessing:

```
[dispatch] fast-path to _handle_confirm | stage='awaiting_confirm' text='{"action":"confirm",...}'
[dispatch] fast-path to _handle_intake  | stage='done'   has_trip=False text='Is this clipper will be more cheaper than credit card ?'
[dispatch] fast-path to _handle_intake  | stage='intake' has_trip=False text='But i think I will travel alot, how can I register for clipp...'
```

The user confirmed a trip (line 1: `action=confirm`, which per `_handle_confirm` runs
`clear_trip_state` and sets `stage=DONE` - "ready for the next one"), then immediately asked a
follow-up question about the trip they'd just booked. That is `stage='done'`, not
`showing_detail`/`awaiting_confirm` as every prior screenshot's *visible, still-on-screen* card had
suggested - the screenshots were showing the last card rendered before confirmation, not the actual
current stage at the moment the question was typed, because confirming replaces that card with the
final itinerary recap and silently advances past it.

`_CARD_STAGES` (the set of stages where `_dispatch_paid` runs free text through the interrupt
classifier instead of straight to `_handle_intake`) never included `DONE` - by original design,
codified in the old comment: *"At INTAKE/DONE free text is always just a (new) trip request."* That
premise is simply wrong for a rider who just finished paying + confirming and now wants to ask
something before their next trip: their question has no origin/destination in it, so plain
`extract_trip` correctly returns empty fields, `validate_trip_texts` correctly rejects that, and
`start_intake(error=...)` sends "I need both a starting point and a destination" - a technically
correct response to "was this a trip request?" but a non-answer to the actual question asked, and it
also flips `stage` to `INTAKE`, so the *next* message (even an unrelated one) hits the identical
"treated as a broken trip" fate regardless of content - explaining why the symptom always seemed to
cascade across two consecutive messages in every report.

This retroactively also best-explains §16/§17's "isolated reproduction succeeds, live still fails"
outcome: in both of those investigations the classifier and `_handle_override` guard were tested
against `SHOWING_DETAIL` state, since that was the last screen visible in the screenshot - but the
live user had almost certainly already tapped Confirm (advancing to `DONE`, clearing trip state)
before typing the question that was screenshotted alongside the now-stale detail card still on
their screen. Every angle *within* `SHOWING_DETAIL` checked out because the bug was never there.

**Fix**: added `DONE` to `_CARD_STAGES`, so free text at `DONE` now goes through the same
`classify_intent` interrupt classifier as every other stage, rather than straight to
`_handle_intake`:
- A real question ("is Clipper cheaper?") classifies as `side_question` and gets a grounded,
  `_FARE_KNOWLEDGE`-backed answer via `_resend_current_card`, whose `DONE` case (not one of
  `SHOWING_ROUTES`/`SHOWING_DETAIL`/`AWAITING_CONFIRM`) already correctly falls through to a plain
  text reply - there is no card left to re-show, which is correct, since the trip is done.
- A genuine new trip request ("Berkeley to the Mission") classifies as `override`, and
  `_handle_override`'s extraction guard (§16) sees real places and proceeds to
  `clear_trip_state` (already a no-op at `DONE`) + `_handle_intake(text)` - identical to the
  pre-fix behavior for this case.
- `escalate`/`accept_default` intents (unlikely at `DONE`, no plan to escalate/default) both
  degrade to the same "start intake" fallback their code already has for an unmatched stage - no
  new failure mode introduced.
- `_intent_agent`'s system prompt (`ai.py`) was updated to mention the `done` step explicitly (a
  rider who just confirmed a trip, asking a follow-up or starting the next one), since it previously
  only described "mid-way through planning a trip", which wouldn't have matched this stage well.
- `INTAKE` was deliberately left out of `_CARD_STAGES`: nothing has been asked there yet, so free
  text is unambiguously a first attempt at a trip request, and running the classifier would be pure
  overhead with no benefit.

Tests: `tests/test_classifier.py::test_done_stage_question_is_answered_not_treated_as_broken_intake`
reproduces the exact live failure (confirmed trip, then a fare question) and asserts it's answered
without bumping `stage` off `DONE`. `test_done_stage_real_trip_request_still_starts_a_new_trip`
confirms a real trip request right after `DONE` is unaffected.
