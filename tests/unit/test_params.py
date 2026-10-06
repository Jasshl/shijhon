from __future__ import annotations

from urllib.parse import parse_qsl

from shijhon.proxy.params import (
    RestCall,
    body_kind,
    is_form,
    media_type,
    parse_pairs,
    rewrite_pairs,
    unreadable,
)

FORM = [(b"content-type", b"application/x-www-form-urlencoded; charset=utf-8")]


def test_repeated_keys_keep_order_and_blank_values() -> None:
    assert parse_pairs(b"id=a&id=b&x=&id=a") == [("id", "a"), ("id", "b"), ("x", ""), ("id", "a")]


def test_form_values_come_before_query_values_like_navidrome() -> None:
    # Go's ParseForm: body values first; Navidrome reads the first value of a key.
    call = RestCall.build("star", "POST", b"/rest/star", b"u=me&id=q1", FORM, b"id=f1&id=f2")
    assert call.getall("id") == ["f1", "f2", "q1"]
    assert call.get("id") == "f1"
    assert call.get("u") == "me"
    assert call.form


def test_non_form_body_is_not_parsed() -> None:
    call = RestCall.build("x", "POST", b"/rest/x", b"", [(b"content-type", b"text/plain")], b"id=1")
    assert call.params == []
    assert not call.form


def test_rewrite_keeps_untouched_bytes() -> None:
    raw = b"u=me&t=abc&s=x%2Fy&songId=sj-1&songId=native&name=My+List%21&odd=%E2%9C%93"
    out = rewrite_pairs(raw, lambda k, v: "N1" if v == "sj-1" else None)
    assert out == raw.replace(b"songId=sj-1", b"songId=N1")


def test_rewritten_call_changes_query_and_form() -> None:
    call = RestCall.build("star", "POST", b"/rest/star", b"id=sj-a", FORM, b"id=sj-b&c=app")
    changed = call.rewritten(lambda k, v: v.upper() if v.startswith("sj-") else None)
    assert changed.query == b"id=SJ-A"
    assert changed.body == b"id=SJ-B&c=app"
    assert changed.getall("id") == ["SJ-B", "SJ-A"]


def test_a_form_is_what_gos_parseform_reads() -> None:
    """Navidrome reads a form body only of a POST, PUT or PATCH, whatever the letter case
    of its media type: Shijhon reads the same parameters, so the same user."""
    body, query = b"u=body", b"u=query"
    shouted = [(b"Content-Type", b"Application/X-WWW-Form-Urlencoded")]
    for method in ("POST", "PUT", "PATCH"):
        assert (
            RestCall.build("ping", method, b"/rest/ping", query, shouted, body).get("u") == "body"
        )
    for method in ("DELETE", "OPTIONS", "GET"):
        call = RestCall.build("ping", method, b"/rest/ping", query, FORM, body)
        assert not call.form and call.get("u") == "query"
    other = [(b"content-type", b"application/x-www-form-urlencoded-not")]
    assert RestCall.build("ping", "POST", b"/rest/ping", query, other, body).get("u") == "query"


def test_a_form_whatever_the_case_and_shape_of_its_content_type() -> None:
    """Go lowers and trims the media type before it compares it (``mime.ParseMediaType``);
    its parameters, and how the header itself is written, change nothing."""
    for value in (
        b"application/x-www-form-urlencoded",
        b"Application/X-WWW-Form-Urlencoded",
        b"APPLICATION/X-WWW-FORM-URLENCODED;CHARSET=UTF-8",
        b"  application/x-www-form-urlencoded  ; charset=utf-8",
        b"\tapplication/x-www-form-urlencoded\t",
        b"application/x-www-form-urlencoded;",
        b"application/x-www-form-urlencoded\xc2\xa0",  # a no-break space: trimmed too
        "appl\u0130cation/x-www-form-urlencoded".encode(),  # lowers to "i" in Go
    ):
        for name in (b"content-type", b"Content-Type", b"CONTENT-TYPE"):
            assert is_form("POST", [(name, value)]), value
            call = RestCall.build("x", "POST", b"/rest/x", b"id=q", [(name, value)], b"id=b")
            assert call.form and call.get("id") == "b"
    for value in (
        b"",
        b"text/plain",
        b"application/json",
        b"multipart/form-data; boundary=x",
        b"application/x-www-form-urlencoded-not",
        b"application /x-www-form-urlencoded",
        b"x; application/x-www-form-urlencoded",
        b"application/x-www-form-urlencoded\xa0",  # no valid text: not trimmed by Go
    ):
        assert not is_form("POST", [(b"content-type", value)]), value
    assert not is_form("POST", [])
    # The first header counts, as for Go.
    both = [(b"content-type", b"text/plain"), (b"content-type", FORM[0][1])]
    assert not is_form("POST", both) and is_form("POST", both[::-1])
    for method in ("GET", "HEAD", "DELETE", "OPTIONS", "post"):
        assert not is_form(method, FORM)


