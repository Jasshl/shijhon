---
name: shijhon-playlists
description: Create Shijhon playlists from descriptions or ordered recordings through its connected MCP tools, including explicitly requested recurring playlists. Use for music curation and playlist recovery; excludes server administration and audio downloads.
---

# Shijhon playlists

Use the connected Shijhon MCP tools. OAuth handles sign-in; do not request account
passwords, bearer keys or AI credentials in chat. Without a connection, direct the
user to add their installation's `/shijhon/mcp` URL and sign in.

Call `get_capabilities` to choose a workflow. Configuration and granted permissions
are independent. Missing server AI does not prevent library reads, configured
history reads or submitting agent-curated recordings. Report missing setup for the
requested feature, using the returned setup URL. Do not silently switch modes or
enable personal context to work around a missing setting.

- With server AI configured, send the user's description and parameters to
  `create_playlist`. Daily/weekly refreshes need an explicit user request,
  `save_instructions: true` and saved server AI. Shijhon runs them while the agent
  is offline; connecting an agent does not delegate its AI subscription.
- For agent curation, choose the exact requested count and ordered recordings,
  then submit artist/title plus album/version where needed. Preserve the original
  constraints in `prompt`. Explicit tracks use no server model and cannot recur.
- Read history or library only when the user asks to use that source for this
  task, through `get_listening_history`, `search_library` or `get_native_playlists`.
  Permission alone is not consent. Library membership is not listening evidence.
- When the user relates a request to a playlist or songs they have ("like my Evening
  mix, but more energetic"), find it with `get_native_playlists` and pass its ID in
  `reference_playlists` (song IDs in `reference_songs`) instead of pasting names into
  the prompt. This needs the library permission; the chosen references' songs are left
  out of the result unless you set `exclude_reference` to false. Without the permission,
  Shijhon also does not look up playlist names written in the prompt; with it, a playlist
  the prompt only names is found by name and its songs are not removed. Look at it first
  with `get_playlist_profile` if that helps you decide.

`get_song_facts`, `get_playlist_profile` and the `include_sound` option of
`search_library` and `get_native_playlists` add year, genres, Last.fm tags (when the user
connected a Last.fm key) and, for songs Shijhon's sound analysis has, tempo, key, energy
and feature percentiles within the library. These are approximate hints and many songs
have none. Your own knowledge of the music comes first, and a song is never rejected only
because facts are missing.

For agent-curated lists, `find_similar_songs`, `search_by_sound` and `find_sound_path`
help find library songs and check a direction such as "more energetic"; your judgement
decides what goes in the list. They need sound analysis (see `get_capabilities`); if it
is not set up or is unreachable, carry on without it, and tell the user that a Navidrome
administrator can connect AudioMuse-AI on the Sound analysis page of Shijhon's settings. Tell the
user how many songs you could not check. When Shijhon's own AI describes the playlist,
sound-suggested songs join only if you set `use_library`.

Preserve requested counts, order, versions, artist limits and exclusions. Choose
a short musical name unless the user supplies one. Report insufficient or
ambiguous recordings instead of substituting versions or relaxing constraints.
For exact payload fields and the optional HTTP client, consult
[the request contract](references/api.md).

A request to create a playlist authorizes its native publication. Retain the exact
input, one unique idempotency key and the returned request ID until completion.
After a timeout or uncertain response, check `get_playlist_request`, or replay the
same input and key to recover its ID. Never create again under a new key merely
because the response was lost. A conflict requires recovering the original input.
After diagnosing a recoverable failure, use `retry_playlist_request` at most once
and recheck status. Report pending or uncertain work honestly; only `published`
confirms verified native publication.

Publication can add catalog releases or missing tracks as library placeholders;
it does not download all their audio or prepare device copies. `preview: true`
deliberately waits for publication in the human dashboard. Do not bypass it.

Use `manage_playlist_refresh` only for requested pause/resume/refresh actions.
`forget_edits` is one of them: it makes refreshes forget what they learned from the
user's edits to the published playlist, so removed songs may be suggested again. Offer
it only when the user asks, and report the `forgotten` count it returns.
`configure_playlist_connection` changes the model on Shijhon's existing server AI
connection, with its separate permission and the user's direction. Shijhon uses
that model for description-based and recurring playlists; it does not change your
own model. Last.fm account changes, new credentials and endpoints belong in signed-in
browser setup; installing/authenticating a server runner belongs on the host.
Report the specific prerequisite rather than asking for credentials or inventing
an external schedule.
