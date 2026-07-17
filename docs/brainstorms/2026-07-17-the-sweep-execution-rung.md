# The sweep-execution rung — the write half: pay the card, two legs, behind a port, moving nothing first

**Date:** 2026-07-17 · **Status:** scope, re-based 2026-07-17 (see Revision History) · **Feeds:** a plan doc, then tickets
**Touches:** a new `backend/transfer/`, `backend/db/models.py`, a new migration, `engine/` (the
`SWEEP_IN_FLIGHT` feedback only), `backend/main.py`, and — new after the re-base — a platform
**funding/FBO account** provisioning path. **`sim/` and `mobile/` are not touched.**
**Depends on** the Plaid transport rung ([`2026-07-16-plaid-the-transport-rung.md`](./2026-07-16-plaid-the-transport-rung.md)) —
it mints the item this rung authorizes off.

**Assumptions locked 2026-07-17 (post re-base):** this rung **pays down a credit card** with the
customer's money ([5.0] resolved: card-targeted, not a reserve-account harness). Moving that money is
**two legs behind a `TransferProvider` port**: a **debit leg** (ACH pull from the user's checking into
a platform funding account — an ACH provider, Increase working assumption) and a **payoff leg** (land
it on any issuer's card — **Method** working assumption, fact-checked below). Because Method can only
pay *from* a platform funding account, the money transits an account we hold: **the rung commits to an
FBO/custodial posture** ([5.2] resolved). **Shadow mode is still the first shipped state** — the whole
saga is wired and `submit()` is a logged no-op; real money does not move until the tail-risk number
*and* the FBO/Reg E/MTL compliance posture are both real.

---

## [0] What this actually is

The Plaid transport rung reads. It "ends at rows in a table" and does not cross into
`assemble_snapshot()`. That is the *ingest* half of the sweep. This document scopes the **write**
half — the thing that actually moves the dollars onto a card — which every core doc has carved out
and left unbuilt on purpose:

- `architecture.md` [5] "Money movement (**not yet built**)" — the state machine, the idempotency
  key, the returns, "this is where Temporal earns its place."
- `decision-engine.md` [6.4] — *"Everything downstream of `Decision` — authorization, idempotency,
  the ACH state machine, returns, NSF, reconciliation — is out of scope here and is the other half
  of the engineering problem."*

The `Decision` object is produced today and carries a `target_debt_id`. Nothing consumes it. **This
rung is the consumer, literally**: it pays that debt.

### [0.1] The two legs the sweep moves through, named precisely

Every core doc is emphatic that moving money to pay a card is **two legs, not one**
(`status.html` line 660, `architecture.md` [5], `prd.md` [6]):

1. **The debit leg — pull from the user's checking.** Ordinary ACH; a solved commodity. It needs a
   **verified funding source** (account + routing), which is **Plaid Auth**, and a **processor
   token** minted off the same linked item to hand to the ACH provider. Same item as transport,
   different product, different credential. The debit lands the money in **a platform funding
   account**, not on the card — plain ACH cannot route to an arbitrary issuer.

2. **The payoff leg — land it on the card at *someone else's* issuer.** `architecture.md` [5]:
   *"There is no universal 'pay this card' API. Card networks are not a repayment rail; each issuer
   decides what it accepts."* Plain ACH cannot do this. The 2026 channel that can is a
   **biller-payoff API — Method** ([0.2]) — which pays **any of 15,000+ creditors** from a platform
   funding account.

**The seam between the legs is a platform funding account, and that is the whole custody story:**
Method's *source* must be an account we control ([0.2]), so the user's money transits an account we
hold. That is the FBO/custodial posture — resolved as committed ([5.2], [4.4]).

### [0.2] The rail, fact-checked against the vendor (2026-07-17)

`architecture.md` [5]'s state machine is **provider-agnostic**:

```
proposed → authorized → submitted → pending → settled
                              ↘ returned / failed / cancelled
```

This repo's memory is that vendor claims go stale, so the payoff rail was fact-checked against
**Method's live API docs (2026-07-17)** before pinning anything. What it confirmed:

| Method fact (verified 2026-07-17) | Maps onto |
|---|---|
| Lifecycle `pending → processing → sent → posted`; from `sent`: `reversal_processing → reversed`; else `failed` / `canceled` | `architecture.md` [5] `submitted→pending→settled` / `returned` / `failed` / `cancelled` — near 1:1 |
| **Idempotency keys** to prevent duplicate payments (header) | U1's `(household, day)` guard maps to Method's idempotency key |
| **Webhooks required** for status; posting timing is the creditor's, not Method's | U4's status path is an inbound trust boundary, not a poll ([4] / U4) |
| **Simulations API forces a payment to `reversed` in sandbox** | U6's hard gate: authorize → submit → settle → **force a return** |
| Destination = **any creditor/card, 15,000+ institutions**, discovered via Connect | The payoff leg is a commodity |
| **Source must be the platform's own funding account** (corporate/FBO); an end-user's personal checking **cannot** be a source | Forces the FBO custody posture ([5.2], [4.4]) |

Sources: `methodfi.com`, `docs.methodfi.com/guides/payments/{overview,lifecycle,errors}`,
`docs.methodfi.com/guides/accounts/overview`. The debit-leg provider (Increase default) is still an
assumption; **fact-check its `authorize`/`submit`/`status`/`handle_return` against current docs before
the plan pins those signatures**, the same discipline applied here to Method and to Plaid.

The rung is designed against a **`TransferProvider` port** that spans both legs
(`authorize` / `submit` / `status` / `handle_return`), with the debit provider and Method each filling
their half. What the assumptions fix vs. leave free:

| Fixed by the assumptions (cheap to unwind) | Free / provider-independent (the expensive parts) |
|---|---|
| Which ACH provider does the debit leg (Increase↔Dwolla, one-file swap) | The state machine and its transitions (`architecture.md` [5]) |
| That the payoff leg is Method vs. another biller-payoff API | The idempotency guard (its exact tuple — U1 / [5.4]) |
| Each provider's sandbox for the harness | The append-only ledger and reconciliation |
| | The Temporal saga (`architecture.md` [1.2]: adopt "*when money moves*") |
| | The `SWEEP_IN_FLIGHT` feedback into the engine ([4.3]) |
| | **The FBO funding account and its custody/reconciliation obligations ([4.4])** |

**These are assumptions, not vendor commitments.** `prd.md` §6 keeps named processors rail-neutral;
naming Increase/Method here is a *reversible, port-backed working assumption* to unblock design. No
code or config commits to a vendor until the plan argues the cost/control ladder.

**One caveat this rung cannot settle itself:** `prd.md` [7.1] sequences the **distribution** decision
*first*, because an embedded/B2B2C channel likely means the partner owns the rail **and the custody** —
in which case the FBO investment [5.2] commits to is *"money spent on an option that distribution may
simply delete."* Committing to FBO custody here presumes a direct-to-consumer / owned-rail model; if
distribution is unsettled, that presumption — not the vendor names — is the real risk.

### [0.3] What this rung buys — and the honest first version moves no money

`architecture.md` [6] and `prd.md`'s roadmap both put **shadow mode first**: *"Ingest → normalize →
snapshot → decide → log. **Move nothing.**"* That is this rung's first version. The whole saga is
wired — authorize, build the idempotency key, run the pre-flight re-check, write the ledger row — and
both legs' `submit()` are **logged no-ops**. What it buys is the one number the company is graded on
before a dollar is at risk: *"Compute what we would have swept, then check against what actually
happened"* (`prd.md`).

So the sequence is: **build the machine → run it in shadow → then, and only then, let `submit()` move
money.** The provider assumptions let us build the machine now; they do not license turning money on.

**What shadow mode does and does not de-risk.** It validates decision quality, the tail-risk number,
and the saga's own logic against real households. It does **not** de-risk the provider integration:
`ShadowProvider` never calls Increase or Method, so the only real-SDK exercise is U6's scripted
sandbox run. U6 is therefore extended to **replay a sample of real shadow-mode decisions through both
providers' sandboxes** before `submit()` is ever promoted — so the integration risk, not just the
decision risk, is retired before money moves. **And turning money on now carries a second gate the
original scope did not: the FBO/Reg E/MTL compliance posture must be real ([4.4], [3]).**

---

## [1] Scope

**In.** A `TransferProvider` port spanning both legs. The `architecture.md` [5] state machine as a
durable saga. An append-only `transfers` ledger, RLS-scoped, idempotent on the tuple U1 pins. The
**debit-leg authorization** (Plaid Auth + processor-token mint on the existing item). The **payoff
leg** via Method (Connect to discover the destination card; platform funding account as source).
**The platform FBO funding account provisioning** — pulled *into* scope by the [5.2] custody
commitment (it was out before the re-base). Card-targeted end to end: the concrete path pays a
`target_debt_id` the engine actually emits. A `SWEEP_IN_FLIGHT` feedback so an in-flight transfer
suppresses the next decision. A provider-sandbox harness that drives authorize → submit → settle →
return end to end against **both** providers' real sandboxes. **Shadow mode is the first shipped
state: `submit()` logs, moves nothing.**

**Out.** Turning `submit()` on in production. KMS for the processor token and the Method API key
beyond the env-flag guard — but see [4.1], the trigger is closer here. The full Reg E/GLBA/MTL
compliance *build* — committed as the posture ([4.4]) but its implementation is a predecessor to
turning money on, not this rung's shadow deliverable. See [3] for the trigger on each.

**The rung ends at a settled card payment in Method's sandbox (with a forced reversal), a funded
platform account in the debit provider's sandbox, and a shadow-mode ledger against real households.**
No production money moves in it.

---

## [2] The units

| # | Unit | Depends on |
|---|---|---|
| U1 | The `transfers` ledger + RLS + migration (`HOUSEHOLD_SCOPED` 8 → 9) | transport U1 |
| U2 | The two-leg `TransferProvider` port + the shadow (`submit` = no-op) implementation | U1 |
| U3 | Debit-leg auth (Plaid Auth + processor token) + Method Connect + the platform funding account | transport U1 |
| U4 | The durable state machine (Temporal saga): debit → payoff → poll/webhook → settle/return | U1, U2, U3 |
| U5 | The `SWEEP_IN_FLIGHT` feedback — an in-flight transfer becomes a snapshot input | U4, engine |
| U6 | The provider-sandbox harness (both legs) — the unit that makes the other five real | U1–U5 |

### U1 — the `transfers` ledger

Append-only, exactly like `plaid_transactions`: a transition is a new row, never an `UPDATE`.
Columns carry the state machine (`state`, `provider_transfer_id`, `return_code`) modeled on
**Method's verified lifecycle** (`pending/processing/sent/posted/reversed/failed/canceled`, [0.2]) and
the idempotency guard `architecture.md` [5] calls *"the highest-stakes idempotency in the system,
because a duplicate sweep is an overdraft."* **The guard keys on `(household_id, decision_date)` — not
on `decision_id`** — because a same-day re-decision mints a *new* `decision_id` and would slip a
`(…, decision_id)` constraint, defeating the exact double-debit it exists to prevent. A re-decision
produces a **superseding** transfer against the same `(household, day)` slot; the invariant is that
**at most one transfer per slot reaches `submitted`** — a partial unique index or a per-slot lock,
pinned at plan time, *not* a blanket row `UNIQUE`. **This `(household, day)` key is what maps to
Method's idempotency-key header** ([0.2]). RLS follows `0001`/`0004`; the migration hardcodes its
`HOUSEHOLD_SCOPED` list (`0033`'s trap, the same one transport's U1 hits).

### U2 — the two-leg port, and shadow first

`TransferProvider` = `authorize(funding, dest, amount, idem) → auth` · `submit(auth) → ref` ·
`status(ref) → state` · `handle_return(ref) → return`, with the **debit provider** and **Method**
each behind their half. The first implementation is `ShadowProvider`: every method logs and advances
the ledger, `submit` calls no vendor. This runs against real households to produce the tail-risk
number **before** either real SDK is wired.

### U3 — the two authorizations and the funding account

Three things, because the re-base split the old single "authorization endpoint" in two and added the
account: **(a)** Plaid Auth on the already-linked item, then `/processor/token/create` for the ACH
debit provider — the money-moving credential for the debit leg, stored on the item; **(b)** **Method
Connect** to discover the destination card as a liability account; **(c)** the **platform funding
account** registered as Method's payment source (corporate/FBO, verified once). The processor token
and the Method application API key are both money-moving credentials — see [4.1].

### U4 — the durable saga

`architecture.md` [5] as a Temporal workflow, now two-legged: **debit (fund the platform account) →
payoff (Method push to the card) → poll/webhook → settle/return**, with the idempotency key, the
**pre-flight re-check immediately before submission** (*"a decision is a proposal; authorization is a
separate, fresher act"* — re-run the freshness gates), returns as normal events, and compensation
(a returned payoff or a returned debit must unwind cleanly). `architecture.md` [1.2] cut Temporal
*"when money moves"*; money moves here.

**Status delivery is a trust boundary, not just a poll.** Method reports settlement and reversals
**by webhook** (verified [0.2]), and the ACH debit provider does the same. Both get the same treatment
as the transport rung's Plaid doorbell: **verify each provider's webhook signature and reject replays
before the callback touches the saga** — a forged or replayed callback must not fake a settlement or a
reversal. A poll is retained as the reconciliation backstop for missed webhooks.

**Reconciliation is U4's, and it is that poll** — the periodic walk of every non-terminal transfer,
matching ledger state against each provider's truth, the backstop for a webhook the doorbell never
received. Named so it does not go unbuilt.

### U5 — the feedback the engine already expects

`decision-engine.md` [130]/[598]: `SWEEP_IN_FLIGHT` *"blocks while ACH settles, so each sweep already
suppressed the next"* — *"stacking is how you overdraft someone with their own money."* The engine
**already models this state**; nothing produces it. U5 closes the loop: a transfer in
`submitted`/`pending` makes the next `assemble_snapshot()` carry `SWEEP_IN_FLIGHT`, and a
`returned`/`reversed` transfer unwinds it.

### U6 — the provider-sandbox harness, and why it is the point

For the **payoff leg**: authorize → submit → poll to `posted` → **force a `reversed`** via Method's
Simulations API ([0.2]) → assert the ledger and the `SWEEP_IN_FLIGHT` feedback both unwind. For the
**debit leg**: the ACH provider's sandbox, funding the platform account and forcing a return. Against
real sandboxes, not mocks — the transport rung's U5 exists because *"a mechanism built, tested, and
never actually exercised"* is every defect this repo has shipped. A money-movement path with unit
tests and no sandbox settlement + return would be the most expensive entry on that list.

---

## [3] What this rung does not build, and the trigger for each

Every trigger is an **event, not a date** — `architecture.md` [4.1]'s rule.

| Not built | Trigger |
|---|---|
| **Turning `submit()` on in production** | A measured tail-risk number from shadow mode (`prd.md`: *"the only honest way to earn the right"*), the guarantee/reimbursement posture (`architecture.md` [6], build-order item 2), **and** the FBO/Reg E/MTL compliance posture actually standing ([4.4]). |
| **The full Reg E / GLBA / MTL compliance build** | The [5.2] commitment fixes the *posture* (we hold funds); the *implementation* — money-transmitter licensing where required, Reg E unauthorized-transaction handling, GLBA safeguards, the reconciliation runbook — is a predecessor to the first production debit, not a shadow deliverable. `prd.md` line 304: *"Moving money is not a UI change."* |
| **KMS for the processor token and the Method API key** | See [4.1] — the trigger is production `submit()`, nearer than transport's. The env-flag guard is the interim; a money-moving credential and a whole-tenant API key raise the bar. |
| **Non-card destinations (reserve/T-bills)** | A separate product bet. `decision-engine.md` [4] is explicit that idle savings cash is the one thing the engine *"won't sweep,"* and there is no reserve decision type. Out of scope; this rung pays debt. |

---

## [4] Findings that were not in the ask

### [4.1] The KMS question the transport rung deferred bites harder — and there are now two credentials

Transport's U1 stores a Sandbox `access_token` in plaintext behind an env-flag start guard, correctly
— *"a Sandbox token protects nothing."* This rung has **two** credentials that do not have that excuse
for long: **(a)** the per-item **processor token** (authorizes the ACH debit); **(b)** Method's
**application-level API key** that authenticates every call and can initiate a payment to any creditor.
The API key's blast radius is the whole tenant, not one item, so it gets the stricter home from day
one — **secrets manager, never an env var or source, rotated, with sandbox and production keys that
are never interchangeable.** U6's sandbox work must not log it. Recommendation: reuse transport's
env-flag guard shape for the processor token, and make production `submit()` (not merely a production
env flag) the hard trigger for KMS — the guard should refuse to move real money against a plaintext
credential, the way `assert_rls_binds()` refuses to start against a bypass-capable role.

### [4.2] Attestation (`0016`) gates every sweep and is still unbuilt

`CoverageState.UNATTESTED` is the default and blocks every sweep — *"attestation is a real onboarding
gate, not a checkbox."* Shadow mode's whole value is running the saga *against real households*, and
every unattested household is silently excluded — so a shadow run on an unattested population measures
nothing. **It is a blocking dependency for this rung's payoff, not a footnote:** the plan must either
name `0016`'s closure as an explicit predecessor of the shadow run, or state the fallback (attest the
internal pilot households by fixture). Owner is whichever of Link (transport follow-up) or this rung
ships first — it must not be discovered a third time.

### [4.3] The engine already has the socket for this rung

`SWEEP_IN_FLIGHT` and `BELOW_MIN_SWEEP` (`decision-engine.md` [130]/[135]) exist and are unreachable
in production because no transfer is ever in flight. This rung energizes them. A comfort — the engine
was designed expecting a write half — and a warning: U5's feedback is the guard that stops a second
sweep stacking on an unsettled first one.

### [4.4] The FBO custody commitment is the rung's heaviest dependency (new after the re-base)

Method's source must be a platform funding account ([0.2]), so paying a user's card with the user's
money means the funds **transit an account we hold** — the FBO/custodial posture `architecture.md` [5]
and `prd.md` [6] both say to *"avoid as long as possible."* The [5.2] decision **commits** to it, which
pulls in obligations the original scope had deferred: **Reg E** unauthorized-transaction liability from
the first live transfer, **GLBA** safeguards, a **reconciliation** discipline between the FBO balance
and the ledger, and **money-transmitter licensing** where holding user funds triggers it. None of this
blocks the *shadow* build — no funds transit in shadow — but all of it is a predecessor to turning
`submit()` on ([3]), and it is the single biggest reason `prd.md` [7.1] says distribution should settle
first: an embedded partner may own the rail *and the custody*, deleting this investment.

---

## [5] Open questions

- **[5.0] Reserve-account harness vs. card-targeted. (Resolved — card-targeted.)** The rung pays a
  real credit card (`Decision.target_debt_id`) via the debit + Method payoff legs. Reserve-account
  ACH is dropped; the engine emits no reserve decision type, and idle cash is the one thing
  `decision-engine.md` [4] *"won't sweep."*
- **[5.1] Which providers behind the port?** Payoff leg: **Method** (default — verified card-payoff
  API, [0.2]). Debit leg: **Increase** default (developer-first, transparent ACH, real sandbox),
  Dwolla the one-file alternative. Decide concretely at plan time; do not let either SDK leak past the
  port; cite pricing/rail control.
- **[5.2] FBO custody. (Resolved — committed.)** Method forces a platform funding account as the
  payment source, so the money transits an account we hold. The rung commits to the FBO/custodial
  posture; its compliance build is a predecessor to turning money on ([3], [4.4]). This presumes an
  owned-rail model — revisit if distribution ([7.1]) is embedded.
- **[5.3] Does the pre-flight re-check re-read balances live, or trust the last sync?** `architecture.md`
  [5] says re-run the freshness gates before submission. With real ACH + card-posting latency, "fresh"
  may require a synchronous `/accounts/balance/get` at authorize time, not the swept
  `last_successful_sync_at`. Sizing question for U4.
- **[5.4] Idempotency across a decision re-run. (Resolved — see U1.)** A same-day re-decision mints a
  new `decision_id`, so a `(…, decision_id)` key would not stop the double-debit it exists to prevent.
  Resolved by keying the guard on `(household_id, decision_date)`, making a re-decision a *superseding*
  transfer with at most one per `(household, day)` slot reaching `submitted`, and mapping that key to
  Method's idempotency-key header.

---

## [6] How this gets verified

U6 **is** the verification and it is a hard gate: a **Method sandbox** run that authorizes, submits,
settles to `posted`, and **forces a `reversed`** via the Simulations API ([0.2]), plus the debit
provider's sandbox funding and return — asserting the ledger and the `SWEEP_IN_FLIGHT` feedback both
unwind, on real sandboxes, not mocks. Plus the `0021` IDOR suite extended to `transfers`, since RLS on
a new scoped table is precisely the mechanism *"built, tested, and never actually exercised"* twice
already. Plus the idempotency assertion: two saga runs on one `(household, day)` slot produce **one**
payment, the highest-stakes test in the repo.

**A green test suite is not evidence here** — and here the thing it would be wrong about is a debit
that already left someone's account, or a card payment that already posted.

---

## Review History

**2026-07-17 — re-base after fact-check + product decisions (this revision).** The original scope
(below) assumed **Increase/Dwolla ACH** as *the* money-movement rail and offered reserve-account ACH
as its concrete path. Three things changed it, in order:

1. **The feature is card paydown** — pay a customer's credit card from their bank. That resolved the
   open product decision [5.0] to **card-targeted** (reserve-account ACH dropped: the engine emits no
   reserve decision type; idle cash is the one thing `decision-engine.md` [4] won't sweep).
2. **ACH is only the *debit* leg.** Paying a card is two legs; the *payoff* leg — landing money on an
   arbitrary issuer — has *"no universal 'pay this card' API"* and plain ACH cannot route it. The
   verified 2026 channel is a biller-payoff API: **Method** (fact-checked 2026-07-17 against
   `docs.methodfi.com` — lifecycle, idempotency keys, required webhooks, a Simulations API that forces
   reversals in sandbox, any-creditor coverage). So the rail is **ACH debit leg + Method payoff leg**,
   both behind the existing `TransferProvider` port; the ACH work is not wasted, it funds the payoff.
3. **Method forces custody.** Its payment *source* must be a platform funding account — an end-user's
   personal checking cannot be a source. So the user's money transits an account we hold, which is the
   FBO/custodial posture the core docs say to avoid. Decision [5.2] **commits** to it, pulling Reg E /
   GLBA / MTL / reconciliation obligations into the rung as predecessors to turning money on ([4.4]).

What survived the re-base unchanged: the state machine as a durable Temporal saga, the append-only
RLS-scoped ledger, the `(household, day)` superseding idempotency guard, the `SWEEP_IN_FLIGHT`
feedback, shadow-mode-first, the webhook trust boundary, the `0016` attestation gate, and U6 as the
hard sandbox gate. Sources for the fact-check: `methodfi.com`;
`docs.methodfi.com/guides/payments/{overview,lifecycle,errors}`; `docs.methodfi.com/guides/accounts/overview`.

**2026-07-17 — `/ce-doc-review` (coherence, feasibility, product, security, scope, adversarial), on the
pre-re-base scope.** 20 findings; 15 actioned. The review's central catches — reserve-account ACH is a
rail the engine emits no decision for (surfaced as [5.0], now resolved above); the *"cheap to unwind"*
claim; rail-neutrality per `prd.md` §6; the provider status/return webhook as a trust boundary; the
provider API-key storage story ([4.1]); the `(user, date, decision_id)` idempotency hole ([5.4]); shadow
mode not de-risking the SDK (U6 replay); reconciliation and `0016` ownership; and the uncited
uniform-ACH-shape claim (which prompted the vendor fact-check that drove this re-base) — are all folded
into the current text.
