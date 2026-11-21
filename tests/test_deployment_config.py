"""SEC-01: the edge must terminate TLS and send HSTS.

These assert on the shipped configuration, since a running stack cannot prove the
production path in CI. The HTTPS path itself was verified live at the M0 gate
(Caddy internal CA: HTTP/2 200, HSTS present, :80 -> 308 https).
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_caddy_enables_automatic_https_for_a_configured_domain() -> None:
    config = (ROOT / "caddy" / "Caddyfile").read_text()
    assert "auto_https off" not in config, "auto-HTTPS must not be disabled in production"
    assert "EVA_DOMAIN" in config
    # A bare domain (not http://) is what makes Caddy obtain a certificate, and the
    # dev fallback must be `:80` (a listener address) rather than `http://localhost`
    # (a host name).
    assert "{$EVA_DOMAIN::80}" in config


def test_dev_fallback_does_not_pin_the_host_header() -> None:
    """A site address naming a host serves only that Host and answers every other
    one with **200 and an empty body**.

    Measured 2026-11-21: with the dev default `http://localhost`, requests to the same
    box by IP or tailnet name returned `200 Content-Length: 0` -- a blank page with no
    error, which a user reads as "the app is broken" and which gives nothing to
    diagnose. `:80` is a listener address and accepts any Host.
    """
    config = (ROOT / "caddy" / "Caddyfile").read_text()
    assert "http://localhost}" not in config, (
        "the dev site address must not name a host: it would serve only Host: localhost"
    )
    # `http://` (scheme only) would bind :80 but emit an https redirect that loops
    # with no TLS configured, so the fallback must be the bare listener.
    assert not re.search(r"\{\$EVA_DOMAIN:http://\}", config), (
        "the dev fallback must be :80, not http:// (which redirects and loops without TLS)"
    )


def test_hsts_is_sent_only_over_tls() -> None:
    config = (ROOT / "caddy" / "Caddyfile").read_text()
    assert "Strict-Transport-Security" in config
    assert "max-age=31536000" in config
    # Gated on https, otherwise local plaintext dev gets a poisoned HSTS policy.
    assert re.search(r"@tls\s+protocol\s+https", config)


def test_https_port_is_published() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    assert '"443:443"' in compose


def test_domain_default_is_applied_in_compose_not_caddy() -> None:
    """Caddy's {$VAR:default} only applies when the variable is unset; passing an
    empty EVA_DOMAIN blanked the site address and crash-looped the container
    (observed at the M0 gate)."""
    compose = (ROOT / "compose.yaml").read_text()
    assert "EVA_DOMAIN:" in compose
    # `:80`, because compose always sets the variable, so the Caddyfile default never
    # applies and the compose value is what Caddy actually matches on. `http://localhost`
    # here pinned the site to one Host and blanked every other (see the test above).
    assert "${EVA_DOMAIN:-:80}" in compose
    assert "${EVA_DOMAIN:-}" not in compose
    assert "${EVA_DOMAIN:-http://localhost}" not in compose
