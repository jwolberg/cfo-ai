# Users

Who opens this app, and what they're doing when they do. Two distinct audiences use this
repo's frontend, and they should not be conflated: the persona the product is designed
for, and the person actually looking at this demo.

---

## The Resfi customer (the persona the product is built for)

**Who they are.** A household with stable, low-variance income (a hard gate — see
[`docs/prd.md`](docs/prd.md) §2.2) and $8,000–40,000 of revolving credit card debt at
20–30% APR. They can see their own balance and APR; they are not confused about the
arithmetic. They persistently keep more cash in checking than they need in the next 30
days, because the cost of being wrong about that — an overdraft, a bounced rent check —
is immediate and humiliating, while the interest they're paying is invisible and
deferred.

**What they were doing before they opened the app.** Getting paid on a predictable
schedule, paying the minimum on two or three cards, and keeping a buffer in checking "just
in case," the way they've done for years. At some point they connected their checking,
savings, and cards to Resfi once. Since then the system has been running on its own — the
entire point is that they don't have to keep opening the app for it to work.

**Why they're touching the app right now.** Not to make a decision — the product's whole
premise is that they shouldn't have to. They open it because a notification told them
something happened ("we moved $220 today"), because it's been a while and they're
checking in, or because someone asked them what it actually does. They are not here to
budget, categorize spending, or manage the account. They are here to see whether the
system did anything, and to be reassured if it didn't.

**What they see.** A personal dashboard: the household's recent decisions (swept or
refused) in plain language, cumulative interest avoided, the buffer currently protecting
them, and which debt is being targeted. Tapping any decision opens a short explanation of
why — narrated from the engine's `Reason` codes, never an invented number.

**What they can do.**
- Read the decision feed and the summary numbers.
- Tap into any decision for a plain-language explanation.
- Ask the explain assistant a follow-up question about their own history ("why not last
  Tuesday?").

**What they cannot do, in this MVP** (see the requirements doc's Scope Boundaries,
[`docs/brainstorms/2026-07-13-decision-engine-frontend-mvp-requirements.md`](docs/brainstorms/2026-07-13-decision-engine-frontend-mvp-requirements.md)):
connect a real bank account, trigger or approve a real transfer, or change the buffer or
caps. This version shows what the engine decided; it does not let them do anything the real
product's later phases haven't earned yet.

**And they cannot switch households — but the demo can, and that distinction is the point.**
Ticket `0024` added a household switcher, and it is emphatically not a customer feature: a
customer has one household, which is *theirs*. What the switcher exists for is below.

---

## The interviewer / reviewer (the actual audience for this artifact)

**Who they are.** Someone evaluating this repo as evidence of engineering judgment for a
founding engineer role — most concretely, the audience described in
[`private/interview-prep-resfi.md`](private/interview-prep-resfi.md).

**What they were doing before opening the app.** Reading a resume or a repo link, deciding
how much of it is real versus aspirational.

**Why they're touching the app.** To see, in under a minute, whether the product argument
in `docs/prd.md` and `docs/strategy.md` is backed by working code — not to use it as a
customer would.

**What they see and can do.** The same dashboard and explain assistant the customer
persona sees — there is no separate reviewer-facing view. The demonstration *is* the
product surface; a second, instrumented "admin" view would undercut the point.

**Plus one thing the customer does not get: a household switcher.** Four synthetic households —
biweekly with one card, semimonthly with three, monthly with two, and one whose issuer reports no
APR at all (`backend/archetypes.py`). It exists because every household this engine had ever run
against was biweekly with one card, including all sixty in the calibration population, so
`decision-engine.md` §9.3's admission that the engine approximates everyone else poorly had never
been tested against an everyone else. The switcher is how you look at what that costs. It is a
reviewer's instrument, not a product feature.

---

## ⚠️ The auth posture, stated here rather than buried in a code comment

**Any API key may read any household.** The key is a shared secret, it is inlined into the web
bundle at build time (`EXPO_PUBLIC_API_KEY`), and every household is selectable by id. There is no
per-user identity anywhere in this system.

That is a **demo posture and a deliberate one**, not a defect to file: these households are
synthetic, nobody owns them, and there is no one to authenticate *as*. Real auth (Clerk) arrives
with Plaid, when there is a real user and a real balance behind the door.

**Say the uncomfortable half out loud.** The scoping mechanism underneath is real and tested —
every query is bound by the repository *and* by Postgres row-level security, and
[`tests/test_idor.py`](tests/test_idor.py) proves each layer with the other removed. But
`backend/auth.py`'s own docstring is the honest summary: the key is *"a lock on a door, not an
identity system."* **An IDOR suite is reassuring in a way a shared key does not earn**, and a
reader who saw the one and assumed the other would be wrong in the direction that matters. Ticket
`0021` asked for this paragraph and `0024` is where it finally got written.
