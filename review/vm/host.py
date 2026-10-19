"""Run the review inside a VM that can move to the next leg of the workflow.

In VM mode the x86_64-linux review runs in a QEMU guest instead of directly on
the runner.  When the leg's time is up, the guest is paused, and its memory and
disk are saved for the workflow to upload.  The next leg restores the guest
from them, so builds that were running at the hand-off continue instead of
starting over, even when a single build takes longer than one job.
"""

import base64
import io
import json
import os
import random
import shutil
import socket
import subprocess
import tarfile
import time
from pathlib import Path

SYSTEM = "x86_64-linux"
LEG = int(os.environ["RELAY_LEG"])
LAST_LEG = os.environ["RELAY_LAST_LEG"] == "true"
INPUTS = json.loads(os.environ["INPUTS"])
RELAY_AFTER = int(INPUTS.get("relay-after") or 0)
# The last leg has no successor, so it keeps the job's full time limit
DEADLINE = int(os.environ["JOB_STARTED_AT"]) + RELAY_AFTER * 60 if RELAY_AFTER > 0 and not LAST_LEG else None
ROOT = Path(os.environ["VM_DIR"])
WORK, IN, OUT = ROOT / "work", ROOT / "in", ROOT / "out"
GUEST_DIR = "/root/nrgha"
BIN = "/run/current-system/sw/bin"
REPORT = f"report_{SYSTEM}.json"
# The x86-64-v3 feature level on a generic model, so that the state restores on
# runners with other CPUs, Intel or AMD, while builds and tests still get AVX2
CPU = ",".join(
    ["qemu64"]
    + [
        f"+{feature}"
        for feature in "cx16 popcnt ssse3 sse4.1 sse4.2 avx avx2 bmi1 bmi2 fma f16c movbe xsave abm aes pclmulqdq".split()
    ]
)
# The same files as in the sparse checkout of the review job
REVIEW_FILES = ["review", "flake.nix", "flake.lock", "patches", "drv-build-support.nix"]


def log(message):
    print(f"[vm] {message}", flush=True)


def set_output(name, value):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"{name}={value}\n")


def nix_bin(attr, exe):
    """Return the path of `exe` from the flake's nixpkgs package `attr`."""
    out = subprocess.check_output(["nix", "build", "--no-link", "--print-out-paths", f".#{attr}"], text=True)
    return next(f"{p}/bin/{exe}" for p in out.split() if Path(f"{p}/bin/{exe}").exists())


class Monitor:
    """Minimal client for QMP or the QEMU guest agent on a UNIX socket."""

    def __init__(self, path, qmp=True, timeout=60):
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

    def send(self, name, **args):
        self.file.write(json.dumps({"execute": name, "arguments": args}) + "\n")
        self.file.flush()

    def receive(self, name):
        """Return the next reply, skipping events."""
        while True:
            message = json.loads(self.file.readline())
            if "return" in message:
                return message["return"]
            if "error" in message:
                raise RuntimeError(f"{name}: {message['error']['desc']}")

    def cmd(self, name, **args):
        self.send(name, **args)
        return self.receive(name)

    def close(self):
        self.sock.close()


