"""Subsonic request parameters.

Navidrome merges form bodies into the query parameters, so Shijhon reads both, keeping
repeated keys in order. When a value must change (a catalog ID becoming a native ID),
only that ``key=value`` piece is re-encoded; every other byte is forwarded as sent.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote_plus, unquote_to_bytes

FORM_TYPE = "application/x-www-form-urlencoded"
FORM_METHODS = ("POST", "PUT", "PATCH")
# The longest body Navidrome 0.64.2 reads (its form middleware's limit): it answers a longer
# form with an error whatever the Subsonic method, before it looks at any parameter.
BODY_LIMIT = 10 << 20
# RFC 2045's "tspecials", and what else ends a token of a media type (Go's ``mime``).
_TSPECIALS = '()<>@,;:\\"/[]?='
_NOT_TOKEN = re.compile(r'[\x00-\x20\x7f-\U0010ffff()<>@,;:\\"/\[\]?=]')
# What Go's ``unicode.IsSpace`` takes for a space (Unicode's White_Space).
_SPACE = (
    " \t\n\v\f\r\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def _token(text: str) -> tuple[str, str]:
    """The token ``text`` starts with (possibly none), and the rest."""
    found = _NOT_TOKEN.search(text)
    end = found.start() if found else len(text)
    return text[:end], text[end:]


def _value(text: str) -> tuple[str | None, str]:
    """A media parameter's value (a token or a quoted string) and the rest; None: none."""
    if not text.startswith('"'):
        token, rest = _token(text)
        return (token, rest) if token else (None, text)
    value: list[str] = []
    at = 1
    while at < len(text):
        char = text[at]
        if char == '"':
            return "".join(value), text[at + 1 :]
        if char == "\\" and at + 1 < len(text) and text[at + 1] in _TSPECIALS:
            at += 1  # (a backslash escapes only one of the specials, as in Go; else it stays)
            char = text[at]
        if char in "\r\n":
            return None, text
        value.append(char)
        at += 1
    return None, text


def media_type(value: bytes) -> tuple[str, bool]:
    """A ``Content-Type`` as Go's ``mime.ParseMediaType`` reads it: the media type (lowered
    and trimmed, as Unicode text) and whether Go parses the whole header without an error -
    the type's syntax, each parameter a token and a value, none twice with two values.
    Navidrome answers a
    POST, PUT or PATCH whose header Go refuses with an error, before it looks at anything."""
    # (Bytes that are no UTF-8 stay apart from each other, as in Go's strings.)
    text = value.decode("utf-8", "surrogateescape")
    base, _, _ = text.partition(";")
    media = base.replace("\u0130", "i").lower().strip(_SPACE)
    major, rest = _token(media)
    if not major:
        return "", False
    if rest:
        minor, after = _token(rest[1:]) if rest.startswith("/") else ("", rest)
        if not minor or after:
            return "", False
    rest = text[len(base) :]
    seen: dict[str, str] = {}
    while rest.strip(_SPACE):
        rest = rest.lstrip(_SPACE)
        if not rest.startswith(";"):
            return media, False
        after = rest[1:].lstrip(_SPACE)
        name, after = _token(after)
        after = after.lstrip(_SPACE)
        if not name or not after.startswith("="):
            # (Trailing semicolons are no error.)
            return media, rest.strip(_SPACE) == ";"
        found, after = _value(after[1:].lstrip(_SPACE))
        if found is None:
            return media, False
        if seen.setdefault(name.lower(), found) != found:  # (the same value twice is none)
            return "", False
        rest = after
    return media, True


def body_kind(http_method: str, headers: list[tuple[bytes, bytes]]) -> str | None:
    """How Navidrome treats the request's body, as Go's ``ParseForm`` decides: "form" - a
    POST, PUT or PATCH whose first ``Content-Type`` names the form's media type (in any
    letter case, with any parameters, spaces around it): its parameters count; "refused" -
    one whose ``Content-Type`` Go's parser refuses: Navidrome's error; None: no parameters
    in it (another type, another method)."""
    if http_method not in FORM_METHODS:
        return None
    for key, value in headers:
        if key.lower() == b"content-type":
            if not value:
                return None  # (as none: Go takes it for "application/octet-stream")
            media, valid = media_type(value)
            if not valid:
                return "refused"
            return "form" if media == FORM_TYPE else None
    return None


def is_form(http_method: str, headers: list[tuple[bytes, bytes]]) -> bool:
    """Whether Navidrome reads the request's body as parameters (``body_kind``)."""
    return body_kind(http_method, headers) == "form"


# The most parameters Navidrome 0.64.2 reads from a form or a query (Go's limit on
# ``url.ParseQuery``): with more it answers an error before it looks at any of them.
MAX_PARAMS = 10_000
_WINDOW = 32 * 1024  # bytes of a value decoded at a time


_BAD_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")


