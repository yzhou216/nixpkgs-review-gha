# NixOS guest that runs one long Nix build and reports its progress on the
# serial console.  The build stands in for a compile that takes longer than a
# GitHub Actions job: every step writes build files, hashes them, and logs the
# step number together with a token that the build picks once at its start and
# the guest's boot ID, so the log shows whether the same build kept running
# across hand-offs instead of starting over.
{
  steps,
  chunkMb,
  pause,
}:

{
  config,
  pkgs,
  modulesPath,
  ...
}:

let
  builder = pkgs.writeText "poc-builder.sh" ''
    set -eu
    mkdir work
    boot=$(cat /proc/sys/kernel/random/boot_id)
    token=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')
    for i in $(seq 1 "$steps"); do
      head -c "''${chunkMb}M" /dev/urandom > "work/chunk-$i"
      sum=$(sha256sum "work/chunk-$i" | cut -c1-12)
      echo "poc step $i/$steps token $token boot ''${boot:0:8} $(date -u +%T) $sum"
      sleep "$pause"
    done
    mkdir "$out"
    echo "$steps steps, $(du -sh work | cut -f1) of build files, token $token, boot ''${boot:0:8}" > "$out/summary"
  '';

  # Evaluated inside the guest, where the inputs are already in the store
  buildNix = pkgs.writeText "poc-build.nix" ''
    derivation {
      name = "poc-long-build";
      system = "x86_64-linux";
      builder = "''${builtins.storePath "${pkgs.bash}"}/bin/bash";
      args = [ (builtins.storePath "${builder}") ];
      PATH = "''${builtins.storePath "${pkgs.coreutils}"}/bin";
      steps = "${toString steps}";
      chunkMb = "${toString chunkMb}";
      pause = "${toString pause}";
    }
  '';
in

{
  imports = [ "${modulesPath}/profiles/qemu-guest.nix" ];

  boot.loader.grub.device = "/dev/vda";
  boot.loader.timeout = 0;
  boot.kernelParams = [ "console=ttyS0,115200" ];
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  services.qemuGuest.enable = true;
  # Keep the serial console free of login prompts
  systemd.services."serial-getty@ttyS0".enable = false;
  documentation.enable = false;
  system.stateVersion = "26.11";

  systemd.services.poc-build = {
    wantedBy = [ "multi-user.target" ];
    requires = [ "nix-daemon.socket" ];
    after = [ "nix-daemon.socket" ];
    path = [ config.nix.package ];
    serviceConfig = {
      Type = "oneshot";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
    script = ''
      echo "POC-START boot $(cat /proc/sys/kernel/random/boot_id)"
      if out=$(nix-build ${buildNix} --no-out-link); then
        echo "POC-DONE $out: $(cat "$out/summary")"
      else
        echo "POC-FAILED"
      fi
    '';
  };
}
