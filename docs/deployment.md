# Deployment

Shijhon runs in front of a stock Navidrome that only Shijhon reaches.
[quick-start.md](quick-start.md) sets one up with the Compose file and the example
configuration in [`packaging/`](../packaging); [advanced deployment](reference/advanced-deployment.md)
has the rarer setups.

## Starting from an existing Navidrome

Shijhon's Navidrome can start from a copy of the one you run, keeping its users, IDs,
playlists, favorites and play counts. These steps have not been tested with Compose yet.

1. Write `.env` and the configuration as in the quick start's
   [steps by hand](quick-start.md#by-hand), steps 1 and 2. Start nothing yet.
2. Take a **consistent** copy of the database with SQLite's backup, not a file copy of a
   running database, into a folder of its own:
   `sqlite3 'file:/path/navidrome.db?mode=ro' ".backup /path/to/copy/navidrome.db"`.
   Copy the old data folder's `artwork` folder there too (uploaded images). Then put the
   copy into Navidrome's volume, owned by your `PUID:PGID` from `.env` (1000:1000 here),
   from `packaging/`:

   ```sh
   docker compose up --no-start &&
   docker run --rm -v shijhon_navidrome-data:/data -v /path/to/copy:/from:ro alpine \
       sh -c 'cp -a /from/. /data/ && chown -R 1000:1000 /data'
   ```

   With `NAVIDROME_DATA` naming a folder, copy it into that folder instead.
3. Mount the music at the **same path inside the container** as before (usually `/music`,
   as the Compose file does), so that the IDs stay the same. Use the same Navidrome
   version, and check its log for "no migrations to run".
4. Keep Navidrome's `PasswordEncryptionKey` as it was (unset stays unset), or existing
   accounts cannot log in.
5. Create Shijhon's service account as in the quick start's step 3, and set
   `[navidrome] library_id`: `getMusicFolders` lists the IDs, and a migrated Navidrome's
   library is not always 1.
6. Start everything. Before you switch your apps over, compare with the old Navidrome:
   playlists (entry counts and order), song and album counts, favorites and a sample of
   song IDs, before and after a full scan.

The copy is then the new setup's data; do not copy the old database over it again.

### What Shijhon needs from Navidrome

- **Version 0.64.2**, unmodified, **not reachable from outside**, at the root of its
  address (no `BaseURL`).
- **A service account**: a Navidrome admin account used only by Shijhon, not a person's.
- **The library at the same path** in Shijhon's container, read-only, with only the
  placeholder folder writable.
- **`Scanner.PurgeMissing = never`**, its default. With another value a placeholder that
  is gone for a moment would lose every user's favorites and playlist entries of it, so
  Shijhon then writes nothing into the library: such actions are answered with the reason,
  and downloads get the add-on's own format. Shijhon reads the value from Navidrome's
  configuration, which Navidrome shows its admins while `DevUIShowConfig` is on (its
  default). With that off, set `[navidrome] purge_missing = "never"` yourself.

## Network and reverse proxy

The Compose file publishes Shijhon's port on every address of the machine, unencrypted,
for a network you trust. For access from outside, put a TLS reverse proxy in front:

1. Set `SHIJHON_BIND=127.0.0.1` in `.env`, so that Shijhon's port is published on this
   machine only. Otherwise a device that connects directly could appear to come from the
   proxy's address, and would be trusted.
2. List the proxy's address, and nothing else, in `[server] trusted_proxies`. For a proxy
   on the host, the example configuration has the line. Never list an address apps
   connect from directly.
3. Have the proxy append the client's address to `X-Forwarded-For` (`X-Real-IP` is not
   read) and send `X-Forwarded-Proto: https`, which marks the dashboard's cookie `Secure`.
4. Keep credentials out of the proxy's access log: apps send them in the query string
   (`p`, `t`, `s`), and Navidrome's web interface in the `X-ND-Authorization` header.

