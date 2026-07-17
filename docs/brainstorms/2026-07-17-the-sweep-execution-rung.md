# The sweep-execution rung — the write half, behind a provider port, moving nothing first

**Date:** 2026-07-17 · **Status:** scope, not yet planned · **Feeds:** a plan doc, then tickets
**Touches:** a new `backend/transfer/`, `backend/db/models.py`, a new migration, `engine/` (the
`SWEEP_IN_FLIGHT` feedback only), `backend/main.py`. **`sim/` and `mobile/` are not touched.**
**Depends on** the Plaid transport rung ([`2026-07-16-plaid-the-transport-rung.md`](./2026-07-16-plaid-the-transport-rung.md)) —
it mints the item this rung authorizes off.

**Assumption locked 2026-07-17:** the sweep moves money through a **`TransferProvider` port**;
reserve-account ACH exercises the machinery first (shippable-vs-harness is an open product call,
[5.0]), card paydown is interface-only; the port is filled by a **Plaid Auth → processor token →
dedicated ACH provider (Increase/Dwolla)** *working assumption*, not a vendor commitment ([0.2]). The
assumption is deliberately low-lock-in — scoped to Increase↔Dwolla — and the reasons are [0.2].

---

## [0] What this actually is

The Plaid transport rung reads. It "ends at rows in a table" and does not cross into
`assemble_snapshot()`. That is the *ingest* half of the sweep. This document scopes the **write**
half — the thing that actually moves the dollars — which every core doc has carved out and left
unbuilt on purpose:

- `architecture.md` [5] "Money movement (**not yet built**)" — the state machine, the idempotency
  key, the returns, "this is where Temporal earns its place."
- `decision-engine.md` [6.4] — *"Everything downstream of `Decision` — authorization, idempotency,
  the ACH state machine, returns, NSF, reconciliation — is out of scope here and is the other half
  of the engineering problem."*

The `Decision` object is produced today. Nothing consumes it. This rung is the consumer.

### [0.1] The two endpoints the sweep touches, named precisely

A sweep touches two external systems, and the transport rung covers neither:

1. **Authorization — a *second* Plaid surface.** The transport rung stores an `access_token` and
   calls `/transactions/sync`. Moving money needs a **verified funding source** (account + routing),
   which is **Plaid Auth**, and a **processor token** minted off the same linked item to hand to the
   ACH provider. Same item, different product, different credential. This is not built by transport
   and is not the same token.

2. **The ACH vehicle.** The rail that debits checking and credits the destination. `prd.md` line 71:
   *"Moving money is a commodity — Plaid Transfer, Dwolla, and bill-pay providers all do it."* The
   commodity claim is the whole reason the choice can be an assumption ([0.2]).

### [0.2] The assumption, and why it commits us to almost nothing expensive

`architecture.md` [5]'s state machine is **already provider-agnostic**:

```
proposed → authorized → submitted → pending → settled
                              ↘ returned / failed / cancelled
```

Every ACH provider exposes *roughly* this shape — authorize, submit, poll status, receive a return —
and *"roughly"* is doing work: that is an **uncited vendor claim**, and this repo's memory is that
vendor claims go stale (the transport rung found two of ours already had). **Before the plan pins the
port's method signatures, fact-check `authorize`/`submit`/`status`/`handle_return` against Increase's
and Dwolla's current API docs and cite the date** — the discipline the transport rung applied to
Plaid. With that caveat, the rung is designed against a **`TransferProvider` port**
(`authorize` / `submit` / `status` / `handle_return`), and the assumed provider *fills* it. What the
assumption fixes and what it leaves free:

| Fixed by the assumption (cheap to unwind) | Free / provider-independent (the expensive parts) |
|---|---|
| Which of Increase/Dwolla sits behind the port (a one-file swap *between the two*) | The state machine and its transitions (`architecture.md` [5]) |
| The processor-token *shape* of the Plaid handoff | The idempotency guard (its exact tuple — see U1 / [5.4]) |
| The provider's sandbox for the harness | The append-only ledger and reconciliation |
| | The Temporal saga (`architecture.md` [1.2]: adopt "*when money moves*") |
| | The `SWEEP_IN_FLIGHT` feedback into the engine ([4.3]) |

