---
id: "0033"
title: The deployed image cannot start — two dependency lists, and nothing compares them
type: bug
status: open
priority: high
repo: cfo-ai
agentId: infra-devops-agent
agentKind: classic
agentScope: repo
source: docs/tickets/0031-the-spend-surface-per-household.md
depends_on: []
created: 2026-07-16
---

# The deployed image cannot start — two dependency lists, and nothing compares them

**Found by `0031`, while deleting the paragraph in `architecture.md` that documented it.** The
manifest is fixed here; **the deploy is still broken** and that part is `0026`'s remaining half.

## What is wrong

`backend/requirements.txt` is what Cloud Run's buildpack installs. Until `0031` it read:

```
fastapi, uvicorn[standard], anthropic
```

`backend/main.py` has imported SQLAlchemy **at module load** since ticket `0024`:

```python
from sqlalchemy import text
from backend.db.session import assert_rls_binds, make_engine
```

So the deployed container `ImportError`s on boot. Not a 500 on a route — the process does not start.

## Why nobody noticed, and this is the interesting part

**There are two dependency lists and nothing compares them.**

| list | who installs it | had the driver? |
|---|---|---|
| `pyproject.toml` `[api]` extra | developers, **CI** (`pip install -e ".[dev]"`) | ✅ since `0020` |
| `backend/requirements.txt` | **Cloud Run** | ❌ since `0020` |

Every test passes, locally and in CI, because CI installs the list that is correct. The list that is
wrong is only ever exercised by a manual `gcloud run deploy`, and `0026` — the ticket that owns the
deploy path — is half done: Neon is provisioned and migrated, Cloud Run wiring and Secret Manager
are not. So nothing has deployed since the driver became necessary.

`git log` on the file says it plainly: last touched by ticket **`0004`**. `0020` added persistence,
`0024` put it in the read path, `0031` finished the job — and the manifest that Cloud Run reads has
not been opened since the explain-assistant endpoint.

It is this plan's signature defect once more, from a new angle: **a mechanism that was built,
tested, and never actually exercised.** Here the untested mechanism is *the deploy itself*.

`architecture.md` described the symptom accurately and drew the opposite conclusion — *"the deployed
image contains no database driver at all"* — and called it **deliberate and safe**. It was, exactly
as long as `db/` had a writer and no reader. `0024` made it fatal and the sentence stayed true and
stopped being reassuring, which is a hard thing to notice in a doc that reads as if it still applies.

## What `0031` fixed

`backend/requirements.txt` now carries `sqlalchemy`, `alembic`, `psycopg[binary]`, pinned to match
the `[api]` extra, with a comment naming the trap.

**That makes the container import. It does not make the deploy work.**

## What is left

- [ ] **`DATABASE_URL` reaches the container.** Secret Manager + the Cloud Run service config —
      `0026`'s unfinished half. Without it `make_engine()` raises at startup, which is correct
      behaviour (ADR-0004 [3.2]) and still a service that will not come up.
- [ ] **The app connects as a role RLS binds for**, not Neon's default role. `assert_rls_binds()`
      will refuse to start otherwise, which is the guard doing its job — see
      `docs/runbooks/neon-provisioning.md`, which creates `cfo_runtime`.
- [ ] **A deploy is actually attempted.** Nothing in this repo has proved the image boots since
      `0020`. That is the whole finding, and it does not close by editing a file.

## The check that would have caught it

Nothing compares the two lists. Options, cheapest first:

1. **Make `requirements.txt` derive from the extra** (`pip-compile`, or a one-line
   `-e .[api]`) so there is one list. Least code, removes the class.
2. **A CI job that installs `backend/requirements.txt` alone and imports `backend.main`.** Would
   have failed the day `0024` merged, in the four seconds an import takes.

(1) is the real fix; (2) is worth having anyway, because "the deployed image can start" is a claim
no current test makes. Neither is in `0031`'s scope — filed for `0026` to pick up with the rest of
the deploy path.

## Worth saying plainly

`USERS.md` says the deployed demo **is** the product surface. If the last live revision predates
`0024`, it is still serving the old file-backed build and looks fine; the next deploy is what fails.
Either way, `main` is currently not deployable, and it has not been for eleven tickets.
