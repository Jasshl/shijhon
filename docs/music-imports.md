# Import music files

Music imports keep original audio files and their existing tags. Choose files or a
folder, transfer the files privately, review the results, then explicitly approve
the frozen review. Receiving or reviewing a file does not publish it. Other
authorized people can play the physical files and explicitly choose them for their
collections; only the verified importing account receives original-import credit.
New accounts inherit no import credit. Client originals are never changed.

Keep the tab open while uploading. If it closes or the transfer is interrupted,
open the batch under **Your imports** and choose **Resume upload**. Select the same
files or folder, then choose **Continue upload**. Completed files are kept; only an
unfinished file starts over. **Discard upload** ends the batch permanently; start
a new import afterward. It deletes uploaded files that were not added to the library
and frees their reserved space once cleanup finishes. Nothing is published before you
approve its review.

The upload bar measures bytes across the whole batch, including files currently
transferring. The received-file count advances when each complete file is accepted.
Resuming restores saved progress; an unfinished file starts from zero again.

If an import needs a fresh sign-in, use **Sign in again** and return to the same
batch, then resume. You do not need to sign out first. Uploaded files are kept.

Uploads verify current account and library access before receiving a file, once
per second while data arrives, and before accepting the complete file. A failed
check discards the unfinished attempt. Stop and storage checks run for every chunk.

Files in folders the native scanner ignores, including dot-folders, receive visible
destination names. Leading dots are removed; a suffix keeps distinct source folders or
filenames apart when names collide. The review keeps the selected source paths.
If an approved file is not indexed, its row is held with an indexing error and
its published bytes stay retained. Other files continue and receive their verified
import credit; once they finish, **Resume import** retries the unindexed files.

