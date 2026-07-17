---
title: Provisioning the database (Neon, phase 1)
last-verified: 2026-07-16
anchor: RB-neon-provisioning
---

# Provisioning the database (Neon, phase 1)

Ticket `0026`. Decision: [ADR-0004](../decisions/0004-postgres-scoped-by-household.md);
phases: [`architecture.md`](../architecture.md) [4.1].

**No secrets in this file, or in any file in this repo.** The connection string lives in Secret
Manager for the deployed service, and in `DEPLOY.local.md` (gitignored) or your shell for local
work.

---

## ⚠️ Read this first: `neondb_owner` must not be the runtime role

Neon provisions its default owner role with **`rolbypassrls = true`**.

A `BYPASSRLS` role ignores **every** row-level security policy on **every** table — silently, with
no error. `household_scope()` still sets the variable. The policies still exist. `pg_policies`
still lists them. Every query returns every household's rows anyway, and there is no symptom until
someone reads someone else's financial life.

Measured against this project's own Neon instance on 2026-07-16, scoped to one household:

```
neondb_owner  scoped to alice sees: ['acct-alice', 'acct-bob']   <-- the scope was ignored
cfo_runtime   scoped to alice sees: ['acct-alice']               <-- RLS binds
```

**So the obvious deployment — paste the connection string Neon hands you into `DATABASE_URL` —
turns [`architecture.md`](../architecture.md) [4]'s defense-in-depth into one layer, and turns
`tests/test_idor.py` into a suite proving a property production does not have.**

`backend/db/session.py`'s `assert_rls_binds()` refuses to start under such a role. Do not work
around it by granting more; that is the failure it exists to prevent.

---

## Two hosts, and they are not interchangeable

Neon gives you both. Use each for one thing:

| | Host | Used by |
|---|---|---|
| **Direct** | `ep-<id>.<compute>.<region>.aws.neon.tech` | **Migrations.** Alembic takes session-level locks that pgbouncer's transaction pooling does not carry. |
| **Pooler** | `ep-<id>-pooler.<compute>.<region>.aws.neon.tech` | **The running service.** Cloud Run scales; connections must be pooled. |

**`-pooler` is appended to the endpoint-id segment only**; everything from `<compute>` rightward
is identical between the two. Do not drop the `<compute>` label (`c-2`, `c-11`, …) — a Neon host
is *not* `ep-<id>.<region>.aws.neon.tech`, and an earlier draft of this table said it was.

> ⚠️ **Both defaults are the wrong one, and they compound.** Neon's console hands you
> `postgresql://…` against the **direct** host; the pooler is behind a toggle and the driver
> prefix is not mentioned at all. Paste that string as-is and you get a runtime credential that
> is unpooled *and* unusable — see "Two more ways the obvious string is wrong" below. This is not
> hypothetical: it is how this project's own runtime entry was first stored.

> **DNS is not evidence.** Both names resolve to the same Neon gateway addresses — Neon routes by
> **SNI**, not by IP — so `dig`ging the pooler name and getting an answer tells you nothing about
> whether you have the right host. Only connecting does.

---

## Two more ways the obvious string is wrong

Distinct from the `BYPASSRLS` trap above, and cheaper to hit:

1. **`postgresql://` must be `postgresql+psycopg://`.** `pyproject.toml` pins psycopg **3**, and
   `database_url()` hands the string straight to `create_engine`. A bare `postgresql://` makes
   SQLAlchemy reach for psycopg2, which is not installed — so this fails at **import**, as
   `ModuleNotFoundError: No module named 'psycopg2'`, which reads like a packaging bug rather
   than a connection-string typo.

2. **Passwords must be URL-safe or percent-encoded.** Any of `@ : / ? #` in a generated password
   breaks URL parsing and surfaces as an authentication failure — sending you off to rotate a
   credential that was fine. Generate alphanumeric (`_` and `-` are safe) and the question never
   comes up.

Both are catchable in one line, before a deploy ever reads the value:

```bash
<the-string> | grep -q '^postgresql+psycopg://cfo_runtime:' || echo 'WRONG — do not deploy'
```

The pooler is also *why* `household_scope()` uses `set_config(..., is_local => true)` rather than
`SET`: a plain `SET` persists for the life of the connection, and under transaction pooling the
next checkout inherits it. Verified against Neon's real pooler — the scope is `None` on a fresh
checkout and an unscoped read returns zero rows.

---

## Provision

1. **Create the project** in the Neon console. Note the direct host, the pooler host, the database
   name, and the owner credentials.

2. **Migrate, as the owner, against the direct host.** The owner *should* own the schema — that is
   the one thing `BYPASSRLS` is right for.

   ```bash
   export DATABASE_URL='postgresql+psycopg://neondb_owner:<pw>@<direct-host>/neondb?sslmode=require'
   .venv/bin/python -m alembic upgrade head
   ```

   This creates the tables, the partitions, the RLS policies, and the `cfo_app` role (NOLOGIN — it
   is an application role, not a person).

3. **Create the runtime role.** It must be neither `SUPERUSER` nor `BYPASSRLS`, and both are said
   explicitly rather than left to a default:

   ```sql
   CREATE ROLE cfo_runtime LOGIN PASSWORD '<generated>' NOBYPASSRLS NOSUPERUSER IN ROLE cfo_app;
   GRANT USAGE ON SCHEMA public TO cfo_runtime;
   ```

   `IN ROLE cfo_app` gives it the table privileges the migration granted. The RLS policies carry no
   `TO` clause, so they bind every non-superuser role — which is what makes this work.

4. **Verify RLS actually binds**, through the pooler, as the runtime role. Do not skip this: it is
   the only step that distinguishes a working deployment from one where the policies are
   decoration.

   ```bash
   export DATABASE_URL='postgresql+psycopg://cfo_runtime:<pw>@<pooler-host>/neondb?sslmode=require'
   .venv/bin/python -c "
   from sqlalchemy import create_engine
   from backend.db.session import assert_rls_binds, database_url
   with create_engine(database_url()).connect() as c:
       assert_rls_binds(c); print('RLS binds')"
   ```

5. **Store the runtime connection string in Secret Manager**, never in the deploy command and never
   in the repo. The owner credential is not needed at runtime at all — it is a migration credential.

---

## Rotation

Both roles rotate in the Neon console. Rotate the **owner** credential if it has ever been pasted
anywhere it should not have been (a chat, a ticket, a screenshot) — it can drop the database.

Rotate `cfo_runtime` on the normal schedule and whenever someone with access leaves. The service
reads it at startup and fails fast if it is wrong, so a bad rotation is a failed deploy rather than
a slow leak.

---

## Version skew, known and unpinned

CI runs `postgres:16`, this Neon project is on **18.4**, and a developer's laptop is whatever
Homebrew last installed (17.10 here). Everything this schema uses — declarative partitioning, RLS,
`FORCE ROW LEVEL SECURITY`, `set_config` — has been stable since Postgres 10, so nothing here
depends on the difference.

It is recorded rather than fixed because the *reason* it is fine is a claim about what the schema
uses, and that claim needs re-checking the day someone reaches for a newer feature. Pinning CI to
18 would be closer to production; it would also stop CI from telling us the day we accidentally
depend on something 16 does not have. Neither is obviously right, so: written down.