class Agent:
    """Guest agent commands that the host needs."""

    def __init__(self, patience=600):
        deadline = time.time() + patience
        while True:
            monitor = Monitor(WORK / "qga.sock", qmp=False, timeout=5)
            # Replies to earlier attempts can still be queued, so wait for this one
            sync = random.randrange(1 << 31)
            try:
                monitor.send("guest-sync", id=sync)
                while monitor.receive("guest-sync") != sync:
                    pass
                break
            except (OSError, ValueError):
                monitor.close()
                if time.time() > deadline:
                    raise RuntimeError("the guest agent did not answer")
                time.sleep(2)
        monitor.sock.settimeout(120)
        self.monitor = monitor

    def close(self):
        self.monitor.close()

    def exec(self, *argv):
        """Run a command in the guest and return its output."""
        pid = self.monitor.cmd("guest-exec", path=argv[0], arg=list(argv[1:]), **{"capture-output": True})["pid"]
        while not (status := self.monitor.cmd("guest-exec-status", pid=pid))["exited"]:
            time.sleep(0.5)
        if status.get("exitcode"):
            error = base64.b64decode(status.get("err-data", "")).decode(errors="replace")
            raise RuntimeError(f"{' '.join(argv)} failed: {error.strip()}")
        return base64.b64decode(status.get("out-data", "")).decode(errors="replace")

    def write(self, path, data):
        handle = self.monitor.cmd("guest-file-open", path=path, mode="w")
        for start in range(0, len(data), 1 << 20):
            chunk = base64.b64encode(data[start : start + (1 << 20)]).decode()
            self.monitor.cmd("guest-file-write", handle=handle, **{"buf-b64": chunk})
        self.monitor.cmd("guest-file-close", handle=handle)

    def read(self, path):
        """Return the contents of `path`, or None if it does not exist."""
        try:
            handle = self.monitor.cmd("guest-file-open", path=path, mode="r")
        except RuntimeError:
            return None
        data = b""
        while True:
            result = self.monitor.cmd("guest-file-read", handle=handle, count=1 << 20)
            data += base64.b64decode(result["buf-b64"])
            if result["eof"]:
                break
        self.monitor.cmd("guest-file-close", handle=handle)
        return data


def size(path):
    return f"{path.stat().st_size / 2**30:.2f} GiB"


