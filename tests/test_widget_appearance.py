"""Appearance validation (WIDG-09, §12.2).

`bots.appearance` is tenant JSONB that renders inside a third party's page, so these
tests are security tests as much as feature tests: each one asserts that a value
which is hostile in a host page does not reach it.
"""

from __future__ import annotations

from app.widget import appearance


def test_defaults_when_nothing_is_configured() -> None:
    assert appearance.normalise({}) == {}
    assert appearance.normalise(None) == {}
    # A JSONB column can hold an array or a scalar as easily as an object. Returning
    # a list here would have made `.get` in the caller an AttributeError.
    assert appearance.normalise("not a dict") == {}
    assert appearance.normalise(["a"]) == {}
    assert appearance.normalise(7) == {}


def test_a_valid_configuration_round_trips() -> None:
    configured = {
        "primaryColor": "#1f6feb",
        "position": "bottom-left",
        "avatarUrl": "https://cdn.example.com/avatar.png",
        "headerText": "Ask Northwind",
    }
    assert appearance.normalise(configured) == configured


def test_hex_shorthand_and_functional_colours_are_accepted() -> None:
    assert appearance.normalise({"primaryColor": "#abc"})["primaryColor"] == "#abc"
    assert (
        appearance.normalise({"primaryColor": "rgb(31, 111, 235)"})["primaryColor"]
        == "rgb(31, 111, 235)"
    )
    assert (
        appearance.normalise({"primaryColor": "hsl(210, 80%, 52%)"})["primaryColor"]
        == "hsl(210, 80%, 52%)"
    )


def test_a_colour_that_escapes_into_css_is_dropped() -> None:
    for hostile in (
        "red;}</style><script>alert(1)</script>",
        "#fff; background:url(javascript:alert(1))",
        "var(--host-secret)",
        "expression(alert(1))",
        "red",
        "url(data:text/html,<script>)",
        "#ffff",
        "",
    ):
        assert "primaryColor" not in appearance.normalise({"primaryColor": hostile}), (
            f"{hostile!r} was accepted"
        )


def test_an_avatar_must_be_https() -> None:
    """`data:` would put tenant-controlled bytes in an <img>; `http:` would be
    blocked as mixed content on every realistic host page."""
    assert "avatarUrl" not in appearance.normalise(
        {"avatarUrl": "data:image/svg+xml;base64,PHN2Zz4="}
    )
    assert "avatarUrl" not in appearance.normalise(
        {"avatarUrl": "http://example.com/a.png"}
    )
    assert "avatarUrl" not in appearance.normalise(
        {"avatarUrl": "javascript:alert(1)"}
    )
    assert "avatarUrl" not in appearance.normalise(
        {"avatarUrl": "https://user:pass@example.com/a.png"}
    )
    assert "avatarUrl" not in appearance.normalise({"avatarUrl": "https://exa mple.com/a.png"})


def test_the_avatar_length_is_capped() -> None:
    long_url = "https://example.com/" + "a" * 600
    assert "avatarUrl" not in appearance.normalise({"avatarUrl": long_url})


def test_header_text_is_length_capped_and_stripped_of_control_characters() -> None:
    assert "headerText" not in appearance.normalise({"headerText": "x" * 61})
    # A bidi override makes the rendered string differ from the stored one, so it is
    # stripped rather than kept.
    cleaned = appearance.normalise({"headerText": "Support\u202e evil"})
    assert cleaned["headerText"] == "Support evil"
    assert "\u202e" not in cleaned["headerText"]
    assert appearance.normalise({"headerText": "  Ask us  "})["headerText"] == "Ask us"
    assert "headerText" not in appearance.normalise({"headerText": "   "})


def test_unknown_keys_are_dropped_rather_than_passed_through() -> None:
    """A field the widget does not understand must not reach the host page: the
    next person to read `appearance` in the frame would otherwise trust it."""
    result = appearance.normalise(
        {
            "primaryColor": "#000",
            "customCss": "body{display:none}",
            "scripts": ["alert(1)"],
            "position": "middle",
        }
    )
    assert result == {"primaryColor": "#000"}


def test_position_is_a_closed_set() -> None:
    assert appearance.normalise({"position": "bottom-right"})["position"] == "bottom-right"
    for bad in ("left", "top-right", "fixed", "", "bottom-right;top:0"):
        assert "position" not in appearance.normalise({"position": bad}), bad
