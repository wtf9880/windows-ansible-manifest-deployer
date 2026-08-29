{
  description = "development environment";
  # https://nix.dev/manual/nix/2.34/command-ref/new-cli/nix3-flake.html#flake-reference-attributes
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    systems.url = "github:nix-systems/default";
  };
  outputs = { self, nixpkgs, systems, ... }:
    let
      # generate a set of attributes for each system
      eachSystem = nixpkgs.lib.genAttrs (import systems);
    in {
      devShells = eachSystem (system: 
        let 
          # Move pkgs here so 'system' is in scope
          pkgs = import nixpkgs { inherit system; }; 
        in {
          default = pkgs.mkShell {
            buildInputs = with pkgs; [
              (ansible.override { windowsSupport = true; })
              gnumake
              python3
              python3Packages.ruamel-yaml
              zig
              mono
              unzip
              yamllint
            ];
          };
        }
      );
    };
}
