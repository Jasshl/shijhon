# Playlist request API

The client appends `/shijhon/api/playlists` to `SHIJHON_URL` (an optional deployment
path prefix is retained). Authentication is `Authorization: Bearer` using
`SHIJHON_PLAYLIST_TOKEN` from the environment, never URL credentials or query
parameters. Obtain an access token through Shijhon's OAuth authorization-code flow
with S256 PKCE. The preferred interface
is the Streamable HTTP MCP endpoint `/shijhon/mcp`, which discovers authentication
and exposes typed tools. See [agent setup](../../../docs/agent-access.md).

Default scopes are `create,status`. History and library each need a separate
optional permission **and** an explicit request boolean; reference playlists and songs
need only the library permission. Status and retry belong
to the same durable OAuth connection across access-token refreshes. Dashboard
cookies never authenticate this API. The optional HTTP client accepts an OAuth
access token in `SHIJHON_PLAYLIST_TOKEN`; the calling application must manage its
OAuth login and refresh. The MCP interface handles this through its client.

## Creation

`POST /requests`, JSON object, required header `Idempotency-Key` matching
`[A-Za-z0-9_.:-]{8,128}`. Unknown fields are forbidden.

| Field | Default | Accepted input |
| --- | --- | --- |
| `name` | `"Described playlist"` | Printable nonblank text, at most 100 characters; `""` chooses automatically in prompt mode |
| `prompt` | `""` | Text, at most 2000 characters; retain user constraints (with `tracks`: saved as the playlist's brief) |
| `count` | `30` | Integer 1..100 |
| `max_per_artist` | `2` | Integer 1..10 |
| `use_history` | `false` | Boolean; explicit user opt-in and token history grant |
| `use_library` | `false` | Boolean; explicit user opt-in and token library grant |
| `tracks` | `[]` | Ordered artist/title objects; optional album/version text |
| `save_instructions` | `false` | Boolean; retain instructions on the server |
| `cadence` | `"manual"` | `"manual"`, `"daily"` or `"weekly"` |
| `preview` | `false` | Boolean; wait for review before publication (dashboard, or MCP `review_playlist_preview`) |
| `reference_playlists` | `[]` | Up to 3 distinct Navidrome playlist IDs the account can read; prompt mode; token library grant |
| `reference_songs` | `[]` | Up to 50 distinct Navidrome song IDs the account can read; prompt mode; token library grant |
| `exclude_reference` | `true` | Boolean; leave the chosen references' songs out of the result |
| `require_all` | `false` | Boolean; explicit tracks only: publish nothing unless every track resolves |

Require a nonblank prompt or a nonempty `tracks` list whose length equals `count`.
Each track requires nonblank `artist` and `title`, at most 300 characters each;
`album` defaults to `""` and permits 300, `version` defaults to `""` and permits 100.
No IDs, URLs, model configuration or actions are accepted as track fields.
Explicit tracks must respect `max_per_artist` (artist names are normalized as during
generation); an over-limit list is rejected before a request is created.
Explicit tracks preserve order, require manual cadence and both context flags
false, cannot save refresh instructions, and make no model call even if an original
prompt is retained. `version` is a literal recording qualifier, such as `Live` or
`Radio Edit`; leave it empty for an unqualified recording title. Each explicit
track becomes the library's recording, else the catalog's (a library placeholder on
publication). When several releases match, the original is chosen: not a clean edit,
no remaster or deluxe marker, the earliest year. The credit may differ in featured artists
and in "and" for "&" ("A and B feat. C" matches "A & B"). The tracks that resolve are published in
their order and the others are left out and reported in status; with `require_all`
nothing is published unless every track resolves. A request whose tracks all fail is
not published. Prompt mode
uses only saved server AI. A nonmanual cadence also requires a prompt, saved
instructions and usable saved server AI; the server enforces AI availability.

Supply an evocative name for agent-curated playlists; explicit tracks require a
nonblank name and never call a model. Prompt requests can send `"name":""` to choose
a short title in the final ordering call, with no extra model call. The title is
saved together with the selection before preview or publication. Retries, restarts
and refreshes keep that first title. Supplied names are preserved exactly, including
the literal `"Described playlist"`. Omitting `name` uses that default;
it does not opt into automatic naming. Original input and replay hashes never change.

References relate a prompt to music the user already has: pass playlist IDs from
`get_native_playlists` or song IDs from `search_library` instead of pasting their songs
into `prompt`. IDs are 1..100 characters of letters, digits, `_`, `.`, `:` and `-`,
without repeats; explicit tracks cannot use references. Choosing references needs the
token's `library` grant but no `use_library` boolean. Without explicit references, the
server takes up to three of the user's own playlists whose names the prompt mentions
(whole words, ignoring case; a one-word name only in quotes or next to "playlist" or
"mix"), but only if the submitting token includes `library` and the current connection
still grants it, checked again at every generation and scheduled refresh. The request
keeps the submitting token's scope ceiling; later consent cannot broaden that ceiling.
Without it, no playlist is listed or read and status shows no reference; losing it while
a playlist found by name is in use stops that generation. References are read at every
generation, including refreshes; one that cannot be read fails the run with its ID,
while a playlist found by name is skipped. `exclude_reference` removes the songs of the
references the caller chose; songs of a playlist found by name are not removed, and the
model is told to avoid repeating them unless the prompt asks for them. Reference fields
left at their defaults are not part of the replay hash; the HTTP client sends them only
when supplied.

Minimal one-time prompt input file:

```json
{"name":"","prompt":"A quiet instrumental evening mix, with no live recordings"}
```

Explicit selection input file:

```json
{"name":"Evening pair","count":2,"max_per_artist":1,"tracks":[{"artist":"Quill & Marrow","title":"Night Light","album":"Paper Sky"},{"artist":"Glass Orchard","title":"First Lantern"}]}
```

The client validates locally and supplies defaults deterministically, without
stripping or rewriting strings. Keep the file unchanged during recovery. First
acceptance returns `202`; an identical replay returns `200` with the same request
status. Changed input for the same owner/key returns `409`.

## Context, status and retry

- `POST /context` with `{"use_history":false,"use_library":false,"limit":30}`.
  Booleans are independent; limit is an integer 1..100. Only opted-in, granted
  sources are returned, as bounded metadata. Context imports have a server deadline
  of at most 12 seconds; a timeout fails without returning partial context. Key and
  native authorization are checked between sources and history pages, including
  after request pacing waits. Imports leave status and revocation available. The CLI requires `--history` and/or
  `--library` to request those sources.
- `GET /requests/{id}` returns the token's own status, including `id`, `state`,
  safe `error`, `playlist_id`, `name`, `count`, and ordered `selected` songs when
  available. Each has `artist`, `title`, `album`, the model's `reason`, `fit`
  (`"meets"`, `"partly"`, `"misses"` or `""`), `source` (`"own"` files, `"added"` to the
  library by Shijhon, `"catalog"`, or `""`) and `facts`, the hints the model was shown:
  optional `year`, `genres`, `tags`, `sound` (`tempo`, `key`, `energy`, `features`,
  `labels`; `energy` and `features` are percentiles within the library) and `suggested_by`
  (`"sound_similar"` or `"sound_search"`). Described requests also report `goal` (one
  sentence), `references` (`playlists: [{"id","name","songs","detected"}]`, explicit
  `songs` count, `shown` count, `exclude`), `reference_profile` (`songs`, `with_sound`,
  and where known `energy`, `tempo`, `features`, `tags`, `genres`, `years`),
  `revision` (`"none"`, `"revised"` or `"kept"`), `coverage` (`songs`, `with_sound`,
  `with_tags`, and `sound`/`tags` each `"used"`, `"off"` or `"unavailable"`),
  `sound_pool` (`similar`, `search`: library songs added by sound analysis) and
  `left_out` (candidates left out as reference songs). Facts are hints: report songs
  without them rather than treating missing facts as a mismatch. A refresh also reports
  `edits`, what it learned from the user's edits of the published playlist: `learned`
  (`removed` and `added` songs since the previous publication, each `id`, `artist`,
  `title`, `album`), `remembered` (`removed`, `added` counts) and `left_out` (suggestions
  left out as songs the user removed); `{}` before the first refresh. It records that
  run: after `manage_playlist_refresh` with `action: "forget_edits"` (MCP only, on the
  user's request; it returns `forgotten`, the number of songs cleared) later refreshes
  start without them, and the earlier run's `edits` stays as it was.
  Explicit-track requests report `resolution` (`requested`, `library`, `catalog`,
  `unresolved` counts) and `tracks`, one entry per submitted track in order:
  `position` (1-based), `input` (`artist`, `title`, `album`, `version`), `outcome`
  (`"library"`, `"catalog"` or `"unresolved"`) and, when resolved, `song` (`artist`,
  `title`, `album`). Without the library permission, `outcome` and the count say only
  `"resolved"` and candidates come from the catalog alone. An unresolved track has
  `reason`, a fixed `detail` sentence and, when something near was found, up to five
  `candidates` (`artist`, `title`, `album`). Reasons: `not_found`; `album_mismatch`
  (only on other albums); `version_mismatch` (only other versions of the title, such as
  `Song (Live)` or `Song - Main Title`); `artist_mismatch` (the title with a different
  credit that shares a name); `title_mismatch` (a similar title by the artist, such as
  one without a leading "The"); `unconfirmed` (listed, but its release could not be
  confirmed); `catalog_unavailable` (the catalog did not answer, also after a pause; add
  it again later); `not_checked` (the run's time ran out first); `duplicate` (the same
  recording as the track at `same_as`); `library_refused` (found, but the library would
  not add it to its album, such as an owned album kept as it is; `detail` says what to
  review in Library; without the library permission it reads `not_added`). Status after a run that published nothing still
  has `tracks`.
- `POST /requests/{id}/tracks` (MCP: `add_playlist_tracks`) with
  `{"tracks":[...]}` and a new `Idempotency-Key` adds 1..100 tracks to an explicit-track
  request that is published, or whose selection failed. A track with `replaces` (the
  `position` of a track status reports `unresolved`) takes its place; tracks without it
  go at the end. The request's list then has at most 100 tracks within `max_per_artist`.
  A new run of the same request keeps the published tracks as they are, looks up the new
  ones and the still unresolved ones again, and republishes the same native playlist;
  status reports it like the first. It is accepted after a publication, a failed
  selection or a discarded preview, and refused (`409`) while a run is active, when the
  playlist was edited in Navidrome after publication (Shijhon keeps those edits), and
  after 10 additions. Replaying the key with the same tracks returns
  `200` and adds nothing; other tracks under it return `409`.
- `POST /requests/{id}/retry` with `{}` retries that same request. Frozen selections
  are reused; selection may regenerate if not frozen. It does not create a new
  request. The client never retries automatically.

`published` means verified native publication; `preview` awaits review (the dashboard,
or MCP `review_playlist_preview`). `failed` and `uncertain` need inspection/recovery. Other
states can indicate work in progress. Status polling makes 1..30 GET attempts
(`--attempts`, default 1), separated by 1..30 seconds (`--interval`, default 2),
and stops on terminal or unrecognized states. Each HTTP operation has a 15-second
socket timeout; JSON responses are limited to 1 MiB. No redirects are followed,
including same-origin redirects. The client ignores environment proxy settings.

Errors: `400` bad input, `401` invalid token, `403` forbidden scope/context, `409`
changed input under an existing key, `413` oversized body, `415` unsupported media
type, `424` missing required setup, and `429` too many active requests. These errors
do not create a new request. `503` means a temporary upstream/server failure; keep
the same input and key for recovery. A change that waited about 10 seconds behind
another playlist's publication answers `503` with `Retry-After: 30` (over MCP,
`temporarily_unavailable` saying nothing was changed): send the same call again. Status
reads do not wait for a publication. A Navidrome outage does not revoke OAuth
credentials or require new consent. The client prints fixed error messages rather
than HTTP response bodies or exception details; it redacts the bearer token from
decoded successful responses too. It exits 1 on client/HTTP failures, 2 on invalid
CLI arguments, and 0 when a JSON status or context was read successfully. Exit 0
alone does not mean publication succeeded. A missing request ID or an uncertain
result never warrants creation under a fresh key. Over HTTP, preview publication
requires the authenticated dashboard; no approval or external scheduling API is provided.

## Managing playlists (MCP)

These tools act on any of the user's Shijhon playlists in this library, whoever made it
(the dashboard, this agent or another one); never on another account's. They take the
playlist's `request_id`. Changes need `create` permission. A replacement, edit,
addition, retry, one-off refresh or preview publication runs with the calling agent's
own grant: revoking it stops that work before it runs, and the owner can still retry or
publish it on the dashboard under their own authority. Scheduled refreshes keep the
authority the playlist was made with.

- `list_playlist_requests` (`limit` 1..100, `offset`, `kind`: `all`, `agent_curated`,
  `described`, `discovery`, `import`) lists `id`, `kind`, `name`, `state`,
  `playlist_id`, `cadence`, `refreshes` and `made_by` (`this_agent`, `another_agent`,
  `dashboard`, or `account` for discovery mixes and imports), newest first.
- `get_playlist_request` adds `kind`, `made_by`, `brief` and `cadence`. `selected` is
  the latest run's songs; `published_songs` the last published ones, each with its
  `song_id`, also while a later preview waits or after a later change failed.
  `user_edits` lists up to 100 songs the user removed from the playlist and up to 100
  they added, each `song_id`, `artist`, `title`, `album`, with `remembered` counts. On a
  playlist this agent did not make, each run's songs (`selected`, `published_songs`)
  need `library` permission, plus `history` when listening history chose them (a
  discovery mix, or a description using history). The one exception is a run whose
  songs this agent chose itself: its own replacement. Additions, edits, retries,
  refreshes and preview publications keep or bring songs others chose, so they show
  none. Without the permissions, hidden songs leave `selected` empty, leave out
  `published_songs`, and `songs_hidden` says what is missing. Without library permission, outcomes read `resolved` and there is no
  `source`, references or edits. An import shows its counts (`entries`, `matched`,
  `missing`, `removed`), `review_key` and recheck state.
- `replace_playlist_tracks` (`tracks` 1..100, optional `max_per_artist`, `require_all`,
  `preview`, `brief`, a new `idempotency_key`) replaces every song of a playlist that is
  not an import, keeping its Navidrome ID. Tracks resolve as explicit tracks; a song the
  user removed is left out with reason `removed_by_user`. Edits the user made in an app
  since the last publication are learned first. A described playlist or discovery mix
  keeps its instructions, rules and schedule; only an agent-curated playlist takes
  `brief`, and its tracks and brief change only when the replacement is published.
  Refused (`409`) while a run is unfinished or a preview waits. Replaying the key with
  the same input changes nothing (`replaced: false`); other input conflicts.
- `edit_playlist_tracks` (`remove`: song IDs, each taking out one copy of that song,
  and/or `order`: every remaining song in its new place, as often as it stays; a new
  `idempotency_key`) edits a published playlist as Navidrome has it now. A song the user
  put in the playlist more than once may stay so; an edit adds no copy. On a playlist this
  agent did not make, an edit needs the permissions that read its songs. Songs no longer
  in the playlist are remembered as the user's removals. The edit is not applied if the
  playlist changes again before it runs.
- `review_playlist_preview` (`run_id`, `action`: `publish` or `discard`) acts on the
  current preview only. Publishing needs `history` for a discovery mix or a description
  using history, and `library` for one using the library. For a discovery mix,
  `enable_refreshes` true or false turns refreshes on or off; left out, it keeps what was
  asked when the mix was made.
- `manage_playlist_refresh` actions `pause`, `resume`, `refresh`, `preview` (refresh
  for review) and `forget_edits` apply to described playlists (server AI) and discovery
  mixes (history permission, Last.fm). An agent-curated playlist takes only `pause` and
  `forget_edits`. `forget_edits` with `song_ids` forgets only those songs' edits, for
  example to let one removed song back.
- `create_discovery_mix` (`mix`: `name`, `count` 1..100, `unfamiliar_percent`,
  `max_per_artist`, `exclude_days`, `repeat_days`, `cadence`, `instructions` (server AI),
  `preview`, `enable_refreshes`; an `idempotency_key`) and `update_discovery_mix`
  (`request_id`, `mix`) need `create` and `history` permission and a connected Last.fm
  profile.
- `import_playlist` (`name`, `idempotency_key`, and either `lines`, up to 100,000
  characters, or `entries` of `artist`, `title`, `album`, `version`, `isrc`, `song_id`)
  needs `create` and `library` permission and uses the dashboard's import matching.
  `get_playlist_import` (`offset`, `limit` 1..100, `only_missing`) lists entries with
  `state`, `note`, `song` and up to 10 `choices`. `review_playlist_import` (`run_id`,
  `review_key`, `action`: `choose` with `entry` and `choice`, `publish` with
  `missing_count`, `discard`, `retry`, `recheck`, `stop`, `resume`, `keep`) acts as the
  import page does.

## Song facts and sound analysis (MCP)

Read tools for the connected account, with `library` permission. They need no AI,
history permission or request opt-in. Song IDs are the Navidrome IDs that
`search_library` and `get_native_playlists` return. Every returned song has `id`,
`title`, `artist` and `album`, so it can be submitted to `create_playlist` as an
artist/title track. Results never include play counts, ratings or favorites.

Song entries also show `year`, `genres` and, when the file has them, `bpm` and `moods`;
unknown values are left out. When the user's playlist connection has a Last.fm API key,
entries also show up to five community `tags`: lists (`search_library`,
`get_native_playlists`) show only the tags Shijhon already knows and ask Last.fm for
nothing, while the tools below look them up for their songs within about five seconds and
40 requests a call, so a long result may be only partly tagged and a repeated call adds
more. Results that may carry tags report `tags`: `ok`, `partial` (time or the request
limit ran out), `unavailable`, `not_configured` (no Last.fm key; lists leave the field
out) or `cached_only` (lists). A missing or slow Last.fm never fails a tool. With `include_sound: true` (on `search_library` and
`get_native_playlists`, always on the tools below) a song that sound analysis has
carries `sound`: `tempo`, `key`, `energy`, `features` and `labels`. `energy` and
`features` (`danceable`, `aggressive`, `happy`, `party`, `relaxed`, `sad`) are
percentiles from 0 to 100 among the analyzed songs of this library, so they compare
songs with each other. They are approximate hints; many songs have none. Results that
may carry sound facts also report `sound_analysis`: `ok`, `not_configured` or
`unavailable`; the song lists still work without it. Sound analysis is the optional
AudioMuse-AI connection described in [sound analysis](../../../docs/sound-analysis.md).

| Tool | Input | Result |
| --- | --- | --- |
| `get_song_facts` | `song_ids` (1 to 100) | `songs`, `unreadable_song_ids`, `without_sound_facts` (IDs), `sound_analysis`, `tags` |
| `get_playlist_profile` | `playlist_id`, `limit=30` (1 to 100) | `playlist`, `summary`, `songs` (the first `limit`), `sound_analysis`, `tags` |
| `find_similar_songs` | `seed_song_ids` (up to 20) and/or `playlist_id`, `avoid_song_ids` (up to 20), `count=30` (1 to 100) | `songs`, `seeds`, `unreadable_seed_ids`, `unreadable_song_ids`, `sound_analysis`, `tags` |
| `search_by_sound` | `text` (up to 300 characters, English), `more` and `less` (up to 10 phrases each, up to 100 characters), `count=30` (1 to 100) | `songs`, `unreadable_song_ids`, `sound_analysis`, `tags` |
| `find_sound_path` | `start_song_id`, `end_song_id`, `steps=12` (2 to 30) | `songs` in order, both ends included; `unreadable_song_ids`, `sound_analysis`, `tags` |

`get_playlist_profile` reads an owned native playlist. Its `summary` covers up to the
first 500 songs and gives `songs` (profiled), `playlist_songs`, `year`
and `bpm` (`songs` with a value, `min`, `median`, `max`), `genres` (most common first,
with a song count), `tags` and `with_tags` (the Last.fm tags known for the profiled
songs: those of the first `limit` songs are looked up, the others only if already known),
`with_sound_facts` and, when some songs have facts, `sound`: spreads of `energy`, `tempo`
and each feature, and the most common `labels`.
`find_similar_songs` takes the seeds, plus up to 30 songs spread over the playlist, and
leaves the seeds, avoided songs and the playlist's own songs out of the answer.
Playlists of another account are never readable here.

`find_similar_songs`, `search_by_sound` and `find_sound_path` answer `setup_required`
(`missing_configuration: ["sound_analysis"]`) when sound analysis is not connected and
`temporarily_unavailable` (`feature: "sound_analysis"`) when it is away or slow.
`get_capabilities` reports the same in `features.sound_analysis` (`configured`,
`reachable`, `analyzed_songs`, `tools`, `setup_url`: the Sound analysis page, which a Navidrome
administrator uses to connect AudioMuse-AI); `features.song_facts` lists the tools that
work without it and whether Last.fm `tags` are available. Songs the account cannot read are listed as unreadable, not described.

## Server audio preparation (MCP)

Preparation is a separate explicit action after native playlist publication. The
tools use OAuth `downloads` permission for writes and `status` for progress reads.
Existing grants do not gain `downloads`; request new browser consent when needed.
No AI, Last.fm, history permission or library-context opt-in is required. Native
playlist discovery still uses `get_native_playlists` with `library` permission.
These tools are MCP-only; the optional playlist HTTP client does not submit jobs.

| Tool | Input | Result |
| --- | --- | --- |
| `prepare_playlist_audio` | `playlist_id`, `idempotency_key` | `batch` with durable ID and initial progress |
| `get_audio_preparation` | `batch_id`, `offset=0`, `limit=30` | `batch` with counts and a page of song outcomes |
| `list_audio_preparations` | `offset=0`, `limit=30` | `batches` belonging to this OAuth connection |
| `manage_audio_preparation` | `batch_id`, `action` | Read-back `batch` after `retry`, `resume` or `cancel` |

Native IDs must be printable, nonempty strings of at most 100 characters and
cannot be catalog IDs. Keys match `[A-Za-z0-9_.:-]{8,128}`. The first submission
freezes the native entries of a playlist owned by the connected immutable Navidrome
user, up to the installation's playlist import limit (`limits.audio_preparation_entries`
in `get_capabilities`, 50,000 by default). Duplicate entries share acquisition while
preserving entry totals. Jobs are asynchronous; each MCP call has a 20-second deadline.
At most 100 retained batches and 1000 agent submission keys per account are accepted.
Read pages contain 1..100 rows; a batch's song offset goes up to that entry limit, and a
listing's offset up to 1000.

