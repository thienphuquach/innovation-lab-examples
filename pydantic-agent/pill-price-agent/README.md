# Pill Price Agent (Pydantic AI)

Tells you the honest floor price of a prescription drug and what its FDA label
says. No coupon-card upsell, no invented medical claims, and no single confident
number when the underlying data genuinely disagrees with itself.

Built on [Pydantic AI](https://ai.pydantic.dev/) inside a uAgent, with ASI:One
Interactive Cards for the UI and Stripe test-mode Checkout gating access. One
upfront charge unlocks the whole agent for the session — there is no free tier.
The pricing data is CMS's NADAC file; the drug information is openFDA. Both are
free, keyless or nearly so, and US-only, which is the scope of this agent.

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the component breakdown, the
conversation state machine, and the approval-gate sequence.

## Setup

```bash
cd pydantic-agent/pill-price-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
python agent.py
```

The first run downloads the current NADAC release (~73 MB, a few seconds) and
indexes the curated drugs into SQLite. Afterwards it checks daily and re-downloads
only when CMS publishes.

| Key | Needed for | Where to get it |
| --- | --- | --- |
| `ASI_ONE_API_KEY` | Intent extraction and price narration | [asi1.ai/dashboard/api-keys](https://asi1.ai/dashboard/api-keys) |
| `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` | The upfront access charge | Stripe Dashboard → Developers → API keys (`sk_test_…` / `pk_test_…`) |
| `OPENFDA_API_KEY` | Optional. Raises the openFDA quota from 1,000/day per IP to 120,000/day | [open.fda.gov](https://open.fda.gov/apis/authentication/) |

NADAC needs no key, no registration and no partnership.

Stripe test cards: success `4242 4242 4242 4242`, decline `4000 0000 0000 0002`.
The agent refuses to start with a live key.

## What it does

### One upfront charge, then everything is unlocked

The very first message of a session - even a bare "hi" - triggers a Stripe
checkout before any drug or intent processing happens. Decline it and nothing
works, not even a price check; there is no reduced free tier. Once it clears,
price checks, drug info, price trend history, and brand-vs-generic comparison
are all free for the rest of that chat window. A new chat window starts the
gate over, even for the same address - see [`ARCHITECTURE.md`](ARCHITECTURE.md)
for why it's scoped that way.

Once the checkout is paid, the agent notices on its own - it polls Stripe every
`STRIPE_POLL_INTERVAL_SECONDS` (default 4s) in the background and unlocks with
no reply needed. Typing "paid" still works as an immediate, zero-wait shortcut
instead of waiting for the next poll tick.

### Price checks, in three tiers of precision

**Tier 1 — you have the NDC** off a filled bottle. Exact match, exact price, no
ambiguity to resolve.

**Tier 2 — you have the drug name and strength** off a prescription. This is
where the interesting problem lives. One name at one strength routinely maps to
several genuinely different products at genuinely different prices:

```
METFORMIN HCL 1,000 MG TABLET    $0.02338/unit   $10.70-$13.70 for 30
METFORMIN ER 1,000 MG OSM-TAB    $0.31495/unit   $19.45-$22.45 for 30
METFORMIN ER 1,000 MG GASTR-TB   $0.34906/unit   $20.47-$23.47 for 30
```

Those are three real products, not three sellers. The agent shows all of them
rather than picking one, and says that the NDC on the bottle you are eventually
handed is what settles it. When formulations agree within 10% it collapses them
into a single number instead, so you never get a range that is really just noise.

**Tier 3 — you only know the drug name.** A range across every strength,
labelled as the rough estimate it is.

Every price answer, at every tier, shows the `as_of` survey date beside the
number and quotes NADAC acquisition cost **plus** a $10–13 dispensing fee. NADAC
excludes that fee by design; quoting the bare floor as "the price" would make
every honest pharmacy look like it is overcharging.

### Drug information

FDA label text, reformatted for reading and never reworded. The default answer
comes from the Medication Guide where one exists and Patient Counseling
Information where it does not, and the card names which field it came from.
Clinical-depth sections are behind an explicit "full detail" tap.

If a question needs a clinician — "should I take this", "is this dose right for
me" — the agent says so and points at a pharmacist. That check runs before
anything else on every turn, so it holds even mid price check.

### Price trend and brand-vs-generic

Two analyses, unlocked the same as everything else by the upfront charge: price
trend history for a formulation, and brand-versus-generic comparison. Both are
computed straight from the NADAC file already cached - no separate charge, no
approval step, no new data source.

## Tests

```bash
source .venv/bin/activate
ruff format --check . && ruff check . && mypy .
pytest -q
```

183 tests, all offline — `TestModel`/`FunctionModel` for the Pydantic AI agents,
`httpx.MockTransport` for openFDA, and a SQLite store seeded with real rows from
the 2026-07-22 release including the metformin split. Coverage includes all three
tiers, the quantity gate, the combination-product exclusion, the label fallback
chain, shortage semantics, the medical boundary, quote comparison, card
navigation, and the paywall - a fresh session hitting the charge before any
intent processing, a decline hard-stopping and re-arming it, and the background
poller unlocking the session automatically once Stripe confirms.
`tests/conftest.py` refuses to run against a non-test Stripe key.

Two scripts check the live surface instead of mocks:

```bash
python verify_drugs.py   # clustering + openFDA resolution for all 30 drugs
python live_check.py     # every conversation path against the real APIs
```

## Adding a drug

Run `verify_drugs.py` first. It reports the formulation spread per strength and
which openFDA patient field the drug actually has. Both matter: the curated list
is short precisely because each entry has been checked, and a drug added without
that check can produce a confident, wrong single price.

Combination products are excluded by the `^DRUG-` filter in `drugs.py`. This is
not cosmetic — `AMLODIPINE%` matches 33 descriptions in the current file and 30
of them are combinations like `AMLODIPINE-ATORVAST`.

## Known limitations

By design, this example does not implement:

- **Open-vocabulary "any drug"**: the curated list is ~30 verified generics.
  Every multi-formulation generic risks the ambiguity metformin demonstrates, and
  each one needs the clustering check before it is trustworthy.
- **Combination products**: excluded in v1 for the same reason.
- **Pill photo / imprint identification**: NIH's Pillbox API was retired in
  January 2021 with no replacement, so there is no free government source to
  build against. Use a third-party imprint search instead.
- **Patient assistance program matching**: no public API exists, only paid data
  licensing.
- **Exact per-state dispensing fees**: CMS publishes real quarterly per-state
  figures, but as a formatted page rather than a queryable API. The agent quotes
  the $10–13 range and labels it as typical. Worth revisiting if an API appears.
- **Non-US pricing or regulation**: out of scope, matching both data sources.
- **Any live Stripe account**: real charges are intentionally impossible here.

## Notes on the data

- The NADAC CSV filename embeds a release date that rotates on CMS's schedule.
  It is always resolved from the dataset metastore, never templated.
- A row's `as_of` date can lag the file's own release by months, because NADAC
  carries a price forward for any NDC it did not re-survey that cycle. The
  current file has 29 distinct `as_of` values. Part of an apparent price gap
  between two formulations can be one side simply being staler.
- openFDA answers an empty result with HTTP 404 and an error body, so "no
  shortage" and "request failed" look alike unless you normalise it.
- The brand-versus-generic feature has an honest empty case for a curated
  drug whose brand simply isn't in the current NADAC file at all (confirmed
  live for Advil, Glucophage, Coreg), separate from CMS leaving the
  corresponding-generic-price field blank on a brand row that does exist -
  see [`ARCHITECTURE.md`](ARCHITECTURE.md) for the loading bug that used to
  make every drug look like the former.

## License

Apache 2.0. See the root [`LICENSE`](../../LICENSE) of the Innovation Lab repository.
