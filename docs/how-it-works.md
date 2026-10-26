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
- **Streams** come from your add-ons and are not stored in your library - unless
  **Keep streamed songs** is on (the Cleanup page, [`keep_streamed_audio`](configuration.md#delivery)):
  then a song whose whole audio Shijhon received while an app streamed it replaces its
  placeholder, as a download does, and Navidrome serves it from then on. Shijhon fetches
  nothing more for this; a song skipped before Shijhon had all of it is not kept. A play's start and
  its seeks count together when they are of the same file at the add-on.
- **Downloads and conversions** (to another format, or a lower bitrate than the add-on's
  audio has), Navidrome's jukebox and its share links: Shijhon fetches the whole song
  first and puts it in place of the placeholder, and Navidrome serves it from there. After
  30 days unused, the song (also a kept stream) is replaced by its silent placeholder
  again; once such audio
  takes more than 10 GB in total, the least recently used are replaced first
  ([`delivered_days`, `delivered_gb`](configuration.md#delivery)).
- **A track found in your library** on another release (the single of an album track, say)
  plays your own file - unless that file is below your
  [minimum audio quality](configuration.md#audio-quality).
- **Your minimum audio quality** (your Audio quality page): a song whose file is below it
  plays from the add-ons when they have it.

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
  The dashboard's Library page lists these picks, and you can change them there until the
  album is filled ([`auto_pick`](configuration.md#fill-and-cleanup) turns picking off).
  With picking on, a manual choice can also use a release numbered differently from your
  files; added songs keep its numbers, and your files stay unchanged.
- Other uncertain matches go to the review list on the Library page: for example, when
  your tracks are only on releases with other titles, such as a compilation. Such an album
  stays as it is, and searches and artist pages show the catalog's versions of it beside
  it, so you can play those. The same goes for albums without a match, and for albums you
  keep as they are. Matching one of these albums to a usable release hides the separate
  catalog editions again.
- A matched album is shown complete, with the complete album's song count and length.
  Nothing else Navidrome says about your own music is changed, and your files are never
  modified.
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

## The cleanup

Off by default. After a set number of days, it removes catalog releases and fills that
nobody has used. It takes out whole releases only. A release stays once a daily check has
seen any user use it: a favorite, a rating, a play, a playlist entry, a queue, a bookmark
or a share, even one removed since. The daily checks note these uses whether the cleanup
is on or not, so set the [usage export](deployment.md#the-usage-export) up early. A
release taken out comes back, normally with the same song IDs, when an app uses one of its
old IDs in a way that adds songs to the library. A removed fill leaves its album shown as
complete.