**"Cheap to unwind" is scoped to Increase↔Dwolla.** Reverting to the third commodity option, Plaid
Transfer, is *not* a one-file swap — it would **delete U3** (the processor-token mint) rather than
change an SDK behind it, so it is a distinct, costlier reversal, not part of the low-lock-in claim.

**This is an assumption, not a vendor commitment.** `prd.md` §6 is explicit that named processors are
*"never a vendor commitment… keep it that way."* Naming Increase/Dwolla here is a *reversible,
port-backed working assumption* to unblock design — no code or config commits to a vendor until the
plan argues the cost/control ladder. Increase/Dwolla over Plaid Transfer buys rail and pricing control
at scale, at the cost of the processor-token handoff U3 builds now; **pin one concretely when the plan
is written** (default: Increase — developer-first, transparent ACH, a real sandbox).

**One caveat this rung cannot settle itself:** `prd.md` [7.1] sequences the **distribution** decision
*first*, because an embedded/B2B2C channel likely means the partner owns the money-movement rail — in
which case U3–U6's rail-ownership investment is *"money spent on an option that distribution may simply
delete."* This rung's provider assumption presumes a direct-to-consumer / owned-rail model; if
distribution is unsettled, that presumption — not the vendor name — is the real risk.

### [0.3] What this rung buys — and the honest first version moves no money

`architecture.md` [6] and `prd.md`'s roadmap both put **shadow mode first**: *"Ingest → normalize →
snapshot → decide → log. **Move nothing.**"* That is not a separate project from this rung — it *is*
this rung's first version. The whole saga is wired: authorize, build the idempotency key, run the
pre-flight re-check, write the ledger row — and `submit()` is a **logged no-op**. What it buys is the
one number the company is graded on before a dollar is at risk: *"Compute what we would have swept,
then check against what actually happened"* (`prd.md`). The provider SDK is not even called until the
saga has proven itself against real households in shadow.

So the sequence is: **build the machine → run it in shadow → then, and only then, let `submit()`
call the provider.** The provider assumption lets us build the machine now; it does not license
turning money on.

**What shadow mode does and does not de-risk.** It validates the engine's decision quality, the
tail-risk number, and the saga's own logic against real households. It does **not** de-risk the
provider integration: `ShadowProvider` never calls Increase/Dwolla, so the only real-SDK exercise is
U6's scripted sandbox run. Left there, production `submit()` would be the first time the real SDK
meets real household volume, timing, and diversity. U6 is therefore extended to **replay a sample of
real shadow-mode decisions through the provider sandbox** before `submit()` is ever promoted — so the
integration risk, not just the decision risk, is retired before money moves.

---

## [1] Scope

**In.** A `TransferProvider` port. The `architecture.md` [5] state machine as a durable saga. An
append-only `transfers` ledger, RLS-scoped, idempotent on the tuple U1 pins. The Plaid Auth
+ processor-token mint on the existing item. Reserve-account ACH (own-account → own-account) as the
one concrete path that exercises the machinery end to end — **but whether it ships as a user feature
or serves only as an internal validation harness is an open product decision ([5.0])**, because the
engine does not today emit a reserve-account sweep. A `SWEEP_IN_FLIGHT` feedback so an in-flight
transfer suppresses the next decision. A provider-sandbox harness that drives authorize → submit →
settle → return end to end. **Shadow mode is the first shipped state: `submit()` logs, moves nothing.**

**Out.** Card paydown (interface-only — [3]). Turning money on in production. The reserve
*destination account* provisioning. KMS for the processor token beyond the env-flag guard — but see
[4.1], the trigger is closer here than it was for transport. See [3] for the trigger on each.

**The rung ends at a settled reserve transfer in the provider's sandbox, and a shadow-mode ledger
against real households.** No production money moves in it.

---

## [2] The units

| # | Unit | Depends on |
|---|---|---|
| U1 | The `transfers` ledger + RLS + migration (`HOUSEHOLD_SCOPED` 8 → 9) | transport U1 |
| U2 | The `TransferProvider` port + the shadow (`submit` = no-op) implementation | U1 |
| U3 | Plaid Auth + processor-token mint on the existing item; token storage | transport U1 |
| U4 | The durable state machine (Temporal saga): authorize → submit → poll → settle/return | U1, U2 |
| U5 | The `SWEEP_IN_FLIGHT` feedback — an in-flight transfer becomes a snapshot input | U4, engine |
| U6 | The provider-sandbox harness — the unit that makes the other five real | U1–U5 |

