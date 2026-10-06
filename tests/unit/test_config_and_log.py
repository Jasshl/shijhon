from __future__ import annotations

import logging
from pathlib import Path

import pytest

from shijhon.cli import main
from shijhon.config import load_settings
from shijhon.log import RedactingFilter, redact


def test_toml_env_and_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "shijhon.toml"
    config.write_text('[navidrome]\nurl = "http://navidrome.test:4533"\nuser = "svc"\n')
    monkeypatch.setenv("SHIJHON_NAVIDROME__USER", "from-env")
    settings = load_settings(config, state_dir=tmp_path)
    assert settings.navidrome.url == "http://navidrome.test:4533"
    assert settings.navidrome.user == "from-env"
    assert settings.state_dir == tmp_path


def test_password_file(tmp_path: Path) -> None:
    secret = tmp_path / "pw"
    secret.write_text("s3cret\n")
    settings = load_settings(None, navidrome={"password_file": secret})
    assert settings.navidrome.service_password() == "s3cret"


@pytest.mark.parametrize(
    ("given", "folder"),
    [("_shijhon", "_shijhon"), ("./x", "x"), ("x/", "x"), ("a//b/./c", "a/b/c"), ("./a/b", "a/b")],
)
def test_the_placeholder_folder_is_kept_as_its_parts(given: str, folder: str) -> None:
    """What is a placeholder is told by this prefix of its library path: "./x" would match
    no path (its placeholders taken for owned songs)."""
    assert load_settings(None, placeholders={"folder": given}).placeholders.folder == folder


@pytest.mark.parametrize("folder", ["/abs", "../outside", "", ".", "./", "x/../..", "./."])
def test_placeholder_folder_must_stay_inside_library(folder: str) -> None:
    with pytest.raises(ValueError):
        load_settings(None, placeholders={"folder": folder})


def test_delivery_wait_settings() -> None:
    delivery = load_settings(None).delivery
    assert delivery.max_wait_seconds == 30.0 and delivery.primary_cooldown_switches == 3
    assert delivery.primary_cooldown_timeouts == 2
    with pytest.raises(ValueError, match="max_wait_seconds"):
        load_settings(None, delivery={"budget_seconds": 9.0, "max_wait_seconds": 5.0})
    with pytest.raises(ValueError):
        load_settings(None, delivery={"primary_cooldown_switches": 0})


