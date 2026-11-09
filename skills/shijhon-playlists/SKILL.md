---
name: shijhon-playlists
description: Create, refresh and manage Shijhon playlists through its connected MCP tools - mainly by choosing the songs yourself - including discovery mixes, playlist imports and explicitly requested recurring playlists. Use for music curation and playlist recovery; excludes server administration and audio downloads.
---

# Shijhon playlists

Use the connected Shijhon MCP tools. OAuth handles sign-in; do not request account
passwords, bearer keys or AI credentials in chat. Without a connection, direct the
user to add their installation's `/shijhon/mcp` URL and sign in.

Call `get_capabilities` to choose a workflow. Configuration and granted permissions
are independent. Missing server AI does not prevent library reads, configured
history reads or any agent-curated playlist. Report missing setup for the requested
feature, using the returned setup URL. Do not silently switch modes or enable
personal context to work around a missing setting.

## Your own curation: the main path

- Choose the exact requested count and ordered recordings, then send artist/title
  plus album/version where needed as `tracks` to `create_playlist`. Put the user's
  wish and constraints in `prompt`: Shijhon saves it as the playlist's brief, so you
  or another agent later know what the playlist is for. No server model is used.
- Shijhon publishes the tracks that resolve (the original release when several
  versions match) and leaves out the rest; status `tracks` gives each one's outcome
  and, for a left-out track, its `reason` and `candidates`. Close the gaps with
  `add_playlist_tracks` on the same request, never a new request: send the
  candidate's exact title or album when it is the recording you meant, or a fitting
  replacement within the user's constraints, with `replaces` set to the left-out
  position. Set `require_all: true` only when the user wants all or nothing.
- To refresh a playlist when the user asks, read it with `get_playlist_request`,
  choose a new list and send it to `replace_playlist_tracks`. The Navidrome playlist
  keeps its ID. `brief` updates what an agent-curated playlist is for. Shijhon does not
  refresh agent-curated playlists on a schedule; you do it when asked.
- Respect the user's edits. `user_edits` lists songs the user removed from the playlist
  in their apps (and songs they added). Removed songs stay out of every replacement
  (`removed_by_user` in `tracks`); treat added songs as examples of what they want.
  When the user wants a removed song back, use `manage_playlist_refresh` with
  `forget_edits` and that song's `song_ids`; without `song_ids` it forgets all their
  edits. Do it only when the user asks, and report the `forgotten` count.
- `edit_playlist_tracks` removes songs or reorders a published playlist by `song_id`
  (from `published_songs`), only on the user's request. Each listed ID removes one copy.
  Songs no longer in the playlist are remembered like the user's own removals.

## The user's other playlists

`list_playlist_requests` lists all the user's Shijhon playlists: made on the dashboard,
by you, by other agents, discovery mixes and imports. `get_playlist_request` reads any
of them. You may refresh, edit and manage them as above, with the create permission.
The songs of a playlist you did not make need the library permission, and the history
permission too when listening history chose them (a discovery mix, or a description that
used history). Without them you see its name and state, and only the songs you chose
yourself in a replacement; editing it needs those permissions too.

## Reading personal context

- Read history or library only when the user asks to use that source for this
  task, through `get_listening_history`, `search_library` or `get_native_playlists`.
  Permission alone is not consent. Library membership is not listening evidence.
- When the user relates a request to a playlist or songs they have ("like my Evening
  mix, but more energetic"), find it with `get_native_playlists` and look at it with
  `get_playlist_profile` before choosing. With a description for server AI, pass its ID
  in `reference_playlists` (song IDs in `reference_songs`) instead of pasting names into
  the prompt. This needs the library permission; the chosen references' songs are left
  out of the result unless you set `exclude_reference` to false. Without the permission,
  Shijhon also does not look up playlist names written in the prompt; with it, a playlist
  the prompt only names is found by name and its songs are not removed.

`get_song_facts`, `get_playlist_profile` and the `include_sound` option of
`search_library` and `get_native_playlists` add year, genres, Last.fm tags (when the user
connected a Last.fm key) and, for songs Shijhon's sound analysis has, tempo, key, energy
and feature percentiles within the library. These are approximate hints and many songs
have none. Your own knowledge of the music comes first, and a song is never rejected only
because facts are missing.

`find_similar_songs`, `search_by_sound` and `find_sound_path` help find library songs
and check a direction such as "more energetic"; your judgement decides what goes in the
list. They need sound analysis (see `get_capabilities`); if it is not set up or is
unreachable, carry on without it, and tell the user that a Navidrome administrator can
connect AudioMuse-AI on the Sound analysis page of Shijhon's settings. Tell the user how
many songs you could not check. When Shijhon's own AI describes the playlist,
sound-suggested songs join only if you set `use_library`.

## Optional: Shijhon's server AI, discovery mixes and imports

- With server AI configured, `create_playlist` also takes a description alone.
  Daily/weekly refreshes of a described playlist need an explicit user request,
  `save_instructions: true` and saved server AI. Shijhon runs them while you are
  offline; connecting an agent does not delegate its AI subscription.
- `create_discovery_mix` makes a playlist of songs similar to the user's Last.fm
  history, chosen on the server. It needs history permission and a connected Last.fm
  profile. `update_discovery_mix` changes its rules. Turn on refreshes
  (`enable_refreshes` with a daily or weekly cadence) only when the user asks.
- `manage_playlist_refresh` pauses, resumes or runs once (`refresh`, or `preview` to
  review first) the server's refreshes of described playlists and discovery mixes, only
  when requested.
- `import_playlist` imports a list the user gives you (pasted lines or entries), with
  library permission. Shijhon matches each entry in the background. Read the matches
  with `get_playlist_import`, `choose` among an ambiguous entry's `choices`, and
  `publish` with `review_playlist_import`, confirming how many entries stay missing
  (`missing_count`). Do not build the playlist again with `create_playlist`.

## Every change

Preserve requested counts, order, versions, artist limits and exclusions. Choose
a short musical name unless the user supplies one. Never relax constraints to fill
gaps. Tell the user which tracks were left out and why, and which you replaced with
what.
For exact payload fields and the optional HTTP client, consult
[the request contract](references/api.md).

A request to create or change a playlist authorizes its native publication. Retain
the exact input, one unique idempotency key per change and the returned request ID
until completion. After a timeout or uncertain response, check `get_playlist_request`,
or replay the same input and key. A `temporarily_unavailable` answer that says nothing
was changed means another playlist was being published: send the same call again a
minute later, with the same key. Never repeat a change under a new key merely because
the response was lost. A conflict requires recovering the original input. After
diagnosing a recoverable failure, use `retry_playlist_request` at most once and
recheck status. Report pending or uncertain work honestly; only `published` confirms
verified native publication.

Publication can add catalog releases or missing tracks as library placeholders;
it does not download all their audio or prepare device copies. `preview: true` waits
for review: show the user the `selected` songs and publish or discard with
`review_playlist_preview` only when they say so.

`configure_playlist_connection` changes the model on Shijhon's existing server AI
connection, with its separate permission and the user's direction. Shijhon uses
that model for description-based and recurring playlists; it does not change your
own model. Last.fm account changes, new credentials and endpoints belong in signed-in
browser setup; installing/authenticating a server runner belongs on the host.
Report the specific prerequisite rather than asking for credentials or inventing
an external schedule.
