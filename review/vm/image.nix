# qcow2 disk image of the VM mode guest
{ nixpkgs }:

let
  pkgs = import nixpkgs { system = "x86_64-linux"; };
  nixos = import "${nixpkgs}/nixos/lib/eval-config.nix" {
    system = "x86_64-linux";
    modules = [ ./guest.nix ];
  };
in

import "${nixpkgs}/nixos/lib/make-disk-image.nix" {
  inherit pkgs;
  inherit (pkgs) lib;
  inherit (nixos) config;
  format = "qcow2";
  additionalSpace = "1G";
}