def test_a_wait_cap_below_the_budget_is_refused_at_startup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One clear line naming both settings, no traceback and no secrets."""
    config = tmp_path / "shijhon.toml"
    config.write_text(
        '[navidrome]\npassword = "hunter2"\n[delivery]\nbudget_seconds = 9\nmax_wait_seconds = 5\n'
    )
    with pytest.raises(SystemExit) as stopped:
        main(["--config", str(config), "serve"])
    assert stopped.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("shijhon: configuration error in delivery: delivery.max_wait_seconds")
    assert "(5 s) is below the byte-zero budget delivery.budget_seconds (9 s)" in err
    assert "hunter2" not in err and "Traceback" not in err


def test_a_dash_quality_range_whose_first_end_is_above_its_second_is_refused() -> None:
    with pytest.raises(ValueError, match=r"dash_quality_from \(320\) is above"):
        load_settings(None, delivery={"dash_quality_from": "320", "dash_quality_to": "128"})
    with pytest.raises(ValueError, match="dash_quality_from"):
        load_settings(None, delivery={"dash_quality_from": "lossless", "dash_quality_to": 320})
    same = load_settings(None, delivery={"dash_quality_from": 192, "dash_quality_to": 192})
    assert (same.delivery.dash_quality_from, same.delivery.dash_quality_to) == ("192", "192")
    default = load_settings(None).delivery
    assert (default.dash_quality_from, default.dash_quality_to) == ("any", "lossless")


def test_redact_removes_credentials_and_url_paths() -> None:
    line = "GET /rest/stream?u=bob&t=abc123&s=salt9&id=1 via https://user:pw@cdn.example/a/b?sig=x"
    cleaned = redact(line)
    for secret in ("abc123", "salt9", "pw@", "/a/b", "sig=x"):
        assert secret not in cleaned
    assert "u=bob" in cleaned
    assert "https://cdn.example/…" in cleaned


def test_filter_redacts_formatted_records() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "p=%s", ("hunter2",), None)
    RedactingFilter().filter(record)
    assert "hunter2" not in record.getMessage()


def test_formatter_redacts_tracebacks() -> None:
    from shijhon.log import RedactingFormatter

    try:
        raise RuntimeError(
            "HTTP 500 for url 'http://navidrome.test:4533/rest/startScan?u=svc&t=abc123&s=salt9'"
        )
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    text = RedactingFormatter("%(message)s").format(record)
    assert "abc123" not in text and "salt9" not in text and "startScan" not in text


# --- a record is one line, whatever clients send --------------------------------------


def test_escape_writes_control_characters_out() -> None:
    from shijhon.log import escape

    assert escape("Title\nwith\ttabs\r") == "Title\\nwith\\ttabs\\r"
    assert escape("\x1b[31mred\x00\x7f\x85") == "\\x1b[31mred\\x00\\x7f\\x85"
    separators = "line\u2028separator\u2029 and \u202eoverride\u2066"
    assert escape(separators) == "line\\u2028separator\\u2029 and \\u202eoverride\\u2066"
    assert (
        escape("Vi\u00f6l Ask - Kv\u00f3ld (\u65e5\u672c\u8a9e)")
        == "Vi\u00f6l Ask - Kv\u00f3ld (\u65e5\u672c\u8a9e)"
    )  # text stays
    assert escape("a\nb\x1b", keep="\n") == "a\nb\\x1b"


def test_a_clients_newline_cannot_start_a_log_record() -> None:
    """The access line of ``GET /rest/ping%0A<a forged record>`` with a newline in ``c``."""
    from shijhon.log import RedactingFormatter

    forged = "2026-09-30 12:00:00,000 INFO shijhon.dashboard: admin signed in"
    label = f"rest ping\n{forged} c=app\r\n{forged}"
    record = logging.LogRecord(
        "shijhon.access", logging.INFO, __file__, 1, "%s %s %s", (label, "GET", 200), None
    )
    RedactingFilter().filter(record)
    line = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s").format(record)
    assert "\n" not in line and "\r" not in line
    assert "rest ping\\n2026-09-30" in line and "c=app\\r\\n2026" in line


def test_a_traceback_cannot_carry_a_forged_record() -> None:
    from shijhon.log import RedactingFormatter

    try:
        raise ValueError("no such title: x\n2026-09-30 12:00:00,000 INFO forged: record\x1b[2J")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    text = RedactingFormatter("%(levelname)s %(message)s").format(record)
    first, *rest = text.split("\n")
    assert first == "ERROR boom" and len(rest) > 2
    assert all(line.startswith("  ") for line in rest)  # no line reads as a record
    assert "\x1b" not in text and "\\x1b[2J" in text


def test_recent_errors_keep_one_line() -> None:
    from shijhon.dashboard.errors import RecentErrors

    errors = RecentErrors()
    record = logging.LogRecord(
        "shijhon.views", logging.WARNING, __file__, 1, "search of %s failed", ("a\nb",), None
    )
    errors.emit(record)
    assert [e.message for e in errors.recent()] == ["search of a\\nb failed"]
    # A credential cut in two by a newline is removed whole, not shown from the cut on.
    cut = logging.LogRecord(
        "shijhon.proxy", logging.WARNING, __file__, 1, "%s failed", ("p=first\nSECOND",), None
    )
    errors.emit(cut)
    assert "SECOND" not in errors.recent()[0].message and "first" not in errors.recent()[0].message


def test_the_formatter_alone_keeps_a_message_on_one_line() -> None:
    """A handler with the formatter but not the filter (a log file of its own)."""
    from shijhon.log import RedactingFormatter

    record = logging.LogRecord(
        "shijhon.access", logging.INFO, __file__, 1, "%s %s", ("rest ping c=app\nforged", 200), None
    )
    assert RedactingFormatter("%(name)s: %(message)s").format(record) == (
        "shijhon.access: rest ping c=app\\nforged 200"
    )
