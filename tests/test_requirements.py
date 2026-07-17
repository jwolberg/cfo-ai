"""The deploy manifest and the code agree about what the service needs.

**Nothing else checks this file.** `backend/requirements.txt` is what the Cloud Run buildpack
installs. The venv and CI both install `pyproject.toml`'s `[api]` extra, which is a superset — so a
package the service imports and the manifest omits is invisible to every test in this suite and
fatal in production, at import, before the first request.

That is not hypothetical. Ticket `0024` moved the read path onto Postgres, `backend/main.py` began
importing SQLAlchemy, and this manifest still listed `fastapi`, `uvicorn`, `anthropic`. 496 tests
passed. The container would not have started.

It is the same shape as every other defect this repo has found in itself — a mechanism nobody
exercised — with the twist that the thing nobody exercised was *the deploy*, which is exercised
exactly once, in the place where being wrong is most expensive.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "backend" / "requirements.txt"

# What the deployed service imports, mapped to the distribution that provides it. Only third-party
# roots appear here; stdlib and first-party (`backend`, `engine`, `sim`) are resolved out below.
DISTRIBUTION_OF = {
    "fastapi": "fastapi",
    "starlette": "fastapi",  # a dependency of fastapi, never named directly
    "pydantic": "fastapi",
    "uvicorn": "uvicorn",
    "anthropic": "anthropic",
    "sqlalchemy": "sqlalchemy",
    "psycopg": "psycopg",
    # Plaid transport rung (0034-0038). Register every new import root here — an unknown root is
    # silently skipped by `test_every_module_..._in_the_deploy_manifest`, which is exactly the
    # PR #50 hole where a real import escaped the check. `jwt` is PyJWT; `google` is the namespace
    # package `google-cloud-tasks` ships under.
    "plaid": "plaid-python",
    "google": "google-cloud-tasks",
    "jwt": "pyjwt",
}

# Modules the service imports at runtime. `alembic` is deliberately absent: migrations are an admin
# task run against the database, not something the container does on the way up.
FIRST_PARTY = {"backend", "engine", "sim"}


def _requirements() -> set[str]:
    names = set()
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        # `uvicorn[standard]>=0.30,<1.0` -> `uvicorn`
        names.add(re.split(r"[\[><=!~;]", line)[0].strip().lower())
    return names


def _imported_roots(path: Path) -> set[str]:
    """Every top-level module a file imports, from its AST rather than by importing it."""
    tree = ast.parse(path.read_text())
    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])

    return roots


def _service_modules() -> list[Path]:
    """Everything the service can reach from `backend/main.py`, first-party only.

    Walked rather than listed: a list would be the second thing to forget, after the manifest.
    `db/` is included because `main.py` imports it — which is the whole point of this file.
    """
    return sorted((ROOT / "backend").rglob("*.py"))


def test_every_module_the_service_imports_is_in_the_deploy_manifest() -> None:
    declared = _requirements()
    missing: dict[str, set[str]] = {}

    for module in _service_modules():
        # The seeder is an admin task, not a request path — but it lives in `backend/` and the
        # buildpack packages it, so an import it needs must still be installable.
        for root in _imported_roots(module):
            if root in FIRST_PARTY or root not in DISTRIBUTION_OF:
                continue
            dist = DISTRIBUTION_OF[root]
            if dist not in declared:
                missing.setdefault(dist, set()).add(module.relative_to(ROOT).as_posix())

    assert not missing, (
        "backend/requirements.txt is what Cloud Run installs, and these are imported without "
        f"being declared: { {k: sorted(v) for k, v in missing.items()} }. The container fails at "
        "import, before the first request, and no other test in this suite can see it."
    )


# Declared, never imported, and both load-bearing — which is exactly why they need naming rather
# than an exemption. An import scan cannot see either, so a future cleanup that "removed the unused
# dependencies" would remove the process that runs the app and the driver that reaches the database.
NOT_IMPORTED = {
    # The Procfile's entrypoint: `uvicorn backend.main:app`. Nothing in this package imports it,
    # because it imports *us*.
    "uvicorn": "invoked by the Procfile, not imported",
    # The driver `DATABASE_URL`'s `postgresql+psycopg://` names. SQLAlchemy loads it by string; a
    # missing driver is a startup failure one layer below a missing library, and looks nothing like
    # an ImportError when it happens.
    "psycopg": "loaded by SQLAlchemy from the URL scheme, not imported",
}


def test_the_manifest_does_not_declare_what_nothing_imports() -> None:
    """The other direction. A dependency nobody imports is one nobody removed.

    Except the two that nobody *can* import — see `NOT_IMPORTED`. The point of listing them with
    reasons rather than skipping this test is that the next person to add an unimported dependency
    has to write down why it is there, in the file that decides what ships.
    """
    declared = _requirements()
    imported = {
        DISTRIBUTION_OF[root]
        for module in _service_modules()
        for root in _imported_roots(module)
        if root in DISTRIBUTION_OF
    }

    unused = declared - imported - set(NOT_IMPORTED)
    assert not unused, (
        f"declared in the deploy manifest and imported nowhere: {sorted(unused)}. If it is loaded "
        f"by name rather than imported (a driver, an entrypoint), add it to NOT_IMPORTED with the "
        f"reason — do not delete it."
    )


def test_the_engine_and_the_simulator_still_have_no_dependencies() -> None:
    """`engine/` is pure by design and `README.md` says so. This is where that stops being a claim.

    A dependency here would not merely be weight: the engine is deterministic — no clock, no
    network, no randomness — and every import is a chance for one of those to arrive.

    Read from the source rather than from `pyproject.toml`'s declared dependencies: the declaration
    is the promise and the imports are the fact, and this file exists because the two came apart
    somewhere else.
    """
    third_party: dict[str, set[str]] = {}
    for package in ("engine", "sim"):
        for module in sorted((ROOT / package).rglob("*.py")):
            for root in _imported_roots(module):
                if root in FIRST_PARTY:
                    continue
                if root in DISTRIBUTION_OF:
                    third_party.setdefault(package, set()).add(root)

    assert not third_party, f"engine/ and sim/ have zero dependencies by design: {third_party}"


@pytest.mark.parametrize("root", sorted(DISTRIBUTION_OF))
def test_the_map_from_import_to_distribution_is_honest(root: str) -> None:
    """`import psycopg` does not tell you the distribution is `psycopg[binary]`, and `starlette`
    arrives via `fastapi`. This map is hand-written, so assert the names it claims are real."""
    import importlib.util

    assert importlib.util.find_spec(root) is not None, (
        f"{root!r} is in the import-to-distribution map but is not installed — the map has drifted "
        f"from what the code actually imports"
    )
