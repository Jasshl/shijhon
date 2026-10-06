# Development

[CONTRIBUTING.md](../CONTRIBUTING.md) has the rules for changes.

## Setup

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 and the dependencies)
- ffmpeg on `PATH`, for the tests' synthetic audio
- Network access on the first test run: the pinned Navidrome release and the Subsonic XML
  schema are downloaded once, checked against their SHA-256 and cached in
  `~/.cache/shijhon` (`SHIJHON_TEST_CACHE` changes the location)

```sh
uv sync
uv run pytest -n auto      # all default suites, in parallel
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

To run from source: `uv run shijhon --config /path/to/shijhon.toml serve`, with a
configuration kept outside the repository.

## Checks

- `tests/unit/`: single components.
- `tests/acceptance/test_<letter>_*.py`: against a real, disposable Navidrome. A, requests
  passed on as they are; B, credentials and who the caller is; C, placeholder audio and
  adding releases; D, tags; E, IDs that survive; F, owned files left alone; G, catalog
  views; I, fills and the library pass; J, search and catalog entries, in JSON and XML;
  K, delivery from add-ons; L, owned recordings behind placeholders; M, the cleanup and
  the usage export; N, the network policy; Q, the dashboard; R, stops in the middle of
  library writes.
- `tests/harness/`: what the suites share: a Navidrome per test module, synthetic albums,
  a test client for every login style, a scriptable add-on, the made-up catalog's
  records with injected failures.
- `tests/demo-catalog`: the made-up catalog the tests use (`kind = "demo"`), a
  development-only package that the wheel and the image do not contain.

Markers: `canary` (the suites a Navidrome upgrade must pass,
[below](#a-new-navidrome-version); also in the default run), `live` (opt-in, real add-ons
or catalogs), `client` (playback in a real audio player).

- **Timing tests can fail once on a busy machine**, most often among `test_K_*`. Run a
  failing one alone before you conclude anything.
- **The three tests in `tests/client/` need a Mac that can play audio.** With the screen
  locked or the lid closed they fail.
- **Tests that make a folder read-only fail when run as root.**

### The setup script

`setup.sh` is POSIX `sh`, for the shells and tools Linux and macOS come with.
`tests/unit/test_setup_script.py` runs it with a stand-in `docker` that records every
call. `uvx --from shellcheck-py shellcheck -s sh setup.sh` must be clean. Rehearse a change
with real Docker too, in a temporary checkout with a music folder and a Compose project
name (`COMPOSE_PROJECT_NAME`) of its own, and remove its volumes afterwards.

### Tests with real add-ons and an audio player

- `SHIJHON_LIVE_CONFIG=/path/outside/repo/devserver.toml uv run pytest -m live` tests each
  configured add-on alone (cold start, seek, replay, `HEAD`), and with
  `SHIJHON_LIVE_SEARCH=<term>` the configured catalog. Timings and status codes, never
  URLs or settings, are written next to the configuration.
- `tests/client/` runs `tools/avcheck.swift`, a headless AVFoundation player, against
  Shijhon with the fake add-on: play, seek and download. It runs with the default suite on
  macOS with the Swift toolchain, and is skipped elsewhere.

### Checks with real apps

The suites show what the server answers; what an app does with it is only established with
the app. For a change to what apps see, go through this with the apps you have, one of
them an app that reads the API as XML, and note each app's version and result:

1. Sign in with a password and with a salted token.
2. Search: catalog results after your own, no duplicate albums, artist pictures.
3. An artist page: your releases and the catalog's, singles and EPs labeled, no
   duplicates.
4. A partly owned album: the complete track list; your tracks play your files; the
   album's cover and type unchanged.
5. A catalog album: play, seek, skip; the next track starts quickly.
6. Favorite a catalog track; add catalog tracks to a playlist (one twice); look at
   both in Navidrome and on a second device.
7. The play queue on a second device; scrobbles and play counts.
8. Save a catalog track for offline use; play one at a lower bitrate setting.
9. Playback in a car interface, where the app has one.
10. A placeholder whose add-on delivers another format than its silent file's (AAC for a
    FLAC placeholder) plays.
11. What Navidrome does alone stays as it is: lyrics, genres, random and starred lists,
    your own covers.

## The development instance

`tools/devserver.py` runs the pinned Navidrome and Shijhon, registers add-ons and writes
chosen catalog releases as placeholders, for checks with real apps. Copy
`tools/devserver.example.toml` **outside the repository** (it holds add-on addresses) and
run:

```sh
uv run python tools/devserver.py --config /path/outside/repo/devserver.toml --state var/devserver
```

State survives restarts (`--reset` starts over). Requests are logged without credentials
in `<state>/requests.log`.

## Source map

Under `src/shijhon/`:

| Package | What is in it |
|---|---|
| `app.py`, `cli.py`, `config.py`, `log.py` | The ASGI app, the command line, the settings, logging without credentials |
| `proxy/` | The app-facing proxy: forwarding, credentials first, who the client is, Subsonic answers in JSON and XML |
| `navidrome/` | Shijhon's own calls to Navidrome, startup checks, scans, usage and the usage export |
| `catalog/` | The catalog interface, adapters as plugins, the contract kit, caching, edition de-duplication, covers, the catalog of an add-on |
| `views/` | Catalog items as apps see them: IDs, entries shaped like Navidrome's, additions to searches and artist pages, albums shown complete, actions that add a release |
| `placeholders/` | Writing releases into the library and recovering interrupted writes; the silent files and their tags |
| `matching/`, `fill/` | Matching an owned album with a catalog release; fills and the library pass |
| `delivery/` | Add-ons: the protocol client, routing and streaming, DASH, download-first, limits, warm-ahead, the network policy |
| `cleanup.py` | Taking unused releases out again |
| `dashboard/` | The dashboard, generated from the settings' definitions |
| `store/` | SQLite access, migrations, the one-writer lock |

[Known gaps](reference/known-gaps.md) lists what is known not to be right yet.

## The add-on protocol

The protocol is the one Eclipse Music uses; its
[add-on developer guide](https://eclipsemusic.app/docs) is the reference. BitChord's
([its guide](https://bitchord.kushagrasingh.in/docs)) is a close variant: the same
`/manifest.json`, `/search` and `/stream/{id}`, with no ISRC or title lookup, so Shijhon
finds songs at such an add-on through its search. Of BitChord's additions Shijhon reads
`allowDownloads`. What Shijhon sends and reads, the catalog endpoints, the availability
extension and the limits: [the add-on protocol](reference/add-on-protocol.md).

## Writing a catalog adapter

A catalog adapter is a package of its own that makes one catalog available to
Shijhon. An independent adapter - one that contains no code copied from Shijhon; importing
its modules and implementing its interface is fine - may be under any license
([NOTICE](../NOTICE)). Shijhon finds installed adapters through the entry point group
`shijhon.catalogs`:

```toml
# the adapter's pyproject.toml
[project]
name = "shijhon-catalog-example"
dependencies = ["shijhon"]          # with the versions the adapter works with

