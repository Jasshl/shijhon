"""A made-up catalog adapter with the kind of settings a real one has - a region, a token
from one of three places, headers for its token service, a request rate - for the tests of
what Shijhon does with an adapter's declaration (``shijhon.catalog.plugin``): reading its
settings from the file and the environment, the environment's locks, the dashboard's Catalog page,
secrets, and settings that must stay with their host. ``[catalog] kind = "sample"``
(registered by ``tests/conftest.py``); it answers from the demo catalog's records.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from shijhon.catalog.plugin import Adapter, Context, Problem, Words
from shijhon_demo_catalog import DemoCatalog

KIND = "sample"
_WRITE_ONLY = "Write-only: stored on the server and never shown."


class SampleSettings(BaseModel):
    region: str = "xx"  # two letters
    # A token, or a file holding it, or the URL of a service issuing one: one choice.
    token: SecretStr | None = None
    token_file: Path | None = None
    token_url: SecretStr | None = None
    headers: dict[str, SecretStr] = Field(default_factory=dict)  # sent to the catalog
    token_headers: dict[str, SecretStr] = Field(default_factory=dict)  # to the token service
    requests_per_second: float = Field(default=10.0, gt=0)

    def secret_token(self) -> str | None:
        if self.token is not None:
            return self.token.get_secret_value()
        if self.token_file is not None:
            return self.token_file.read_text().strip() or None
        return None


class SampleCatalog(DemoCatalog):
    """The demo's records, with what the adapter was built from (for the tests to see)."""

    def __init__(self, settings: Any, context: Context) -> None:
        super().__init__(region=settings.region)
        self.settings = settings
        self.context = context
        self.token = settings.secret_token()
        self.token_headers = {k: v.get_secret_value() for k, v in settings.token_headers.items()}


def problem(settings: Any) -> Problem | None:
    if not re.fullmatch(r"[a-z]{2}", settings.region or ""):
        return Problem("region", "Enter two letters, e.g. us.", "must be two letters.")
    if settings.token is None and settings.token_file is None and settings.token_url is None:
        return Problem(
            "token",
            "Set a token, a token file or a token service to connect the catalog.",
            "is needed to connect the catalog (or a token file or a token service).",
        )
    if settings.token is None and settings.token_file is not None:
        try:
            readable = bool(Path(settings.token_file).read_text().strip())
        except (OSError, UnicodeDecodeError):
            readable = False
        if not readable:
            return Problem(
                "token_file", "Shijhon can't read a token from this file.", "can't be read."
            )
    return None


SAMPLE = Adapter(
    label="Sample catalog",
    build=SampleCatalog,
    settings=SampleSettings,
    words={
        "region": Words(
            "Region",
            "The two-letter region of the catalog.",
            short=True,
            max_length=2,
            pattern=r"[a-z]{2}",
            pattern_error="Enter two letters, e.g. us.",
        ),
        "token": Words("Token", f"A token. {_WRITE_ONLY} Leave empty to keep the current one."),
        "token_file": Words("Token file", "Or the path of a file holding the token."),
        "token_url": Words(
            "Token service",
            f"Or the URL of a service that issues tokens. {_WRITE_ONLY} Leave empty to keep"
            " the current one.",
        ),
        "headers": Words("Catalog headers", "From the configuration file.", "advanced"),
        "token_headers": Words("Token service headers", "From the configuration file.", "advanced"),
        "requests_per_second": Words(
            "Request rate",
            "Requests at most.",
            "advanced",
            unit="per s",
            spoken="requests per second",
        ),
    },
    one_choice=(("token", "token_file", "token_url"),),
    bound={"token_headers": "token_url"},
    problem=problem,
)
