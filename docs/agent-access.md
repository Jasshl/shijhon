# Connect an agent

Shijhon exposes music tools over Streamable HTTP MCP. Add your installation's
`https://music.example.invalid/shijhon/mcp` URL to an MCP client, sign in with your
Navidrome account and approve permissions. No manually created credential or
Python client installation is needed. The optional [playlist skill](../skills/shijhon-playlists/SKILL.md)
adds curation and recovery guidance; the tools describe their own inputs.
The optional [audio preparation skill](../skills/shijhon-audio-preparation/SKILL.md)
covers explicitly requested server preparation and its recovery.

## Enable access

Set the canonical public origin and restart Shijhon:

```toml
[agents]
public_url = "https://music.example.invalid"
```

The environment equivalent is `SHIJHON_AGENTS__PUBLIC_URL`. Empty disables MCP.
The URL must be an HTTPS origin without a path, credentials, query or fragment;
HTTP is accepted on loopback for local development. Path-prefix installations are
not supported for this interface. Changing the origin invalidates existing OAuth
credentials and pending authorizations; reconnect agents at the new address.

The reverse proxy must preserve the public Host header and route these paths to
Shijhon, including the well-known paths at the origin root:

- `/shijhon/mcp`
- `/shijhon/agents/*`
- `/.well-known/oauth-protected-resource/shijhon/mcp`
- `/.well-known/oauth-authorization-server/shijhon/agents`

Keep query strings, authorization headers and form bodies out of proxy logs.
The authorization server supports client registration, the authorization-code
flow with S256 PKCE, refresh-token rotation and revocation. Browser sign-in and
consent remain on Shijhon. Native passwords are not given to the agent or retained.
As with existing playlist connections, the native owner must have
access to the configured library. OAuth does not add user accounts or delegate
the connected agent's model account.

## Features with partial configuration

Start with `get_capabilities`. It reports each feature's configuration,
permissions and missing setup separately. It does not import music/history or
call a model to test the connection. Configured AI can still fail at runtime.

- `search_library` browses/searches bounded native song metadata. No AI or history
  connection is needed. Library entries can include placeholders.
- `get_native_playlists` lists owned native playlists or reads their ordered
  tracks. It needs library-read permission, with no AI requirement.
- Library and playlist entries show year, genres and, when the files have them, bpm
  and moods. `include_sound` adds approximate hints from
  [sound analysis](sound-analysis.md) (tempo, key, energy and feature percentiles within
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
  need the [sound analysis](sound-analysis.md) connection (AudioMuse-AI), which a
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
- `get_playlist_request`, `list_playlist_requests` and `retry_playlist_request`
  operate on that connection's requests. `manage_playlist_refresh` pauses,
  resumes or refreshes saved instructions when requested, or, with `forget_edits`,
  forgets what refreshes learned from the user's edits to the playlist (the result's
  `forgotten` counts the songs; the playlist itself does not change). It cannot approve a
  preview: that deliberate review step stays in the human dashboard.
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
  download action. See the [preparation contract](../skills/shijhon-playlists/references/api.md#server-audio-preparation-mcp).

History and library reads have separate optional permissions. The agent should
use each source only when the user asks to use it for the current task. Permission
does not opt every future playlist into personal context. Creation influence
flags default off; explicit-track requests keep them off.

Missing configuration returns `setup_required`, the missing feature and the
signed-in setup URL. Missing permissions return `permission_required` and an
OAuth scope challenge, so supporting clients can request a new authorization.
Neither condition disables unrelated features. The complete creation payload
and recovery rules are in the [request contract](../skills/shijhon-playlists/references/api.md).

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
Server CLI installation, server-side CLI login and enabling private API destinations
remain installation tasks. An agent connecting over MCP cannot make its own AI account available to
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

Revoke a connection under **Describe a playlist → Agent access**, or through the
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
