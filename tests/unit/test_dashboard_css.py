"""Guards on the stylesheet that a browser test would otherwise have to catch.

`position: sticky` keeps the sidebar and the top bar in view only if no ancestor between them and
the page is a scroll container. Setting overflow hidden/auto/scroll on BOTH <html> and <body> turns
<body> into one, and the sidebar then scrolls away with the page (seen on the live site after the
sideways-scroll guard was added). `overflow-x: clip` clips without creating a scroll container."""

import pathlib
import re

CSS = (pathlib.Path(__file__).resolve().parents[2] / "app" / "dashboard" / "static" / "dashboard"
       / "app.css").read_text(encoding="utf-8")
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
SCROLLING = re.compile(r"overflow(?:-x|-y)?\s*:\s*(?:hidden|auto|scroll)\b")


def _rules(selector: str):
    for sel, body in RULE.findall(re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)):
        if selector in [s.strip() for s in sel.split(",")]:
            yield sel.strip(), body


def test_body_is_never_a_scroll_container():
    bad = [(sel, body.strip()) for sel, body in _rules("body") if SCROLLING.search(body)]
    assert bad == [], bad


def test_the_sideways_scroll_guard_uses_clip_on_body():
    assert any("overflow-x: clip" in body for _sel, body in _rules("body"))


def test_the_sidebar_and_top_bar_are_still_sticky():
    nav = " ".join(body for sel, body in RULE.findall(CSS) if sel.strip() == ".nav")
    assert "position: sticky" in nav and "top: 0" in nav and "height: 100vh" in nav
