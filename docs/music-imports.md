# Import music files

Choose **Import music** in Collection to add your own audio files to the library. The
files you upload are kept unchanged, and nothing is added before you approve a review of
them. Other people with access to the library can play imported files and add them to
their own libraries. Only the importing account gets them as its imports, and new
accounts inherit none. The files on your computer are never changed.

## Upload and approve

1. Choose **Choose files** or **Choose a folder**. Before the upload, the page explains
   sharing and shows the supported formats, the file count, the size and the unsupported
   files it will skip. If imports are not set up, the page says what is missing and keeps
   the upload controls unavailable.
2. **Upload for review** sends the files to a private area on the server. Keep the tab
   open while they upload. The upload bar counts bytes across the whole batch, and the
   file count goes up as each file is complete.
3. The review shows how many files are ready and how many are skipped, with a reason for
   each problem. **Needs attention** opens first when there are problems, and **All
   files** shows the whole selection. Missing title, artist or album tags give warnings;
   adding them is optional.
4. Choose **Import N songs; skip M files** to approve the reviewed set. An upload is not
   an import, and no songs are added before this approval.

After approval, the progress shows the files being placed and then made available in the
library. An import is finished only once its music is in the library and available to your
account. The import keeps running when you leave the page and reopens under **Your
imports**. Finished music joins Original imported library and All, and the page links to
both. Your apps keep using the library they used before, even a custom library. If the
server restarts, an approved import waits until you resume it.

When Navidrome does not index an approved file, its row is held with an indexing error
and the file stays in place. The other files go on, and once they finish, **Resume
import** tries the held files again.

If the tab closes or the upload is interrupted, open the batch under **Your imports**,
choose **Resume upload**, select the same files or folder and choose **Continue upload**.
Completed files are kept, and only an unfinished file starts over. A source file you
changed since needs a new import. When an import asks for a fresh sign-in, use **Sign in
again** and return to the same batch; there is no need to sign out first, and uploaded
files are kept.

