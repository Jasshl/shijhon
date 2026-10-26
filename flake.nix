{
  description = "Shijhon native package and NixOS service";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs =
    inputs@{ self, nixpkgs, ... }:
    let
      forLinux = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
      mkShijhon = import ./nix/package.nix {
        inherit (inputs) pyproject-nix uv2nix pyproject-build-systems;
      };
    in
    {
      lib.mkShijhon = mkShijhon;
      packages = forLinux (system: rec {
        shijhon = mkShijhon { pkgs = nixpkgs.legacyPackages.${system}; };
        default = shijhon;
      });
      nixosModules.default = { lib, pkgs, ... }: {
        imports = [ ./nix/module.nix ];
        services.shijhon.package = lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      };
      nixosModules.shijhon = self.nixosModules.default;
      checks = forLinux (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          package = self.packages.${system}.shijhon;
        in
        {
          package = pkgs.runCommand "shijhon-package-check" { } ''
            ${package}/bin/shijhon version
            ${package}/bin/shijhon --help > /dev/null
            ${package.pythonEnvironment}/bin/python ${./tests/nix/check_package.py} ${./uv.lock}
            touch "$out"
          '';
        }
      );
      formatter = forLinux (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
