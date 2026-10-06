"""Logging setup. Credentials travel in Subsonic query strings, so nothing Shijhon logs
may contain a query string, a full URL or a request body. And clients choose much of what
is logged - method names, client names, titles, parameters -, so a record is one line
whatever they send: control characters are escaped (a newline would start a forged
record), and the lines of a traceback are indented."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable

# Subsonic credential parameters and anything that looks like one.
_SECRET_PARAM = re.compile(r"(?i)\b(p|t|s|jwt|apiKey|password|token|salt)=([^&\s\"']+)")
_URL = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")


def redact(text: str) -> str:
    """Remove credential parameters and reduce URLs to their origin."""

    def _origin(match: re.Match[str]) -> str:
        url = match.group(0)
        scheme, _, rest = url.partition("://")
        host = rest.split("/", 1)[0].split("?", 1)[0]
        host = host.rsplit("@", 1)[-1]  # drop userinfo
        return f"{scheme}://{host}/…"

    return _URL.sub(_origin, _SECRET_PARAM.sub(r"\1=…", text))


# Control characters, line and paragraph separators, and the bidirectional controls that
# reorder what a terminal shows.
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def escape(text: str, *, keep: str = "") -> str:
    """Text fit for one log line: control characters written out (``\\n``, ``\\x1b``,
    ``\\u2028``), everything else as it is. ``keep``: characters left alone."""

    def _written(match: re.Match[str]) -> str:
        char = match.group(0)
        if char in keep:
            return char
        code = ord(char)
        return _NAMED.get(char) or (f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")

    return _UNSAFE.sub(_written, text)


def clean(text: str) -> str:
    """A message fit for the log: on one line, without credentials. Escaped first: a
    credential a newline cuts in two is then removed whole."""
    return redact(escape(text))


class RedactingFilter(logging.Filter):
    """Last line of defense: redact every formatted record, and keep its message on one
    line."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = clean(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


class RedactingFormatter(logging.Formatter):
    """Redacts the whole formatted record, including tracebacks (exception messages from
    HTTP libraries can contain URLs with credentials). A record's first line is its
    message, escaped; what follows - a traceback, whose exception message may hold a
    client's text too - is indented, so no line of it reads as a record of its own."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        return escape(super().formatMessage(record))  # also without the filter

    def format(self, record: logging.LogRecord) -> str:
        first, _, rest = redact(super().format(record)).partition("\n")
        if not rest:
            return first
        lines = redact(escape(rest, keep="\n")).split("\n")
        return "\n".join([first, *(f"  {line}" for line in lines)])


def configure_logging(level: str = "INFO", debug: Iterable[str] = ()) -> None:
    """``debug``: loggers logged at debug level whatever ``level`` says."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in debug:
        logging.getLogger(name).setLevel(logging.DEBUG)
    # uvicorn's access log prints full request lines including credentials.
    logging.getLogger("uvicorn.access").disabled = True
    quiet_libraries()


def quiet_libraries() -> None:
    """Libraries whose logging would print secrets: httpx logs full request URLs at INFO,
    aiosqlite every statement with its parameters (add-on URLs, saved tokens) at DEBUG."""
    for name in ("httpx", "httpcore", "aiosqlite"):
        logging.getLogger(name).setLevel(logging.WARNING)
