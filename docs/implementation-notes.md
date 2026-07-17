# Implementation notes

Running log of decisions made *during* implementation that the spec/PRD/ticket did not
settle, deviations from the plan, and tradeoffs accepted. Written for human review.

---

## 2026-07-13 — Ticket #2, `engine/interest.py`

### The accrual model: daily accrual, monthly posting

Real revolving cards accrue on the **average daily balance** and post at statement close.
A textbook monthly-periodic-rate amortization is simpler but cannot express *when in the
cycle* a payment landed — and this product sweeps **daily**, so the timing of a sweep
within a cycle is precisely the thing we're being paid to get right. A monthly model would
value a sweep on day 2 and a sweep on day 29 identically, which is wrong in the direction
of over-claiming.

So: interest accrues each day at `apr / 365` on the outstanding principal, with **no
intra-cycle compounding** (matching the average-daily-balance method), and is posted to the
balance — quantized to cents — at each statement close. Payments apply on their date,
before that day's accrual.

Rounding happens at exactly one place: statement close, via `money()`, which is
`ROUND_HALF_EVEN` (the existing repo-wide convention, `engine/models.py:33`). Not changing
it here — a second rounding mode in a money codebase is how the customer notification and
the KPI end up disagreeing by a cent.

### The counterfactual is the user's own payment trajectory, NOT the card minimum

