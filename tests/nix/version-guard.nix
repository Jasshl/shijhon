# With a Navidrome version Shijhon was not tested with, the configuration does not build
# unless navidrome.allowUntestedVersion is set, which turns the refusal into a warning.
{
  pkgs,
  nixpkgs,
  module,
}:
let
  inherit (nixpkgs) lib;
  configured =
    allow:
    (nixpkgs.lib.nixosSystem {
      inherit (pkgs.stdenv.hostPlatform) system;
      modules = [
        module
        {
          boot.isContainer = true;
          system.stateVersion = "26.05";
          services.navidrome.package = pkgs.navidrome // {
            version = "0.0.1";
          };
          services.shijhon = {
            enable = true;
            libraryPath = "/srv/music";
            navidrome.allowUntestedVersion = allow;
          };
        }
      ];
    }).config;
  refusals =
    config:
    lib.filter (lib.hasPrefix "services.shijhon") (
      map (a: a.message) (lib.filter (a: !a.assertion) config.assertions)
    );
  refused = configured false;
  tried = configured true;
in
assert lib.assertMsg (lib.any (lib.hasInfix "allowUntestedVersion = true") (
  refusals refused
)) "an untested Navidrome version is refused";
assert lib.assertMsg (refusals tried == [ ]) "allowUntestedVersion allows it";
assert lib.assertMsg (lib.any (lib.hasInfix "0.0.1 has not been tested") tried.warnings)
  "allowUntestedVersion warns";
pkgs.runCommand "shijhon-navidrome-version-guard" { } "touch $out"
