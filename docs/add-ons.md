# Add-ons

Shijhon gets audio from **add-ons**: small HTTP services that find a track and hand over
a link to its audio: a file, or a DASH stream ([below](#dash-links)). HLS links do not
play. Shijhon uses existing add-on protocols instead of a new one: the one
[Eclipse Music](https://eclipsemusic.app/docs) uses, and [BitChord](https://bitchord.kushagrasingh.in/docs)'s
close variant of it. Shijhon is an independent project, not affiliated with or endorsed by
Eclipse Music or BitChord.

## Adding an add-on

On the dashboard's **Add-ons** page: add one by its URL, order, enable, disable or remove
it, and fill in the settings its manifest declares.

Or in the configuration file, in your order:

```toml
[[addons]]
name = "My add-on"
base_url = "https://addon.example.invalid/your-configuration"
reach = "public"
settings = {}           # the add-on's own settings, if it has any: single values
# budget_seconds = 45   # its own time to first audio, e.g. for a slow one
```

- `base_url` is the add-on's manifest URL, with or without `/manifest.json`; a query in it
  is kept and sent with every request.
- A list in the file is applied at every start, until you change the add-ons on the
  dashboard ([which list applies](reference/settings.md#the-add-on-list-the-files-or-the-dashboards)).
- `reach` says where the add-on, its redirects and its audio links may lead:

| `reach` | Addresses |
|---|---|
| `"public"`, the default | Public addresses only |
| `"loopback"` | Also Shijhon's own machine. In a container that is the container itself |
| `"private"` | Also Shijhon's own machine and your network. In a container, an add-on on the Docker host or in another container needs this |

## Routing

`[delivery] routing` picks the add-on for a song:

- `"ordered"`, the default: the add-ons are tried in your order.
- `"primary_first"`: `primary_source` starts at once, while the add-ons that can say
  whether they have a song ready are asked. The others are ordered by how fast they
  delivered recently (`reliable_source` first, until that is known).
- `"ready_first"`: the first add-on that says it has the song ready, else
  `reliable_source`.

`primary_source` and `reliable_source` take an add-on's `name`, exactly as you gave it.

## Limits

An add-on gets at most 2 API requests a second and 4 audio requests being opened at once,
from all users and Shijhon's background work together. Each user can look up 4 songs and
download 2 whole songs at a time, and start 4 downloads in quick succession, then one
every 30 seconds. Over a limit, a request waits its turn; the song being played goes
first. An add-on you run yourself can have higher limits of its own
(`requests_per_second`, `request_burst`, `audio_openings`). Add-ons at one address share
their limits ([every limit](reference/add-on-protocol.md#limits)).

## DASH links

By default a DASH link plays from its first segments, served as one MP4 file. With
`[delivery] dash_start = "complete"`, it plays once its segments are joined into a FLAC,
M4A or MP3 file.
Songs saved to the library are always such files. ffmpeg does the copying: the Docker
image has it. Unsupported streams, and audio whose length differs from the song's
([`length_tolerance_seconds`, `length_tolerance_percent`](reference/settings.md#delivery)),
go to the next add-on ([details](reference/add-on-protocol.md#dash-links)).

## `allowDownloads`

When an add-on's manifest has `"allowDownloads": 0` (or `false`, or `"0"`), Shijhon fetches
no whole songs from it: downloads, conversions, share links and the jukebox use your other
add-ons. Plays, and preparing the next songs in the queue, still use it.

## When an add-on fails

- **It does not have the song** (HTTP 404): the next add-on is asked.
- **"Too many requests"** (HTTP 429): nothing new is asked of it until its `Retry-After`,
  an hour at most, or 30 seconds when it names none. Songs already playing keep playing.
- **Three errors in a row** (HTTP 5xx, no connection, a broken answer), no more than ten
  minutes apart: it is passed over for 30 seconds, and again after each further error
  until it delivers audio.

## For add-on authors

An add-on needs `/stream/{id}` and one way to find a song: by ISRC (`/resolve-isrc`), by
title, artist and length (`/resolve`), or a search (`/search?q=`).
[The add-on protocol](reference/add-on-protocol.md) has the rest.
