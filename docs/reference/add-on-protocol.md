# The add-on protocol

Requests are relative to an add-on's base URL: its manifest URL without `/manifest.json`.
Where the protocol comes from, and its guides: [development.md](../development.md#the-add-on-protocol).

| Request | Used for |
|---|---|
| `GET /manifest.json` | `id`, `name`, `resources` (Shijhon uses `isrc`, `resolve`, `search`, `stream` and `availability`; `catalog` of the add-on that is the catalog), `settings` (their keys, defaults, types and options; a free-text setting is write-only on the dashboard unless it says `"secret": false`), `allowDownloads` ([below](#allowdownloads)) |
| `GET /resolve-isrc?isrc=…` | Finding a track by its ISRC: `{"trackId": "…"}` (or `"id"`); HTTP 404 when the add-on does not have it |
| `GET /resolve?title=…&artist=…&durationMs=…&isrc=…` | The fallback when the ISRC lookup finds nothing: the answer's `item` |
| `GET /search?q=<artist> <title>` | The fallback at an add-on without `resolve`: its `tracks` |
| `GET /stream/{id}` | The link to the audio: `{"url": "…"}`, optionally with `format`, `container`, `codec`, `quality`, `expiresAt` and `headers` |

Shijhon does not send the `quality` and `atmos` request parameters.

## Finding a song

An add-on needs `stream` and at least one way to find a song: `isrc`, `resolve` or
`search`. Shijhon asks, in this order:

1. `/resolve-isrc`, when the add-on declares `isrc` and the song has an ISRC.
2. If that is not asked or finds nothing: `/resolve`, when the add-on declares `resolve`.
3. At an add-on without `resolve`: `/search`, when it declares `search`. An add-on that
   declares `resolve` is never searched for a song.

A song of the add-on that is the catalog is asked for by its own track ID, without a
lookup.

**Matching.** An item of `/resolve`, or a track of `/search`, is the song only with the
same title, artist and version (not a live, remixed or clean one of an explicit track) and
a length within 3 s. One without a length counts only with the song's ISRC; one with the
song's ISRC is the same version, whatever its title says about clean or explicit. A title
or an artist of more than 500 characters is no match.

A search's `tracks` are read as the catalog's are: `id`, `title` (or `name`), `artist`
(a name), `duration` in seconds (or `durationMs`), `isrc`, `explicit` (or `isExplicit`) and
`album`. The first 100 are compared, and a search counts as one lookup at the add-on's
limits. Of several tracks that match:

1. The one with the song's ISRC.
2. The version Shijhon looks for: explicit, or clean when the song's title says "(Clean)"
   or the catalog marks it clean. It is told first by the track's `explicit` flag (true
   or false, 1 or 0, or "true", "explicit", "yes", "false", "clean", "no"), then by its
   `album` title ("(Clean)", "(Clean Version)", "(Edited)" or "(Edited Version)" for clean,
   "(Explicit)" for explicit). Each time: that version first, then a track without a
   marker, then the other version.
3. A track without another ISRC, then one whose own title names the version, then the
   closest length.

## Answers and errors

- The add-on's settings go along as query parameters on every request: the manifest's
  defaults, overridden by the installation's. A query in the add-on's link goes along too.
- Answers are read leniently (numeric IDs, empty optional fields). A JSON answer may be
  1 MiB at most.
- HTTP 404 means "not here", 401 and 403 are refusals (at `/resolve` and `/search`: the
  add-on does not have the song), 410 an expired link, 5xx errors.
- HTTP 429 is a rate limit: nothing new is asked until its `Retry-After` (seconds, a
  fraction accepted, or an HTTP date; an hour at most; 30 seconds when it names none). A
  later 429 can only make that longer. When the audio request answers 429, audio requests
  stop too. The pause is kept in memory, so a restart asks again.
- The audio link is a file (progressive HTTP, ranges welcome) or a DASH manifest, told by
  the answer's `"manifest": "dash"`, a path ending in `.mpd`, or `Content-Type:
  application/dash+xml`. HLS links (`"manifest": "hls"`, `.m3u8`) do not play.
- Every request carries `User-Agent: Shijhon/<version> (+https://github.com/Jasshl/shijhon)`;
  the request for the audio too, unless the link's `headers` set another. Those headers go
  only to the link's own host.

## DASH links

- **Played:** a static manifest with one period, and audio in MP4 (AAC, FLAC, ALAC or MP3),
  in any standard layout (`SegmentTemplate` with a `SegmentTimeline` or a duration,
  `SegmentList`, `SegmentBase`). Relative addresses resolve against the manifest's own
  address, after its redirects; segments may be on another host.
- **Not played** (the next add-on is asked): a live manifest, several periods, protected
  or encrypted audio, other codecs (Opus, Vorbis, AC-3) or containers (WebM), a manifest
  over 1 MiB, more than 3,000 segments, a song over 1 GiB. Audio shorter than the
  catalog's song by more than `length_tolerance_seconds` or `length_tolerance_percent` is
  not played either; the add-on is asked again after a minute.
- **At once**, the default: Shijhon first asks each segment for one byte, which tells its
  size (about 60 requests for a four-minute song, 8 at once), then serves the init segment,
  an index and the segments as one `audio/mp4` file, also for FLAC. A seek waits only for
  the segments it needs. A segment must keep the size its first byte told; after a new
  link, a play continues only with the same segments.
- **Joined first** (`dash_start = "complete"`, and always when the segments do not tell
  their size or the stream is a single file): ffmpeg copies the audio into a FLAC, M4A or
  MP3 file, served once complete. The library gets such a file either way.
- **Requests:** each segment request and each one-byte request is one of the add-on's
  audio requests, up to `dash_segments_at_once` (4) segments at once for one song. Every
  address, redirects included, is checked against the add-on's `reach`. A request that
  fails with HTTP 5xx or a broken connection is asked once more. An app's `HEAD` request,
  or its request for the first bytes, starts the whole song being fetched in the
  background, and a song not ready in time is fetched on up to `max_wait_seconds`.
- **Quality:** the highest within `dash_quality_from` and `dash_quality_to`; with none in
  the range, the next add-on plays. Lossless is above every bitrate; a bitrate within 5 %
  of an end counts as at it, because manifests state peak rates.

## `allowDownloads`

With `"allowDownloads": 0` ([add-ons.md](../add-ons.md#allowdownloads)), songs prepared
for later (`prepare_when_not_ready`) also come from the other add-ons. When none has the
song, a `download` fails, a conversion plays in the add-on's own format, and share links
and the jukebox cannot play it. Readiness checks, and offline saves through ordinary
streams (they look like plays), still use the add-on. The routing reads a changed manifest
only at a restart or a change of the add-on list.

## The catalog endpoints

Asked of one add-on only: the one an installation chose as its catalog
([catalogs.md](../catalogs.md#the-catalog-of-an-add-on)). Its manifest must list the
resources `search` and `catalog` and have an `id`, which identifies its catalog.

| Request | Read from the answer |
|---|---|
| `GET /search?q=…` | `tracks`, `albums` and `artists`, each a list; anything else is ignored |
| `GET /album/{id}` | The album's fields and `tracks`, in the album's order |
| `GET /artist/{id}` | The artist's fields, `albums`, and `topTracks` (or `tracks`), the most popular first |

| Item | Needed | Optional |
|---|---|---|
| track | `id`, `title` (or `name`), `duration` in seconds (or `durationMs`) | `artist` and `album` (names), `isrc`, `artworkURL` (or `cover`) |
| album | `title` (or `name`); `id` in a list | `artist`, `year` or `releaseDate`, `trackCount` (or `totalTracks`), `artworkURL` (or `cover`) |
| artist | `name` (or `title`); `id` in a list | `artworkURL` (or `cover`) |

- An item without what is needed is left out. A list is read up to 100 items (500 tracks of
  an album, 500 albums of an artist), a title or name up to 500 characters.
- A track without a length is left out (a placeholder is a file of the song's length). Its
  album is then incomplete, as is one with fewer tracks than its `trackCount`: it never
  completes an album in the library.
- Track and disc numbers are not part of the protocol: tracks are numbered in the order of
  the answer, as one disc.
- An album or artist answer may leave its `id` out; one it has must be the ID asked for,
  or the answer is refused. An unknown ID is answered with HTTP 404.
- A track without an `isrc` is kept; other add-ons then find it by title, artist and
  length, which misses more often.
- `artworkURL` is fetched by Shijhon, from public addresses only, as a raster image of
  10 MiB at most. Apps are never given the address.
- IDs are used exactly as sent. A song is asked for at `/stream/{id}` with the catalog's
  track ID; an ID that is refused is then looked up as any other song.
- Catalog requests count against the API request limit, behind the song being played.
  None are sent while the add-on is passed over or paused after a 429, and a 429 on one of
  them pauses lookups too.

## The availability extension

Shijhon's own, optional. An add-on that lists the resource `availability` answers
`GET /availability?isrc=…&title=…&artist=…&durationMs=…` with
`{"available": true | false | null, "id": "<its track ID>", "preparing": false}`: whether
it can deliver this recording now (`null`: it cannot tell). The
check must be quick and change nothing. Shijhon asks only with the routings
`primary_first` and `ready_first`. With `ready_first` and `prepare_when_not_ready` on,
when no add-on has the song ready, it adds `prepare=true` to ask one that said no to
prepare the recording for next time.

## Limits

| Limit | Default | Setting in `[delivery]` |
|---|---|---|
| API requests to one add-on | 2 a second, after 4 at once following a quiet moment | `addon_requests_per_second`, `addon_request_burst` |
| Audio requests being opened at one add-on at once, besides the songs being played | 4 | `addon_audio_openings` |
| Songs one user looks up at once | 4 | `user_routings` |
| Whole-song downloads of one user at once | 2 | `user_downloads` |
| Whole-song downloads of one user in a row | 4, then one every 30 seconds (120 an hour) | `user_download_burst`, `user_downloads_per_hour` |
| Warm-ahead jobs at once, for all listeners | 2 | `warm_ahead_jobs` |
| DASH songs fetched or joined at once, for all listeners, besides the songs being played | 3 | `dash_joins_at_once` |

- An add-on's own limits (`requests_per_second`, `request_burst`, `audio_openings`): 0
  requests a second, or 0 openings, is no limit; the burst is 1 or more.
- Limits are per origin as the address is written: `localhost` and `127.0.0.1` count as
  two.
- A redirect of an API request counts as a request. A redirected audio request is one
  opening, however many redirects it follows; an opening lasts until the answer's first
  bytes.
- A request past a limit waits its turn within its own time; if no turn comes in time, it
  ends, and nothing retries it.

**A play or a download.** The song being played has turns of its own and never waits
for the limits per user; it takes from the hour's downloads without waiting, down to one
hour's worth below zero. Shijhon takes a request for the song being played when the app
reports that song as playing, or when it is a stream from the song's start and the same
app started fetching no other song in the half second before (`ahead_window_seconds`; 0:
every such stream waits its turn). So the limits per user slow down pre-loading, saving
many songs at once, probes (`HEAD`) and `download` requests for songs not reported as
playing, but not saves that come one by one as streams: those look like plays.

## What a song costs an add-on

- A play of a song found by its ISRC and not looked up before: two API requests (the lookup
  and the link; three when the ISRC lookup finds nothing and the add-on has `/resolve` or a
  search) and one request for the audio, more when the listener seeks or the app asks in
  ranges.
- With `primary_first` or `ready_first`, an add-on with the availability extension that is
  not the one asked first gets one check for every song routed. A check that names the
  track saves the lookup.
- While a song plays, the next two are routed the same way (warm-ahead: the same requests,
  and the first bytes of the audio).
- An add-on that fails or lacks a song gets the requests up to that point. With
  `primary_first`, the likely next add-on is looked up (no link) once the first one's
  lookup takes longer than half a second.
- The manifest is read once after a start or a change of the add-on list, and when someone
  looks at the dashboard's Add-ons or Diagnostics page (at most once a minute).
- Nothing is asked when albums are listed, searched or browsed, at startup or on a timer -
  except of the add-on that is the catalog: a search, an album and an artist page cost
  one request each (kept for `cache_seconds`), an artist opened from a song or an album two
  the first time, and the library pass asks at its own pace.