def vm_config():
    """Return the VM's size, which must stay the same across legs."""
    if (IN / "vm.json").exists():
        return json.loads((IN / "vm.json").read_text())
    total = next(int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemTotal")) // 1024
    return {
        "memory": os.environ.get("VM_MEMORY") or f"{max(4096, total - 3072)}M",
        "cpus": int(os.environ.get("VM_CPUS") or os.cpu_count()),
        "disk": "250G",
    }


def start_vm(config, zstd, resume):
    qemu = nix_bin("qemu_kvm", "qemu-system-x86_64")
    args = [
        qemu,
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        CPU,
        "-smp",
        str(config["cpus"]),
        "-m",
        config["memory"],
        "-drive",
        f"file={WORK}/disk.qcow2,if=virtio,format=qcow2,discard=unmap,detect-zeroes=unmap",
        "-nic",
        "user,model=virtio-net-pci",
        "-serial",
        f"file:{WORK}/console-{LEG}.log",
        "-qmp",
        f"unix:{WORK}/qmp.sock,server=on,wait=off",
        "-device",
        "virtio-serial-pci",
        "-chardev",
        f"socket,id=qga,path={WORK}/qga.sock,server=on,wait=off",
        "-device",
        "virtserialport,chardev=qga,name=org.qemu.guest_agent.0",
        "-chardev",
        f"file,id=output,path={WORK}/output-{LEG}.log",
        "-device",
        "virtserialport,chardev=output,name=org.nrgha.output",
        "-display",
        "none",
        "-daemonize",
        "-pidfile",
        f"{WORK}/qemu.pid",
    ]
    if resume:
        args += ["-incoming", f"exec:{zstd} -dc {IN}/memory.zst"]
    subprocess.run(args, check=True)
    qmp = Monitor(WORK / "qmp.sock")
    if resume:
        start = time.time()
        while qmp.cmd("query-status")["status"] == "inmigrate":
            time.sleep(1)
        log(f"restored the VM in {time.time() - start:.0f}s")
        qmp.cmd("cont")
    return qmp


def qemu_running():
    return Path(f"/proc/{int((WORK / 'qemu.pid').read_text())}").exists()


def stop_vm(qmp):
    pid = int((WORK / "qemu.pid").read_text())
    try:
        qmp.cmd("quit")
    except (OSError, ValueError):
        pass
    while Path(f"/proc/{pid}").exists():
        time.sleep(0.5)


def start_review(agent):
    """Send the review files and variables to the guest and start the review there."""
    env = {
        name: os.environ.get(name, "")
        for name in ["PR_JSON", "GITHUB_TOKEN", "IDENTIFY_STILL_FAILING_PACKAGES", "IDENTIFY_UNSUPPORTED_PACKAGES"]
    }
    # Remote builders and pushing to a cache need secrets that must not end up in the VM state
    env["INPUTS"] = json.dumps(INPUTS | {"builders": "gha", "push-to-cache": "false"})
    env |= {
        name: ""
        for name in [
            "ATTIC_SERVER",
            "ATTIC_CACHE",
            "ATTIC_TOKEN",
            "CACHIX_CACHE",
            "CACHIX_AUTH_TOKEN",
            "CACHIX_SIGNING_KEY",
        ]
    }
    # The VM itself hands off, so the review inside runs without a deadline
    env |= {
        "RELAY_LEG": "1",
        "RELAY_LAST_LEG": "true",
        "JOB_STARTED_AT": "0",
        "GITHUB_OUTPUT": f"{GUEST_DIR}/github-output",
    }

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name in REVIEW_FILES:
            if Path(name).exists():
                tar.add(name)
        for name, data in [("env.json", json.dumps(env)), ("extra-nix.conf", os.environ.get("EXTRA_NIX_CONFIG", ""))]:
            info = tarfile.TarInfo(name)
            info.size = len(data.encode())
            tar.addfile(info, io.BytesIO(data.encode()))
    agent.write("/root/nrgha.tar", archive.getvalue())
    agent.exec(f"{BIN}/mkdir", "-p", GUEST_DIR)
    agent.exec(f"{BIN}/tar", "-xf", "/root/nrgha.tar", "-C", GUEST_DIR)
    # Transient units only get a minimal PATH, which has no bash on NixOS.  When
    # memory runs out, the kernel should kill a build rather than the review, so
    # that the package fails and the review goes on.
    agent.exec(
        f"{BIN}/systemd-run",
        "--unit=nrgha-review",
        "--property=OOMScoreAdjust=-900",
        "--property=OOMPolicy=continue",
        f"{BIN}/bash",
        f"{GUEST_DIR}/review/vm/run.sh",
    )
    log("started the review in the VM")


def hand_off(qmp, agent, zstd):
    OUT.mkdir(parents=True, exist_ok=True)
    start = time.time()
    # Free blocks and the page cache do not need to be saved
    for argv in ([f"{BIN}/fstrim", "-a"], ["/bin/sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"]):
        try:
            (agent or Agent(patience=30)).exec(*argv)
        except (OSError, RuntimeError, ValueError) as error:
            log(f"could not run {argv[0]} in the guest: {error}")
    qmp.cmd("stop")
    qmp.cmd("migrate-set-parameters", **{"max-bandwidth": 8 << 30})
    qmp.cmd("migrate", uri=f"exec:{zstd} -T0 -3 -q -o {OUT}/memory.zst")
    while (status := qmp.cmd("query-migrate"))["status"] not in ("completed", "failed", "cancelled"):
        time.sleep(1)
    if status["status"] != "completed":
        raise RuntimeError(f"saving the VM memory {status['status']}: {status.get('error-desc')}")
    stop_vm(qmp)
    subprocess.run([zstd, "-T0", "-3", "-q", "--rm", f"{WORK}/disk.qcow2", "-o", f"{OUT}/disk.qcow2.zst"], check=True)
    shutil.copy(WORK / "vm.json", OUT / "vm.json")
    log(
        f"saved the VM in {time.time() - start:.0f}s: memory {size(OUT / 'memory.zst')}, "
        f"disk {size(OUT / 'disk.qcow2.zst')}"
    )
    set_output("relay", "true")


def finish(qmp, agent, code):
    report = agent.read(f"{GUEST_DIR}/{REPORT}")
    if report is not None:
        Path(REPORT).write_bytes(report)
    stop_vm(qmp)
    set_output("relay", "false")
    if code != 0:
        raise SystemExit(f"the review in the VM failed with exit code {code}")
    if report is None:
        raise SystemExit("the review in the VM did not write a report")


def main():
    cpu = next(l for l in open("/proc/cpuinfo") if l.startswith("model name")).split(":")[1].strip()
    log(f"leg {LEG} on {cpu}")
    zstd = nix_bin("zstd", "zstd")
    WORK.mkdir(parents=True, exist_ok=True)
    resume = (IN / "memory.zst").exists()
    config = vm_config()
    (WORK / "vm.json").write_text(json.dumps(config))

    if resume:
        subprocess.run([zstd, "-d", "-q", "--rm", f"{IN}/disk.qcow2.zst", "-o", f"{WORK}/disk.qcow2"], check=True)
    else:
        nixpkgs = subprocess.check_output(["nix", "eval", "--raw", ".#legacyPackages.x86_64-linux.path"], text=True)
        image = subprocess.check_output(
            [
                "nix",
                "build",
                "--no-link",
                "--print-out-paths",
                "-f",
                "review/vm/image.nix",
                "--arg",
                "nixpkgs",
                nixpkgs,
            ],
            text=True,
        ).strip()
        shutil.copy(f"{image}/nixos.qcow2", WORK / "disk.qcow2")
        (WORK / "disk.qcow2").chmod(0o644)
        qemu_img = nix_bin("qemu_kvm", "qemu-img")
        subprocess.run([qemu_img, "resize", "-q", f"{WORK}/disk.qcow2", config["disk"]], check=True)
        log(f"built the VM image, {config['cpus']} CPUs and {config['memory']} of memory")

    qmp = start_vm(config, zstd, resume)
    agent = Agent()
    if not resume:
        start_review(agent)
    if DEADLINE:
        log(f"handing off to leg {LEG + 1} at {time.strftime('%H:%M:%S', time.gmtime(DEADLINE))} UTC")

    output = WORK / f"output-{LEG}.log"
    offset = 0

    def print_output():
        nonlocal offset
        with open(output, "rb") as f:
            f.seek(offset)
            data = f.read()
            offset += len(data)
        if data:
            print(data.decode(errors="replace").replace("\r", ""), end="", flush=True)
        return bool(data)

    last_check = 0
    last_output = last_note = time.time()
    # When the guest agent stopped answering, if it did
    agent_lost = None
    while True:
        if print_output():
            last_output = time.time()
        elif time.time() - max(last_output, last_note) > 900:
            last_note = time.time()
            log(f"no output from the review for {(last_note - last_output) / 60:.0f} minutes")
        if not qemu_running():
            print((WORK / f"console-{LEG}.log").read_text(errors="replace")[-5000:])
            raise SystemExit("the VM stopped unexpectedly")
        if time.time() - last_check > 15:
            last_check = time.time()
            try:
                agent = agent or Agent(patience=30)
                code = agent.read(f"{GUEST_DIR}/exit-code")
                if code is not None:
                    time.sleep(2)
                    print_output()
                    finish(qmp, agent, int(code))
                    return
                state = agent.exec(f"{BIN}/systemctl", "show", "--property=ActiveState", "--value", "nrgha-review")
                if state.strip() in ("failed", "inactive"):
                    print(agent.exec(f"{BIN}/journalctl", "--unit=nrgha-review", "--no-pager", "--lines=50"))
                    stop_vm(qmp)
                    raise SystemExit("the review in the VM stopped without an exit code")
                agent_lost = None
            except (OSError, RuntimeError, ValueError) as error:
                # The agent can restart or stall while the guest is short of memory
                if agent_lost is None:
                    agent_lost = time.time()
                    log(f"the guest agent did not answer, retrying: {error}")
                elif time.time() - agent_lost > 3600:
                    raise SystemExit("the guest agent has not answered for an hour")
                if agent:
                    agent.close()
                    agent = None
        if DEADLINE and time.time() >= DEADLINE:
            hand_off(qmp, agent, zstd)
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
