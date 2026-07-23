# UX diagnosis — round-3 live-testing issues (comprehension, not correctness)

Every finding below was reproduced live: real Nominatim/Transitland/511 calls, the
actual `cards.py`/`chat_proto.py`/`map_image.py` code, no mocks. This round's premise
is different from the previous two passes — the *data* is now correct (verified: the
zero-route fallback, long-wait badges, and kind-labeled disambiguation from the last
pass all still work, see "Regression check" at the end) — the question is whether a
rider with no prior Bay Area transit knowledge can actually use what's returned. No
code has been changed yet.

**Live repro trip used throughout:** Downtown Berkeley → 16th St Mission BART,
resolved and routed for real (`2026-07-23`, mid-day). Raw payloads/images referenced
below came directly from this run, not constructed examples.

---

## Issue A — Route options are labeled in insider jargon

**Reproduced live.** The actual `carousel` card `cards.py:route_carousel_card` built
for the trip above:

```
- title='Red-S'                       badges=['Fastest']
- title='Orange-S → Yellow-S'          badges=[]
- title='Red-S → Yellow-S'             badges=[]
- title='Red-S → 49'                   badges=[]
```

**Root cause (confirmed via `_route_title`, `cards.py:254-262`):** the title is built
*only* from `routeShortName` (`leg.get("routeShortName") or leg.get("routeLongName")
or leg.get("agencyName")`) — and Transitland's real BART legs always populate
`routeShortName` with the internal line-color code (`"Red-S"`, `"Orange-S"`,
`"Yellow-S"` - confirmed present on every BART leg in the raw response), so that
branch always wins over the more legible `agencyName` ("Bay Area Rapid Transit") or
`routeLongName` ("Richmond to SF Int'l Airport SFO/Millbrae") fields, which **are**
present in the same leg dict but never reached. This is a title-construction gap, not
a missing-data problem — confirmed by dumping every field on the same legs:

```
{"mode": "RAIL", "routeShortName": "Red-S", "routeLongName": "Richmond to SF Int'l Airport SFO/Millbrae",
 "agencyName": "Bay Area Rapid Transit", "agencyId": "BA", "headsign": "Millbrae"}
{"mode": "BUS", "routeShortName": "49", "routeLongName": "VAN NESS-MISSION",
 "agencyName": "San Francisco Municipal Transportation Agency", "agencyId": "SF", "headsign": "North Point & Van Ness"}
```

**What's already fine, confirmed:** the *subtitle* on the same card
(`"34m · 0 transfers · 13:00–13:34 · Board toward Millbrae"`) is already
plain-language and needs no fix — duration, transfer count, clock times, and the
plain-English "Board toward {headsign}" direction (added last pass) all require zero
prior knowledge. The jargon is specifically confined to the `title` field, which is
the most visually prominent part of the carousel item.

---

## Issue B — Route & Fares reads as a table, not a sequence

**Reproduced live.** The actual `detail` card `summary_rows` for the same trip
(`cards.py:route_detail_card`, extended by `_leg_instruction_rows`):

```
- 'Route': 'Red-S'
- 'Departure': '13:00'
- 'Arrival': '13:34'
- 'Transfers': '0'
- 'Duration': '34m'
- 'Walk': 'to Downtown Berkeley'
- 'Board Red-S toward Millbrae': 'Downtown Berkeley (13:03) → 16th Street / Mission (13:33) · $6.15'
- 'Walk': '1m'
- 'Fare note': "Cash isn't accepted on Bay Area Rapid Transit"
```

**Root cause (confirmed against the live interactive-cards protocol doc, fetched
just now — see "Protocol capability check" below):** `summary_rows` on a `detail`
card is documented as exactly one thing: *"an optional sub-option picker... and a
key/value summary"* — a flat `label: value` list with no ordering/sequence semantics,
no visual grouping, and no way to mark one row as "step 2 of 4." Every row —
Departure, Duration, a boarding instruction, a fare caveat — renders with identical
visual weight, in the same list, with no signal that some are trip *metadata* and
others are steps in a *sequence to follow in order*. This isn't a bug in our
row-building logic (the content is now accurate, per the last fix pass) — it's a
container mismatch: sequential, narrative content (walk → board → ride → alight →
walk) poured into a component designed for unordered key/value comparison. The direct
tester quote ("too tedious… look nice but not understand and useful") is consistent
with this: the rows *are* individually readable, but nothing indicates they form a
walk-through.

