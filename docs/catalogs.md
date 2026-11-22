# Catalogs

The catalog is the music outside the library that apps show: search results, artist
discographies, album track lists, an artist's top songs and covers. Its audio comes from
[add-ons](add-ons.md). There is no catalog until one is chosen.

The recommended catalog is an add-on's. Any add-on with a catalog of its own (a search,
albums, artists) can serve as the catalog, with nothing to install. A *catalog adapter*,
a separate package built into Shijhon's image, is the legacy way, and remains supported
([below](#catalog-adapters-legacy)).

## The catalog of an add-on

On the dashboard's **Catalog** page, choose **An add-on's catalog** and save, choose the
add-on and save again, and press **Restart Shijhon**. No image rebuild is needed. Or in
the configuration file:

```toml
[catalog]
kind = "addon"
addon = "My add-on"       # its name, as on the Add-ons page or in [[addons]]
```

An add-on can supply a catalog and audio, or a catalog only. The Add-ons page lists
catalog-only add-ons apart, under **Catalog only** ([add-ons](add-ons.md#adding-an-add-on)).
The separate [Shijhon Mornye extension bridge](https://github.com/Jasshl/shijhon-mornye-bridge)
makes Mornye extensions usable as add-ons, including as catalogs when an extension
supplies one. It is not affiliated with Mornye. To write an add-on with a catalog, see
[for add-on authors](add-ons.md#for-add-on-authors).

- Catalog requests count against that add-on's [limits](add-ons.md#limits): a search, an
  album and an artist page take one request each, plus any release pages.
- Renaming the add-on on the Add-ons page keeps its catalog, and changes `addon` too.
- Choosing another add-on starts the matches and saved catalog lists over, unless both
  share a [confirmed identity](#a-confirmed-catalog-identity). What is in your library
  stays and plays, and albums not filled yet are matched again. If you change an add-on's
  setup so that it serves a different catalog, remove it and add it again under another
  name.
- Apps get the add-on's image addresses directly only if the add-on opts in, and only for
  public HTTPS hosts. Other artwork reaches apps through Shijhon.

Playlist imports also search the catalogs of the other enabled add-ons that have one, for
entries that neither your library nor the configured catalog has
([playlist imports](playlist-imports.md#add-on-catalogs)). This does not change the
configured catalog and needs no restart.

### The full-catalog extension

An add-on with the optional
[full-catalog extension](reference/add-on-protocol.md#the-full-catalog-extension) can
supply disc and track numbers, release kinds, clean and explicit versions, song and ISRC
lookups, and paged discographies. An add-on without it has less to go on, with the
limits in [known issues](known-issues.md#the-catalog-of-an-add-on).

### A confirmed catalog identity

A confirmed identity lets you move from an adapter to an add-on, or from one add-on to
another, without matches and saved catalog lists starting over. A full-catalog add-on can
say which catalog it serves, as a key and optionally a region. Once you confirm that, what
Shijhon learned from it is kept under the catalog rather than under the add-on. Without a
confirmed identity, what Shijhon learned stays tied to the add-on it came from.

The Catalog page reads the chosen add-on's manifest and shows "This add-on says its
catalog is …". When you choose another add-on, its claim is shown before you save the
choice. Confirm an identity only when the add-on uses that catalog's own item IDs with the
same meaning. To confirm it, select **Confirm this identity**, save, and restart Shijhon.
In a configuration file, confirm it yourself:

```toml
[catalog]
kind = "addon"
addon = "My add-on"
identity = "examplemusic.us"  # the declared key.region; key alone without a region
```

Leaving the identity empty keeps what Shijhon learned under the add-on's key. Confirming
the identity later changes the catalog key, so matches and saved catalog lists start over.
Library music stays and plays. While an adapter is still the configured catalog, an add-on
that declares the same key gets no special treatment: songs are looked up at it as at any
other add-on.

Shijhon checks the add-on's claim again from time to time. If the claim has changed or is
gone, catalog requests stop, and apps get answers from the library alone until you check
and confirm the identity on the Catalog page. Shijhon never moves to another catalog key
on its own, and library music stays in place. The [identity
rules](reference/add-on-protocol.md#the-full-catalog-extension) say when the claim is
checked.

## A catalog that answers slowly

A search, the page of an artist in your library and an artist's top songs wait up to 8
seconds for the catalog ("Wait for the catalog" on the Catalog page, `[search]
budget_seconds`). When it takes longer, the answer has only your library's results, and
the same search a moment later has the catalog's too. Raise the wait for a catalog
that needs longer. Pages of artists and albums not in your library wait until the
catalog answers or fails. After a search or an artist page found the catalog unavailable
or busy, searches are answered only from the library for half a minute.

A [radio from a catalog item](how-it-works.md#radio-from-catalog-items) waits the same
time and then starts with the songs found by then. The rest are looked up in the
background, and the same radio asked for again has them all.

## Catalog adapters (legacy)

A catalog adapter is a separate Python package, built into Shijhon's image, that makes
one catalog available to Shijhon. An adapter supplies a catalog only, and its songs play
through your add-ons. Adapters remain supported, but add-on catalogs need no rebuild, and
an add-on can supply the catalog and the audio together. To move from an adapter to an
add-on that serves the same catalog, see [a confirmed catalog
identity](#a-confirmed-catalog-identity).

### Installing an adapter

With the Compose file, set `SHIJHON_ADAPTERS` in `packaging/.env` to the address of the
adapter's source archive or wheel. `setup.sh` asks for it too. Then build the image again
from `packaging/` with `docker compose up -d --build`. Afterwards,
`docker compose exec shijhon shijhon catalogs` lists the adapter. An adapter that is not
published is installed from its wheel ([deployment](deployment.md#catalog-adapters)). On
NixOS, see [its reference](reference/nixos.md#catalog-adapters). To write one, see
[development](development.md#writing-a-catalog-adapter).

### Choosing an adapter

On the dashboard's Catalog page, choose an installed adapter and save, enter the
settings the page then shows and save again, and press **Restart Shijhon**. Or in the
configuration file:

```toml
[catalog]
kind = "example"          # the adapter's name
# ... and the adapter's own settings (its region, its credentials), as its notes say
```

- An adapter's settings belong to its `kind`. Choosing another catalog on the dashboard
  removes what was saved for the one before.
- If `kind` names an adapter that is not installed, Shijhon does not start, and its error
  lists the installed adapters.
- Removing an adapter leaves the songs added from its catalog in the library, and they
  keep playing. New searches, artist pages and fills need a catalog again.
- An adapter's notes should say whether it needs a longer
  [wait for the catalog](#a-catalog-that-answers-slowly).

### The MusicBrainz adapter

An example adapter: [`shijhon-catalog-musicbrainz`](https://github.com/Jasshl/shijhon-catalog-musicbrainz),
for [MusicBrainz](https://musicbrainz.org), the open music encyclopedia, with its own
repository and notes. It needs no account or key (`kind = "musicbrainz"`). To install it,
from `packaging/`:

```sh
echo 'SHIJHON_ADAPTERS=https://github.com/Jasshl/shijhon-catalog-musicbrainz/archive/refs/tags/v0.1.0b1.tar.gz' >> .env
docker compose up -d --build
docker compose exec shijhon shijhon catalogs       # lists "musicbrainz"
```

Then choose MusicBrainz on the dashboard's Catalog page and press **Restart Shijhon**.

MusicBrainz allows one request a second, so a first search takes four to five seconds;
once Shijhon has cached a page, it appears at once. It has no artist pictures, and some
albums have no cover. An artist's top songs need a ListenBrainz token, a setting of the
adapter.

An installed adapter can also be used only to find the artists of joined credits
("A & B"), beside the catalog you use. Name it in
[`artist_lists_catalogs`](configuration.md#catalog-and-search)
([how it works](how-it-works.md#each-artist-of-a-joined-credit)).