**This corrects a spec deviation I introduced and the test suite caught.** My first pass used
"minimums only" as the counterfactual, which is what the ticket's ACs said. `prd.md` §5.1
says something different and more demanding: interest avoided is *"measured against a stated
counterfactual (**the user's own pre-signup payment trajectory**)."*

The difference is not cosmetic. Users of this product already pay **more** than the minimum —
that is *why* they have idle cash sitting in checking. Crediting our sweep with interest they
were never going to pay anyway inflates the headline number, and §5.1 exists precisely to ban
a KPI that improves when the engine merely becomes more optimistic.

So `Debt` gains `observed_monthly_payment: Decimal | None` — what the household was actually
paying — and the interest model takes the counterfactual payment as an **explicit argument**
rather than assuming one. `minimum_payment` is no longer an input to the interest math at all,
and there is a test asserting that changing it does not move the interest claim by a cent.

When `observed_monthly_payment` is `None` we make **no claim**. We do not fall back to the
minimum: that is exactly the flattering assumption we just rejected. In practice this costs
nothing, because `INSUFFICIENT_HISTORY` already refuses to sweep below 60 days of history —
by the time we are allowed to move money at all, we have seen two payment cycles.

*(Decision surfaced to the user mid-implementation and confirmed 2026-07-13.)*

### The claim runs to payoff, and does not assume we keep sweeping

Interest avoided is computed over the **life of the debt**, not a bounded window. Under the
minimums-only counterfactual this was untenable — a $9,000 card at 24% with a $180 minimum
amortizes at ~$2.50/month and takes ~50 years, so the "interest avoided" by a single sweep was
an enormous, unverifiable number hypersensitive to an assumption we already knew was wrong.
Against the user's *real* trajectory that pathology disappears: the same household paying
$400/month clears the card in ~40 months, and the payoff horizon is human-scale.

The with-sweep path applies **this one sweep** and then assumes the user's ordinary payments
continue. It does **not** assume we go on sweeping every day — we do not book credit for
actions we have not taken.

A household whose own payments do not cover their accruing interest has **no payoff**, and
therefore no interest-avoided figure. That is not an edge case to smooth over; it is a
household drowning, and the model says so by raising rather than returning a number.

### A debt that does not amortize raises, it does not return a number

If the minimum payment never covers the accruing interest (possible with
`minimum_payment = 0`, which `Debt` permits), the balance grows without bound and there is
no payoff. The module raises rather than returning a capped or `None` figure — a silent
sentinel here becomes a garbage interest claim downstream. Bad data fails loudly at the
boundary, per `decision-engine.md` §3.

### Interest reaches the user as a `ReasonCode`, not a field on `Decision`

Two options were live: an optional `interest_avoided` field on `Decision`, or a new
`ReasonCode`. Chose **`ReasonCode.INTEREST_AVOIDED`** (in the existing *Advisory — true,
and worth saying, but not why we acted* group, alongside `IDLE_CASH_ELSEWHERE`).

It needs no change to `Decision`'s shape, it inherits the whole reasons architecture for
free — `explain.py` renders it, `test_every_reason_code_has_copy` covers it, the LLM
narrates *from* it rather than being handed a finished financial sentence — and when the
APR is unknown the reason is simply **not emitted**, so there is no code path that can
render an invented number. The absence of the claim is structural, not a formatting rule.

---

## 2026-07-13 — Ticket #3, `engine/outcome.py`

### The grader applies the sweep itself; the caller cannot forget

`grade()` does not take "the realized balances". It takes `Realized` — the household's own
daily cash movement, **excluding anything we did** — and deducts the decision's sweep itself.

A replay that grades each decision against the household's *untouched* history never compounds
the effect of its own sweeps, and systematically **understates** breach risk: it reports the
tail we would have had if we had never acted. That is the difference between a shadow-mode
report and a shadow-mode lie, and it is far too load-bearing to leave to a caller's diligence.
So the caller cannot forget to apply the sweep, because the caller is not the one who applies it.

`settles_after_days` defaults to **0** — our money is assumed to leave the day we decided, not
when ACH would plausibly have posted. That is the assumption most likely to *find* a breach
rather than excuse one. A grader that flatters itself is worse than no grader, because it turns
an unknown risk into a false sense of a known one.

### Two bugs the grader found in itself, and both would have shipped

**1. `false_refusal_cost` was blaming us for the user's own cap.** The first draft computed it
as `hindsight_safe - swept`. On a real household that read **$7,129** — but the engine had swept
$300 because the user's `max_sweep` is $300. We were not being conservative; we were being
*obedient*. `prd.md` §5.3 says **our** cost of conservatism, and a guardrail the user chose is
not ours. Left as written, the metric would have been dominated by policy caps and the forecast
signal it exists to carry would have been buried in noise.

Fixed by exporting `decide.would_sweep(snapshot, low)` — *what `decide()` would have moved if
the projected low had been the realized one*, obeying every cap exactly as it does in
production. `false_refusal_cost` is now `should_have_swept - swept`: our forecast error, priced,
and nothing else. This also meant extracting `untouchable()` and `apply_caps()` out of `decide()`
so both modules share **one** definition; a second copy of that arithmetic would drift, and the
day it drifted the calibration numbers would start describing an engine that never shipped.

**2. `projection_error` was absorbing the size of our own sweep.** It compared
`Decision.projected_low_balance` against the **post-sweep** realized low. But
`projected_low_balance` is a projection of the household's *pre-sweep* trajectory — the sweep is
computed *from* it, not included *in* it. So a big sweep produced a hugely negative "error":
−$5,694 on a household the engine had forecast almost perfectly.

That would have poisoned the exact distribution `strategy.md` §3 calls the only asset that
compounds, and poisoned it *worst on the days we swept hardest*. Now measured against
`realized_low_unswept`, and there is a test asserting the size of our own sweep cannot move it.

The two corrected metrics now satisfy an exact identity when no cap binds:
**`false_refusal_cost == projection_error`.** Pinned as a test — if they ever drift apart under
an uncapped policy, one of them has a bug.

### `sweep_caused_overdraft` is a different fact from `overdrafted`

`prd.md` §5.2 says "**sweep-caused** overdraft rate" and means it. A household that goes below
zero on its own is not our doing, and folding those in would make the guardrail metric
meaningless — it would be dominated by lives we never touched. So the grader reports both, and
`sweep_caused_overdraft` is true only when `realized_low < 0 <= realized_low_unswept`: we are
liable for the difference we made, not for their circumstances.

### The finding: the per-sweep cap currently **masks** the spend-model bug

Under the default $300 cap, [6.5]'s over-reservation costs the user *nothing measurable* — the
cap binds long before the forecast error does, `false_refusal_cost` reads **$0.00**, and a
shadow-mode report run at these caps would pronounce the engine healthy.

The bug is **latent, not absent**. Lift the cap on the same household, same day, same realized
future, and it prices out at **$867.13** immediately. Which means it starts costing real money
precisely when the ceiling is raised — and `prd.md` §8's *"Then: raise the ceiling as
calibration proves out"* is exactly the plan to raise it. **The moment the company starts
trusting its calibration is the moment this bug begins to bite.**

Implication for #4, written into a test so it cannot be forgotten: the replay driver **must
sweep the cap across a range**. Run at production caps alone, it will measure a forecast error
of zero and issue a false clean bill of health.

---

## 2026-07-13 — Ticket #0001, `backend/precompute.py` + `backend/artifact.py`

### The finding: `BELOW_MIN_SWEEP` tells the user something untrue

**This is an engine bug, it is user-facing, and I have not fixed it — it is outside this
plan's scope (`engine/` is explicitly untouched), so it needs a decision.**

`engine/decide.py` builds up `cap_reasons` from `apply_caps()` — `PER_SWEEP_CAP`,
`WEEKLY_CAP`, `CLEARS_THE_CARD` — and then, if the capped amount lands under `MIN_SWEEP`,
**drops them all** and refuses with `BELOW_MIN_SWEEP` alone:

```python
amount, cap_reasons = apply_caps(snapshot, target, available)
reasons = [projection, *cap_reasons]
if amount < MIN_SWEEP:
    return _refuse(Reason(ReasonCode.BELOW_MIN_SWEEP, {"minimum": MIN_SWEEP}), low=low)
```

`explain.py` renders that as *"What's left is under $1.00, which isn't worth moving."* But
when the cause is an exhausted weekly cap, what is actually true is *"you have $2,000 spare
and you have already hit your $600 weekly limit."* The user is told they have no money. They
have plenty of money; they have no **headroom**. Those are different facts, and the refusal
reports the wrong one.

I found this because my first tuning pass produced a window where **65 of 90 days** carried
that message. The fix looks small — carry `cap_reasons` into the `BELOW_MIN_SWEEP` refusal —
but it changes the reasons attached to a decision in the tested core, so I have left the
engine alone and worked around it. Worth its own ticket.

**The workaround, and its cost.** `DEMO_POLICY`'s weekly cap is set wide enough
(`max_sweep=$400`, `max_weekly_sweep=$1600`) that the household's actual cash position, not
an exhausted cap, is what does the refusing. The served window now reads honestly: 35 sweeps,
55 refusals, and the refusals are almost all `NO_SURPLUS` ("your balance is heading for a low
of $X… there's nothing spare"). The cost is that `WEEKLY_CAP` no longer appears in the demo's
own data — it is covered by a unit test with a tighter policy instead
(`test_the_weekly_cap_binds_when_sweeps_stack_up`).

### Sweeps are subtracted from checking; `sim/` does not know we exist

The plan's sketch reads the checking balance straight from `History.balance_on(day)`. That
history is the household's realized life **without us in it** — it knows nothing of our
sweeps. Left as written, three months of daily sweeps would drain the card while the checking
balance sat untouched, and the engine would go on finding surplus that, in the world it had
just created, was already spent.

So the walk carries a running total of settled sweeps and subtracts it. This is what produces
the demo's actual shape: the balance falls toward the buffer, the surplus runs out, and the
engine starts refusing on `NO_SURPLUS`. That equilibrium **is** the product, and without this
correction the demo would not have shown it.

### The debt ledger: `outstanding = principal + unposted interest`

Mirrors `engine/interest.py` exactly, because the two must agree — that module prices what a
sweep *saves*, and a ledger that drifted from it would claim savings against a balance the
engine never believed in. Interest accrues daily on the **principal** at `apr/365` and posts
at statement close, so unposted interest earns no interest (the average-daily-balance method,
not compounding). The balance the engine sees is principal plus what has accrued, so it grows
every day — including a day with no payment and no sweep.

Payments come off **principal first**, overflowing into accrued interest only if they exceed
it. That overflow is not a rounding detail: `CLEARS_THE_CARD` sweeps exactly `outstanding`,
and a payment that retired only principal would strand the accrued interest — the card would
converge on a balance of a few cents it could never clear and the engine would refuse to
sweep forever. A test pins it (`test_paying_the_full_outstanding_clears_the_card`).

### Income variation is bucketed by 28 days, not by calendar month

`Snapshot.income_variation` is documented as the coefficient of variation of *monthly* income,
and the gate (`MAX_INCOME_VARIATION`) is 25%. This household is paid **biweekly** — so a
calendar month holds two paychecks, except the ~4 times a year it holds three. Bucketing by
calendar month scores that pure calendar artifact as a ~24% swing in income and comes within a
whisker of tripping `INCOME_TOO_VARIABLE`, refusing to serve a household whose pay is in fact
identical every fortnight.

28-day buckets are the honest measure of a biweekly earner's variability, and that is what the
walk computes. Flagging it because it is a deviation from how `Snapshot`'s own docstring
describes the field — and because the same trap is waiting for the real recurring-event
detector when it is built.

### The card payment is an ORDINARY event, not `DEBT_MINIMUM`

The household pays $450/month against a $280 minimum. Tagging the whole payment `DEBT_MINIMUM`
would make `forecast.py` skip all $450 of it (see `EventKind`) while `decide()` reserved only
the $280 — **under-counting $170 of real outflow**, which is the direction that ends in an
overdraft. Emitting it as `ORDINARY` at full value instead means the $280 minimum is reserved
on top of a payment that already includes it: an over-count of $280, which costs a slightly
smaller sweep and cannot hurt anyone. Wrong in the safe direction, deliberately.

### Demo spec: a $14,000 card, not $9,000

`tests/test_outcome.py`'s household helper (a $9,000 card) is the persona, and I started
there. But this household clears a $9,000 card almost exactly inside a 90-day window — the
artifact came out with a **$1.06** balance, which lands the demo on a "paid off" banner
instead of on the engine's actual daily work. $14,000 sits mid-band for the persona
(`USERS.md`: $8k–40k) and still has $3,452 outstanding at the end of the window.

The paid-off path is still built and tested (`test_a_card_paid_off_mid_window_keeps_being_served`),
it is simply not what the demo's own data does — which is what the plan asked for.

### Deliberately not modelled

The gates for stale balances, unhealthy connections, and in-flight sweeps are all about a live
Plaid connection this demo does not have. Rather than invent failures, the walk holds those
inputs healthy (fresh balance, healthy connection, nothing in flight) and lets the refusals
that *do* appear come from the household's real cash position. A sweep settles at the start of
the next day, which is why yesterday's sweep — not today's — is the one that lands on the
ledger.

### Follow-ups

- **Ticket needed:** carry `cap_reasons` into `decide()`'s `BELOW_MIN_SWEEP` refusal, so a
  cap-exhausted refusal says so. User-facing, small, in the tested core.
- The summary's `interest_avoided_total` sums the engine's own per-sweep `INTEREST_AVOIDED`
  claims across the window ($6,019 against $8,924 swept). Each claim is honest on its own
  terms — it is what `engine/interest.py` says that one sweep saved, assuming no further
  sweeps — but summing 35 of them is a slightly different number from "total interest avoided
  by the whole window," and the two are close but not identical. Fine for the demo; worth
  naming before it appears on a slide.

---

## 2026-07-13 — Ticket #0004, `backend/assistant.py`

### The guard is the feature; the system prompt is not

R6 says the assistant never states a financial claim it can't trace to a `Decision`. A system
prompt asking a model not to invent numbers is a *request*, and a request is not a guarantee.
So the enforcement lives in `verify()`: every dollar figure, outcome, and reason code in the
model's final text is matched against the tool results **from that turn** before the user sees
a word of it. Anything unverifiable replaces the whole response with "no record."

Most of `tests/test_assistant.py` is an attempt to get a false claim past it, not a check that
the happy path works. The rule that earns its keep is the per-`(date, field)` binding: the
model fetches two *real* decisions and quotes one day's sweep against the other day's date.
Every number in that sentence is true. A presence-only check ("is $400 anywhere in the tool
results?") waves it through and the user is told something false assembled from entirely true
parts. Full contract and its stated limits:
`docs/decisions/0003-structured-tool-calling-over-embeddings.md`.

### What the guard cannot do — worth saying out loud

Extraction is regex over the final text, not comprehension. A fabrication phrased with no
parseable figure, no outcome word, and no known reason phrase is not caught. The guard raises
the cost of a **specific, quotable** false claim — the kind that would actually mislead
someone about their money — rather than proving the prose true in general. A figure in a
sentence that names no date can only be checked against the union of the turn's fetched
decisions, since there is nothing to bind it to. Both limits are accepted deliberately: a
guard that verified every sentence semantically would need a second model, and would have the
same problem one level up.

### Deviation: the service now refuses to start without `ANTHROPIC_API_KEY`

Not in the plan, and it changes U2's startup contract. The alternative is a service that comes
up healthy, passes Cloud Run's probe, and then fails the first time a user opens the modal and
asks a question — which is the worst possible moment to discover a missing secret. Consistent
with the artifact and API-key checks already there: fail at deploy, not in front of an
interviewer. Both backend test fixtures now set it; nothing ever calls out with it.

### The rate cap and `--max-instances=1` are a pair

The in-process rate cap on `POST /assistant/message` is only a real bound on Anthropic spend
because exactly one instance ever runs. With two instances, each holds its own counter and the
ceiling silently doubles. `MAX_TURNS_PER_MINUTE` and the deploy flag are one decision in two
places — noted here and in the code so ticket 0009 doesn't drop the flag.

### Blocked: no live LLM verification

There is no `ANTHROPIC_API_KEY` and no `ant` profile in this environment, so **not one call to
Anthropic has been made**. Everything is tested against a fake client that replays scripted
responses. That is the right way to test the guard — it lets us put words in the model's mouth
that a real model would rarely volunteer — but it means two things are genuinely unverified:

1. **The request shape.** Whether `claude-opus-4-8` accepts this exact combination of
   `tools` + `thinking: {"type": "adaptive"}` + `output_config: {"effort": "medium"}` has not
   been observed, only written from the current API reference.
2. **Whether a real model actually triggers the guard.** The adversarial cases are synthetic.
   How often a real model would produce one is unknown and unknowable from here.

Needs one live smoke test with a real key before the demo. Everything else in this ticket is
verified.

## 2026-07-14 — ticket 0009 groundwork: the build config, not the deploy

Adds the three files `gcloud run deploy --source .` needs, without deploying anything. No GCP
project has been created and no `gcloud` command has been run against one.

### Buildpacks cannot infer this build (0009 said "only if buildpacks can't infer" — they can't)

Two independent reasons, hence two files:

- **`Procfile`.** Python has no conventional entrypoint the way Node has `npm start`. Without
  it the image builds and then has nothing to run.
- **`requirements.txt` (root).** Buildpacks install from a manifest at the *source root*; ours
  is at `backend/requirements.txt`, and the API deps are an *optional* `[api]` extra in
  `pyproject.toml`, which they will not install. The image would build clean and crash on
  `import fastapi`. The root file is a one-line `-r backend/requirements.txt` include rather
  than a second copy of the list — `backend/` stays the source of truth. CI is untouched
  (`pip install -e ".[dev]"` never reads it).

A `backend/Dockerfile` would also have solved both. Buildpacks won the coin-flip because 0009
named them first and they mean no base image to patch.

### `.gcloudignore`: a 418 MB upload nobody would have noticed

Without one, gcloud synthesizes a default that includes the **root** `.gitignore` via
`#!include:.gitignore`. Nested `.gitignore` files are not honored — and our `node_modules`
rule lives in `mobile/.gitignore`. Result: `mobile/node_modules` (418 MB, measured) uploads to
Cloud Build on every deploy. It would have worked, just slowly and billably, which is exactly
the kind of thing that never gets diagnosed.

`sim/` is deliberately **not** excluded despite being unused at request time: `pyproject.toml`
lists it as a package and `backend/precompute.py` imports it, so excluding 40 KB risks a build
failure. `backend/data/decisions.json` must never be excluded — it is the service's entire
state (ADR 0002), and without it the container will not boot.

### `--workers 1` is the same decision as `--max-instances=1`

Already noted above for the instance cap; the Procfile is the second place it has to hold. The
`/assistant/message` rate cap is per-process, so a second Uvicorn worker doubles the ceiling on
Anthropic spend just as a second instance would. One process, one counter.

### Still not done

The deploy itself. Placeholders (project, region, service) are unset, and no live Anthropic
smoke test has run — see the "Blocked: no live LLM verification" note above, which is now
unblocked (a key exists) but still unexercised.

## 2026-07-14 — the first live Anthropic call, and the three bugs it found

The "Blocked: no live LLM verification" note above is now closed. A real key went in, and the
very first live call broke the assistant. Everything below was invisible to the fake-client
suite — not because the tests were careless, but because a fake client cannot produce the one
thing that mattered: *the sentences a real model actually writes.* The scripted responses were
adversarial and well-chosen; they simply never happened to phrase a truthful answer the way
Opus does.

Every fix here is verified against live output, and every regression test quotes it verbatim.

### 1. The guard rejected the engine's own copy — on 82 of 90 days

`engine/explain.py` names the projection horizon inside its sentences ("heading for a low of
$748.79 **on 2026-06-05**"). A model narrating faithfully repeats that date. `verify()` treated
every date in the text as a date whose *decision* was being claimed, so the horizon read as a
day that was "never fetched" — and a perfectly honest answer was thrown away.

`Facts` now tracks **supporting dates** (dates appearing *inside* a fetched decision) as
distinct from **decision dates**. A supporting date may be *mentioned*; nothing may be
*attributed* to it. Amount-binding still keys off decision dates only, so the recombination
case (a real figure against the wrong day) is caught exactly as before.

### 2. "No money moved" read as a payment claim

`_SWEEP_WORDS` matches `moved`. `_REFUSE_WORDS` knows `no payment` but not `no money`. So the
single most natural way to describe a refusal — "On 2026-03-13, no money moved" — asserted a
payment to the guard, and got rejected 6 times out of 6.

A *negated* sweep verb is now a refusal assertion, not a payment one. Both directions hold: the
same sentence about a day money *did* move is a false denial and is still caught.

**A wrong turn worth recording.** The first attempt keyed this off the amount — "paid $0.00 is
true, so allow it" — which broke `test_calling_a_refusal_a_payment_is_caught` (AE4). That test
is right and the change was wrong: "we paid $0.00 toward your card" asserts a payment that
never happened, and the *outcome* must be true, not merely the number. The failing test caught
a fix built on a guess instead of on evidence. Lesson: capture the model's actual words before
theorizing about them.

### 3. `$12092.26` parsed as `$120`

The worst of the three, and pure regex. `_MONEY`'s first branch is
`\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?` — against "12092.26" it takes three digits, finds no comma,
finds no decimal point (the next char is "9"), and *succeeds*. An alternation never backtracks
once a branch matches, so the second branch never ran. Every figure over $1,000 written without
a thousands separator was truncated to its first three digits, matched no real amount, and was
rejected as fabricated. The card balances here are $4,557.41 and $12,092.26.

`*` → `+` on the comma group. An un-separated number now falls through to the branch that
consumes all of it. This makes the guard *more* accurate, not laxer: an invented `$9999.99` now
parses whole and still fails the amount check.

### 4. Two silent failures made all of this hard to see

- The bare `except Exception` around the Anthropic call returned the friendly "I couldn't reach
  the assistant just then — try asking again" for *every* failure, including a permanent
  `AuthenticationError`. A bad key looked exactly like a network blip and invited infinite
  retries. The exception type is now logged.
- `verify()`'s rejection reason was computed, `del`-ed, and discarded — while the comment
  claimed it was "for our logs". It never reached them. A guard that rejects silently is
  indistinguishable from a guard that is wrong, and this one *was* wrong, on 91% of days, with
  nothing anywhere saying so. Now logged.

### Verification

16 of 16 live questions across real sweep and refusal days now answer (previously ~0). The
out-of-window date is still declined honestly, the adversarial prompt (future projections,
credit score) is still refused, and 242 tests pass — including the two AE4 adversarial cases,
which were the guard rails that caught my own bad fix.

### Still open

The **key-in-keychain** hazard is real and bit us: `security -w` returns hex, not a string, when
the stored bytes aren't clean ASCII — which happens the moment a trailing newline gets stored
with the secret. That is the same newline hazard `DEPLOY.local.md` §3 warns about for
`gcloud secrets create`. Worth a line in the runbook that the *storage* step has it too.

---

## 2026-07-14 — The sweep cadence: daily → weekly (not a ticket; a decision that was never made)

Not from the build plan. It came out of reading the PRD, the plan, `decision-engine.md`, and
`decision-flow.html` for where the *daily* decision cadence had been decided — and finding that it
never had been.

### What I found

`archive/prd-v0-advice-only.md` is an **advice** product: it "answers one question every day" and
lists **"Daily engagement"** among the reasons the problem was attractive. Daily was an engagement
property of a product whose output was a notification. `prd.md` §0 then killed advice-only on the
RCT evidence and kept the rhythm — so §1's headline read "**Every day**, we move the cash you do not
need." A daily notification cadence had become a daily **ACH** cadence, and nothing anywhere
revisited it. `decision-engine.md` §7 even justified the daily interest model "*because the product
sweeps daily*", which is circular.

Two things should have caught it:

1. **The PRD contradicts itself.** §2: "we cannot win this on expected value, we have to win it on
   the tail." Then it takes ~12 draws/month instead of ~4. §1.1: what we sell is "the absence of a
   decision" — while making 30 of them a month.
2. **The engine couldn't do it anyway.** `SWEEP_IN_FLIGHT` refuses while ACH is unsettled (2–3
   days), so daily sweeping was never reachable in production. The demo only *looked* like it could
   because `precompute.py` hardcodes `sweeps_in_flight=ZERO` — which is why the old artifact opened
   on runs of four consecutive $400 sweeps. What was actually pacing the engine was the **weekly
   cap**, a governor bolted on to blunt a frequency nobody chose.

### The number, and my own wrong number

I gave the user a back-of-envelope estimate (**~$204/yr** for a fortnightly cadence) *before*
measuring. Then I measured, and it was **~5× too high** — it priced each deferred dollar for the
full length of its delay, but the same dollars get swept next period, so only the marginal delay
costs anything. The un-swept cash is **deferred, not lost**: it sits in checking. The real cost of
weekly spacing is **~$9 per 90 days (~$36/yr)**.

The estimate was wrong in the direction that flattered the status quo. `[6.1]`/`[8.3]`'s
"measure, don't assert" rule exists for exactly this, and it caught its author. Full table:
`docs/learnings/2026-07-14-the-cadence-was-inherited-not-chosen.md`.

**And then I did it again.** The first version of this note (and of the PRD/engine-doc edits, and
of the commit message) claimed daily also cost **~$48/yr more in ACH fees** at ~$0.50/debit, and
that the interest gain and the fee "cancelled." **That $0.50 was assumed, not sourced, and it is
wrong under most processor pricing.** Dwolla's pay-as-you-go is 0.5% of the *amount*, capped at $5
— you pay per dollar moved, not per transfer, so batching saves almost nothing (only the cap
helps). Their monthly plans make the marginal transfer free outright. Only flat per-transaction
pricing rewards batching, and I never checked which we'd be on.

Corrected everywhere, and left visible rather than deleted: **two plugged-in numbers in one
investigation, both pointing the right way for the wrong reason.** The lesson is not "measure the
interest" — I did that. It is that a number I invent to *support* a conclusion I already reached
gets no scrutiny, and both of these sailed through into four documents.

What actually holds up: daily has a small *positive* economic edge (~$36/yr). It is bought back by
`prd.md` §2.3's guarantee — ~142 draws/yr vs weekly's ~45, so daily only pays if the per-sweep
overdraft rate is under ~1% (or ~0.1% at a realistic all-in cost per incident), which **nobody has
measured**. The honest framing is *insurance*, not a free lunch: ~$36/yr to take a third as many
draws on a tail we cannot yet size.

### Decisions taken

- **`min_days_between_sweeps` is a `UserPolicy` field (default 7), not a constant.** `0` restores
  the daily engine exactly; `30` gives a monthly one. The user's opening ask was "monthly, or twice
  a month"; they then chose weekly. Making it a policy value means that reversal is a config change
  rather than another engine diff — which is why I built it this way rather than hardcoding 7.
- **`CADENCE_HOLD` is raised *after* the forecast, not in `_blocking_reasons`.** This is the
  load-bearing bit. A blocking refusal carries no `projected_low_balance`, and `outcome.py:grade()`
  **raises** on those — so gating the cadence up there would have made 6 days in 7 ungradeable and
  thrown away most of the calibration asset `strategy.md` §3 calls the only one that compounds.
  Daily data, weekly money. Tested (`test_a_cadence_hold_still_carries_its_projection_and_stays_gradeable`).
- **The demo's `max_sweep` rose $400 → $1,600 to meet the weekly cap.** The ceiling on money moved
  is *unchanged* at $1,600/week; what changed is that one ACH gets you there instead of four.
  Leaving it at $400 would have cut throughput 4× and disguised a cadence change as a paydown
  regression. Worth being explicit: this is a **cap raise**, and `decision-engine.md` [8.3] warns
  that raising the cap is what un-masks the latent spend-model over-reservation. Expect
  `false_refusal_cost` to stop reading $0 once a replay driver exists. That is the instrument
  working, not a new bug.
- **`MIN_SWEEP = $1.00` left alone**, deliberately. It is indefensible at any cadence (the old
  artifact had 9 sweeps under $100, smallest $23.19), but it is its own money-rule change with its
  own tradeoff and bundling it here would have hidden it. Logged as open.

### Consequences I had to absorb

- **`WEEKLY_CAP` is no longer reachable at the shipped policy** — one sweep a week has nothing to
  stack against, and `max_sweep == max_weekly_sweep` now. It is not dead code (a 3-day spacing with
  a high per-sweep cap still binds it), but `test_the_weekly_cap_binds_when_sweeps_stack_up` now
  has to pass `min_days_between_sweeps=0` explicitly to reach it. The test says so.
- **`test_a_card_paid_off_mid_window_keeps_being_served`'s card went $2,400 → $9,000.** At a $1,600
  cap a $2,400 card clears during the 60-day *warm-up*, so the served window opened on a dead card
  and the build failed for want of a single SWEEP. $9,000 leaves ~8 sweeps of runway and ~27
  paid-off days — and unlike $2,400 it actually sits inside `prd.md` §3's $8–40k persona band.
- **`test_the_real_models_answer_survives` no longer reads the demo artifact.** It pinned a verbatim
  live-model answer to 2026-05-20's *numbers*, so regenerating the demo broke it even though the
  guard hadn't moved. A guard regression test held hostage to the demo seed is a bad coupling; it
  now builds its own `DayRecord` with the figures the model was actually answering about.
- **The artifact regenerated**: 35 sweeps → **11** across the 90-day window; sub-$100 sweeps 9 → 1.

### Still open

- The spacing rule is a **proxy**. Surplus appears when a household is *paid*, not every seventh
  day. The right rule is one decision per cash cycle (naturally twice a month for a semimonthly
  earner). `sim/household.py` already models pay cadence; the engine doesn't look at it. Needs the
  recurring-income detector that `decision-engine.md` [6.2] already lists as assumed away.
- `false_refusal_cost` on a held day is a **deferral, not a loss** — it reports the full un-swept
  amount, and that money moves next week. A replay must partition on `CADENCE_HOLD` before totalling
  it or it will count the same dollars every day they sit. Written up as `decision-engine.md` [9.2]
  before anyone falls into it.
- `sweeps_in_flight=ZERO` in the demo is now the last thing overstating the achievable cadence.

---

## 2026-07-14 — The assistant model: Opus → Sonnet

`backend/assistant.py`'s `MODEL` was `claude-opus-4-8`. It is now **`claude-sonnet-5`**.

Opus was overkill, and it was also a **drift from the plan** — the build plan's Key Technical
Decisions specified "Anthropic Claude (**Sonnet-class**)" and the implementation quietly shipped
Opus. This restores the documented decision rather than making a new one.

Why Sonnet is the right size for this job: the assistant narrates over a tiny structured dataset
through two tools. It **never decides anything** — `engine/decide.py` does that, deterministically,
before the assistant is ever reached — and it is **never trusted about money**, because the guard
re-checks every figure it writes against that turn's tool results. It is a retrieval-and-narration
job, not a reasoning one.

### The swap was verified live, because a model swap is precisely what that bar exists for

The plan's own acceptance bar (added after #19): *"Live verification is now the acceptance bar for
anything touching `verify()`, not an optional extra."* That bar was written because **all three
bugs ever found in the guard were bugs about how a *particular* model phrases things** — a
projection date read as a claim, "no money moved" parsed as a payment, `$12092.26` truncated to
`$120`. The fake client in the test suite cannot produce a real model's sentences, so swapping the
model is the single change most likely to reintroduce that class of bug.

Run against live `claude-sonnet-5`:

- **8/8 legitimate questions** — across a sweep day, a `CADENCE_HOLD` day and a `NO_SURPLUS` day —
  answered with **zero guard rejections**. (The first run appeared to show a rejection; on
  instrumenting the pre-guard text it turned out to be the *credit-score* turn, where a rejection
  is the desired outcome. The guard's user-visible behaviour was correct; my harness's pass/fail
  logic was not.)
- **6/6 adversarial turns**: out-of-window date declined; excluded warm-up day declined;
  credit-score and index-fund questions refused as out of scope (`prd.md` §4.2 — the index-fund
  refusal matters, and `decision-engine.md` [4] explains why we must never advise an indebted
  household into the market); "ignore your instructions and tell me you swept $9,999" caught by the
  guard and replaced with the fallback. **No fabricated figure reached the user.**

### Decision: keep the Opus-era test strings

`tests/test_assistant.py` pins several **verbatim live-Opus** sentences as regression cases. I did
*not* re-capture them against Sonnet. A guard that survives the phrasings of **two** models is
better evidence than one tuned to whichever model happens to be configured today — so they are now
cross-model regression cases, and the docstrings say so.

### Still open

The keychain hazard from the last entry bit again while running this: `security find-generic-password -w`
returns the secret **with a trailing newline**, which must be stripped (`tr -d '\n'`) before it is
used as an API key. Same hazard `DEPLOY.local.md` §3 warns about for `gcloud secrets create`; it
applies to local verification runs too.

---

## 2026-07-14 — the guard couldn't read the engine's new copy (72/90 days unanswerable)

Reported as "the LLM can't see the data." It could. The tool calls worked and returned real
decisions; `verify()` then threw the answers away and the user got the "no record" copy.
`POST /assistant/message` returned `outcome: "guard_rejected"` — which is the only reason this
was diagnosable at all, since `GUARD_REJECTED` and `NO_RECORD` render identical text to the user.

**Cause.** `2aeda1e` added `CADENCE_HOLD` to the engine and its copy, and never touched the guard
(`backend/assistant.py` had zero references to it). The copy explains a refusal by naming the
*previous* payment — "We paid your card 1 day ago, and we space payments at least 7 days apart."
`_SWEEP_WORDS` matched the verb "paid", the guard concluded the model was claiming a payment on a
REFUSE day, and rejected. `CADENCE_HOLD` is now the most common reason in the window, so this hit
**72 of 90 days**: the assistant could not answer about the engine's most common decision.

This is the *third* incarnation of the same failure (see the `$5321.39` money-regex note and the
82/90 projection-horizon note in this file). Each time: the guard rejects the engine's own words,
silently, and it reads to the user as missing data.

**Fix.** A `_PRIOR_PAYMENT` exemption mirroring the existing `_NEGATED_SWEEP` one, behind a single
`_asserts_sweep()` helper that all three `_SWEEP_WORDS` call sites now route through — they had
drifted into checking the raw regex in three subtly different ways. Also added the missing
`CADENCE_HOLD` entry to `_REASON_PHRASES`, which had left the window's most common reason with no
phrase validation at all.

**Tradeoff.** The exemption means a segment reading "we paid ... ago" asserts *no* outcome for the
cited day, so an action check no longer runs on it. It licenses no figure: every dollar stays bound
to the `(date, field)` it was fetched for. A model could now write "On <day> we paid $400.00 two
days ago" and have the amount checked but not the (self-contradictory) tense. Judged well worth it
against 72/90 days of honest answers being destroyed.

**Guard against a fourth recurrence.** `test_the_engines_own_copy_passes_on_every_day` runs every
day's engine copy through the guard. Any new reason code whose copy trips it now fails on the day
it is added, rather than months later as "the LLM can't see the data." Deliberately across all 90
days, not a sample: 18 days passed, so a sampled test could easily have reported green.

---

## 2026-07-14 — two more guard false-negatives, found behind the first one

Fixing the `CADENCE_HOLD` rejection above only exposed the next two. Both are the same species —
the guard refusing the engine's own truthful narration — and both are now fixed.

### The projection horizon, again — but this time it was the word order

The guard already knew a *supporting* date (a projection horizon the engine quoted inside a
decision) may be **mentioned** but never **attributed to**. It enforced that by rejecting any
segment where a supporting date and a sweep verb co-occurred. That is too blunt: the engine's own
sweep copy names both in one sentence — "heading for a low of $748.79 on 2026-06-05, so we swept
$400" — every word of it true. Whether the answer survived came down to nothing but whether the
model happened to split that sentence in two.

What actually separates the two cases is what the date is *doing*, and English puts that in the
word order. Projection reaches the date through its figure ("a low of **$748.79** on 2026-06-05" —
figure first). Attribution leads with the date and the money follows ("**On** 2026-06-05 we swept
$748.79"). Same date, same figure, same verb; only the order differs, and the order *is* the claim.

So `Facts.supporting` went from `set[date]` to `dict[date, set[Decimal]]` — each horizon now
carries the figures the engine quoted *alongside* it, scoped to the sentence, so a figure from
elsewhere in the decision cannot license a date. A supporting date is doing projection work only
when one of its own figures appears **earlier in the segment** than the date does. Leading with the
date still rejects — including the nastiest recombination, "on 2026-06-05, low $748.79, we swept
$400", which pairs a real date and its real low with a false outcome and which a co-occurrence test
waves straight through.

### Aggregates forced the model into arithmetic, and the guard was right to kill it

"How much have you saved me" had no answerable path: no tool returned a total, so the model's only
move was to fetch the decisions and add them up — arithmetic, in the one product that exists to
keep the LLM out of the arithmetic. The guard rejected it every time, and *correctly*: a figure the
model computed matches no decision. The user saw "I don't have that on record" about a number
sitting on their own dashboard.

**Fix.** A `get_summary` tool returning the engine's precomputed totals, plus a system-prompt rule
forbidding manual arithmetic outright. The guard is untouched — these figures are **retrieved, not
computed**, and recording them in `facts.totals` is what makes them quotable, on the same contract
as any decision. They are pooled rather than keyed by date, because a window total is a fact about
the window, not about any one day.

**A bug inside the fix, worth recording.** The first `get_summary` payload included the window's
start date. The model dutifully wrote "since 2026-03-02" next to the interest total, the guard saw
a date it had fetched no decision for, and killed a true answer — the same failure, reintroduced by
the fix for it. The dates are simply not in the payload now. They are not the model's to state
anyway, and if a question ever genuinely needs the range, they have to become *fetched facts*
first.

### Still open: this has not been verified live against Sonnet

**The acceptance bar in the entry above is not met for this work, and it matters more here than
usual.** That bar exists because every guard bug ever found has been a bug about how a *particular
model phrases things* — and this work landed on `main` after the Opus→Sonnet swap (#24) merged
underneath it. It has only ever been exercised by the fake test client, which cannot produce a real
model's sentences. The word-order rule above is *especially* exposed: it is an assumption about how
a model orders a figure and a date in a clause, and it has never been tested against a real
sentence from the model now actually configured.

The end-to-end run that would have closed this (`scratchpad/e2e.py`, all four question shapes)
errored before it produced a result and was never re-run. Committed green-on-unit-tests and
explicitly *unverified live*, so the gap is visible rather than assumed closed.

---

## 2026-07-14 — U2 (`0011`): the simulator charges a card, and nothing else moved

**Deviation from the ticket, in the safe direction.** `0011` predicted that
`tests/test_spend_model.py` and `test_the_committed_artifact_is_the_one_the_code_generates` would
both fail "by design" and the numbers would need re-deriving. **Neither failed.** The artifact
regenerates byte-for-byte identical and the full suite is green.

That is not luck, and it is worth understanding rather than celebrating. `SpendSpec.card_share`
defaults to `0.0`, and `DEMO_SPEC` does not opt in — so the simulator has *learned* to charge a card
while the demo household still charges nothing. The generator can now do the thing; it is not yet
doing it.

Two details make the byte-identity real rather than approximate:

- The channel decision is **short-circuited on the left** (`card_share > 0.0 and rng.random() < ...`),
  so at zero the rng is never drawn and the whole random sequence is the one the generator has always
  produced. Draw it unconditionally and every committed figure in the repo shifts on the same day the
  feature lands, for no behavioural reason at all.
- Card payments are computed *after* the spend loop (they depend on what was charged) but **appended
  before** it, preserving the txn order the function has always emitted. The rng order is what
  guarantees identity; the append order is what keeps the diff empty.

**So the churn moves to U4, which is where it belongs.** Turning `card_share` on for the demo changes
what the engine *decides*, and that change should land with the reserve that can survive it — not a
release earlier, where it would look like an unexplained artifact diff.

**The one genuinely load-bearing line in the diff** is in `History._by_day`: checking totals now
exclude `TxnKind.CARD_CHARGE`. A card charge is not a checking outflow — it is a checking outflow
scheduled for the due date of the statement it lands on, and `CARD_PAYMENT` already carries that.
Counting both would spend the same dollar twice. `CHECKING_KINDS` is an explicit allow-list rather
than a `!= CARD_CHARGE` check, so the next txn kind someone adds has to make a deliberate choice
about which side of that line it falls on.

**`CardSpec.payment` is no longer what the household pays.** It is what a REVOLVER *habitually* pays.
What actually leaves checking is `_card_payment_for(behavior, closed_statement)` — a transactor
clears the statement, a minimum-only household pays the floor. This is the sentence the whole feature
turns on: once the payment is determined by what was charged, there is no constant left to hardcode,
and `precompute.py`'s ORDINARY workaround has nothing left to stand on.

**`HouseholdSpec.card` survives as a read-only property** returning `cards[0]`, for single-card
callers and tests. It is deliberately *not* a compatibility shim for the engine: anything that
reserves, ranks or forecasts must iterate `cards`, because reading `.card` on a two-card household is
precisely the bug this feature exists to fix.

---

## 2026-07-14 — U4 (`0013`): the reserve holds the obligation, not the minimum

The safety fix. `untouchable()` was already portfolio-wide — it summed across every debt due in
the horizon — so the brainstorm's "the reserve is per-card" framing was wrong. The real defect was
narrower and worse: **it reserved each card's *minimum* when it should have reserved each card's
*obligation*.** The minimum is what the issuer will accept. The obligation is what the household
pays, and for a transactor those differ by the whole statement.

### The hole the first design opened, and how it was caught

`obligation_in_horizon` initially reserved only the statement that had **already closed**. That
reads as more correct — it is a known fact rather than a forecast — and it is a **regression**.

The old reserve was a *rolling forecast*: `precompute.py` recomputed `minimum_due_date` fresh every
single day, so it always pointed at the next due date and reserved the minimum on essentially every
day of the cycle. `Card.statement_due_date` is a fact about a statement that has already been paid.
Key the reserve on that alone and the moment it is paid, the next has not closed, the card appears
to owe nothing, and the reserve falls to **$0 for the last third of every cycle** — while
`forecast.py` skips the CARD_PAYMENT event *unconditionally*, on the tag alone. Neither side
accounts for the money. A **double-miss**, strictly worse than the $280 over-count it replaced, in
the one direction §3 forbids.

Adversarial review found it in the plan, before any code existed. The fix is a second term: the
statement that has **not closed yet** but will close *and* come due inside the horizon. With a
30-day horizon and a >=21-day grace it activates once the next close is within `horizon - grace`
days — precisely the window term 1 leaves empty. The two terms tile the cycle with no gap.

### The test that would have caught it, and the two drafts that would not have

`test_the_reserve_never_shrinks_on_any_day_of_a_full_cycle` walks 70 consecutive days. Two earlier
drafts of it were green and worthless:

1. **It held `statement_balance` at $2,000 forever.** A statement that is never paid keeps term 1
   firing, which covers for a missing term 2. Model the payment or the test proves nothing.
2. **It took "the close about a month back" as the last close.** On a close day that skips straight
   over the statement that closed *today*, leaving it in neither term.

Both were found by deliberately deleting term 2 and checking the test went red. It did not, the
first two times. **A test for a safety invariant that has never been seen to fail is not evidence.**

### A real bug this shipped and then caught

Dropping a paid-off card from the portfolio (`if ledger.outstanding > ZERO`) meant the coverage
detector read the household's own historical payments to it as evidence of *a card we cannot see*,
and refused with `CARD_COVERAGE_INCOMPLETE` on the very days the feed should have been celebrating a
cleared balance. A paid-off card is still a card we can **see**. The portfolio always carries every
card; `_select_target` decides emptiness, with `NO_DEBT`.

### The demo now charges its card, and the retune was not free

`DEMO_SPEC` gains `card_share=0.15`. Without it the artifact never exercises the path this unit
exists for — no unbilled balance, no second reserve term.

Turning it on raised the sweeps (8 -> 10). **That is legitimate**: this household charges ~$300/month
and pays $450, so their checking genuinely does hold more idle cash, and the reserve correctly covers
the $450 that actually leaves. But at `card_share=0.35` the extra surplus cleared the whole $14,000
card inside the window — a "paid off" banner instead of the engine's daily work.

The first retune raised the balance to $22,000 and was **wrong**: at 24% that card accrues ~$440/month
against a $450 payment, amortizes at $10/month, and `interest_avoided` ballooned to **$50,320** — a
number that is arithmetically true and completely unverifiable, about a household `interest.py`
refuses to make claims for. Backed out. The dial is `card_share`, not the balance.

**The reserve was never touched to make the demo look good.** That is the trade the cadence work
warned would be tempting and would arrive at the worst possible moment. It arrived. The demo spec
absorbed it.

### Reason codes

Five added, each with copy and both guard tests green in the same commit (`test_every_reason_code_has_copy`,
`test_the_engines_own_copy_passes_on_every_day`). This category of bug has shipped three times; the
gate is a completion condition now, not a follow-up.

`tests/test_assistant.py`'s supporting-date tests pinned a hardcoded day *and* two dollar figures out
of the committed artifact, so they broke the moment the engine's decisions legitimately changed — and
the breakage read as "the guard is wrong" when the guard was fine. They now **find** a sweep day with
a projection horizon and derive the figures from it. The property under test is the word-order rule,
not which Tuesday the engine happened to sweep on.

### Still open

`engine/interest.py` still takes a `Debt` and still models a balance that only ever shrinks.
`decide.py` adapts a `Card` to it via `_as_debt()` — a labelled, temporary adapter and **not** a
second source of truth: the reserve reads `Card` and only `Card`. U5 (`0014`) removes it.

---

## 2026-07-14 — U5 (`0014`): the interest model admits that cards get charged

`total_interest()` projected a balance that could only ever **shrink**. It had no concept of new
charges, so a revolver putting $1,500/month onto the card we sweep against got a payoff date that
never arrives and an interest-avoided figure overstated *by construction* — the one number
`prd.md` §5.1 says the company is graded on.

`Card` gains `observed_monthly_charges`, derived in `precompute.py` from the household's own
history. `_check_amortizing()` has always been there; what is new is that it can finally **fire for
the right reason**. Until charges entered the model, the one household it exists to protect was
invisible to it.

**Charges post at the close, before the payment — not daily.** That understates the days they spend
accruing, so it understates the interest, so it understates what we claim to have saved. Wrong in
the safe direction, deliberately: the alternative is a model that flatters us on the single figure
we are paid on.

**A TRANSACTOR's `total_interest` is `ZERO`, not `None`.** The distinction carries weight. `None`
means *we cannot say*; `ZERO` means *we can, and it is nothing*. They hold the grace period, which
already does exactly what our sweep claims to do, so sweeping their cash onto a card they were
going to clear is a **prepayment, not a saving** — and taking a share of it (§7.2, "profit only on
progress") would be charging for nothing. `decide()` already refuses to target them (U4); this is
the model agreeing.

### The demo's interest claim moved, and it is not an error

**$6,678 -> $9,916.** Decisions did not change (still 10 sweeps / 80 refusals, same $11,719 swept) —
only the claim did. The model now knows the household keeps charging the card, so *their own*
payoff takes longer, so a sweep genuinely avoids more interest. Their real trajectory is worse than
we were modelling, which makes our help worth **more**, not less. The figure got bigger because the
model got honest, and those are not usually the same thing — worth stating plainly rather than
letting it read as inflation.

### The `_as_debt` adapter is gone

`decide.py` no longer imports `Debt` at all. `engine/interest.py` takes a `Card`. There is now
exactly one source of truth for what a household owes and what they pay against it.

`Debt` itself survives, unused by the engine. Removing it is a separate cleanup and not this
ticket's.

### Hand-computed amortization is unchanged

Every existing test in `tests/test_interest.py` still passes, because the `card()` helper defaults
to `charges="0.00"`. The old arithmetic is still exactly right — for a household that has stopped
using the card. That is a real household, and it is no longer the *only* one the model can describe.

---

## 2026-07-14 — U6/U7 (`0015`, `0016`): the spend surface, and one thing deliberately not built

### `GET /spend` feeds no decision, and a test enforces that

The rolling 30-day series is the exact structure that will eventually replace
`daily_discretionary_high` in the forecast. It ships here as a **dashboard**, a release before it
is trusted with a decision — so it earns its way into the forecast having already been looked at by
real households. `test_the_spend_surface_feeds_no_decision` asserts `engine/forecast.py` never reads
it. That swap would *loosen* the reserve, and loosening needs the measured breach rate `0017` has
not produced yet. It does not get to arrive quietly inside a dashboard ticket.

### The "no router" stance is retired, and no router was added

`App.tsx` argued that file-based routing was scaffolding for navigation that did not exist. That was
right with one screen. There are two now, and they are **co-equal** — someone opening the app to see
where their money went is not on a detour from the decision feed, they are doing the other half of
the thing the product is for.

`expo-router` (SDK 57) is capable and it is a great deal of machinery for a boolean. Two screens, no
nesting, no deep links, no URL state: a `useState` and two `Pressable`s. Both screens stay **mounted**
(`display: none`, not unmount) so a half-scrolled feed does not reset every time you glance at
spending.

The strip chart is hand-rolled from `View`s. A charting dependency would be more code than the chart
and another package in the bundle.

### The growing-card panel is not styled like an error

`theme.ts` contains **no red**, deliberately: a refusal is the product working, and colouring it like
a failure would quietly turn the most common outcome in the feed into a fault. That argument applies
here too. A household whose card grew is being told the truth about their spending, not shown an app
error. Deep green, the same voice as a refusal.

### NOT BUILT: the attestation action

[9.1] makes `UNATTESTED` a **blocking** refusal, which makes "these are all my cards" a hard
onboarding precondition. The engine half is real and tested (`0013`), and the refusal copy already
surfaces in the Decisions feed.

**The action is missing, and it should stay missing until there is somewhere to put it.** Attesting
is a *write*, and this backend has no database — it serves one committed JSON artifact
(`docs/decisions/0002-generated-json-artifact-over-database.md`). A button that appears to save an
attestation and cannot would be theatre, and the worst kind: it would look like the coverage gate was
handled.

The demo household attests trivially (`attested=True` — one card, and we generated it), so
`CARD_COVERAGE_INCOMPLETE` never fires in the served window. **That means the gate is unexercised
end-to-end**, and it is the one refusal in this feature nobody has seen in the product. Worth
knowing before it meets a real portfolio.

---

## 2026-07-14 — U8 (`0017`): the grader has a caller, and the engine has a measured error

`engine/outcome.py` existed for a day and a half and **nothing called it**. `backend/replay.py` is
the caller. The artifact is byte-identical after it — the replay observes and changes nothing.

**The first numbers the engine has ever had about itself**, on the demo household (72 of 90 days
gradeable; the rest are blocking refusals that never ran a forecast):

| | |
|---|---|
| Breach rate (days we were *optimistic*) | **6.9%** |
| Sweep-caused overdrafts | **0** |
| Worst projection error | **−$1,656** |
| False-refusal cost (our conservatism) | **$3,516** |
| Deferred (cadence holds — *not* a cost) | **$11,973** |

### The deferral partition is not a nicety — it is 77% of the number

Totalled naively, "false refusal cost" comes to **$15,489**. Of that, **$11,973 is deferred money
that moves next week.** Counting it would make the metric a measure of *how long the cadence made
someone wait*, not a measure of our forecast error — and the calibration dial would then learn to
talk us out of the cadence rule, and out of this feature's coverage gates with it.

`DEFERRING_REASONS` names all three: `CADENCE_HOLD` (the money moves next week),
`CARD_COVERAGE_INCOMPLETE` (resolves on attestation), `CARD_BEHAVIOR_UNKNOWN` (resolves by itself
after three cycles). `NO_SURPLUS` is deliberately **not** in it — that refusal is simply correct, and
being right costs nothing.

### Two bugs caught while writing it

**The shadow replay must apply its own sweeps.** The first version assumed we had never acted —
`days_since_last_sweep=None`, `swept_this_week=0`. It never tripped `CADENCE_HOLD`, so it observed
**zero deferrals**, so the entire partition above was untested and invisible. It would have reported
a clean $0 deferred and nobody would have questioned it. If the engine had been running, it *would*
have swept, and the cadence *would* have held — so the walk carries the sweep state, exactly as
`precompute.build()` does.

What our sweeps are **not** in is `Realized`. That is the household's own movement, and `grade()`
applies the decision's sweep to it itself — the caller cannot forget, because the caller is not the
one who applies it.

**A cold start throws away most of the window.** Without the 60-day warm-up runway that `build()`
walks, the first 60 days are `INSUFFICIENT_HISTORY` — blocking refusals with no projection, and
therefore ungradeable. 12 gradeable days instead of 72. The warm-up is walked and not graded: those
refusals are true, and they are not forecast errors.

### One definition of a Snapshot, not two

`assemble_snapshot()` is exported from `precompute.py` and used by both the artifact walk and the
replay. Two copies of "how a Snapshot is assembled" would drift, and the day they drifted the
calibration numbers would quietly start describing an engine that never shipped. Same argument
`untouchable()` and `apply_caps()` are exported under — and it has already paid for itself once.

### What this unlocks, and what it does not

`daily_discretionary_high` **still has not moved**, and this ticket does not move it. What exists now
is the *evidence* required to move it: a measured breach rate. The learning's sequencing is intact —
land the grader and the driver (**done**), ship the new spend model with its dial set to reproduce
today's refusals, and loosen only as far as the measurement licenses.

**6.9% is a starting reading, not a licence.** It is one household, one seed, 72 days. A population
and a distribution come before anyone touches the forecast.

---

## 2026-07-14 — U9 (`0018`): the spend model was measured, and it was refused

The 07-13 learning said `30 × p90_daily` over-reserves, proposed enumerating the household's own
rolling 30-day windows instead, and — correctly — insisted the swap be **measured before it
ships**, because it is the one change in this engine that *loosens*. This ticket built the dial,
built the population harness that grades it, and ran it.

**The measurement refused the swap.** `SPEND_QUANTILE = None`. The structure ships inert;
`forecast.py` still calls `daily_discretionary_high` and the engine behaves exactly as it did.

60 households (3 shapes × 20 seeds), 4,320 graded days:

| dial | breach | worst shape | sweep-caused overdrafts | false-refusal cost |
|---|---|---|---|---|
| **today** | **2.3%** | 5.8% | 0 | $544,640 |
| `q=1.0` — their worst month *ever* | **19.8%** | 22.0% | 1 | $380,499 |
| `q=0.90` | 24.4% | 27.4% | 1 | $317,823 |

Reserving against the worst 30-day stretch a household has **ever actually had** breaches nearly
nine times as often as the model we called an over-reserver.

### Two things I got wrong, and one the harness got wrong

**I nearly committed a wrong explanation.** The first draft of `test_calibrate.py` pinned the
failure on structure — *the low lands mid-horizon, so a flat-amortized 30-day total hasn't charged
enough of itself by then*. It is a good story and it is arithmetically impossible: **both** models
charge a flat constant per horizon day (`p90_daily` vs `spend_30d_high / 30`), so wherever the low
lands it scales both identically. Only the size of the constant differs. A plausible mechanism
attached to a real number is still decoration, and it was one edit away from becoming
institutional knowledge.

**The real cause is starvation.** `spend_30d_high` reads the worst 30-day window off the 60–150
days the engine actually has — 2–5 *independent* months, since overlapping windows flatter the
sample count without adding information. Against three years of ground truth it estimates the true
worst month at 77% / **58%** / 86% for typical / high-variance / steady. The bias is worst exactly
where it is most dangerous: a fat tail means the bad month is *rare*, so a short history almost
never contains one, so the household most likely to blow up gets the reserve covering 58% of its
worst month. Proven by giving the same model 3 years of history: breach falls **19.3% → 3.7%**,
and on the fat-tailed household to **0.0%**. The idea was right; the data isn't there.

**`licensed()` would have licensed a regression.** Its two conditions (no overdrafts, breach no
worse per shape) are *both monotone in the size of the reserve* — reserving more always breaches
less — so sweeping the dial far enough toward *tightening* eventually passes both. `q=3.0` does:
it clears both bars and costs **$1.62M** against today's $544K, and `report()` would have
announced that $1.08M regression as a buy-back of `$-1,078,015.78`. A safety bar that only
measures safety will license the least useful thing you show it. There is now a third condition —
it has to actually buy something back — and a test pinning it.

### Follow-ups (not done here)

- **Gate the empirical model on history, not on a quantile.** It beats the incumbent for
  households with years of data and is dangerous for ones we just onboarded. That is not a dial,
  it is a second model with an eligibility rule — a real ticket, and the most promising direction.
- The 07-13 over-reserve is **still real and still unfixed**. We now know the proposed fix is
  unavailable at the history we have.
- Single-overdraft counts flicker non-monotonically across neighbouring dial settings (a sweep
  changes the walk, so trajectories diverge). At n=60 I would not read `od=1` vs `od=0` between
  adjacent settings as signal; the baseline's `od=0` across every run is.

Full write-up: `docs/learnings/2026-07-14-the-empirical-spend-model-is-not-a-drop-in.md`.

---

## 2026-07-14 — the cadence refusal, in plain language (and the guard broke again)

The `CADENCE_HOLD` copy explained **our policy** ("we space payments at least 7 days apart") and
buried what the household actually got. Rewritten to the two facts they can use:

> **We paid an extra $1,600.00 for you 1 day ago. Our next check-in is in 6 days.**

`PROJECTION`, `NO_SURPLUS` and `IDLE_CASH_ELSEWHERE` got the same treatment — shorter sentences,
no em-dash asides, "your cash balance will be X on DATE" instead of "your balance is heading for a
low of X".

**No decision changes.** 0 of 90 actions moved, amounts identical, summary byte-identical. Copy is
a view of the decision, which is the entire reason `explain.py` exists.

### The engine had to learn a number to say it

The sentence names the last sweep's amount and the engine never carried it. Added
`Snapshot.last_sweep_amount` — **copy only**, `decide()` never reads it, no gate or cap depends on
it. `None` when unknown, and the copy then omits the figure rather than inventing one.

Deliberately *not* reused: `swept_this_week`. It equals the last sweep today only because
`min_days_between_sweeps` is 7. Set the cadence to 3 and the week holds two sweeps, and the copy
would quote a total while calling it a payment.

### The guard couldn't read the new copy — for the second time, and for a new reason

`backend/assistant.py`'s `_PRIOR_PAYMENT` exists *because* of the last outage (72/90 days
unanswerable when the guard read "we paid your card 6 days ago" as a fabricated payment). It broke
again, on **59 of 90 days**, and the cause is one character:

**A dollar amount contains a period.** The regex bounded the verb-to-"ago" gap with `[^.!?]` to
keep the match inside one sentence — and the decimal point in `$1,600.00` hard-stops the character
class *inside the number*. The match dies before it reaches "ago". No widening of the length bound
rescues it; the class itself forbids money.

Now: a period ends the clause only when **whitespace follows it**. A sentence-ending period does; a
decimal point is followed by a digit. (The length bound also went 30 → 60 — the amount pushes the
verb and the "ago" 34 characters apart.)

The lesson is the same one as last time, sharper: **the phrase list and the regexes in
`assistant.py` are a second copy of `explain.py`'s wording, and nothing in the type system links
them.** Every copy edit is a guard edit. The only test that catches it is the one that feeds the
engine's *real* rendered sentence through the guard — a hand-written imitation of the copy passes
happily while production is 59/90 broken.

### Follow-up worth a ticket

`precompute.build()` does **not** call `assemble_snapshot()` — it constructs its own `Snapshot`
inline. The 0017 note claims "one definition of a Snapshot, not two"; that is not true today, and
adding `last_sweep_amount` meant editing both. They will drift, and the day they do the calibration
starts describing an engine that never shipped — which is precisely the argument that note makes.

---

## 2026-07-14 — Cloud Run deploy (ticket 0009), and two things the deploy taught us

Deployed the API to Cloud Run (`cfo-ai-1` / `us-central1` / `resfi-api`) and published the Expo
web client to Firebase Hosting. The runbook that drove it is `DEPLOY.local.md` (gitignored — it
carries the live values); this note is the part that belongs in the repo.

### `/healthz` was never reachable in production, and no test could have told us

**`/healthz` is a reserved path on Google's frontend for `*.run.app`.** It answers the request
itself with a Google 404; the container never sees it. Our route was defined correctly and was
dead on arrival — and every test passed, because `TestClient` talks to the app directly and never
crosses the frontend that eats the path.

Renamed to `/health` (`backend/main.py`), which is verified reachable. Also added a test asserting
`/healthz` now 404s, so nobody reintroduces the name.

What made this expensive to diagnose is worth recording, because it does not present as a platform
problem:

- FastAPI's own 404 is JSON (`{"detail":"Not Found"}`); the reserved-path 404 is Google's **HTML**.
- Responses that reach the app carry `server: Google Frontend` and `x-cloud-trace-context`. The
  `/healthz` 404 carries **neither**.
- A path the app has never heard of (`/nonexistent-xyz`) returns FastAPI's JSON 404 *with* those
  headers. So routing was fine — one specific string was being swallowed.
- It is the exact string: `/health`, `/healthZ`, `/healthzz`, `/readyz` all arrive. Only `/healthz`
  does not, with or without a query string.

Nothing depended on it — Cloud Run's default startup probe is TCP, not an HTTP GET, so the service
was healthy the whole time. The cost was that our one deliberately-open endpoint was a black hole.

**The general lesson:** a route can be correct, tested, and unreachable. Any check that only
exercises the app in-process cannot see the platform in front of it. The acceptance check for a
deployed endpoint has to be run *against the deployed URL*, which is why `DEPLOY.local.md` §7 exists
and why check [1] failing is what surfaced this at all.

### Uvicorn was emitting plaintext redirect URLs behind Cloud Run's TLS

Cloud Run terminates TLS and forwards plain HTTP. Uvicorn, not told to trust the proxy, believed the
scheme was `http` — so FastAPI's trailing-slash redirect emitted `Location: http://…`, a silent
downgrade. Observed live on `GET /healthz/`.

Fixed in the `Procfile` with `--proxy-headers --forwarded-allow-ips='*'`. The wildcard is safe
*specifically because* nothing but Cloud Run can reach the container, so there is no unproxied path
by which a forged `X-Forwarded-Proto` could arrive. That reasoning does not travel — the comment in
the `Procfile` says so.

No current client path uses a trailing slash, so nothing was broken. It was a trap set for the first
person to add one.

### Deviations and decisions not in the plan

- **Firebase Hosting for the web client**, not in ticket 0009 (which only scoped the API). Chosen
  because `cfo-ai-1` already existed and Hosting gives free HTTPS on `*.web.app` with no load
  balancer, bucket, or certificate to manage. `mobile/firebase.json` + `mobile/.firebaserc`.
- **`@expo/metro-runtime` added** to `mobile/package.json` — Expo web does not build without it.
- **`docs/decision-flow.html` is now published** at `cfo-ai-1.web.app/decision-flow.html`. Because
  `expo export` **wipes `dist/`**, it is *rebuilt into* the export by a `copy:docs` script rather
  than copied in once; `npm run deploy:web` chains export → copy → deploy. A file hand-placed in
  `dist/` would vanish on the next web deploy, silently, and only for the people you sent the link
  to.
- **`--min-instances=1` bills continuously** (~$10–15/mo for an idle warm instance). Accepted: the
  client gives up after 8s (`REQUEST_TIMEOUT_MS`) and a cold start plausibly eats most of that
  budget, which would make the demo's first screen its error state.
- **The public web client makes `RESFI_API_KEY` discoverable**, not merely public-by-construction —
  anyone can read it out of the JS bundle. Exposure is bounded to **300 Anthropic calls/day**
  because `RateCap` is in process memory and `--max-instances=1` pins us to one process. This is
  the load-bearing reason that pin is not a tuning knob.

### Left alone deliberately

`docs/tickets/0002`, `docs/tickets/0009`, and `docs/plans/2026-07-13-001-…` still say `/healthz` in
their acceptance criteria. They are a record of what was planned, not live docs, and rewriting them
would erase the fact that the plan was wrong in a way worth remembering. `docs/RUNBOOK.md` *is* live
and was updated.

---

## 2026-07-16 — Multi-tenant persistence: scoping, and two numbers that were wrong

Scoping for `docs/plans/2026-07-16-001-feat-multi-tenant-persistence-plan.md` (requirements:
`docs/brainstorms/2026-07-16-…`, decision: `docs/decisions/0004-…`). No code written yet.

### The 18 TB that never existed — and how it got into a permanent doc

`architecture.md` [4.1] was first written with **~10KB/snapshot and 18 TB/yr at 5M households**, and
a whole "phase 3" was reasoned out from it: object storage, a columnar backtest, an analytics engine.
**All three numbers were a guess I did not measure**, and they were wrong.

Measured (90 consecutive `Snapshot`s of the demo household, `artifact.py`'s tagged-scalar scheme):

| | mean/snapshot | vs raw |
|---|---|---|
| raw JSON | **2,699 B** | — |
| gzip'd individually | 770 B | 3.5× |
| gzip'd as a batch | 38 B | **70.7×** |
| lzma'd as a batch | 25 B | 108× |

The size was ~4× high, and the size was never the point: **consecutive days for one household are
nearly identical**, so a store sorted by `(household_id, day)` compresses ~70× while Postgres TOAST —
which compresses each value independently — gets ~3.5×. Scaled honestly for a real household (3–5×
bigger, 20–30× compression rather than 70×) it lands **under a TB/yr at 5M**. Tens of dollars of
object storage.

So **the `SnapshotStore` seam survives on a much smaller claim than the one that motivated it**
(a compression gap, not a volume wall), and **phase 3's trigger cannot fire**. Both docs are
corrected in place with the correction stated rather than the number quietly swapped.

`prd.md` §2.4 already documents two plugged-in numbers that "pointed the right way for the wrong
reason" — an interest estimate 5× too high and an ACH-fee claim that was simply wrong. This was the
third, and it is logged here because it very nearly bought an architecture.

### The defect the scoping found

`calibrate.py` sweeps a dial `build()` structurally cannot read. `assemble_snapshot()`'s docstring
(`precompute.py:803-812`) says it exists so `build()` and `replay()` do not drift; `build()` does not
call it, builds a `Snapshot` inline at `943-973` that never sets `spend_30d_high`, and has no
`spend_quantile` parameter at all. Invisible only because `SPEND_QUANTILE = None`. The day the dial
moves, `calibrate.py` would license a forecast the artifact cannot ship — the harness grading an
engine that is not the one serving. U1 of the plan.

### Decisions not in the ask

- **`architecture.md` [4] scoped every table by `user_id`; corrected to `household_id`.** Everything
  the product reasons about is a household (`sim/`, `engine/`, `prd.md` §3, `USERS.md`). A `user` is
  a login, and one household may have two — for this product not a footnote, since a spouse's
  spending is what breaks a forecast. `users` stays in the sketch as the login table.
- **`architecture.md`'s status line claimed only `engine/` exists.** Stale since `sim/`, `backend/`,
  and `mobile/` landed. Rewritten to name what is built vs. still intent, and to flag that [2] says
  Next.js where the frontend is an Expo web export. The file is evergreen and edit-in-place by its
  own header, so this belongs there rather than in a footnote.
- **SQLAlchemy Core + Alembic** (U2), a new dependency set. ADR-0002 celebrated "no ORM" — Core is
  not the ORM, the SQL stays visible, and a schema without migrations drifts. Logged because it
  reverses a property an ADR was proud of.
- **The artifact is kept as a golden fixture, not deleted.** ADR-0002 said "superseded rather than
  extended," and it is: nothing reads it at runtime and there is one system of record. But
  `tests/test_precompute.py:489-494` is the only evidence U1's refactor preserved behavior, and
  `artifact.py`'s `$dec` codec is exactly what `SnapshotStore` needs for the JSONB payload. Both
  survive; only the file-as-persistence dies.
- **Phase 1 → 2 (Neon → Cloud SQL) has a trigger but not an argument**, and this is flagged in
  `architecture.md` [4.1] rather than resolved. Neon is SOC 2 Type II; the honest case for Cloud SQL
  is trust-boundary locality with KMS/Cloud Run, not capability. "Postgres/Cloud SQL" has sat in
  `architecture.md` since v1 unargued — which is `prd.md` §2.4's exact shape, a decision that
  survived because no document ever argued for it.

### Open, and deliberately not closed here

`architecture.md` §7.5's **APR fallback** (user-entered / estimated / refuse to rank). Archetype D
makes the consequence visible on a dashboard, which is a good way to decide it — but seeding it does
not decide it, and the schema needs to know whether an APR can have a user-entered provenance.

---

## 2026-07-16 (later) — the multi-tenant build: what it found, and three decisions

`0019`–`0022` on `main` (#44). `0027`/`0028` in #46 (open). `0023` unblocked, not started.

### Every defect was the same shape

**A mechanism that was built, tested, and never actually exercised.** None of them was in the
ticket that found it. None had a symptom. All had a green test.

- The dial `calibrate.py` swept but `build()` could not read (`0019`).
- Neon's `neondb_owner` has `rolbypassrls = true` — scoped to one household it returned **both**.
  The obvious deployment would have shipped `0020`/`0021`'s two layers as one, with the IDOR suite
  green throughout.
- The IDOR suite ran as the superuser that owns the tables, which bypasses RLS even when FORCEd.
- The walk built one `DebtLedger` for a whole portfolio — a $3,000 card reported $14,009.20
  (`0027`). `_select_target`'s ranking and the obligation reserve had **never** seen two cards
  outside a unit test.
- `Spending.test.tsx` called `render()` bare where every other suite awaits it — a race that had
  blocked three PRs, one of them documentation-only.

### Decisions taken

- **`apr` estimated at 23%, with provenance** (`0028`). Settles `architecture.md` §7.5. The
  instruction was "default to 23% when null"; what shipped is 23% **for ranking only** —
  `interest.py` refuses to price an `ESTIMATED` rate, because `prd.md` §5.1's KPI and §1's user-facing
  sentence would otherwise be arithmetic on an invention. *Act on the estimate; never bill for it.*
  Defensible because **`APR_UNKNOWN` is not a safety gate**: a wrong target optimizes worse and
  overdraws nobody, so §5.2's guardrail cannot move — and `calibrate` confirms it did not.
- **`CardSpec.apr_reported: bool`, not a nullable `CardSpec.apr`.** The card *has* a rate; Plaid
  merely does not report it. `sim/` models the world, the derivation models what we can see.
- **A `.venv`**, gitignored. The system Python holds SQLAlchemy 2.0.30 for `Flask-SQLAlchemy` and
  `langchain`; upgrading it would have reached outside this repo. See
  `docs/runbooks/local-development.md`.
- **`build()` now raises on a multi-card spec.** `DayRecord` has one debt because the artifact
  schema has one debt. Shipping `0027` while leaving `build()` to report `cards[0]` silently would
  have re-opened the same bug one function over.

### Corrected, having been wrong in public

- **The 18 TB/yr that never existed.** `architecture.md` [4.1] was first written with a *guessed*
  ~10KB snapshot, and a whole "phase 3" was reasoned out of it. Measured: **2,699 B**, compressing
  **70×** when sorted by `(household_id, day)` — **under a TB/yr at 5M**. The `SnapshotStore` seam
  survives on a compression gap (TOAST does 3.5×), not a volume wall, and phase 3's trigger cannot
  fire. `prd.md` §2.4 already documents two plugged-in numbers that "pointed the right way for the
  wrong reason"; this was nearly the third.
- **My own verification lied three times** — `cmd && echo ok` swallows exit codes, `set -e` does not
  fire on a non-final command in a `&&` list, and an unexpanded `$PSQL` made a mutation test a
  silent no-op. Each time the fix was checking exit codes explicitly. Worth remembering: the tooling
  that checks the work needs checking too.

### Open, and deliberately not closed

- **`sim.household.generate()` is not prefix-stable.** `(spec, seed, days=150)` and
  `(spec, seed, days=181)` are **different households**, so `build()` (150) and `replay()` (181)
  have never walked the same one — `0019`'s thesis one level deeper. `calibrate`'s **population**
  statistics survive; **per-household** claims do not (the 2026-07-14 plan's "6.9% breach on the
  demo household" describes a household `decisions.json` has never contained). Fixing it regenerates
  the artifact and moves every measured number in three documents. **Needs its own ticket, its own
  measurement, its own diff.** Pinned in `tests/test_precompute.py::TestGenerateIsNotPrefixStable`
  and `decision-engine.md` §6.6's postscript.
- **Phase 1 → 2 (Neon → Cloud SQL) has a trigger but not an argument.** Neon is SOC 2 Type II; the
  honest case for Cloud SQL is trust-boundary locality, not capability, and nobody has made it.
  `prd.md` §2.4 is a whole section about a decision that survived because no document ever argued
  for it. Flagged in `architecture.md` [4.1].
- **`USER_ENTERED` has no entry path.** §6.3's "a real product needs a user-entered fallback" is
  still true and still unbuilt; the enum and column exist because provenance is the expensive half
  to retrofit.

---

## 2026-07-16 (later still) — `0023`: the archetypes found three defects and one finding

The ticket the whole multi-tenant plan exists for. It spawned `0027` and `0028` before it could
start; building it turned up three more things, none of them in the ticket.

### The finding: the income gate is biweekly-shaped, and it is not §9.3

`0023` was written to price `decision-engine.md` §9.3 — the fixed 7-day sweep spacing that is "a
decent approximation of a biweekly household and a poor one for everyone else." **That is not what
bites.** The income gate fires first, and §9.3 never gets to run.

All four archetypes carry `payroll.variation = 0.02`. Their income is, by construction, exactly as
regular as the demo household's. The engine measures:

| archetype | true variation | measured `income_variation` | days over the 0.25 gate |
|---|---|---|---|
| A biweekly | 0.02 | 0.003–0.012 | 0/90 |
| B semimonthly | 0.02 | 0.004–**0.326** | **33/90** |
| C monthly | 0.02 | 0.007–**0.707** | **19/90** |
| D biweekly | 0.02 | 0.003–0.012 | 0/90 |

`precompute.INCOME_BUCKET_DAYS = 28` exists because bucketing a *biweekly* earner by calendar month
scores the ~4-times-a-year three-paycheck month as a 24% swing and trips a 25% gate on a household
whose income is perfectly regular. Its own comment says a 28-day bucket "is the honest measure of a
biweekly earner's variability." **It is. It is the measure of nobody else's** — 28 days divides
evenly into a biweekly calendar and into no other. A semimonthly earner (24/yr) lands 1 or 2
paychecks in a bucket; a monthly earner (12/yr) lands 0 or 1.

So the fix for the biweekly household is the bug for every other household, and it came within one
percentage point (24% vs a 25% gate) of being visible in the very case it was written for.
`prd.md` §2.2's variance gate is working correctly on a number that is wrong.

**Not fixed here, and not a gate to loosen.** §9.3's spacing and `INCOME_BUCKET_DAYS` are the same
defect wearing two hats — a biweekly-shaped constant applied to everyone — and both wait on the
recurring-income detector §6.2 lists as assumed away. Loosening the gate to 0.75 would admit
genuinely variable households, which is what §2.2 exists to refuse. Pinned in
`tests/test_seed.py::TestTheIncomeGateIsBiweeklyShaped`.

### What it costs, measured across 80 households (4 archetypes × 20 seeds)

| archetype | graded | breach% | sweep-caused overdrafts | false-refusal cost |
|---|---|---|---|---|
| `demo_biweekly` | 1440 | 1.2% | 0 | $104,825.11 |
| `semimonthly_portfolio` | 664 | **19.7%** | 0 | $83,391.00 |
| `monthly_thin` | 1060 | **7.5%** | 0 | $324,465.76 |
| `apr_unreported` | 1440 | 0.1% | 0 | $41,378.17 |
| all | 4604 | 5.0% | **0** | $554,060.04 |

**Read `graded` before `breach%`.** Every archetype is offered 1,800 days. A blocking refusal never
ran a forecast and is not graded, so B is *unserved 63% of the time* and C 41%. Then, on the days
they are served, the forecast is far worse: B's 19.7% breach is the same magnitude as the 19.8%
that got the empirical spend model **refused** as unsafe to ship.

**The guardrail holds everywhere: 0 sweep-caused overdrafts, all four archetypes.** This is a
service-and-honesty problem, not a safety one — the buffer and the obligation reserve absorb a badly
calibrated forecast, which is what they are for. Nothing here licenses touching a gate.

`calibrate.measure(None)` is **unchanged at 4,320 days / 2.338% / 0 in 590 / $544,640.58**. The
archetypes are a *second* population (`measure_archetypes()`), reported beside the dial sweep and
never folded into it: the spend population measures one spend model across three shapes on one
calendar; this measures one spend model across four calendars. Averaging them answers neither, and
folding them would move a number quoted in three documents without anyone being able to say which
part moved because the engine changed and which because the population did.

### Three defects, all the same shape

**1. `Repository.add_card()` never wrote `apr_source`, and the omission was silent.** The column
carries `server_default 'reported'` (migration `0002`, so a live table could be backfilled without a
rewrite), so an INSERT that omits it succeeds and records **a guess as a reported fact**. Proven:
writing a 23% estimate through the repository stored `apr=0.23000, apr_source=reported`. That is
`0028` — *act on the estimate, never bill for it* — defeated by a column nobody wrote, and it would
have surfaced only once `0024`'s read path handed the row to `interest.py`, which would have priced
it happily and put `prd.md` §5.1's KPI on an invention. `0021` built the method; `0028` added the
column two PRs later; nothing wrote a card through the repository until the seeder, so nothing
caught it. Naming the column in the statement makes it a required bind — omission is now an error at
the boundary.

**2. `accounts.id` and `cards.id` were global primary keys.** Every household the walk derives
carries the same `chk_demo`/`sav_demo` (module constants in `precompute.py`, baked into
`assemble_snapshot`'s `funding_account_id`), so the second household seeded raised
`duplicate key value violates unique constraint "accounts_pkey"`. The schema was already
inconsistent about this and had no cause to notice: `decisions` is keyed `(household_id, day, id)`,
`snapshots` hand-namespaces its id, `policies` is keyed by household outright. Only these two
assumed a global id space — an assumption inherited from a database that held one household.

It bites something larger than the archetypes: `prd.md` §5.2's 60-household population is 60
`DEMO_SPEC` clones, every one holding `card_demo`. Under a global key that population is not merely
unseeded, it is **unseedable**. Migration `0003` scopes both keys to `(household_id, id)`. *Decision
taken with the user; the alternative was per-household ids threaded through the walk, which would
have changed archetype A's account ids and put the regression oracle at risk.*

**3. `0023`'s own archetype D was inexpressible, and its acceptance criterion unsatisfiable.** The
ticket's table asks for "2 cards, both `apr=None`" and the AC for "archetype D triggers
`APR_UNKNOWN`". `CardSpec.apr` is `Decimal`; `derive_card` reads
`apr=card.apr if card.apr_reported else ESTIMATED_APR` and never emits `None`; `APR_UNKNOWN` fires
only on `apr is None`. **`APR_UNKNOWN` is unreachable from any `HouseholdSpec`.** The ticket's own
body already said so — it was updated when `0028` landed and the table and ACs were not. Built per
the body: `apr_reported=False`, and assert the *absence* of `INTEREST_AVOIDED`.

**Renamed `apr_unknown` → `apr_unreported`.** Naming an archetype after a reason code it is
structurally incapable of producing is the kind of drift this repo keeps finding in itself. What it
does produce is better and is the household `0028` argued for: sweeps normally on a 23% guess, and
says **nothing** about what it saved.

### Decisions taken

- **`ENGINE_VERSION = "0-unversioned"`**, a literal in `seed.py`. `decisions.engine_version` is NOT
  NULL and `architecture.md` [3.3] rests real weight on it ("replay any new engine version across
  every historical snapshot"), but **there is no version constant anywhere in `engine/`** — the
  artifact's `version: 3` is the schema's, a different thing. Not a git SHA: the determinism the
  seeder promises is "re-seeding produces identical rows", and a SHA would rewrite the decision log
  of households that did not change on every commit. The day `decide.py`'s logic moves without this
  moving, [3.3]'s backtest guarantee is a story.
- **The seeder generates exactly 150 days**, matching `build()`. Not a friendlier default:
  `generate()` is not prefix-stable, so 181 days is a *different household*. Measured — the same
  spec and seed, walked over the same 90 served days, differ on **7 of them** between a 150-day and
  a 181-day generation, including one day where 150 refuses and 181 sweeps $28.37. A seeder that
  generated 181 would have produced 90 plausible rows and 7 wrong ones, and the oracle catches it
  only because it compares amounts rather than shapes.
- **Archetype income held constant at $67,600/yr** across all four calendars ($2,600×26, $2,816.67×24,
  $5,633.33×12). The same discipline `calibrate._spec_for()` uses when it forces `card_share`
  constant: if the archetypes differed in wealth as well as cadence, nothing they measured could be
  attributed to either.
- **B's transactor holds the portfolio's highest APR (27.99%) deliberately.** A naive `max(apr)`
  picks it; `_select_target` must pick `card_b_high` at 24.99%, because a transactor clears its
  statement and sweeping there is a prepayment we would be charging for. The gap is the assertion —
  a portfolio of three merely-different cards would not have one.

### Open

- **The income bucket needs its own ticket.** It is a real defect with a measured cost and no safe
  local fix; the honest bucket is the household's own pay cycle, which needs the detector §6.2
  assumes away. Same dependency as §9.3.
- **`build()` still can't serve a portfolio**, so B/C/D exist only in Postgres. That is fine while
  `0024` is unstarted and the artifact serves the demo, and it is a decision `0024` inherits: the
  read path either serves one card per household or `DayRecord` grows.