### U1 — the `transfers` ledger

Append-only, exactly like `plaid_transactions`: a transition is a new row, never an `UPDATE`.
Columns carry the state machine (`state`, `provider_transfer_id`, `return_code`) and the idempotency
guard `architecture.md` [5] calls *"the highest-stakes idempotency in the system, because a duplicate
sweep is an overdraft."* **The guard keys on `(household_id, decision_date)` — not on `decision_id`** —
because a same-day re-decision mints a *new* `decision_id` and would slip a `(…, decision_id)`
constraint, defeating the exact double-debit it exists to prevent. A re-decision therefore produces a
**superseding** transfer against the same `(household, day)` slot, and the invariant enforced is that
**at most one transfer per slot reaches `submitted`** — a partial unique index or a per-slot lock,
pinned at plan time, *not* a blanket row `UNIQUE` that append-only corrections would violate ([5.4],
resolved this way). RLS follows `0001`/`0004`; the migration hardcodes its `HOUSEHOLD_SCOPED` list
(`0033`'s trap, the same one the transport rung's U1 hits).

### U2 — the port, and shadow first

`TransferProvider` = `authorize(funding, dest, amount, idem) → auth` · `submit(auth) → ref` ·
`status(ref) → state` · `handle_return(ref) → return`. The first implementation is `ShadowProvider`:
every method logs and advances the ledger, `submit` calls no vendor. This is what runs against real
households to produce the tail-risk number **before** the real SDK is wired. Build order [6] is not a
future phase; it is U2.

### U3 — the authorization endpoint (the second Plaid surface)

Plaid Auth on the already-linked item, then `/processor/token/create` for the provider. The processor
token is a **money-moving credential** and is stored on the item. The transport rung deferred KMS
because a Sandbox `access_token` protects nothing (its U1 env-flag guard); a processor token does not
have that excuse for long — see [4.1].

### U4 — the durable saga

