# How it works

Your apps talk to Shijhon as they would to Navidrome, and Shijhon passes their requests on
unchanged. To the answers for searches, artist pages and albums it adds results from the
catalog. Navidrome checks every login.

## Placeholders

- **When you use a catalog track** - the app's "now playing" report, a favorite, a
  rating, a playlist entry, a bookmark, a share, a download, or a stream that has to be
  converted - Shijhon writes its whole release into a **placeholder folder** in your
  library: silent FLAC files with the right tags. Navidrome scans them, so they become
  normal tracks with real IDs. Looking at catalog items, and playing them as plain
  streams, writes nothing.
- **Streams** come from your add-ons. While the app stays connected, Shijhon reads each
  song on to its end - also one the app only loaded ahead and reads nothing of for a
  while - and feeds the app from what it holds, so the add-on's connection never sits idle
  and the play's seeks are served from that audio, also after the song's link expired. It
  is not stored in your library - unless **Keep streamed songs** is on (the Cleanup page,
  [`keep_streamed_audio`](configuration.md#delivery)): then a song whose whole audio Shijhon
  received replaces its placeholder, as a download does, and Navidrome serves it from then
  on. A song the app left before Shijhon had all of it is not kept. A play's start and its
  seeks count together when they are of the same file at the add-on. An add-on that asks
  not to be used for downloads is read as the app reads, not on to the end.
- **Downloads and conversions** (to another format, or a lower bitrate than the add-on's
  audio has), Navidrome's jukebox and its share links: Shijhon fetches the whole song
  first and puts it in place of the placeholder, and Navidrome serves it from there. After
  30 days unused, the song (also a kept stream) is replaced by its silent placeholder
  again; once such audio
  takes more than 10 GB in total, the least recently used are replaced first
  ([`delivered_days`, `delivered_gb`](configuration.md#delivery)).
- **A song or album credited to several artists** ("A & B") names each of them in its
  tags when the catalog lists them, so Navidrome gives A and B their own pages and
  pictures instead of an artist called "A & B". A band whose name has an "&" or a comma
  stays one artist, and so does a credit whose song title names a featured artist.
  When the catalog's answer does not list them, Shijhon asks for them in the background
  after a start and every few hours, and rewrites those placeholders in place (their IDs
  stay); downloaded audio keeps its tags until it is a placeholder again. When the catalog
  cannot say, and **Each artist of a joined credit** is on, the other catalogs named for
  artist lists and the add-ons that are a catalog only are asked as for your own files
  ([below](#each-artist-of-a-joined-credit)).
- **A track found in your library** on another release (the single of an album track, say)
  plays your own file - unless that file is below your
  [minimum audio quality](configuration.md#audio-quality).
- **Your minimum audio quality** (your Audio quality page): a song whose file is below it
  plays from the add-ons when they have it.

## The daily cap on adding

An administrator can set how many catalog releases each account may add to the library
within 24 hours: **Releases a user may add each day** on the Catalog page, or
[`user_releases_per_day`](configuration.md#catalog-and-search). It is 0 by default, which
means no limit, and a change applies at once.

- A release counts for the account whose action writes its first files into the
  library, whatever the way: an app's play report, favorite, rating, playlist entry,
  queue, bookmark, share, download or converted stream; a playlist Shijhon publishes for
  the account, from the dashboard or an agent; a playlist import; the catalog's version
  of an imported album; a change on the Collection page that brings music back. An owned
  album that is filled because the account used it counts too.
- Navidrome administrators are not limited. Albums that Shijhon fills by the fill policy
  or the library pass count for nobody, and neither does anything already in the library,
  whoever added it. Whether an account is an administrator is read from Navidrome when
  its release is about to be written, and an answer is used for 5 seconds at most, so an
  account that stops being one is counted from then on, also in the middle of a request.
- The count is of the 24 hours before the request, not of a calendar day, so a new day
  brings nothing back at once. Each release stops counting 24 hours after it was added.
  The count is kept in Shijhon's database and survives a restart.
- A release that the cleanup took out needs a place again when it is added back, unless
  the same account added it within the last 24 hours: then it still has its place.
- A request takes the places for everything it would add in one step, before it writes
  anything, and keeps them until it is done. This holds for the catalog songs it names
  and for the releases it brings back by using songs the cleanup took out. So a request
  adds all of its releases or none, whatever other requests do at the same time.
- At the cap nothing is written. An app's request is answered with an error that says
  the daily limit of added releases is reached; it is never answered as done about a
  song that is not there. Catalog results are still shown, music in the library plays as
  usual, and a catalog song still plays as a plain stream, which never adds anything.
- A playlist takes its places in the order of its songs. It is published without the
  songs of the releases beyond the room, and says how many were left out. Explicit
  tracks that must all be published fail as a whole instead, with nothing added. A
  playlist import and a music import stop with that reason, keep what fitted, and go on
  from where they stopped when they are retried or resumed later.
- A scheduled refresh that would have no song left is put off. It is not a failure: the
  playlist keeps its songs, its schedule stays on, its page says that the refresh was
  put off because of the daily limit, and the next scheduled refresh tries again.
- A release the cleanup took out comes back for anyone else at once: a request refused
  at the cap leaves nothing waiting.
- Albums a connected agent adds for an account ([agent access](agent-access.md)) count
  for that account. Those over the cap are not added, and the agent is told how many
  were added and how many the limit left out.
- The Collection page tells each limited account how many releases it has added and,
  at the cap, from when it can add more. An agent reads the same from `get_capabilities`.

## Artist pictures

Navidrome looks for an artist's picture in the artist's folders and then with its external
services, and some artists end up with none: the services have no picture under that name,
or pick another artist of the same name. For those, Shijhon sets the catalog artist's
picture in Navidrome, through the same upload you can use in Navidrome's own interface.
Navidrome then serves it to every app.

The catalog artist is the one credited on the artist's songs that Shijhon added or filled,
or else the one artist of that name in the catalog with a release (an album, single or EP)
titled as one of the artist's albums in your library, or with two releases titled as two
of the artist's songs, as singles often are. Shijhon compares these titles with the
catalog's release titles only, not with the songs on those releases. When several artists
of that name in the catalog qualify, none of them is taken. A name also matches with its words in another order ("Utada
Hikaru" and "Hikaru Utada"), but never as part of a longer name, so a credit joining
several artists in one name gets no single artist's picture. An artist without such a
match gets no picture, and Shijhon asks the catalog again a month later.

Shijhon goes through the artists one at a time in the background, with a short pause
between them, starting a few minutes after it starts; a large library's first round takes
an hour or two. After that, it looks for artists without a picture every 6 hours.

A picture you replace or remove in Navidrome stays your choice: Shijhon leaves that artist
alone from then on. When Navidrome's external services later find a picture of their own
for the artist, Shijhon removes its picture so that Navidrome can show theirs. A picture
file added to the artist's folder later is not noticed; remove Shijhon's picture in
Navidrome to show it. Turning off **Pictures for artists without one** (the Catalog page,
under Advanced settings, [`artist_pictures`](configuration.md#catalog-and-search)) removes
the pictures Shijhon set, except those you changed.

## Radio from catalog items

Apps start a radio, or show similar artists, by asking the server for similar songs and
similar artists. Navidrome answers for the music in your library. It knows nothing of a
song, an album or an artist that is only in the catalog, so its answer for those is empty
and a radio started there has no songs.

Shijhon answers these requests with what two sources list as similar. ListenBrainz, on
by default for everyone ([`[listenbrainz]`](configuration.md#listenbrainz)), lists the
songs and artists its listeners play together. Last.fm lists its similar songs and
artists for a person who has set up Last.fm on the Playlists page
([playlists.md](playlists.md#discovery-playlists)), asked with that person's key. Their
lists are taken in turn: Last.fm's first song, then ListenBrainz's first, then each one's
second, and so on, each song once. A catalog song's
radio is the songs listed as similar to it, each looked up in the catalog by its artist
and title. An artist's radio, and an album's, is the artist's top songs in the catalog and
those of the similar artists, a few of each in turn. A song for which fewer than ten
similar songs are found is filled up with its artist's radio. A catalog artist's similar
artists are the ones listed: your library's artist where you have one of that name, the
catalog's artist otherwise.

For a person with Last.fm, ListenBrainz is never waited for. The first radio from an item
is made of Last.fm's songs alone, and ListenBrainz is asked in the background; its songs
are then looked up in the catalog and taken in turn with the saved list's, and the next
radio from that item has both, without Last.fm being asked again. A radio saved before
ListenBrainz was added, or while it was off or not answering, gets ListenBrainz's songs the
same way the next time it is asked for. This background work asks the catalog after the
listeners' requests, and waits, like the rest of the radio, while an app syncs or the
catalog rests after a failure. A radio made with ListenBrainz is made again when the
ListenBrainz token changes. A person without Last.fm waits for ListenBrainz,
their only source, no longer than for the catalog.

ListenBrainz has similar songs only for a song's canonical MusicBrainz recording. Shijhon
looks for it in up to three steps, each one request, and stops at the first recording that
has similar songs. First ListenBrainz Labs' lookup of the exact artist and title,
punctuation, accents and capitals aside: as the catalog names the song, without
qualifiers such as "(feat. ...)", and by its first artist alone. With a ListenBrainz token,
then ListenBrainz's own metadata lookup, which matches names more loosely. Then Labs'
recording search for the song's lead artist and title, of whose results Shijhon takes only
one credited to that lead artist with that title (the title without qualifiers), not tried
yet, preferring the one whose credit also names the song's featured artists. A result
marked as another version - live, karaoke, instrumental, a remix, acoustic, a demo and
the like - or credited to a tribute, karaoke or covers act is never taken, unless the
song's own names say the same. The exact
lookup often finds a compilation's or a single's copy of a song with a featured credit,
such as "Daft Punk feat. Pharrell Williams - Get Lucky", which has no similar songs; the
search finds the original. An artist is known by the lookup of a few of its top songs.
ListenBrainz Labs is an experimental service, so its part is optional. After a failure or
a rate limit, ListenBrainz is not asked for a minute (a rate limit for as long as it says,
ten minutes at most). Answers are kept in memory for a day.

A song of a radio that is in your library is your library's own entry. The others are
catalog songs like those in search results: they play, and are added to playlists and
favorites, the same way. The answer itself adds nothing to your library.

Shijhon saves each list and refreshes it in the background once it is older than
`[search] discography_max_age_days` (a week), so a radio asked for again answers at once.
The first request for an item waits for the sources and the catalog no longer than a search
does (`[search] budget_seconds`) and then answers with the songs found by then. The rest
are looked up in the background, two catalog requests at a time, and the next request has
the whole list. A catalog artist's page does not wait for its similar artists: the first
view shows none, they are looked up in the background, and the page has them from the next
view on.

A person who has not set up Last.fm gets ListenBrainz's songs alone. Their lists are saved
apart from those made with Last.fm, so nobody without a key is shown Last.fm's songs, and a
list made without them never stands in for one made with them. With ListenBrainz off, a
person without Last.fm gets Navidrome's empty answer, as before, and so does everyone while
`[search] radio` is off. The same holds whenever the person's first source (Last.fm with a
key, ListenBrainz without one) does not answer, or the catalog does
not answer; nothing is asked again for a minute after that. What Navidrome answers about the
songs, albums and artists in your library does not change.
[Privacy and security](privacy-and-security.md#network) says what is sent to Last.fm and
ListenBrainz.

## Each artist of a joined credit

Some files name several artists in one artist text, such as "Lumen Vale, Orrin Pike" or
"Lumen Vale & Orrin Pike", without the separate list of artists that taggers can add.
Navidrome splits a few separators itself (" / ", " feat. ", "; " and the like) but not a
comma or "&", so it shows such a text as one artist: neither person gets a page or a
picture. With **Each artist of a joined credit** on (the Catalog page, under Advanced
settings, [`artist_lists`](configuration.md#catalog-and-search); on by default), Shijhon
adds the standard list of artists (`ARTISTS`) to such files when a catalog lists the
song's artists. The artist text the file shows stays as it is.

The list comes from the catalog item linked to the song (a song of an album Shijhon
filled, say), else from the catalog itself, the other catalogs you name for artist lists,
and the add-ons that are a catalog only (listed apart on the Add-ons page; an add-on that
delivers audio is never asked, since its search can reach a service that acts on it): the
song's ISRC, and only when that
gives no list, a search by title, artist and length with the same rules Shijhon uses to
find a song at an add-on. They are asked at the same time, each at its own pace (the
catalogs at the library pass's), and their answers are read in that order: the catalog,
the other catalogs, then the add-ons. A catalog or an add-on that fails twice in a row is
not asked again until the next pass, and until then the lists of the others count without
it (a song none of them has a list for is asked about again later). The catalog's own list
counts also while another catalog or an add-on cannot be asked.

The other catalogs are installed catalog adapters, named in
[`artist_lists_catalogs`](configuration.md#catalog-and-search) (**More catalogs for artist
lists** on the Catalog page, under Advanced settings; none by default). With the
MusicBrainz adapter installed, `["musicbrainz"]` asks MusicBrainz too, whose credits name
each artist of a song. Such a catalog is used only for these lists, with its adapter's
default settings, so an adapter that needs an account or a token of its own cannot be
used this way. One that is not installed or cannot be built is noted in the log at
startup and passed over; changes apply after a restart.

A list counts only when its names, in the catalog's order, are exactly the
file's text with nothing but separators between them (commas, "&", "and", "x", ";", "/",
"+", "with"). Shijhon never splits a text on its own: "Tyler Quill, The Maker & Kali Fen"
is two artists only when a catalog names "Tyler Quill, The Maker" and "Kali Fen". A
featured artist missing from the text, a name in another script, "Various Artists", two
catalogs that give different lists, or a catalog that names one artist with the whole text
(a band such as "Salt, Ash & Ember") mean that nothing is written. Only an add-on's own
list of a song's artists counts, never a credit split on "feat.". When a catalog cannot be
asked, the song waits for the next day, also when another catalog gave a list.

A file without an album artist also gets the album artist Navidrome showed for it (its
artist text), so that the album keeps its ID; a compilation keeps "Various Artists". When
every song of an album gets the same list and the album's artist text is those names,
each file of the album also gets the list of album artists (`ALBUMARTISTS`), and the album
is listed under each person: all of the album's files or none.

Your imports and Shijhon's own songs get their lists with the setting alone. The files in
your music folder only with **Also fix files in your music folder** on too (the Catalog
page, under Advanced settings, [`artist_lists_library`](configuration.md#catalog-and-search);
off by default): the folder is read-only for Shijhon in the standard setup, so you also
make it writable ([deployment](deployment.md#what-shijhon-needs-from-navidrome),
[NixOS](nixos.md#your-music-folder)); until it is, those files are counted as read-only
and Diagnostics says so. Turning it off gives back the files Shijhon changed there, those
still as it left them, as turning the first setting off does.

Shijhon writes only the lists and that album artist; every other tag stays as it was. The
new file is a copy made beside the original under a hidden name, with the original's
permissions and owner, and it takes the original's name only once it is complete, so its
modification time changes. With Navidrome's `RecentlyAddedByModTime` on, such songs move up
among the recently added. The files of an album are written together, then Navidrome scans
their folder once, and Shijhon checks that each song kept its ID and its album and that
each artist is listed; a file for which that is not so gets its original back. Once no song
credits the joined artist any more, Navidrome drops it, with any star or rating it had.
Shijhon leaves a file alone when it is a link or has other links, lies on another disk than
its own folder, cannot be written or would get another owner, is in a format whose tags it
cannot write (or Matroska), or already lists its artists or has MusicBrainz artist
identifiers. Your [music imports](music-imports.md) are given their lists as they are
published (see
[there](music-imports.md#each-artist-of-a-joined-credit)), Shijhon's placeholders as
[above](#placeholders).

Shijhon looks at such songs in the background, a few minutes after it starts and then
every 6 hours, one at a time with a short pause. A song without a list, with differing
lists, or whose file cannot take one, is looked at again after a month (a week for a file
that could not be written); when a catalog could not be asked, the next day. Turning the
setting off takes the lists out of the files Shijhon changed, for those still as it left
them; a file changed since stays as it is, also when the setting is on again.

To see what it would do before it writes anything, start Shijhon with
`SHIJHON_ARTIST_LISTS_REPORT=1` in its environment: a few minutes after it starts it then
logs once how many songs would at most get a list, by source, and why others would not,
and writes nothing (a file can still turn out not to take its list when it is written).
The credit repair then asks the add-on catalogs nothing either. What an earlier stop left
half done is still put right.

## New releases

Each person can follow their artists on the dashboard's **New releases** page. It is off
until they turn it on, and it applies to their account alone.

Shijhon follows the artists the account favorited in Navidrome, the artists of the albums
and songs it favorited, and its most played artists. Navidrome keeps a play count and
the last play of each album, not every play. The most played artists are therefore the 25
([`new_releases_most_played`](reference/settings.md#catalog-and-search)) with the most
plays among the albums the account played in the last 90 days, counting all plays of
those albums. Shijhon reads both from Navidrome as the account, with the sign-in the
dashboard saved, in two requests a day. "Various Artists" and similar names are not
followed.

Once a day, and right after it is turned on, a background check takes each followed
artist's releases as the artist's page shows them. For several catalog artists of one
name it is the one whose releases share the most titles with your library, as on that
page. The check uses the discography saved for the page when it is less than 20 hours
old. Otherwise it asks the catalog, which is a search and one list of releases for an
artist, at the pace of the library pass and after the requests of people who are
listening. The answer is saved as the page's discography, so the page opens at once
afterwards. One check asks about 150 artists at most
([`new_releases_artists_per_day`](reference/settings.md#catalog-and-search)), starting
with those it has not looked at for the longest, so someone who follows more is covered
over several days. When the catalog fails or asks for a rest, the check ends, what was
saved stays, and the check is tried again an hour later.

A release is new when it came out in the last 30 days
([`new_releases_days`](configuration.md#catalog-and-search)) and the artist's page would
add it to your library's albums: one card per album by the edition rules, and none for an
album you have. A date without a day counts as the first of its month, and a release
dated by its year alone is never new. A release announced for a later day shows from
that day on.

In your apps, the new releases come first in the list of newest albums (`getAlbumList2`
with `type=newest`, in JSON and XML), the latest release first, followed by Navidrome's
own list. They are catalog albums like those in a search: they open, play and take
favorites the same way, and nothing is written to your library until you use one. The
list is answered from what the check saved, so an app's request asks the catalog nothing.
Pages count the new releases first, so no album repeats or goes missing between pages.
A release leaves the list once it is in your library, where Navidrome lists it itself, or
once it is older than the 30 days. When another account added the album and your library
does not show it, the release stays in your list and opens that album.

The followed artists and the releases found are kept per account in Shijhon's database.
Other accounts never see them, and turning the feature off removes them.

## Accounts for sound analyzers

A sound analyzer such as AudioMuse-AI signs in like any app, lists your library and
downloads every song to analyze it. Give it a Navidrome account of its own (with access to
your library) and name that account in
[`[placeholders] real_audio_accounts`](reference/settings.md#top-level-server-navidrome-placeholders):

```toml
[placeholders]
real_audio_accounts = ["audiomuse"]
```

That account sees only songs whose audio is in your library: your own and imported files,
and catalog songs whose audio was downloaded into the library. Shijhon adds nothing for it:

- Lists, searches, albums, artists, playlists, favorites and play queues leave out
  placeholders and catalog results, and their song counts and lengths count only what
  remains. Albums and artists without such songs are not shown, and albums are not shown
  complete.
- Playing or downloading a placeholder or a catalog song answers "not found". It never
  starts a download from your add-ons, and nothing is added to the library. Downloaded
  audio plays from the library as usual.
- Sharing and the jukebox are off for that account, as when they are off in Navidrome.
  Share links it made earlier that hold songs without real audio answer "Share not
  found".

This applies to what apps ask through the Subsonic API, as analyzers do; Navidrome's own
website is not limited this way, so use the account only for the analyzer. Every other
account is unchanged.

## Matching and filling

- Albums partly in your library are matched against the catalog when they are first
  shown, and by a background pass over the whole library.
- Albums with no match are checked again after matching rules or your files' metadata
  change, or after seven days. The Library page lists them under **No match**;
  **Match again** checks one album without adding missing tracks.
- When several releases fit an album, or the one that fits numbers its tracks differently
  from your files, Shijhon picks the most likely: the release with the most of your tracks
  at their own track numbers (or with their ISRCs), then the one with your album's exact
  title, its year, and its clean or explicit kind, then the one the catalog lists first.
  Its tracks keep the release's numbers, even where one of your files has the same number.
  The dashboard's Library page lists these picks, which can be changed there
  ([`auto_pick`](configuration.md#fill-and-cleanup) turns picking off).
  With picking on, a manual choice can also use a release numbered differently from your
  files; added songs keep its numbers, and your files stay unchanged.
- Other uncertain matches go to the review list on the Library page: for example, when
  your tracks are only on releases with other titles, such as a compilation. Such an album
  stays as it is, and searches and artist pages show the catalog's versions of it beside
  it, and those play. The same goes for albums without a match, and for albums you
  keep as they are. Matching one of these albums to a usable release hides the separate
  catalog editions again.
- A filled album can be filled from another release that fits it (**Wrong edition?**
  on the Library page, among the picks and the filled albums). Shijhon first takes out
  the songs the fill added, by the cleanup's rule: only when nobody has used any of them,
  by Navidrome's usage records read at that moment and again once the files are gone.
  Otherwise the album stays as it is, with every song. The album is then shown complete
  with the chosen release and filled from it at once; other albums of yours whose songs
  are all on that release become its parts, and one that was a part of the old release
  only is matched again by itself. If the fill fails or Shijhon stops in between,
  Shijhon finishes it by itself: at its next start, and after a failure three times more
  (after one minute, ten minutes and an hour). If that keeps failing, the album stays
  shown complete and is filled when one of its songs is next used; the Library page says
  so meanwhile.
- The song IDs of the old release can still be in an app. Shijhon looks at the requests
  that record a use on a song: a favorite, a rating, a play report, a playlist or queue
  entry, a bookmark and a share. One that names such an ID goes on at once where a song
  has that ID again: Navidrome gives a song of the chosen release the ID the old
  release's song had when both are in the album under the same disc, number and title.
  That is Navidrome's own rule, and it does not compare the recordings: a song that
  differs between the editions under the same number and title (another mix, a clean
  version) keeps the ID, and with it any favorite, rating or playlist entry made for that
  ID from then on. While the replacement is under way or unfinished, the request waits
  for it, at most as long as a request for audio may wait
  ([`max_wait_seconds`](configuration.md#delivery)), then is answered with an error to
  try again later while the replacement goes on. A request for an ID that no song has
  is answered "not found". None of these requests is passed on while the ID is no song,
  nor when Navidrome cannot be asked whether it is one (the answer is then to try again
  later): Navidrome would record the use on a song that is gone. Shijhon keeps these IDs
  for as long as no song has them, since Navidrome keeps the missing songs too.
- Everything else is Navidrome's to answer for such an ID, with its own errors: a
  stream, a download, a transcode, the jukebox, a view, and taking a favorite or a rating
  back, which records no use and must stay possible. The requests of an account without
  access to Shijhon's library are Navidrome's altogether, as all its requests are.
- A matched album is shown complete, with the complete album's song count and length.
  Nothing else Navidrome says about your own music is changed, and your files are never
  modified unless you turn on **Also fix files in your music folder**
  ([below](#each-artist-of-a-joined-credit)).
- Its missing tracks are written as placeholders into the same album when you first use
  the album or one of them. They are also added automatically for albums you have enough
  of (3 songs, or a quarter of the album), depending on `[fill] library_pass`:
  - `"dry_run"`, the default: the pass records what it would fill, and fills nothing;
  - `"on"`: the pass fills those albums, and so does opening one in an app;
  - `"off"`: no pass; such an album is filled when it is shown.
- Using a catalog release also checks for an existing album of the same artist and
  title, even if an earlier check found no match. When exactly one eligible album fits
  every owned recording, Shijhon adds the missing tracks there and keeps your files and
  song IDs. Apps do not get the same album twice: when your album of the same name is
  the release's album but has a recording the release does not (your live version of a
  song, say), the release's songs your album lacks are added into it, beside your own,
  with the release's track numbers. It is the same album when they share a song; sharing
  none, when their years and covers do not clearly differ (a second disc on its own).
  Another album of the same name - another year, or clearly another cover - and another
  edition ("Deluxe Edition") are added as albums of their own, unless Navidrome would put
  the release's songs into your album (the same album artist, title and release date).
  When several albums fit, or the album cannot take the release (you keep it as it is,
  it is in the review list or already filled, or - for an edition of another name - its
  files say every track is there), nothing is added and it stays blocked for review in
  Library.

### Track lists read again

A release in your library keeps the track list the catalog had when it was added. After
its albums, each run of the library pass reads the lists of some of these releases again,
both catalog albums you added and the releases that filled your own albums.

- Each release is read about once a month, and once a week while it is new, which means
  that its release date lies ahead or no more than two months back. A release is first
  read that long after it was added. A run reads only a share of the releases that are
  due, so a large library is spread over many runs, and each request waits its turn in
  the pass's pace.
- A track the catalog has added since becomes a placeholder in the same album. A song
  that has no audio yet gets its length when the catalog corrected it. Its ID, favorites,
  ratings, plays and playlist entries stay.
- Nothing else about a song changes. Its title, artists, ISRC and explicit mark stay as
  they were written, and it keeps its disc and track number, also when the catalog lists
  it differently now. The Library page counts such differences as left as they are.
- A new track is added only where no song of the album is, so a track the catalog puts
  between two others is left out.
- A track the catalog lists under a new ID is still the song your album has. A new track
  with the ISRC of a song of the album, or with its title where no ISRC tells them apart,
  is not added a second time.
- Nothing is removed. A track the catalog no longer lists stays, and so does a release
  the catalog no longer has.
- Your own files are never changed, and a song with downloaded, kept or imported audio
  keeps its file and its tags. A new track with the title of a file you added to the
  album yourself is not added beside it.
- In an album that was filled, a track list the catalog could not read in full changes
  nothing. Neither does one without track numbers that lists the songs in another order
  than the album has them.
- As a dry run, which is the pass's mode until you switch it on, the pass writes nothing
  and counts what it would add and correct. Once it is switched on, its next run applies
  that.
- When the catalog fails, nothing changes, the catalog rests as after a failed match, and
  the releases wait for a later run.

The Library page says how many releases have had their track list read again, and what
their latest readings added, corrected and left as it is. These are not totals over time:
a release's next reading replaces its numbers.
[`[fill] refresh_track_lists`](configuration.md#fill-and-cleanup) turns this off.
Releases added from another catalog than the one in use are not read again.

## The cleanup

Off by default. After a set number of days, it removes catalog releases and fills that
nobody has used. It takes out whole releases only. A release stays once a daily check has
seen any user use it: a favorite, a rating, a play, a playlist entry, a queue, a bookmark
or a share, even one removed since. The daily checks note these uses whether the cleanup
is on or not, so set the [usage export](deployment.md#the-usage-export) up early. A
release taken out comes back, normally with the same song IDs, when an app uses one of its
old IDs in a way that adds songs to the library. A removed fill leaves its album shown as
complete.
