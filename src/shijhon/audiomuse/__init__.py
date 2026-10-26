"""The optional AudioMuse-AI connection: sound facts and sound-based suggestions for
described playlists (``shijhon.playlists.sound``). ``docs/sound-analysis.md`` describes what
it adds and how to set it up."""

from shijhon.audiomuse.client import AudioMuseClient
from shijhon.audiomuse.service import AudioMuse

__all__ = ["AudioMuse", "AudioMuseClient"]