`architecture.md` [5] verbatim, as a Temporal workflow: idempotency key, **pre-flight re-check
immediately before submission** (*"a decision is a proposal; authorization is a separate, fresher
act"* — re-run the freshness gates, the decision may be hours old), returns as normal events
(R01/R02/R03), compensation. `architecture.md` [1.2] cut Temporal *"when money moves"*; money moves
here, so this is where it enters — and nowhere earlier.

**Status delivery is a trust boundary, not just a poll.** Increase and Dwolla report settlement and
returns primarily by **webhook**, not pure polling — an inbound, internet-facing signal that pushes a
money saga into `settled` or unwinds `SWEEP_IN_FLIGHT`. So the status path gets the same treatment as
the transport rung's Plaid doorbell: **verify the provider's webhook signature and reject replays
before the callback touches the saga** — a forged or replayed callback must not fake a settlement or a
return. A poll is retained as the reconciliation backstop for missed webhooks.

**Reconciliation is U4's, and it is that poll.** The framing quote in [0] names reconciliation as part
of the problem; here it is the periodic poll that walks every non-terminal transfer and matches ledger
state against provider truth — the backstop that catches a webhook the doorbell never received. Named
so it does not go unbuilt.

### U5 — the feedback the engine already expects

`decision-engine.md` [130]/[598]: `SWEEP_IN_FLIGHT` *"blocks while ACH settles, so each sweep already
suppressed the next"* — *"stacking is how you overdraft someone with their own money."* The engine
**already models this state**; nothing produces it. U5 closes the loop: a transfer in
`submitted`/`pending` makes the next `assemble_snapshot()` carry `SWEEP_IN_FLIGHT`, and a `returned`
transfer unwinds it. The write rung is not fire-and-forget; its state is an engine input.

### U6 — the provider-sandbox harness, and why it is the point

authorize → submit → poll to `settled` → force a **return** → assert the ledger and the
`SWEEP_IN_FLIGHT` feedback both unwind. Against the provider's real sandbox, not a mock — the
transport rung's U5 exists because *"a mechanism built, tested, and never actually exercised"* is
every defect this repo has shipped. A money-movement path with unit tests and no sandbox settlement +
return would be the most expensive entry on that list, because the thing it is wrong about is a
debit.

---

## [3] What this rung does not build, and the trigger for each

Every trigger is an **event, not a date** — `architecture.md` [4.1]'s rule.

| Not built | Trigger |
|---|---|
| **Card paydown execution** | A validated reserve sweep in production. `prd.md` line 277 / `architecture.md` [5]: *"no universal pay-this-card API… deep-link → bill-pay → FBO/custodial (heaviest; avoid as long as possible)."* The ACH port **plausibly** carries over to a future bill-pay/FBO rail, but **likely not to the deep-link handoff** `architecture.md` lists *first* — a redirect to the issuer's own payment page has no backend `authorize`/`submit`/`poll` for the port to wrap. So this rung de-risks the *machinery* an ACH-shaped card rail would reuse; it does **not** by itself bring the flagship interest-saving rail closer if that rail turns out to be deep-link. We do not pretend the hard rail is solved. |
| **Turning `submit()` on in production** | A measured tail-risk number from shadow mode (`prd.md`: *"the only honest way to earn the right"*) **and** the guarantee/reimbursement posture (`architecture.md` [6], build-order item 2). |
| **The reserve destination account** | A product decision on where idle cash lands (user-owned savings vs. an FBO we hold). Own-account ACH needs no FBO; holding funds does. |
| **KMS for the processor token** | See [4.1] — the trigger is production, and it is nearer than transport's. The env-flag guard is the interim, but a money-moving credential raises the bar. |
| **Reg E / GLBA money-movement posture** | The first production debit. `prd.md` line 304: *"Moving money is not a UI change"* — Reg E unauthorized-transaction liability is real from the first live transfer. |

---

## [4] Findings that were not in the ask

### [4.1] The KMS question the transport rung deferred bites harder here

Transport's U1 stores a Sandbox `access_token` in plaintext behind an env-flag start guard, correctly
— *"a Sandbox token protects nothing."* A **processor token authorizes a debit.** In a provider
sandbox it still moves only fake money, so the env-flag guard is a defensible interim here too — but
the day `PLAID_ENV`/the provider env flips, a plaintext column becomes a credential that can drain a
checking account, not just read it. `architecture.md` [7.2] wants KMS envelope encryption *plus a
rotation/revocation runbook tied to item-error webhooks* before real tokens. Recommendation: **reuse
transport's env-flag guard shape for U3, and make production `submit()` (not merely production
`PLAID_ENV`) the hard trigger for KMS** — the guard should refuse to move real money against a
plaintext processor token, the same way `assert_rls_binds()` refuses to start against a
bypass-capable role.

**And there is a *second* credential the per-item processor token overshadows:** the provider's own
**application-level API key/secret** that authenticates every call to Increase/Dwolla. Its blast
radius is the whole tenant, not one item, so it gets the stricter home from day one — **secrets
manager, never an env var or source, rotated, with sandbox and production keys that are never
interchangeable.** U6's sandbox work must not log it.

### [4.2] Attestation (`0016`) gates every sweep and is still unbuilt

`CoverageState.UNATTESTED` is the default and blocks every sweep — *"attestation is a real onboarding
gate, not a checkbox."* The transport rung's brainstorm [4.1] already flagged that Link is this
backend's first write path and *"whoever builds Link should expect to close `0016`."* This rung is the
other place it surfaces: a household that has not attested cannot be swept, so **either Link (transport
follow-up) or this rung closes `0016`** — it must not be discovered a third time. **It is a blocking
dependency for this rung's payoff, not a footnote:** shadow mode's whole value is running the saga
*against real households*, and every unattested household is silently excluded — so a shadow run on an
unattested population measures nothing. The plan must either name `0016`'s closure as an explicit
predecessor of the shadow run, or state the fallback (e.g. attest the internal pilot households by
fixture). Owner is whichever of Link or this rung ships first.

### [4.3] The engine already has the socket for this rung

`SWEEP_IN_FLIGHT` and `BELOW_MIN_SWEEP` (`decision-engine.md` [130]/[135]) exist and are unreachable
in production because no transfer is ever in flight. This rung is what energizes them. That is a
comfort — the engine was designed expecting a write half — and a warning: U5's feedback is not
optional polish, it is the guard that stops a second sweep stacking on an unsettled first one.

---

## [5] Open questions

