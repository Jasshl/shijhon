# Deployment

Shijhon runs in front of a stock Navidrome that only Shijhon reaches. For a new
installation, follow the [quick start](quick-start.md), which uses the Compose file and the
example configuration in [`packaging/`](../packaging). To put Shijhon in front of a
Navidrome you already run, read
[the next section](#in-front-of-a-navidrome-you-already-run). The rarer setups are in
[advanced deployment](reference/advanced-deployment.md).

On NixOS, use the [NixOS service](nixos.md) instead. It sets up Navidrome and Shijhon
together, also with a Navidrome you already run. Catalog adapters and moving from the
Docker setup are in [its reference](reference/nixos.md).

## In front of a Navidrome you already run

Shijhon can go in front of the Navidrome you run now, which keeps running as it is, with
its accounts, IDs, playlists, favorites and play counts. Nothing is copied: Shijhon reaches
your Navidrome over the network, Compose starts no Navidrome of its own, and you change two
settings of yours.

This needs Docker Compose 2.24.4 or newer (`docker compose version`). Run `./setup.sh` in
the checkout and answer `running` to its first question. It then asks for the music folder
your Navidrome scans, your Navidrome's address, an admin account of yours, your Navidrome's
data for the usage export, and the port your apps connect to. The admin account is used
once, to create Shijhon's own admin account, `shijhon`, and its password is not saved.

The script also makes a Docker network for Shijhon and your Navidrome. It is named
`shijhon-navidrome`, or `<name>-navidrome` with another `COMPOSE_PROJECT_NAME`. Its range
is one that nothing on the machine uses (`SHIJHON_NETWORK` in `packaging/.env`), and
Shijhon's address on it always ends in `.10`, for example `172.30.99.10`. Once the image
is built, the script prints the changes your Navidrome needs, with the network's name and
Shijhon's address. At a terminal it waits until you press Enter once your Navidrome has
joined; without one it stops there, and the next run goes on. If Shijhon still can't reach
your Navidrome, the script says so and offers to try again.

The script writes `COMPOSE_FILE=compose.yaml:compose.existing-navidrome.yaml` into
`packaging/.env`, so later Compose commands in `packaging/` use both files. The
[second file](../packaging/compose.existing-navidrome.yaml) leaves out the Compose file's
Navidrome and uses that network.

### Your music folder

Give the folder your Navidrome scans, as this machine has it: for a Navidrome in a
container, the folder on the host that is mounted as its music folder, not a folder above
or inside it. Shijhon creates its placeholder folder, `_shijhon`, in that folder, so
Navidrome finds the files Shijhon writes inside its own library, at the path it has for
it. Navidrome can keep the folder mounted read-only. Shijhon's container runs as `PUID:PGID`
from `packaging/.env`, your user, which has to be able to read the music and write
`_shijhon`.

If your Navidrome has more than one library, Shijhon uses the first one. For another, set
`library_id` under `[navidrome]` in `packaging/config/shijhon.toml` to its ID
(`getMusicFolders` lists them) and restart Shijhon.

### Your Navidrome trusts Shijhon

Navidrome limits failed logins per app address, and it learns each app's address from
Shijhon only when it trusts the address it sees Shijhon at. That is its
`ExtAuth.TrustedSources` (`ND_EXTAUTH_TRUSTEDSOURCES` in its environment). Without it, all
apps count as one, and a device with an old password locks that person out on every other
device for a while ([client addresses](reference/advanced-deployment.md#client-addresses)).
Trust only Shijhon's address, written as a network (`/32`) without spaces, and make sure
apps can no longer reach Navidrome directly. Which address that is depends on where your
Navidrome runs.

**A Navidrome in a container on this machine**, in a Compose project of its own or started
with `docker run`, joins Shijhon's network. There it sees Shijhon at Shijhon's own address,
and Shijhon reaches it by its service or container name, for example
`http://navidrome:4533`. In your Navidrome's Compose file, with the range and the network
name the script printed in place of `172.30.99` and `shijhon-navidrome`:

```yaml
services:
  navidrome:
    environment:
      ND_EXTAUTH_TRUSTEDSOURCES: 172.30.99.10/32
    networks: [default, shijhon]
    # Remove its "ports:" once your apps use Shijhon.
networks:
  shijhon:
    name: shijhon-navidrome
    external: true
```

Then run `docker compose up -d` in your Navidrome's folder. A container started with
`docker run` joins with `docker network connect <name>-navidrome <its name>`, which lasts
until the container is created again, and gets the setting with
`-e ND_EXTAUTH_TRUSTEDSOURCES=172.30.99.10/32` when you create it.

**A Navidrome on this machine outside Docker** is reached at
`http://host.docker.internal:4533` (with its port). On Linux, which has not been tried yet,
that name is the address of Docker's bridge, usually `172.17.0.1`. Navidrome then listens
on every address (`ND_ADDRESS=0.0.0.0`), with a firewall that keeps its port from the
network but lets Shijhon's range in, for example with ufw
`sudo ufw allow from 172.30.99.0/24 to any port 4533 proto tcp`. It sees Shijhon at
Shijhon's own address, `172.30.99.10/32` in the example. With Docker Desktop on a Mac,
Navidrome can keep listening on `127.0.0.1`, and it sees Shijhon at `127.0.0.1`.
Trusting `127.0.0.1/32` trusts every program on that computer, which can then sign in to
Navidrome as anyone, so only do that where you trust them all.

**A Navidrome on another machine** is reached at its address, for example
`http://192.168.1.20:4533`. It sees Shijhon at this machine's address on your network, which
is the one to trust; let only this machine reach Navidrome's port, with a firewall on
Navidrome's machine. Shijhon also needs the same music files: share Navidrome's music folder
with this machine (over NFS or SMB, for example), give the shared folder as the music folder,
and let your user write `_shijhon` in it. Leave out the usage export, because a database
shouldn't be read over a network share.

If your Navidrome signs people in through a reverse proxy's header (`ExtAuth.UserHeader`
or `ReverseProxyUserHeader`), see
[sign-in by the reverse proxy](reference/advanced-deployment.md#sign-in-by-the-reverse-proxy).
Your Navidrome must also meet [what Shijhon needs](#what-shijhon-needs-from-navidrome).

### Moving your apps

Your apps then connect to Shijhon, at the port you gave, with the same accounts. Add
Shijhon as a **Subsonic** server ([connecting](quick-start.md#connecting)). Remove your
Navidrome's published port, so that nothing reaches it except through Shijhon: apps that
still connect to it directly would see Shijhon's placeholders there as silent songs. To give
Shijhon the port your apps used, set `SHIJHON_PORT` in `packaging/.env` to it once
Navidrome no longer uses it, and run `docker compose up -d` in `packaging/`. A reverse
proxy that sent people to Navidrome now sends them to Shijhon instead
([network and reverse proxy](#network-and-reverse-proxy)).

### Taking Shijhon out again

Stop Shijhon with `docker compose down` in `packaging/`. Your Navidrome still lists the
songs Shijhon wrote into `_shijhon`. Most are silent placeholders, but the folder also
holds audio Shijhon saved (downloads, kept streams) and the audio of catalog songs that
music imports gave real files to. Back up Navidrome's database and copy out any audio you
want to keep into a folder Navidrome scans, then delete `_shijhon`, let Navidrome scan,
check the result, and remove the missing files in its web interface. Then take the network and
`ND_EXTAUTH_TRUSTEDSOURCES` out of your Navidrome's settings and restart it, remove the
network with `docker network rm <name>-navidrome`, and delete the `shijhon` account under
**Users**.

### The usage export with your Navidrome

The cleanup needs [the usage export](#the-usage-export), which reads your Navidrome's
database read-only. Give the script your Navidrome's data folder, the one mounted as its
`/data` (it holds `navidrome.db`), or the name of its Docker volume (`docker volume ls`
lists them). The export runs as `PUID:PGID` and needs to be able to read those files: the
files of a Navidrome container that runs as root can be read by everyone, and so can those
of one that runs as your user.

Without it, Shijhon can't tell whether anyone used a catalog release it added: the cleanup
doesn't run, and when an album is filled from another release, the earlier release's songs
stay in the library. Everything else works. To add it later, add
`EXISTING_NAVIDROME_DATA='/path'` (a folder) or `EXISTING_NAVIDROME_VOLUME=name` (a volume)
and `COMPOSE_PROFILES=usage-export` to
`packaging/.env`, uncomment `usage_export_path` under `[navidrome]` in
`packaging/config/shijhon.toml`, and run `docker compose up -d` in `packaging/`.

### By hand

The same, from `packaging/`:

1. Write `.env` and the configuration as in the quick start's
   [steps by hand](quick-start.md#by-hand), steps 1 and 2, with `MUSIC_DIR` your Navidrome's
   music folder, and add to `.env`:

   ```dotenv
   SHIJHON_NETWORK=172.30.99
   COMPOSE_FILE=compose.yaml:compose.existing-navidrome.yaml
   ```

   In `config/shijhon.toml`, set `url` under `[navidrome]` to your Navidrome's address, and
   for the usage export follow [the section above](#the-usage-export-with-your-navidrome),
   or comment out `usage_export_path`.

2. Make Shijhon's network with that range, and connect your Navidrome to it
   ([above](#your-navidrome-trusts-shijhon)). It is named `shijhon-navidrome`, or
   `<name>-navidrome` with `COMPOSE_PROJECT_NAME=<name>` in `.env`. Other containers on it
   get addresses from `.128` up, so `.10` stays Shijhon's:

   ```sh
   docker network create --subnet 172.30.99.0/24 --ip-range 172.30.99.128/25 shijhon-navidrome
   ```

3. Create Shijhon's account in your Navidrome's web interface, under **Users**, as an
   admin, with a long random password. Put that password in `config/navidrome-password`,
   readable only by you, as in the quick start's step 3.

4. Start Shijhon:

   ```sh
   docker compose up -d --build
   ```

### What Shijhon needs from Navidrome

- **Version 0.64.2**, unmodified, **not reachable from outside**, at the root of its
  address (no `BaseURL`).
- **A service account**, a Navidrome admin account used only by Shijhon, not a person's.
- **The same library files** in Shijhon's container, at `/music` there whatever path
  Navidrome has them at. They are mounted read-only, with only the placeholder folder
  writable. If you turn on **Also fix files in your music folder**
  ([how it works](how-it-works.md#each-artist-of-a-joined-credit)), mount the library
  writable for Shijhon instead (in `compose.yaml`, drop `:ro` from the `/music` line). Only
  files owned by Shijhon's user are changed.
- **`Scanner.PurgeMissing = never`**, its default. With another value, a placeholder that
  is gone for a moment would lose every user's favorites and playlist entries of it, so
  Shijhon then writes nothing into the library. Such actions are answered with the reason,
  and downloads get the add-on's own format. Shijhon reads the value from Navidrome's
  configuration while Navidrome's `DevUIShowConfig` is on (its default). With that off,
  set `[navidrome] purge_missing = "never"` yourself.

## Music imports

Optional [music imports](music-imports.md), where people upload their own music files,
need a dedicated writable folder of their own. Create it, give `PUID:PGID` access to it,
set `MUSIC_IMPORTS_DIR` to it in `packaging/.env`, and add the
[`compose.imports.yaml`](../packaging/compose.imports.yaml) overlay there too:

```dotenv
COMPOSE_FILE=compose.yaml:compose.imports.yaml
```

Then start from `packaging/`:

```sh
docker compose up -d --build
```

With Shijhon [in front of a Navidrome you already run](#in-front-of-a-navidrome-you-already-run),
keep its second file in the list,
`COMPOSE_FILE=compose.yaml:compose.existing-navidrome.yaml:compose.imports.yaml`, and mount
the same folder into your Navidrome as `_imports` inside its music folder, read-only.

Compose and `./setup.sh` use the file set in `.env` on later runs, so keep the one you want
there ([in detail](reference/advanced-deployment.md#music-imports-in-detail)). Back up
the whole imports folder together with Shijhon's state: it holds people's original music,
not disposable catalog audio. Complete the administrator setup under **Collection → Import
music** before accepting uploads.

## Network and reverse proxy

The Compose file publishes Shijhon's port on every address of the machine, unencrypted,
for a network you trust. For access from outside, put a TLS reverse proxy in front:

1. Set `SHIJHON_BIND=127.0.0.1` in `.env`, so that Shijhon's port is published on this
   machine only. Otherwise a device that connects directly could appear to come from the
   proxy's address, and would be trusted.
2. List the proxy's address, and nothing else, in `[server] trusted_proxies`. For a proxy
   on the host, the configuration has the line, with the gateway of Shijhon's network
   (`.1` of `SHIJHON_NETWORK` in `.env`). Never list an address apps connect from directly.
3. Have the proxy append the client's address to `X-Forwarded-For` (`X-Real-IP` is not
   read) and send `X-Forwarded-Proto: https`, which marks the dashboard's cookie `Secure`.
4. Keep credentials out of the proxy's access log: apps send them in the query string
   (`p`, `t`, `s`), and Navidrome's web interface in the `X-ND-Authorization` header.

Steps 2 and 3 pass each app's address on to Navidrome, whose login limits count by it
([client addresses](reference/advanced-deployment.md#client-addresses)).

For agents connecting from elsewhere, set the agent address on the dashboard
([agent access](agent-access.md#turn-on-agent-access)) and have the proxy keep the `Host`
header and forward [the paths agents use](agent-access.md#with-a-reverse-proxy), two of
them at the root of the address. Agent access does not work under a path prefix.

## Catalog adapters

Catalog adapters are the legacy way to get a catalog; an add-on's catalog needs nothing
installed ([catalogs](catalogs.md)). An adapter is installed into the image when it is
built. A published one takes a line in `packaging/.env` and a rebuild
([installing an adapter](catalogs.md#installing-an-adapter)). One that is not published is
installed from its wheel (`uv build --wheel` in the adapter's project): with Compose, add
the folder of wheels to the `shijhon` service's build
([as shown](reference/advanced-deployment.md#catalog-adapters-in-detail)) and run
`docker compose up -d --build` in `packaging/`. Without Compose, build the image in the
checkout with:

```sh
docker build --build-context adapters=/path/to/wheels -t shijhon .
```

An adapter is code that runs inside Shijhon, with everything Shijhon reads: review it, and
the dependencies it brings, before you install it
([in detail](reference/advanced-deployment.md#catalog-adapters-in-detail)).

## The usage export

The cleanup has to know whether any user ever used a release. That is in Navidrome's
database, alongside its secrets and its users' passwords, so Shijhon's container does not
mount it. Instead the `usage-export` service, which has no network access, writes a small
file with only what the cleanup reads
([what it holds](privacy-and-security.md#what-the-cleanup-reads)). The Compose file sets
it up, and `[navidrome] usage_export_path` names the export.

To switch the cleanup on ([what it does](how-it-works.md#the-cleanup)), have the usage
export running early, since every daily check notes the uses it sees, even while the
cleanup is off. Look at what it would take out (`shijhon cleanup`, or "Run a dry run now"
on the Cleanup page), then set `[cleanup] mode = "on"` in the file or on that page.

## Operating

### Where the data is

The Compose file keeps the data in three Docker volumes, named with the Compose project's
name in front (`docker volume ls`): `shijhon_navidrome-data` (Navidrome's database and
cache), `shijhon_shijhon-data` (Shijhon's `state_dir`: its database, the snapshots taken
before upgrades, backups made with `shijhon backup`, downloads under way, the cover cache
and DASH songs) and `shijhon_usage-export`. `docker compose down` keeps them, and
`docker compose down -v` deletes them.

To keep one in a folder instead, give its absolute path in `.env` as `NAVIDROME_DATA`,
`SHIJHON_DATA` or `USAGE_EXPORT_DATA`. Don't do this with Docker Desktop, where a database
in a folder can be corrupted
([moving data into a volume](reference/advanced-deployment.md#moving-data-into-a-volume)).

### Backups

`shijhon backup` makes a backup of Shijhon's own state, while Shijhon runs or after it
stopped. It writes a folder named `shijhon-backup-` and the date and time (in UTC) into
`backups` in Shijhon's volume. The folder holds a checked copy of the database, the
configuration file, the service account's password file and a `README.txt` that says what
the backup holds, what it leaves out and with which version it was made. The command
prints the same. When the configuration file or the password file cannot be read, it makes
no backup and says which file.

The database holds the settings saved on the dashboard, secrets included, so the folder
and its files are readable by their owner only. Keep the backup private, outside the
checkout. The command has to run as the user Shijhon runs as, which it does in the
container ([in detail](reference/advanced-deployment.md#backups-in-detail)).

From `packaging/`, make a backup and copy it out of the volume, with the folder name the
command printed:

```sh
docker compose exec shijhon shijhon --config /config/shijhon.toml backup
mkdir -p ~/shijhon-backups && chmod 700 ~/shijhon-backups
docker compose cp shijhon:/data/backups/shijhon-backup-20261112T153000Z ~/shijhon-backups/
docker compose exec shijhon rm -r /data/backups/shijhon-backup-20261112T153000Z
```

Backups stay in the volume until you remove them, as the last line does once the copy is
out. With Shijhon stopped, `docker compose run --rm --no-deps shijhon --config
/config/shijhon.toml backup` makes one the same way.

The backup is not a copy of the whole installation. Back these up as well:

- Navidrome's database. Navidrome makes its own backups, for example with
  `ND_BACKUP_PATH=/backups`, `ND_BACKUP_SCHEDULE="30 0 * * *"` and `ND_BACKUP_COUNT=14`, on
  backed-up storage.
- Your music, as you already back it up, and the placeholder folder in it.
- With music imports enabled, the whole imports folder, hidden files included. It holds
  original music, also of removed imports. Restore it into the same folder, not a new one
  ([storage and recovery](music-imports.md#storage-and-recovery)).
- `.env`, and any other place where settings come from the environment.

Shijhon's records must match the placeholder folder, Navidrome's database and, with **Also
fix files in your music folder** on, the artist tags Shijhon changed in your music folder.
For a set to restore together, stop Shijhon, make the backup with `docker compose run` as
above, save those folders and Navidrome's database at the same time, and start Shijhon
again ([restoring](reference/advanced-deployment.md#restoring-a-backup)).

### Restarting

**Restart Shijhon**, on the Diagnostics page and wherever a saved setting waits for a
restart, shuts Shijhon down cleanly, and Docker or your service manager starts it again.
It stops nothing if the configuration file has an error or names another address or port.
A song that is playing may have to be started again.

The Compose file's `restart: unless-stopped` starts it again; **a container run without a
restart policy stays stopped after you press it.** Outside a container, set `[server]
restart_by_supervisor = true` under a service manager that restarts Shijhon after an exit
([in detail](reference/advanced-deployment.md#restarting-and-stopping)).

### Upgrades

- Read [CHANGELOG.md](../CHANGELOG.md) first. Between beta versions the configuration can
  change.
- Update the checkout to the new release: `git pull`, or `git fetch --tags` and
  `git checkout` of the release's tag.
- Build the new image and start it: `docker compose up -d --build` in `packaging/`. The
  line in `.env` installs its adapters again; a folder of wheels has to be given again.
- Stay on the Navidrome version the Shijhon release is tested with.
- With music imports enabled, back up the whole imports folder before upgrading.

Shijhon's database migrates itself at startup, and an older Shijhon does not start on a
database a newer one migrated. Before it migrates, the first start writes a checked
snapshot of Shijhon's database into `snapshots` in Shijhon's volume. To be able to roll
back completely, also back up Navidrome's data and the placeholder folder from the same
time.

When the snapshot cannot be written (not enough free space, a folder it cannot write, or
a damaged database), Shijhon upgrades and starts without it. Its log says why
(`docker compose logs shijhon` in `packaging/`), administrators see a line at the top of
every dashboard page until one of them dismisses it, and it is reported once when
[notification](configuration.md#notifications) addresses are set. Free some space or fix
the folder, so that the next upgrade can take its snapshot, and make a backup now with
`shijhon backup` ([backups](#backups)). `upgrade_snapshots = false` (or
`SHIJHON_UPGRADE_SNAPSHOTS=false`) migrates without a snapshot. The
[details](reference/advanced-deployment.md#upgrades-in-detail) cover the snapshots' names,
a damaged database, and what each schema changed.

To go back, stop Shijhon, restore the snapshot and what changed with it, and only then
start the earlier version ([restoring](reference/advanced-deployment.md#restoring-a-backup)).
Work done after the upgrade is lost then. The snapshot holds only Shijhon's database:
keep the earlier configuration if you change it for the new version.

### Docker Desktop on macOS and Windows

For a first look only; Shijhon is meant to run on a Linux host. Keep the data in Docker
volumes, the default: in a folder of the Mac or Windows disk, Navidrome's database can be
corrupted, and a Navidrome account created while the server runs is lost. Every device
that connects directly reaches Shijhon from one address, so one that keeps sending an old
password keeps that user out on all of them, 20 seconds at a time.
