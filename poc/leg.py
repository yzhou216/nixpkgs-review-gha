"""Run or resume the proof-of-concept VM for one leg of the workflow.

The first leg boots a fresh image.  Later legs restore the VM from the memory
state and the disk that the previous leg saved, so the build inside the guest
keeps running instead of starting over.  When the leg's time is up, the VM is
paused, its memory is saved to a file, and the workflow uploads that file
together with the disk for the next leg.
"""

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

LEG = int(os.environ["LEG"])
LAST_LEG = os.environ.get("LAST_LEG") == "true"
DEADLINE = int(os.environ["JOB_STARTED_AT"]) + int(os.environ["LEG_MINUTES"]) * 60
ROOT = Path(os.environ.get("POC_DIR", "/nix/poc"))
VM, IN, OUT = ROOT / "vm", ROOT / "in", ROOT / "out"
MEMORY = os.environ.get("POC_MEMORY", "8G")
CPUS = os.environ.get("POC_CPUS", "4")
# Overrides for the image, such as a shorter build for local tests
IMAGE_ARGS = json.loads(os.environ.get("POC_IMAGE_ARGS", "{}"))


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def nix_bin(attr, exe):
    """Return the path of `exe` from the flake's nixpkgs package `attr`."""
    paths = subprocess.check_output(
        ["nix", "build", "--no-link", "--print-out-paths", f".#{attr}"], text=True
    ).split()
    return next(f"{p}/bin/{exe}" for p in paths if Path(f"{p}/bin/{exe}").exists())


def set_output(name, value):
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"{name}={value}\n")


class Monitor:
    """Minimal client for QMP and the QEMU guest agent on a UNIX socket."""

    def __init__(self, path, qmp=True, timeout=None):
        for _ in range(150):
            try:
                self.sock = socket.socket(socket.AF_UNIX)
                self.sock.connect(str(path))
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError(f"cannot connect to {path}")
        self.sock.settimeout(timeout)
        self.file = self.sock.makefile("rw")
        if qmp:
            json.loads(self.file.readline())
            self.cmd("qmp_capabilities")

    def cmd(self, name, **args):
        self.file.write(json.dumps({"execute": name, "arguments": args}) + "\n")
        self.file.flush()
        while True:
            message = json.loads(self.file.readline())
            if "return" in message:
                return message["return"]
            if "error" in message:
                raise RuntimeError(f"{name}: {message['error']}")


def size(path):
    return f"{path.stat().st_size / 2**30:.2f} GiB"


def start_vm(zstd, resume):
    qemu = nix_bin("qemu_kvm", "qemu-system-x86_64")
    args = [
        qemu,
        "-machine", "q35,accel=kvm",
        # A baseline model, so that the state restores on runners with other CPUs
        "-cpu", "qemu64",
        "-smp", CPUS,
        "-m", MEMORY,
        "-drive", f"file={VM}/disk.qcow2,if=virtio,format=qcow2",
        "-nic", "user,model=virtio-net-pci",
        "-serial", f"file:{VM}/serial-{LEG}.log",
        "-qmp", f"unix:{VM}/qmp.sock,server=on,wait=off",
        "-chardev", f"socket,id=qga,path={VM}/qga.sock,server=on,wait=off",
        "-device", "virtio-serial-pci",
        "-device", "virtserialport,chardev=qga,name=org.qemu.guest_agent.0",
        "-display", "none",
        "-daemonize",
        "-pidfile", f"{VM}/qemu.pid",
    ]
    if resume:
        args += ["-incoming", f"exec:{zstd} -dc {IN}/state.zst"]
    subprocess.run(args, check=True)
    qmp = Monitor(VM / "qmp.sock")
    if resume:
        start = time.time()
        while qmp.cmd("query-status")["status"] == "inmigrate":
            time.sleep(1)
        log(f"restored the VM state in {time.time() - start:.0f}s")
        qmp.cmd("cont")
    return qmp


def drop_guest_caches():
    """Shrink the memory state by dropping the guest's page cache first."""
    qga = Monitor(VM / "qga.sock", qmp=False, timeout=30)
    qga.cmd("guest-sync", id=LEG)
    pid = qga.cmd(
        "guest-exec", path="/bin/sh", arg=["-c", "sync; echo 3 > /proc/sys/vm/drop_caches"]
    )["pid"]
    while not qga.cmd("guest-exec-status", pid=pid)["exited"]:
        time.sleep(0.5)


def stop_vm(qmp):
    pid = int((VM / "qemu.pid").read_text())
    try:
        qmp.cmd("quit")
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    while Path(f"/proc/{pid}").exists():
        time.sleep(0.5)


def hand_off(qmp, zstd):
    OUT.mkdir(parents=True, exist_ok=True)
    start = time.time()
    try:
        drop_guest_caches()
    except (OSError, RuntimeError) as error:
        log(f"could not drop the guest page cache: {error}")
    qmp.cmd("stop")
    qmp.cmd("migrate-set-parameters", **{"max-bandwidth": 8 << 30})
    qmp.cmd("migrate", uri=f"exec:{zstd} -T0 -3 -q -o {OUT}/state.zst")
    while (status := qmp.cmd("query-migrate"))["status"] not in ("completed", "failed", "cancelled"):
        time.sleep(1)
    if status["status"] != "completed":
        raise RuntimeError(f"saving the VM state {status['status']}: {status.get('error-desc')}")
    stop_vm(qmp)
    shutil.move(VM / "disk.qcow2", OUT / "disk.qcow2")
    log(
        f"saved the VM in {time.time() - start:.0f}s: "
        f"memory {size(OUT / 'state.zst')}, disk {size(OUT / 'disk.qcow2')}"
    )
    set_output("relay", "true")


def main():
    cpu = next(l for l in open("/proc/cpuinfo") if l.startswith("model name")).split(":")[1].strip()
    log(f"leg {LEG} on {cpu}")
    zstd = nix_bin("zstd", "zstd")
    VM.mkdir(parents=True, exist_ok=True)
    resume = (IN / "state.zst").exists()
    if resume:
        shutil.move(IN / "disk.qcow2", VM / "disk.qcow2")
    else:
        nixpkgs = subprocess.check_output(
            ["nix", "eval", "--raw", ".#legacyPackages.x86_64-linux.path"], text=True
        )
        args = [arg for k, v in IMAGE_ARGS.items() for arg in ("--arg", k, str(v))]
        image = subprocess.check_output(
            ["nix", "build", "--no-link", "--print-out-paths", "-f", "poc/image.nix", "--arg", "nixpkgs", nixpkgs, *args],
            text=True,
        ).strip()
        shutil.copy(f"{image}/nixos.qcow2", VM / "disk.qcow2")
        (VM / "disk.qcow2").chmod(0o644)
        log(f"built the image, {size(VM / 'disk.qcow2')}")

    qmp = start_vm(zstd, resume)
    log(f"{'resumed' if resume else 'booted'} the VM, hand-off at {time.strftime('%H:%M:%S', time.localtime(DEADLINE))}")

    serial = VM / f"serial-{LEG}.log"
    offset = 0
    while True:
        with open(serial, errors="replace") as f:
            f.seek(offset)
            lines = f.readlines()
            offset = f.tell()
        for line in lines:
            if "poc step" in line or "POC-" in line:
                log(f"guest: {line.strip()}")
            if "POC-DONE" in line:
                stop_vm(qmp)
                set_output("relay", "false")
                return
            if "POC-FAILED" in line:
                stop_vm(qmp)
                raise SystemExit("the build inside the guest failed")
        if not LAST_LEG and time.time() >= DEADLINE:
            hand_off(qmp, zstd)
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