The imported copy preserves the selected filename and safe relative folders under
one server-generated batch folder. An untagged song keeps a useful filename for
Navidrome's title fallback. Missing title, artist or album tags produce warnings;
adding tags is optional. Shijhon never transcodes or rewrites imported bytes or tags, except
in two cases that keep the received file: a copy that carries an album's tags
([below](#music-the-library-already-has)) and an upgrade from the add-ons you allow
([below](#upgrading-your-imported-songs)).

## Formats and verification

The following matrix is tested with unmodified Navidrome **0.64.2**, its default
TagLib scanner and Mutagen **1.48.1** on **Darwin ARM64** with FFmpeg/ffprobe
**9.0.2**, and **Linux ARM64** with Debian FFmpeg/ffprobe **5.1.9**. Results agree
on both tested builds except for TrueAudio, as recorded below. These are concrete
codec/container checks, rather than a claim about every codec that fits a suffix.
The acceptance test generates invented tones, imports tagged and untagged copies,
verifies the native path/title/tags and personal asset receipt, compares the complete
native download and raw stream with the original bytes, and fully decodes both
responses. Private staged files are absent from the native index before approval.

| Codec/container | Tested extensions | Result |
|---|---|---|
| MPEG audio layer III / layer II | `.mp3`, `.mp2` | Scanned and playable; tags retained |
| Native FLAC | `.flac` | Scanned and playable; tags retained |
| AAC in MPEG-4 | `.m4a`, `.m4b` | Scanned and playable; tags retained |
| ALAC in MPEG-4 | `.m4a`, `.alac` | Scanned and playable; tags retained |
| Raw ADTS AAC | `.aac` | Scanned and playable; ID3 retained; native duration is zero |
| Ogg Opus | `.opus`, `.ogg` | Scanned and playable; tags retained |
| Ogg Vorbis | `.ogg` | Scanned and playable; tags retained |
| Ogg FLAC | `.oga` | Scanned and playable; tags retained; native duration is zero |
| Ogg Speex | `.ogg`, `.spx` | Scanned and playable; tags retained |
| PCM integer / floating-point WAVE | `.wav` | Scanned and playable; ID3 retained |
| AIFF / AIFF-C PCM | `.aif`, `.aiff`, `.aifc` | Scanned and playable; ID3 retained |
| WMA v2 in ASF | `.wma` | Scanned and playable; tags retained |
| WavPack | `.wv`, `.wvp` | Scanned and playable; tags retained |
| FLAC in Matroska | `.mka` | Scanned and playable; tags retained |
| Monkey's Audio | `.ape` | Scanned and playable; APEv2 retained |
| Musepack SV8 | `.mpc` | Scanned and playable; APEv2 retained |
| Shorten | `.shn` | Untagged original scanned and playable; native duration is zero |
| DSD64 in DSF | `.dsf` | Scanned and playable; ID3 retained |
| TrueAudio | `.tta` | Linux ARM64: scanned and playable, ID3 retained. Darwin ARM64: valid files not indexed; see below |
| TAK 2.3.3 | `.tak` | **Native scanner rejects valid audio**; refused before upload |
| Raw AC-3 | `.ac3` | Generated files decode, but neither tested stock scanner indexes this suffix; not accepted |
| Different/invalid container under an accepted suffix | e.g. FLAC named `.m4a` | Refused during private audio checks |
| Archives, URL/playlist text and sidecars | e.g. `.zip`, `.m3u`, `.cue` | Not audio import sources |

The TAK evidence uses a complete invented tone encoded and verified by the author's
TAK 2.3.3 encoder and independently decoded by FFmpeg. Mutagen reads both tagged and
untagged files; stock Navidrome logs `invalid file` and skips them. Setting
`Scanner.Extractor=ffmpeg` on the tested stock binary logs that the extractor is
unavailable, falls back to TagLib and still rejects TAK. **No tested alternate
extractor setting enables TAK here.** The importer reports `native_unsupported`,
reserves no upload bytes and publishes no file or personal provenance. This is a
remaining native compatibility limitation.

TrueAudio uses Mutagen's existing read-only parser and ID3 support. Tagged and
untagged `.tta` originals complete the import, native download, raw-stream and
full-decode checks on the tested Linux build. The tested Darwin build does not
index those valid originals. Its acceptance check verifies that approved copies
stay byte-identical, the import pauses with `indexing`, no completion or personal
import credit is recorded, and Stop retains the published copies. TrueAudio cannot
finish importing on that native build.

The picker accepts `.tta` on every Shijhon host: Navidrome can run on another
machine, so Shijhon's operating system does not establish its scanner's capability.
Every completed import still requires exact caller-authenticated native evidence.
Check the native server's compatibility before approving TrueAudio; no universal
TrueAudio scanning claim is made for other native builds or platforms.

Other codec profiles, DRM/encrypted media, additional suffix aliases and other
scanner/platform versions are unverified; the format picker does not promise them.
Shorten has no standard tag case in this matrix. Existing tags in every accepted
file remain byte-identical, including tags the scanner does not expose. Native
zero-duration answers are preserved; Shijhon does not invent replacement metadata.

Header validation uses existing mature Mutagen parsers in a read-only subprocess.
Matroska, Shorten and TrueAudio's channel count (not exposed by Mutagen) use
ffprobe with a fixed local demuxer, capped stdin and only
the `pipe` protocol: no filename, file/network protocol or playlist loader is
available to that probe. Parser input reads are capped at 16 MiB, as is ffprobe's
single-allocation limit. Oversized metadata or an input that cannot be inspected
within those bounds is refused. Available durations must be finite, positive and
at most seven days; sample rates and channels must be valid, including DSD rates.
A missing container duration is allowed rather than excluding playable native
audio, as with Shorten.
The subprocess has a wall deadline, a CPU limit and, on Linux, a 512 MiB address-space
limit. Cancellation and deadlines kill and join its process group, including ffprobe.
These are metadata/header checks, not a full production decode or a guarantee that
every frame plays. The acceptance matrix separately performs complete decode checks.

Native coverage comes from the pinned release's
[audio MIME definitions](https://github.com/navidrome/navidrome/blob/v0.64.2/resources/mime_types.yaml),
[audio eligibility](https://github.com/navidrome/navidrome/blob/v0.64.2/model/file_types.go)
and [scanner adapter](https://github.com/navidrome/navidrome/blob/v0.64.2/adapters/gotaglib/gotaglib.go).
Parser behavior is documented by [Mutagen](https://mutagen.readthedocs.io/en/latest/api/index.html)
and [ffprobe](https://ffmpeg.org/ffprobe.html). The executable evidence is
[test_music_import_formats.py](../tests/acceptance/test_music_import_formats.py);
[fixture generation notes](../tests/harness/fixtures/music-import-formats/README.md)
identify the rare-format encoders, commands and hashes. No recordings are downloaded.

## Storage and recovery

Provision a dedicated writable imports root, normally `_imports`, beside the
disposable managed folder in the native music library. It must already exist and
must not contain symlink components. On first use it must be empty. The service
creates a marker bound to one state installation and library, holds an exclusive
root lock, and uses private `.incoming` staging with `.ndignore` on that same
mount. Never put retained originals inside the disposable managed folder.

The original read-only music library need not move or become writable. Full
backup/restore must include the imports root, its marker and private staged files,
plus the state database containing receipts, provenance and aliases. A different
database cannot adopt a marked root. Restoring only one side requires operator
reconciliation. Permissions must let the native scanner read published files.

Asset identity remains opaque and independent of names. Copies use
`_imports/batch-<job-id>/<selected relative path>`. Relative paths must be valid UTF-8,
within 2,048 bytes and 32 components, with no absolute path, traversal, empty
component, backslash or control character. Exact duplicates, Unicode-equivalent
or case-folded names, and file/folder conflicts are explicitly refused across the
whole manifest, including earlier pages. No collision causes an overwrite or a
silent rename. Paths the pinned scanner always ignores, such as a dot-prefixed
filename or a `.git` folder, are refused before upload with `native_ignored`;
the original selected path remains in the result. Dot-folder settings beyond
these native exclusions remain Navidrome's. The current page is bounded; earlier paths are read in 500-row
cursor pages under the configured maximum job size.

Published files never overwrite existing files, even when their bytes are identical.
Shijhon checks file identity during recovery and pauses the import if it cannot safely
continue. It keeps placed and uncertain originals.

Unfinished transfers restart from byte zero. Fully received files survive retries and
restarts. After a restart, the same account must sign in and resume an approved import.
**Stop import** prevents pending work; already placed originals and completed import
credit remain. Uploaded files that were not published are deleted after Stop, freeing
their space. Completed files are retained privately as well as in the music library.
When an import finishes, received files that were skipped, rejected, excluded or
duplicates in the selection are deleted too. Cleanup runs in saved batches and resumes
after a restart; reservations are released only after the files are gone. Originals
needed for recovery, upgrades or restoration stay retained.
Files linked to music the library already has are not kept, except audio retained for
catalog songs ([details](#music-the-library-already-has)). Retained and unfinished files
count against storage limits until their cleanup is complete.

Music counts as imported only after a scan, checks with your own access and a completed
library refresh confirm the published files. Interrupted imports resume by checking
files already placed before publishing more. An unexpected change to a file or its
native identity pauses the import and keeps the files for inspection.

The [music-import service reference](reference/music-imports.md) describes publication,
recovery and integration with the library index.

## Music the library already has

Importing never shows a song or an album twice in apps. Once every received file is
checked, the review compares the files with the music the importing account can access
(an original, any account's import, also the account's own earlier import, a song
Shijhon added from the catalog) and with each other. It compares with the library as a
refresh made with the importing person's own access shows it at that moment, so music
added to the folder since the last refresh counts too; Navidrome's own song details are
compared (its ISRCs, MusicBrainz recording IDs, explicit or clean status, disc and track
numbers). Two songs are the same recording on the same album:

- **The same album**: the same album artist and album name, letter case, accents,
  spacing and punctuation aside ("NightLetters" is "Night Letters"), and either a song
  in common, or no song in common but no other year and no clearly different cover (a
  second disc imported on its own). An edition's words count: "Night Letters (Deluxe
  Edition)" is another album, and an artist's self-titled albums of different years
  that share no song stay apart. A compilation without an album artist tag is an album
  of "Various Artists", as Navidrome shows it. The same rule brings the files of one
  selection together.
- **The same recording**: a shared MusicBrainz recording ID or ISRC, else the same
  recording title ("(Live)", "(Remix)" and other version markers make other audio;
  "- 2011 Remaster" does not), a shared artist and lengths within 2 seconds. A clean
  edit is never the same as an unmarked or explicit song. Exactly the same bytes as an
  earlier import are always the same.

A file without an album name - an untagged file, or Shorten, which has no tags - never
joins an album.

What happens to each file:

| The file | Outcome |
|---|---|
| Already in the library with its own audio | Not added again. The import links to that song; it counts as the importer's own import (Original imported library and All). Nothing in the library is overwritten, whatever the two qualities. The received file is not kept. |
| A song Shijhon added from the catalog | Linked to that song the same way, and the file's audio goes into it ([below](#your-audio-for-catalog-songs)). The received file is kept. |
| A song the library's same-named album lacks | Added. When its own tags would make Navidrome show a second album of that name (another release date, MusicBrainz album ID, version tag or spelling), the library gets a copy carrying that album's album-level tags, so the song joins the album; the received file stays unchanged and retained. With several same-named albums, the one with the most songs is used. Copies are made in every accepted format but Shorten (which never needs one): with Mutagen for the formats it writes, and for Matroska with ffmpeg as a stream copy, nothing re-encoded. A file that still cannot be fitted into the album (its copy could not be made) is not added; the review and the report say why. |
| Another edition, or another album of the same name | Added as its own album, shared songs included. |
| The same song twice in this selection | The better copy (lossless first, then bitrate) is added. |
| Close to a song of the album, but not clearly the same | Added into the album like a song it lacks (as a copy when needed), marked "may already be in the library". |

The review lists every such file with its reason and both qualities. **Not the same
song** keeps a file from being linked: it is added like a song its album lacks (into
that album, as a copy when needed); either choice gives the review a new revision. A
file that is not added because it would make a second album of its name has no such
choice. Approval links the songs already in the library before it publishes the rest.
Right before a link is made, the importing person's own read of that song must still
show the same recording on the same album; a song that left the library or became
another recording after the review gets the file published instead, as any file is.
A file that is exactly the bytes of an earlier import is linked by that import's
record instead of its tags (Navidrome may name an untagged song by its file name):
the earlier import must still have published exactly those bytes - its file must still
be the one Shijhon placed or, when another file of that size was put in its place, hold
exactly those bytes (Shijhon reads it to compare) - and your own read must show the song
at that file. Other bytes of that size in its place, or something there that is not a
file, stop the import to check storage; a song shown at another file stops it to check
indexing. Neither publishes the file.

Before each publication batch, every file of it still to be published - also a copy
carrying an album's tags - is compared with the library again, as it is then. A song
something else added meanwhile (another import, a file put into the folder) is linked;
any other change of what the review decided (a song that would now make a second album
of its name, a copy whose album changed) takes the import back to review: its rows say
the library changed, and it needs a new approval. Files already placed, published or
linked stay as they are.

After the import, show **Music the library already had** in the file list to see what
was not added again and why. A song Navidrome still puts into a second album despite the prediction is
marked on its row. Several imports of one person linked to the same song keep it among
that person's own imports while any of them is active. A catalog song an active import
is linked to is never taken out by the unused-release cleanup.

### Below your minimum quality

Each person sets a minimum audio quality (the dashboard's **Audio quality** page): by
default compressed audio of at least 192 kbps, lossless audio always, and at least
44.1 kHz. The review applies the importing person's own minimum, album by album, so an
album never ends up half your files and half catalog songs. When a file of an album
that would be added, or would give its audio to a song Shijhon added from the catalog,
is below that minimum:

| The album | Outcome |
|---|---|
| The catalog has it (every such song on one release) | Your files are not added. On approval the catalog's release is added to the library - into the library's album of the same name when there is one - and your files are linked to its songs: they are catalog songs, played from the add-ons when they have the audio, and count as your imports. The received files are not kept. |
| The catalog does not have it, or no catalog is set up | Added as it would have been, marked "below your minimum" in the review and the report. |
| The catalog could not be reached | Waits in the review: **Check again**, or **Add my files anyway**. Approved like that, it is not added. |

A song already in the library stays linked whatever the quality of either copy, and a
song Shijhon added from the catalog keeps its own audio: a file below the minimum is
linked to it without giving its audio. Clean edits stay clean: the catalog's release is
looked for as the files' own kind of a clean/explicit pair, and one that has only the
other recording does not count. A quality the file does not state (a raw AAC
file's bitrate, say) is never below a minimum. **Add my files anyway** adds the album's
files as they would have been; **Use the catalog's version** takes that back. Either
choice, and **Check again**, gives the review a new revision.

At publication the catalog's release must be exactly the reviewed one. When the library
refuses it (it would join an album that cannot take it, or the catalog no longer has or
changed it), those files are not added and their rows say why; any other failure pauses
the import, and **Resume import** tries again.

### Your audio for catalog songs

A catalog song Shijhon added (it played from the add-ons, or from audio downloaded for a
while) plays the imported file's audio once the import is done. It stays the same song:
its ID, favorites, plays and playlist entries are kept. Apps get a copy of the audio
carrying the catalog song's tags; the received file stays unchanged and retained. That
audio belongs to the import: it does not expire like downloaded audio, the cleanup
counts it as a use, and the copy counts against the importer's storage (reserved before
it is made, released once it is gone). Nothing that is already there is replaced: a
catalog song playing another import's audio keeps it. A file below the importer's
minimum quality ([configuration.md](configuration.md#audio-quality)), or in a format the
swap cannot tag (only FLAC, Ogg Vorbis and Opus, MP3 and MPEG-4 can), stays a plain link
and the song keeps playing from the add-ons. A step that has to wait (the song's
downloaded audio in use, say) is tried again later, up to ten times. Each step starts
from what the song plays: audio of the import that a stop left in place before it was
recorded is recorded as the import's (and stays reserved), also when the importer's
minimum changed meanwhile, and a reservation goes only once the song is known not to
play that audio. The import's report says which happened. Removing the import puts such a catalog song back to playing from
the add-ons - the removal says how many still have to go back - and restoring the import
puts its audio back. Installation rechecks the importing account's current access at
the swap; an administrator's restoration uses that administrator's current access.

### Upgrading your imported songs

When you play one of your own imported songs whose file is below your minimum quality,
it plays from the add-ons when they have it well enough. With **Upgrade my imported
songs from the add-ons** on (the Audio quality page; on unless you switch it off), the
add-ons' audio then becomes the song's file, fetched in the background: in whatever
format it comes (a FLAC stays FLAC, nothing is converted) - any container whose tags
Shijhon can write and Navidrome reads, but not raw ADTS AAC, whose length Navidrome does
not read; a fragmented MP4 or Ogg FLAC, whose length it does not read either, comes as
the same audio in an ordinary M4A (FLAC, MP3: their own files) or a FLAC - carrying your
file's whole metadata so that the song keeps its ID,
favorites, plays and playlist entries: tag by tag when both formats keep the same kind of
tags (ID3, Vorbis comments, MPEG-4 atoms, APEv2: comments with their descriptions and
languages, ratings, applications' private data, any other tag under its own name), else
every tag through that format's own names (lyrics, BPM, track and disc totals, sort
names, original artist and album, identifiers, ReplayGain, comments by their
descriptions), and the picture. What
describes your file's own encoding or a player's state of it (its encoder and settings,
length, gapless and volume-normalization data, a streaming timestamp) is not carried: it would be wrong for the new audio. Only songs
you imported yourself: never a file someone else imported, also not when your import was
linked to it (it is their file), never a file in the music folder, never a song at or
above your minimum quality, never while the import is being removed. A song your later
import was linked to because your earlier import already had it is your earlier import's,
and is upgraded as such. Files in Matroska or Shorten are not upgraded (no tags can be
carried out of them). Your original file stays retained privately; the upgraded file
counts against your storage as well. The import's report shows the upgrade with both
qualities. Removing the import takes the upgraded file out; restoring it puts the
upgraded file back.

Further upgrades keep the original receipt and all earlier audio retained and counted
against storage.

The swap stays in step when stopped at any point: its storage is reserved and a recorded
intent comes first, before the upgraded file is made; right before the original's name
changes, your current access and Navidrome's settings that keep a missing file's identity
are checked; the upgraded file takes the original's place (same folder and name, its own
suffix), and only when a scan and your own native read show the same song ID at the new
path - with the song's details as they were - does the import record it. Anything else
puts the original back, and Navidrome must show it again (scan and your own exact read)
before the intent goes; such a roll back is finished later when it cannot be at once.

## Bounds

Default storage and upload limits, configurable by the administrator:

| Limit | Default |
|---|---:|
| One file | 2 GiB |
| One job | 100,000 files / 512 GiB |
| Active jobs per owner | 4 |
| Reserved bytes per owner | 512 GiB |
| Reserved bytes globally | 1 TiB |
| Unreserved free space | 5 GiB |
| Concurrent raw uploads | 2 |
| Upload wall time | 1,800 seconds |
| One header validation | 15 seconds |
| Manifest page | 500 entries |
| Status item page | 500 entries |

Declarations reserve capacity before uploads. Actual free space and all outstanding
reservations gate admission; actual free space is checked while streaming. Stop
releases unreceived reservations; unpublished uploaded files release theirs after
deletion. Retained published originals and recovery files continue counting against
the byte quotas. These are conservative retained-storage quotas, not bandwidth
quotas. Increase them deliberately as storage grows. Body sizes and optional client
SHA-256 assertions are independently checked against server-counted/hash bytes.

Jobs are bound to the library ID, the imports root's verified identity and the imports
folder name. Changing limits does not invalidate existing jobs. A local
mount path can change if it still opens that same verified root; copying or replacing
the root requires reconciliation.

## Remove an import and restore it

**Stop import** only stops unfinished work. It keeps published files. To correct an
import, open it under **Your imports** and choose **Review removal**. Review lists every published
file, which files will leave the shared library, and which stay
available because they have recorded use. Favorites, plays, ratings, playlists,
queues, bookmarks, shares and other people's saved libraries all count. Private
playlist names and other people's personal records are not shown.

Confirming the reviewed removal makes exactly the approved files unavailable to all
accounts. Files already copied to a device stay there. Shijhon checks current access,
file identity, native retention policy and fresh shared-use evidence again. Changes
invalidate the review or restore an interrupted removal. Work is bounded and saved
between batches; an interrupted operation offers **Retry** and **Restore**.

Songs the import only linked, because the library already had them, are not its files:
removal takes them out of the importer's own imports and Restore brings them back;
nobody else's music changes. Another account's import linked to a file counts as use
of that file (credited to another person) when its own importer removes it.

Removal retains the original bytes privately in the imports directory. It does not
free disk space. **Restore this import** puts the same bytes and tags back at the
same paths without overwriting anything, then verifies the native IDs. Personal
provenance, history, selections, favorites and playlist references are kept throughout;
removed entries are unavailable until restoration. Native missing-file retention must
remain `Scanner.PurgeMissing = "never"`.

Published originals that Navidrome never indexed are included in removal review,
with their paths and an explicit unindexed label. They receive no invented song ID
or personal import credit. Removal requires a fresh caller-authorized native check
including missing entries and a fresh usage snapshot with no record at that exact
path. A retained missing entry is an existing native identity, not an unindexed file.
New indexing after review invalidates removal approval. Restore puts verified
retained bytes back without overwriting files or creating import credit. If a
previously unindexed original acquires a native ID through a delayed scan, Restore
verifies its exact native path/size, stable asset aliases and fresh shared-use
source. It finishes with the new ID and a visible indexing warning. Choose
**Review removal** again to check that identity and its current use before another
removal. The original import receipt and frozen review ID stay unchanged. Known
native IDs remain strict; conflicting identity, access or source evidence pauses
the operation with bytes retained, and **Retry** rechecks the current facts.

An administrator can open **Manage all imports**, review another
account's batch, explicitly include music already in use, stop unfinished importing,
or restore a batch after its original account lost access. These actions use the
administrator's own current authorization and preserve the original import credit.
Ordinary accounts can manage only their own batches. Removing used shared music is
an administrator action because it affects other people's saved music.

Removal requires the dedicated imports root to be available and imports enabled,
plus the same fresh usage export or read-only usage source used by cleanup. An
unavailable or changed source refuses removal. Retained originals have no automatic
expiry or permanent-delete action. Include them in backups; a whole backup restore
is emergency recovery and can rewind newer activity from every account.