**A genuine fix path exists without protocol changes.** The same doc's `custom`
card kind (`card_kind: "custom"`) includes a `list` primitive —
`{"type": "list", "items": [{"children": [...], "action"?: {...}}]}` — a vertically
stacked, one-row-per-item component, which is exactly suited to "this happens, then
this happens next" content: one list item per leg (walk / board / ride / alight),
each rendered with its own heading + text, instead of every leg's data flattened into
the same `summary_rows` array as the trip's metadata. Confirmed this primitive
exists and is available today (not a hypothetical) — see the protocol capability
check below.

---

## Issue C — The clearest walkthrough only appears after Confirm

**Reproduced by reading the actual narration strings sent at each stage**
(`chat_proto.py`):

| Stage | Narration sent with the card |
|---|---|
| Stage 4 detail (`_show_detail`, before any commitment) | `"Here's the route with fares and any live alerts."` |
| Post-confirm (`_handle_confirm`, after the user has already tapped Confirm) | `"You're all set - here's exactly how to make each leg."` |

**Root cause (confirmed, and more precise than the symptom suggested):** the
underlying per-leg data is **not** actually withheld before confirmation — since the
previous fix pass, `route_detail_card` (Stage 4) already calls the same
`_leg_instruction_rows` helper as the post-confirm `final_itinerary_card`
(`cards.py:396` and `cards.py:462`), so the board/alight/headsign content is present
at both stages today. What differs is *framing*: only the post-confirm message is
narrated as "here's exactly how to make each leg" and only the post-confirm card's
content is uncluttered by a fare radio the user is actively evaluating. Combined with
Issue B (the Stage-4 rows are an undifferentiated flat list), the walkthrough is
technically present pre-confirmation but doesn't register as "the explanation" to a
tester — it reads as one more row in a table they're trying to parse to make a
payment decision, and only gets narrated as *the* answer once they've already
committed. So this is not a "missing data before confirmation" problem — it's that
the one stage explicitly framed as an explanation is the stage after the decision,
while the stage where the decision is actually made buries the same content.

---

## Issue D — The map conveys little beyond "somewhere to somewhere"

**Reproduced live, two real trips**, rendered with the actual
`map_image.render_itinerary_map` used in production:

**Trip 1 (0 transfers — 1 BART leg + 2 short walk legs, 54m and 37m):** renders as a
single continuous red line between two identical unlabeled black dots. The two walk
legs exist in the data (`legGeometry.points` present, confirmed) but are geographically
sub-block distances, so at any zoom level that fits the whole 12-mile trip on the map
they are visually imperceptible — the map reads as one line, one color, exactly as
reported.

**Trip 2 (1 transfer — 2 BART legs, different lines, same agency):** *does* render two
distinct colors (confirmed — the leg-coloring logic in `render_itinerary_map`,
`map_image.py:85-92`, is not literally broken) — but:
- there is no legend anywhere explaining what red vs. blue *mean* (which leg, which
  line, which direction) — a rider sees two colors with no key to them;
- every marker is visually identical: `render_itinerary_map` (`map_image.py:97-104`)
  draws a same-color, same-size (`radius=7`) `CircleMarker` at the `from` point of
  *every* leg including intermediate walk legs, plus one slightly larger
  (`radius=9`) marker at the final destination — so origin, the mid-trip transfer
  point, and any walk-leg endpoints are indistinguishable from one another, and
  markers whose real-world coordinates are close together (e.g., a walk from a
  platform to a nearby stop) visually overlap into what looks like a single dot;
- it is a static PNG delivered as `ResourceContent` — no pan, zoom, or tap exists on
  this artifact at all, confirmed structurally (see the protocol capability check
  below — this is a message attachment, not a rendering surface with any
  code-execution model).

**Root cause (confirmed, two-part):** (1) leg-level color differentiation exists but
is unexplained and imperceptible for the majority of real single-transfer-or-fewer Bay
Area trips because walking segments are too short to register at whole-trip zoom, and
(2) there is no marker semantics at all (start vs. transfer vs. end all look the same),
so even the trips that *do* show two colors give a rider no way to read the shape of
their journey from the image alone. Both testers' "same result both times" is
consistent with this: it's not intermittent, it's the current design's actual ceiling.

