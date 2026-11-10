# NixOS reference

[The NixOS guide](../nixos.md) sets the service up. This page has its options, what it
sets up, and the less common setups.

## Options

All options are under `services.shijhon`.

| Option | Default | What it does |
|---|---|---|
| `enable` | `false` | Runs Shijhon. |
| `libraryPath` | (required) | Your music folder, which must exist. |
| `account.name`, `account.passwordFile` | none | Your own Navidrome account. In a new Navidrome it is created as an admin. In one that has accounts, it must be an admin there, and Shijhon uses it to create its own account. The file is only read until both accounts exist. |
| `listenAddress` | `"127.0.0.1"` | Where Shijhon listens. `"0.0.0.0"` or `"::"` listens on every address. |
| `port` | `4533` | The port apps connect to. |
| `openFirewall` | `false` | Opens `port` in the firewall. |
| `settings` | `{ }` | Other settings from `shijhon.toml` ([configuration](../configuration.md)), such as `cleanup.mode`. They end up in the Nix store, so don't put secrets here. |
| `environmentFile` | none | A file of `SHIJHON_*` variables for secrets, such as `SHIJHON_AUDIOMUSE__TOKEN`. |
| `navidrome.manage` | `true` | Sets up NixOS's Navidrome for Shijhon, including [one you already run](../nixos.md#you-already-run-servicesnavidrome). `false` uses [your own Navidrome](../nixos.md#an-existing-navidrome). |
| `navidrome.allowUntestedVersion` | `false` | Allows a Navidrome version Shijhon isn't tested with ([the Navidrome version](../nixos.md#the-navidrome-version)). |
| `navidrome.url`, `navidrome.passwordFile` | none | Your own Navidrome's address, and the password file of an account you created there for Shijhon, used instead of `account`. |
| `navidrome.user` | `"shijhon"` | Shijhon's own account in Navidrome. |
| `navidrome.group` | Navidrome's group | The group Navidrome reads the library with. The placeholder folder belongs to it. |
| `usageExport.enable` | `navidrome.manage` | Runs the usage export ([what it is](../deployment.md#the-usage-export)). |
| `usageExport.navidromeDataDirectory` | Navidrome's data folder | Where Navidrome's database is. Only the export reads it. |
| `usageExport.user` | Navidrome's user | The user the export runs as, which must be able to read that database. |
| `usageExport.directory` | `/var/lib/shijhon-usage` | Where the export is written. |
| `usageExport.everySeconds` | `900` | How often the export is written. Shijhon can also ask for one. |
| `aiRunner.packages`, `aiRunner.home` | none | [Local AI runners](#local-ai-runners) for playlists, and the folder for their sign-in. |
| `placeholderFolder` | `"_shijhon"` | The placeholder folder's name in the library. |
| `placeholderPath` | inside the library | Where that folder really is, when `libraryPath` is a read-only union you set up that shows it. |
| `importsPath`, `importsFolder` | none, `"_imports"` | [Music imports](#music-imports). |
| `package` | this flake's | Shijhon's package, with any [catalog adapters](#catalog-adapters). |
| `user`, `group`, `createUser` | `"shijhon"`, `"shijhon"`, `true` | Shijhon's own user and group. |
| `stateDirectory` | `/var/lib/shijhon` | Shijhon's database, caches and Navidrome password. |

Add-ons, the catalog and the settings shown on the dashboard are saved in Shijhon's
database. A value in `settings` applies until the same setting is saved on the dashboard.

## What the service sets up

- **Navidrome** (`services.navidrome`, new or yours), with `libraryPath` as its music
  folder. It listens only on a Unix socket that Shijhon and `shijhon-accounts` can open,
  through a group of their own, `shijhon-navidrome`. Navidrome trusts that socket to tell
  it each app's address and user, so its login limits apply to each app separately
  ([client addresses](advanced-deployment.md#client-addresses)). Its other settings can be
  changed in `services.navidrome.settings`, except the ones Shijhon needs: `Address`,
  `UnixSocketPerm`, `ExtAuth.TrustedSources`, an `ExtAuth.UserHeader` that Shijhon removes
  from apps' requests (`Remote-User`, or the name in
  `settings.server.reverse_proxy_user_header`), no `BaseUrl`, and `Scanner.PurgeMissing` at
  `"never"`. The build fails if one of these is set differently.
- **`shijhon-prepare`** runs as root before the others. It checks that the library exists
  and creates or fixes the `_shijhon` folder, owned by `shijhon` and Navidrome's group, mode
  2755. If the library is missing, it stops with a message instead of creating it.
- **`shijhon-accounts`** runs before Shijhon and creates the accounts. While Navidrome isn't
  answering yet, it tries for three minutes, starts over ten seconds later if it has to,
  and Shijhon waits for it. Until the accounts exist, it keeps a copy of your password file in
  `/run`. In a Navidrome that already has accounts, it signs in with `account` to create
  Shijhon's. If it can't, because `account` isn't set, the password is wrong or the
  account isn't an admin, it stops with a message. The other way is to create Shijhon's
  account by hand, write its password to `/var/lib/shijhon/navidrome-password` (owned by
  `shijhon`, mode 0600) and restart `shijhon-accounts`.
- **`shijhon`** listens on `listenAddress:port`. Anyone who can read the library can read
  the files Shijhon writes into it. Its state folder has mode 0700 and its secrets 0600.
  It trusts proxies on loopback (`settings.server.trusted_proxies`), so on a machine with
  other users, their programs can send a client address through Shijhon's port. That can
  get an app's address locked out by Navidrome for a few seconds, but can't sign anyone
  in.
- **`shijhon-usage-export`** runs without network access, as Navidrome's user and
  Shijhon's group. It reads Navidrome's database read-only and writes the export that
  Shijhon reads, so Shijhon never opens Navidrome's database itself.
- The `shijhon` user and group, and the firewall port with `openFirewall`.

## The sandbox

Shijhon, `shijhon-accounts` and the usage export each run in an empty root filesystem with
only their runtime and the folders they need. They have no capabilities, can't gain
privileges and run in their own PID namespace. `shijhon-prepare` runs as root, with only
the capabilities it needs to create the placeholder folder and copy the password file.

Shijhon sees the library read-only at `/music`, with the placeholder folder (and the
imports folder) writable inside it, its state at `/data`, the export at `/usage`, and
Navidrome's socket. It can't run programs from the folders it can write to.

When Shijhon is stopped, it has 30 seconds to shut down before systemd ends it. **Restart
Shijhon** on the dashboard shuts it down normally, and systemd starts it again.

## Local AI runners

Codex CLI and Claude Code, the [local AI runners](local-ai-runners.md) for playlists, run
inside Shijhon's sandbox. An agent connected [over MCP](../agent-access.md) needs none of
this. Give the
module the runner's package from Nixpkgs and a folder for its sign-in, which becomes
Shijhon's home folder in the sandbox:

```nix
services.shijhon = {
  aiRunner = {
    packages = [ pkgs.codex ]; # or pkgs.claude-code
    home = "/var/lib/shijhon-runner";
  };
  settings.playlists = {
    allow_cli = true;
    codex_command = "codex"; # or claude_command = "claude"
  };
};
environment.systemPackages = [ pkgs.codex ]; # to sign in
```

Switch, then sign the runner in as Shijhon's user, with that folder as its home. Use a
sign-in method that doesn't need a browser on the machine (see the runner's
documentation), for example:

```sh
sudo -u shijhon env HOME=/var/lib/shijhon-runner codex login --device-auth
sudo -u shijhon env HOME=/var/lib/shijhon-runner claude   # then /login
```

The sign-in stays in that folder, with mode 0700. The sandbox gives the runner CA
certificates (`SSL_CERT_FILE`), its user name and its home folder, but no user database.

The runner can reach everything Shijhon can: its state in `/data`, including Shijhon's
Navidrome password and the secrets saved on the dashboard, the placeholder folder, the
network, and Navidrome's socket, where a request can act as any user.
[Local AI runners](local-ai-runners.md) recommends running a runner where it has
nothing but its sign-in, and this sandbox isn't that, so weigh this before turning a
runner on.

The runner versions in Nixpkgs can be older than the ones Shijhon was checked with, and a
runner that doesn't accept a flag fails the run. So far the module has only been checked
with a stand-in command, not with the real runners.

## An existing Navidrome in detail

This adds to [an existing Navidrome](../nixos.md#an-existing-navidrome) in the guide.

### Navidrome's login limits

Navidrome can be set to trust Shijhon to pass on each app's address, so that its login
limits count each app separately ([client addresses](advanced-deployment.md#client-addresses)).
Without that, a device with an old password locks that user's other devices out for a few
seconds.

Over TCP, set Navidrome's `ExtAuth.TrustedSources` to `"127.0.0.1/32"` and `navidrome.url`
to `127.0.0.1` (`localhost` can mean `::1`). Navidrome then trusts every program on the
machine, and any of them can sign in to Navidrome as any user, so only do this on a machine
where you trust every user and program. A Unix socket that only Shijhon can open avoids
this: set `ExtAuth.TrustedSources = "@"`, as the managed Navidrome does.

Either way, Shijhon has to remove the user header Navidrome reads (`ExtAuth.UserHeader`,
or the deprecated `ReverseProxyUserHeader`) from apps' requests. Leave it at
`Remote-User`, or set `settings.server.reverse_proxy_user_header` to the same name.

### Over a Unix socket

Set the socket in Shijhon's settings and let Shijhon's sandbox reach it, and with
`account` also the sandbox of `shijhon-accounts`. Use a group that can open the socket and
that nobody else has. The `url` then only gives the `Host` header
([configuration](../configuration.md)):

```nix
services.shijhon.navidrome.url = "http://navidrome.invalid";
services.shijhon.settings.navidrome.socket = "/run/navidrome-socket/navidrome.sock";
systemd.services.shijhon.serviceConfig = {
  BindReadOnlyPaths = [ "/run/navidrome-socket" ];
  SupplementaryGroups = [ "navidrome-socket" ];
};
systemd.services.shijhon-accounts.serviceConfig = {
  BindReadOnlyPaths = [ "/run/navidrome-socket" ];
  SupplementaryGroups = [ "navidrome-socket" ];
};
```

### Other differences

- The placeholder folder gets `navidrome.group`, which is Shijhon's own group unless you
  set it.
- With `account` set, Shijhon waits at every boot until `shijhon-accounts` has reached
  Navidrome, so a Navidrome that starts late, such as a container, delays Shijhon too.

## Repairing the service account

If `shijhon-accounts` says the password in `/var/lib/shijhon/navidrome-password` doesn't
work for Navidrome's `shijhon` account, for example because the file was lost or
Navidrome's data came from elsewhere, give the account a new random password with
Navidrome's own command. Run it as Navidrome's user, with Shijhon and Navidrome stopped:

```sh
sudo systemctl stop shijhon navidrome
sudo sh -c 'umask 077; tr -dc a-f0-9 < /dev/urandom | head -c 48 > /var/lib/shijhon/navidrome-password; echo >> /var/lib/shijhon/navidrome-password'
sudo chown shijhon:shijhon /var/lib/shijhon/navidrome-password
sudo cat /var/lib/shijhon/navidrome-password # to paste twice below
nd=$(systemctl show -P ExecStart navidrome | sed -n 's/.*argv\[\]=\([^;]*\) ;.*/\1/p')
sudo systemd-run --pty --wait --collect -p User=navidrome -p Group=navidrome \
  -p WorkingDirectory=/var/lib/navidrome $nd user edit -u shijhon --set-password
sudo systemctl start navidrome
sudo systemctl restart shijhon-prepare shijhon-accounts && sudo systemctl start shijhon
```

`$nd` is Navidrome's program with the configuration its service uses. If you set
`services.navidrome.environmentFile`, add `-p EnvironmentFile=` with that file, because the
command needs its `ND_PASSWORDENCRYPTIONKEY`. If you changed Navidrome's `user`, `group` or
`DataFolder`, use yours. If the message says the account isn't an admin, run the same
command with `--set-admin` instead of `--set-password`, and skip the password steps.

Don't make Navidrome's socket reachable over TCP (with `socat`, for example) to use its web
interface instead, because Navidrome trusts that socket to act as any user.

## Catalog adapters

To add a catalog adapter, build it into Shijhon's Python environment with
`lib.mkShijhon`. For an adapter that provides an overlay for the pyproject.nix package
set:

```nix
services.shijhon.package = shijhon.lib.mkShijhon {
  inherit pkgs;
  extraOverlays = [ adapter.overlays.default ];
  extraPackages = [ "shijhon-catalog-example" ];
};
```

`extraOverlays` adds the package definitions and `extraPackages` picks the adapters to
install. They are resolved together with Shijhon's own dependencies, and the build fails
if their requirements conflict. When you write an adapter overlay, keep Shijhon's locked
dependencies, and give any extra dependency its own pinned source and hash. `python`
picks another compatible interpreter from Nixpkgs, and the package's `pythonEnvironment`
gives the whole interpreter environment for scripts you run alongside Shijhon.

An adapter runs with Shijhon's access, so review its source, dependencies and overlay
before installing it. `shijhon catalogs` lists the installed adapters (add
`config.services.shijhon.package` to `environment.systemPackages` to get the command).
Then choose the adapter on the dashboard's Catalog page.

## Music imports

To let people upload their own music files ([music imports](../music-imports.md)), create
an empty folder for them in the library that Shijhon can write to, and set `importsPath`:

```sh
sudo install -d -o shijhon -g navidrome -m 755 /srv/music/_imports
```

```nix
services.shijhon.importsPath = "/srv/music/_imports";
```

Navidrome must see the folder as `_imports` in its library. Back it up in full, including
hidden files, together with Shijhon's state.

## Moving from the Docker setup

Your Compose installation's data can move into the NixOS service. Navidrome keeps its
users, IDs, playlists and favorites, and the music folder can be at a different path.

1. Take a [backup](../deployment.md#backups), then stop everything with
   `docker compose down` in `packaging/`. The volumes are kept.

2. Write the configuration as in [the guide](../nixos.md#set-it-up), but leave out
   `account`, and switch. This creates the users and folders and starts a new, empty
   Navidrome. Since you have no account there yet, `shijhon-accounts` stops with a message
   and Shijhon doesn't start. Stop Navidrome and the export:

   ```sh
   sudo systemctl stop shijhon-usage-export navidrome
   ```

3. Replace the new data with the Compose installation's: Navidrome's volume
   (`shijhon_navidrome-data`, or your `NAVIDROME_DATA` folder), Shijhon's
   (`shijhon_shijhon-data`) and Shijhon's Navidrome password:

   ```sh
   sudo find /var/lib/navidrome /var/lib/shijhon -mindepth 1 -delete
   sudo docker run --rm -v shijhon_navidrome-data:/from:ro -v /var/lib/navidrome:/to alpine cp -a /from/. /to/
   sudo docker run --rm -v shijhon_shijhon-data:/from:ro -v /var/lib/shijhon:/to alpine cp -a /from/. /to/
   sudo cp packaging/config/navidrome-password /var/lib/shijhon/navidrome-password
   ```

4. Give the data to the new users, and the placeholder folder to Shijhon and Navidrome's
   group:

   ```sh
   sudo chown -R navidrome:navidrome /var/lib/navidrome
   sudo chown -R shijhon:shijhon /var/lib/shijhon
   sudo chmod 700 /var/lib/shijhon && sudo chmod 600 /var/lib/shijhon/navidrome-password
   sudo chown -R shijhon:navidrome /srv/music/_shijhon
   sudo find /srv/music/_shijhon -type d -exec chmod 2755 {} + -o -type f -exec chmod 644 {} +
   ```

   With music imports, set `importsPath` to your `MUSIC_IMPORTS_DIR` folder and change its
   owner the same way (`chown -R shijhon:navidrome`, keeping its permissions). Navidrome
   must see it as `_imports` in the library. If it's somewhere else, read
   [storage and recovery](../music-imports.md#storage-and-recovery) before moving it.

5. Move the settings you changed in `packaging/config/shijhon.toml` into
   `services.shijhon.settings`, and secrets into `environmentFile`. Settings saved on the
   dashboard are in the database and come along. If you gave Navidrome a
   `PasswordEncryptionKey`, give it the same one in `services.navidrome.environmentFile`.

6. Start Shijhon, then compare with what you had: playlists, song and album counts,
   favorites.

   ```sh
   sudo systemctl restart shijhon-accounts && sudo systemctl start shijhon
   ```

The usage export is created again. Don't start the Compose installation again on the same
data, because two copies of Shijhon must not share one state. To go back, copy the data
back the same way.

## The package alone

```sh
nix build github:Jasshl/shijhon/v0.6.1
./result/bin/shijhon version
```

In a checkout, `nix flake check` builds the package, checks it against `uv.lock` and runs
the service in a virtual machine. The package is built with
[uv2nix](https://pyproject-nix.github.io/uv2nix/) from the versions in `uv.lock`, uses
Python 3.12 and has ffmpeg on its `PATH`.