Navidrome limits failed logins per client address, so it has to learn each client's
address ([how](reference/advanced-deployment.md#client-addresses)).

## Catalog adapters

An adapter is installed into the image when it is built: a published one by a line in
`packaging/.env` and a rebuild ([catalogs.md](catalogs.md#installing-an-adapter)), one
that is not published from its wheel (`uv build --wheel` in the adapter's project):

```sh
docker build --build-context adapters=/path/to/wheels -t shijhon .
```

An adapter is code that runs inside Shijhon, with everything Shijhon reads: review it, and
the dependencies it brings, before you install it
([in detail](reference/advanced-deployment.md#catalog-adapters-in-detail)).

## The usage export

The cleanup has to know whether any user ever used a release. That is in Navidrome's
database, alongside its secrets and its users' passwords, so Shijhon's container does not
mount it: the `usage-export` service, which has no network access, writes a small file
with only what the cleanup reads
([what it holds](privacy-and-security.md#what-the-cleanup-reads)). The Compose file sets
it up; `[navidrome] usage_export_path` names the export.

To switch the cleanup on ([what it does](how-it-works.md#the-cleanup)), have the usage
export running early, since every daily check notes the uses it sees, even while the
cleanup is off. Look at what it would take out (`shijhon cleanup`, or "Run a dry run now"
on the Cleanup page), then set `[cleanup] mode = "on"` in the file or on that page.

## Operating

### Where the data is

The Compose file keeps the data in three Docker volumes, named with the Compose project's
name in front (`docker volume ls`): `shijhon_navidrome-data` (Navidrome's database and
cache), `shijhon_shijhon-data` (Shijhon's `state_dir`: its database, downloads under way,
the cover cache and DASH songs) and `shijhon_usage-export`. `docker compose down` keeps
them; `docker compose down -v` deletes them. A one-shot `data-owner` service gives new
volumes to the user the containers run as at each start.

To keep one in a folder instead, give its absolute path in `.env`: `NAVIDROME_DATA`,
`SHIJHON_DATA` or `USAGE_EXPORT_DATA`. Not with Docker Desktop, where a database in a
folder can be corrupted
([moving data into a volume](reference/advanced-deployment.md#moving-data-into-a-volume)).

### Backups

- **Navidrome**: its own backups, for example `ND_BACKUP_PATH=/backups`,
  `ND_BACKUP_SCHEDULE="30 0 * * *"`, `ND_BACKUP_COUNT=14`, on backed-up storage.
- **Shijhon**: stop it, then save its volume, the placeholder folder and the configuration
  together, so that they match. The archive holds the saved settings, secrets included:
  keep it private, outside the checkout.
- **Your music**: as before. Shijhon writes only inside its placeholder folder.

From `packaging/` (the new archive replaces the earlier one only once it reads back
whole; [restoring](reference/advanced-deployment.md#restoring-a-backup)):

```sh
mkdir -p ~/shijhon-backups && chmod 700 ~/shijhon-backups
docker compose stop &&
docker run --rm -v shijhon_shijhon-data:/data:ro -v ~/shijhon-backups:/backup alpine \
    sh -c 'cd /backup && umask 077 && tar -czf new.tar.gz -C /data . &&
        chmod 600 new.tar.gz && tar -tzf new.tar.gz >/dev/null &&
        mv new.tar.gz shijhon-data.tar.gz'
docker compose start
```

### Restarting

**Restart Shijhon**, on the Diagnostics page and wherever a saved setting waits for a
restart, shuts Shijhon down cleanly, and Docker or your service manager starts it again.
It stops nothing if the configuration file has an error or names another address or port.
Requests still open get 5 seconds: a song that is playing may have to be started again.

The Compose file's `restart: unless-stopped` starts it again; **a container run without a
restart policy stays stopped after you press it.** Outside a container, set `[server]
restart_by_supervisor = true` under a service manager that restarts Shijhon after an exit
([in detail](reference/advanced-deployment.md#restarting-and-stopping)).

### Upgrades

- Read [CHANGELOG.md](../CHANGELOG.md) first. Between beta versions the configuration can
  change.
- Take a backup. Shijhon's database migrates itself at startup, and an older Shijhon does
  not start on a newer database: going back means restoring that backup, the database and
  the placeholder folder together.
- Build the new image. The line in `.env` installs its adapters again; a folder of wheels
  has to be given again.
- Stay on the Navidrome version the Shijhon release is tested with.

### Docker Desktop on macOS and Windows

For a first look only; Shijhon is meant to run on a Linux host. Keep the data in Docker
volumes, the default: in a folder of the Mac or Windows disk, Navidrome's database can be
corrupted, and a Navidrome account created while the server runs is lost. Every device
that connects directly reaches Shijhon from one address, so one that keeps sending an old
password keeps that user out on all of them, 20 seconds at a time.
