"""The operator console's theme: the light palette, and the control that reaches it.

Two independent properties, deliberately in one file because they fail for the same reason —
a light theme nobody can select is not a light theme, and a light theme nobody can read is not
one either.

**The palette tests need no database.** They read `backend/operator.css` and do arithmetic. That
is on purpose: legibility is not a property of Postgres, and gating it behind `requires_db` would
mean CI's no-database lane silently stops checking it (139 tests already skip there).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.operator import create_operator_app
from backend.seed import seed_all
from tests.conftest import requires_db

PASSWORD = "god-mode-test"
CSS = (Path(__file__).parents[1] / "backend" / "operator.css").read_text(encoding="utf-8")


# --- reading the palettes out of the stylesheet ----------------------------------------


def _vars_in(block: str) -> dict[str, str]:
    """Every `--name:#rrggbb` declaration in a chunk of CSS."""
    return {
        m.group(1): m.group(2) for m in re.finditer(r"--([\w-]+)\s*:\s*(#[0-9A-Fa-f]{6})", block)
    }


def _block(selector: str) -> str:
    """The declarations of the first rule whose selector text ends with `selector`.

    Deliberately dumb — the stylesheet is 150 lines of hand-written CSS with no nesting inside the
    palette rules, so `{`..`}` is unambiguous and a real CSS parser would be a dependency for
    nothing.
    """
    i = CSS.index(selector)
    start = CSS.index("{", i)
    return CSS[start + 1 : CSS.index("}", start)]


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(fg: str, bg: str) -> float:
    """WCAG 2.1 relative contrast ratio, 1.0 (invisible) to 21.0 (black on white)."""
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# Every foreground/background pair the console actually renders as text, named by where it shows
# up. `muted` is the one that matters most: it carries every label, table header and timestamp.
TEXT_PAIRS = [
    ("ink", "surface", "household id, policy values, card table"),
    ("body", "surface", "body copy in every card"),
    ("muted", "surface", "labels, .hlabel, .aat, .fnote"),
    ("muted", "ground", "h2 section headings on the page ground"),
    ("muted", "surface-2", "step ordinals, .thr code, .ledger"),
    ("ink", "surface-2", "ledger values, hover rows"),
    ("body", "ground", "body copy directly on the ground"),
    ("accent", "surface", ".eyebrow, .linkbtn, sign out"),
    ("sweep", "surface", ".dact.sweep, .aact.resume"),
    ("sweep", "sweep-wash", ".lrow.total inside a terminal sweep step"),
    ("pass", "surface", ".pill.active"),
    ("hold", "surface", ".dact on a paused row"),
    ("hold", "hold-wash", ".spill.s-hold, .err, .pill.paused"),
]

# White-on-color: the halt strip, the halt/resume buttons, the outcome pill. These are the `-deep`
# variants precisely because the plain `--hold`/`--sweep` are *text* colors — a color light enough
# to read on a dark surface cannot also be a fill behind white text.
ON_COLOR_PAIRS = [
    ("sweep-deep", ".primary, .btn.resume, .tpout.sweep"),
    ("hold-deep", ".haltstrip, .btn.halt, .tpout.refuse"),
]

AA = 4.5


def html_tag(body: str) -> str:
    """Just the opening `<html …>` tag.

    Necessary, not fussy: the stylesheet is inlined into every page by `_page`, and it now
    contains the literal string `data-theme` in its own selectors. Asserting against the whole
    document would match the CSS and pass no matter what the attribute actually says.
    """
    start = body.index("<html")
    return body[start : body.index(">", start) + 1]


def theme_button(body: str, value: str) -> str:
    """The picker's button for one choice, so assertions do not depend on attribute order."""
    match = re.search(rf'<button[^>]*value="{value}"[^>]*>', body)
    assert match, f"no theme button for {value!r}"
    return match.group(0)


@pytest.mark.parametrize("fg,bg,where", TEXT_PAIRS)
def test_the_light_palette_is_legible(fg: str, bg: str, where: str) -> None:
    """WCAG AA (4.5:1) for every text pair in light mode.

    This is the test that was missing. The light block was written once and never measured; the
    commit that brightened the dark neutrals to slate-400 left light behind, so `--muted` — which
    carries nearly every label in the console — sat at 3.09:1 on white.
    """
    light = _vars_in(_block(':root[data-theme="light"]'))
    ratio = contrast(light[fg], light[bg])
    assert ratio >= AA, f"--{fg} on --{bg} is {ratio:.2f}:1, below AA {AA} ({where})"


@pytest.mark.parametrize("fg,bg,where", TEXT_PAIRS)
def test_the_dark_palette_is_legible(fg: str, bg: str, where: str) -> None:
    """The same bar for dark, so a later light fix cannot quietly regress it."""
    dark = _vars_in(_block(":root{"))
    ratio = contrast(dark[fg], dark[bg])
    assert ratio >= AA, f"--{fg} on --{bg} is {ratio:.2f}:1, below AA {AA} ({where})"


