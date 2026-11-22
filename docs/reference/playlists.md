# Playlists in detail

The exact rules behind [playlists](../playlists.md) and
[playlist imports](../playlist-imports.md): how described playlists and discovery mixes
choose and publish songs, how imports read, match and write their entries, and what
Shijhon keeps.

## Described playlists

Automatic naming uses the existing final ordering call. The name and selection are
saved together before preview or publication, so retries, restarts and refreshes keep
the first title.

Listening history needs a configured profile and application key. The library-context
option shares a bounded sample of up to 100 artist/title/album entries; library
membership does not establish that you listened to a track. Exact recording lookup still
checks your accessible library to reuse music; that lookup does not feed unrelated
library contents into AI.

Shijhon asks for bounded artist/title suggestions, looks them up in your library and
the configured catalog, then asks AI to choose and order only resolved candidates.
An explicit album or recording-version qualifier must match, including during
publication and recovery. Version text must be evidenced in the resolved title;
a version that cannot be verified fails rather than accepting a different remaster.
Ambiguous or unavailable recordings are skipped. There are at most three suggestion
rounds and at most the configured candidate limit (capped at 300) attempted suggestions.
The final selection must have the exact count, no repeated recording and the requested
artist limit. AI also checks the free-text instructions; those semantic judgments depend
on the model. An invalid or insufficient selection fails clearly instead of silently
ignoring the instructions. Track selection and release effects are frozen before native
publication.

The final call also returns a one-sentence goal and, for each song, a short reason and
whether it meets, partly meets or misses the instructions. When some songs do not fully
meet them and unused candidates remain, Shijhon asks once more for a revised selection,
with those songs flagged. The revision is used when it passes the same checks; otherwise
the first selection stays. The ratings are shown with the result and never reject songs
by themselves.

The request's page also shows, for each reference, how many suggested songs were left out
because they are already in a chosen reference, and whether the selection was revised or
checked once more. With sound facts, it shows how many of the songs had sound facts or
Last.fm tags. A request with explicit tracks, and songs without facts, show only what
they have.

Requests are one-off by default. Their instructions and run state remain in private
state for status and recovery, but no schedule is enabled. Daily or weekly refreshes
require **Save these instructions for reuse**, an explicit cadence and usable saved AI;
they start after verified publication. Retry reuses a frozen selection and reconciles an
uncertain native write rather than choosing different music or blindly creating a second
playlist. A selection interrupted before it was frozen can be regenerated.

## References

Requests accept up to three Navidrome playlist IDs (`reference_playlists`) and up to 50
song IDs (`reference_songs`) that your account can read. Without them, Shijhon looks for
your own playlists whose names the instructions mention: whole words, ignoring case,
longer names first, at most three. Names shorter than four letters and names of only
general words, such as "Chill Mix", are ignored; a one-word name counts only in quotes or
next to "playlist" or "mix" (`"Driftwood"`, `my Driftwood playlist`). Of several
playlists with the same name, the most recently changed one is used, and a refreshed
playlist is never its own reference. The dashboard's **Compare with your playlists**
lists the first 500 of your playlists.

References are read through your Navidrome account each time the playlist is generated,
so refreshes follow their current songs. An explicit reference that cannot be read fails
the run with its ID; a playlist found by name that cannot be read is skipped. Shijhon
keeps up to 200 reference songs, your listed songs first, and shows the AI an even
sample of up to 100 with their facts and a short profile: typical energy, tempo and mood
percentiles, common tags and genres, and the range of years. Songs of a playlist found by
name are not removed automatically: the AI is asked to avoid repeating them unless the
instructions ask for them. References need a description; explicit tracks cannot use
them.

Agents need the library permission for references. A request with chosen references is
refused without it. For a request an agent created, playlist names in the instructions
are looked up only if the submitting token included the library permission and the
agent's connection still grants it, checked again at every generation and scheduled
refresh: without it, no playlist is listed or read and the result shows no reference. If
the permission is removed while a playlist found by name is in use, that generation
stops. Your own requests from the dashboard always look up names.

## Facts and Last.fm tags

