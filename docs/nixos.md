# NixOS

Shijhon's flake has a NixOS service. It runs Navidrome from your Nixpkgs with Shijhon in
front of it, and creates the Navidrome accounts at the first start. Docker Compose is the
other way to install Shijhon ([quick start](quick-start.md)).

You need NixOS with flakes, on Linux x86-64 or ARM64, with systemd 257 or newer.

## Set it up

Your music stays where it is, and Navidrome and Shijhon both need to be able to read it.
The section [on your music folder](#your-music-folder) below explains how to check that.

If you don't run Navidrome yet, follow the steps below.

If you already run `services.navidrome` on this machine, make
[these changes](#you-already-run-servicesnavidrome) and then follow the steps. Navidrome
keeps its accounts, playlists and favorites.

If you run Navidrome another way on this machine, for example in a container or with a
unit of your own, follow the steps together with
[an existing Navidrome](#an-existing-navidrome). This has been tried on one server with
Navidrome in a container, and in test virtual machines with Navidrome as a systemd unit of
its own.

If you are moving from Shijhon's Docker setup, see
[moving from the Docker setup](reference/nixos.md#moving-from-the-docker-setup). If you
used Shijhon's NixOS service from 0.4.0 or earlier, see
[from the earlier NixOS service](reference/nixos.md#from-the-earlier-nixos-service).
Neither of these has been tried yet.

1. **Add the flake input** to your system's `flake.nix`, pinned to a release tag, and
   import the module:

   ```nix
   {
     inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
     inputs.shijhon.url = "github:Jasshl/shijhon/v0.10.0"; # a release tag

     outputs = { nixpkgs, shijhon, ... }: {
       nixosConfigurations.yourhost = nixpkgs.lib.nixosSystem {
         system = "x86_64-linux";
         modules = [
           ./configuration.nix
           shijhon.nixosModules.default
         ];
       };
     };
   }
   ```

   Don't add `inputs.shijhon.inputs.nixpkgs.follows`. Shijhon is built with the Nixpkgs
   it was tested with, and Navidrome still comes from yours. The first evaluation takes a
   while, because it builds Shijhon's package.

2. **Enable the service** in `configuration.nix`:

   ```nix
   services.shijhon = {
     enable = true;
     libraryPath = "/srv/music";
     account = {
       name = "alex";
       passwordFile = "/etc/shijhon/alex-password";
     };
   };
   ```

   `libraryPath` is your music folder. `account` is your own Navidrome account, the one
   you sign in to apps and the dashboard with. Paths can only contain letters, digits and
   `_ . / @ + -`.

3. **Write your password** to that file, readable only by root:

   ```sh
   sudo install -d -m 700 /etc/shijhon
   sudo sh -c 'umask 077; IFS= read -rsp "Password: " pw && echo && printf "%s\n" "$pw" > /etc/shijhon/alex-password'
   ```

   Don't put the password in your Nix files, because everything in the Nix store can be
   read by every user. The file is only read at the first start, and can be deleted once
   the accounts exist. A decrypted secret from agenix or sops-nix works too.

4. **Switch** and check that everything started. Run these on that machine, in the folder
   with your system's `flake.nix`:

   ```sh
   sudo nixos-rebuild switch --flake .#yourhost
   systemctl status navidrome shijhon-prepare shijhon-accounts shijhon shijhon-usage-export
   ```

   `systemctl status` shows Shijhon as active a little before it answers, so if the
   dashboard doesn't open yet, wait until `journalctl -u shijhon` says "Application startup
   complete". The first start creates your account as an admin, and an account for Shijhon
   itself, `shijhon`. One "Unsuccessful login" warning in Navidrome's log
   (`journalctl -u navidrome`) is expected: it comes from the first try to sign in as
   `shijhon` before that account exists. If something needs fixing first, such as a wrong
   password, `journalctl -u shijhon-accounts` says what, and Shijhon doesn't start. After
   fixing it, run:

   ```sh
   sudo systemctl restart shijhon-prepare shijhon-accounts
   sudo systemctl start shijhon
   ```

   If Navidrome didn't answer, `shijhon-accounts` tries again for about a quarter of an
   hour and then gives up. In that case, run `sudo systemctl reset-failed shijhon-accounts`
   first.

5. **Open the dashboard** at `http://127.0.0.1:4533/shijhon/` on that machine. From
   another computer, run `ssh -L 4533:127.0.0.1:4533 yourhost` on that computer first and
   open the same address there. Sign in with your account. Add add-ons and choose a
   catalog there too ([add-ons](add-ons.md), [catalogs](catalogs.md)).

6. **Let your apps reach it.** Add Shijhon in your apps as a **Subsonic** (or
   OpenSubsonic) server. On a network you trust, apps can connect directly, without
   encryption, at `http://<this machine's address>:4533`:

   ```nix
   services.shijhon.listenAddress = "0.0.0.0";
   services.shijhon.openFirewall = true;
   ```

   From outside your network, put a TLS reverse proxy on the same machine in front of
   Shijhon and keep Shijhon on `127.0.0.1`. With Caddy, which gets the certificate itself:

   ```nix
   services.caddy = {
     enable = true;
     virtualHosts."music.example.com".extraConfig = ''
       reverse_proxy 127.0.0.1:4533
     '';
   };
   networking.firewall.allowedTCPPorts = [ 80 443 ];
   ```

   Another proxy works if it meets
   [these requirements](deployment.md#network-and-reverse-proxy). If you give the proxy
   an access log, keep query strings out of it, because apps send their credentials
   there.

7. **Add other people** in Navidrome's web interface, at `/app` on Shijhon's address,
   under **Users**. Each person signs in to apps and the dashboard with their own account.
   Passwords are changed there too.

8. **Connect an AI agent**, if you use one. Enter Shijhon's HTTPS address on the dashboard
   under **Server → Agent access**, or set `services.shijhon.settings.agents.public_url`.
   **Playlists** then shows the address to add in the agent app
   ([agent access](agent-access.md)).

### Your music folder

`libraryPath` is the folder your music is in now. Shijhon doesn't move, rename or retag
anything in it, and doesn't change its permissions. It only adds the `_shijhon`
placeholder folder, and you keep adding music as before. Music you add appears in apps by
itself, usually within a minute or two and at most about five minutes after Navidrome has
scanned it, and **Collection → More → Refresh music** on the dashboard shows it at once.

The one exception to leaving the folder alone is opt-in. With `libraryWritable = true;`
the folder is writable for Shijhon, and **Also fix files in your music folder** on the
dashboard may then add lists of artists to files whose one artist text names several
([how it works](how-it-works.md#each-artist-of-a-joined-credit)). A file is changed only
when it is owned by Shijhon's user, and Diagnostics says when files are left as they are.

The folder has to be outside `/home`, `/root` and `/run/user`, because the services can't
see those. If your music is in your home folder, move it, for example to `/srv/music`, or
mount it there as well:

```nix
fileSystems."/srv/music" = {
  device = "/home/alex/Music";
  options = [ "bind" ];
};
```

Navidrome and Shijhon run as their own users, so both need to be able to read every file.
A folder everyone can read, which is what the default permissions give you, works as it
is. After the first switch, this prints the first file Shijhon can't read, or nothing if
it can read them all (use `-u navidrome` to check Navidrome):

```sh
sudo -u shijhon find /srv/music ! -readable -print -quit
```

If only you can read your music, either make it readable by everyone with
`sudo chmod -R a+rX /srv/music`, or give its group read access and add both users to that
group:

```nix
users.users.shijhon.extraGroups = [ "users" ]; # the library's group
users.users.navidrome.extraGroups = [ "users" ];
```

A music folder that Navidrome's own NixOS module created can only be read by Navidrome.
`sudo chmod 755 /srv/music` fixes that.

### You already run `services.navidrome`

The module takes over the `services.navidrome` you have and sets it up for Shijhon.
Navidrome keeps its accounts, passwords, playlists, favorites and play counts, and your
songs keep their IDs. If you added more than one library in Navidrome, Shijhon uses the
first one.

If your Navidrome settings have no `MusicFolder`, your music is in
`/var/lib/navidrome/music`, where Shijhon can't read it. Move it out first, with Navidrome
stopped. The songs keep their IDs.

```sh
sudo systemctl stop navidrome
sudo mv /var/lib/navidrome/music /srv/music && sudo chmod 755 /srv/music
```

1. **Back up Navidrome's data** with Navidrome stopped:

   ```sh
   sudo systemctl stop navidrome
   sudo sh -c 'umask 077; tar -czf /root/navidrome-backup.tar.gz -C / var/lib/navidrome'
   sudo systemctl start navidrome
   ```

2. **Remove the settings Shijhon now sets** from your `services.navidrome`: `openFirewall`,
   and in `settings` the `MusicFolder` (that folder becomes `libraryPath`), `Address`,
   `Port`, `UnixSocketPerm`, `BaseUrl`, `ExtAuth.TrustedSources`, `ReverseProxyWhitelist`
   and `ReverseProxyUserHeader`. If you leave in one that conflicts with what Shijhon sets,
   the build stops with a message. The others don't stop the build and are ignored, but
   remove them anyway.
   If you use an `environmentFile`, remove the same settings from it as well
   (`ND_ADDRESS`, `ND_PORT`, `ND_MUSICFOLDER`, `ND_BASEURL`, `ND_UNIXSOCKETPERM`,
   `ND_EXTAUTH_TRUSTEDSOURCES`, `ND_EXTAUTH_USERHEADER`, `ND_REVERSEPROXYWHITELIST` and
   `ND_REVERSEPROXYUSERHEADER`), because the build can't see that file. Keep everything
   else, such as `ND_PASSWORDENCRYPTIONKEY`, which your accounts need.

   If a reverse proxy signed people in to Navidrome with a header, set that up in Shijhon
   instead ([sign-in by the reverse proxy](reference/advanced-deployment.md#sign-in-by-the-reverse-proxy)).

3. **Set `account`** to your Navidrome admin account and its password file, as in steps 2
   and 3. At the first start Shijhon uses it once to create its own account. Your account
   isn't changed.

4. **Point your apps at Shijhon.** Shijhon listens on port 4533, Navidrome's default, so a
   reverse proxy pointing at `127.0.0.1:4533` keeps working. Shijhon listens only on this
   machine by default, so if apps connected to Navidrome directly over the network, add
   step 6's two lines, or they can't connect. Change their server type to Subsonic. They
   sign in with the same accounts.

5. **Switch** and check as in step 4, then open the dashboard as in step 5.

## An existing Navidrome

If you run Navidrome yourself on this machine, in a container or with a unit of your own,
turn off the management and give Shijhon Navidrome's address and your admin account:

```nix
services.shijhon = {
  enable = true;
  libraryPath = "/srv/music";
  account = {
    name = "alex"; # an admin account your Navidrome has
    passwordFile = "/etc/shijhon/alex-password";
  };
  navidrome = {
    manage = false;
    url = "http://127.0.0.1:4533";
    group = "navidrome"; # the group your Navidrome reads the library with
  };
  port = 8765;
};
```

At the first start Shijhon uses your account once to create its own admin account in
Navidrome. To create that account yourself instead, create it in Navidrome's web interface
under **Users**, as an admin with a long random password, and write that password to a file
only root can read:

```sh
sudo install -d -m 700 /etc/shijhon
sudo sh -c 'umask 077; IFS= read -rsp "Password: " pw && echo && printf "%s\n" "$pw" > /etc/shijhon/navidrome-password'
```

Then set `navidrome.passwordFile = "/etc/shijhon/navidrome-password";` in place of
`account`.

Your Navidrome has to meet [what Shijhon needs](deployment.md#what-shijhon-needs-from-navidrome).
Only Shijhon should reach it, so it should listen on `127.0.0.1` or a Unix socket, not on
the network. If Navidrome uses port 4533, its default, give Shijhon another `port`, as in
the example, and point your apps at that port.

Navidrome has to see the same music files as Shijhon, including the `_shijhon` folder, but
it can see them at a different path. If its library isn't the first one, set
`settings.navidrome.library_id`. For the usage export
([what it is](deployment.md#the-usage-export)), also set `usageExport.enable = true`,
`usageExport.navidromeDataDirectory` and `usageExport.user`. Without it, the cleanup
doesn't run, and when an album is filled from another release, the earlier release's
songs stay in the library.

Steps 4 and 5 then differ a little. There is no `navidrome` unit of Shijhon's, and
`shijhon-usage-export` only with the usage export, so check with:

```sh
systemctl status shijhon-prepare shijhon-accounts shijhon
```

Add `shijhon-usage-export` if you turned it on. The dashboard and your apps use Shijhon's
`port`, so with the example the dashboard is at `http://127.0.0.1:8765/shijhon/`, and the
`ssh -L` of step 5 is `ssh -L 8765:127.0.0.1:8765 yourhost`.

The reference covers [connecting over a Unix socket and Navidrome's login limits](reference/nixos.md#an-existing-navidrome-in-detail).

## Upgrades

1. Read the [changelog](../CHANGELOG.md). Options can change between beta versions.
2. Change the tag in `inputs.shijhon.url`, run `nix flake update shijhon` and switch.

Shijhon updates its database at the first start, and an older version won't start on the
updated one. Before it changes the database, that start writes a checked snapshot of it
into `/var/lib/shijhon/snapshots`, as the [deployment guide](deployment.md#upgrades)
describes, also for when the snapshot can't be written. `journalctl -u shijhon` then says
why; make a backup with `shijhon backup` ([below](#backups)). Setting
`services.shijhon.settings.upgrade_snapshots = false` updates without a snapshot. With
music imports, back up the imports folder before you switch.

To go back, stop Shijhon and copy the snapshot beside the database under a temporary name.
Once the copy is complete, remove the database's `-wal` and `-shm` files, which belong to
the database being replaced, and rename the copy:

```sh
sudo systemctl stop shijhon
sudo -u shijhon sh -c 'cd /var/lib/shijhon &&
    install -m 600 snapshots/shijhon-schema37-20261112T153000Z-4.sqlite3 restored.sqlite3 &&
    rm -f shijhon.sqlite3-wal shijhon.sqlite3-shm && mv restored.sqlite3 shijhon.sqlite3'
```

Restore what changed since with it, from the same time: Navidrome's data, the placeholder
folder and, with music imports, the imports folder
([in detail](reference/advanced-deployment.md#restoring-a-backup)). Only then switch to the
older tag, which starts Shijhon again. Work done since the update is lost.

### The Navidrome version

Shijhon is tested with Navidrome 0.64.2 and takes Navidrome from your Nixpkgs. If your
Nixpkgs has another version, which can also happen after you update it, the build stops
and says so. To try that version anyway, set
`services.shijhon.navidrome.allowUntestedVersion = true;`. The build and Shijhon's log then
warn that it hasn't been tested. For a Navidrome you run yourself, the build can't check
the version, so only the log warns.

## Backups

`shijhon backup` copies Shijhon's database while Shijhon runs, and checks the copy. Add
`config.services.shijhon.package` to `environment.systemPackages` to get the command, and
run it as the service's user, which it needs
([in detail](reference/advanced-deployment.md#backups-in-detail)):

```sh
sudo -u shijhon shijhon backup --database /var/lib/shijhon/shijhon.sqlite3
```

It writes a folder into `/var/lib/shijhon/backups` with the copy and a `README.txt`, and
prints what it holds. Run this way, it holds neither your settings, which are in your NixOS
configuration, nor the service account's password file. The database holds the settings
saved on the dashboard, secrets included, so the folder is readable by the service's user
only. Copy it somewhere safe and remove it from the state folder.

For a set to restore together, stop Shijhon and save its state folder and the placeholder
folder together, then start it again. This also keeps the password file the module created.
With music imports, save the imports folder too. With `libraryWritable` and **Also fix files
in your music folder** on, Shijhon changes the artist tags of files in your library and
records in its state what undoes each change, so back up the library at the same time. The
archive contains the settings saved on the dashboard, secrets included, so keep it private.

```sh
sudo systemctl stop shijhon
sudo sh -c 'umask 077; tar -czf /root/shijhon-backup.tar.gz -C / var/lib/shijhon srv/music/_shijhon'
sudo systemctl start shijhon
```

Navidrome can make its own backups:

```nix
services.navidrome.settings.Backup = {
  Path = "/var/backup/navidrome";
  Schedule = "30 0 * * *";
  Count = 14;
};
```

The other way is to save `/var/lib/navidrome` with Navidrome stopped.

## More

[The NixOS reference](reference/nixos.md) has every option and what the service sets up,
and covers local AI runners for playlists, catalog adapters, music imports, repairing
Shijhon's Navidrome account, moving from the Docker setup and from the earlier NixOS
service.
