# The add-on protocol

Requests are relative to an add-on's base URL: its manifest URL without `/manifest.json`.
Where the protocol comes from, and its guides: [development.md](../development.md#the-add-on-protocol).

| Request | Used for |
|---|---|
| `GET /manifest.json` | `id`, `name`, `resources` (Shijhon uses `isrc`, `resolve`, `search`, `stream`, `availability`, `cached` and `catalog`), `settings` (their keys, defaults, types and options; a free-text setting is write-only on the dashboard unless it says `"secret": false`), `allowDownloads` ([below](#allowdownloads)) |
| `GET /resolve-isrc?isrc=…` | Finding a track by its ISRC: `{"trackId": "…"}` (or `"id"`); HTTP 404 when the add-on does not have it |
| `GET /resolve?title=…&artist=…&durationMs=…&isrc=…` | The fallback when the ISRC lookup finds nothing: the answer's `item` |
| `GET /search?q=<artist> <title>` | The fallback at an add-on without `resolve`: its `tracks` |
| `GET /stream/{id}` | The link to the audio: `{"url": "…"}`, optionally with `format`, `container`, `codec`, `quality`, `expiresAt` and `headers` |

Shijhon does not send the `quality` and `atmos` request parameters.

## Finding a song

An add-on needs `stream` and at least one way to find a song: `isrc`, `resolve` or
`search`. An add-on whose manifest lists no `stream` is used only for its catalog: plays,
availability checks, downloads and preparation never ask it for a song. Shijhon asks, in
this order:

1. `/resolve-isrc`, when the add-on declares `isrc` and the song has an ISRC.
2. If that is not asked or finds nothing: `/resolve`, when the add-on declares `resolve`.
3. At an add-on without `resolve`: `/search`, when it declares `search`. An add-on that
   declares `resolve` is never searched for a song.

A song that came from an add-on's catalog (the add-on that is the catalog, or one a
playlist import found it in) is asked of that add-on by its own track ID, without a
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
- HTTP 503 with a valid `Retry-After` (seconds or an HTTP date; any body) means "not ready
  yet": the add-on is preparing the audio. At `/stream/{id}`, Shijhon asks again after
  that time (half a second at least), as often as the add-on's time for the song allows
  (in a play the app's wait, `max_wait_seconds`; for a download its time for a download,
  `budget_seconds`, else `[delivery] budget_seconds`). When the time named is longer than
  what is left, it stops asking. This is no error: it counts toward no cooldown, and a
  retry of the song asks that add-on again. An attempt that waited until its time ran
  out counts as a timeout; one whose first answer named more time than was left moves on
  at once and counts as none. Either way it is kept among the add-on's recent attempts
  as one without audio. At the add-on's other requests such an answer is not counted as
  an error either and is not asked again. At the audio link's own address, a 503 is an
  error, with or without `Retry-After`. A 503 without a valid `Retry-After` is an error,
  unless it says the add-on needs sign-in (next).
- HTTP 503 with a JSON object whose `error` is `"needs_sign_in"` means the add-on can't
  answer until its operator signs in again (or verifies an account). It may add `"url"`,
  an `http` or `https` address of the add-on's own page where they do. For example:
  `{"error": "needs_sign_in", "url": "https://addon.example/manage"}`. A user name or
  password in the address is left out, and an address that isn't `http` or `https` is
  not shown. Shijhon reads this answer from any request: a song's lookup, its link, its
  availability check, its audio (a DASH manifest and its segments too), the reads of the
  add-on's manifest and the catalog's requests, whether or not the answer has a
  `Retry-After`. It is no error, counts toward no cooldown and is no failed start of the
  add-on. Plays and downloads pass an add-on that streams over for 10 minutes at a time
  and ask it again after them; a playlist preparation waits for it as for a rate limit.
  An add-on with a catalog only is not passed over. When a play or a download reads the
  manifest of an add-on with a catalog only and that read itself answers that it needs
  sign-in, the add-on is passed over like any other, because Shijhon can't yet tell that
  it has a catalog only. The Add-ons page, the plan, Diagnostics and a play's steps show
  "Needs sign-in", with a link to that page, until the add-on delivers audio again (one
  with a catalog only: until its catalog answers again). Its operator tells Shijhon they
  signed in with **Check again** beside it on the Add-ons page; saving its settings does
  the same, and so does turning it off and on, though that also ends playlist
  preparations that wait for it alone. Album matching does not rest on such an answer
  from the catalog's add-on. Keep that body small (16 KiB at most); a longer one is read
  as a plain 503.
- HTTP 429 is a rate limit: nothing new is asked of that add-on until its `Retry-After`
  (seconds, a fraction accepted, or an HTTP date; an hour at most; 30 seconds when it
  names none). Other add-ons at its address are asked as before. A later 429 can only make
  that longer. When the audio request answers 429, audio requests
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
streams (they look like plays), still use the add-on. Shijhon reads a changed manifest for
this only at a restart or a change of the add-on list.

## The catalog endpoints

Asked of the add-on an installation chose as its catalog
([catalogs.md](../catalogs.md#the-catalog-of-an-add-on)), and by
[playlist imports](../playlist-imports.md#add-on-catalogs) of every enabled add-on that
declares a catalog. Its manifest must list the resources `search` and `catalog` and have
an `id`, which identifies its catalog.

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
  10 MiB at most. Apps are given the address only with the full-catalog extension
  and its artwork opt-in below.
- IDs are used exactly as sent. A song is asked for at `/stream/{id}` with the catalog's
  track ID; an ID that is refused is then looked up as any other song.
- Catalog requests count against the API request limit, behind the song being played
  (an import's behind every app request). None are sent while the add-on is passed over
  or paused after a 429, and a 429 on one of them pauses lookups too.
- An import searches for `<artist> <title>` and opens the album of the track it matches;
  a track that names its album by title only has that album found by a search for the
  album's title and artist.

## The full-catalog extension

Shijhon's own, optional. An add-on listing `search`, `catalog` and `fullcatalog` in
its manifest's `resources` can supply the catalog interface's richer metadata and
lookups. Add-ons without `fullcatalog` keep the behavior above. Other clients can
ignore the additional fields and paths.

The optional manifest object `fullCatalog` declares an identity and artwork permission:

```json
"fullCatalog": {"key": "examplemusic", "region": "us", "clientArtwork": true}
```

- `key`: optional, lower-case letters and digits starting with a letter, at most 32
  characters. `addon` prefixes, `demo` and `none` are reserved. A key claims that the
  add-on's item IDs have the same meaning as every other source of that catalog key.
- `region`: optional, empty by default; lower-case letters and digits, at most 16
  characters. Saved matches and catalog lists belong to the key and region together.
- `clientArtwork`: optional boolean, false by default. Allows apps to receive image
  addresses, checked only for items actually shown: only HTTPS on public hosts, with
  no credentials in the authority. Host resolution is bounded; an unresolved, slow or
  private host leaves the field out. Checks share one DNS lookup per host and port;
  verdicts are cached for five minutes (30 seconds for unresolved or slow hosts).
  Cover IDs remain available through Shijhon's ordinary image checks and limits.

Only the configured catalog can use a declared identity, confirmed in `[catalog]`
`identity` as `key.region` (or `key` without a region). The Catalog page shows the
chosen add-on's claim beside the saved identity; selecting **Confirm this identity**
and saving confirms it. An unrelated save preserves the saved identity. Without
confirmation, the catalog keeps the add-on's key and tagged IDs, while still reading
the extension.
Confirming later changes the catalog key and starts saved matches and catalog lists
over. Before catalog requests, including cached answers, a confirmed catalog rechecks
its manifest if the last check was at least five minutes ago. An answer naming a
different item forces a recheck too. Concurrent checks share one request at background
urgency, bounded by two seconds or the catalog timeout, whichever is shorter. Failure
keeps the known manifest; age-based retries wait five minutes. A confirmed identity
that no longer matches a successfully read manifest stops catalog requests
with an `invalid` error asking for confirmation on the Catalog page. Nothing is
silently re-keyed; declaration changes between checks cannot be detected immediately.
Saved discographies, top songs and radio lists remain usable after restart while the
manifest cannot be read: an unchecked identity is not a confirmed change.
With confirmation, item IDs are used unchanged: letters, digits and dots, at most 190
characters; `.` and `..` are not path segments. Invalid IDs are left
out. Playlist imports' other add-on catalogs always keep their add-ons' keys; the
configured add-on is skipped by its key. For audio, only that configured add-on receives
an unchanged catalog item ID directly, and only while its current declaration matches
the confirmed identity and the song's catalog key. Other add-ons, unconfirmed claims
and changed declarations use ordinary recording lookup and matching. Catalog references
under an add-on's key retain their existing direct-ID behavior.

### Fields

All additional fields are optional. The existing required ID, title and positive
track duration still apply, as do the existing alternate names.

| Item | Additional fields |
|---|---|
| Track | `discNumber`, `trackNumber` (integers >= 1); `explicit`, `clean` (booleans); `artists` (`[{"id":"…","name":"…"}]`, either ID or name may be omitted); `album` (`{"id":"…","title":"…"}` or a title string); `releaseDate`; `genres` (strings); `artworkTemplate` |
| Album | `artists`; `kind` (`album`, `single`, `ep`, `compilation`, otherwise the title rule above); `explicit`, `clean`; `label`, `upc`, `genres`; `artworkTemplate`; `incomplete` (boolean, on album answers) |
| Artist | `artworkTemplate` |

A declared `single`, `ep` or `compilation` kind takes precedence over the title.
For `album`, a title ending in "- Single" or "- EP" supplies the more specific kind;
otherwise it remains an album. Missing or unrecognized kinds use the same title rule.
The resulting kind is reported in OpenSubsonic `releaseTypes` on artist releases and
album answers.

Track fields apply to search results, album tracks, top tracks and the new lookups;
album fields apply to search results, album answers and every release page. Dates are
`YYYY`, `YYYY-MM` or `YYYY-MM-DD`; album `year` remains an alternative. ISRCs are
normalized to 12 upper-case letters and digits. `durationMs` preserves milliseconds;
`duration` remains seconds. `explicit` takes precedence if both version flags are true.
Artist IDs are used when supplied; missing artist items can be found by name.
An `artists` entry with only an ID retains that reference without fetching its name.
The `artist` display credit stays unchanged; individual artist names are kept only
when supplied. Entries with neither a usable ID nor a name are ignored. Artist search
results and artist-page objects still require a name.

When every album track has both numbers, its own disc and track numbers are used.
Otherwise all tracks are numbered in answer order on disc 1, with `numbered` false.
Missing or skipped tracks, or fewer tracks than `trackCount`, mark the album incomplete.
On full-catalog album answers, an explicit `incomplete: true` or `incomplete: false`
replaces only the advertised-count comparison without changing `trackCount`.
An empty track list or any listed track Shijhon could not read still marks the album
incomplete, even with `incomplete: false`.
This lets a catalog distinguish unread tracks from source tracks unavailable in its
market. The add-on must set it true when its own bounds or parsing omit tracks that
make the result incomplete. Absent or non-boolean values keep the count/list rule;
add-ons without `fullcatalog` ignore this field.
An `artworkTemplate` is an HTTP(S) address with `{w}` and `{h}` for dimensions;
otherwise `artworkURL` is used. Templates remain usable by cover ID after size
substitution and a restart.

### Requests

| Request | Answer |
|---|---|
| `GET /search?q=…&limit=N` | The existing search object; `limit` is 1–100 per item type |
| `GET /catalog/track/{id}` | One track object, with its `album` object |
| `GET /catalog/isrc/{isrc}` | `{"tracks": […]}`, each with its album object; an empty list or HTTP 404 when none |
| `GET /artist/{id}` | The existing artist object, with optional `releasesNext` cursor |
| `GET /catalog/artist/{id}/releases?cursor=…` | `{"albums": […], "next": "cursor"}`; null or absent `next` ends the list |

Single-song and ISRC requests provide album references without a search for the album.
Confirmed catalogs can look a song up immediately after restart. A catalog under the
add-on's key uses these requests once its manifest has been read; until then, the
legacy no-request song lookup is preserved.

Cursors are opaque strings of at most 512 characters, returned unchanged in
the URL-encoded query. Shijhon reads at most 50 pages including the artist answer,
and 5,000 releases, keeping the first occurrence of each album ID. An invalid cursor
ends the release list, treated as its end just like the paging bounds. Invalid or
repeated cursors and bounds log a short notice without cursor text.
One artist's pages share the catalog timeout, including the first page and pacing:
this keeps a large discography from multiplying the request's wait by its page count.
A deadline or later page failure returns the releases already read and logs a notice;
that partial list is cached in memory for at most 60 seconds and is never saved as a
complete discography. Successfully fetched pages stay cached for the ordinary catalog
cache duration, so a retry resumes without fetching them again. Every page takes an
add-on API turn.
Each full-catalog list is limited to 100 items, except the first artist page's releases
(500) and album track lists (500); answers remain at most 1 MiB and text fields 500
characters.

Every request uses the add-on's settings, network policy and API request limit,
behind the song being played. HTTP 404 is not found, 401/403 unauthorized, 429 pauses
requests using `Retry-After`, and 5xx is unavailable (a 503 with `Retry-After` too, but
it is not counted as an add-on error). Failures contain no addresses or credentials.

## The availability extension

Shijhon's own, optional. An add-on that lists the resource `availability` answers
`GET /availability?isrc=…&title=…&artist=…&durationMs=…` with
`{"available": true | false | null, "id": "<its track ID>", "preparing": false}`: whether
it can deliver this recording now (`null`: it cannot tell). The
check must be quick and change nothing. Shijhon asks only with `[delivery] ready_checks`
on, for a song someone plays. With `prepare_when_not_ready` also on, when no add-on has
the song ready, it adds `prepare=true` to ask one that said no to prepare the recording
for next time.

This extension reports ordinary readiness. It does not bind a cached quality or forbid
preparation when `/stream/{id}` is later requested. A ready ID can be looked up again if
it expires; it is not used as a cached-delivery guarantee.

## The cached delivery extension

Optional and separate from ordinary readiness. Standard add-ons need not implement it:
download scheduling works with their normal lookup and stream endpoints.

An add-on declaring resource `cached` answers
`GET /cached?isrc=…&title=…&artist=…&durationMs=…` with
`{"available": true | false | null, "selector": "<opaque cached-variant selector>"}`.
An optional `version=clean|explicit` supplies the catalog's version flag when known.
The check must be quick and have no side effects: it must not start a download or
preparation. `true` means a complete cached variant of the correct recording is
retrievable now; `false` means none is available; `null` means the add-on cannot tell.
Any cached quality is acceptable. The selector binds the actual variant, rather than
an ordinary recording ID or a requested quality.
Shijhon omits `quality` and `atmos` settings on these two cached endpoints so preferred
settings cannot replace that selection.

Shijhon asks `GET /cached-stream/{selector}` for its usual stream descriptor (`url`,
optional format, codec, expiry and headers). This request and the resulting audio link
must serve that cached variant or fail, without preparing audio or choosing a different
uncached quality. A missing or expired selector returns HTTP 404 or 410. Shijhon never
replaces it through ordinary lookup within the cached path. It can check again or return
to the separately limited ordinary acquisition path.

The add-on handles backend cache calls and credentials. Shijhon uses only these generic
endpoints and still applies download permission, network policy, recording and audio
checks. A positive answer without a selector is unknown. The selector is treated as an
opaque path segment and is not shown in logs or on the dashboard.
Selectors must be nonempty, at most 512 characters, and neither `.` nor `..`.

## The plan in detail

The rules behind [the plan](../add-ons.md#the-plan) on the dashboard's Add-ons page.

An add-on still trying when another's audio comes counts a timeout toward its cooldown
only when it was slow for itself: past its step's time and past `[delivery] slow_after`
times (2) its usual time to first audio. An add-on that has no usual time yet counts once
it is past its step's time. The last step has no time of its own: there the default step
time (`[delivery] budget_seconds`) counts as its time, from when it joined. Waits at the
add-on's own request limits are not its slowness. Give one that is often a little late but
works a higher **Slow after** of its own in its settings. When apps stop waiting with no
audio at all, every add-on still trying counts a timeout.

An add-on that times out `cooldown_timeouts` times in a row (2), slow or still trying when
apps stop waiting, is passed over for `cooldown_seconds`, and one that said it doesn't
have a song is not asked for it again for `miss_hours` (24).

A play's audio is held while it goes on: Shijhon reads the add-on's answer on to the
song's end while the app stays connected - also a song the app only loaded ahead - and
feeds the app from it. The play's seeks, and an app reading on after a pause, are served
from it at once, without asking the add-on again, also once the song's link expired and
the add-on gives no new one. Only the play's own file is used: the same add-on track, size
and ETag (without an ETag, the same link or the same first bytes); what is not held yet goes
to the add-on as before, and a new link to a file the add-on gives no ETag for is accepted
only when its first bytes are the play's. An add-on that asks not to be used for downloads
(`allowDownloads`) is read only as the app reads. With **Keep streamed songs** on, a song
held in full goes into your library; a play's start and its seeks count together when they
are of the same file at the add-on.

Requests that nobody waits on never race. An app's fetch ahead goes to the plan's first
add-on alone, and warm-ahead and downloads ask one add-on at a time in the plan's order, so
several add-ons are not asked for the same song. Each add-on then has its **Time for a
download** (`budget_seconds`, else `[delivery] budget_seconds`), not its step's time. An
app's probe of a song just before it plays races like the play. When an app loads two songs
at the same moment, as a player with two audio elements loads the song it plays and the
next one, Shijhon cannot tell which one plays until the app reports it, so both race like
plays.

In Shijhon's log, each request's line gives its whole time - for a song, as long as the
app kept reading - and when Shijhon sent the answer's start and its first body bytes
(`start sent`, `first body sent`), and when it saw the app disconnect (`disconnect seen`).
"Sent" means handed to the web server: an app that already closed the connection does not
get it, and Shijhon notices a disconnect only when it looks, so an app can be gone before
the line says so.

## Limits

| Limit | Default | Setting in `[delivery]` |
|---|---|---|
| API requests to one add-on | 5 a second, after 10 at once following a quiet moment | `addon_requests_per_second`, `addon_request_burst` |
| Audio requests being opened at one add-on at once, besides the songs being played | 4 | `addon_audio_openings` |
| Songs one user looks up at once | 4 | `user_routings` |
| Scheduled whole-song fetches for everyone at once | 4 | `bulk_downloads` |
| Whole-song fetches of one add-on or shared group at once | 2 | `addon_downloads` |
| New preparations of one add-on or shared group at once | 1 | `addon_preparations` |
| Whole-song downloads of one user at once | 4 | `user_downloads` |
| Optional hourly download quota | Off (0); burst 4 when a quota is set | `user_downloads_per_hour`, `user_download_burst` |
| Warm-ahead jobs at once, for all listeners | 2 | `warm_ahead_jobs` |
| DASH songs fetched or joined at once, for all listeners, besides the songs being played | 3 | `dash_joins_at_once` |

- An add-on's own limits (`requests_per_second`, `request_burst`, `audio_openings`): 0
  requests a second, or 0 openings, is no limit; the burst is 1 or more.
- Transfer and preparation limits are installation settings, not limits discovered in a
  standard manifest. Add-on overrides `downloads` and `preparations` inherit when omitted;
  0 removes that cap. Slots cover the full transfer, including a DASH background fetch.
- One add-on is its origin (scheme, host and port) and the `id` in its manifest. Add-ons
  served at one origin count separately: each has its own requests a second, audio
  openings, rate-limit pause and transfer capacity. One add-on configured twice shares
  them, with the stricter of its entries' settings. A manifest request made before the
  add-on's `id` is known (the first read of its manifest since Shijhon started, a
  dashboard check of a new address) waits its turn at the origin as a whole, under the
  strictest settings of the add-ons there, and once the `id` is known counts at the add-on
  too. Redirect hops to another origin count at that origin as a whole. An add-on whose manifest names no `id`
  counts at its origin as a whole.
- Add-ons with the same nonempty `capacity_group` share transfer capacity; connected
  groups use the strictest nonzero limit. Different origins do not prove independent
  accounts or workers. API pacing and rate-limit pauses stay per add-on and do not follow
  an account group.
- A source without the cached extension is unknown, not rejected. The scheduler chooses
  one source before its normal stream request, so it does not start preparation at every
  source while checking availability. Failures can move to another source in turn.
- Limits are per origin as the address is written: `localhost` and `127.0.0.1` count as
  two.
- A redirect of an API request counts as a request. A redirected audio request is one
  opening, however many redirects it follows; an opening lasts until the answer's first
  bytes.
- A request past a limit waits its turn within its own time; if no turn comes in time, it
  ends, and nothing retries it. Playlist preparation is the exception: its songs wait for
  a transfer slot, and for add-ons paused by a rate limit or a cooldown (an hour in all
  at most), and then ask again. The download timeout covers each transfer from its slot.

**A play or a download.** The song being played has turns of its own and never waits
for the limits per user; it takes from the hour's downloads without waiting, down to one
hour's worth below zero. Shijhon takes a request for the song being played when the app
reports that song as playing, or when it is a stream from the song's start and the same
app started fetching no other song in the half second before (`ahead_window_seconds`; 0:
every such stream waits its turn) - or only one, asked for together with it, as a player
with two audio elements loads the song it plays and the next one at the same moment: then
both are taken as the song being played, unless that app's own report names the other. So the limits per user slow down pre-loading, saving
many songs at once, probes (`HEAD`) and `download` requests for songs not reported as
playing, but not saves that come one by one as streams: those look like plays.

## Downloads and playlist preparation

The rules behind [downloads and playlist
preparation](../add-ons.md#downloads-and-playlist-preparation). Neither an app's
`download` request nor the Downloads page's playlist preparation needs a playlist batch
detector or a special add-on.

Playlist preparation also works with slow add-ons. Its songs wait for a free slot, and
the download timeout (`download_timeout_seconds`, 180 seconds) counts only from the
moment a song gets its slot. When every add-on that could provide a song is paused
(rate limited with HTTP 429 and `Retry-After`, cooling down after errors, or needing its
operator to sign in again), the song waits for the pause to end instead of failing.
Nothing is sent to a paused add-on until then; a 429 pauses only that add-on, not the
others in its shared group, which still share its transfer slots. A song fails when no
enabled add-on permits downloads or has it, or after waiting an hour in all for paused
add-ons. A waiting song holds none of the user's download turns. When it gets a slot while
the user's turns are busy, or while an app is fetching the same song, it gives the slot
back and tries again shortly, so app requests go first. If an app puts the song in place
meanwhile, the song counts as ready. A song your app reports as playing goes ahead of
other waiting work. Cancel also stops waiting songs. An app's `download` request does not
wait like this: it fails at once when its add-ons are paused, and its timeout includes its
wait for a slot.

The preparation limit bounds the requests Shijhon starts. An add-on can keep a remote
job running after a request ends; the standard protocol cannot cancel or confirm that
job. When readiness remains uncertain, Shijhon briefly pauses new preparation on that
backend rather than assuming the remote work stopped.

The optional [cached delivery check](#the-cached-delivery-extension) can select the exact
cached variant of the correct recording, at any audio quality. Shijhon keeps that choice
and uses local conversion when needed. A general readiness answer or quality label does
not prove that a file is cached.

An offline save sent as an ordinary stream still uses playback rules because that
request does not reveal offline intent.

## What a song costs an add-on

- A play of a song found by its ISRC and not looked up before: two API requests (the lookup
  and the link; three when the ISRC lookup finds nothing and the add-on has `/resolve` or a
  search) and one request for the audio, more when the listener seeks or the app asks in
  ranges.
- With `ready_checks`, an add-on with the availability extension that is not in the
  plan's first step gets one check for every song played. A check that names the track
  saves the lookup.
- An add-on in a step with others is asked for every song that reaches that step, also
  when another one's audio comes first; its request is then stopped.
- While a song plays, the next two are routed the same way (warm-ahead: the same requests,
  and the first bytes of the audio).
- An add-on that fails or lacks a song gets the requests up to that point.
- The manifest is read once after a start or a change of the add-on list, and when someone
  looks at the dashboard's Add-ons or Diagnostics page (at most once a minute).
  Catalog's **Check now** also reads it again. A confirmed full-catalog add-on additionally
  rechecks before catalog requests when its last check is five minutes old, and after
  an answer naming a different item, as described above. There is no manifest polling timer.
- Nothing is asked when albums are listed, searched or browsed, at startup or on a timer -
  except of the add-on that is the catalog: a search, an album and an artist page cost
  one request each before any full-catalog release paging (kept for `cache_seconds`),
  an artist opened from a song or an album two
  the first time, and the library pass asks at its own pace. Playlist imports ask the
  catalogs of add-ons only for entries they match.
