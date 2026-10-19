# NixOS guest that runs the review in VM mode.  The host sends the review files
# through the QEMU guest agent, starts `run.sh`, and reads the report back the
# same way.  The review's output goes to the `org.nrgha.output` virtio port.

{ modulesPath, pkgs, ... }:

{
  imports = [ "${modulesPath}/profiles/qemu-guest.nix" ];

  boot.loader.grub.device = "/dev/vda";
  boot.loader.timeout = 0;
  boot.kernelParams = [ "console=ttyS0,115200" ];
  boot.growPartition = true;
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
    autoResize = true;
  };

  # QEMU's user networking always uses these addresses, also after a hand-off
  networking = {
    usePredictableInterfaceNames = false;
    useDHCP = false;
    interfaces.eth0.ipv4.addresses = [
      {
        address = "10.0.2.15";
        prefixLength = 24;
      }
    ];
    defaultGateway = "10.0.2.2";
    nameservers = [ "10.0.2.3" ];
  };

  # Large reviews can run several heavy builds at once.  Compressed swap gives
  # them more room before the OOM killer has to step in.
  zramSwap = {
    enable = true;
    memoryPercent = 50;
  };

  services.qemuGuest.enable = true;
  # The host talks to the guest through the agent, so it must survive low memory
  systemd.services.qemu-guest-agent.serviceConfig.OOMScoreAdjust = -900;
  # Keep the serial console free of login prompts
  systemd.services."serial-getty@ttyS0".enable = false;
  documentation.enable = false;

  environment.systemPackages = with pkgs; [
    git
    gnutar
    nushell
  ];

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    accept-flake-config = true;
    trusted-users = [ "root" ];
    max-jobs = "auto";
    # Up to four builds at once, but with two cores each, so that heavy builds
    # running together fit into the VM's memory
    cores = 2;
  };
  # Filled in by run.sh from the EXTRA_NIX_CONFIG variable
  nix.extraOptions = "!include /etc/nix/nrgha.conf";

  system.stateVersion = "26.11";
}
