# The NixOS service in a virtual machine, with the Navidrome it manages: the services and
# the usage export start, the accounts are made (and kept, the password file gone),
# Navidrome is on its socket only and counts apps apart by the address Shijhon names,
# Navidrome reads a placeholder Shijhon writes, Shijhon reads the export but not
# Navidrome's database, a local AI runner is inside Shijhon's sandbox (with the CA
# certificates), and Shijhon starts after its accounts unit failed once. A second machine
# has a services.navidrome of its own, whose admin account is there before Shijhon's
# accounts unit runs: Shijhon's service account is created through it, and given a new
# password with the guide's commands once its own is lost. Invented accounts, catalog
# (the tests' demo catalog) and commands only.
{
  pkgs,
  module,
  package,
}:
let
  password = "invented-password";
  socket = "/run/shijhon-navidrome/navidrome.sock";
  # Through Shijhon, as an app behind a proxy on this machine at that address.
  ping =
    user: pass: address:
    "curl -sS -H 'X-Forwarded-For: ${address}' 'http://127.0.0.1:4533/rest/ping.view?u=${user}&p=${pass}&v=1.16.1&c=vmtest&f=json'";
  # A stand-in for a local AI runner: it reads the CA certificates as an HTTPS client
  # does, and writes into its home, as a sign-in does.
  runner = pkgs.writeShellScriptBin "invented-runner" ''
    set -eu
    for bundle in "$SSL_CERT_FILE" /etc/ssl/certs/ca-certificates.crt; do
      found=""
      while read -r line; do
        case $line in *"BEGIN CERTIFICATE"*) found=1; break ;; esac
      done < "$bundle"
      [ -n "$found" ]
    done
    echo signed-in > "$HOME/invented-runner-login"
  '';
  # What both machines have: a person's library and password file.
  person = {
    users.users.librarian = {
      isNormalUser = true;
      group = "users";
    };
    systemd.tmpfiles.rules = [
      "d /srv/music 0755 librarian users - -"
      "d /etc/shijhon-test 0700 root root - -"
      "f /etc/shijhon-test/password 0600 root root - ${password}"
    ];
  };