The AI is told that facts are approximate hints, that many songs have none and that its
own knowledge of the music comes first; percentiles compare songs within this library.
A missing fact never excludes a song. An unavailable or slow source means fewer facts
for that run and never fails the playlist; the result records how many selected songs
had sound facts and tags, and which sources were unavailable.

Last.fm tags come from `track.getTopTags`, or `artist.getTopTags` when Last.fm has no
tags for the song. Shijhon sends the artist, title and your API key, not your Last.fm
username or listening history. Tags are kept for 30 days and shared by all accounts.
A generation makes at most 120 Last.fm requests, four at a time with short timeouts;
failed requests are not remembered. The tools of [connected agents](../agent-access.md)
look tags up the same way for the songs they are asked about, with at most 40 requests
and about five seconds a call.

With [sound analysis](../sound-analysis.md) set up and the library-context option on,
the AI may also describe the wanted sound in a short text, with words to emphasize or
avoid. Shijhon then adds up to the requested number of library songs that match that
description and, when there are reference songs, as many that sound like them (never the
reference songs themselves). They go through the same choice and checks as the AI's own
suggestions.

## Learned edits

Before each refresh, Shijhon compares the playlist in Navidrome with what it last
published there. Removed songs are matched by song ID and by artist and title. Adding a
removed song back counts as adding it, and a song that left the library does not count
as removed. Shijhon keeps the latest 200 of each per playlist and shows the AI the latest
100. The refresh then replaces the songs, including those you added; a comment or
visibility you set stays. With AI curation off, removed songs of a discovery mix are only
left out.

## Discovery mixes

### The Last.fm connection

Shijhon obtains a reusable Navidrome authorization token; it does not retain your
password. Changing the password invalidates that authorization, so sign in or reconnect
afterward. By default Shijhon reuses the Last.fm application API key configured in
Navidrome, which it reads with your Navidrome password when you save the username.

Navidrome does not expose the username of its linked Last.fm account, so the username is
entered separately; Shijhon does not verify it against the linked account. Reading
public history does not need the personal scrobbling session or a new Last.fm login.
The temporary native login and callback token used while reading Navidrome's API key
are discarded. On later saves, a blank Navidrome password keeps the saved key; enter
the password again to pick up a changed key. Switching key sources requires a new
manual key or a password to read Navidrome's key.

