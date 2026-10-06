# Quick start with Docker Compose

```sh
git clone https://github.com/Jasshl/shijhon && cd shijhon
./setup.sh
```

The script runs on Linux and macOS; on Windows, run it inside WSL. It has not been
tested in WSL yet. Already running Navidrome? To keep its users, IDs,
playlists and favorites, see
[deployment.md](deployment.md#starting-from-an-existing-navidrome).

It asks where your music is, for a username and a password for your own account, for the
port your apps connect to (4533 by default), and which catalog adapters to install (none
by default). Then it checks Docker, the music folder and the port, and creates the
placeholder folder `_shijhon` in your music folder. It writes `packaging/.env` and
`packaging/config/shijhon.toml`, and creates your account and Shijhon's own admin account,
`shijhon`, whose password goes into `packaging/config/navidrome-password`. Last, it builds
the image and starts everything. If you run it again, it continues from what is already
there; `./setup.sh --help` lists its options.

The [Compose file](../packaging/compose.yaml) runs Navidrome, which only Shijhon can
reach; Shijhon itself; and the usage export, a small helper with no network access that
tells Shijhon which releases are used. Their data is in Docker volumes
([where the data is](deployment.md#where-the-data-is)).

## Connecting

Connect your apps to Shijhon, never to Navidrome: `http://127.0.0.1:4533` on this
machine, `http://<this machine's address>:4533` from other devices. Navidrome's web
interface is at `/app` (to add other people's accounts), the dashboard at `/shijhon/`. To
check the installation, play something from your library and look at the dashboard's
Diagnostics page.

The connection is not encrypted, as with a plain Navidrome, so this is for a network you
trust. For access from outside, put a TLS reverse proxy in front
([deployment.md](deployment.md#network-and-reverse-proxy)).

## What to add next

Add a catalog ([catalogs.md](catalogs.md)) and add-ons, which supply the audio for
catalog tracks ([add-ons.md](add-ons.md)).

To try a catalog, install the example adapter for
[MusicBrainz](https://musicbrainz.org), which needs no account or key
([its limits](catalogs.md#the-musicbrainz-adapter)). Give this address when the script
asks for catalog adapters, or add it to `.env` later and build again, from `packaging/`:

```sh
echo 'SHIJHON_ADAPTERS=https://github.com/Jasshl/shijhon-catalog-musicbrainz/archive/refs/tags/v0.1.0b1.tar.gz' >> .env
docker compose up -d --build
docker compose exec shijhon shijhon catalogs       # lists "musicbrainz"
```

Then choose MusicBrainz on the dashboard's Catalog page and press **Restart Shijhon**.

## By hand

The same installation, from `packaging/`.

1. **Write `.env`**, and create `_shijhon` before the first start (otherwise Docker
   creates it owned by root). The containers run as `PUID:PGID` (1000:1000 unless set),
   which must be able to read the music; it is mounted read-only, except `_shijhon`.

   ```sh
   cat > .env <<EOF
   MUSIC_DIR=/path/to/music
   PUID=$(id -u)
   PGID=$(id -g)
   EOF
   . ./.env
   mkdir -p "$MUSIC_DIR/_shijhon"
   ```

   Optional: `SHIJHON_PORT=4533`; `SHIJHON_BIND=127.0.0.1` (this machine only);
   `NAVIDROME_DATA`, `SHIJHON_DATA`, `USAGE_EXPORT_DATA` (a folder instead of a Docker
   volume; not with Docker Desktop).

2. **Copy the configuration** and keep it private: it will hold a password, add-on URLs
   and catalog access. The example needs no change.

   ```sh
   mkdir -p config
   cp shijhon.example.toml config/shijhon.toml
   chmod 600 config/shijhon.toml
   ```

3. **Start Navidrome, then create the accounts**: Shijhon's service account, an admin
   account used only by Shijhon, then your own. Each asks for its password. Put the
   service account's in `config/navidrome-password`, private like the configuration (both
   readable by `PUID`).

   ```sh
   docker compose up -d navidrome
   until docker compose logs navidrome | grep -q "server is ready"; do sleep 1; done
   docker compose run --rm -it navidrome user create -u shijhon --admin
   docker compose run --rm -it navidrome user create -u yourname --admin
   read -rs pw && printf '%s\n' "$pw" > config/navidrome-password && unset pw
   chmod 600 config/navidrome-password
   ```

4. **Start everything** (the first start builds the image), then
   [connect your apps](#connecting):

   ```sh
   docker compose up -d --build
   docker compose logs -f shijhon
   ```
