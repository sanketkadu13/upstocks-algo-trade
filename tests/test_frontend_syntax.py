"""The dashboard is plain JS with no build step, so nothing catches a syntax
error before the browser does — and a parse failure takes out the WHOLE file,
leaving a page that renders but has no working controls.

That shipped once: a stray literal newline inside a string made every
strategy setting unreachable while the page still looked fine.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static" / "dashboard"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_js_parses():
    result = subprocess.run(
        ["node", "--check", str(STATIC / "app.js")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"app.js has a syntax error:\n{result.stderr}"


def test_no_stray_newline_inside_a_js_string_literal():
    """Catch the specific failure even without node: an unterminated quote.

    Counts unescaped double quotes per line; a line ending mid-string is the
    signature of an escape that was written as a real newline.
    """
    offenders = []
    for line in (STATIC / "app.js").read_text(encoding="utf-8").splitlines():
        stripped = line.split("//")[0]
        # ignore backtick templates, which legitimately span lines
        if "`" in stripped:
            continue
        unescaped = len([
            i for i, ch in enumerate(stripped)
            if ch == '"' and (i == 0 or stripped[i - 1] != "\\")
        ])
        if unescaped % 2:
            offenders.append(line.strip()[:70])
    assert not offenders, "line(s) ending inside a string literal: " + "; ".join(offenders)


def test_referenced_static_files_exist():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for asset in ("app.css", "app.js"):
        assert asset in html, f"index.html no longer references {asset}"
        assert (STATIC / asset).exists(), f"{asset} is referenced but missing"