For a [radio from a catalog item](../how-it-works.md#radio-from-catalog-items), Shijhon
sends Last.fm an artist's name, or an artist and a title, with the key. It sends neither
your Last.fm username nor anything about what you listened to.

### Previews and refreshes

With **Review before publishing**, creation, rule saves, manual generation and scheduled
refreshes stop at a preview. Previewing adds no library files; one waiting preview blocks
further generations of that mix until published or discarded. **Generate preview** always
stops at a preview, even if the mix rules were changed in another tab.

Leaving **Enable regular refreshes after publishing** unchecked keeps a new or paused mix
paused. Saving rules shows the current refresh choice, to keep or change; manual
generation preserves it. Daily and weekly refreshes run 24 hours or seven days after the
last successful publication; the service must be running. After downtime, one overdue
refresh runs, without replaying every missed interval. Pause also cancels a pending
request to enable refreshes. A scheduled selection still choosing tracks stops when
paused; an already-started publication finishes its verification. Saving rules generates
and publishes a new selection, or creates a preview; refreshes wait for that publication.

### Candidates

Later history imports overlap seven days to pick up delayed scrobbles. Your library's
contents and placeholder files do not count as listening history. Candidates are the
songs Last.fm and ListenBrainz list as similar to the twelve songs you played most,
recently weighing more. A song both list scores for both and comes first. Shijhon sends
ListenBrainz the artists and titles of those twelve songs, without your name or account.
ListenBrainz is asked beside Last.fm and waited for at most ten seconds after Last.fm's
last answer, and when it fails the mix is made from Last.fm's songs. Shijhon checks
recording titles, versions, artists and available duration, album and recording
identifiers. Ambiguous matches are skipped.

When a local album has a current match, discovery prefers that edition when the
recording's ISRC and details agree. The saved selection records the edition that will
actually be added. Changed or ambiguous matches are skipped. A saved match whose local
files have disappeared does not block adding the catalog release. Album-merge checks
still apply, and preparation errors identify the blocked step.

### Publishing

Publishing reuses accessible library recordings first. Selected catalog songs use the
existing library commit path: adding one can add its whole release as placeholders, or
missing tracks in a matched album. It does not download audio. Playback and download
rules still apply. If track preparation fails, some releases may already have been
added; Shijhon does not delete them automatically to undo a playlist failure.

Each mix owns a native Navidrome playlist. Refreshing keeps that playlist's ID and
replaces its ordered entries in one request. Shijhon checks every native song and reads
the playlist back before reporting success. It refuses to replace a playlist owned by
another user, including when its connection belongs to an admin. Navidrome offers no
conditional replacement API, so a simultaneous edit between the final check and write
can still race with a refresh.

Selections and publication intent are saved before publication. A retry uses the same
selection. An uncertain creation is checked using a unique provisional name and the
owner; Shijhon does not blindly create another playlist. A provisional name can briefly
appear in apps until the verified new playlist is renamed. Failed direct publications
and scheduled runs pause the mix. **Retry publication** uses its frozen selection where
possible; an explicit pause stays in effect on retry.

## Prepared playlist audio

**Downloads** can reuse a valid discovery connection, or connect separately with a
Navidrome password and a chosen playlist. It saves a reusable authorization token, not
the password. A retry uses the saved playlist selection rather than creating a fresh
discovery mix. Disconnecting forgets this preparation connection; it does not remove a
discovery connection, history, native playlists or audio.

## Server AI

With server AI, the brief, candidate artists/titles/albums, familiarity flags and
similarity scores are sent to the selected model. Account credentials and the raw
listening timeline are excluded. Shijhon validates the model's candidate IDs, track
count, unfamiliar share and artist limits.

Executables, runner login, network permissions, deadlines and other installation limits
remain administrator-controlled. Personal choices and agent tools cannot change them or
change another account's model. Ordinary users see approved model names without the
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
fields for each backend and work without JavaScript. Redirects are refused, and metadata
and link-local addresses stay blocked.

### Limits

The installation configuration also sets the limits:

```toml
[playlists]
history_days = 90
history_pages = 100
candidate_limit = 160
run_seconds = 900
ai_seconds = 180
```

The history page limit bounds one complete import (200 listens per page).
Increase it for very busy histories. An incomplete import never advances its saved
cursor. Increase the run deadline if the selected catalog is slow; requests and model
calls also have individual deadlines.

See the [Last.fm history API](https://www.last.fm/api/show/user.getRecentTracks) for its
contract.

## Privacy and storage

Connections, imported history, definitions and run evidence are scoped to the immutable
Navidrome user ID, not shared between dashboard accounts. They are held in Shijhon's
private SQLite state and its backups. The authorization token and API keys are reusable
secrets protected by the state's filesystem permissions; they are not encrypted in the
database. Secret fields are write-only in the dashboard. Blank secret inputs retain their
saved value for the same key source; changing the API URL clears the old API key unless
you enter a new one. Choosing a different AI connection removes the API URL and key.

Ordinary Navidrome users with access to Shijhon's configured library can manage their
own playlists, imports, requests, preparation jobs, history profile and appearance.
Sign-in refreshes reusable Subsonic authorization without keeping the password; a second
sign-in or history/AI setup is unnecessary for library reads and native playlists.
A deleted/recreated username inherits none of the former user's personal records.
Personal model choices are scoped to immutable Navidrome user IDs and are removed on
disconnect. The administrator's shared AI configuration survives a personal disconnect.

## Playlist imports

### Reading the input

Automatic detection keeps artist/title pairs as text when names contain slashes,
backslashes or dotted song titles. A tab-separated title such as `Name` or `Path`
alone does not identify a table header. Recognizable artist/title headers and
leading native-ID/path headers identify tables; playlist/export file extensions
and an explicit format choice take precedence. For an ambiguous unlabeled
two-column export with an unrelated first header, choose **CSV** or upload it
with its `.csv` or `.tsv` filename.

These are finite track-list imports. Streaming manifests, dynamic smart-playlist
rules, indefinite repeats and timed, parallel or conditional playback instructions
are not converted into song lists. ASX start times, durations, previews and markers
are explicitly refused on entries and their alternate references. ASX `ENTRYREF`
documents must be exported as their ordered tracks; they are never treated as songs
or opened recursively. Malformed music rows reject the submission with their entry
number instead of disappearing from the imported list; only an unreadable ISRC or
duration hint is ignored for its entry. This is not a claim to read every playlist
dialect or every service's export.

M3U per-entry start-time, stop-time and run-time instructions are explicitly refused
before a review is saved. Ordinary comments, recording metadata and `EXTINF` duration
hints remain accepted; a duration hint by itself does not request a clipped recording.

A UTF-16 byte-order mark is detected. XML is parsed with the existing hardened XML
library; document types, external entities and entity expansion are refused.

### Columns, ISRC and duration

Only ambiguity among columns supplying an imported field needs mapping. Distinct
unused headers that normalize alike, non-Latin headers and duplicated unused columns
are ignored. Exact mapping can distinguish `Track Name` from `Track_Name`; repeated
identical headers for a selected field must be renamed in the source export.

`ISRC` accepts the compact or hyphenated code in either letter case. Fractional seconds
are allowed in durations. Units never depend on how large the number is. Blank or zero
duration means unknown. A code or length Shijhon cannot read, such as `n/a`, `3m45s` or a
date, is ignored for that entry, which is then matched by its names alone, and the other
entries import as usual. The same holds when the two duration columns of an entry
disagree.

XSPF and JSPF durations are milliseconds. Their recording identifiers can use
`isrc:ZZSHJ2600001` or `urn:isrc:ZZSHJ2600001`; native song identifiers use `navidrome:`
or `subsonic:`. Several ISRC identifiers are kept as alternatives for one track.
M3U `EXTINF` and PLS `LengthN` durations are seconds; `-1` means unknown. In these
formats too, a duration or ISRC identifier that cannot be read is ignored for its
entry.

For matching by names, a conflicting known ISRC or a known duration differing by more
than five seconds rules out a candidate. Among otherwise eligible candidates, the
requested ISRC is preferred over an unknown one. Unknown metadata can still match by
names, and album and version requirements still apply. These hints stay saved for
review, retries and missing-entry rechecks. Explicit native IDs and file paths keep
their existing authority; choosing a native ID manually remains an explicit override.

### Paths and locations

Local paths are matched against Navidrome's actual file metadata as the signed-in
caller. Windows separators, file URIs and Unicode normalization are handled lexically.
Matching prefers the longest shared folder suffix; unique complete relative paths can
match across different machine roots. Native IDs are checked as the caller and against
this installation's exact library before use.

XML locations inherit nested `xml:base` values. ASX `BASE` scopes apply to the document
or individual entry, including entries inside repeat groups; an entry override does
not change subsequent entries. Bases and relative references are resolved as text
with no URL requests or filesystem access. Remote bases remain remote, including
media URI schemes, so their relative filenames cannot select unrelated local songs.
See the [ASX BASE reference](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/wmp/base-element),
[ASX timing reference](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/wmp/starttime-element)
and [XML Base specification](https://www.w3.org/TR/xmlbase/).

Path matching reuses the original native sign-in authorization held temporarily in
memory. No native bearer is stored in import evidence. Previously matched recordings
remain frozen when unresolved entries are retried.

### Add-on catalogs

The library and the catalogs use the same matching rules. An add-on that is the
configured catalog is not asked twice (identified by its add-on name). Add-ons are read
from the current Add-ons page for every match. These catalogs keep their add-ons' keys
even when an add-on declares a stable identity through the full-catalog extension. The
configured catalog does not change. Add-on catalog requests count against the add-on's
[limits](../add-ons.md#limits) and wait behind requests from apps. An add-on's own time
limit (`budget_seconds`, for a slow add-on) also applies to its catalog requests here.

An add-on that is busy (HTTP 429) or does not answer in time has not said it lacks the
song. It is skipped for the rest of that matching attempt. Imports and other playlist
work take turns, so a slow add-on does not hold up other people's playlists. Before
publishing, Shijhon checks the album again with the add-on. Its songs play from that
add-on by the add-on's own track IDs.

### Progress and batches

Administrators can change `[playlists] import_max_entries` (1,000–1,000,000) and
`import_max_bytes` (1,024–67,108,864 bytes) in installation configuration. These are
installation resource budgets. Curated mixes retain their separate 100-track limit.

Matching and publication run in saved batches. Native path pages contain at most 500
songs, matching visits at most 100 input rows per worker turn (fewer when slow catalogs
use half of `[playlists] run_seconds`), and native publication adds at most 200 entries
per mutation. Preparation and final recording verification also use bounded batches.
Repeated inputs can reuse saved matching results without losing their separate
positions. A Recheck counts only the entries it matches again. Progress is checkpointed
at matching-batch boundaries and on ordinary matching failures, with fresh authorization
and Stop checks before each new recording match and before checkpoints. A process
termination or worker timeout can require rematching the unfinished batch of at most 100
entries; saved matches and explicit recording choices stay frozen.

Original rows and exclusions remain in the saved evidence. A stale review cannot approve
recordings changed by later matching or recording choices. Resubmitting the same
original form reopens the same import; changed input requires a new form.

### Rechecks and playlist updates

Entries still in the playlist keep their native song IDs, so replacing a catalog does
not require finding them again. Each newly found song goes right after the nearest
earlier entry of the import that is still in the playlist; without one, before the
nearest later one; if no imported entry is left, at the end. An imported song you moved
still counts as its entry. Repeated entries stay separate. If you already added a found
library song yourself, that copy is used instead of adding a second one.

Up to 5,000 entries, the update replaces the playlist's songs in one native write
(`createPlaylist` with the playlist's ID), which Navidrome applies as one change and
which keeps the playlist's ID, name, comment, visibility and owner. If its answer is
lost, the playlist's songs are read back: the reviewed result means it was applied; the
songs as reviewed mean it was not, and **Retry** sends the same replacement again;
anything else stops with nothing more written. Once the update has been sent, a new
name, comment or visibility you set meanwhile is kept.

Larger playlists are updated in steps, because Navidrome refuses requests with 10,000
or more parameters. The unchanged beginning stays in place; the rest of the reviewed
playlist is appended in batches of at most 200 entries, then the old copies are removed
by position. Until this finishes the playlist can contain extra copies, never fewer
songs. A lost append or removal answer is reconciled against its exact intended
result and never blindly repeated. A change to the playlist's songs between steps
stops the update; a new name, comment or visibility is yours, stays, and the update
goes on. **Resume update** checks the playlist before it continues. Recheck waits until
the update is finished, so the temporary copies are not taken for your own songs.
After **Keep playlist as it is**, everything then in the playlist counts as yours.

### Writing to Navidrome

Publication uses native song IDs, owner-authenticated native playlist calls and the
existing verified catalog preparation path. A preparation failure can leave releases
added.

Each native create/append/replace intent is durable before the request. The entire
saved playlist is read back after every write and its exact order, repeats and
metadata are checked. Native edits stop further publication. A lost append response
is reconciled by observing its exact intended result; the append is never blindly
repeated. If its result remains unconfirmed, retry later or stop the import. A lost
create response cannot create a second playlist. Final success requires complete
ordered native readback and bounded verification of the frozen recording identities;
for a Recheck update, the imported entries are verified and your own entries must be
exactly as reviewed, in their order. Navidrome has no conditional write API, so the
final check/write race remains.

A native mutation already being sent when you choose **Stop** can finish. A stopped
import is not resumed by later worker ticks. Failed or uncertain publication retries
retain the review, exclusions, native IDs and pending intent; they do not rematch or
recurate. When a Recheck after a stopped update matches a library song that update
already wrote to the playlist, it uses that copy instead of adding a second one.

Original text, normalized rows, path matches, recording choices and publication review
are private state scoped to the immutable Navidrome owner ID and installation library.
They survive restarts and are removed with that account's saved playlist connection;
native playlists and library files remain.
