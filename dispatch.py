"""Self-chaining nixpkgs-review-gha batch dispatcher.

Each run of the batch-review workflow calls this script.  It dispatches
review.yml for the next candidate pull requests, newest first, and keeps the
number of in-flight review runs under a cap.  Every pull request is checked
again just before its review is dispatched, and the outcome of that check is
recorded in the state file so that later runs skip it.

GitHub often has no macOS arm64 runner to spare during US working hours.
The aarch64-darwin job of a review then fails after about fifteen minutes
without starting, and the review posts nothing.  The script re-runs only the
failed jobs of such a review once runners are available again, which keeps
its Linux results, provided that the head of the pull request is unchanged.
If the head changed, it checks the pull request again and dispatches a new
review.  While these failures are recent, it keeps fewer Darwin reviews in
flight and holds back re-runs.  The script cancels a review that is still
running after MAX_RUNTIME seconds, since few of them finish before GitHub's
six-hour job limit, and they hold runners that other reviews need.

GitHub Pro runs 40 jobs at a time but only 5 of them on macOS.  The script
therefore counts the jobs that review runs occupy rather than the runs, and
caps Darwin reviews separately.  A pull request whose CI label reports no
Darwin rebuilds is reviewed without the aarch64-darwin job, so it needs no
Mac at all.  While the Darwin cap is full, the script keeps dispatching such
Linux-only reviews.

Pull requests by COMMENT_ONLY_AUTHORS get the review result as a comment
but no approval.  A committer asked on #550000 for no automated approvals of
r-ryantm's pull requests, and said that the results are still useful, since
r-ryantm only builds on x86_64-linux.  Records with the status "requeue" ask
for a pull request to be reviewed again, for example after an update, and go
to the front of the queue.  The state file can also hold re-reviews of pull
requests that are not candidates, such as the user's own, which run with
their own on-success setting and are tracked like any other review.

A run lasts up to TIME_BUDGET seconds, commits the state file every
COMMIT_INTERVAL seconds, and then dispatches the next batch-review run, so
the batch does not depend on GitHub's unreliable cron schedule.  It also
starts the next run after a crash, unless the crash came within ten minutes
of starting, which would otherwise repeat in a tight loop.  A scheduled run
exits at once while another batch-review run is active, so the schedule only
restarts a broken chain.  Disabling the batch-review workflow stops the
current run within a few minutes and ends the chain.  Once every candidate
has been handled and no batch review is still running or waiting for a
re-run, the script disables the workflow itself.

Usage: dispatch.py CANDIDATES_JSON STATE_JSON

Environment: GH_TOKEN, REPO, OWNER, and optionally EVENT, RUN_ID, LIMIT,
DRY_RUN and TIME_BUDGET (seconds).  Runs with a LIMIT or DRY_RUN do not
chain.
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
EVENT = os.environ.get("EVENT", "")
RUN_ID = int(os.environ.get("RUN_ID") or 0)
LIMIT = int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None
DRY_RUN = os.environ.get("DRY_RUN") == "true"
TIME_BUDGET = int(os.environ.get("TIME_BUDGET") or 19200)

MAX_JOBS = 36
MAX_DARWIN = 8
MAX_DARWIN_CONSTRAINED = 3
MAX_LONG = 10
LONG_RUN = 5400
MAX_RUNTIME = 14400
MIN_INTERVAL = 30
MIN_RATE_REMAINING = 1000
MAX_TRIES = 5
MAX_ATTEMPTS = 8
RETRY_POSITION = 15
RERUN_DELAY = 1800
MAX_RERUN_DELAY = 14400
CAPACITY_WINDOW = 1800
SCAN_INTERVAL = 300
POLL_INTERVAL = 30
SEEN_INTERVAL = 60
COMMIT_INTERVAL = 1800
ENABLED_CHECK_INTERVAL = 120
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
NOT_ACQUIRED = "was not acquired by Runner"
SMALL = {"1", "1-10", "11-100"}
COMMENT_ONLY_AUTHORS = {"r-ryantm"}
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


def epoch(stamp):
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="seconds")


def gh(args):
    for attempt in range(6):
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        err = p.stderr
        if "Not Found" in err or "HTTP 404" in err:
            raise LookupError(err.strip()[:200])
        # Retrying cannot help with a request that GitHub rejects as such
        if "HTTP 409" in err or "HTTP 422" in err:
            raise RuntimeError(err.strip()[:200])
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


def active_runs():
    """Return the queued and in-progress review runs.

    Unlike the most recent runs, this includes re-run attempts of old runs.
    """
    runs = {}
    for status in ("queued", "in_progress"):
        d = api(f"repos/{REPO}/actions/workflows/review.yml/runs?status={status}&per_page=100")
        runs |= {r["id"]: r for r in d["workflow_runs"]}
    return list(runs.values())


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


def needs_darwin(pr):
    """Return whether a pull request rebuilds anything on Darwin, going by its CI label."""
    return bucket([l["name"] for l in pr["labels"]], "10.rebuild-darwin") != "0"


def check_pr(cand, ignore_own=False):
    """Check a candidate just before its review is dispatched.

    Return a (status, reason, title, darwin, on_success) tuple.  The status
    is "ok" when the review can be dispatched, "skipped" when the pull request
    no longer qualifies, and "retry" when the check could not be decided yet,
    for example while GitHub is still computing mergeability.  The darwin
    flag tells whether the review needs the aarch64-darwin job, and
    on_success whether a successful review may approve.  With ignore_own,
    earlier results and reviews by the owner do not count, as when a pull
    request is reviewed again after an update.
    """
    n = cand["number"]
    pr = api(f"repos/NixOS/nixpkgs/pulls/{n}")

    def skip(reason, status="skipped"):
        return status, reason, pr["title"], None, None

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
    if ignore_own:
        comments = [c for c in comments if (c["user"] or {}).get("login") != OWNER]
    if any("nixpkgs-review-gha" in (c["body"] or "") for c in comments):
        return skip("has nixpkgs-review-gha comment")
    reviews = api(f"repos/NixOS/nixpkgs/pulls/{n}/reviews?per_page=100", paginate=True)
    if not ignore_own and any((r["user"] or {}).get("login") == OWNER for r in reviews):
        return skip("already reviewed by owner")
    on_success = "nothing" if pr["user"]["login"] in COMMENT_ONLY_AUTHORS else "approve"
    return "ok", None, pr["title"], needs_darwin(pr), on_success


def dispatch(n, on_success="approve", darwin=True, extra_args=None):
    inputs = INPUTS | {"on-success": on_success}
    if not darwin:
        inputs["aarch64-darwin"] = "no"
    # For example --eval local, when the CI evaluation of an old pull request has expired
    if extra_args:
        inputs["extra-args"] = extra_args
    args = ["workflow", "run", "review.yml", "-R", REPO, "--ref", "main", "-f", f"pr={n}"]
    args += [a for k, v in inputs.items() for a in ("-f", f"{k}={v}")]
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


def failure_kind(run_id):
    """Classify why the latest attempt of a failed review run failed.

    Return a (kind, time, reason) tuple.  The kind is "no runner" when every
    failed job is a review job that never got a runner, "prepare" when only
    the prepare job failed, for example while GitHub was still computing
    mergeability, and "other" otherwise.  The time is when the last failed
    job ended.
    """
    jobs = api(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=50")["jobs"]
    bad = [j for j in jobs if j["conclusion"] not in ("success", "skipped")]
    if not bad:
        return "other", time.time(), "no failed job found"
    for j in bad:
        notes = api(f"repos/{REPO}/check-runs/{j['id']}/annotations")
        j["notes"] = [a["message"] for a in notes if a["annotation_level"] == "failure"]
    when = max((epoch(j["completed_at"]) for j in bad if j["completed_at"]), default=time.time())
    if all(j["name"].startswith("review") and any(NOT_ACQUIRED in m for m in j["notes"]) for j in bad):
        return "no runner", when, "no runner"
    if [j["name"] for j in bad] == ["prepare"]:
        return "prepare", when, "prepare failed"
    return "other", when, "; ".join(f"{j['name']}: {(j['notes'] or [j['conclusion']])[0][:100]}" for j in bad)


def scan_failures(records, since):
    """Examine batch reviews whose latest attempt failed, and schedule re-runs.

    Each failed attempt is examined once.  Reviews that failed for lack of a
    runner, or in the prepare job, are re-run up to MAX_ATTEMPTS attempts in
    total, and other failures are recorded as final.  The delay before a
    re-run doubles with every attempt, so that one busy afternoon does not
    use up all attempts.  Return the end time of the most recent failure for
    lack of a runner.
    """
    by_run = {r.get("run_id"): n for n, r in records.items() if r["status"] == "dispatched"}
    page = 1
    while True:
        d = api(
            f"repos/{REPO}/actions/workflows/review.yml/runs"
            f"?status=failure&created=%3E%3D{since}&per_page=100&page={page}"
        )
        for run in d["workflow_runs"]:
            n = by_run.get(run["id"])
            rec = records.get(n) if n else None
            if rec is None or rec.get("examined") == run["run_attempt"]:
                continue
            kind, when, reason = failure_kind(run["id"])
            rec["examined"] = run["run_attempt"]
            if kind == "no runner":
                rec["no_runner_at"] = iso(when)
            if kind != "other" and run["run_attempt"] < MAX_ATTEMPTS:
                rec["rerun"] = kind
                delay = min(RERUN_DELAY * 2 ** (run["run_attempt"] - 1), MAX_RERUN_DELAY)
                rec["rerun_after"] = iso(when + delay)
                rec.pop("final", None)
                log(f"#{n} run {run['id']} attempt {run['run_attempt']} failed ({kind}), re-run scheduled")
            else:
                rec.pop("rerun", None)
                rec.pop("rerun_after", None)
                rec["final"] = reason if kind == "other" else f"{kind}, gave up after {run['run_attempt']} attempts"
                log(f"#{n} run {run['id']} attempt {run['run_attempt']} failed: {rec['final']}")
        if len(d["workflow_runs"]) < 100:
            break
        page += 1
    return max((epoch(r["no_runner_at"]) for r in records.values() if r.get("no_runner_at")), default=0.0)


def reviewed_head(run_id):
    """Return the pull request head that a review run built, from its report artifacts."""
    for a in api(f"repos/{REPO}/actions/runs/{run_id}/artifacts")["artifacts"]:
        if a["name"].startswith("report_") and not a["expired"]:
            return json.loads(gh(["api", a["archive_download_url"]]))["head"]
    return None


def rerun_failed_jobs(run_id, rec):
    """Re-run the failed jobs of a run, and return whether GitHub accepted it."""
    if DRY_RUN:
        return True
    try:
        gh(["api", "-X", "POST", f"repos/{REPO}/actions/runs/{run_id}/rerun-failed-jobs"])
    except RuntimeError as e:
        rec["final"] = f"re-run refused: {e}"[:300]
        return False
    return True


def rerun(n, rec, cand):
    """Re-run the failed jobs of a batch review, or review a changed head afresh.

    Return whether a review was started, and a short description of what
    was done.
    """
    kind = rec.pop("rerun")
    rec.pop("rerun_after", None)
    run_id = rec["run_id"]
    # Someone else may have re-run it meanwhile, and the scan sees any new failure
    run = api(f"repos/{REPO}/actions/runs/{run_id}")
    if run["status"] != "completed" or run["conclusion"] != "failure":
        return False, f"run {run_id} is {run['conclusion'] or run['status']} now, not re-running it"
    pr = api(f"repos/NixOS/nixpkgs/pulls/{n}")
    if pr["state"] != "open":
        rec["final"] = f"{pr['state']} before the re-run"
        return False, rec["final"]
    ran_with = rec.get("on_success", "approve")
    on_success = "nothing" if pr["user"]["login"] in COMMENT_ONLY_AUTHORS else ran_with
    # Re-running failed jobs keeps the original inputs, including an approval
    keeps_inputs = on_success == ran_with
    head = reviewed_head(run_id) if kind == "no runner" else None
    if kind == "no runner" and head == pr["head"]["sha"] and keeps_inputs:
        if not rerun_failed_jobs(run_id, rec):
            return False, rec["final"]
        return True, f"re-ran the failed jobs of run {run_id}"
    head = head or rec.get("head") or (cand or {}).get("headRefOid") or pr["head"]["sha"]
    if on_success != "approve":
        # Without an approval, a review of the latest head needs no triviality check
        status, reason, title, darwin = "ok", None, pr["title"], needs_darwin(pr)
    else:
        # The head that will be built has not been checked yet
        cand = {"number": n, "headRefOid": head}
        status, reason, title, darwin, on_success = check_pr(cand, ignore_own=rec.get("requeued", False))
    if status == "retry" and rec.get("tries", 0) < MAX_TRIES:
        rec["tries"] = rec.get("tries", 0) + 1
        rec["rerun"] = kind
        rec["rerun_after"] = iso(time.time() + RERUN_DELAY)
        return False, f"re-run postponed: {reason}"
    if status != "ok":
        rec["final"] = f"not re-run: {reason}"
        return False, rec["final"]
    if kind == "prepare" and keeps_inputs:
        if not rerun_failed_jobs(run_id, rec):
            return False, rec["final"]
        return True, f"re-ran run {run_id}"
    new_id = None if DRY_RUN else dispatch(n, on_success, darwin, rec.get("extra_args"))
    for key in ("examined", "no_runner_at", "tries"):
        rec.pop(key, None)
    rec.update(run_id=new_id, title=title, time=iso(time.time()), head=pr["head"]["sha"], darwin=darwin)
    rec.update(on_success=on_success, redispatched=True)
    return True, f"dispatched run {new_id} in place of run {run_id}: {title}"


def rate():
    core = api("rate_limit")["resources"]["core"]
    return core["remaining"], core["limit"]


def workflow_enabled():
    return api(f"repos/{REPO}/actions/workflows/batch-review.yml")["state"] == "active"


def other_dispatcher_active():
    """Return whether another batch-review run is running or about to start.

    A queued run counts only for a quarter of an hour, since GitHub sometimes
    leaves a run queued for good, and it would block the schedule forever.
    """
    runs = api(f"repos/{REPO}/actions/workflows/batch-review.yml/runs?per_page=20")["workflow_runs"]
    return any(
        r["id"] != RUN_ID
        and (r["status"] == "in_progress" or r["status"] in ACTIVE and time.time() - epoch(r["created_at"]) < 900)
        for r in runs
    )


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


def run(start):
    """Dispatch reviews and re-runs until the time budget is used up.

    Return whether another batch-review run should follow this one.
    """
    candidates = json.load(open(sys.argv[1]))
    state_path = sys.argv[2]
    state = json.load(open(state_path))
    records = state["records"]
    # Re-reviews of pull requests outside the candidates count as batch reviews too
    numbers = {c["number"] for c in candidates} | {int(n) for n in records}
    by_number = {c["number"]: c for c in candidates}
    since = min(r["time"] for r in records.values() if r.get("time"))[:10]

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

    def due_reruns(now):
        due = [n for n, r in records.items() if r.get("rerun") and epoch(r["rerun_after"]) <= now]
        return sorted(due, key=lambda n: records[n]["rerun_after"])

    # Requested re-reviews go first, compared against the head they last reviewed
    requeued = [
        {"number": int(n), "title": r.get("title", ""), "headRefOid": r.get("head"), "requeue": True}
        for n, r in records.items()
        if r["status"] == "requeue"
    ]
    queue = requeued + [c for c in candidates if pending(c)]
    seen = {pr_of(r) for r in fork_runs(pages=3)} - {None}
    seen |= {int(n) for n, r in records.items() if r["status"] == "dispatched"}
    remaining, limit = rate()
    log(f"queue {len(queue)} of {len(candidates)}, rate limit {remaining}/{limit}, dry run {DRY_RUN}")

    dispatched = reran = 0
    last = 0.0
    fetched = scanned = seen_at = 0.0
    no_runner_at = 0.0
    cancelled = set()
    # Whether a candidate needed Darwin when last checked, to pass over it while Macs are busy
    darwin_hint = {}
    jobs = darwin_reviews = 0
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
            waiting = sum(bool(r.get("rerun")) for r in records.values())
            log(
                f"dispatched {dispatched} and re-ran {reran} so far, {len(queue)} left in queue,"
                f" {waiting} waiting for a re-run, rate limit {remaining}/{limit}"
            )
            log(f"{jobs} of {MAX_JOBS} jobs and {darwin_reviews} Darwin reviews in flight")
        if now - scanned > SCAN_INTERVAL:
            scanned = now
            no_runner_at = scan_failures(records, since)
            save()
        if now - fetched > POLL_INTERVAL:
            active = active_runs()
            fetched = now
        batch = [r for r in active if pr_of(r) in numbers]
        long_runs = [r for r in batch if now - epoch(r["run_started_at"]) > LONG_RUN]
        # Few reviews still running by now finish before the six-hour job limit
        for r in long_runs:
            n = str(pr_of(r))
            if (
                now - epoch(r["run_started_at"]) > MAX_RUNTIME
                and records.get(n, {}).get("run_id") == r["id"]
                and r["id"] not in cancelled
            ):
                cancelled.add(r["id"])
                if not DRY_RUN:
                    try:
                        gh(["api", "-X", "POST", f"repos/{REPO}/actions/runs/{r['id']}/cancel"])
                    except RuntimeError as e:
                        log(f"#{n} could not cancel run {r['id']}: {e}")
                        continue
                records[n]["final"] = f"cancelled after {MAX_RUNTIME // 3600} hours"
                log(f"#{n} run {r['id']} cancelled after {MAX_RUNTIME // 3600} hours: {records[n]['title']}")
                save()
        constrained = now - no_runner_at < CAPACITY_WINDOW
        reruns = [] if constrained else due_reruns(now)

        def ours(r):
            rec = records.get(str(pr_of(r)))
            return rec if rec and rec.get("run_id") == r["id"] else None

        def uses_mac(r):
            # Re-run attempts repeat the darwin job, and other people's runs may use a Mac too
            return r["run_attempt"] > 1 or (ours(r) or {}).get("darwin", True)

        def footprint(r):
            return 1 if r["run_attempt"] > 1 else 3 if uses_mac(r) else 2

        jobs = sum(footprint(r) for r in active)
        darwin_reviews = sum(1 for r in batch if r not in long_runs and uses_mac(r))
        darwin_room = darwin_reviews < (MAX_DARWIN_CONSTRAINED if constrained else MAX_DARWIN)
        if not queue and not reruns:
            if not batch and not any(r.get("rerun") for r in records.values()):
                if not DRY_RUN:
                    log("all candidates handled and no batch review in flight, disabling batch-review")
                    gh(["workflow", "disable", "batch-review.yml", "-R", REPO])
                chain = False
                break
            time.sleep(60)
            fetched = 0.0
            continue
        remaining, _ = rate()
        if jobs + 2 > MAX_JOBS or len(long_runs) >= MAX_LONG or remaining < MIN_RATE_REMAINING:
            time.sleep(30)
            fetched = 0.0
            continue
        wait = MIN_INTERVAL - (time.time() - last)
        if wait > 0 and not DRY_RUN:
            time.sleep(wait)
            fetched = 0.0
            continue
        # Re-runs come first, since their pull requests are already half reviewed
        if reruns and darwin_room:
            n = reruns[0]
            try:
                started, what = rerun(int(n), records[n], by_number.get(int(n)))
            except LookupError as e:
                started, what = False, f"re-run lookup failed: {e}"
                records[n]["final"] = what
            log(f"#{n} {what}")
            save()
            if started:
                last = time.time()
                fetched = 0.0
                reran += 1
            continue
        if not queue:
            time.sleep(60)
            fetched = 0.0
            continue
        if time.time() - seen_at > SEEN_INTERVAL:
            seen |= {pr_of(r) for r in fork_runs()} - {None}
            seen_at = time.time()
        # With the Darwin cap full, pass over candidates known to need a Mac
        pick = 0 if darwin_room else next((i for i, c in enumerate(queue) if darwin_hint.get(c["number"]) is not True), None)
        if pick is None:
            time.sleep(30)
            fetched = 0.0
            continue
        cand = queue.pop(pick)
        n = cand["number"]
        requeue = cand.get("requeue", False)
        old = records.get(str(n), {})
        if n in seen and not requeue:
            status, reason, title, darwin, on_success = "skipped", "fork already has a run", cand["title"], None, None
        else:
            try:
                status, reason, title, darwin, on_success = check_pr(cand, ignore_own=requeue)
            except LookupError as e:
                status, reason, title, darwin, on_success = "skipped", f"lookup failed: {e}", cand["title"], None, None
        darwin_hint[n] = darwin
        if status == "ok" and darwin and not darwin_room:
            queue.insert(0, cand)
            continue
        if requeue and status == "retry" and old.get("tries", 0) < MAX_TRIES:
            old["tries"] = old.get("tries", 0) + 1
            log(f"#{n} re-review postponed: {reason}")
            save()
            queue.insert(min(len(queue), RETRY_POSITION), cand)
            continue
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
            flags = ("" if darwin else " without Darwin") + ("" if on_success == "approve" else " comment only")
            log(f"#{n} would dispatch{flags}: {title}")
            dispatched += 1
            continue
        extra_args = old.get("extra_args") if requeue else None
        run_id = dispatch(n, on_success, darwin, extra_args)
        last = time.time()
        fetched = 0.0
        dispatched += 1
        seen.add(n)
        records[str(n)] = {
            "status": "dispatched",
            "run_id": run_id,
            "title": title,
            "time": iso(time.time()),
            "darwin": darwin,
            "on_success": on_success,
            **({"requeued": True, "previous_run_id": old.get("previous_run_id")} if requeue else {}),
            **({"extra_args": extra_args} if extra_args else {}),
        }
        flags = ("" if darwin else " without Darwin") + ("" if on_success == "approve" else " comment only")
        log(f"#{n} dispatched run {run_id}{flags}: {title}")
        save()

    remaining, limit = rate()
    log(f"dispatched {dispatched} and re-ran {reran}, {len(queue)} left in queue, rate limit {remaining}/{limit}")
    return chain


def start_next_run():
    gh(["workflow", "run", "batch-review.yml", "-R", REPO, "--ref", "main"])


def main():
    if EVENT == "schedule" and other_dispatcher_active():
        log("another batch-review run is active, so this scheduled run has nothing to do")
        return
    start = time.time()
    try:
        chain = run(start)
    except Exception:
        # Keep the batch going after a crash, but not in a tight loop
        if LIMIT is None and not DRY_RUN and time.time() - start > 600:
            log("dispatcher crashed, starting the next batch-review run anyway")
            start_next_run()
        raise
    if chain:
        log("starting the next batch-review run")
        start_next_run()


if __name__ == "__main__":
    main()
