# How it works

Your apps talk to Shijhon as they would to Navidrome, and Shijhon passes their requests on
unchanged. It adds results from the catalog to the answers for searches, artist pages and
albums. Navidrome checks every login.

## Placeholders

- **When you use a catalog track**, Shijhon writes its whole release into a **placeholder
  folder** in your library: silent FLAC files with the right tags. Any of these counts as
  a use: the app's "now playing" report, a favorite, a rating, a playlist entry, a
  bookmark, a share, a download, or a stream that has to be converted. Navidrome scans the
  files, so they become normal tracks with real IDs. Looking at catalog items, and playing
  them as plain streams, writes nothing.
- **Streams** come from your add-ons, and seeks are served from what Shijhon has received
  ([add-ons](add-ons.md#the-plan)). They are not stored in your library unless **Keep
  streamed songs** is on (the Cleanup page,
  [`keep_streamed_audio`](configuration.md#delivery)). With it on, a song whose whole
  audio Shijhon received replaces its placeholder, as a download does, and Navidrome
  serves it from then on. If the app disconnects before Shijhon has received the whole
  song, the song is not kept.
- **Downloads, conversions, the jukebox and share links** need the whole song. A
  conversion is a stream in another format, or at a lower bitrate than the add-on's audio
  has. Shijhon fetches the song first and puts it in place of the placeholder, and
  Navidrome serves it from there. Saved songs, kept streams included, turn back into
  silent placeholders after 30 days unused. Once saved audio takes more than 10 GB, the
  least recently used turn back first ([`delivered_days`,
  `delivered_gb`](configuration.md#delivery)).
- **A song or album credited to several artists** ("A & B") gets each artist named in its
  tags when the catalog lists them. Navidrome then gives A and B their own pages and
  pictures instead of an artist called "A & B". A band whose name has an "&" or a comma
  stays one artist. When
  the catalog's answer does not list the artists, Shijhon looks for them later in the
  background ([detailed rules](reference/how-it-works.md#placeholders-of-joined-credits)).
- **A track found in your library** on another release (the single of an album track, say)
  plays your own file.
- **A song whose file is below your [minimum audio quality](configuration.md#audio-quality)**
  (set on the Audio quality page) plays from the add-ons when they have it.

## The daily cap on adding

An administrator can set how many catalog releases each account may add to the library
within 24 hours: **Releases a user may add each day** on the Catalog page, or
[`user_releases_per_day`](configuration.md#catalog-and-search). It is 0 by default, which
means no limit, and a change applies at once.

A release counts for the account whose action first writes it into the library, however
that happens: an app's play report, favorite or playlist entry, a download, a playlist
Shijhon publishes, an import or an agent's addition. Navidrome administrators are not
limited. Albums that Shijhon fills on its own, by the fill policy or the library pass,
count for nobody. The count covers the 24 hours before a request, not a calendar day.

At the cap nothing is written, and apps get an error that says the daily limit of added
releases is reached. Catalog songs still play as plain streams, and music in the library
plays as usual. A playlist Shijhon makes is published without the songs whose releases
are over the cap, and it says how many were left out. A playlist import or a music import
stops at the cap, keeps what fitted, and goes on from there when it is retried or
resumed. The Collection page tells each limited account how many releases it has added
and, at the cap, from when it can add more. The
[detailed rules](reference/how-it-works.md#the-daily-cap-on-adding) cover requests that add
several releases, scheduled refreshes, imports and agents.

## Artist pictures

Artists without a picture in Navidrome get the catalog artist's picture, and every app
shows it. These are artists whose folders have no picture and for whom Navidrome's
external services find none. Shijhon sets the picture through the same upload Navidrome's
own interface offers.

Shijhon uses a catalog artist's picture only when it can tell it is the same artist. That
is the artist credited on songs Shijhon added, or the only one of that name whose release
titles match the artist's albums or songs in your library. An artist without such a match
gets no picture, and the catalog is asked again a month later ([how the artist is
chosen](reference/how-it-works.md#artist-pictures)).

Shijhon goes through the artists in the background, one at a time with a short pause
between them. The first round begins a few minutes after Shijhon starts and, for a large
library, takes an hour or two. After that, Shijhon looks for artists without a picture
every 6 hours.

A picture you replace or remove in Navidrome stays your choice: Shijhon leaves that artist
alone from then on. When Navidrome's external services later find a picture of their own
for the artist, Shijhon removes its picture so that Navidrome can show theirs. Shijhon
does not notice a picture file added to the artist's folder later. To show that picture,
remove Shijhon's picture in Navidrome. Turning off **Pictures for artists without one**
(the Catalog page, under Advanced settings,
[`artist_pictures`](configuration.md#catalog-and-search)) removes the pictures Shijhon
set, except those you changed.

## Radio from catalog items

Radio and similar artists also work for music that is only in the catalog. They come from
what ListenBrainz and Last.fm list as similar. For music in the library, Navidrome's own
radio and similar artists stay as they are.

ListenBrainz lists the songs and artists its listeners play together. It is on by default
for everyone ([`[listenbrainz]`](configuration.md#listenbrainz)). Last.fm lists similar
songs and artists. It is asked only for a person who has set up Last.fm on the Playlists
page ([playlists.md](playlists.md#discovery-playlists)), with that person's key. A catalog
song's radio is the songs listed as similar to it, found in the catalog. An artist's
radio, and an album's, mixes the top songs of the artist and of similar artists. For each
similar artist of a catalog artist, Shijhon shows your library's artist of that name if
you have one, and the catalog's artist otherwise.

A radio song that is in your library appears as your library's own entry. The others
are catalog songs like those in search results, and they play and go into playlists and
favorites the same way. Getting a radio adds nothing to your library.

The first radio from an item waits no longer than a search (`[search] budget_seconds`)
and starts with the songs found by then. Shijhon saves each radio and refreshes it in the
background, so a radio asked for again answers at once and has the rest. A catalog
artist's similar artists appear from the second time its page is opened.

When ListenBrainz is off, a person without Last.fm gets Navidrome's empty answer. Everyone
gets that answer while `[search] radio` is off. What Navidrome answers about the music in
your library does not change. The [detailed
rules](reference/how-it-works.md#radio-from-catalog-items) say how the two lists are
combined, how ListenBrainz's songs are found and when a source is not asked. What is sent
to Last.fm and ListenBrainz is listed under [privacy and
security](privacy-and-security.md#network).

## Each artist of a joined credit

**Each artist of a joined credit** is on by default (the Catalog page, under Advanced
settings, [`artist_lists`](configuration.md#catalog-and-search)). With it, a song credited
to "Lumen Vale & Orrin Pike" or "Lumen Vale, Orrin Pike" gives each of them a page and a
picture of their own, instead of one artist named after the whole credit. When a catalog
lists the song's artists, Shijhon adds them to the file as the standard list of artists
(`ARTISTS`). The artist text the file shows stays as it is. Credits joined with " / ",
" feat. " or "; " already show as separate artists, since Navidrome splits those itself.

The list comes from a catalog: the catalog in use, any catalog adapters named in
**More catalogs for artist lists**
([`artist_lists_catalogs`](configuration.md#catalog-and-search); none by default), and the
add-ons that are a catalog only. Shijhon uses a list only when the catalog's names, with
nothing but separators between them, are exactly the file's text. A band such as "Salt,
Ash & Ember" therefore stays one artist, and Shijhon never splits a text on its own. It
writes only the lists and, where a file has no album artist, the album artist. Every other
tag stays as it was.

Your imports and Shijhon's own songs get their lists with the setting alone. Files in your
music folder get them only when **Also fix files in your music folder** is on too (the
Catalog page, under Advanced settings,
[`artist_lists_library`](configuration.md#catalog-and-search); off by default), and the
folder is writable for Shijhon
([deployment](deployment.md#what-shijhon-needs-from-navidrome),
[NixOS](nixos.md#your-music-folder)). A changed file gets a new modification time, so with
Navidrome's `RecentlyAddedByModTime` on, its songs move up among the recently added. Once
no song credits the joined artist ("A & B") any more, Navidrome drops that artist, with
any star or rating it had. Turning either setting off restores the files Shijhon changed,
as long as they are still as Shijhon left them.

Shijhon looks at such songs in the background, a few minutes after it starts and then
every 6 hours. To see what it would do before it writes anything, start Shijhon with
`SHIJHON_ARTIST_LISTS_REPORT=1`: it then logs how many songs would get a list, and writes
nothing. The [detailed rules](reference/how-it-works.md#each-artist-of-a-joined-credit)
cover the sources' order, which files are left alone and how a write is checked.

## New releases

For each account that turns on **New releases** ([your
collection](collection.md#new-releases)), Shijhon follows the artists the account
favorited in Navidrome, the artists of its favorite albums and songs, and its 25 most
played artists. Once a day it checks their releases in the background. A release from the
last 30 days ([`new_releases_days`](configuration.md#catalog-and-search)) counts as new
when your library's albums lack it.

In apps, the new releases come first in the list of newest albums, followed by
Navidrome's own list. They are catalog albums like those in a search, and nothing is
written to your library until you use one. A release leaves the list once it is in your
library or older than the 30 days. The followed artists and the releases found are kept
per account; other accounts never see them, and turning the feature off removes them.
The [detailed rules](reference/how-it-works.md#new-releases) say how the most played
artists are counted, how many artists a check asks about and which releases count as
new.

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
and catalog songs whose audio was downloaded into the library. Placeholders and catalog
results are left out of everything it lists, and a request to play one gets "not found"
without starting a download. Sharing and the jukebox are off for it. These limits apply to
requests through the Subsonic API, which analyzers use. Navidrome's own website is not
limited this way, so use the account only for the analyzer. Every other account is
unchanged ([in detail](reference/how-it-works.md#accounts-for-sound-analyzers)).

## Matching and filling

- Albums partly in your library are matched against the catalog when they are first
  shown, and by a background pass over the whole library. A matched album is shown
  complete, with the complete album's song count and length. Nothing else Navidrome says
  about your own music is changed, and your files are never modified unless you turn on
  **Also fix files in your music folder** ([above](#each-artist-of-a-joined-credit)).
- When several releases fit an album, Shijhon picks the most likely. The dashboard's
  Library page lists these picks, which can be changed there
  ([`auto_pick`](configuration.md#fill-and-cleanup) turns picking off).
- Uncertain matches, such as tracks found only on a compilation, go to the review list on
  the Library page. Until one is settled, the album stays as it is, and searches and
  artist pages show the catalog's versions next to it. Albums with no match are
  listed under **No match** and checked again after seven days, or sooner when your files
  or the matching rules change.
- A matched album's missing tracks are written as placeholders into the same album when
  you first use the album or one of those tracks. They are also added automatically for
  albums you have enough of (3 songs, or a quarter of the album), depending on
  `[fill] library_pass`:
  - `"dry_run"`, the default: the pass records what it would fill, and fills nothing;
  - `"on"`: the pass fills those albums, and so does opening one in an app;
  - `"off"`: no pass; such an album is filled when it is shown.
- A filled album can be filled again from another release that fits it (**Wrong
  edition?** on the Library page). This works as long as nobody has used the songs the
  first fill added.
- When you use a catalog release of an album you already have in part, its missing tracks
  are added to your album, keeping your files and song IDs. This happens only when exactly
  one album fits.

The [detailed rules](reference/how-it-works.md#matching-and-filling) cover how a release is
picked, what happens to song IDs after a change of edition, and when a catalog release
joins an album you have.

### Track lists read again

A release in your library keeps the track list the catalog had when it was added. The
library pass reads these lists again from the catalog, about once a month, or once a week
while a release is new. It adds tracks the catalog has added since as placeholders in the
same album, and corrects the length of songs that have no audio yet. Nothing is removed or
renumbered, and your own files never change. In dry run mode the pass only counts what it
would do. The Library page shows what the latest readings added and corrected, and
[`[fill] refresh_track_lists`](configuration.md#fill-and-cleanup) turns this off
([detailed rules](reference/how-it-works.md#track-lists-read-again)).

## The cleanup

The cleanup is off by default. When it is on, it removes catalog releases and fills that
nobody has used, after a set number of days. It takes out whole releases only. A release
is kept once a daily check has seen any user use it: a favorite, a rating, a play, a
playlist entry, a queue, a bookmark or a share, even one undone since. The daily checks
note these uses whether the cleanup is on or not. They read them through the [usage
export](deployment.md#the-usage-export), so set that up early. A release taken out comes
back, normally with the same song IDs, when an app uses one of its old IDs in a way that
adds songs to the library. A removed fill leaves its album shown as complete.
