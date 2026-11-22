# Agent tools

[Agent access](../agent-access.md) covers turning it on and connecting an app.
This page describes the tools, permissions and recovery rules in detail. The tools also
describe their own inputs to the agent.

Start with `get_capabilities`. It reports each feature's configuration, permissions and
missing setup separately. It does not import music or history, or call a model.
Configured server AI can still fail at runtime.

## What the tools do

- `search_library` browses/searches bounded native song metadata. No AI or history
  connection is needed. Library entries can include placeholders.
- `get_native_playlists` lists owned native playlists or reads their ordered
  tracks. It needs library-read permission, with no AI requirement.
- Library and playlist entries show year, genres and, when the files have them, bpm
  and moods. `include_sound` adds approximate hints from
  [sound analysis](../sound-analysis.md) (tempo, key, energy and feature percentiles within
  the library, labels) to the songs that have them. Listening data such as play counts
  and ratings is never included.
- Community tags from Last.fm appear on entries when the user's playlist connection has
  a Last.fm API key. Lists show only tags Shijhon already knows and ask Last.fm for
  nothing; `get_song_facts`, `get_playlist_profile` (for the songs it lists) and the sound
  tools look tags up for their songs, within about five seconds and 40 requests a call,
  so a long list may be only partly tagged and a second call adds more. A missing or
  slow Last.fm never makes a tool fail; the result's `tags` says `not_configured`,
  `partial`, `unavailable` or `ok`. Only artist and title go to Last.fm.
- `get_song_facts` and `get_playlist_profile` (library-read permission) return those
  facts for songs and summarize an owned playlist: year range, top genres and tags,
  medians of energy, tempo and the feature percentiles, and how many songs have sound
  facts. They work without sound analysis or tags, with fewer facts.
- `find_similar_songs`, `search_by_sound` and `find_sound_path` (library-read
  permission) suggest library songs that sound like seed songs or a playlist, match
  an English description of a sound, or move gradually from one song to another. They
  need the [sound analysis](../sound-analysis.md) connection (AudioMuse-AI), which a
  Navidrome administrator sets up on the **Sound analysis** page of Shijhon's settings, and say so
  when it is not set up or not reachable; `get_capabilities` reports both. All of this is
  optional, approximate information: an agent's own knowledge of the music comes first,
  and songs without facts are never a reason to drop a song.
- `find_listened_together` (library-read permission) lists songs that people listen to
  together with up to five seeds: library songs by Navidrome song ID (`seed_song_ids`)
  and songs named by artist and title (`seed_tracks`). It asks
  [ListenBrainz](../configuration.md#listenbrainz) for everyone, unless an administrator
  turned it off, and Last.fm when the user's playlist connection has a Last.fm key, and
  sends them only the seeds' artists and titles. It returns up to 100 songs (`count`,
  30 by default), the songs on several lists and high on them first, each with its artist,
  title and the `sources` that list it; the seeds themselves are left out. `sources` in
  the result says for each source `ok`, `partial` (it answered for some seeds, but failed
  or ran out of the call's twelve seconds for others), `unavailable` (it answered for
  none), `off` (ListenBrainz turned off) or `not_configured` (no Last.fm key). Each
  source's songs are kept as they arrive: one source running out of time never loses the
  other's. Without either source the tool answers `setup_required` with
  `listening_similarity`, as `get_capabilities` reports. The songs are names: whether the
  library or the catalog has them is not looked up, so an agent finds them with
  `search_library` or `search_catalog`, or submits them as artist/title tracks to
  `create_playlist`. Like the sound tools, this is a hint for the agent's own judgement.
- `get_listening_history` returns bounded artist/title listen counts from the
  connected Last.fm history source, restricted to the configured `history_days` window.
  It needs that source configured and history permission; it does not substitute library membership for listening evidence.