[project.entry-points."shijhon.catalogs"]
example = "shijhon_catalog_example:adapter"       # [catalog] kind = "example"
```

`demo`, `addon` and `none` are taken. An adapter uses `shijhon.catalog.base` (the
interface, `CatalogError`), `model` (the data), `plugin` (the declaration) and
`contract` (the checks); the package is typed.
[`shijhon-catalog-musicbrainz`](https://github.com/Jasshl/shijhon-catalog-musicbrainz)
is a complete example.

**The catalog** implements `base.Catalog`: `search`, `album` (with all its tracks),
`song`, `songs_by_isrc`, `artist`, `artist_releases`, `top_songs`, `artists_of`, `artwork`,
`aclose`, and optionally `check` (one small request for the dashboard's "Check now"),
`album_of` (the album of a song that does not name it) and `remember` (for a catalog
that cannot be asked for one song). It only fetches and translates: caching, request
coalescing, edition de-duplication and the library pass's pacing are Shijhon's.

- `key`: lower-case letters and digits, starting with a letter. It is in every ID and
  saved match (`sh.al.<key>.<id>`): never change it once a library holds its items.
  `region`: the market the answers are for, `""` for none.
- Item IDs are the catalog's own, of letters, digits and dots.
- Items are complete and typed as `model` says: lengths in milliseconds above 0, ISRCs in
  upper case, dates as `YYYY`, `YYYY-MM` or `YYYY-MM-DD`, tuples not lists, `explicit` and
  `clean` flags when the catalog has them.
- A release whose tracks could not all be read says `incomplete`: it is never filled into
  an album. One without disc and track numbers numbers them in its list's order and says
  `numbered = False`.
- An artwork template is an http(s) address with `{w}` and `{h}` where the size goes,
  nothing else left open. `artwork(url)` fetches only the catalog's own image addresses,
  refuses any other (`CatalogError("invalid", …)`), and returns a raster image (JPEG,
  PNG, GIF or WebP) with its content type. Apps also get the image addresses themselves, unless the catalog says
  `client_artwork = False`.
- Every failure is a `CatalogError` with its kind (`not_found`, `unauthorized`,
  `rate_limited`, `unavailable`, `invalid`) and a short reason that never holds an
  address, a token or another secret.
- The adapter keeps to its catalog's request limits itself. When a search takes longer
  than `[search] budget_seconds`, its notes say which value lets a first search show the
  catalog's results.
- Network access goes through `context.http()`: a client under Shijhon's network policy,
  with the configured timeout.

**The declaration** is a `plugin.Adapter`:

```python
from pathlib import Path
from pydantic import BaseModel, SecretStr
from shijhon.catalog.plugin import Adapter, Context, Problem, Words


