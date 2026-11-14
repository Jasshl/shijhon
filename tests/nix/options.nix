# What the module makes of the ways people bring their own Navidrome, evaluated only: a
# Navidrome they run themselves with their admin account or a service account they made,
# and a services.navidrome they ran before, whose own settings Shijhon's must replace.
# It checks that the assertions give these messages, not that a build shows them before
# Nix's own error about conflicting definitions (they come first: the toplevel checks
# assertions before it reads Navidrome's settings).
{
  pkgs,
  nixpkgs,
  module,
}:
let
  inherit (nixpkgs) lib;
  configured =
    extra:
    (nixpkgs.lib.nixosSystem {
      inherit (pkgs.stdenv.hostPlatform) system;
      modules = [
        module
        {
          boot.isContainer = true;
          system.stateVersion = "26.05";
          services.shijhon = {
            enable = true;
            libraryPath = "/srv/music";
          };
        }
        extra
      ];
    }).config;
  refusals =
    config:
    lib.filter (lib.hasPrefix "services.shijhon") (
      map (a: a.message) (lib.filter (a: !a.assertion) config.assertions)
    );
  refusedWith =
    words: config:
    lib.any (lib.hasInfix words) (refusals config)
    || throw "not refused with \"${words}\": ${builtins.toJSON (refusals config)}";
  accepted = config: refusals config == [ ] || throw (builtins.toJSON (refusals config));
  own = {
    services.shijhon.navidrome = {
      manage = false;
      url = "http://127.0.0.1:4534";
    };
  };
  through = configured {
    imports = [ own ];
    services.shijhon.account.name = "alex";
  };
  byHand = configured {
    imports = [ own ];
    services.shijhon.navidrome.passwordFile = "/etc/shijhon/navidrome-password";
  };
  accountsUnit = through.systemd.services.shijhon-accounts;
  withPassword = configured {
    imports = [ own ];
    services.shijhon.account = {
      name = "alex";
      passwordFile = "/etc/shijhon/alex-password";
    };
  };
  passwordUnit = withPassword.systemd.services.shijhon-account-password;
  # A services.navidrome as people ran it before Shijhon.
  before = settings: {
    services.navidrome = {
      enable = true;
      inherit settings;
    };
  };
  # The library bound read-only into Shijhon's service, or read-write when allowed.
  shijhonBinds = config: config.systemd.services.shijhon.serviceConfig;
  writable = configured { services.shijhon.libraryWritable = true; };
  kept = configured (before {
    MusicFolder = "/srv/music";
    Port = 4533;
    ReverseProxyUserHeader = "X-Invented-User";
  });
in
# The music folder: read-only unless libraryWritable.
assert lib.elem "/srv/music:/music" (shijhonBinds through).BindReadOnlyPaths;
assert !(lib.elem "/srv/music:/music" (shijhonBinds through).BindPaths);
assert lib.elem "/srv/music:/music" (shijhonBinds writable).BindPaths;
assert !(lib.elem "/srv/music:/music" (shijhonBinds writable).BindReadOnlyPaths);
# Their own Navidrome, the service account made through their admin account.
assert accepted through;
assert lib.elem "shijhon-accounts.service" through.systemd.services.shijhon.requires;
assert through.services.shijhon.settings.navidrome.password_file == "/data/navidrome-password";
assert through.systemd.services.shijhon.serviceConfig.LoadCredential == [ ];
assert !accountsUnit.serviceConfig.PrivateNetwork;
assert lib.elem "AF_INET" accountsUnit.serviceConfig.RestrictAddressFamilies;
assert lib.hasInfix " --account alex" accountsUnit.serviceConfig.ExecStart;
# Its tries end, so that a switch or Shijhon's start fails instead of waiting for good.
assert accountsUnit.unitConfig.StartLimitIntervalSec == "30min";
assert accountsUnit.unitConfig.StartLimitBurst == 5;
# The person's password copied again at each start of shijhon-accounts, not only by
# shijhon-prepare at boot; and only with a password file.
assert accepted withPassword;
assert lib.elem "shijhon-accounts.service" passwordUnit.requiredBy;
assert lib.elem "shijhon-accounts.service" passwordUnit.before;
assert !(passwordUnit.serviceConfig.RemainAfterExit or false);
assert lib.hasInfix "shijhon-account-password" passwordUnit.serviceConfig.ExecStart;
assert
  !(lib.hasInfix "shijhon-account-password" (
    builtins.toJSON withPassword.systemd.services.shijhon-prepare.serviceConfig.ExecStart
  ));
assert !(through.systemd.services ? shijhon-account-password);
# The service account's name is no person's account, as setup.sh refuses it.
assert refusedWith "the name of Shijhon's own account" (configured {
  imports = [ own ];
  services.shijhon.account.name = "Shijhon";
});
# Or with a service account they made.
assert accepted byHand;
assert !(byHand.systemd.services ? shijhon-accounts);
assert
  byHand.systemd.services.shijhon.serviceConfig.LoadCredential == [
    "navidrome-password:/etc/shijhon/navidrome-password"
  ];
assert refusedWith "not both" (configured {
  imports = [ own ];
  services.shijhon.account.name = "alex";
  services.shijhon.navidrome.passwordFile = "/etc/shijhon/navidrome-password";
});
assert refusedWith "set either account" (configured own);
# A services.navidrome they had: the same music folder and a port are no conflict, the
# deprecated user header is overruled, and its firewall port is named.
assert accepted kept;
assert kept.services.navidrome.settings.ExtAuth.UserHeader == "Remote-User";
assert lib.any (lib.hasInfix "services.navidrome.openFirewall")
  (configured {
    services.navidrome.openFirewall = true;
  }).warnings;
assert refusedWith "remove services.navidrome.settings.Address" (
  configured (before {
    Address = "0.0.0.0";
  })
);
assert refusedWith "remove services.navidrome.settings.MusicFolder" (
  configured (before {
    MusicFolder = "/mnt/music";
  })
);
assert refusedWith "remove services.navidrome.settings.MusicFolder" (
  configured (before {
    MusicFolder = lib.mkForce "/mnt/music";
  })
);
assert refusedWith "remove services.navidrome.settings.ExtAuth.TrustedSources" (
  configured (before {
    ExtAuth.TrustedSources = "127.0.0.1/32";
  })
);
pkgs.runCommand "shijhon-module-options" { } "touch $out"
