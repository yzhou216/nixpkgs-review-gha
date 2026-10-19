"""Self-chaining nixpkgs-review-gha batch dispatcher.

Each run of the batch-review workflow calls this script.  It dispatches
review.yml for the next candidate pull requests, newest first, and keeps the
number of in-flight review runs under a cap.  Every pull request is checked
again just before its review is dispatched, and the outcome of that check is
recorded in the state file so that later runs skip it.

A run lasts up to TIME_BUDGET seconds, commits the state file every
COMMIT_INTERVAL seconds, and then dispatches the next batch-review run, so
the batch does not depend on GitHub's unreliable cron schedule.  Disabling
the batch-review workflow stops the current run within a few minutes and
ends the chain.  Once every candidate has been handled and no batch review
is still running, the script disables the workflow itself.

Usage: dispatch.py CANDIDATES_JSON STATE_JSON

Environment: GH_TOKEN, REPO, OWNER, and optionally LIMIT, DRY_RUN and
TIME_BUDGET (seconds).  Runs with a LIMIT or DRY_RUN do not chain.
"""

import datetime
import json
import os
import re
import subprocess
import sys
import time

REPO = os.environ["REPO"]
OWNER = os.environ["OWNER"]
LIMIT = int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None
DRY_RUN = os.environ.get("DRY_RUN") == "true"
TIME_BUDGET = int(os.environ.get("TIME_BUDGET") or 19200)

MAX_INFLIGHT = 8
MAX_TOTAL_INFLIGHT = 12
MIN_INTERVAL = 75
MIN_RATE_REMAINING = 1000
MAX_TRIES = 5
RETRY_POSITION = 15
COMMIT_INTERVAL = 1800
ENABLED_CHECK_INTERVAL = 120
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
SMALL = {"1", "1-10", "11-100"}
INPUTS = {
    "x86_64-linux": "true",
    "aarch64-linux": "true",
    "x86_64-darwin": "no",
    "aarch64-darwin": "yes_sandbox_true",
    "riscv64-linux": "false",
    "allow-unfree": "true",
    "allow-insecure": "true",
    "builders": "gha",
    "push-to-cache": "true",
    "upterm": "false",
    "post-result": "true",
    "on-success": "approve",
}

BUMP = re.compile(r"^(?P<attr>[A-Za-z0-9_.+-]+): (?P<old>[^\s]+) (?:->|→) (?P<new>[^\s]+)$")

# Generated lock and source data that is regenerated mechanically on a bump
GENERATED = re.compile(
    r"(^|/)(Cargo\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lock|"
    r"deps\.json|gemset\.nix|Gemfile\.lock|composer\.lock|pubspec\.lock\.json|"
    r"mix\.lock|poetry\.lock|uv\.lock|sources\.json|versions?\.json|hashes\.json|"
    r"git-hashes\.json|missing-hashes\.json|lock\.json|info\.json|pin\.json|src\.json|"
    r"source\.json)$"
)
HASHVAL = r"(?:sha(?:256|512|1)-[A-Za-z0-9+/=]+|[0-9a-z]{52}|[0-9a-f]{40}|[0-9a-f]{64}|[0-9a-f]{128})"
ALLOWED = [
    # Version, rev, tag and hash style string assignments
    re.compile(
        r'^\s*(?:version|buildNumber|rev|tag|hash|sha256|sha512|sha1|outputHash|srcHash|'
        r'[A-Za-z0-9_]*(?:Hash|hash|Sha256|sha256))\s*=\s*"[^"$]*"\s*;\s*$'
    ),
    # Per-platform hash attrsets, e.g. x86_64-linux = "sha256-..."
    re.compile(r'^\s*"?[A-Za-z0-9_.-]+"?\s*=\s*"' + HASHVAL + r'"\s*;\s*$'),
    # Bare hash strings in lists or function arguments
    re.compile(r'^\s*"' + HASHVAL + r'"\s*[;,]?\s*$'),
]
RUN_TITLE = re.compile(r"^review #(\d+)\b")