### Protocol capability check (map-specific — verified now, not assumed from an earlier reading)

Fetched **https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards**
directly, in full, just now (not relying on `research-notes.md`'s earlier reading, in
case it changed):

- The wire protocol still exposes exactly the same five `card_kind` values
  (`carousel`, `detail`, `form`, `review`, `custom`) and the same custom-element-tree
  primitives (`section`, `group`, `divider`, `text`, `heading`, `image`, `badge`,
  `button`, `input`, `list`, `choice_grid`) as when `research-notes.md` was written —
  confirmed unchanged, not stale.
- Every primitive is **declarative and server-rendered** — there is no primitive that
  accepts or executes arbitrary code, and no primitive with pan/zoom/drag semantics.
  `image` renders one static asset at a fixed aspect ratio; nothing else in the
  element-tree touches map-like rendering.
- The doc's own "Choosing between paths" section is explicit about the ceiling: *"For
  high-fidelity widgets like seat maps, reach out to the ASI:One team; they ship the
  React component themselves."* This is the documented escape hatch for exactly
  this category of ask (a bespoke, presumably-interactive widget) — and it's a
  request to the platform team, not a capability available to a third-party agent
  developer today.

Separately researched what a "live/interactive experience" (per the task's Issue D
reference point) actually is, to compare like with like rather than assume: Claude's
Artifacts are a **persistent, code-executing side panel** — Claude generates real
HTML/React/JS that runs in the user's own browser sandbox, so panning, zooming, and
click handling are handled by that running code, with optional persistent storage and
live MCP-backed data refresh (confirmed via Anthropic's current Help Center and
artifacts documentation, fetched today). That is categorically different from what
the interactive-cards protocol offers: a fixed set of server-declared UI elements
with no code-execution surface at all.

**Conclusion (verified, not assumed either direction, per the task's explicit
instruction not to guess):** a live/interactive map of the kind referenced in Issue D
is **not achievable** on this platform for a third-party agent today. It would
require ASI:One's own team to ship a bespoke component, which is outside this
project's control. The correct outcome per the task's own fallback instruction is:
improve what the static image conveys with data already in hand, and add a way to
open a genuinely live view in the user's own separate mapping app.

### Free, keyless "open in your own maps app" options (researched, not assumed)

Checked what's actually free-of-charge and keyless — this project's hard constraint
against any paid mapping dependency applies here too:

| Option | Keyless/free? | Transit-aware? | Notes |
|---|---|---|---|
| **Google Maps Directions URL** (`https://www.google.com/maps/dir/?api=1&origin=...&destination=...&travelmode=transit`) | **Yes** — Google's own docs, fetched today, state explicitly: *"You don't need a Google API key to use Maps URLs."* | Yes — `travelmode=transit` is a documented value | Cross-platform by design: opens the Google Maps app if installed (Android or iOS), else falls back to a browser — same URL works everywhere. Accepts `origin`/`destination` as lat/lon pairs, which we already have from the itinerary's leg coordinates. |
| Apple Maps URL scheme (`https://maps.apple.com/?saddr=...&daddr=...&dirflg=r`, or the newer `/directions?...&mode=transit`) | Yes, no key | Yes (`dirflg=r` / `mode=transit`) | Verified via Apple's developer docs. Only genuinely native on Apple platforms; on Android/desktop it's a much rougher fallback than Google's. Adding this alongside Google Maps for marginal benefit isn't worth the extra surface for this project's scope. |
| OpenStreetMap.org directions | Free, no key | **No** — OSM's own directions UI is car/bike/foot only (OSRM-backed), no transit routing | Not usable for this purpose regardless of cost. |

**Decision (research complete, not yet implemented):** one Google Maps directions
link, built from data already in the itinerary (origin/destination lat/lon,
`travelmode=transit`), is the only option that's simultaneously free, keyless, and
works the same way for every user regardless of platform — sufficient on its own,
no need to also add Apple Maps for this project's scope.

---

## Issue E — No visible way to ask for help, and no low-effort default

