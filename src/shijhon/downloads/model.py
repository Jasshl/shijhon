"""Bounded private preparation data; no audio links are persisted."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from shijhon.playlists.import_formats import MAX_ROWS

# The longest playlist prepared is as long as a playlist import may be ([playlists]
# import_max_entries, ``Preparation.max_entries``); this is that setting's default.
MAX_ENTRIES = MAX_ROWS
MAX_PLAYLISTS = 1000
MAX_BATCHES = 100
MAX_AGENT_REQUESTS = 1000
ACTIVE = ("waiting", "preparing", "fetching", "verifying")


class PreparationError(Exception):
    """Fixed messages safe to display; remote error bodies never reach the dashboard."""


class ConnectionRequired(PreparationError):
    """First use or explicit disconnect; the normal connection form is sufficient."""


class Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    user_id: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=100)
    token: SecretStr
    salt: SecretStr

    def stored(self) -> str:
        return json.dumps(
            {
                "user_id": self.user_id,
                "username": self.username,
                "token": self.token.get_secret_value(),
                "salt": self.salt.get_secret_value(),
            }
        )


def native_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 100
        or any(not c.isprintable() for c in value)
        or value.startswith("sh.")
    ):
        raise PreparationError("The playlist contains an invalid native song or playlist ID.")
    return value