def unreadable(raw: bytes) -> bool:
    """Whether Navidrome answers a form or a query with an error instead of reading it, as
    Go's ``url.ParseQuery`` refuses it: more parameters than its limit (its
    ``&``-separated pieces), a semicolon between them, a percent sign that begins no
    escape."""
    return raw.count(b"&") >= MAX_PARAMS or b";" in raw or _BAD_ESCAPE.search(raw) is not None


def _unquoted(piece: bytes) -> str:
    """A key or a value as sent: "+" a space, percent-escapes their bytes (one that is no
    escape stays), read as UTF-8. A long one is decoded a window at a time: megabytes of
    escapes cost no more memory than their size (the standard library's way takes twenty
    times that - before any credential check)."""
    if b"+" in piece:
        piece = piece.replace(b"+", b" ")
    if b"%" not in piece:
        return piece.decode("utf-8", "replace")
    if len(piece) <= _WINDOW:
        return unquote_to_bytes(piece).decode("utf-8", "replace")
    out = bytearray()
    start = 0
    while start < len(piece):
        end = min(len(piece), start + _WINDOW)
        if end < len(piece):  # never cut inside an escape: it begins the next window
            cut = piece.rfind(b"%", end - 2, end)
            end = cut if cut >= 0 else end
        out += unquote_to_bytes(piece[start:end])
        start = end
    return out.decode("utf-8", "replace")


def parse_pairs(raw: bytes) -> list[tuple[str, str]]:
    """The parameters of a query or a form, in order, blank values kept."""
    if not raw:
        return []
    pairs = []
    for piece in raw.split(b"&"):
        if not piece:
            continue
        key, _, value = piece.partition(b"=")
        pairs.append((_unquoted(key), _unquoted(value)))
    return pairs


def rewrite_pairs(
    raw: bytes,
    change: Callable[[str, str], str | None],
    drop: Callable[[str, str], bool] | None = None,
) -> bytes:
    """Re-encode only the pieces whose value ``change`` replaces; leave out the pieces
    ``drop`` names."""
    if not raw:
        return raw
    pieces = raw.split(b"&")
    out = []
    for piece in pieces:
        parsed = parse_pairs(piece)
        if len(parsed) == 1:
            key, value = parsed[0]
            if drop is not None and drop(key, value):
                continue
            new = change(key, value)
            if new is not None and new != value:
                piece = f"{quote_plus(key)}={quote_plus(new)}".encode()
        out.append(piece)
    return b"&".join(out)


@dataclass
class RestCall:
    """One ``/rest/<method>`` request as Shijhon sees it."""

    name: str  # method name without ``.view``
    http_method: str
    raw_path: bytes
    query: bytes
    headers: list[tuple[bytes, bytes]]
    # Raw body when it was read (a form, a small body of another kind); None when it is
    # streamed through: a body Navidrome does not read as parameters either.
    body: bytes | None = None
    form: bool = False
    params: list[tuple[str, str]] = field(default_factory=list)
    # Each key's first value (as Navidrome reads one), made at the first lookup.
    _first: dict[str, str] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def build(
        cls,
        name: str,
        http_method: str,
        raw_path: bytes,
        query: bytes,
        headers: list[tuple[bytes, bytes]],
        body: bytes | None,
    ) -> RestCall:
        # A form as Go's ParseForm reads one (Navidrome's).
        form = body is not None and is_form(http_method, headers)
        # Navidrome merges the form into the query with Go's ParseForm, which puts body
        # values before query values; the first value of a key wins.
        params = (parse_pairs(body) if form and body else []) + parse_pairs(query)
        return cls(name, http_method, raw_path, query, headers, body, form, params)

    def get(self, key: str, default: str | None = None) -> str | None:
        if self._first is None:
            first: dict[str, str] = {}
            for k, v in self.params:
                first.setdefault(k, v)
            self._first = first
        return self._first.get(key, default)

    def getall(self, key: str) -> list[str]:
        return [v for k, v in self.params if k == key]

    @property
    def fmt(self) -> str:
        return self.get("f") or "xml"

    @property
    def xml(self) -> bool:
        """Navidrome answers in XML: any format but "json" and "jsonp" (its default)."""
        return self.fmt not in ("json", "jsonp")

    @property
    def client(self) -> str:
        return self.get("c") or ""

    def rewritten(
        self,
        change: Callable[[str, str], str | None],
        drop: Callable[[str, str], bool] | None = None,
    ) -> RestCall:
        """A copy with values replaced (or left out) in the query and (form) body."""
        query = rewrite_pairs(self.query, change, drop)
        body = rewrite_pairs(self.body, change, drop) if self.form and self.body else self.body
        return RestCall.build(self.name, self.http_method, self.raw_path, query, self.headers, body)


def header(headers: list[tuple[bytes, bytes]], name: bytes) -> str:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""
