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
- `get_listening_history` returns bounded artist/title listen counts from the
  connected Last.fm history source, restricted to the configured `history_days` window.
  It needs that source configured and history permission; it does not substitute library membership for listening evidence.
- `create_playlist` accepts either a description using saved server AI, or an
  ordered artist/title list curated by the external agent without server AI.
  Track versions and all user constraints are preserved. Native/catalog IDs and
  arbitrary URLs are not accepted as explicit selections.
  Each explicit track becomes the library's recording, else the catalog's (the original
  release when several versions match). The tracks that resolve are published in order;
  the request's status reports every track's outcome and, for each one left out, why
  (not found, only on other albums, only other versions, credited differently, the
  catalog did not answer, a repeat) with the nearest songs found. `require_all` publishes
  nothing unless every track resolves. `add_playlist_tracks` puts tracks in the places
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
- Daily/weekly described playlists need saved server AI and explicitly saved
  refresh instructions. Shijhon's durable worker runs them after the agent
  disconnects. A remote agent does not need to stay connected or supply a timer.
- `list_playlist_requests` lists all the user's Shijhon playlists, in short summaries:
  those made on the dashboard, by this or another agent, discovery mixes and imports.
  `get_playlist_request` reads one, with its brief and the songs the user removed or
  added in their apps. Its songs need the library permission when another agent or the
  dashboard made the playlist, and the history permission when it was made from
  listening history. `retry_playlist_request` retries a failed run.
- `replace_playlist_tracks` publishes a new ordered list (up to 100 tracks) under the
  same playlist, resolved like `create_playlist`'s tracks. Songs the user removed stay
  out (`removed_by_user`). On a playlist the agent curated, `brief` updates what it is
  for; described playlists and discovery mixes keep their instructions and schedule.
- `edit_playlist_tracks` removes songs from a published playlist or reorders it, by
  `song_id`. Songs it removes are remembered like the user's own removals. The edit is
  not applied if the playlist changed again before it ran.
- `review_playlist_preview` publishes or discards a preview. Replacements and edits wait
  while a run is unfinished or a preview waits for review.
- `manage_playlist_refresh` pauses, resumes, runs or previews the server's refreshes of
  described playlists and discovery mixes, or, with `forget_edits`, forgets what
  refreshes learned from the user's edits (the result's `forgotten` counts the songs;
  the playlist itself does not change).
- `create_discovery_mix` and `update_discovery_mix` make and change discovery mixes. They
  need the history permission and a connected Last.fm profile.
- `import_playlist` imports pasted lines or structured entries through the same matching
  as the dashboard's import, with the library permission. `get_playlist_import` reads
  the matches, and `review_playlist_import` chooses among an entry's candidates,
  publishes with a confirmed number of missing entries, and rechecks or completes them
  later.
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

History and library reads have separate optional permissions. The agent should
use each source only when the user asks to use it for the current task. Permission
does not opt every future playlist into personal context. Creation influence
flags default off; explicit-track requests keep them off.

Missing configuration returns `setup_required`, the missing feature and the
signed-in setup URL. Missing permissions return `permission_required` and an
OAuth scope challenge, so supporting clients can request a new authorization.
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
newly requested ones. Clear a permission to remove it deliberately. Authorizing
additional permissions for the same OAuth client/account preserves the connection and
its requests. Removing a permission takes effect on outstanding credentials too. Client
registration describes possible scopes; only browser consent grants access to the user's
data or actions. Status remains readable with status permission after creation
permission is removed. Pausing needs creation/management permission but no
history/library read permission; starting another generation needs its saved context
permissions. Preparation status similarly remains readable after `downloads` is removed.
Removing `downloads` or revoking the connection pauses unfinished preparation at the
existing worker preflights, including after acquisition waits and before placement.
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
The `settings` permission permits only the connected account's approved model choice.
It grants no administrator rights or authority to change host endpoints, credentials,
executables, approved models or another account's preferences. Model approval and
current account authorization are checked again before each model request, including
scheduled jobs. Revoking an agent grant stops subsequent agent settings changes;
it does not erase the user's saved personal model preference.