@pytest.mark.parametrize("var,where", ON_COLOR_PAIRS)
def test_white_text_on_solid_fills_is_legible_in_both_themes(var: str, where: str) -> None:
    """`.haltstrip` is white on `--hold`. If that fails, the one banner that says money movement
    is stopped is the least readable thing on the page."""
    for name, block in (("light", ':root[data-theme="light"]'), ("dark", ":root{")):
        ratio = contrast("#FFFFFF", _vars_in(_block(block))[var])
        assert ratio >= AA, f"{name}: #fff on --{var} is {ratio:.2f}:1, below AA ({where})"


def test_the_auto_and_explicit_light_palettes_are_the_same_colors() -> None:
    """Light is spelled twice — once under `prefers-color-scheme` for Auto, once under
    `[data-theme=light]` for the explicit pick. They must not drift apart."""
    assert _vars_in(_block(':root[data-theme="light"]')) == _vars_in(
        _block(":root:not([data-theme])")
    )


def test_an_explicit_dark_pick_overrides_a_light_operating_system() -> None:
    """The `prefers-color-scheme:light` rule must not win over an explicit `dark` choice. It is
    scoped `:not([data-theme])` for exactly this reason — without that, picking Dark on a
    light-mode Mac would do nothing at all."""
    assert ":root:not([data-theme])" in CSS
    media = CSS.index("@media (prefers-color-scheme:light)")
    assert CSS.index(":root:not([data-theme])") > media


# --- the control that reaches it -------------------------------------------------------


@pytest.fixture
def seeded_engine(db_engine, monkeypatch):
    monkeypatch.setenv("OPERATOR_PASSWORD", PASSWORD)
    monkeypatch.setenv("OPERATOR_USER", "operator")
    with db_engine.begin() as c:
        c.execute(text("DELETE FROM decisions"))
        c.execute(text("TRUNCATE households CASCADE"))
        c.execute(text("TRUNCATE operator_actions"))
    seed_all(db_engine)
    return db_engine


@pytest.fixture
def client(seeded_engine):
    return TestClient(create_operator_app(seeded_engine))


def _login(client: TestClient) -> None:
    assert (
        client.post(
            "/login", data={"user": "operator", "password": PASSWORD}, follow_redirects=False
        ).status_code
        == 303
    )


@requires_db
class TestTheThemeControl:
    def test_the_default_follows_the_operating_system(self, client) -> None:
        """No cookie, no `data-theme` — the media query decides, which is the right default for a
        tool you open next to everything else on your desktop."""
        _login(client)
        assert "data-theme" not in html_tag(client.get("/").text)

    @pytest.mark.parametrize("choice", ["light", "dark"])
    def test_picking_a_theme_pins_every_page(self, client, choice: str) -> None:
        _login(client)
        r = client.post("/theme", data={"to": choice, "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        for path in ("/", "/household/hh_demo_biweekly"):
            assert f'data-theme="{choice}"' in html_tag(client.get(path).text)

    def test_picking_auto_again_clears_the_pin(self, client) -> None:
        _login(client)
        client.post("/theme", data={"to": "light", "next": "/"})
        client.post("/theme", data={"to": "system", "next": "/"})
        assert "data-theme" not in html_tag(client.get("/").text)

    def test_the_control_shows_which_theme_is_current(self, client) -> None:
        _login(client)
        client.post("/theme", data={"to": "light", "next": "/"})
        body = client.get("/").text
        assert "tbtn on" in theme_button(body, "light")
        assert "tbtn on" not in theme_button(body, "dark")
        assert "tbtn on" not in theme_button(body, "system")

    def test_a_junk_theme_is_refused_rather_than_stored(self, client) -> None:
        """The cookie is echoed into an HTML attribute. Anything but the two known values is
        rejected at the door, so nothing arbitrary can reach `data-theme`."""
        _login(client)
        r = client.post(
            "/theme",
            data={"to": '"><script>alert(1)</script>', "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 400
        body = client.get("/").text
        assert "<script" not in body
        assert "alert(1)" not in body
        assert "data-theme" not in html_tag(body)

    def test_a_forged_theme_cookie_cannot_inject_an_attribute(self, client) -> None:
        """Belt and braces: even a cookie set outside the route is validated on the way out."""
        _login(client)
        client.cookies.set("op_theme", '" onload="alert(1)')
        body = client.get("/").text
        assert "onload" not in body
        assert "data-theme" not in html_tag(body)

    def test_it_returns_you_to_the_page_you_were_on(self, client) -> None:
        _login(client)
        r = client.post(
            "/theme",
            data={"to": "light", "next": "/household/hh_demo_biweekly"},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/household/hh_demo_biweekly"

    @pytest.mark.parametrize(
        "hostile", ["https://evil.example/x", "//evil.example/x", "/\\evil.example"]
    )
    def test_it_will_not_bounce_you_off_site(self, client, hostile: str) -> None:
        """`next` comes from a form field. An operator console that forwards to an attacker's
        login page is a phishing primitive, so anything not a plain local path goes to `/`."""
        _login(client)
        r = client.post("/theme", data={"to": "light", "next": hostile}, follow_redirects=False)
        assert r.headers["location"] == "/"

    def test_the_theme_route_is_behind_the_gate(self, client) -> None:
        """Every other route requires a session; this one is no different, even though it only
        changes colors."""
        r = client.post("/theme", data={"to": "light", "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"