in
pkgs.testers.runNixOSTest {
  name = "shijhon-service";
  nodes.existing =
    { config, lib, ... }:
    {
      imports = [
        module
        person
      ];
      virtualisation.memorySize = 2048;
      virtualisation.cores = 2;
      systemd.services.shijhon-accounts.serviceConfig.TimeoutStartSec = lib.mkForce "30min";
      # A Navidrome set up before Shijhon: its music folder (now libraryPath) and port.
      services.navidrome = {
        enable = true;
        settings = {
          MusicFolder = "/srv/music";
          Port = 4533;
        };
      };
      # Its admin account, the person's, there before Shijhon's accounts unit runs.
      systemd.services.existing-admin = {
        wants = [ "navidrome.service" ];
        after = [ "navidrome.service" ];
        before = [ "shijhon-accounts.service" ];
        requiredBy = [ "shijhon-accounts.service" ];
        path = [ pkgs.curl ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        script = ''
          until curl -fsS --unix-socket ${socket} http://localhost/ping; do sleep 2; done
          curl -fsS --unix-socket ${socket} -H 'Content-Type: application/json' \
            -d '{"username":"alex","password":"${password}"}' http://localhost/auth/createAdmin
        '';
      };
      environment.systemPackages = [ pkgs.curl ];
      services.shijhon = {
        enable = true;
        inherit package;
        libraryPath = "/srv/music";
        account = {
          name = "alex";
          passwordFile = "/etc/shijhon-test/password";
        };
        settings.catalog.kind = "demo";
      };
    };
  nodes.machine =
    { config, lib, ... }:
    {
      imports = [
        module
        person
      ];
      virtualisation.memorySize = 3072;
      virtualisation.cores = 2;
      # Without KVM the machine is emulated, many times slower than real hardware.
      systemd.services.shijhon-accounts.serviceConfig.TimeoutStartSec = lib.mkForce "30min";
      # Another member of Shijhon's own group: that is no way into Navidrome.
      users.users.groupmate = {
        isNormalUser = true;
        extraGroups = [ "shijhon" ];
      };
      environment.systemPackages = [
        pkgs.curl
        pkgs.sqlite
        config.services.shijhon.package
      ];
      services.shijhon = {
        enable = true;
        inherit package;
        libraryPath = "/srv/music";
        account = {
          name = "alex";
          passwordFile = "/etc/shijhon-test/password";
        };
        settings.catalog.kind = "demo";
        aiRunner = {
          packages = [ runner ];
          home = "/var/lib/shijhon-runner";
        };
      };
    };
  testScript = ''
    import json

    socket = "${socket}"


    def navidrome(path):
        """Navidrome itself, through its socket (root may open it)."""
        return machine.succeed(f"curl -fsS --unix-socket {socket} 'http://localhost{path}'")


    def subsonic(method, extra=""):
        answer = machine.succeed(
            f"curl -fsS 'http://127.0.0.1:4533/rest/{method}"
            f"?u=alex&p=${password}&v=1.16.1&c=vmtest&f=json{extra}'"
        )
        return json.loads(answer)["subsonic-response"]


    def as_shijhon(command):
        umask = machine.succeed("systemctl show -P UMask shijhon.service").strip()
        return (
            "systemd-run --wait --pipe --quiet -p User=shijhon -p Group=shijhon"
            f" -E PATH=/run/current-system/sw/bin -p UMask={umask} sh -c '{command}'"
        )


    start_all()
    machine.wait_for_unit("multi-user.target")
    print(machine.succeed("time shijhon version 2>&1"))
    try:
        for unit in ("navidrome", "shijhon-accounts", "shijhon", "shijhon-usage-export"):
            machine.wait_for_unit(f"{unit}.service", timeout=2400)
        machine.wait_for_open_port(4533, timeout=1200)
    finally:
        print(machine.execute(
            "journalctl --no-pager -u shijhon-prepare -u shijhon-accounts -u shijhon"
            " -u shijhon-usage-export | tail -60"
        )[1])

    with subtest("Navidrome only on its socket, which only Shijhon's units open"):
        machine.succeed(f"test \"$(stat -c '%U %G %a' {socket})\" = 'navidrome shijhon-navidrome 660'")
        machine.succeed("test \"$(stat -c '%a' /run/shijhon-navidrome)\" = 2750")
        machine.fail("ss -ltnp | grep -q navidrome")
        for other in ("librarian", "groupmate"):
            machine.fail(
                f"systemd-run --wait --pipe --quiet -p User={other}"
                f" /run/current-system/sw/bin/curl -fsS --unix-socket {socket} http://localhost/ping"
            )
        navidrome("/ping")

    with subtest("accounts"):
        machine.succeed(
            "test \"$(stat -c '%U %a' /var/lib/shijhon/navidrome-password)\" = 'shijhon 600'"
        )
        machine.succeed(
            "pw=$(cat /var/lib/shijhon/navidrome-password) && curl -fsS"
            f" --unix-socket {socket} -H 'Content-Type: application/json'"
            " -d '{\"username\":\"shijhon\",\"password\":\"'\"$pw\"'\"}'"
            " http://localhost/auth/login | grep -q '\"isAdmin\":true'"
        )
        machine.wait_until_succeeds("${ping "alex" password "10.9.0.1"} | grep -q '\"status\":\"ok\"'")
        # The copy of the person's password went once the accounts were there.
        machine.wait_until_succeeds("! test -e /run/shijhon-secrets/account-password")
        assert subsonic("getUser", "&username=alex")["user"]["adminRole"]

    with subtest("Navidrome counts apps apart by the address Shijhon names"):
        token = json.loads(machine.succeed(
            f"curl -fsS --unix-socket {socket} -H 'Content-Type: application/json'"
            " -d '{\"username\":\"alex\",\"password\":\"${password}\"}' http://localhost/auth/login"
        ))["token"]
        machine.succeed(
            f"curl -fsS --unix-socket {socket} -H 'Content-Type: application/json'"
            f" -H 'X-ND-Authorization: Bearer {token}'"
            " -d '{\"userName\":\"robin\",\"name\":\"robin\",\"password\":\"robin-password\",\"isAdmin\":true}'"
            " http://localhost/api/user"
        )
        machine.succeed("for i in 1 2 3 4 5 6; do ${
          ping "robin" "wrong" "10.9.0.7"
        } > /dev/null || true; done")
        refused = machine.succeed("${ping "robin" "robin-password" "10.9.0.7"}")
        assert '"status":"failed"' in refused, refused
        machine.succeed("${ping "robin" "robin-password" "10.9.0.8"} | grep -q '\"status\":\"ok\"'")

    with subtest("Navidrome reads a placeholder Shijhon writes"):
        folder = "/srv/music/_shijhon"
        machine.succeed(f"test \"$(stat -c '%U %G %a' {folder})\" = 'shijhon navidrome 2755'")
        machine.succeed("test \"$(stat -c '%U %G %a' /srv/music)\" = 'librarian users 755'")
        # A favorite of a catalog song: Shijhon writes the release into the library.
        subsonic("star", "&id=sh.tr.demo.900000136")
        machine.wait_until_succeeds(f"find {folder} -name '*.flac' | grep -q .", timeout=900)
        written = machine.succeed(f"find {folder} -name '*.flac' | head -1").strip()
        machine.succeed(f"test \"$(stat -c '%U %G %a' \"{written}\")\" = 'shijhon navidrome 644'")
        machine.succeed(
            f"test \"$(stat -c '%G %a' \"$(dirname \"{written}\")\")\" = 'navidrome 2755'"
        )
        machine.wait_until_succeeds(
            f"curl -fsS --unix-socket {socket} 'http://localhost/rest/search3.view"
            "?u=alex&p=${password}&v=1.16.1&c=vmtest&f=json&query=FROZEN'"
            " | grep -q '\"title\":\"FROZEN\"'",
            timeout=900,
        )

    with subtest("usage export"):
        export = "/var/lib/shijhon-usage/usage.sqlite3"
        machine.wait_until_succeeds(f"test -s {export}")
        machine.succeed(f"test \"$(stat -c '%U %G %a' {export})\" = 'navidrome shijhon 640'")
        # A request as Shijhon writes it is answered: the exporter reads Shijhon's file.
        machine.succeed(
            as_shijhon(f"echo vmtest:1 > {export}.request.new && mv {export}.request.new {export}.request")
        )
        machine.wait_until_succeeds(
            f"test \"$(sqlite3 'file:{export}?mode=ro' 'SELECT answers FROM shijhon_usage_export')\""
            " = vmtest:1"
        )
        machine.succeed(as_shijhon(f"cat {export} > /dev/null"))
        machine.fail(as_shijhon("cat /var/lib/navidrome/navidrome.db > /dev/null"))

    with subtest("a local AI runner inside Shijhon's sandbox"):
        pid = machine.succeed("systemctl show -P MainPID shijhon.service").strip()
        environment = machine.succeed(f"tr '\\0' '\\n' < /proc/{pid}/environ")
        assert "HOME=/runner" in environment.splitlines(), environment
        assert "${runner}/bin" in environment, environment
        uid = machine.succeed("id -u shijhon").strip()
        gid = machine.succeed("id -g shijhon").strip()
        certificates = next(
            line for line in environment.splitlines() if line.startswith("SSL_CERT_FILE=")
        )
        machine.succeed(
            f"nsenter -t {pid} -m -r -w -S {uid} -G {gid} -- ${pkgs.runtimeShell} -c"
            f" 'HOME=/runner {certificates} PATH=${runner}/bin invented-runner'"
        )
        # Not in the other units' sandboxes.
        accounts = machine.succeed(
            "systemctl show -P ExecPaths shijhon-accounts.service shijhon-usage-export.service"
        )
        assert "${runner}" not in accounts
        machine.succeed(
            "test \"$(stat -c '%U %a' /var/lib/shijhon-runner/invented-runner-login)\" = 'shijhon 644'"
        )
        machine.succeed("test \"$(stat -c '%a' /var/lib/shijhon-runner)\" = 700")

    with subtest("accounts again, after a restart, with the password file gone"):
        machine.succeed("rm /etc/shijhon-test/password")
        machine.succeed("systemctl restart shijhon-prepare.service shijhon-accounts.service")
        machine.wait_for_unit("shijhon.service")
        machine.wait_until_succeeds(
            "${ping "alex" password "10.9.0.1"} | grep -q '\"status\":\"ok\"'", timeout=1200
        )
        machine.succeed("journalctl -u shijhon-accounts | grep -q 'kept, with the password it has'")

    with subtest("Shijhon starts once its accounts unit succeeds after a failed try"):
        machine.succeed("systemctl stop shijhon.service shijhon-accounts.service")
        # Navidrome out of reach: its socket's folder closed to the socket's group.
        machine.succeed("chmod 0700 /run/shijhon-navidrome")
        since = machine.succeed("date '+%Y-%m-%d %H:%M:%S'").strip()
        machine.succeed("systemctl start --no-block shijhon.service")
        # (Its wait for Navidrome ends after 180 s: status 1, started again in 10.)
        machine.wait_until_succeeds(
            f"journalctl -u shijhon-accounts --since '{since}' | grep -q 'does not answer'",
            timeout=1800,
        )
        machine.succeed("chmod 2750 /run/shijhon-navidrome")
        machine.wait_for_unit("shijhon.service", timeout=1800)
        machine.succeed(
            f"journalctl -u shijhon-accounts --since '{since}' | grep -q 'kept, with the password'"
        )
        machine.wait_until_succeeds(
            "${ping "alex" password "10.9.0.1"} | grep -q '\"status\":\"ok\"'", timeout=1200
        )

    with subtest("a Navidrome with an admin account gets Shijhon's through it"):
        try:
            existing.wait_for_unit("shijhon.service", timeout=2400)
            existing.wait_for_open_port(4533, timeout=1200)
        finally:
            print(existing.execute(
                "journalctl --no-pager -u existing-admin -u shijhon-accounts -u shijhon | tail -40"
            )[1])
        existing.succeed(
            "journalctl -u shijhon-accounts | grep -q 'created for Shijhon, an admin, by your account'"
        )
        existing.succeed(
            "pw=$(cat /var/lib/shijhon/navidrome-password) && curl -fsS"
            f" --unix-socket {socket} -H 'Content-Type: application/json'"
            " -d '{\"username\":\"shijhon\",\"password\":\"'\"$pw\"'\"}'"
            " http://localhost/auth/login | grep -q '\"isAdmin\":true'"
        )
        # The person's account and password are as they were.
        existing.wait_until_succeeds(
            "${ping "alex" password "10.9.0.1"} | grep -q '\"status\":\"ok\"'", timeout=1200
        )
        existing.wait_until_succeeds("! test -e /run/shijhon-secrets/account-password")
        existing.fail("ss -ltnp | grep -q navidrome")

    with subtest("a lost service-account password, set again as the guide says"):
        existing.succeed("systemctl stop shijhon navidrome")
        existing.succeed("rm /var/lib/shijhon/navidrome-password")
        existing.succeed("systemctl start navidrome && systemctl restart shijhon-prepare")
        since = existing.succeed("date '+%Y-%m-%d %H:%M:%S'").strip()
        existing.fail("systemctl restart shijhon-accounts")  # (what needs changing: no retry)
        existing.succeed(
            f"journalctl -u shijhon-accounts --since '{since}'"
            " | grep -q 'Repairing the service account'"
        )
        existing.succeed("! test -e /var/lib/shijhon/navidrome-password")
        # The documented commands (docs/reference/nixos.md, "Repairing the service account").
        existing.succeed("systemctl stop shijhon navidrome")
        existing.succeed(
            "sh -c 'umask 077; tr -dc a-f0-9 < /dev/urandom | head -c 48"
            " > /var/lib/shijhon/navidrome-password; echo >> /var/lib/shijhon/navidrome-password'"
        )
        existing.succeed("chown shijhon:shijhon /var/lib/shijhon/navidrome-password")
        new = existing.succeed("cat /var/lib/shijhon/navidrome-password").strip()
        assert len(new) == 48, new
        # The password typed twice at a terminal (script's: systemd-run --pty forwards
        # no input from a pipe).
        print(existing.succeed(
            r"nd=$(systemctl show -P ExecStart navidrome | sed -n 's/.*argv\[\]=\([^;]*\) ;.*/\1/p')"
            f" && export nd && printf '%s\\n%s\\n' {new} {new} | script -qec"
            " 'systemd-run --pty --wait --collect -p User=navidrome -p Group=navidrome"
            " -p WorkingDirectory=/var/lib/navidrome $nd user edit -u shijhon --set-password'"
            " /dev/null 2>&1",
            timeout=900,
        ))
        existing.succeed("systemctl start navidrome")
        existing.succeed("systemctl restart shijhon-prepare shijhon-accounts")
        existing.succeed("systemctl start shijhon")
        existing.wait_for_open_port(4533, timeout=1200)
        existing.succeed(
            f"curl -fsS --unix-socket {socket} -H 'Content-Type: application/json'"
            f" -d '{{\"username\":\"shijhon\",\"password\":\"{new}\"}}'"
            " http://localhost/auth/login | grep -q '\"isAdmin\":true'"
        )
        existing.wait_until_succeeds(
            "${ping "alex" password "10.9.0.1"} | grep -q '\"status\":\"ok\"'", timeout=1200
        )
  '';
}