- **[5.0] Is reserve-account ACH a shippable path, or only a validation harness? (Product decision — surfaced, not resolved.)**
  The rung's one concrete rail is own-account ACH, but `decision-engine.md` [4] is explicit that idle
  savings cash is the one thing the engine *"won't sweep,"* and the `Decision` object only emits sweeps
  against a `target_debt_id` — there is no reserve-account decision type for the units to consume. So
  either **(a)** reserve-account ACH is an **internal validation harness** for the saga machinery,
  never a user-facing path (rename it as such throughout), or **(b)** the concrete path is
  **card-targeted** from the start, even against a bill-pay/deep-link mock, so *"this rung is the
  consumer of the `Decision` object"* is literally true of what ships. This is a product call, not an
  engineering one, and it wants a decision before the plan.
- **[5.1] Increase or Dwolla behind the port?** Both fit; the port makes it reversible. Default
  Increase (developer-first, transparent ACH, real sandbox). Decide at plan time, cite pricing/rail
  control, do not let the SDK leak past the port.
- **[5.2] Reserve destination: user-owned account, or an FBO we hold?** Own-account ACH is the light
  path and needs no custodial relationship; holding funds is the heavy one and pulls in the FBO
  compliance `architecture.md` [5] says to *"avoid as long as possible."* This rung assumes
  own-account and defers FBO to the trigger in [3].
- **[5.3] Does the pre-flight re-check re-read balances live, or trust the last sync?** `architecture.md`
  [5] says re-run the freshness gates before submission. With real ACH latency, "fresh" may require a
  synchronous `/accounts/balance/get` at authorize time, not the swept `last_successful_sync_at`.
  Sizing question for U4.
- **[5.4] Idempotency across a decision re-run. (Resolved — see U1.)** A same-day re-decision mints a
  new `decision_id`, so a `(…, decision_id)` key would not stop the double-debit it exists to prevent.
  Resolved by keying the guard on `(household_id, decision_date)` and making a re-decision a
  *superseding* transfer, with at most one transfer per `(household, day)` slot reaching `submitted`.

---

## [6] How this gets verified

U6 **is** the verification and it is a hard gate: a provider-sandbox run that authorizes, submits,
settles, and **forces a return**, asserting the ledger and the `SWEEP_IN_FLIGHT` feedback both unwind
— not a mocked one. Plus the `0021` IDOR suite extended to `transfers`, since RLS on a new scoped
table is precisely the mechanism *"built, tested, and never actually exercised"* twice already. Plus
the idempotency assertion: two saga runs on one `(user, date, decision_id)` produce **one** debit, the
highest-stakes test in the repo.

**A green test suite is not evidence here** — and here the thing it would be wrong about is a debit
that already left someone's account.

---

## Review History

**2026-07-17 — `/ce-doc-review` (coherence, feasibility, product, security, scope, adversarial).**
20 findings; 15 actioned here. Three citation fixes applied silently (`architecture.md [6.2]`→`[6]`,
two `[61]`→`[1.2]`, and a `"until money moves"` misquote). The review's central catches, now folded
in above: reserve-account ACH is a rail the engine does not today emit a decision for (the P0 — idle
cash is the one thing `decision-engine.md` [4] *"won't sweep"*; surfaced as the open product decision
[5.0], not silently resolved); the *"cheap to unwind"* claim leaked for Plaid Transfer (scoped to
Increase↔Dwolla, since reverting to Plaid Transfer deletes U3); the vendor names contradicted `prd.md`
§6's rail-neutrality rule (reframed as a reversible working assumption); the provider status/return
webhook was an unguarded trust boundary (U4 now requires signature + replay verification); the
provider's own application API key had no storage story ([4.1]); the `(user, date, decision_id)`
idempotency key did not stop a same-day re-decision double-debit (U1 re-keyed on `(household, day)`
with supersession, [5.4] resolved); shadow mode de-risked the engine but not the provider SDK (U6
extended to replay shadow decisions through the sandbox); the ACH port likely does not carry to the
deep-link card rail; reconciliation and the `0016` attestation gate were named but unowned (given to
U4 and made a blocking dependency of the shadow run); and the uniform-ACH-shape claim was uncited
vendor fact (flagged for fact-checking before the plan pins signatures). Five FYI observations were
left as notes.
