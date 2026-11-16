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
    # A bare domain (not http://) is what makes Caddy obtain a certificate.
    assert "{$EVA_DOMAIN:http://localhost}" in config


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
    assert "${EVA_DOMAIN:-http://localhost}" in compose
    assert "${EVA_DOMAIN:-}" not in compose
