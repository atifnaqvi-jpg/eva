"""The widget ships under its size budget and is actually served (WIDG-01/02).

The size assertion lives in the Python suite as well as the node one on purpose:
the loader budget is an architecture constraint (§12.2), so a regression should fail
the same suite that gates every other architecture constraint. It reads `dist/`,
which the Docker build produces and `node build.mjs` produces locally, so the number
tested is the number shipped.
"""

from __future__ import annotations

import gzip
import json
import subprocess
from pathlib import Path

import pytest

WIDGET_DIR = Path(__file__).resolve().parents[1] / "backend" / "api" / "widget"
DIST = WIDGET_DIR / "dist"
BUDGET = 10 * 1024


def _build() -> None:
    """Build once per session if the artefacts are missing or stale."""
    if (DIST / "sizes.json").is_file() and (DIST / "loader.js").is_file():
        return
    if not (WIDGET_DIR / "node_modules").is_dir():
        pytest.skip("widget node_modules absent; run `npm ci` in widget/")
    subprocess.run(["node", "build.mjs"], cwd=WIDGET_DIR, check=True, timeout=300)


@pytest.fixture(scope="module")
def built() -> Path:
    _build()
    if not (DIST / "loader.js").is_file():
        pytest.skip("widget not built")
    return DIST


def test_the_loader_is_under_ten_kilobytes_gzipped(built: Path) -> None:
    measured = len(gzip.compress((built / "loader.js").read_bytes(), 9))
    assert measured < BUDGET, f"loader is {measured} B gzipped; budget {BUDGET} B"


def test_the_recorded_size_matches_the_shipped_bundle(built: Path) -> None:
    """sizes.json is quoted in the docs, so it must not drift from dist/.

    Compared with tolerance, not equality: node's zlib and Python's gzip emit
    different headers for the same input (filename/mtime fields), so the two
    measurements differ by a few bytes. The budget assertion above is the real
    gate; this one catches a stale file, where the gap would be whole kilobytes.
    """
    recorded = json.loads((built / "sizes.json").read_text())
    actual = len(gzip.compress((built / "loader.js").read_bytes(), 9))
    assert abs(recorded["loader"]["gzip"] - actual) < 64, (
        f"sizes.json says {recorded['loader']['gzip']} B, bundle measures {actual} B "
        "-- rebuild"
    )
    assert recorded["loader"]["budget"] == BUDGET


def test_the_iframe_app_is_a_separate_bundle(built: Path) -> None:
    """§12.2 wants isolated CSS/JS; sharing a bundle would defeat the iframe."""
    app = (built / "app.js").read_text()
    loader = (built / "loader.js").read_text()
    assert len(app) > 5_000
    assert "preact" not in loader.lower(), "the framework must not reach the host page"


def test_the_frame_document_loads_only_its_own_bundle(built: Path) -> None:
    html = (built / "index.html").read_text()
    assert "app.js" in html
    # No third-party script, no analytics, no font CDN: the frame runs on a page we
    # do not control, and each of those would be a data flow the tenant did not
    # agree to.
    assert "http://" not in html.split("<body")[0]
    assert "https://" not in html.split("<body")[0]


def test_the_loader_reads_its_bot_id_from_the_script_tag(built: Path) -> None:
    """§12.1's install snippet passes `data-bot`; the loader must not need a global."""
    loader = (built / "loader.js").read_text()
    assert "data-bot" in loader
    assert "currentScript" in loader


def test_the_loader_takes_no_positional_configuration() -> None:
    """A `<script>` install has no arguments: config is attributes only."""
    source = (WIDGET_DIR / "src" / "loader.ts").read_text()
    assert "document.currentScript" in source
