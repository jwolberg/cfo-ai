"""The database suites must not be able to skip quietly in CI.

`tests/test_schema.py` and (from ticket 0021) `tests/test_idor.py` skip when `TEST_DATABASE_URL`
is unset. That is right locally — not every checkout has a Postgres — and it is **dangerous in
CI**, because a skipped security test reports green while proving nothing.

That failure has a name in this repo. `prd.md` §5.2: *"a guardrail measured on one household is
not measured."* A guardrail that did not run is not measured either, and the whole point of an
IDOR suite is to be the thing standing between a forgotten `WHERE` clause and someone's complete
financial life.

So: locally, skipping is fine and loud. In CI, skipping is a build failure.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import TEST_DB_ENV


def _in_ci() -> bool:
    # GitHub Actions sets CI=true. So does almost every other runner, which is the point: this
    # should fire anywhere that is not someone's laptop.
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


@pytest.mark.skipif(not _in_ci(), reason="only meaningful in CI — locally, skipping is allowed")
def test_ci_actually_runs_the_database_tests() -> None:
    """If this fails, the database suites silently stopped running and CI has been lying."""
    assert os.environ.get(TEST_DB_ENV), (
        f"{TEST_DB_ENV} is unset in CI, so tests/test_schema.py and tests/test_idor.py skipped. "
        "A skipped security test is not a passing one. Restore the postgres service container "
        "and the env var in .github/workflows/ci.yml."
    )
