---
name: shijhon-audio-preparation
description: Prepare server audio for an owned native Shijhon playlist through MCP, inspect progress and recover or cancel explicitly requested preparation. Device copies and playlist curation use separate workflows.
---

# Prepare playlist audio

Use Shijhon's connected MCP tools only when the user explicitly asks to prepare
playlist audio on the server. Creating a playlist, playing music or granting a
permission does not imply preparation intent. This saves audio in the server
library; copying files onto a device still uses that device's app.

Call `get_capabilities`. Preparation needs optional `downloads` permission and a
native/library connection, without AI or Last.fm. Follow the returned permission
challenge or signed-in setup URL when needed; never ask for credentials in chat.
Use the native playlist ID the user selected. If discovery is needed,
`get_native_playlists` requires its separate `library` permission and task intent.

Submit `prepare_playlist_audio` with the native `playlist_id` and one unique
`idempotency_key`. Retain both exact values and the returned batch ID. After a
lost response, replay those same values to recover the original batch; a changed
playlist ID conflicts. A playlist edited later does not change the saved snapshot.
This action keeps playlist membership as it is. Its catalog songs bring their albums
into the person's Shijhon library and All through recorded preparation activity;
this is separate from explicitly saving an album on the Collection page.

Read `get_audio_preparation` with `status` permission. Use bounded polling, up to
30 checks separated by at least two seconds, then report ongoing work with its
batch ID. Only `complete` confirms all snapshot audio is available on the server;
`finished` may include failures. Inspect per-song outcomes before reporting success.
`list_audio_preparations` recovers this connection's batch IDs.

Use `manage_audio_preparation` only for the user's requested `retry`, `resume` or
`cancel`. Retry requeues failed songs; resume requeues paused songs after a restart
or reauthorization. Diagnose a failure before one retry and read back the result;
do not loop retries indefinitely. Cancellation stops new songs and keeps completed
audio. Bounded active work and independent app downloads may finish safely.

Removing `downloads` pauses unfinished work at its next authorization preflight.
Status remains readable with `status` permission. Restore permission on the same
OAuth connection before explicit resume; a new connection cannot claim old jobs.
An explicitly disconnected Downloads authorization needs signed-in reconnection.

See the [preparation contract](../shijhon-playlists/references/api.md#server-audio-preparation-mcp)
for input bounds, progress fields and recovery limits.