def log(*args):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(ts, *args, flush=True)


def gh(args):
    for attempt in range(6):
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        err = p.stderr
        if "Not Found" in err or "HTTP 404" in err:
            raise LookupError(err.strip()[:200])
        log("gh error, retrying:", " ".join(args[:3]), err.strip()[:200])
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"gh {' '.join(args[:3])} failed: {err.strip()[:300]}")


def api(path, paginate=False):
    if paginate:
        return [x for page in json.loads(gh(["api", "--paginate", "--slurp", path])) for x in page]
    return json.loads(gh(["api", path]))


def fork_runs(pages=1):
    runs = []
    for page in range(1, pages + 1):
        d = api(f"repos/{REPO}/actions/workflows/review.yml/runs?per_page=100&page={page}")
        runs += d["workflow_runs"]
        if len(d["workflow_runs"]) < 100:
            break
    return runs


def pr_of(run):
    m = RUN_TITLE.match(run.get("display_title") or "")
    return int(m[1]) if m else None


def version_variants(v):
    out = {v}
    if v.startswith("v"):
        out.add(v[1:])
    out |= {x.replace(".", "_") for x in list(out)}
    out |= {x.replace(".", "-") for x in list(out)}
    return out


def classify(title, files):
    """Return the reasons why a bump is not trivial, or an empty list.

    A trivial bump changes only version, rev, tag and hash values, or lines
    that differ from the old ones by the version alone, plus generated lock
    and source data files.  At least one hash must change, so that a bump
    which forgot the source hash does not pass on the old cached source.
    """
    m = BUMP.match(title.strip())
    old, new = m["old"], m["new"]
    reasons = []
    hash_changed = False
    for f in files:
        name = f["filename"]
        if f["status"] != "modified":
            reasons.append(f"{f['status']} file {name}")
            continue
        if GENERATED.search(name):
            if re.search(r"^[+-].*" + HASHVAL, f.get("patch") or "", re.M):
                hash_changed = True
            continue
        if not name.endswith(".nix"):
            reasons.append(f"non-nix file {name}")
            continue
        patch = f.get("patch")
        if patch is None:
            reasons.append(f"no patch for {name}")
            continue
        removed = [l[1:] for l in patch.splitlines() if l.startswith("-")]
        added = [l[1:] for l in patch.splitlines() if l.startswith("+")]
        olds = version_variants(old) if len(old) >= 3 else set()
        news = version_variants(new)
        # Lines explained by substituting the old version with the new one
        subst = {r.replace(o, n) for r in removed for o in olds for n in news if o in r}
        bad = [
            line.strip()
            for line in removed + added
            if not any(p.match(line) for p in ALLOWED)
            and not (line in subst or any(o in line for o in olds) and line in removed)
        ]
        # Removed lines with the old version need a matching added line
        bad += [
            r.strip()
            for r in removed
            if not any(p.match(r) for p in ALLOWED)
            and not any(r.replace(o, n) in added for o in olds for n in news if o in r)
        ]
        if any(re.search(HASHVAL, l) for l in added):
            hash_changed = True

        # Assigned attribute names must be unchanged, e.g. no sha256 -> hash rename
        def keys(lines):
            return sorted(l.split("=", 1)[0].strip() for l in lines if "=" in l)

        if keys(removed) != keys(added):
            bad.append(f"attribute names changed {keys(removed)} -> {keys(added)}")
        if bad:
            reasons.append(f"{name}: " + " | ".join(dict.fromkeys(bad))[:300])
        if len(removed) != len(added):
            reasons.append(f"{name}: {len(removed)} removed vs {len(added)} added lines")
    if not reasons and not hash_changed:
        reasons.append("no hash changed")
    return reasons


def bucket(labels, prefix):
    vals = {l.split(": ", 1)[1] for l in labels if l.startswith(prefix + ": ")}
    if not vals:
        return None
    big = [v for v in vals if "-" in v or "+" in v]
    return big[0] if big else sorted(vals)[0]


