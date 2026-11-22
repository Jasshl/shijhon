# Your collection

Open **Collection** in Shijhon to choose your music. Each account has three libraries of
its own, while the music files stay shared:

- **All** is what apps show by default: everything you collected, the other two together.
- **Original imported library** holds the music you imported, the albums of playlists you
  imported with **Import a playlist**, and the songs and albums you starred.
- **Shijhon library** holds the catalog albums Shijhon added because of you. These are
  albums your apps played, starred, rated, put into a playlist, downloaded, shared or
  bookmarked. They also include the albums of playlists whose audio you prepared on the
  **Downloads** page, and of playlists Shijhon made for you (discovery mixes, described
  playlists, agent requests).

Albums in a library are always whole, including the songs Shijhon added to complete them,
whether or not their audio is downloaded. On this page you can add albums and songs to
each library and remove them. A song you add brings its whole album. What you remove
stays out of that library, even when something would otherwise bring it back in. So an
album you took songs out of can stay partial. All holds what the other two hold after
your changes to them, and you can also add to All or remove from it directly. For a named
selection that changes only when you change it, make a
[custom library](#optional-custom-libraries).

Beside the library picker, **Used in my apps** marks the library your music apps and the
native website use. Choosing another library only lets you browse it. To switch your apps
to it, choose **Use in my apps** there. The switch applies to your whole account on every
device, and other accounts keep their own choices.

Right after installation or an upgrade, apps can say for a few minutes that the library
is being prepared. Music added to your folder later shows up by itself, and **More → Refresh
music** shows it at once.

Imported music joins the importer's Original imported library and All. When an import
contains songs the library already had, catalog songs included, those songs join these
libraries too. Until an administrator completes personal library setup in **Collection →
Import music**, the library's existing music files are shared: their albums appear in
every account's Original imported library and All. After that setup, new accounts start
with empty libraries. Another account's music, plays and choices do not change yours.
Someone else's imports enter your libraries when you add them, or when something you do
brings in their album.

## What brings music in

A library grows by itself when you do something with music. Each action counts as soon as
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
| ask a connected agent to add an album or an artist's releases ([agent access](agent-access.md)) | - | the albums |

When an administrator has set a
[daily cap on adding](how-it-works.md#the-daily-cap-on-adding), the Collection page says
how many catalog releases you have added within 24 hours and, at the cap, from when you
can add more.

A favorite counts in both libraries, even one made in Navidrome directly or before
Shijhon recorded favorites. Shijhon picks those up when it reads your favorites, at most
every five minutes while you use your apps, the native website or this page. When you
take back a favorite, its album leaves Original imported library unless something else
keeps it there. What the favorite brought into Shijhon library stays, like everything
Shijhon added for you.

Browsing, searching and opening an album bring nothing in. Neither does streaming or
fetching audio, including fetching ahead, unless your app reports a play. Songs Shijhon
newly adds to the library,
such as the rest of an album it completes, usually reach apps' lists within a minute. The
[detailed rules](reference/library-visibility.md#collection-rules) cover playlists you
change later and catalog albums from before Shijhon recorded who added them.

## Import your files

Choose **Import music** in Collection to add your own music files. The steps, the
supported formats and what happens to music the library already has are in
[Import music files](music-imports.md). Importing never changes the library your apps
use, and music the library already has is linked to your import instead of added again.

Music-file imports are separate from **Import a playlist**, which takes a playlist as text
or as a playlist file such as M3U or CSV ([details](playlist-imports.md)).

## Find and choose music

Albums and artists start in a grid with their artwork, and List gives a compact view.
Search across artist, album and song names, then filter and sort the results.

- **Show → In this library**, the default, lists the music in the library you picked.
- **Show → Not in this library** lists the music you collected that the picked library
  does not hold. Music you collected is what your libraries hold or held, and everything
  you added or that was added for you. An album or artist appears when at least one of
  its songs is outside the library, and its count includes only those songs.
- **Show → Everything on the server** lists all music in the library, also what other
  accounts imported or collected, so you can add it to your libraries. It is for browsing
  only, not a library your apps can use.
- **Source → Imported** shows only imported files and the songs your imports were linked
  to. **Source → From the catalog** shows every other song.

Some music you collected needs preparing before apps can show it, such as catalog songs
the cleanup removed or recent additions not indexed yet. **Prepare and add** reuses or
restores them when you approve the change. Music that cannot be restored stays out of apps
and is counted as skipped, and missing original files need importing again. Everything on
the server shows only music that is there now.

In **Not in this library**, select rows or **Every match** to choose the matching songs,
or use a row's button to choose its whole album or artist. Adding a song brings its whole
album, except songs you removed from that library. Added songs leave this view, and
**Undo** brings them back where nothing has changed them since.

A partly included album shows how many of its songs are in the library, and how many are
imported and from the catalog. **Choose songs** opens an album's or artist's songs to
choose them one by one. It keeps the Show filter, so switch to **Not in this library** to
see the songs a library lacks. Song rows include the album name to tell versions apart.
**All songs**, or going back to albums or artists, leaves that list, and **Reset
filters** clears the filters.

An album opened in a music app shows its complete track list, and opening it adds
nothing. Library lists, synchronization, shuffle and search in apps use the library you
chose for them. Shuffle can play songs whose audio is not downloaded yet, as albums do.
A search also shows a catalog song or album when its copy in the server's library is
outside the library you chose. It appears once, and playing or opening it uses that copy.

## Bulk changes

Every library can be changed, Original imported library and your own imports included. A
removed import stays out of that library and keeps its file. Removing music from a
library never deletes or moves files, and favorites, playlists and listening history stay.
Large changes open a review first, while small ones start at once and offer Undo. **Back
to browsing** returns to the view you came from, and each library lists its own **Recent
changes**. A change to a whole artist covers only the music it has at that time, and its
future releases need a choice of their own.

**Select this page** selects the rows shown, and a selection stays across pages, up to
1,000 rows. **Select every match** (or **Every match** in the bar) takes all matching
pages instead, or the whole collection when no filter is set. Ticking a row or **Select
this page** switches back to **Selected**. Changing the library, Show, view or filters
clears the selection and says so. Once something is selected, a bar at the bottom names
the mode and its album, artist or song count, and offers **Add to …** and **Remove
from …**.

The review shows exactly which songs match, how many groups they form and how many will
change visibility. Confirm to start the change in the background. Its progress stays
under **Recent changes**, also after you leave the page, and **Retry** resumes an approved
change. **Undo this change** restores the earlier choices where they have not changed
again. Songs with newer choices are skipped and counted, so Undo cannot overwrite later
work.

## Optional custom libraries

Choose **Create custom library** beside the library picker, and start from a copy of
Original imported library, an empty selection or a copy of another library. Rename it,
choose its songs, and switch apps to it with **Use in my apps** when it is ready. A
library started empty opens on **Not in this library**, ready for choosing its music,
and the library your apps use stays as it was.

A copy holds what its source held when you made it. Later imports, favorites, plays and
changes to the source do not change it, and a song the source left out stays out. A
custom library changes only when you change it, with one exception. Like every album in a
library, its albums take in songs that join them later, such as those Shijhon adds to
complete an album. **Archive** keeps a custom library's choices, and **Restore** brings it
back. Switch apps to another library before archiving the one they use.

## New releases

**New releases**, under Music beside Collection, follows the artists you favorite and
play most. Turn it on there, and their releases of the last 30 days that your library
lacks come first among the newest albums in your apps. The page lists the same
releases, the artists followed and when they were last checked.

Showing a new release brings nothing into your libraries. Once you play, favorite,
rate or add it, it joins your Shijhon library like any catalog album, and leaves the list
of new releases. The [details](how-it-works.md#new-releases) cover who is followed and
when the catalog is asked.