def test_a_content_type_gos_parser_refuses_is_navidrome_s_refusal() -> None:
    """(Each of these is compared with the real Navidrome in suite A.)"""
    form = "application/x-www-form-urlencoded"
    for value, expected in (
        (f"{form}; charset=utf-8", (form, True)),
        (f'{form}; charset="utf-8"; x="a;b"', (form, True)),
        (f"{form}; a=1; a=1", (form, True)),  # the same value twice
        (f"{form}; a=1; A=2", ("", False)),
        (f'{form}; x="a\\ b"; x="a b"', ("", False)),  # a backslash stays before a space
        (f'{form}; x="a\\ b"; x="a\\\\ b"', (form, True)),  # ... and is one when escaped
        (f"{form};\u2003charset=utf-8", (form, True)),
        (f"{form}; charset", (form, False)),
        (f"{form}; charset=", (form, False)),
        (f'{form}; charset="utf-8', (form, False)),
        (f"{form} charset=utf-8", ("", False)),
        (f"{form};", (form, True)),
        (f"{form}; charset=utf-8; ;", (form, False)),
        ("text", ("text", True)),
        ("text/", ("", False)),
        ("/plain", ("", False)),
        ("not a media type", ("", False)),
    ):
        assert media_type(value.encode()) == expected, value
        kind = body_kind("POST", [(b"content-type", value.encode())])
        assert kind == ("refused" if not expected[1] else "form" if expected[0] == form else None)
    assert body_kind("GET", [(b"content-type", b"not a media type")]) is None
    assert body_kind("POST", [(b"content-type", b"")]) is None and body_kind("POST", []) is None


def test_parameters_are_read_as_before_within_the_memory_of_their_size() -> None:
    for raw in (
        b"a=1&b=2&a=3",
        b"a&b=&=c&&d=%zz&e=%4&f=1+2%2B3",
        b"q=%C3%A9t%c3%a9&%6bey=v",
        "é=ü".encode(),
    ):
        assert parse_pairs(raw) == parse_qsl(raw.decode(), keep_blank_values=True)
    # A long value decoded a window at a time, whatever falls on the windows' edges.
    for size in (32_766, 32_767, 32_768, 32_769, 100_003):
        for unit in (b"%41", b"a%41", b"ab%41", b"%c3%a9x", b"%", b"+%2b"):
            raw = b"k=" + (unit * (size // len(unit) + 2))[:size]
            assert parse_pairs(raw) == parse_qsl(raw.decode(), keep_blank_values=True)


def test_what_gos_query_parser_refuses() -> None:
    for raw in (b"a=%zz", b"a=100%", b"a=%4", b"%", b"a=1;b=2", b";", b"&".join([b"a"] * 10_001)):
        assert unreadable(raw), raw[:20]
    for raw in (b"", b"a=1&b=2", b"a=%41%2f%2F", b"a=+&=x&&b", b"&".join([b"a"] * 10_000)):
        assert not unreadable(raw), raw[:20]
    # Bytes that are no UTF-8 in two values of one media-type parameter stay two values.
    form = b"application/x-www-form-urlencoded"
    assert media_type(form + b'; x="\xff"; x="\xfe"') == ("", False)
    assert media_type(form + b'; x="\xff"; x="\xff"') == (form.decode(), True)