Keep the same key and exact playlist ID after an uncertain response. Identical
replay returns the same batch even if the native playlist was edited or removed.
A changed ID under that connection/key returns `conflict`. A removed finished batch
leaves a tombstone: replay fails instead of creating another job. Token refresh
and new consent for the same active OAuth client/account preserve durable ownership.
A separate or revoked-and-recreated connection cannot access the former jobs.

Batch fields include `id`, `playlist_id`, `title`, `state`, safe `error`, `created`,
`total`, `unique_songs`, `available`, `failed`, `location: "server"`, `items`,
`offset` and `has_more`. Items include native `song_id`, snapshot `position`,
duplicate `entries`, title/artist, state and safe error. No credentials, audio
URLs or filesystem paths are returned. Listing returns batch summaries only.

`queued` and `running` are in progress; song stages include `waiting`, `preparing`,
`fetching` and `verifying`. `complete` confirms every snapshot entry's audio was
verified available on the server. `finished` can include failed songs; `paused`
requires explicit recovery, and `cancelling` may still have active work before
`cancelled`. Poll at most 30 times at intervals of at least two seconds, then
report the pending batch ID. Completion does not establish device copies.

`retry` requeues failed songs; `resume` requeues paused songs. Both use the frozen
snapshot, recheck native ownership and library access, and require current
`downloads` permission. Restart pauses unfinished work for explicit resume.
Removing permission or revoking the connection stops unfinished work at ordinary
service preflights, including after awaits and before verified placement. Restore
permission on the same connection to resume. An explicitly disconnected Downloads
authorization requires browser reconnection. Status remains readable with `status`
after `downloads` is removed. Cancellation stops new songs while safe bounded
active work and independent shared app downloads may finish. Completed audio stays.
Preparation does not change audio routing, playlist membership or saved collections.
