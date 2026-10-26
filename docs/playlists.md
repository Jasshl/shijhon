# Playlists

To preserve an existing ordered track list, including repeated songs, use
[Import a playlist](playlist-imports.md). Paste artist/title lines or upload a CSV,
review matched and unresolved entries, then explicitly publish the reviewed selection.

## Describe a playlist

Open **Playlists → Describe a playlist** in the dashboard. Signing in already connects
your Navidrome account (after you disconnect, connect it again on **Playlists**), and an
AI service must be set up ([AI connections](#ai-connections)); Last.fm is optional. Enter the
instructions, track count (1–100) and maximum tracks per artist (1–10), then choose
**Create playlist**. Publication is included. The **Review a preview before
publication** option, under **Advanced**, stops before publication until you choose
**Publish playlist**.

The playlist name is optional: leave it blank to choose a short title from your
instructions and the selected music, or enter a name to keep it exactly as supplied.
Automatic naming uses the existing final ordering call. The name and selection are
saved together before preview or publication, so retries, restarts and refreshes keep
the first title.

With [AudioMuse-AI](sound-analysis.md) connected, facts about how songs sound are one more
hint for the AI, beside its own knowledge of the music.

Listening-history and library-context influences are independent and off by default.
History needs a configured profile and application key. When enabled, AI receives
up to 100 aggregated artist/title listening counts, without a timeline. Library
context shares a bounded sample of up to 100 artist/title/album entries; library
membership does not establish that you listened to a track. With both off, AI sees
your instructions, its attempted suggestions, the recordings resolved for this request
with their [facts](#facts-as-hints), and the songs of [references](#reference-playlists-and-songs)
named in this request. Exact recording lookup still checks your accessible library to
reuse music; that lookup does not feed unrelated library contents into AI.

Shijhon asks for bounded artist/title suggestions, looks them up in your library and
the configured catalog, then asks AI to choose and order only resolved candidates.
An explicit album or recording-version qualifier must match, including during
publication and recovery. Version text must be evidenced in the resolved title;
a version that cannot be verified fails rather than accepting a different remaster.
Ambiguous or unavailable recordings are skipped. There are at most three suggestion rounds and at most the
configured candidate limit (capped at 300) attempted suggestions. The final selection
must have the exact count, no repeated recording and the requested artist limit. AI
also checks the free-text instructions; those semantic judgments depend on the model.
An invalid or insufficient selection fails clearly instead of silently ignoring the
instructions. Track selection and release effects are frozen before native publication.

The final call also returns a one-sentence goal and, for each song, a short reason and
whether it meets, partly meets or misses the instructions. When some songs do not fully
meet them and unused candidates remain, Shijhon asks once more for a revised selection,
with those songs flagged. The revision is used when it passes the same checks; otherwise
the first selection stays. The ratings are shown with the result and never reject songs
by themselves. The AI is asked for a coherent order that uses tempo, key and energy,
where they are known, when the instructions ask for flow.

The request's page shows what the AI worked with and what it chose:

- the references it was compared with, each marked as chosen or found by name in your
  instructions, with a short profile (typical energy and tempo, years, genres, tags) and
  how many suggested songs were left out because they are already in a chosen reference;
- the goal sentence, and whether the selection was revised or checked once more;
- for each song: the AI's reason, whether it fits, partly fits or doesn't fit, where it
  comes from (your files, added by Shijhon or from the catalog), whether sound analysis
  suggested it, and its year, genres, tags and key;
- with sound facts: each song's energy as a bar with the reference's typical energy
  marked, its tempo beside the reference's, and how many of the songs had sound facts or
  Last.fm tags. Energy is a percentile in your library.

A request with explicit tracks, and songs without facts, show only what they have.

Requests are one-off by default. Their instructions and run state remain in private
state for status and recovery, but no schedule is enabled. **Save these instructions for
reuse** also allows an explicit manual refresh from the request page. Daily or weekly refreshes
require that checkbox, an explicit cadence and usable saved AI; they start after verified
publication. Pause stops future refreshes. Retry reuses a frozen selection and reconciles
an uncertain native write rather than choosing different music or blindly creating a
second playlist. A selection interrupted before it was frozen can be regenerated.

### Reference playlists and songs

A description can relate to music you already have: "like my Velvet Orbit playlist but
more energetic". Requests accept up to three Navidrome playlist IDs
(`reference_playlists`) and up to 50 song IDs (`reference_songs`) that your account can
read. Without them, Shijhon looks for your own playlists whose names the instructions
mention: whole words, ignoring case, longer names first, at most three. Names shorter
than four letters and names of only general words, such as "Chill Mix", are ignored;
a one-word name counts only in quotes or next to "playlist" or "mix" (`"Driftwood"`,
`my Driftwood playlist`). Of several playlists with the same name, the most recently
changed one is used, and a refreshed playlist is never its own reference. The request
shows the references used and whether they were found by name.

In the dashboard, **Compare with your playlists** lists your own Navidrome playlists by
name (the first 500); choose up to three. **Leave out songs already in the reference** is checked by
default (`exclude_reference`). With no playlist chosen there, a playlist named in the
instructions is used as described above.

References are read through your Navidrome account each time the playlist is generated,
so refreshes follow their current songs. An explicit reference that cannot be read fails
the run with its ID; a playlist found by name that cannot be read is skipped. Shijhon
keeps up to 200 reference songs, your listed songs first, and shows the AI an even
sample of up to 100 with their facts and a short profile: typical energy, tempo and mood
percentiles, common tags and genres, and the range of years. By default
(`exclude_reference`) every song of the references you chose is left out of the new
playlist; turn it off to allow them. Songs of a playlist found by name are not removed
automatically: the AI is asked to avoid repeating them unless the instructions ask for
them. References need a description; explicit tracks cannot use them.

Agents need the library permission for references. A request with chosen references is
refused without it. For a request an agent created, playlist names in the instructions
are looked up only while the agent's connection has the library permission, checked
again at every generation and scheduled refresh: without it, no playlist is listed or
read and the result shows no reference. If the permission is removed while a playlist
found by name is in use, that generation stops. Your own requests from the dashboard
always look up names.

### Facts as hints

For reference songs and candidates, the AI also sees a few facts in case they help:

- year and genres, as your library or the catalog gives them;
- up to five Last.fm community tags, when your playlist connection has a Last.fm API key;
- tempo, key, energy and mood percentiles from [sound analysis](sound-analysis.md) with
  AudioMuse-AI, when it is set up and has analyzed the song's audio;
- where the song comes from: your own files, added to the library by Shijhon, or the
  catalog (not in the library yet).

The AI is told that facts are approximate hints, that many songs have none and that its
own knowledge of the music comes first; percentiles compare songs within this library.
A missing fact never excludes a song. An unavailable or slow source means fewer facts
for that run and never fails the playlist; the result records how many selected songs
had sound facts and tags, and which sources were unavailable.

Last.fm tags come from `track.getTopTags`, or `artist.getTopTags` when Last.fm has no
tags for the song. Shijhon sends the artist, title and your API key, not your Last.fm
username or listening history. Tags are kept for 30 days and shared by all accounts.
A generation makes at most 120 Last.fm requests, four at a time with short timeouts;
failed requests are not remembered. The tools of [connected agents](agent-access.md) look
tags up the same way for the songs they are asked about, with at most 40 requests and
about five seconds a call.

With [sound analysis](sound-analysis.md) set up and the library-context option on, the
AI may also describe the wanted sound in a short text, with words to emphasize or avoid.
Shijhon then adds up to the requested number of library songs that match that
description and, when there are reference songs, as many that sound like them (never the
reference songs themselves). They are labelled as suggested by sound analysis and go
through the same choice and checks as the AI's own suggestions. Without the
library-context option, no songs found by sound join and no library songs that nobody
named reach the AI.

### Your edits to a refreshed playlist

Before each refresh, Shijhon compares the playlist in Navidrome with what it last
published there. Songs you removed are remembered for that playlist and left out of its
later refreshes, matched by song ID and by artist and title; the AI is told you removed
them, so it can avoid similar choices. Songs you added are shown to the AI as examples of
what you want. Adding a removed song back counts as adding it, and a song that left the
library does not count as removed. Shijhon keeps the latest 200 of each per playlist and
shows the AI the latest 100. Your edits teach only that playlist's refreshes.

The refresh then replaces the songs, including those you added; a comment or
visibility you set stays. Renaming the playlist in Navidrome stops the next refresh: give
it its earlier name, or pause its refreshes to keep the new name. Each refresh's result
shows what it learned and how many suggestions it left out.

The request page of a described playlist and the page of a discovery mix list the songs
Shijhon remembers. **Forget learned edits** (also the agents' `forget_edits` refresh
action) clears them for that playlist: removed songs can be suggested again and added
songs stop guiding the AI. Your playlist in Navidrome does not change, and edits made
since its last refresh are learned again at the next one.

## External agents

Connect to `/shijhon/mcp` with OAuth. Agents discover which features are configured,
which permissions were granted and what needs setup. Library reads, configured
history reads and agent-curated explicit selections work without server AI.
Descriptions and daily/weekly refreshes use the saved server AI connection;
Shijhon runs recurring work after the external agent disconnects.

See [agent setup](agent-access.md) for enabling the endpoint, reverse-proxy routes,
permissions, tools and configuration choices. The optional
[playlist skill](../skills/shijhon-playlists/SKILL.md) adds curation and recovery
guidance.

The [request contract](../skills/shijhon-playlists/references/api.md) describes
payload limits and recovery. Keep the same idempotency key and identical input
after an uncertain response. OAuth token refresh preserves request ownership.
History and library remain independent opt-ins for each task. Publishing can add
library placeholders; audio downloads remain separate.

## Discovery playlists

Open **Playlists** in the dashboard. Signing in already connects your Navidrome account
here; after you disconnect, connect again with your password under **Connection and AI
settings** (open at the top until you connect). A Last.fm username, entered there, is
required for history discovery. By default, Shijhon reuses the Last.fm application API
key configured in Navidrome, which it reads with your Navidrome password when you save
the username. If that key is unavailable, choose **Enter an API key**
and provide your own. Shijhon obtains a reusable
Navidrome authorization token; it does not retain your password. Changing the password
invalidates that authorization, so sign in or reconnect afterward.

Navidrome does not expose the username of its linked Last.fm account, so enter that
username separately; Shijhon does not verify it against the linked account. Reading
public history does not need the personal scrobbling session or a new Last.fm login.
The temporary native login and callback token used while reading Navidrome's API key
are discarded. On later saves, a blank Navidrome password keeps the saved key; enter
the password again to pick up a changed key. Switching key sources requires a new
manual key or a password to read Navidrome's key.

Fill in **New discovery mix** and choose **Create and publish**, then listen and review
the playlist in your music app.
Publication can add whole releases as placeholders or missing tracks to a matched album;
it does not download audio. Track and release details are available on the dashboard
under **Track and release details**.

For dashboard approval first, open **Advanced settings** (which also holds the
unfamiliar share, per-artist limit and the windows that leave out recent music) and
select **Review before publishing**. Creation, rule saves, manual generation and scheduled refreshes then stop
at a preview. Choose **Publish playlist** to continue. Previewing adds no library files;
one waiting preview blocks further generations of that mix until published or discarded.
**Generate preview** always stops at a preview, even if the mix rules were changed in
another tab.

Regular refreshes are optional. Choose every day or every week and check **Enable regular
refreshes after publishing**, or enable them later on the mix page. Leaving it unchecked
keeps a new or paused mix paused. Saving rules shows the current refresh choice so you can
keep or change it; manual generation preserves it. Daily and weekly refreshes run 24 hours
or seven days after the last successful publication; the service must be running. After
downtime, one overdue refresh runs, without replaying every missed interval. Pause also
cancels a pending request to enable refreshes. A scheduled selection still choosing tracks
stops when paused; an already-started publication finishes its verification.

The defaults are 30 tracks, at least 70% unfamiliar music, two tracks per artist,
excluding the last 14 days of listening and the last 28 days of this mix's tracks.
You can change these rules. Saving generates and publishes a new selection, or creates
a preview when **Review before publishing** is selected. Refreshes wait for that publication.
A brief such as “melodic, adventurous music for an evening walk” requires AI curation.

The initial import covers 90 days of Last.fm history by default. Later imports add new
listens and overlap seven days to pick up delayed scrobbles. “Unfamiliar” means absent
from the imported history, not that you have never heard a track. Your library's contents
and placeholder files do not count as listening history. Last.fm similarity supplies
candidates; Shijhon checks recording titles, versions, artists and available duration,
album and recording identifiers. Ambiguous matches are skipped. If too few recordings
meet the rules, the run fails and you can adjust the rules and generate again.

When a local album has a current match, discovery prefers that edition when the
recording's ISRC and details agree. The saved selection records the edition that will actually
be added. Changed or ambiguous matches are skipped. A saved match whose local files
have disappeared does not block adding the catalog release. Album-merge
checks still apply, and preparation errors identify the blocked step.

Publishing reuses accessible library
recordings first. Selected catalog songs use the existing library commit path: adding
one can add its **whole release as placeholders**, or missing tracks in a matched album.
It does not download audio. Playback and download rules still apply. If track
preparation fails, some releases may already have been added; Shijhon does not delete
them automatically to undo a playlist failure.

Each mix owns a native Navidrome playlist. Refreshing keeps that playlist's ID and replaces
its ordered entries in one request. Shijhon checks every native song and reads the
playlist back before reporting success. It refuses to replace a playlist owned by another
user, including when its connection belongs to an admin. Songs you remove from or add to
the playlist teach its next refresh, as for
[described playlists](#your-edits-to-a-refreshed-playlist); with AI curation off, removed
songs are only left out. Renaming the playlist stops the next refresh. Navidrome offers no
conditional replacement API, so a simultaneous edit between the final check and write
can still race with a refresh.

Selections and publication intent are saved before publication. A retry uses the same
selection. An uncertain creation is checked using a unique provisional name and the
owner; Shijhon does not blindly create another playlist. A provisional name can briefly
appear in clients until the verified new playlist is renamed. Failed direct publications
and scheduled runs pause the mix. Choose **Retry publication** to use its frozen selection
where possible. Re-enable refreshes if needed; an explicit pause stays in effect on retry.

## Prepare a playlist's audio

After publication, open **Downloads** to prepare the audio of a native playlist in the
server library. This also works for other native playlists owned by your connected
account. Shijhon can reuse a valid discovery connection, or you can connect separately
with your Navidrome password and choose a playlist. It saves a reusable authorization
token, not the password; reconnect if the password changes.
Preparing audio does not edit the playlist, add releases or choose new recordings.
Owned and already downloaded songs need no add-on fetch. A playlist can have as many
entries as a playlist import (`[playlists] import_max_entries`, 50,000 by default).

The page shows each song's progress and failures, with the first 50 songs listed.
Songs wait for their turn at the add-ons, also while an add-on is rate limited
([details](add-ons.md#downloads-and-playlist-preparation)). Refresh to read current
progress; cancel stops queued and waiting songs and lets transfers under way finish;
completed library files remain.
After a restart, unfinished preparation is paused: review it and choose **Resume**.
A retry uses the saved playlist selection rather than creating a fresh discovery mix. Disconnecting
forgets this preparation connection; it does not remove a discovery connection, history,
native playlists or audio.

An app's standard `download` requests use the same configurable scheduling automatically.
No playlist batch detection is needed. Ordinary streams, including offline saves that
an app sends as streams, continue to use playback rules. Native archive downloads do
not start playlist preparation; prepare the playlist first when it has placeholders.

These actions prepare files on the server. They do not push device copies: use the app's
own offline-save feature afterward. Catalog search only shows tracks, and publishing a
discovery playlist can add silent library placeholders without fetching their audio.
The default limits and shared-account controls are explained in [Add-ons](add-ons.md#downloads-and-playlist-preparation).

## AI connections

AI is optional for history discovery. Without it, Shijhon selects from the resolved
candidates using listening
and similarity evidence. With it, the brief, candidate artists/titles/albums, familiarity
flags and similarity scores are sent to the selected model. Account credentials and the
raw listening timeline are excluded. Shijhon validates the model's candidate IDs, track
count, unfamiliar share and artist limits. An AI failure stops the run; it does not
silently substitute a mix that ignores the brief.

The shared administrator-configured service is the default for new accounts. In
**Connection and AI settings → Your AI preferences**, each account can disable AI, follow the administrator's
default model, or select an additional administrator-approved model. This preference
applies across that account's devices and affects described playlists, discovery
curation and future refreshes. History settings and playlist instructions remain
personal. Independent personal provider connections are planned for later.

Administrators configure **Shared AI service** separately: backend, API URL/key,
default model and up to 50 additional approved model names (one per line). The default
model is always approved. Saving host settings changes the shared service for all
accounts; selecting **No AI** disables it. Approve models whose access and cost the
installation can support. Executables, runner login, network permissions, deadlines
and other installation limits remain administrator-controlled. Personal choices and
agent tools cannot change them or change another account's model.

API connections require a URL and default model; local runners allow a blank model
to use the runner's default. Ordinary users see approved model names without the
endpoint or credentials. Removing a selected model stops future model requests until
the user chooses another approved model or the administrator's default. Shijhon
checks current account credentials, administrator status for private connections,
shared-service availability and the approved model before **every** model request,
including retries and final ordering. A request already sent cannot be recalled;
subsequent requests are checked again. Disabling shared AI stops new shared requests.

Existing private administrator connections, personal history and schedules are
retained. A demoted administrator's private connection cannot run or be edited;
the account can select the shared service instead. Sharing or disabling the host
service does not rewrite those private connections. Ordinary users cannot supply
an endpoint, key, executable or arbitrary model name. The forms show relevant
fields for each backend and work without JavaScript.

- **API:** enter a base URL, model and optional key. The endpoint must support
  `POST <base>/chat/completions` and `response_format: json_schema`. Use a base such as
  `https://your-endpoint.example/v1`. Public endpoints require HTTPS. Local/private
  endpoints require the installation setting `allow_private_api = true`. Redirects are
  refused, and the existing network policy excludes metadata/link-local addresses.
- **Codex CLI:** install and authenticate Codex in the environment where Shijhon runs,
  then enable its command below. Runs use a scratch directory, read-only sandbox,
  ephemeral sessions,
  ignored user configuration/rules, disabled shell/app/plugin/web features and a final
  output schema. These controls do **not** establish a universal tool-free boundary:
  read-only still permits reads. Use a dedicated controlled service account/container
  whose files contain only the intended CLI authentication and required runtime data.
- **Claude Code:** install and authenticate it in that service environment. Runs disable
  built-in tools, MCP servers, customizations, hooks and session persistence using supported CLI
  flags. Managed policy can still force hooks, so use a controlled installation with
  known policy. Shijhon does not manage a model service's account login.

CLI usage and limits follow the authenticated account. API usage uses that API account.
The commands run as Shijhon's operating-system user. With Docker, they must be available
inside its container, with their authentication configured there. Installing or signing
in on the Docker host does not automatically make them available inside the container;
authenticating on another computer also does not connect it to Shijhon. A runner on a
separate host is not implemented. The standard image does not bundle either CLI.
Unsupported CLI flags cause a failed
run instead of being dropped. The integration was checked against Codex CLI 0.162.1 and
Claude Code 2.1.296; model inference still depends on your runtime/account.

The installation configuration controls the executable, never a dashboard form:

```toml
[playlists]
allow_cli = true
codex_command = "codex"
claude_command = "claude"
allow_private_api = false
history_days = 90
history_pages = 100
candidate_limit = 160
run_seconds = 900
ai_seconds = 180
```

Use executable paths or commands available on the service's PATH, without embedded
arguments. The history page limit bounds one complete import (200 listens per page).
Increase it for very busy histories. An incomplete import never advances its saved
cursor. Increase the run deadline if the selected catalog is slow; requests and model
processes also have individual deadlines. Runner processes are terminated on timeout
or shutdown.

See the [Codex non-interactive guide](https://learn.chatgpt.com/docs/non-interactive-mode),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
[Claude managed hooks](https://code.claude.com/docs/en/hooks#disable-or-remove-hooks), and
[Last.fm history API](https://www.last.fm/api/show/user.getRecentTracks) for their contracts.

## Privacy and current scope

Connections, imported history, definitions and run evidence are scoped to the immutable
Navidrome user ID, not shared between dashboard accounts. They are held in Shijhon's
private SQLite state and its backups. The authorization token and API keys are reusable
secrets protected by the state's filesystem permissions; they are not encrypted in the
database. Secret fields are write-only in the dashboard. Blank secret inputs retain their
saved value for the same key source; changing the API URL clears the old API key unless
you enter a new one. Choosing a different AI connection removes the API URL and key.
Disconnecting forgets that account's connections, history, definitions and
runs, while its native playlists and library files remain. It also clears the saved
Downloads authorization, which pauses unfinished preparation until you reconnect on
**Downloads** and choose **Resume**.

Ordinary Navidrome users with access to Shijhon's configured library can manage their
own playlists, imports, requests, preparation jobs, history profile and appearance.
Sign-in refreshes reusable Subsonic authorization without keeping the password; a second
sign-in or history/AI setup is unnecessary for library reads and native playlists.
A deleted/recreated username inherits none of the former user's personal records.

Personal model choices are scoped to immutable Navidrome user IDs and are removed on
disconnect. The administrator's shared AI configuration survives a personal disconnect.
