"""Static checks on the single-file UI (CSP, XSS hygiene, API contract)."""

from __future__ import annotations

from importlib.resources import files
import re

from lastseen import web

HTML = files("lastseen").joinpath("static/index.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)
MARKUP = re.sub(r"<script>.*?</script>", "", HTML, flags=re.S)


def test_one_inline_script_and_pinned_leaflet():
    tags = re.findall(r"<script\b[^>]*>", HTML)
    assert tags.count("<script>") == 1 and len(tags) == 2
    external = [t for t in tags if t != "<script>"][0]
    assert 'src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"' in external
    assert 'integrity="sha512-' in external and 'crossorigin="anonymous"' in external
    css = re.search(r'<link rel="stylesheet"[^>]*>', HTML).group(0)
    assert "leaflet/1.9.4/leaflet.min.css" in css and 'integrity="sha512-' in css
    assert "</script>" not in SCRIPT


def test_csp_compatible_markup():
    assert not re.search(r"\son[a-z]+\s*=", MARKUP, re.I), "inline event handlers are blocked by the CSP"
    assert "javascript:" not in HTML
    web.csp_for(HTML.encode())


def test_no_html_injection_sinks():
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in SCRIPT, sink


def test_api_calls_are_relative_and_exist():
    assert not re.search(r"""['"`]/api/""", SCRIPT), "use relative URLs so sub-path proxies work"
    used = set(re.findall(r"""['"`](api/[a-z0-9/-]+)""", SCRIPT))
    known = {p.lstrip("/") for p in [*web.GET_ROUTES, *web.POST_ROUTES]}
    assert used and used <= known, used - known
    for path in ("api/icloud/login", "api/icloud/code", "api/icloud/resend", "api/icloud/security-key",
                 "api/access-password", "api/session/login", "api/poll", "api/fixes", "api/devices"):
        assert path in used


def test_branding_and_disclaimer():
    assert "<title>lastseen</title>" in HTML
    assert "Not affiliated with Apple" in HTML
    assert "findmy" not in HTML.lower()
    assert "Family Sharing" in HTML


def test_dark_mode_and_mobile_layout():
    assert "prefers-color-scheme: dark" in HTML
    assert "100dvh" in HTML and "max-width: 720px" in HTML and "pointer: coarse" in HTML
