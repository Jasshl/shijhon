# Add-ons

Shijhon gets audio from **add-ons**: small HTTP services that find a track and hand over
a link to its audio: a file, or a DASH stream ([below](#dash-links)). HLS links do not
play. Shijhon uses existing add-on protocols instead of a new one: the one
[Eclipse Music](https://eclipsemusic.app/docs) uses, and [BitChord](https://bitchord.kushagrasingh.in/docs)'s
close variant of it. Shijhon is an independent project, not affiliated with or endorsed by
Eclipse Music or BitChord.

## Adding an add-on

The separate [Shijhon Mornye extension bridge](https://github.com/Jasshl/shijhon-mornye-bridge)
makes Mornye extensions usable as add-ons for audio, catalog data or both. It is not
affiliated with Mornye; follow its setup guide to obtain the add-on links.

On the dashboard's **Add-ons** page: add one by its URL, order, enable, disable or remove
it, and fill in the settings its manifest declares. An enabled add-on with a catalog of
its own is also searched by [playlist imports](playlist-imports.md#add-on-catalogs).

To change the order, drag an add-on by its number or use its arrows. **All on** and
**All off** switch the whole list.

To rename an add-on, change its **Name** under **Settings** and save. Only the name you
see changes: the add-on keeps its catalog, its songs and its place, and the settings that
name it (the catalog's add-on, the primary add-on, the preferred fallback) change with it.
Names are unique. When an environment variable sets one of those settings to the add-on's
name, the rename is refused: change the variable first.

Add-ons whose manifest declares no `stream` resource provide a catalog only. They are
listed apart, under **Catalog only**: they have no place in the order, and their
**Settings** leave out what only matters for audio. They keep the name, the add-on's own
settings, the network, the address, the request limits, and **Time for catalog answers**:
how long playlist imports wait for a slow add-on's catalog (`budget_seconds` in the
file). An add-on whose manifest can't be read stays in the main list and shows the
problem.

Or in the configuration file, in your order:

```toml
[[addons]]
name = "My add-on"
base_url = "https://addon.example.invalid/your-configuration"
reach = "public"
settings = {}           # the add-on's own settings, if it has any: single values
# budget_seconds = 45   # its time for a download (and, for playlist imports, for a catalog answer), e.g. for a slow one
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

## The plan

The add-ons are asked for a song in steps, shown as a timeline on the dashboard's Add-ons
page. Add-ons in the same step start together when someone plays a song, and the first to
send audio plays. Each step has a time: when it has passed without audio, the next step's
add-ons join, and the add-ons already asked keep trying. When every add-on in the step has
failed or lacks the song, the next step joins at once. Whichever add-on's audio comes first plays,
and the others are stopped. Apps stop waiting at `[delivery] max_wait_seconds`, so steps
that would begin after that are never reached; the page lists their add-ons under the
timeline. The last step has no time of its own: its add-ons try until apps stop waiting.

Pull an add-on's bar sideways into another step's column to start with it, or onto the line
between two columns to make a step of its own, and pull a step's edge to change its time.
After its step, each bar fades on to the apps' limit, where it keeps trying. An add-on in
the list under the timeline is pulled up into a column, or up and down in that list. The
presets arrange them for you: Fastest (all in one step), Fastest three (the three quickest
right now, then the rest one at a time), Main with backups (the first alone, then the rest
together), One at a time, and Ready first. The page shows each add-on's recent speed, and
Diagnostics lists the latest starts with every add-on asked, when each later step joined,
what each answered and how long it took.

An add-on still trying when another's audio comes counts a timeout toward its cooldown
only when it was slow for itself: past its step's time and past `[delivery] slow_after`
times (2) its usual time to first audio. An add-on that has no usual time yet counts once
it is past its step's time. The last step has no time of its own: there the default step
time (`[delivery] budget_seconds`) counts as its time, from when it joined. Waits at the
add-on's own request limits are not its slowness. Give one that is often a little late but
works a higher **Slow after** of its own in its settings. When apps stop waiting with no
audio at all, every add-on still trying counts a timeout.

Requests that nobody waits on never race. An app's fetch ahead goes to the plan's first
add-on alone, and warm-ahead and downloads ask one add-on at a time in the plan's order, so
several add-ons are not asked for the same song. Each add-on then has its **Time for a
download** (`budget_seconds`, else `[delivery] budget_seconds`), not its step's time. An
app's probe of a song just before it plays races like the play.

With **Ask which add-ons have the song ready** on, the add-ons that can tell are asked when
a song starts. One that says it has the song ready joins once a song has had no audio for
the time you set (0: it goes first), and one that says it doesn't goes last.

In the file, an add-on's `step_seconds` is its step's time, `with_previous = true` puts it
in the step of the add-on before it, `budget_seconds` is its time for a download and
`slow_after` its own "slow after":

```toml
[[addons]]
name = "First add-on"
base_url = "https://first.example/manifest.json"
step_seconds = 5

[[addons]]
name = "Second add-on"
base_url = "https://second.example/manifest.json"
with_previous = true       # starts together with the first
```

An add-on that times out `cooldown_timeouts` times in a row (2), slow or still trying when
apps stop waiting, is passed over for
`cooldown_seconds`, and one that said it doesn't have a song is not asked for it again
for `miss_hours` (24).

## Limits

An add-on gets at most 5 API requests a second, after 10 at once, and 4 audio requests
being opened at once, from all users and Shijhon's background work together. Each user
can look up 4 songs and fetch 4 whole songs at a time. The optional hourly download quota
is off by default. Over a limit, a request waits its turn; the song being played goes
first. An add-on you run yourself can have higher API limits of its own
(`requests_per_second`, `request_burst`, `audio_openings`). Several add-ons served at one
address count separately, by the `id` in each one's manifest; the same add-on added twice
shares its limits ([every limit](reference/add-on-protocol.md#limits)).

### Downloads and playlist preparation

An app's standard `download` request uses the download scheduler for each missing song.
The dashboard's **Downloads** page also prepares a chosen native playlist's
audio in the server library. Neither workflow needs a playlist batch detector or a
special add-on. This prepares server files; the app still needs to save its own device
copies. Showing catalog tracks and adding library placeholders do not download audio.

The defaults on **Playback → Downloads and playlist preparation** are 4 songs fetched
at once for everyone together, and 2 per add-on. Under **Advanced settings**, new
preparation is limited to 1 song per add-on and whole-song fetches to 4 per user. All of
these limits are adjustable; 0 removes that cap. The hourly quota is optional and defaults
to 0 (off).

For each song, Shijhon selects an enabled add-on that permits downloads and has a free
slot. It keeps the slot until the transfer ends. A source that cannot report whether
audio is cached remains usable: only the selected source gets the ordinary lookup and
stream request. If it fails, another source is tried in turn. Playback has priority.

Playlist preparation also works with slow add-ons. Its songs wait for a free slot, and
the download timeout (`download_timeout_seconds`, 180 seconds) counts only from the
moment a song gets its slot. When every add-on that could provide a song is paused
(rate limited with HTTP 429 and `Retry-After`, or cooling down after errors), the song
waits for the pause to end instead of failing. Nothing is sent to a paused add-on until
then; a 429 pauses only that add-on, not the others in its shared group, which still share
its transfer slots. A song fails when no enabled add-on permits
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
removes that add-on's cap. The same add-on added twice always shares transfer limits;
add-ons served at one address do not, unless you group them. Give add-ons the same
**Shared account or worker group** label when they use one account or worker, at one
address or at different ones. A group's strictest nonzero limit applies; 0 does not
cancel another member's cap. Different hostnames do not establish independent backends.
API request pacing stays per add-on, independently of these groups.

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
  an hour at most, or 30 seconds when it names none. Songs already playing keep playing,
  and other add-ons at its address are asked as before.
- **"Not ready yet"** (HTTP 503 with `Retry-After`, while it prepares the song): Shijhon
  asks for the song's link again after that time, within the add-on's time for the song:
  in a play until apps stop waiting, for a download its time for a download. This is not
  an error. When the song is not ready in time, a retry of the song asks this add-on again.
  Give an add-on that prepares whole files a time for a download that covers a usual
  preparation, and a step whose time lets it be asked first.
- **Three errors in a row** (HTTP 5xx, no connection, a broken answer), no more than ten
  minutes apart: it is passed over for 30 seconds, and again after each further error
  until it delivers audio.

## For add-on authors

An add-on needs `/stream/{id}` and one way to find a song: by ISRC (`/resolve-isrc`), by
title, artist and length (`/resolve`), or a search (`/search?q=`). An add-on whose
manifest lists no `stream` serves only its catalog: Shijhon asks it for no audio.
[The add-on protocol](reference/add-on-protocol.md) has the rest.