def check_pr(cand):
    """Check a candidate just before its review is dispatched.

    Return a (status, reason, title) tuple.  The status is "ok" when the
    review can be dispatched, "skipped" when the pull request no longer
    qualifies, and "retry" when the check could not be decided yet, for
    example while GitHub is still computing mergeability.
    """
    n = cand["number"]
    pr = api(f"repos/NixOS/nixpkgs/pulls/{n}")

    def skip(reason, status="skipped"):
        return status, reason, pr["title"]

    if pr["state"] != "open":
        return skip("not open")
    if pr["draft"]:
        return skip("draft")
    if pr["base"]["ref"] != "master":
        return skip("base not master")
    if pr["user"]["login"] == OWNER:
        return skip("own PR")
    if not BUMP.match(pr["title"].strip()):
        return skip("title no longer a plain bump")
    if pr["head"]["sha"] != cand["headRefOid"]:
        files = api(f"repos/NixOS/nixpkgs/pulls/{n}/files?per_page=100", paginate=True)
        reasons = classify(pr["title"], files)
        if reasons:
            return skip(f"head changed, now non-trivial: {reasons[0][:150]}")
    labels = [l["name"] for l in pr["labels"]]
    lin, dar = bucket(labels, "10.rebuild-linux"), bucket(labels, "10.rebuild-darwin")
    if not (lin in SMALL and dar in SMALL | {"0"} or lin == "0" and dar in SMALL):
        return skip(f"rebuilds linux {lin} darwin {dar}")
    for _ in range(8):
        if pr["mergeable"] is not None:
            break
        time.sleep(4)
        pr = api(f"repos/NixOS/nixpkgs/pulls/{n}")
    if pr["mergeable"] is None:
        return skip("mergeability unknown", "retry")
    if not pr["mergeable"]:
        return skip("merge conflict")
    if "nixpkgs-review-gha" in (pr["body"] or ""):
        return skip("gha in PR body")
    comments = api(f"repos/NixOS/nixpkgs/issues/{n}/comments?per_page=100", paginate=True)
    if any("nixpkgs-review-gha" in (c["body"] or "") for c in comments):
        return skip("has nixpkgs-review-gha comment")
    reviews = api(f"repos/NixOS/nixpkgs/pulls/{n}/reviews?per_page=100", paginate=True)
    if any((r["user"] or {}).get("login") == OWNER for r in reviews):
        return skip("already reviewed by owner")
    return "ok", None, pr["title"]


def dispatch(n):
    args = ["workflow", "run", "review.yml", "-R", REPO, "--ref", "main", "-f", f"pr={n}"]
    args += [a for k, v in INPUTS.items() for a in ("-f", f"{k}={v}")]
    started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=10)
    m = re.search(r"/actions/runs/(\d+)", gh(args))
    if m:
        return int(m[1])
    for _ in range(12):
        time.sleep(5)
        for run in fork_runs():
            created = datetime.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if pr_of(run) == n and created >= started:
                return run["id"]
    return None


def rate():
    core = api("rate_limit")["resources"]["core"]
    return core["remaining"], core["limit"]


def workflow_enabled():
    return api(f"repos/{REPO}/actions/workflows/batch-review.yml")["state"] == "active"


def commit_state(state_path):
    """Commit the state file and push it to the batch-review branch."""
    repo_dir = os.path.dirname(os.path.abspath(state_path))

    def git(*args):
        return subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True)

    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("add", os.path.basename(state_path))
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-q", "-m", "batch-review: update state")
    # Rebase onto commits pushed meanwhile, such as dispatcher updates
    url = f"https://x-access-token:{os.environ['GH_TOKEN']}@github.com/{REPO}.git"
    git("pull", "-q", "--rebase", url, "batch-review")
    p = git("push", "-q", url, "HEAD:batch-review")
    log("committed state" if p.returncode == 0 else f"state push failed: {p.stderr.strip()[:200]}")