class Settings(BaseModel):  # the adapter's own [catalog] settings
    region: str = "us"
    token: SecretStr | None = None  # a SecretStr is a secret: never shown or logged
    token_file: Path | None = None


def build(settings: Settings, context: Context) -> ExampleCatalog:
    return ExampleCatalog(context.http(), settings)  # may raise ValueError


def problem(settings: Settings) -> Problem | None:  # what would keep build() from working
    if settings.token is None and settings.token_file is None:
        return Problem("token", "Set a token or a token file.", "is needed (or a token file).")
    return None


adapter = Adapter(
    label="Example Music",
    build=build,
    settings=Settings,
    words={
        "region": Words("Region", "The two-letter region.", short=True, max_length=2),
        "token": Words("Token"),
        "token_file": Words("Token file"),
    },
    one_choice=(("token", "token_file"),),  # the first one set is used; locked together
    problem=problem,
)
```

- Settings need defaults, must not use the names of Shijhon's own `[catalog]` settings,
  and are of types the configuration and the Catalog page can hold: `str`, `int`,
  `float`, `bool`, a `Literal` of strings, `SecretStr`, `Path` (each may be optional),
  `list[int]`, `dict[str, SecretStr]`.
- `problem()` says what is missing; the dashboard shows it and keeps incomplete settings
  from being saved.
- `bound`: settings that belong to the address another setting names
  (`{"service_headers": "service_url"}`). A value from the file or the environment is
  never sent to an address saved on the dashboard on another host, and a value saved on
  the dashboard goes only to the host its address had when it was saved. Declare a
  credential a `SecretStr`.
- `notice`: said on the Catalog page and at startup while the adapter is in use.
- `Words(…, choices="sources")` makes a text setting a choice among the add-ons.
- Messages - a `Problem`'s words, a validator's error, a `ValueError` from `build` - are
  shown and logged: never put a setting's value in them.
- A declaration that does not fit is not installed, and `shijhon catalogs` says why.

**The contract kit** is what every adapter runs in its own tests (pytest with anyio):

```python
import pytest
from shijhon.catalog import contract

pytestmark = pytest.mark.anyio  # with an anyio_backend fixture, as pytest's anyio plugin needs


def test_the_declaration() -> None:
    contract.check_declaration("example", adapter)


async def test_the_catalog() -> None:  # built over recorded answers, or live
    await contract.check_catalog(catalog, contract.Sample(search="a term that finds albums"))
```

It checks the declaration and asks the catalog for a search, an album, a song by ID and
ISRC, an artist with releases and top songs, a cover and IDs it does not have. Whether the
adapter reads its catalog right is for its own tests, against its own recorded answers
(invented names if they are published).

**Running with an adapter** from source: `uv run --with-editable /path/to/the/adapter
shijhon --config … serve` (the same for `tools/devserver.py` and `pytest -m live`). In
tests, `plugin.register("name", adapter)`. In an image:
[deployment.md](deployment.md#catalog-adapters).

## Maintainer tasks

### A new Navidrome version

`-m canary` selects the suites a Navidrome upgrade must pass first: D (tags), E (IDs), C
(placeholder audio and adding releases), the entry contract (`test_J_contract.py`) and the
harness's smoke test:

```sh
SHIJHON_NAVIDROME_VERSION=<x.y.z> uv run pytest -m canary -n 3   # or =latest
```

A failure is a difference in Navidrome to look at before upgrading, not something to adapt
the tests to. `SHIJHON_NAVIDROME_BIN` points the harness at a local binary. The canary runs each suite on a fresh Navidrome, so it cannot see a
migration that changes existing IDs. Once it passes, update the version in three places:
`PINNED_VERSION` and `PINNED_SHA256` in `tests/harness/navidrome.py`, `TESTED_VERSION` in
`src/shijhon/navidrome/checks.py`, and the image tag and digest in
`packaging/compose.yaml`.

### The scale runner

`tools/scale.py` writes synthetic catalog releases through Shijhon's own placeholder
engine, then records Navidrome's scan times, the latency of `search3`, `getAlbumList2` and
`getAlbum` through Shijhon and at Navidrome directly, and the sizes of the databases and
the placeholder folder:

```sh
uv run python tools/scale.py --storage /path/on/the/storage --count 20000
```

The storage folder must be outside every music library a Navidrome watches. Results go
to `var/scale/<time>-<count>/`. `--help` lists the options.

### Publishing an image

This project publishes the Dockerfile, not an image. [NOTICE](../NOTICE) says what someone
who publishes a built image passes on with it. The commands for that:

- `uv export --no-dev` lists the Python packages in the image at their exact versions;
  `pip download --no-binary :all: <name>==<version>` fetches one's source distribution.
- `dpkg-query -W`, run in the image, lists the Debian packages and their versions.