**Discard upload** ends an unapproved batch for good, so start a new import afterward. It
deletes uploaded files that were not added to the library and frees their space once
cleanup finishes. **Stop import** stops pending work after approval. Files already placed
stay on disk, and songs already imported stay in your library. To take an import back
out, use **Review removal** ([below](#remove-an-import-and-restore-it)).

The imported copy keeps the selected file name and folders inside a batch folder the
server names. An untagged song keeps its file name, which Navidrome uses as its title.
Folders Navidrome's scanner ignores, such as dot-folders, get visible names: leading dots
are removed, and a suffix keeps names apart when they collide. The review keeps the paths
you selected. Navidrome always ignores a file name starting with a dot and a `.git`
folder, so these are refused before upload. So are selected paths that are the same apart
from letter case or Unicode form, and paths that clash as a file and a folder.

Shijhon never converts or rewrites imported audio or tags, except in four cases. In each
of them, your uploaded file stays unchanged beside the result:

- a copy that carries an album's tags ([below](#music-the-library-already-has));
- your audio in a catalog song ([below](#your-audio-for-catalog-songs));
- an upgrade from the add-ons you allow ([below](#upgrading-your-imported-songs));
- a file with each artist of a joined credit ([below](#each-artist-of-a-joined-credit)).

## Formats and verification

These formats were tested with unmodified Navidrome **0.64.2** and its default scanner on
Darwin ARM64 and Linux ARM64:

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

TAK files are refused before upload because Navidrome's scanner rejects valid TAK audio,
and no tested scanner setting changes that. Whether TrueAudio works depends on the system
Navidrome runs on. On the tested Linux build its imports finish. The tested Darwin build
does not index valid files, so such an import pauses with an indexing error and keeps its
files. The picker accepts `.tta` everywhere, because Navidrome can run on another machine
than Shijhon. Check your Navidrome's compatibility before approving TrueAudio.

Other codec profiles, DRM-protected or encrypted files, other suffixes and other scanner
or platform versions are untested, and the format picker does not promise them. Existing
tags in every accepted file stay byte-identical, including tags Navidrome does not show.
Where Navidrome reports a duration of zero, Shijhon keeps it and invents no other
metadata. A file whose headers cannot be inspected within Shijhon's limits is refused.
The [format checks](reference/music-imports.md#format-checks) describe the tested setup,
the evidence and the header checks.

## Storage and recovery

The imports folder, normally `_imports`, sits in Navidrome's music library beside
Shijhon's managed folder, never inside it. It must already exist and be empty on first
use, and Navidrome must be able to read the files in it; the
[deployment guide](deployment.md#music-imports) sets it up. The original music library
does not need to move or become writable.

A full backup and restore must include the imports folder, with its marker and its hidden
private files, together with Shijhon's state database. The marker ties the folder to that
database, and another database cannot take it over. Restoring only one of the two needs
reconciliation by the operator. The folder's mount path can change, as long as it still
opens the same verified folder. Copying or replacing the folder needs reconciliation too.

Imported files never overwrite existing files, even identical ones. An interrupted import
resumes by checking the files already placed before it adds more. An unexpected change to
a file or its identity in Navidrome pauses the import and keeps the files for inspection.

Uploaded files that were not added are deleted, and their space is freed. These are the
files left after Stop and, once an import finishes, files that were skipped, rejected,
excluded or duplicates in the selection. Files linked to music the library already has
are not kept either, except audio given to catalog songs. Completed files stay in the
music library and are also kept privately, as are originals needed for recovery,
upgrades and restoring. Kept and unfinished files count against the storage limits until
their cleanup is complete. Publication, recovery and the folder layout are described in the
[service reference](reference/music-imports.md).

## Music the library already has

Shijhon links files it recognizes to the songs the library already has, so an album does
not show twice in apps. A file it is unsure about is flagged in the review. Once every
file is checked, the review compares the files with each other and with the music you can
access: files in the music folder, any account's imports including your own earlier ones,
and songs Shijhon added from the catalog. It compares with the library as it is at that
moment, so music just added to the folder counts too. It uses Navidrome's own song
details (ISRCs, MusicBrainz recording IDs, explicit or clean status, disc and track
numbers). Two songs are the same when they are the same recording on the same album:

- Two albums are the same when they have the same album artist and album name, ignoring
  letter case, accents, spacing and punctuation ("NightLetters" is "Night Letters"), and
  a song in common. Without a song in common, they are still the same when they have no
  different year and no clearly different cover, as with a second disc imported on its
  own. An edition's words count: "Night Letters (Deluxe Edition)" is another album, and
  an artist's self-titled albums of different years that share no song stay apart. A
  compilation without an album artist tag is an album of "Various Artists", as Navidrome
  shows it. The same rule brings the files of one selection together.
- Two recordings are the same when they share a MusicBrainz recording ID or ISRC.
  Without either, they are the same when they have the same recording title, a shared
  artist and lengths within 2 seconds. Version markers such as "(Live)" and "(Remix)" mean other
  audio, but "- 2011 Remaster" does not. A clean edit is never the same as an unmarked or
  explicit song. Exactly the same bytes as an earlier import are always the same.

A file without an album name never joins an album. Untagged files have none, and neither
do Shorten files, which have no tags.

What happens to each file:

| The file | Outcome |
|---|---|
| Already in the library with its own audio | Not added again. The import links to that song; it counts as the importer's own import (Original imported library and All). Nothing in the library is overwritten, whatever the two qualities. The received file is not kept. |
| A song Shijhon added from the catalog | Linked to that song the same way, and the file's audio goes into it ([below](#your-audio-for-catalog-songs)). The received file is kept. |
| A song the library's same-named album lacks | Added. When its own tags would make Navidrome show a second album of that name (another release date, MusicBrainz album ID, version tag or spelling), the library gets a copy carrying that album's album-level tags, so the song joins the album. The received file stays unchanged and retained. With several same-named albums, the one with the most songs is used. A file that still cannot be fitted into the album (its copy could not be made) is not added, and the review and the report say why. |
| Another edition, or another album of the same name | Added as its own album, shared songs included. |
| The same song twice in this selection | The better copy (lossless first, then bitrate) is added. |
| Close to a song of the album, but not clearly the same | Added into the album like a song it lacks (as a copy when needed), marked "may already be in the library". |

The review lists every such file with its reason and both qualities. **Not the same
song** keeps a file from being linked. The file is then added like a song its album
lacks, into that album and as a copy when needed. Either choice gives the review a new
revision. A file that is not added because it would make a second album of its name has
no such choice.

Before each step of publishing, the files still to publish are compared with the library
again. If something else added one of the songs meanwhile, such as another import or a
file put into the folder, the file is linked to it. Any other change to what the review
decided takes the import back to review. Its rows say the library changed, and it needs a
new approval. Files already
placed, published or linked stay as they are. The
[matching details](reference/music-imports.md#matching-details) cover the checks made
right before a link.

After the import, show **Music the library already had** in the file list to see what was
not added again and why. If Navidrome still puts a song into a second album, against what
Shijhon expected, its row is marked.

### Below your minimum quality

Each person sets a minimum audio quality on the dashboard's **Audio quality** page. By
default it is compressed audio of at least 192 kbps, lossless audio always, and at least
44.1 kHz. The review applies the importing person's own minimum, album by album, so an
album never ends up half your files and half catalog songs. The table applies when a file
is below that minimum, and its album would be added or the file would give its audio to a
song Shijhon added from the catalog:

| The album | Outcome |
|---|---|
| The catalog has it (every such song on one release) | Your files are not added. On approval, the catalog's release is added to the library, into the library's album of the same name when there is one. Your files are linked to its songs. Those songs are catalog songs, played from the add-ons when they have the audio, and they count as your imports. The received files are not kept. |
| The catalog does not have it, or no catalog is set up | Added as it would have been, marked "below your minimum" in the review and the report. |
| The catalog could not be reached | Waits in the review: **Check again**, or **Add my files anyway**. If the import is approved while it still waits, that album is not added. |

A song already in the library stays linked, whatever the quality of either copy. A song
Shijhon added from the catalog keeps its own audio: a file below the minimum is linked to
it without giving its audio. Clean edits stay clean: a catalog release that has only the
explicit version of a clean song, or only the clean version of an explicit one, does not
count. A quality the
file does not state (a raw AAC file's bitrate, say) is never below a minimum. **Add my
files anyway** adds the album's files as they would have been, and **Use the catalog's
version** takes that back. Either choice, and **Check again**, gives the review a new
revision.

At publication the catalog's release must be exactly the reviewed one. When the library
refuses it (it would join an album that cannot take it, or the catalog no longer has or
changed it), those files are not added and their rows say why. Any other failure pauses
the import, and **Resume import** tries again. The catalog's release counts toward your
[daily cap on adding](how-it-works.md#the-daily-cap-on-adding) when an administrator has
set one. At the cap the import pauses, saying so, and goes on when you resume it later.

### Your audio for catalog songs

When an import is linked to a catalog song Shijhon added, the song plays the imported
file's audio once the import is done. Until then it played from the add-ons, or from
audio downloaded for a while. It stays the same song, with its ID, favorites, plays and
playlist entries. Apps get a copy of the audio carrying the catalog song's tags, and the
received file stays unchanged and retained. That audio
belongs to the import: it does not expire like downloaded audio, the cleanup counts it as
a use, and the copy counts against the importer's storage. A catalog song already playing
another import's audio keeps it.

Some files stay a plain link, and the song keeps playing from the add-ons. That happens
to a file below the importer's minimum quality
([configuration.md](configuration.md#audio-quality)), and to a file in a format that
cannot take the catalog song's tags. Only FLAC, Ogg Vorbis and Opus, MP3 and MPEG-4 files
can. The import's report says which happened. Removing the import makes the catalog songs
that took its audio play from the add-ons again, and the removal says how many still have
to switch back. Restoring the import puts its audio back.

### Upgrading your imported songs

When you play one of your own imported songs whose file is below your minimum quality, it
plays from the add-ons when they have it well enough. **Upgrade my imported songs from the
add-ons**, on the Audio quality page, is on unless you switch it off. With it on, the
add-ons' audio is then fetched in the background and becomes the song's file. Nothing is
converted, so a
FLAC stays FLAC. The new file carries your file's tags and picture, so the song keeps its
ID, favorites, plays and playlist entries. What describes your file's own encoding, such
as its encoder settings or gapless data, is not carried over, since it would be wrong for
the new audio.

Only songs you imported yourself are upgraded. A file someone else imported is never
upgraded, even when your import was linked to it. Neither are files in the music folder,
songs at or above your minimum quality, or songs whose import is being removed. Files in
Matroska or Shorten are not upgraded, because no tags can be carried out of them. Your
original file stays retained privately, and the upgraded file counts against your storage
as well. Further upgrades keep all earlier audio retained and counted too. The import's
report shows the upgrade with both qualities. Removing the import takes the upgraded file
out, and restoring it puts the upgraded file back. The
[upgrade details](reference/music-imports.md#audio-swaps) cover the formats and how the
swap stays safe when stopped.

### Each artist of a joined credit

Some files name several artists in one artist text, such as "Lumen Vale, Orrin Pike".
With **Each artist of a joined credit** on
([how it works](how-it-works.md#each-artist-of-a-joined-credit)), such a file is
published with the list of those artists, when a catalog gives one within a few seconds.
The import publishes a copy of your file with the list and the album artist Navidrome
would show. Your received file stays retained privately and unchanged, and the copy counts
against your storage as well. A file published without a list may get it later in the
same way, unless it was edited since it was imported. Turning the setting off puts your
retained file back in the copy's place, unless the published file changed since.

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

An import reserves its space before the upload starts, and kept originals go on counting
against these quotas. They limit retained storage, not bandwidth, so increase them
deliberately as storage grows. Changing limits does not invalidate existing jobs.

## Remove an import and restore it

**Stop import** only stops unfinished work and keeps published files. To correct an
import, open it under **Your imports** and choose **Review removal**. The review lists
every published file, which files will leave the shared library, and which stay available
because they are in use. Favorites, plays, ratings, playlists, queues, bookmarks, shares
and other people's saved libraries all count as use. Private playlist names and other
people's personal records are not shown.

Confirming the reviewed removal makes exactly the approved files unavailable to all
accounts. Files already copied to a device stay there. A change since the review asks for
a new one, and an interrupted removal offers **Retry** and **Restore**.

Songs the import only linked, because the library already had them, are not its files.
Removal takes them out of the importer's own imports, and Restore brings them back.
Nobody else's music changes. When an importer removes a file that another account's
import is linked to, that link counts as use of the file, credited to another person.

Removal keeps the original files privately in the imports folder, so it does not free
disk space. **Restore this import** puts the same files and tags back at the same paths
without overwriting anything. Import history, selections, favorites and playlist entries
are kept throughout, and removed songs are unavailable until they are restored.
Navidrome's missing-file setting must stay `Scanner.PurgeMissing = "never"`. Files
Navidrome never indexed are part of the removal review, labeled as unindexed; the
[removal details](reference/music-imports.md#removal-details) cover how they are checked.

An administrator can open **Manage all imports**, review another account's batch,
explicitly include music already in use, stop unfinished importing, or restore a batch
after its original account lost access. These actions use the administrator's own
current authorization and preserve the original import credit. Ordinary accounts can
manage only their own batches. Removing used shared music is an administrator action
because it affects other people's saved music.

Removal needs the imports folder available and imports enabled. It also needs the same
fresh usage export or usage source the cleanup uses, and it is refused when that source
is unavailable or changed. Kept originals have no automatic expiry or permanent-delete
action, so include them in backups. Restoring a whole backup is emergency recovery and
can rewind newer activity from every account.