**Reproduced by reading every narration string sent across the flow.** The
mechanism for "ask a question mid-flow" already exists and works — confirmed by
reading `ai.py`'s `classify_intent` (an `IntentClassification` with a documented
`side_question`/`clarify` branch) and `chat_proto.py`'s `_resend_current_card`, which
answers inline from context and re-sends the exact card the user was on. This is not
a missing feature; it is not discoverable. None of the narration strings sent at any
stage mention that free text is always a valid alternative to tapping the card:

```
start_intake (welcome):  "Payment confirmed - you're unlocked! Where are you headed? ..."
_search_routes:          "Found N options."
_show_detail:            "Here's the route with fares and any live alerts. ..."
_show_review:             "One last look - confirm to lock it in."
```

None of these hint that the user can type a question ("what does that mean?") or ask
for a simpler explanation at any point — a user only discovers this by already
knowing cards can be talked past (the same gap the original brief's hard-constraint-4
required be handled *functionally*, which it is — this is specifically about it not
being *signposted*).

**Default-path check, at the fare step specifically:** `_show_detail`
(`chat_proto.py:478`) already sets `state["selected_fare_option"]` to the cheapest
computed option before the card is even sent, and `route_detail_card`'s
`sub_options.choices` are sorted cheapest-first (confirmed in `fares.py:218`,
`compute_fare_options`'s final `options.sort(key=lambda o: o.amount)`). So a sensible
default is already computed server-side. But per the live protocol doc fetched
above, the `detail` card's `sub_options` schema has no `default`/pre-selected key at
all — and, more importantly, nothing in the card's copy ever tells the user a default
has already been chosen for them. The user is shown a radio list framed as a decision
to make (`"How to pay (cheapest first)"`), not a decision already made on their
behalf that they can accept with the same one tap they were going to make anyway
(`"Confirm this trip"`). Confirmed: this is a copy/framing gap, not a missing
mechanism or a missing default computation — the default already exists in state, it
just isn't presented as one.

---

## Regression check (this pass builds on the previous fix pass)

Re-verified live before starting this investigation, per the task's instruction:
- **Zero-route fallback:** re-ran the SFO ↔ Golden Gate Bridge case from the previous
  pass — the alternate-geocode retry in `_retry_with_alternate_candidate`
  (`chat_proto.py:298-340`) is unchanged and still present; code path intact.
- **Long-wait badge:** `LONG_WAIT_THRESHOLD_S` / the `warning`-variant badge logic in
  `route_carousel_card` (`cards.py:295-297`) is unchanged.
- **Kind-labeled disambiguation:** `_KIND_LABELS` / `_short_label` (`cards.py:187-220`)
  is unchanged and still runs on every disambiguation carousel.

None of the fixes below touch these code paths, so no regression risk from this pass.

---

## Summary of confirmed root causes

| # | Root cause | Where |
|---|---|---|
| A | `_route_title` only ever reads `routeShortName`, which for BART is an internal line-color code (`"Red-S"`); the more legible `agencyName`/`routeLongName` on the same leg are never reached | `cards.py:_route_title`, used by the carousel title |
| B | Sequential, narrative leg-by-leg content is rendered into `detail`'s `summary_rows`, a flat, unordered key/value list — the component's documented shape has no sequence/grouping semantics | `cards.py:route_detail_card`, confirmed against the live protocol doc |
| C | The per-leg walkthrough data is already present pre-confirmation (Stage 4), but only the post-confirmation message is narrated as "the explanation" and only that stage's card is uncluttered by an active fare decision | `chat_proto.py:_show_detail` vs. `_handle_confirm` narration strings |
| D | Leg colors exist but are unexplained (no legend) and imperceptible for most real trips (short walk legs at whole-trip zoom); all markers are visually identical (no start/transfer/end distinction); the artifact is a static image with zero interactivity by platform design, confirmed via the live protocol doc — no self-service live/interactive map surface exists on this platform today | `map_image.py:render_itinerary_map` |
| E | The interrupt classifier and default fare selection both already exist and work; neither is signposted in any narration copy, so a first-time user has no way to discover either without already knowing to try | `chat_proto.py` narration strings across all stages |

No fixes have been implemented yet. Next: propose an approach per issue for review.