def main():
    start = time.time()
    candidates = json.load(open(sys.argv[1]))
    state_path = sys.argv[2]
    state = json.load(open(state_path))
    records = state["records"]
    numbers = {c["number"] for c in candidates}

    def save():
        if DRY_RUN:
            return
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, state_path)

    def pending(c):
        r = records.get(str(c["number"]))
        return r is None or r["status"] == "retry" and r.get("tries", 0) < MAX_TRIES

    queue = [c for c in candidates if pending(c)]
    seen = {pr_of(r) for r in fork_runs(pages=3)} - {None}
    seen |= {int(n) for n, r in records.items() if r["status"] == "dispatched"}
    remaining, limit = rate()
    log(f"queue {len(queue)} of {len(candidates)}, rate limit {remaining}/{limit}, dry run {DRY_RUN}")

    dispatched = 0
    last = 0.0
    fetched = 0.0
    checked = committed = start
    chain = LIMIT is None and not DRY_RUN
    while True:
        now = time.time()
        if now - start > TIME_BUDGET or LIMIT is not None and dispatched >= LIMIT:
            break
        if now - checked > ENABLED_CHECK_INTERVAL:
            checked = now
            if not workflow_enabled():
                log("batch-review is disabled, stopping without starting another run")
                chain = False
                break
        if not DRY_RUN and now - committed > COMMIT_INTERVAL:
            committed = now
            commit_state(state_path)
            remaining, limit = rate()
            log(f"dispatched {dispatched} so far, {len(queue)} left in queue, rate limit {remaining}/{limit}")
        # Reuse the run list briefly so that consecutive skips stay cheap
        if now - fetched > 20:
            runs = fork_runs()
            fetched = now
        seen |= {pr_of(r) for r in runs} - {None}
        active = [r for r in runs if r["status"] in ACTIVE]
        mine = sum(pr_of(r) in numbers for r in active)
        if not queue:
            if mine == 0:
                if not DRY_RUN:
                    log("all candidates handled and no batch review in flight, disabling batch-review")
                    gh(["workflow", "disable", "batch-review.yml", "-R", REPO])
                chain = False
                break
            time.sleep(60)
            fetched = 0.0
            continue
        remaining, _ = rate()
        if mine >= MAX_INFLIGHT or len(active) >= MAX_TOTAL_INFLIGHT or remaining < MIN_RATE_REMAINING:
            time.sleep(30)
            fetched = 0.0
            continue
        wait = MIN_INTERVAL - (time.time() - last)
        if wait > 0 and not DRY_RUN:
            time.sleep(wait)
            fetched = 0.0
            continue
        cand = queue.pop(0)
        n = cand["number"]
        if n in seen:
            status, reason, title = "skipped", "fork already has a run", cand["title"]
        else:
            try:
                status, reason, title = check_pr(cand)
            except LookupError as e:
                status, reason, title = "skipped", f"lookup failed: {e}", cand["title"]
        if status != "ok":
            tries = records.get(str(n), {}).get("tries", 0) + (status == "retry")
            records[str(n)] = {"status": status, "reason": reason, "title": title, "tries": tries}
            log(f"#{n} {status}: {reason} ({title})")
            save()
            # Give GitHub time to finish computing mergeability before trying again
            if status == "retry" and tries < MAX_TRIES:
                queue.insert(min(len(queue), RETRY_POSITION), cand)
            continue
        if DRY_RUN:
            log(f"#{n} would dispatch: {title}")
            dispatched += 1
            continue
        run_id = dispatch(n)
        last = time.time()
        fetched = 0.0
        dispatched += 1
        seen.add(n)
        records[str(n)] = {
            "status": "dispatched",
            "run_id": run_id,
            "title": title,
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }
        log(f"#{n} dispatched run {run_id}: {title}")
        save()

    remaining, limit = rate()
    log(f"dispatched {dispatched}, {len(queue)} left in queue, rate limit {remaining}/{limit}")
    if chain:
        log("starting the next batch-review run")
        gh(["workflow", "run", "batch-review.yml", "-R", REPO, "--ref", "main"])


if __name__ == "__main__":
    main()