- `get_new_releases` returns the new releases of the artists the account follows, its
  favorite and most played artists, as its apps' newest albums start with them
  ([new releases](../how-it-works.md#new-releases)): each with its catalog album ID,
  title, artist, type, release date and track count, the newest first, 1 to 100 a call.
  It also says how many artists are followed and when they were last checked. It needs
  the library and the history permission, because the list follows from the account's
  favorites and plays. Only the user can turn following on, on the dashboard's **New
  releases** page; until then the tool says so and returns no releases. It reads what
  Shijhon's daily check saved and asks the catalog nothing.
- `create_playlist` accepts either a description using saved server AI, or an
  ordered artist/title list curated by the external agent without server AI.
  Track versions and all user constraints are preserved. Native/catalog IDs and
  arbitrary URLs are not accepted as explicit selections.
  Each explicit track becomes the library's recording, else the catalog's (the original
  release when several versions match). The tracks that resolve are published in order;
  the request's status reports every track's outcome and, for each one left out, why
  (not found, only on other albums, only other versions, credited differently, the
  catalog did not answer, a repeat) with the nearest songs found. `require_all` publishes
  nothing unless every track resolves. When an administrator has set a
  [daily cap on adding](../how-it-works.md#the-daily-cap-on-adding), a track whose
  release would pass the account's cap is left out as `library_refused`, with a detail
  that says the daily limit of added releases is reached; the status then also has
  `daily_limit`, with how many songs were left out for it. Places are taken in the order
  of the tracks, so the first tracks are the ones published. A described playlist or a
  discovery mix is published without such songs in the same way. A scheduled refresh
  that would have no song left has the state `postponed`, with the reason in `error`:
  nothing failed, the playlist and its schedule stay as they are, and the next
  scheduled refresh tries again. `get_capabilities`
  lists the cap under `limits.added_releases_per_day`, with how many releases the account
  has added in the last 24 hours, how many are left and when the next one is free; the
  entry is absent while no cap applies to the account. `add_playlist_tracks` puts tracks in the places
  of left-out ones, or at the end, and republishes the same playlist, unless it was
  edited in Navidrome since publication.
  When a request builds on playlists or songs the user already has, the agent finds
  them with `get_native_playlists` or `search_library` and passes them as
  `reference_playlists` or `reference_songs` instead of pasting names. Choosing
  references needs the library permission, and so does finding a playlist the
  description only names. The submitting token must include library permission;
  Shijhon saves that ceiling and rechecks the current connection at every generation
  and refresh. Without both permissions, Shijhon does not look up names. Shijhon leaves
  the chosen references'
  songs out of the result unless `exclude_reference` is false, and only avoids
  repeating the songs of a playlist found by name. The AI sees reference songs with
  their facts, and the request's status shows the references, the goal, each song's
  fit and the facts coverage.
- A change is durable as soon as a tool accepts it, and a worker on the server carries it
  out. The tool's answer waits a few seconds for that work. If it finished, `state` is
  final. If it did not, `state` is `queued`, `generating` or `publishing`, the answer leaves
  out `published_songs`, because those are still the songs from before the change, and
  `next` says what to call. `playlist_id` is empty until a first publication. The same `next`
  appears when a preview waits or a run failed.
- With explicit tracks, `count` is ignored and the number of tracks is used. `max_per_artist`
  defaults to 2 and goes up to 10. A list over the limit is refused with the artists named
  and how to allow them. A track whose recording is credited to an artist the playlist
  already has that many songs by, as when a featured credit resolves to the main artist's
  recording, is left out and reported as `artist_limit`.
- Daily/weekly described playlists need saved server AI and explicitly saved
  refresh instructions. Shijhon's durable worker runs them after the agent
  disconnects. A remote agent does not need to stay connected or supply a timer.
- `list_playlist_requests` lists all the user's Shijhon playlists, in short summaries:
  those made on the dashboard, by this or another agent, discovery mixes and imports.
  `get_playlist_request` reads one, with its brief and the songs the user removed or
  added in their apps. Its songs need the library permission when another agent or the
  dashboard made the playlist, and the history permission when it was made from
  listening history. `retry_playlist_request` retries a failed run. In both, `live` says
  whether the playlist has been published. While a run waits or after one failed, `state`
  and `selected` describe that run, and `brief` and `published_songs` what is live. A
  discarded run changes nothing, so a live playlist then reads as `published`; the run
  appears as `discarded_run` in `get_playlist_request` and as `latest_run_state` in the list.
- `replace_playlist_tracks` publishes a new ordered list (up to 100 tracks) under the
  same playlist, resolved like `create_playlist`'s tracks. Songs the user removed stay
  out (`removed_by_user`). On a playlist the agent curated, `brief` updates what it is
  for; described playlists and discovery mixes keep their instructions and schedule.
- `edit_playlist_tracks` removes songs from a published playlist or reorders it, by
  `song_id`. Songs it removes are remembered like the user's own removals. The edit is
  not applied if the playlist changed again before it ran.
- `review_playlist_preview` publishes or discards a preview. Replacements and edits wait
  while a run is unfinished or a preview waits for review. Publishing a selection that
  Shijhon made from listening history or the library needs that permission too, but
  publishing the preview of a replacement does not, because an agent chose those songs.
- `manage_playlist_refresh` pauses, resumes, runs or previews the server's refreshes of
  described playlists and discovery mixes, or, with `forget_edits`, forgets what
  refreshes learned from the user's edits (the result's `forgotten` counts the songs;
  the playlist itself does not change).
- `create_discovery_mix` and `update_discovery_mix` make and change discovery mixes. They
  need the history permission and a connected Last.fm profile.
- `import_playlist` imports pasted lines or structured entries through the same matching
  as the dashboard's import, with the library permission. Matching runs in the background
  and can take a while. `get_playlist_import` reads the matches and shows `checked` of
  `total`, and its `review_key` is empty until `state` is `preview`. `review_playlist_import`
  chooses among an entry's candidates, publishes with a confirmed number of missing entries,
  and rechecks or completes them later. The key changes after each choice, so read it again
  before the next action. An entry that nothing matched while an add-on did not answer says
  so, and can be published without by counting it in `missing_count`, or asked about again.
- `prepare_playlist_audio` snapshots an owned native playlist and queues server
  audio preparation. It needs separate optional `downloads` permission and an
  explicit user request. A native/library connection is sufficient; AI and Last.fm
  are unnecessary. It reuses the existing preparation worker and verified download
  path. Playlist membership stays as it is; its catalog songs bring their albums
  into the person's Shijhon library and All through recorded preparation activity,
  separately from explicitly saving an album on the Collection page.
- `get_audio_preparation` and `list_audio_preparations` read bounded progress for
  this OAuth connection with `status` permission. `manage_audio_preparation`
  retries failed songs, resumes paused songs or cancels new work with `downloads`
  permission. Completed audio remains; copying audio onto a device uses the app's
  download action. See the [preparation contract](../../skills/shijhon-playlists/references/api.md#server-audio-preparation-mcp).

- `search_catalog` (the `add` permission) searches the catalog for albums and artists to
  add: up to 25 of each, an album as one edition by the edition rules, with its catalog
  album ID, title, artist, type, release date and track count, and an artist with its
  catalog artist ID and name. The existing search tools read the library and name songs
  only, so this is how an agent finds a catalog album or artist; `get_new_releases` also
  returns catalog album IDs. It reads nothing personal and does not say what the library
  already has.
- `add_to_library` (the `add` permission, and the user's request) adds up to 25 catalog
  albums named by `album_ids`, or the releases of one artist named by `artist_id`. Each
  release goes through the same code as an app's action on a catalog album: it is added
  whole, into the owned album it completes or as a catalog album, with the same refusals,
  and once Navidrome shows it to the account it is recorded as the account's activity, so
  it joins the account's Shijhon library and All. An album the person removed from a
  library by hand stays out of that library.
  An artist stands for the releases the artist's page in apps shows, one edition of each
  album: those the library lacks, and those it has, which then join the account's library
  too. Only albums are taken unless `release_types` names others (`album`, `ep`,
  `single`, `compilation`), the newest first, and at most `max_releases` of them (10 unless
  the call says otherwise, never more than 25). The answer says how many releases of
  those types there are and how many the limit left out.
  With `preview: true` nothing is written and no key is needed: the answer lists what
  would be added, and its `album_ids` name exactly those releases. An artist's releases
  are looked up anew by every call that names the artist, and the catalog's list can
  change between two calls, so a preview does not bind a later call with the same
  `artist_id`. To add what the person agreed to, the agent passes the preview's
  `album_ids`. Adding by `artist_id` without a preview remains for a person who asked
  for exactly that, and the answer lists what was added.
  Without `preview`, the call needs an `idempotency_key`. The request is kept
  under that key and a worker adds its releases one after another, so the answer waits a
  few seconds and then says each release's state: `added`, `already_in_library` (the
  library had it; it is now in the account's library too), `not_added` with a `reason`
  and a sentence, or `pending`. While releases are pending, `next` names
  `get_library_addition`. An account has at most three unfinished additions.
  When an administrator has set a
  [daily cap on adding](../how-it-works.md#the-daily-cap-on-adding), the releases count for
  the account. A release over the cap is `not_added` with the reason `daily_limit`, and
  the answer's `daily_limit` says that the limit is reached, how many releases were added
  and how many were left out. A preview and an accepted request also carry
  `added_releases_per_day`, where the account stands, as `get_capabilities` reports it.
- `get_library_addition` reads one addition of this connection, with the `add` or the
  `status` permission.
- `get_review_list` (the `admin` permission, a Navidrome administrator) lists the albums
  in the Library's review list as the dashboard shows them, 25 a page: the album, how many
  of its songs the library owns, why it needs a decision and the releases offered, with
  the chosen one first. With `album_id` it says whether that album is still in the list
  (`in_review_list`) and how its last action went (`last_action`: running, finished or
  failed). The last action is remembered for about a quarter of an hour and until a
  restart, and a keep leaves none.
- `settle_review_album` (the `admin` permission, a Navidrome administrator, and the
  user's decision) settles one album a call through the Library page's own action: `fill`
  from one of the album's offered releases, `keep` as it is, or `rematch`. The page's
  checks apply: only a listed release can fill, an album that is part of another album's
  release is not filled on its own, one action runs for an album at a time and one fill
  at a time, and nothing is matched while the catalog rests after errors. A fill and a
  rematch run in the background; the answer waits a few seconds and says `running`,
  `finished` or `failed` with the dashboard's message. Several albums are settled one
  call after another.
  The call needs an `idempotency_key`, which stands for one action. The key is taken
  before the action starts, so of two calls with one key only one carries an action out:
  the same input joins the first call and gets that action's own result with
  `replayed`, and other input under the key is a `conflict`. A replay answers with what
  its own call did, not with the album's latest action; for background work that is the
  work as it stands. Keys are kept in the server's memory only: each for one hour, the
  newest 500 of them, and a restart forgets them all. After that the same call is a new
  action. An agent therefore does not rely on the key alone after an uncertain result
  (an error, a timeout, a `running` or `unknown` result): it reads the album with
  `get_review_list` and its ID first. `last_action` says how the last action went, and
  `in_review_list` whether the album still waits. A rematch can leave an album in the
  list, so being in the list does not show that nothing happened. The agent sends the
  action again only when no action is running and the person decides so. A result that
  says nothing was started (`refused`, `busy`, `catalog_resting`, `not_started`,
  `not_in_review_list`) stays that key's result, so a later attempt is a new action
  with a new key.
- `get_server_status` (the `admin` permission, a Navidrome administrator) returns a
  read-only summary of what **Diagnostics** shows: the versions of Shijhon, Navidrome and
  the database schema, whether Navidrome answers, the catalog's state, each add-on's
  name and state (`ok`, `off`, `needs_sign_in`, `manifest_unreachable`, `cooling_down`,
  `failing` or `not_checked`), the review list's length, the cleanup's mode and last
  check, whether saved settings wait for a restart, and `needs_attention` in sentences.
  An add-on that needs its operator to sign in names its sign-in page only when that
  address has no query string, no fragment and no user part. The summary holds no other
  address, no token, no setting's value and nothing about any account.

History and library reads have separate optional permissions. The agent should
use each source only when the user asks to use it for the current task. Permission
does not opt every future playlist into personal context. Creation influence
flags default off; explicit-track requests keep them off.

Missing configuration returns `setup_required`, the missing feature and the
signed-in setup URL. Missing permissions return `permission_required` and an
OAuth scope challenge, so supporting clients can request a new authorization.
The administrator tools return `administrator_required` when the grant has the
permission but the account is not a Navidrome administrator now; a new authorization
does not change that.
Neither condition disables unrelated features. The complete creation payload
and recovery rules are in the [request contract](../../skills/shijhon-playlists/references/api.md).

## Connection setup through an agent

With the separate `settings` permission, `configure_playlist_connection` can
save this user's choice among administrator-approved models on the shared AI
service. Blank selects the administrator's default. Ordinary accounts can grant
this permission too. `get_capabilities` lists approved/default models and the personal
choice without exposing host credentials or endpoints. Shijhon uses the choice for
description-based and recurring playlists. This does not change
the external agent's model or give Shijhon access to its AI account.

Change the Last.fm account and enter new AI credentials, AI endpoints and history
application keys through the signed-in **Playlists** page linked in setup results.
Installing and signing in a [local AI runner](local-ai-runners.md) remain installation
tasks, and allowing AI on private addresses is an administrator setting. An agent connecting over MCP cannot make its own AI account available to
unattended server generation merely by signing in.

## Recovery and revocation

Keep the same idempotency key and exact input after a lost response. Identical
replay recovers the original request even if AI/history setup was subsequently
removed. A changed payload under the same key is a conflict. Tokens rotate while
the connection identity stays stable, preserving status, retry and recurring
work. For 20 seconds after refresh-token rotation, retrying the same token with
the same scopes returns the original replacement pair. This covers overlapping
refreshes and immediate retries after a lost response without extending the
window on each retry. Replacement pairs stay only in process memory and are
discarded after the window; the database retains only token hashes. Reuse after
20 seconds, or after a restart has lost the pair, revokes that connection,
including its replacement credentials. During the window, anyone holding the
spent refresh token can recover its replacement; current client, connection and
permission checks still apply.

After sign-in, the consent form selects the connection's existing permissions plus the
newly requested ones. Clear a permission to remove it deliberately. The `add` and `admin`
permissions are the exception: the form shows them cleared until the connection has them,
so a client that asks for every permission does not get them unless the person selects
them, and `admin` is on the form only for an account that is a Navidrome administrator
at that moment. An agent that was connected before these permissions existed does not
have them until it is connected again and they are selected. Authorizing
additional permissions for the same OAuth client/account preserves the connection and
its requests. Removing a permission takes effect on outstanding credentials too. Client
registration describes possible scopes; only browser consent grants access to the user's
data or actions. Status remains readable with status permission after creation
permission is removed. Pausing needs creation/management permission but no
history/library read permission; starting another generation needs its saved context
permissions. Preparation status similarly remains readable after `downloads` is removed.
Removing `downloads` or revoking the connection pauses unfinished preparation at the
existing worker preflights, including after acquisition waits and before placement.
An addition to the library is checked against its connection before every release and
again where files are written: once the connection is revoked, loses `add`, or its OAuth
client is no longer registered, the request stops, its remaining releases are not added,
and its status says so. A review-list action started by an agent is checked the same way,
against the `admin` permission and the account's administrator role: before it starts,
where a fill writes files, and after every wait immediately before a decision is
forgotten or recorded, so a keep or a rematch that waited changes nothing once the
agent's access has ended. A restart continues an unfinished addition where it stopped,
while its connection still allows it; adding a release that is in the library writes
nothing. A release that a stopped request had written before a restart, and that the
restart may no longer add for the account, stays in the server's library and is reported
as not in the account's library.
Independent app downloads can continue through shared acquisitions. After restoring
permission on the same connection, explicitly resume its paused batch. Restart also
pauses unfinished preparation for explicit resume. If the Downloads page was explicitly
disconnected, reconnect there first.

Revoke a connection on **Playlists**, or through the
OAuth revocation endpoint. Revocation stops future/unfinished work at its next
authorization check. Published playlists and prior library additions remain.
The owner can still pause these playlists and discard previews in the dashboard
after revocation. Temporary Navidrome failures return HTTP `503` with
`temporarily_unavailable` during token verification or exchange; retain the refresh
token and retry later. Revoked credentials still return `invalid_grant`.
The JSON request API also requires OAuth access tokens; it never accepts native
passwords or dashboard cookies.

## Personal access

Personal OAuth access requires current identity and configured-library access. Grants,
requests and preparation jobs remain bound to the immutable owner and connection.
The `add` permission adds to the connected account's own library only, counted for that
account. The `admin` permission is the only one that reaches beyond the account, and
only for the review list and the read-only status: whether the account is a Navidrome
administrator is asked of Navidrome at every call and at each write of a fill it
started, never taken from the sign-in. It gives no access to settings, add-on addresses,
credentials or other accounts' data.
The `settings` permission permits only the connected account's approved model choice.
It grants no administrator rights or authority to change host endpoints, credentials,
executables, approved models or another account's preferences. Model approval and
current account authorization are checked again before each model request, including
scheduled jobs. Revoking an agent grant stops subsequent agent settings changes;
it does not erase the user's saved personal model preference.
