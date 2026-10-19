# qcow2 disk image of the proof-of-concept guest
{
  nixpkgs,
  steps ? 150,
  chunkMb ? 20,
  pause ? 8,
}:

let
  pkgs = import nixpkgs { system = "x86_64-linux"; };
  nixos = import "${nixpkgs}/nixos/lib/eval-config.nix" {
    system = "x86_64-linux";
    modules = [ (import ./vm.nix { inherit steps chunkMb pause; }) ];
  };
in

import "${nixpkgs}/nixos/lib/make-disk-image.nix" {
  inherit pkgs;
  inherit (pkgs) lib;
  inherit (nixos) config;
  format = "qcow2";
  diskSize = 20480;
}
