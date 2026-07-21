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
(both reference agents do this first thing), then process. Session identity: `ctx.session`
changes per chat window; `sender` is stable per user and can be **reused across windows**,
so a new window must reset state (see `session_state.check_new_window_and_reset` — we
replicate this, otherwise a brand-new conversation silently resumes an already-paid one).

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
  question) gets the identical gate. Never reuse a stale `client_secret`; if the deadline
  passed, create a new session.
- Stage 1: `on_commit` (seller) → reject if `payment_method != "stripe"` or no
  `transaction_id`; else verify with Stripe (retry 2–3× with short backoff because the UI
  can `CommitPayment` a beat before Stripe settles); on paid set `paid=True/paid_at/stage`,
  send `CompletePayment`, then **immediately** send the Stage 2 form in the same turn.
  Idempotency: if already `paid`, ignore a duplicate `CommitPayment` (double-click), never
  re-grant. "paid"/"done" text is kept only as a manual re-check fallback.

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
