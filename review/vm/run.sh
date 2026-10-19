#!/usr/bin/env bash
# Runs the review inside the VM, with the files and variables that the host sent.
# The output goes through a pipe, so that nix does not draw progress bars.
set -u
export HOME=/root
export PATH=/root/.nix-profile/bin:/run/current-system/sw/bin

port=/dev/virtio-ports/org.nrgha.output
until [ -c "$port" ]; do sleep 1; done
exec > >(exec cat >"$port") 2>&1

cd /root/nrgha
echo "starting the review in the VM"

if [ -s extra-nix.conf ]; then
  cp extra-nix.conf /etc/nix/nrgha.conf
  systemctl restart nix-daemon
fi

for attempt in 1 2 3 4 5 6; do
  git clone --quiet --depth 1 https://github.com/NixOS/nixpkgs nixpkgs && break
  rm -rf nixpkgs
  sleep 10
done

if [ -d nixpkgs ]; then
  nu -c 'open env.json | load-env; ^nu review/review.nu'
  status=$?
else
  echo "could not clone nixpkgs"
  status=1
fi

# The host polls this file, so that a review that ends during a hand-off is not missed
echo "$status" >exit-code.tmp && mv exit-code.tmp exit-code
echo "review finished with exit code $status"
