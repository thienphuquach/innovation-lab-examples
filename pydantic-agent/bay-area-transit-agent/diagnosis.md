# Diagnosis — round 2 live-testing issues

Every finding below was reproduced live (real Transitland/511/Nominatim calls, real
`fares.py`/`clients/*` code, no mocks) before being written down. Where a raw response
mattered, the exact request/response is included so the root cause is verifiable, not
inferred. No code has been changed yet — this file is the investigation, not the fix.

---

## Issue 1 — SFO → Golden Gate Bridge returned zero routes

**Reproduced live** by geocoding both place names exactly as the user did, then calling
`clients.transitland.plan()` with the coordinates the user's disambiguation picks actually
resolved to:

- Origin ("San Francisco International Airport," candidate "780, South Airport Boulevard,
  San Francisco") → `(37.622452, -122.3839894)`.
- Destination ("Golden Gate Bridge," candidate "San Francisco, Marin County, California")
  → `(37.8202408, -122.47857)`.

```
plan(37.622452,-122.3839894 → 37.8202408,-122.47857, +30min) → 0 itineraries
```

Raw response confirms this isn't a timeout/error — Transitland answered normally with an
empty `itineraries: []`.

**Is this a real, transit-served trip?** Yes. I re-ran the identical origin against the
*other three* Golden Gate Bridge geocode candidates from the same Nominatim query:

| Destination candidate | Coordinates | Itineraries found |
|---|---|---|
| "San Francisco, Marin County, California" (picked by the user) | 37.8202, -122.4786 | **0** |
| "Presidio Parkway, San Francisco, California" | 37.8176, -122.4783 | **0** |
| "Marin County, California, 94965" | 37.8322, -122.4808 | **5** |
| "San Francisco, California, 94129" | 37.8075, -122.4756 | **6** |

The two working destinations are the real, transit-served locations: the Marin-side vista
point and the SF-side vista point/toll-plaza area (where Golden Gate Transit and Muni's 28
actually stop — confirmed in the raw leg data, e.g. stop `"Golden Gate Bridge/Parking Lot"`
at `37.80756,-122.47502`). The two that return zero are coordinates that fall **on the
bridge's motorway deck itself, between the two ends** — not a place a pedestrian can be
routed to on foot, so OTP correctly finds no walk-accessible transit connection there.

**Root cause:** this is not a routing-engine bug and not a "no service exists" case. It's
downstream of Issue 3 below: Nominatim's "Golden Gate Bridge" result set mixes two
literally-unreachable mid-span points in with two genuinely-reachable end points, our
disambiguation card presents all four as equally valid choices with no way to tell them
apart, and the user (reasonably) picked one of the two that don't work. `_search_routes()`
(`chat_proto.py:251-297`) has no fallback for this — a zero-itinerary result is treated as
a flat dead end.

---

## Issue 2 — Zero-result fallback loses the user's context

**Read the code path directly** (`chat_proto.py:272-284`):

```python
itineraries = plan_obj.get("itineraries") or []
if not itineraries:
    await send_card(ctx, sender, "No routes found for that time.", terminal_info_card(...))
    await start_intake(ctx, sender, state)   # <-- generic, blank form
    return
```

`start_intake()` (`chat_proto.py:69-94`) always renders the same static
`intake_form_card()` (`cards.py:115-165`) with the same hardcoded placeholder examples
("Downtown Berkeley, or Powell St" / "The Mission, or Fruitvale BART") regardless of what
was just attempted. At the point of failure, `state["trip"]` (or `pending_trip`) still
holds the real origin/destination text the user entered — it is simply never read by the
failure branch or passed into the narration or the re-shown form.

**Root cause:** confirmed — this is a pure omission, not a data-availability problem. The
context needed to make the fallback specific (origin text, destination text, attempted
time) is sitting in `state` one line above the code that discards it.

One constraint I checked before proposing anything here: the live `form` card schema
(fetched from `innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards`,
confirmed against `research-notes.md` §"form") has no `value`/`default` key on a field —
only `name`, `kind`, `label`, `required`, `options`, `placeholder`. So the fix cannot
"pre-fill" the form inputs; it has to work through narration text (which the user always
sees) plus, ideally, offering the specific alternate action available (retry with a
different time, or re-open the candidate list for whichever endpoint was ambiguous)
rather than a blank reset.

---

## Issue 3 — Disambiguation candidates are visually indistinguishable

**Reproduced live** against Nominatim with the exact same query/params `clients/geocode.py`
uses (Bay Area viewbox, `bounded=1`, `limit=5`):

```
"San Francisco International Airport":
  1) label="...780, South Airport Boulevard, San Francisco..."  kind=aeroway/aerodrome   (37.6225,-122.3840)
  2) label="...780, North Link Road, Lomita Park..."             kind=railway/station     (37.6162,-122.3915)

"Golden Gate Bridge":
  1) label="...Presidio Parkway, San Francisco, California..."   kind=man_made/bridge     (37.8176,-122.4783)
  2) label="...San Francisco, Marin County, California..."       kind=highway/motorway    (37.8202,-122.4786)
  3) label="...Marin County, California, 94965..."               kind=highway/motorway    (37.8322,-122.4808)
  4) label="...San Francisco, California, 94129..."              kind=highway/motorway    (37.8075,-122.4756)
```

**SFO case:** the two candidates are *genuinely different physical points* ~750m apart —
one is the airport terminal area, the other is a BART/AirTrain-adjacent point Nominatim
tags as a rail station. `cards.py`'s `_short_label()` (`cards.py:169-174`) does render
different subtitles for these two ("780, South Airport Boulevard, San Francisco" vs. "780,
North Link Road, Lomita Park" — this matches the screenshot exactly), so they are not
literally duplicate text. The real problem here is semantic, not textual: nothing tells the
user *which one is the actual airport entrance* vs. an alternate OSM-tagged point for the
same complex — `kind` (`aeroway/aerodrome` vs. `railway/station`) is fetched
(`models.py:27`, `clients/geocode.py:81`) but is dropped on the floor and never reaches the
card (`_short_label` only ever looks at `c.label`).

**Golden Gate Bridge case:** this is worse — 3 of the 4 candidates are OSM `highway/motorway`
segments (the road/bridge deck itself, tagged at different points along its length), not
points a rider would recognize as distinct destinations. `_short_label`'s
comma-split-and-truncate-to-`parts[1:4]` (`cards.py:169-174`) happens to produce different
short strings for all 4 in *this* run, but the underlying issue is structural, confirmed by
Issue 1: these aren't 4 different "places" a user should be asked to choose between at all —
they're OSM's fragmented representation of one linear landmark, 2 of which are not valid
trip endpoints. Whether a live run renders them as textually identical (as the user saw) or
merely as unhelpfully-similar depends on exactly which fragments Nominatim ranks into the
top 5 that call — the underlying data problem is the same either way.

**Root cause (confirmed, two-part):**
1. `kind` (OSM class/type) is fetched but discarded before it reaches the disambiguation
   card — a real, available signal for distinguishing candidates is thrown away.
2. There is no de-duplication/quality filtering step: near-collinear points along a single
   linear OSM feature (a bridge/road) are surfaced as independent user-facing choices
   without any indication that some of them are not valid, reachable destinations. This is
   also the direct cause of Issue 1.

---

## Issue 4 — No turn-by-turn travel instructions in the final confirmation

**Read `final_itinerary_card()`** (`cards.py:326-344`) and confirmed against a **real** raw
Transitland itinerary I fetched (SFO → Golden Gate Bridge vista point, saved in full during
this investigation). Every transit leg in the real response already contains:

```json
{
  "from": {"name": "Daly City BART Station", ...},
  "to":   {"name": "Golden Gate Bridge/Parking Lot", ...},
  "routeShortName": "28", "agencyName": "San Francisco Municipal Transportation Agency",
  "headsign": "Fisherman`s Wharf",
  "startTime": ..., "endTime": ...
}
```

i.e., exactly the board-stop name, alight-stop name, route, and direction-facing headsign
needed for turn-by-turn guidance — for *every* leg, not just this one.

`final_itinerary_card()` uses none of it. It only calls `_route_title()` (route names
joined by "→") and the *overall* itinerary's first-leg start / last-leg end time
(`cards.py:330-338`) — it never iterates `itinerary["legs"]` to emit a per-leg
board-here/get-off-here/transfer-here sequence. `route_detail_card()` (Stage 4,
`cards.py:269-307`) has the same gap.

**Root cause:** confirmed data-availability is not the problem — this is a pure card-builder
omission. The raw API response already carries everything a per-leg instruction list needs;
none of it is read.

---

## Issue 5 — Route options don't show which direction to board

**Same evidence as Issue 4.** `headsign` is present on every transit leg in the raw
response (`"headsign": "Richmond"`, `"headsign": "Lot D"`, `"headsign": "Fisherman\`s Wharf"`
in the sample I pulled). `_route_title()` (`cards.py:208-216`), which is what both the Stage
3 carousel and the Stage 4/6 detail cards use to label a route, only reads
`routeShortName`/`routeLongName`/`agencyName` — `headsign` is never referenced anywhere in
`cards.py` or `chat_proto.py`.

**Root cause:** confirmed — same class of bug as Issue 4 (data present in the routing
response, never surfaced), not a routing-API limitation. `grep -rn headsign` across the repo
turns up exactly one place: the untouched raw dict from Transitland.

---

## Issue 6 — Fare shows only Clipper, no breakdown, no comparison

**Reproduced live** by running `fares.compute_fare_options()` against a real multi-agency
itinerary's legs (SFO shuttle `SI:Lot D` → BART `BA:Red-N` → Muni `SF:28`):

```
leg SI:Lot D  (network SI): single_product_ids = []       -> clipper=(None, True)  cash=(None, True)
leg BA:Red-N  (network BA): clipper=(6.15, True)           cash=(None, True)   [BA products' media = {clipper, contactless} only — no cash/ticket media row exists]
leg SF:28     (network SF): clipper=(2.85, False)          cash=(3.00, False)  [SF has both]

compute_fare_options(...) -> []   (both options killed entirely)
```

Two distinct, confirmed causes, both inside `compute_fare_options()`
(`fares.py:130-157`):

1. **All-or-nothing per-option pricing.** The per-leg loop does
   `if fare is None: priced, estimated = False, True; break` — if **any single leg** in the
   itinerary can't be priced under a given payment method, the *entire* option for the
   *entire* itinerary is discarded, with no partial/estimated fallback and no explanation
   surfaced to the user. A free airport shuttle leg with no fare product at all
   (`SI:Lot D`) silently kills both Clipper and Cash for a trip where the other two legs
   priced fine.

2. **BART's real GTFS-Fares data has no cash-media fare product at all** — confirmed by
   inspecting the actual downloaded 511 regional feed: every `BA:matrix:*` product's
   `fare_media.txt` media set is `{clipper, contactless}` only, never `cash`/`ticket`. This
   matches reality (BART's turnstiles don't take cash), so "Cash" is genuinely inapplicable
   for any itinerary with a BART leg — but the current code has no way to say "Cash: not
   accepted on this leg" and instead just silently drops the entire Cash option with zero
   indication of why, which is indistinguishable from a computation failure to the user.

   `Day pass` is separately gated on `len(uniq_nets) == 1` (`fares.py:159-163`) — correct
   for a genuinely multi-agency trip (BA+SF here has no single-operator day pass that
   covers both), but the user never sees *why* it's absent either.

   I separately confirmed day-pass product IDs do exist and match the detector's keyword
   pattern for single-operator networks (e.g. `SF:daypass:1-day` is present and would be
   found by `_day_pass()` for a Muni-only itinerary), so the pattern-matching itself is not
   broken — it's specifically the "no fallback for a partially-unpriceable leg" and
   "no reason surfaced" behaviors that produce the screenshot's "only Clipper, no breakdown"
   result.

**Root cause (confirmed):** the fare engine is silently all-or-nothing per option and never
communicates *why* an option is missing (unpriceable leg vs. genuinely not offered by the
operator vs. multi-agency day-pass ineligibility) — it just doesn't show up. Separately, no
per-leg cost breakdown is ever computed or rendered anywhere (`fares.py` only returns a
`total`, never the leg-by-leg components) even for the options that do compute successfully.

---

## Issue 7 — Visual/map component: feasibility research (not a bug)

Checked what the card wire protocol actually supports (live-fetched
`innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards`, confirmed
against `research-notes.md`): both the `carousel` card (`items[].image`) and the `detail`
card (`hero_image`) accept a **plain image URL** — no special "map" card kind exists, but
any static image reachable by URL can be embedded today with no protocol changes.

We already have everything needed to *compute* a map: Transitland's response includes an
encoded polyline per leg (`legGeometry.points`) and every stop's lat/lon.  What's missing is
a zero-cost way to turn that into an **image at a URL**. I evaluated the actual free options,
not just in principle:

| Option | Cost | Reliability | Notes |
|---|---|---|---|
| **A. Plain "Open in Maps" link** (no image) — a `https://www.google.com/maps/dir/?api=1&origin=...&destination=...&travelmode=transit` or `openstreetmap.org/directions?...` URL as plain text/CTA | Free, no key, no signup | High — it's just a URL, not an API call | Not a visual *inside* chat; hands off to the user's own maps app. Zero new infra, zero new failure mode. |
| **B. Third-party hosted static-map image service** (e.g. `staticmap.openstreetmap.de`) | Free, no key | **Low** — confirmed via current OSM community docs/help threads that this service is community-run, has no SLA, and is reported unreliable; its query API also doesn't support drawing an arbitrary route polyline (marker-only) | Would silently degrade the card if the third party is down/slow; not something to depend on for a shipped feature. |
| **C. Self-hosted static map rendering** (e.g. Python `staticmap`/`py-staticmaps` composing OSM tiles + our own polyline/markers, then uploading the PNG to Agentverse External Storage — same upload path `payment.py`/README already document for the shipping-label PDF — to get a stable URL for `hero_image`) | Free (uses public OSM raster tiles) | Medium — feasible and fully in our control, but adds a new dependency, a tile-fetch step subject to OSM's tile-usage fair-use policy (low-volume, must self-identify, not for heavy per-request production traffic), and an upload round-trip per detail-card render | Real work: new library, new code path, new failure mode to handle gracefully. Directly usable data (`legGeometry`, stop coords) already flows through the system today. |
| **D. Do nothing** | — | — | Not part of the original brief; explicitly optional per this task's framing. |

**My assessment, not yet acted on:** Option A is the only one that is simultaneously
zero-cost, zero-new-dependency, and zero-new-reliability-risk, and it directly addresses the
underlying want ("let me see this trip on a map") without taking on infra we'd have to
operate and monitor. Option C is the only path to an actual *in-chat* image and is
technically feasible with data we already have, but is a meaningfully larger scope increase
than anything else in this diagnosis and comes with an ongoing OSM tile-usage-policy
obligation. Option B I'd rule out outright — it fails the "don't depend on things I haven't
verified" standard this whole investigation is built on. I'm presenting this as a decision
for you rather than picking one, per the task's framing of this as an open question.

---

## Summary of confirmed root causes

| # | Root cause | Where |
|---|---|---|
| 1 | Downstream of #3: user was routed to disambiguation candidates that are mid-span, non-pedestrian-reachable points; zero-result search has no fallback to try other resolved candidates for the same query | `chat_proto.py:_search_routes`, `clients/geocode.py` |
| 2 | Zero-result fallback discards known trip context (`state["trip"]`) instead of using it in narration/next steps | `chat_proto.py:272-284`, `chat_proto.py:69-94` |
| 3 | `kind` (OSM class) fetched but never surfaced; no filtering/de-duplication of near-collinear/low-quality candidates (e.g. road segments crossing a landmark) | `clients/geocode.py`, `cards.py:_short_label`/`disambiguation_carousel_card` |
| 4 | Per-leg `from.name`/`to.name`/times exist in every routing response but are never read by any card builder | `cards.py:final_itinerary_card`, `route_detail_card` |
| 5 | Per-leg `headsign` exists in every routing response but is never read anywhere | `cards.py:_route_title` (and everything that calls it) |
| 6 | `compute_fare_options` is all-or-nothing per payment method (one unpriceable leg kills the whole option) and never surfaces *why* an option is missing; no per-leg breakdown is ever computed | `fares.py:compute_fare_options` |
| 7 | Not a bug — zero-cost in-chat map image requires either an unreliable third-party service (rejected) or new self-hosted rendering infra (real scope increase); a plain "open in maps" link is the only zero-risk zero-cost option available today | n/a (research) |

No fixes have been implemented yet. Next: propose a fix approach per issue for review before touching code.
