# Your collection

Open **Collection** in Shijhon to choose your music. Each account has three libraries
of its own; the music files stay shared:

- **All**: the default in apps. Everything you collected: Original imported library and
  Shijhon library together.
- **Original imported library**: your own taste - the music you imported, the albums of
  the playlists you imported with **Import a playlist**, and your favorites (songs and
  albums you starred).
- **Shijhon library**: what Shijhon added from the catalog because of you - the catalog
  albums your apps played, favorited, rated, put into a playlist, downloaded, shared or
  bookmarked, the audio you prepared on the **Downloads** page, and the playlists Shijhon
  made and published for you (discovery mixes, described playlists, agent requests).

Albums in a library are always whole: all of the album's songs, also those Shijhon added
to complete it, with or without downloaded audio. You can add and remove albums and
songs in each library on this page. A song you add brings its whole album; what you
remove stays out of that library, whatever would bring it in - so an album you removed
songs from can stay partial. All holds Original imported library and Shijhon library as
you left them, plus or minus what you add to or remove from All itself. Use **Create
custom library** beside the picker to make a named selection that changes only when you
change it.

Sign in to **Collection** once to prepare the first library view. This starts automatically
using your own access. Progress updates while you wait; the page shows your music when
ready. Choose **More → Open music** to continue to the native website. For a large library,
finish this preparation before opening a music app. The completed view survives restarts.
**More → Refresh music** updates the available music and may ask you to sign in again.

Your imports belong to your Original imported library and All - also songs the library
already had when you imported them, which your import is linked to, catalog songs
included. Until an administrator completes personal library setup in
**Collection → Import music**, existing originals are shared: their albums appear in
every account's Original imported library and All. After that setup, new accounts start
with empty libraries. Another account's music, plays and choices do not change yours;
someone else's imports enter yours when you add them, or when one of your reasons brings
in their album. Choosing a library to browse does not activate it: **Use in my apps**
sets the library your music apps and the native web library use, for your whole account
on every device.

## What brings music in

A library grows by itself when you do something with music; each counts as soon as
Navidrome accepts it:

| You | Original imported library | Shijhon library |
|---|---|---|
| import music files | the albums | - |
| import a playlist (**Import a playlist**) | its songs' albums | its catalog songs' albums |
| favorite a song or album | its album, while it is a favorite | a catalog album |
| play a catalog song (your app reports the play) | - | its album |
| rate, put into a playlist in an app, download (the app's download), share or bookmark a catalog song | - | its album |
| prepare a playlist's audio on the **Downloads** page | - | its catalog songs' albums |
| get a playlist Shijhon made for you (a discovery mix, a described playlist, an agent's request) | - | its catalog songs' albums |

A favorite counts in both libraries, also one made at Navidrome directly or before this
version (once Shijhon reads your favorites). Taken back, it leaves Original imported
library unless something else keeps the album there; what it brought into Shijhon
library stays, as everything Shijhon added for you does. Streaming or fetching audio
without a play your app reports, fetching ahead, browsing, searching and opening an
album bring nothing in. A playlist of many songs counts too: up to 500 songs are counted
with the request, more are counted from Shijhon's records shortly after (usually within a
minute; what cannot be counted then is tried again later).

When this version first started, the catalog albums already in the library that no
account's record explains - many were added before Shijhon recorded who caused them -
went into no library. The server's operator can give them to one account's Shijhon
library ([upgrades](deployment.md#upgrades)); remove them there if you do not want them.
Otherwise **Show → Everything on the server** lists them, to add by hand.

## Import your files

Choose **Import music** in Collection, then **Choose files** or **Choose a folder**.
The page explains sharing before upload and shows the supported formats, file count,
bytes and skipped unsupported files. Supported formats and limits come from the service;
the formats are listed in [Import music files](music-imports.md#formats-and-verification).
If imports are not set up, the page explains what
is missing and keeps upload controls unavailable. Account creation remains with the
administrator through the existing account system.

**Upload for review** transfers files into private staging. Keep the tab open during
upload. Audio is sent directly, with a bounded number of files transferring at once.
The browser does not read complete audio files into memory or hash their contents.

The review shows exact ready and skipped counts with a paginated file list. **Needs
attention** opens first when problems exist and pages through their exact file list.
Choose **All files** to review the complete selection. Each problem has a reason. An upload is not an import: explicitly choose
**Import N songs; skip M files** to approve the reviewed set. No songs are added before
this approval. The service checks the supplied audio; file names and local timestamps
are not proof of its content. Uploaded audio and tags are kept as supplied.

Music the library already has is not added again
([details](music-imports.md#music-the-library-already-has)). A song that is already
there - your own earlier import, someone else's, an original or a song Shijhon added from
the catalog - is linked instead: it counts as your import in Original imported library and
All, and nothing in the library is overwritten. A song an album of the same name lacks
is added into that album, so apps do not show the same album twice; another edition,
such as a Deluxe Edition, is its own album. The review shows these files with their
reasons and both qualities; choose **Not the same song** when a match is wrong. After the
import, show **Music the library already had** in the file list to see what was not
added again and why.

An album below your minimum audio quality (your **Audio quality** page) is added from
the catalog instead when the catalog has it: its songs are catalog songs, played from the
add-ons when they have the audio, and count as your imports. When the catalog does not
have it, your files are added and marked.
Choose **Add my files anyway** to add your own files
([details](music-imports.md#below-your-minimum-quality)).

After approval, progress distinguishes placing files from making them available in the
library. A song counts as imported only after its native identity and personal import
membership are verified. You can leave the page and reopen the job under **Your
imports** on the Import music page. Completed music has links to Original imported library and All. Importing
preserves the library your apps use, including an active custom library.

If upload is interrupted or the browser reloads, open that import and choose the same
files or folder. Completed files are kept and will not upload again; unfinished files
restart from the beginning. A changed source file needs a new import. File path, size
and timestamp help match a reselected source, while the saved upload remains an immutable
snapshot of bytes already received. After an uncertain response, the page checks saved
status before a bounded retry. A reloaded browser has no automatic access to local files.

**Discard upload** stops an unapproved draft. **Stop import** stops pending work after
approval. Files already placed as originals stay on disk, and successfully imported songs
stay in your library. To take an import back out, open it under **Your imports** and
choose **Review removal** ([details](music-imports.md#remove-an-import-and-restore-it));
originals are never deleted automatically.

Music-file imports are separate from **Import a playlist**, which takes a playlist as text
or as a playlist file such as M3U or CSV ([details](playlist-imports.md)).

## Find and choose music

Albums and artists start in Grid, with available artwork and an initials fallback.
Switch to List for a compact view. Songs use a list without replacing your album/artist
layout choice. Search across artist, album and song names, filter and sort the results.
Pages contain at most 40 rows.

- **Show → In this library** (the default): the music in the library you picked.
- **Show → Not in this library**: the music you collected - what your libraries hold or
  held by their rules or your choices, and everything you added or that was added for you
  - that the picked library does not hold. Albums and artists appear when at least one of
  their songs is outside it; the count includes only those songs.
- **Show → Everything on the server**: all music in the library, also what other accounts
  imported or collected, to add to your libraries. It is for browsing only, not a library
  your apps can use.
- **Source → Imported**: imported files, and songs your import was linked to.
  **Source → From the catalog**: every other song - what Shijhon added from the catalog.

The count beside the picker names the library and how much more you collected. Beside
the library picker, **Used in my apps** marks the library your apps use; for another
library, **Use in my apps** switches to it everywhere for you. Other accounts keep their
own choices. Links and forms keep the picked library, Show and Source through search,
song drilldown, pagination, layout and changes; switching Show to or from **Everything on
the server** starts a fresh list.

Music you collected also retains songs that need preparation, including unused
placeholders removed by cleanup and recent additions awaiting indexing. **Prepare and
add** reuses existing music or restores missing catalog placeholders when you approve the
change; preparing and reviewing a change do not create files. Music that cannot be
restored stays out of apps and is counted as skipped. Missing original files need
reimporting. Everything on the server contains only music currently present.

In **Not in this library**, selecting rows or **Every match** chooses the currently
matching songs. Adding a song brings its whole album, except other songs you removed from
that library. The explicit whole-album/artist button chooses the whole current group.
Added songs disappear from this filtered view. **Undo** makes those songs appear
here again where their choices have not subsequently changed. If a change empties the
last page while earlier matches remain, browsing moves to the last valid page and keeps
the library and filters.

A partly included album shows how many of its songs are in the library, and how many are
imported and from the catalog. **Choose songs** opens the named album or artist's songs so
you can make individual choices; it keeps Show, so switch to **Not in this library** to
see the songs a library lacks. Song rows include the album name to distinguish versions.
Return to albums/artists or choose **All songs** to leave the drilldown. Switching Browse
to Albums or Artists clears the song scope. Applied filters remain visible on mobile;
**Reset filters** clears them.

Opening an album in a music app shows its complete track list. Library browsing,
synchronization, shuffle and search use the library used in apps; shuffle can play songs
whose audio is not downloaded yet, as albums do. Opening an album does not add its
songs. A search also shows catalog songs and albums whose library copy is outside that
library, once each; playing or opening one uses the library's copy.

## How your libraries stay current

- A favorite, a play or another action in an app or on the native website counts as soon
  as Navidrome accepts it. Shijhon also reads your favorites from Navidrome at most every
  five minutes while you use your apps, the native website or this page: that picks up
  favorites Shijhon did not see, such as those made at Navidrome directly (they count in
  Original imported library). The first read for your account is waited for a few
  seconds.
- A playlist counts once it is published; a **Recheck** that publishes an imported
  playlist again replaces the songs it counts in Original imported library. Songs you add
  to the playlist yourself count as an app's playlist addition only when you add them
  through Shijhon.
- Songs Shijhon newly adds to the library (an album it completes, a catalog album a
  playlist, an import or a first play adds) enter the lists from your apps' own
  requests: Shijhon looks for them at most every ten seconds while apps read the library
  and adds them at most once a minute - so a large addition comes in as one - usually
  within a minute; apps keep getting their lists meanwhile. **Refresh music**, signing in to Shijhon's pages and the native
  website (at most every five minutes) refresh the whole library, also music added to
  the folder outside Shijhon.

The direct row action explicitly chooses the whole current album or artist, in the
music Show lists. Every library can be changed, also Original imported library, and also
your own imports: a removed import stays out of that library and keeps its file. Large
changes open a review first; small direct changes start immediately and offer Undo. The
change page names the library and keeps your browsing filters, page and layout. **Back to
browsing** returns to that view. Each library lists its own **Recent changes**. Future
releases are separate choices.

## Bulk changes

**Select this page** selects the displayed rows. Selections stay across pages, up to 1,000 rows
and a bounded selection size. Changing the library, Show, view or filters clears
the selection with an explanation. **Select every match** (or **Every match** in the bar)
is a separate choice that includes all matching pages,
including the whole available collection when there are no filters.
Ticking a row or **Select this page** switches back to **Selected**. Once something is
selected, a bar at the bottom of the window names the active mode and its album, artist
or song count and offers **Add to …** and **Remove from …**; without JavaScript the bar
is always shown below the rows.

The review freezes the actual matching songs and shows their exact count, group count,
and how many will change visibility. Bulk actions respect the current source and search
filters. Confirm to start the background change. Progress remains available under Recent
changes; you can leave the page and return. Retry resumes an approved change safely;
an unapproved change always returns to review.

**Undo this change** restores its earlier choices where they have not been changed again.
Songs with newer choices are skipped and counted, so Undo cannot overwrite later work.

## Optional custom libraries

Choose **Create custom library** beside the library picker. Start with a copy of Original
imported library, an empty selection, or a copy of an existing library. Rename it,
choose its songs, and activate it with **Use in my apps** when ready. A library started
empty opens **Not in this library**, so you can choose its music immediately; this leaves
the library your apps use unchanged.

A copy holds what its source held when you made it, as choices of its own: later
imports, favorites, plays and changes to the source do not change it, and a song the
source left out stays out. A custom library changes only when you change it - except that
an album in it, as every album in a library, takes in songs that join the album later
(such as those Shijhon adds to complete it). Archive keeps a custom library's choices and can be reversed with
Restore. Switch to another app library before archiving the active one.

Removing music from a library does not delete or move files, change native IDs,
or remove favorites, playlists or listening history. Explicitly saved catalog music, and
catalog songs an active import is linked to, keep their library metadata from
unused-release cleanup. Downloaded and kept audio (**Saved audio** on the Cleanup page)
has separate retention settings.

## Refresh music

The page reads a local index of music the signed-in account can access. **Refresh music** updates
it in bounded background pages. Browsing continues on the last complete index if a refresh
is interrupted. Personal library choices survive metadata cleanup and adding the music
back. Original and catalog origin are tracked per song, including partly filled albums;
downloading a catalog track does not turn it into an original import.
