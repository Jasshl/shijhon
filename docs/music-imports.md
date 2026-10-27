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
a new import afterward. Nothing is published before you approve its review.

The upload bar measures bytes across the whole batch, including files currently
transferring. The received-file count advances when each complete file is accepted.
Resuming restores saved progress; an unfinished file starts from zero again.

If an import needs a fresh sign-in, use **Sign in again** and return to the same
batch, then resume. You do not need to sign out first. Uploaded files are kept.

Uploads verify current account and library access before receiving a file, once
per second while data arrives, and before accepting the complete file. A failed
check discards the unfinished attempt. Stop and storage checks run for every chunk.

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

All file operations are descriptor-relative and refuse symlink components. New
folders are made scanner-readable even under a restrictive umask, fsynced as
they are created, and directory bindings are checked before
and after use. A durable
placement intent commits before an atomic no-replace link makes a file visible;
file and directory fsyncs protect the transition. No existing destination is
overwritten or adopted, even when its bytes are identical. Recovery recognizes
its own publication by the recorded staging inode and complete digest/size.

Unfinished transfers restart from byte zero. Fully received files survive retries
and restarts. A restart pauses approved publication until the same immutable
owner explicitly resumes with current access. A recreated account cannot resume
another account's job. Stop prevents pending publication; already placed originals
and completed provenance remain. This service has no automatic deletion of placed
or uncertain originals. Completed private staging names remain hard links to the
same data. Music the library already has is linked rather than published again
([below](#music-the-library-already-has)); a linked file's received bytes are deleted,
and its reservation goes only once that deletion is synced - also when the import was
stopped first. A copy that joins an album is recorded, with its reservation, before its
bytes are written. One a new review gives up (reviewing again, excluding its file, a
changed match, the library changing after approval), or one a stop left unfinished,
keeps that record and reservation until its private bytes are deleted and that deletion
is synced: at the next start, before the import is checked again, or when the worker is
idle.
An active upload keeps its admitted reservation through Stop until its private
unfinished attempt is unlinked and that deletion is fsynced. Restart durably
discards every interrupted unfinished attempt before marking it retryable, so a
later Stop cannot release capacity while leaving its partial bytes. Restart also
completes interrupted cleanup of already cancelled attempts. A file's
received-byte/digest commit checks the live uploading job, item and admitted
attempt in the same transaction. If receipt wins, Stop retains the full received
reservation; if Stop wins, completion refuses and discards only that unfinished
attempt. A late cancellation cannot discard an already committed received file.

Completion requires a coordinated native scan, exact caller-native path and size
verification, completed structural refresh, a second exact lookup and local byte
verification. The receipt and owner provenance commit in the same transaction.
Publication selects a deterministic batch of at most 500 approved files and 1 GiB
of original bytes per worker tick. A file above that byte budget is processed
alone, up to the configured per-file limit (2 GiB by default). Already placed or
uncertain files take priority on resume. Each batch shares one scan and one
completed structural refresh. Every file has exact caller-native evidence before
refresh and matching evidence repeated after refresh, followed by retained-byte,
root, approved-revision and current-owner checks before its atomic receipt.
After interruption or partial credit, remaining placed files reconcile together
in a fresh bounded batch; completed receipts are not published again.
Unexpected native identity reuse pauses the import and retains files. A service
account's metadata lookup cannot substitute for the importing caller's lookup.
The read-only resolver also checks every native alias already bound to an import
asset, including IDs learned during a later restore and pending placing/placed
assets whose receipt IDs are not filled yet. Different path/size/root or missing
retained inode evidence stops any caller's structural refresh before an old alias
could attach an existing owner's provenance to a different file. Completed receipts
remain immutable when exact-asset restoration learns another native alias.

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
puts its audio back.

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

The swap stays in step when stopped at any point: its storage is reserved and a recorded
intent comes first, before the upgraded file is made; right before the original's name
changes, your current access and Navidrome's settings that keep a missing file's identity
are checked; the upgraded file takes the original's place (same folder and name, its own
suffix), and only when a scan and your own native read show the same song ID at the new
path - with the song's details as they were - does the import record it. Anything else
puts the original back, and Navidrome must show it again (scan and your own exact read)
before the intent goes; such a roll back is finished later when it cannot be at once.

## Service integration

The public Python boundary is `shijhon.music_imports`: `MusicImports`,
`ManifestEntry(client_key, path, size, sha256="")`, `NativeFile(song_id, path, size)`,
`ImportLimits` and safe `ImportError(code, message)`.

Construct `MusicImports(store, access, *, library_id, library_path,
imports_folder="_imports", managed_folder="_shijhon", limits=ImportLimits(),
scan, native_lookup, refresh, native_song=None, artwork=None, minimum=None,
catalog=None, catalog_audio=None, write_gate=None, forget=None)`. `access` implements the existing collection Access
protocol. The callback signatures are:

```python
scan(folders: Sequence[str]) -> Awaitable[None]
native_lookup(owner_id: str, library_id: str, relative_path: str) -> Awaitable[NativeFile | None]
refresh(owner_id: str, library_id: str) -> Awaitable[None]
native_song(owner_id: str, library_id: str, song_id: str) -> Awaitable[Mapping[str, Any]]
artwork(owner_id: str, library_id: str, cover_id: str) -> Awaitable[tuple[bytes, str]]
minimum(owner_id: str) -> Awaitable[shijhon.quality.Minimum]
```

`minimum` gives the importing person's minimum quality (without it nothing is gated).
`catalog` finds and adds a low-quality album's catalog version
(`music_imports.catalog_route.CatalogRoute(matcher, commits)` in the application: the
existing album matching, then the existing commit path under the import's own
authorization, with the reviewed release's fingerprint checked at the write boundary).
Without it, such albums count as not in the catalog.

`native_song` and `artwork` (`CallerLibrary.song` and `.artwork` in the application)
read one song and one cover with the importing caller's own credentials: a song a file
is linked to is checked with them before the link commits (`NativeAbsent` from
`native_song` is verified absence: the file is published instead; any other failure
pauses the import), an album's tags for a copy are read from the file that answer names,
and an upgrade compares the song's details before and after. Without `native_song`,
matches rest on the shared index alone and no copies are made (such files are held back).

`write_gate()` raises when Navidrome's settings no longer keep a missing file's identity
(`PlaceholderWrites.refused(fresh=True)` in the application); an upgrade asks it right
before it replaces a published file. `forget(song_id)` drops the playback quality gate's
cached facts of a song whose audio an import changed (`QualityGate.forget`).

`catalog_audio` (`CatalogSongAudio(engine)`, ``music_imports/catalog_audio.py``) puts
an import's audio into the catalog songs it matched; without it, they stay plain links.
`MusicImports.upgrades` upgrades a person's own imported song
(``music_imports/upgrade.py``); the application calls it through `AfterPlay` after a play
the quality gate sent to the add-ons.

Use the shared ScanCoordinator for `scan`. The exact lookup must use current
caller credentials, a bounded native response and exact library-relative path
matching. `refresh` must await a completed caller-owned structural refresh rather
than merely queueing a throttled refresh: the review's comparison and the check before
each publication batch await it. Start `run()` in the parent lifespan,
cancel/join requests and the worker before `aclose()`, and initialize the service
once. `tick()` checks one private file or publishes/reconciles one bounded approved
batch for deterministic integration.

Construct `AssetResolver(store, *, library_id, library_path,
imports_folder="_imports", managed_folder="_shijhon")` independently of intake
enablement and pass that callable to the Collection index's trusted asset lookup.
It requires no initialize/close lifecycle and performs no lock, mkdir, chmod or
marker write. Keep it wired when uploads are disabled. The enabled service's
`asset_lookup` delegates to this same resolver.
`Storage.final_parent(name, *, create=False)` is a context manager yielding an
opened parent directory descriptor and leaf filename. The context owns and closes
the descriptor. It accepts nested batch receipts; reads
never create or repair directories. Approved publication alone uses `create=True`.
Operations on a retained final path must use this interface rather than joining
an untrusted path onto the root descriptor.

The resolver accepts at most 500 caller-native source rows and returns native song ID to
durable asset ID only for exact configured-library ledger paths, matching size,
and the retained publication inode. It can recover structural import aliases; it
never establishes personal ownership. Wrong libraries return no matches. Root
failures stop the source refresh and retain the last complete generation.

Initialization permits pending legacy setup. Import operations require an explicit
persisted personal policy; missing or legacy policy yields
`personal_setup_required`. Disabling uploads does not change read policy. The
parent owns configuration, authentication, startup, mounts and legacy setup.

The async API is `capabilities(owner_id)`, `create(owner_id, request_key)`,
`declare(owner_id, job_id, page_key, entries)`, `seal(owner_id, job_id)`,
`receive(owner_id, job_id, item_id, attempt, stream)`,
`review(owner_id, job_id, *, exclude=())`,
`distinct(owner_id, job_id, item_id, *, value=True)`,
`keep_files(owner_id, job_id, item_id, *, value=True)`, `check_again(owner_id, job_id)`,
`approve(owner_id, job_id, revision, request_key)`,
`status(owner_id, job_id, *, offset=0, limit=100, problems_only=False, overlap_only=False)`,
`listing(owner_id, *, limit=20)`, `resume(owner_id, job_id)` and
`cancel(owner_id, job_id)`. `review` queues private `checking` and returns
immediately. The single bounded worker validates one file per tick outside database
transactions, reports `checked_files`, and freezes `review` when finished. Poll
status, then require explicit approval. Restart or access failure pauses checking;
resume retains its exact exclusions and completed checks. Create, declaration pages, seal and exact approval
replays are idempotent. A changed request/page/approval key conflicts. Review
exclusions create a new revision; approval cannot expand that reviewed set.
Declaration checks that the job is still declaring inside its insertion
transaction. Stop either follows committed reservation and releases its unused
capacity, or precedes insertion and refuses it without leaving a page or items.
Problems-only pages include rejected/excluded files, nonempty warnings and held or
unsure files; overlap-only pages the files the library already had or that join one of
its albums or that the quality gate looked at (`outcome`: `linked`, `catalog_song`,
`copy`, `duplicate`, `held`, `unsure`, `low_quality`; `match` holds the reason, the
library song and both qualities, and for the quality gate `below_minimum`, `route`
(`catalog`, `unchecked`, `files`), the catalog `release` and `track`, `audio` and
`kept`; `keep_files` is the person's "add my files anyway"). Both have
exact `filtered_total` and `next_offset` over that filtered set. A copy carrying an
album's tags is its own ledger item (`derived_from` its received original, which stays
`joined`); pages show it on its original's row (`copy_state`, `copy_song_id`). Whole-job counters
remain available. Counts and the page share one database snapshot.

The dashboard must validate its session, current grant and CSRF header before
reading any bytes, then pass the raw asynchronous stream to `receive`. Do not
parse a form/multipart body first. The service separately checks immutable owner,
current grants, policy, item/attempt and capacity before advancing the stream,
and rechecks access after waits and before publication/credit. No durable record
or status JSON contains reusable credentials or server absolute paths.

## Bounds

Defaults, all configurable by the parent:

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
releases unreceived reservations, while retained files continue counting against
the byte quotas. These are conservative retained-storage quotas, not bandwidth
quotas. Increase them deliberately as storage grows. Body sizes and optional client
SHA-256 assertions are independently checked against server-counted/hash bytes.

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

Custom service integrations must provide the optional typed
`ImportRemovals(..., native_absent=...)` callback to handle unindexed files; without it
that case refuses removal. The callback receives owner ID, library ID and exact
library-relative path, returning `True` only after a complete bounded native read
including missing rows, with the same current caller and library grant before and
after the read. `CallerLibrary.imported_file_absent` supplies this in the application.
An ordinary `native_lookup` returning `None` is insufficient negative evidence.

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
