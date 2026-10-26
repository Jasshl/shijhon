# Add-ons

Shijhon gets audio from **add-ons**: small HTTP services that find a track and hand over
a link to its audio: a file, or a DASH stream ([below](#dash-links)). HLS links do not
play. Shijhon uses existing add-on protocols instead of a new one: the one
[Eclipse Music](https://eclipsemusic.app/docs) uses, and [BitChord](https://bitchord.kushagrasingh.in/docs)'s
close variant of it. Shijhon is an independent project, not affiliated with or endorsed by
Eclipse Music or BitChord.

## Adding an add-on

On the dashboard's **Add-ons** page: add one by its URL, order, enable, disable or remove
it, and fill in the settings its manifest declares. An enabled add-on with a catalog of
its own is also searched by [playlist imports](playlist-imports.md#add-on-catalogs).

To change the order, drag an add-on by its number or use its arrows. **All on** and
**All off** switch the whole list.

Or in the configuration file, in your order:

```toml
[[addons]]
name = "My add-on"
base_url = "https://addon.example.invalid/your-configuration"
reach = "public"
settings = {}           # the add-on's own settings, if it has any: single values
# budget_seconds = 45   # its own time to first audio (and, for playlist imports, to a catalog answer), e.g. for a slow one
# downloads = 2         # whole-song transfers at once; omitted: inherit
# preparations = 1      # new preparations at once; omitted: inherit
# capacity_group = "shared-worker" # same label for add-ons sharing an account or worker
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
fetch 4 whole songs at a time. The optional hourly download quota is off by default.
Over a limit, a request waits its turn; the song being played goes first.
An add-on you run yourself can have higher API limits of its own
(`requests_per_second`, `request_burst`, `audio_openings`). Add-ons at one address share
their limits ([every limit](reference/add-on-protocol.md#limits)).

### Downloads and playlist preparation

An app's standard `download` request uses the download scheduler for each missing song.
On the dashboard's **Downloads** page you can also choose a native playlist and prepare
its audio in the server library. Neither workflow needs a playlist batch detector or a
special add-on. This prepares server files; the app still needs to save its own device
copies. Showing catalog tracks and adding library placeholders do not download audio.

The defaults on **Playback → Downloads and playlist preparation** are 4 songs fetched
at once for everyone together, and 2 per add-on. Under **Advanced settings**, new
preparation is limited to 1 song per add-on and whole-song fetches to 4 per user. All of
these limits are adjustable; 0 removes that cap. The hourly quota is optional and defaults
to 0 (off).
Existing file, environment and dashboard overrides keep their values after an upgrade.

For each song, Shijhon selects an enabled add-on that permits downloads and has a free
slot. It keeps the slot until the transfer ends. A source that cannot report whether
audio is cached remains usable: only the selected source gets the ordinary lookup and
stream request. If it fails, another source is tried in turn. Playback has priority.

Playlist preparation also works with slow add-ons. Its songs wait for a free slot, and
the download timeout (`download_timeout_seconds`, 180 seconds) counts only from the
moment a song gets its slot. When every add-on that could provide a song is paused
(rate limited with HTTP 429 and `Retry-After`, or cooling down after errors), the song
waits for the pause to end instead of failing. Nothing is sent to a paused add-on, or to
add-ons that share its limits, until then. A song fails when no enabled add-on permits
downloads or has it, or after waiting an hour in all for paused add-ons. A waiting song
holds none of the user's download turns. When it gets a slot while the user's turns are
busy, or while an app is fetching the same song, it gives the slot back and tries again
shortly, so app requests go first. If an app puts the song in place meanwhile, the song
counts as ready. A song your app reports as playing goes ahead of other waiting work.
Cancel also stops waiting songs. An app's `download` request does not wait like this: it
fails at once when its add-ons are paused, and its timeout includes its wait for a slot.

The preparation limit bounds the requests Shijhon starts. An add-on can keep a remote
job running after a request ends; the standard protocol cannot cancel or confirm that
job. When readiness remains uncertain, Shijhon briefly pauses new preparation on that
backend rather than assuming the remote work stopped.

An optional [cached delivery check](reference/add-on-protocol.md#the-cached-delivery-extension)
can select the exact cached variant of the correct recording, at any audio quality.
Shijhon keeps that choice and uses local conversion when needed. A general readiness
answer or quality label does not prove that a file is cached.

On **Add-ons → Settings → Advanced settings**, blank **Songs fetched at once** and **New
preparations at once** inherit the displayed defaults. A number sets an override; 0
removes that add-on's cap. Add-ons at one address always share transfer limits. Give
add-ons the same **Shared account or worker group** label when they use one account or
worker, even at different addresses. A group's strictest nonzero limit applies; 0 does
not cancel another member's cap. Different hostnames do not establish independent
backends. API request pacing still shares by address, independently of these groups.

An account's remaining credits or quota are not part of the standard add-on protocol.
Configure appropriate limits yourself; Shijhon also honors HTTP 429 and `Retry-After`.
An offline save sent as an ordinary stream still uses playback rules because that
request does not reveal offline intent. Native archive downloads are not playlist
preparation jobs; prepare the playlist first when it contains placeholders.

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
