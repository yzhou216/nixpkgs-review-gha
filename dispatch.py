"""Self-chaining nixpkgs-review-gha batch dispatcher.

Each run of the batch-review workflow calls this script.  It dispatches
review.yml for the next candidate pull requests, newest first, and keeps the
number of in-flight review runs under a cap.  Every pull request is checked
again just before its review is dispatched, and the outcome of that check is
recorded in the state file so that later runs skip it.

GitHub often has no macOS arm64 runner to spare during US working hours.
The aarch64-darwin job of a review then fails after about fifteen minutes
without starting, and the review posts nothing.  Once runners are available
again, the script checks the pull request again and re-runs only the failed
jobs of such a review, which keeps its Linux results, provided that the head
of the pull request and the inputs of the review are unchanged.  Otherwise it
dispatches a new review.  A review whose prepare job failed is re-run in the
same way, and since the re-run fetches the pull request again, a changed head
needs no new review.  While these failures are recent, it keeps fewer
Darwin reviews in flight and holds back re-runs.  The script cancels a
review that is still running after MAX_RUNTIME seconds, since few of them
finish before GitHub's six-hour job limit, and they hold runners that other
reviews need.

GitHub Pro runs 40 jobs at a time but only 5 of them on macOS.  The script
therefore counts the jobs that review runs occupy rather than the runs, and
caps Darwin reviews separately.  A pull request whose CI label reports no
Darwin rebuilds is reviewed without the aarch64-darwin job, so it needs no
Mac at all.  While the Darwin cap is full, the script keeps dispatching such
Linux-only reviews, and the reviews that need a Mac wait in the order they
arrived until there is room.

Pull requests by COMMENT_ONLY_AUTHORS get the review result as a comment
but no approval.  A committer asked on #550000 for no automated approvals of
r-ryantm's pull requests, and said that the results are still useful, since
r-ryantm only builds on x86_64-linux.  Such a pull request is reviewed unless
a result for its current head exists already, whoever posted it.  Records
with the status "requeue" ask for a pull request to be reviewed, for example
again after an update, and go to the front of the queue, newest first.  None
is reviewed again while a result for its current head exists.  The
state file can also hold re-reviews of pull requests that are not candidates,
such as the user's own, which run with their own on-success setting and are
tracked like any other review.  A pull request whose CI evaluation has
expired is reviewed with --eval local, and none is dispatched while a review
of it is still running in the fork.

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

import collections
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
RETRY_DELAY = 600
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
# Head commit line of a nixpkgs-review-gha result comment
RESULT_COMMIT = re.compile(r"^Commit: \[?`([0-9a-f]{40})`", re.M)
EVAL_WORKFLOWS = {"Eval", "PR"}


def log(*args):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(ts, *args, flush=True)


def epoch(stamp):
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="seconds")


class Refused(RuntimeError):
    """GitHub rejected a request as such, so retrying it cannot help."""


def gh(args, tries=6):
    for attempt in range(tries):
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        err = p.stderr
        if "Not Found" in err or "HTTP 404" in err:
            raise LookupError(err.strip()[:200])
        # Retrying cannot help with a request that GitHub rejects as such
        if "HTTP 409" in err or "HTTP 422" in err:
            raise Refused(err.strip()[:200])
        if attempt + 1 < tries:
            log("gh error, retrying:", " ".join(args[:3]), err.strip()[:200])
            time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"gh {' '.join(args[:3])} failed: {err.strip()[:300]}")


def api(path, paginate=False, key=None):
    """Return the response to a GET request, or with paginate the items of every page.

    The key names the list that holds the items of a page, for endpoints whose
    pages are objects rather than lists.
    """
    if paginate:
        pages = json.loads(gh(["api", "--paginate", "--slurp", path]))
        return [x for page in pages for x in (page[key] if key else page)]
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
    olds = version_variants(old) if len(old) >= 3 else set()
    news = version_variants(new)
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
    """Return the largest rebuild count label with the prefix, such as "1-10", or None."""
    vals = [l.split(": ", 1)[1] for l in labels if l.startswith(prefix + ": ")]

    # Upper bound of a bucket, so that "1-10" wins over "1" and "101-500" over "11-100"
    def upper(v):
        nums = re.findall(r"\d+", v)
        return int(nums[-1]) if nums else -1

    return max(vals, key=upper, default=None)


def needs_darwin(pr):
    """Return whether a pull request rebuilds anything on Darwin, going by its CI label."""
    return bucket([l["name"] for l in pr["labels"]], "10.rebuild-darwin") != "0"


def reviewed_heads(comments):
    """Return the pull request heads that nixpkgs-review-gha result comments report."""
    return {
        sha
        for c in comments
        if "nixpkgs-review-gha" in (c["body"] or "")
        for sha in RESULT_COMMIT.findall(c["body"])
    }


def ci_eval(head):
    """Return whether the nixpkgs CI evaluation of a head is "available", "pending" or "missing".

    nixpkgs-review takes the packages to build from the comparison artifact
    of this evaluation.  Without it, nixpkgs-review waits for ten minutes and
    then gives up, so the review needs --eval local instead.  The artifact
    expires a few months after the evaluation.
    """
    runs = api(f"repos/NixOS/nixpkgs/actions/runs?head_sha={head}&per_page=100")["workflow_runs"]
    runs = [r for r in runs if r["name"] in EVAL_WORKFLOWS]
    if any(r["status"] != "completed" for r in runs):
        return "pending"
    for r in runs:
        try:
            artifacts = api(f"repos/NixOS/nixpkgs/actions/runs/{r['id']}/artifacts?per_page=100")["artifacts"]
        except LookupError:
            # Deleted since it was listed, so it holds no evaluation
            continue
        if any(a["name"] == "comparison" and not a["expired"] for a in artifacts):
            return "available"
    return "missing"


def eval_args(head, extra_args):
    """Return whether a review of a head can start, and its extra nixpkgs-review arguments.

    Without extra arguments given, the review waits while the CI evaluation of
    the head is still running, and needs --eval local when it is gone.
    """
    if extra_args:
        return True, extra_args
    evaluation = ci_eval(head)
    return evaluation != "pending", "--eval local" if evaluation == "missing" else None


def not_dispatched(status, reason, title):
    """Return the check_pr tuple of a pull request whose review is not dispatched."""
    return status, reason, title, None, None, None, None


def check_pr(cand, ignore_own=False):
    """Check a candidate just before its review is dispatched.

    Return a (status, reason, title, darwin, on_success, extra_args, head)
    tuple.  The status is "ok" when the review can be dispatched, "skipped"
    when the pull request no longer qualifies, and "retry" when the check
    could not be decided yet, for example while GitHub is still computing
    mergeability.  The darwin flag tells whether the review needs the
    aarch64-darwin job, on_success whether a successful review may approve,
    extra_args holds the extra nixpkgs-review arguments, such as --eval local
    when the CI evaluation of the head is gone, and head is the head that
    passed the check.  A result for the current head always counts as an
    earlier review, whoever posted it.  With ignore_own, the owner's results
    for other heads and the owner's reviews do not count, as when a pull
    request is reviewed again after an update.

    Pull requests by COMMENT_ONLY_AUTHORS need no triviality check, since
    their review only posts a comment.  For them, only a result for the
    current head counts as an earlier review.
    """
    n = cand["number"]
    pr = api(f"repos/NixOS/nixpkgs/pulls/{n}")
    comment_only = pr["user"]["login"] in COMMENT_ONLY_AUTHORS

    def skip(reason, status="skipped"):
        return not_dispatched(status, reason, pr["title"])

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
    if pr["head"]["sha"] != cand["headRefOid"] and not comment_only:
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
    # A second review of the same head would post a second result, even after waiting for a fork run in flight
    if pr["head"]["sha"] in reviewed_heads(comments):
        return skip("has a result for the current head")
    if not comment_only:
        if ignore_own:
            comments = [c for c in comments if (c["user"] or {}).get("login") != OWNER]
        if any("nixpkgs-review-gha" in (c["body"] or "") for c in comments):
            return skip("has nixpkgs-review-gha comment")
        if not ignore_own:
            reviews = api(f"repos/NixOS/nixpkgs/pulls/{n}/reviews?per_page=100", paginate=True)
            if any((r["user"] or {}).get("login") == OWNER for r in reviews):
                return skip("already reviewed by owner")
    ready, extra_args = eval_args(pr["head"]["sha"], cand.get("extra_args"))
    if not ready:
        return skip("CI evaluation still running", "retry")
    on_success = "nothing" if comment_only else "approve"
    return "ok", None, pr["title"], needs_darwin(pr), on_success, extra_args, pr["head"]["sha"]


def dispatch(n, on_success="approve", darwin=True, extra_args=None):
    inputs = INPUTS | {"on-success": on_success}
    if not darwin:
        inputs["aarch64-darwin"] = "no"
    # For example --eval local, when the CI evaluation of an old pull request has expired
    if extra_args:
        inputs["extra-args"] = extra_args
    args = ["workflow", "run", "review.yml", "-R", REPO, "--ref", "main", "-f", f"pr={n}"]
    args += [a for k, v in inputs.items() for a in ("-f", f"{k}={v}")]
    started = time.time() - 10
    failure = None
    try:
        # A failed request may have created the run all the same, so repeating it could review twice
        out = gh(args, tries=1)
    except Refused:
        raise
    except RuntimeError as e:
        failure, out = e, ""
    m = re.search(r"/actions/runs/(\d+)", out)
    if m:
        return int(m[1])
    for _ in range(12):
        time.sleep(5)
        for run in fork_runs():
            if pr_of(run) == n and epoch(run["created_at"]) >= started:
                return run["id"]
    if failure:
        raise failure
    return None


def failure_kind(run_id):
    """Classify why the latest attempt of a failed review run failed.

    Return a (kind, time, reason, mac) tuple.  The kind is "no runner" when
    every failed job is a review job that never got a runner, "prepare" when
    only the prepare job failed, for example while GitHub was still
    computing mergeability, and "other" otherwise.  The time is when the
    last failed job ended, and mac tells whether a Darwin job found no
    runner, the sign of the afternoon shortage of macOS runners.
    """
    jobs = api(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=50")["jobs"]
    bad = [j for j in jobs if j["conclusion"] not in ("success", "skipped")]
    if not bad:
        return "other", time.time(), "no failed job found", False
    for j in bad:
        notes = api(f"repos/{REPO}/check-runs/{j['id']}/annotations")
        j["notes"] = [a["message"] for a in notes if a["annotation_level"] == "failure"]
    when = max((epoch(j["completed_at"]) for j in bad if j["completed_at"]), default=time.time())
    if all(j["name"].startswith("review") and any(NOT_ACQUIRED in m for m in j["notes"]) for j in bad):
        return "no runner", when, "no runner", any("darwin" in j["name"] for j in bad)
    if [j["name"] for j in bad] == ["prepare"]:
        return "prepare", when, "prepare failed", False
    reason = "; ".join(f"{j['name']}: {(j['notes'] or [j['conclusion']])[0][:100]}" for j in bad)
    return "other", when, reason, False


def scan_failures(records, since):
    """Examine batch reviews whose latest attempt failed, and schedule re-runs.

    Each failed attempt is examined once.  Reviews that failed for lack of a
    runner, or in the prepare job, are re-run up to MAX_ATTEMPTS attempts in
    total, and other failures are recorded as final.  The delay before a
    re-run doubles with every attempt, so that one busy afternoon does not
    use up all attempts.  Return the end time of the most recent failure for
    lack of a macOS runner.
    """
    by_run = {r.get("run_id"): n for n, r in records.items() if r["status"] == "dispatched"}
    path = f"repos/{REPO}/actions/workflows/review.yml/runs?status=failure&created=%3E%3D{since}&per_page=100"
    for run in api(path, paginate=True, key="workflow_runs"):
        n = by_run.get(run["id"])
        rec = records.get(n) if n else None
        if rec is None or rec.get("examined") == run["run_attempt"]:
            continue
        kind, when, reason, mac = failure_kind(run["id"])
        rec["examined"] = run["run_attempt"]
        # Only a Darwin job without a runner shows that Macs are short
        if kind == "no runner" and mac:
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
    return max((epoch(r["no_runner_at"]) for r in records.values() if r.get("no_runner_at")), default=0.0)


def reviewed_head(run_id):
    """Return the pull request head that a review run built, from its report artifacts, or None."""
    for a in api(f"repos/{REPO}/actions/runs/{run_id}/artifacts")["artifacts"]:
        if a["name"].startswith("report_") and not a["expired"]:
            try:
                return json.loads(gh(["api", a["archive_download_url"]]))["head"]
            except LookupError:
                # Deleted since it was listed, so the head is unknown, as with an expired one
                continue
    return None


def rerun_failed_jobs(run_id, rec, kind, head):
    """Re-run the failed jobs of a run that will build the given head.

    Return whether GitHub accepted it, and a short description of what was
    done.  The record notes the kind of failure that was re-run, since
    re-running a failed prepare job runs every review job again.  Other
    request failures propagate, so that the caller tries again later.
    """
    if not DRY_RUN:
        try:
            gh(["api", "-X", "POST", f"repos/{REPO}/actions/runs/{run_id}/rerun-failed-jobs"])
        except Refused as e:
            rec["final"] = f"re-run refused: {e}"[:300]
            return False, rec["final"]
    rec.pop("tries", None)
    rec.update(reran=kind, head=head)
    return True, f"re-ran the failed jobs of run {run_id}"


def postpone(rec, kind, reason):
    """Schedule a re-run again for later, or give up after MAX_TRIES tries.

    Return a short description of what was done.
    """
    if rec.get("tries", 0) >= MAX_TRIES:
        rec["final"] = f"not re-run: {reason}"
        return rec["final"]
    rec["tries"] = rec.get("tries", 0) + 1
    rec["rerun"] = kind
    rec["rerun_after"] = iso(time.time() + RERUN_DELAY)
    return f"re-run postponed: {reason}"


def rerun(n, rec, candidate_head=None):
    """Re-run the failed jobs of a batch review, or review it afresh when its head or inputs changed.

    Return whether a review was started, and a short description of what
    was done.  The candidate head is the head of the pull request in the
    candidate list, for a record that holds none.
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
    current = pr["head"]["sha"]
    # A re-run would post a second result if anyone posted one for the current head meanwhile
    comments = api(f"repos/NixOS/nixpkgs/issues/{n}/comments?per_page=100", paginate=True)
    if current in reviewed_heads(comments):
        rec["final"] = "not re-run: has a result for the current head"
        return False, rec["final"]
    # A re-run of the failed jobs of a run without a runner builds the head that the run built
    built = reviewed_head(run_id) if kind == "no runner" else None
    if ran_with == "approve" or pr["user"]["login"] in COMMENT_ONLY_AUTHORS:
        # The pull request must still qualify, as on the main path, and a new head is checked afresh
        head = built or rec.get("head") or candidate_head
        cand = {"number": n, "headRefOid": head, "extra_args": rec.get("extra_args")}
        status, reason, title, darwin, on_success, extra_args, checked = check_pr(
            cand, ignore_own=rec.get("requeued", False)
        )
    else:
        # Re-reviews without an approval outside COMMENT_ONLY_AUTHORS, such as the owner's own, need no check
        status, reason, title, darwin, checked = "ok", None, pr["title"], needs_darwin(pr), current
        on_success = ran_with
        ready, extra_args = eval_args(checked, rec.get("extra_args"))
        if not ready:
            status, reason = "retry", "CI evaluation still running"
    if status == "retry":
        return False, postpone(rec, kind, reason)
    if status != "ok":
        rec["final"] = f"not re-run: {reason}"
        return False, rec["final"]
    # Re-running failed jobs keeps the original inputs, including an approval, --eval local and the darwin job
    same_inputs = on_success == ran_with and extra_args == rec.get("extra_args") and darwin == rec.get("darwin", True)
    # A re-run of a failed prepare job fetches the pull request again, so it builds the checked head too
    if same_inputs and (kind == "prepare" or built == checked):
        return rerun_failed_jobs(run_id, rec, kind, checked)
    try:
        new_id = None if DRY_RUN else dispatch(n, on_success, darwin, extra_args)
    except LookupError as e:
        # As on the main path, a dispatch that GitHub answered with Not Found is tried again later
        raise RuntimeError(f"dispatch failed: {e}") from e
    for key in ("examined", "no_runner_at", "tries", "reran"):
        rec.pop(key, None)
    rec.update(run_id=new_id, title=title, time=iso(time.time()), head=checked, darwin=darwin)
    rec.update(on_success=on_success, redispatched=True)
    if extra_args:
        rec["extra_args"] = extra_args
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
    """Commit the state file, push it to the batch-review branch, and return whether the push succeeded."""
    repo_dir = os.path.dirname(os.path.abspath(state_path))

    def git(*args):
        return subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True)

    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("add", os.path.basename(state_path))
    # Push even without a new commit, in case an earlier push failed
    if git("diff", "--cached", "--quiet").returncode != 0:
        git("commit", "-q", "-m", "batch-review: update state")
    # Rebase onto commits pushed meanwhile, such as dispatcher updates
    url = f"https://x-access-token:{os.environ['GH_TOKEN']}@github.com/{REPO}.git"
    p = git("pull", "-q", "--rebase", url, "batch-review")
    if p.returncode != 0:
        # Leave no rebase in progress, so that the next commit starts from a clean branch
        git("rebase", "--abort")
        log(f"state pull failed: {p.stderr.strip()[:200]}")
        return False
    p = git("push", "-q", url, "HEAD:batch-review")
    log("committed state" if p.returncode == 0 else f"state push failed: {p.stderr.strip()[:200]}")
    return p.returncode == 0


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
    candidate_heads = {c["number"]: c.get("headRefOid") for c in candidates}
    since = min((r["time"] for r in records.values() if r.get("time")), default=iso(start))[:10]

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

    def awaits_rerun(r):
        # Only a dispatched record tracks its run, and a requeued one is reviewed afresh instead
        return r["status"] == "dispatched" and bool(r.get("rerun"))

    def due_reruns(now):
        due = [n for n, r in records.items() if awaits_rerun(r) and epoch(r["rerun_after"]) <= now]
        return sorted(due, key=lambda n: records[n]["rerun_after"])

    # Requested re-reviews go first, newest first, compared against the head they last reviewed
    requeued = [
        {
            "number": int(n),
            "title": r.get("title", ""),
            "headRefOid": r.get("head"),
            "requeue": True,
            "extra_args": r.get("extra_args"),
        }
        for n, r in sorted(records.items(), key=lambda item: -int(item[0]))
        if r["status"] == "requeue"
    ]
    queue = requeued + [c for c in candidates if pending(c)]
    # Candidates with a dispatched record never enter the queue, so only fork runs count
    seen = {pr_of(r) for r in fork_runs(pages=3)} - {None}
    remaining, limit = rate()
    log(f"queue {len(queue)} of {len(candidates)}, rate limit {remaining}/{limit}, dry run {DRY_RUN}")

    dispatched = reran = 0
    last = 0.0
    fetched = scanned = seen_at = 0.0
    no_runner_at = 0.0
    cancelled = set()
    # Checked candidates that need a Mac while no Darwin review fits, served first come, first served
    waiting_for_mac = collections.deque()
    # Requested re-reviews of pull requests that the fork is still reviewing
    waiting_for_run = []
    # Candidates whose check could not be decided yet, each with the time to check it again
    retrying = []
    jobs = darwin_reviews = 0
    # When nothing was left to do, so that a scan afterwards can find reviews that failed meanwhile
    idle_since = None
    checked = committed = start
    chain = LIMIT is None and not DRY_RUN

    def ours(r):
        rec = records.get(str(pr_of(r)))
        return rec if rec and rec.get("run_id") == r["id"] else None

    def single_job(r):
        # Re-run attempts repeat the darwin job, unless a failed prepare job brings back every review job
        return r["run_attempt"] > 1 and (ours(r) or {}).get("reran") != "prepare"

    def uses_mac(r):
        # Other people's runs may use a Mac too, and a re-run of a Linux-only review never does
        return (ours(r) or {}).get("darwin", True)

    def footprint(r):
        return 1 if single_job(r) else 3 if uses_mac(r) else 2

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
            waiting = sum(map(awaits_rerun, records.values()))
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
            in_flight = {pr_of(r) for r in active}
            queue[:0] = [c for c in waiting_for_run if c["number"] not in in_flight]
            waiting_for_run = [c for c in waiting_for_run if c["number"] in in_flight]
        queue[:0] = [c for due, c in retrying if due <= now]
        retrying = [(due, c) for due, c in retrying if due > now]
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
                if not DRY_RUN:
                    try:
                        gh(["api", "-X", "POST", f"repos/{REPO}/actions/runs/{r['id']}/cancel"])
                    except (RuntimeError, LookupError) as e:
                        log(f"#{n} could not cancel run {r['id']}: {e}")
                        continue
                cancelled.add(r["id"])
                records[n]["final"] = f"cancelled after {MAX_RUNTIME // 3600} hours"
                log(f"#{n} run {r['id']} cancelled after {MAX_RUNTIME // 3600} hours: {records[n]['title']}")
                save()
        constrained = now - no_runner_at < CAPACITY_WINDOW
        reruns = [] if constrained else due_reruns(now)
        jobs = sum(footprint(r) for r in active)
        darwin_reviews = sum(1 for r in batch if r not in long_runs and uses_mac(r))
        # A review with the aarch64-darwin job takes three jobs, one without it two
        darwin_room = darwin_reviews < (MAX_DARWIN_CONSTRAINED if constrained else MAX_DARWIN) and jobs + 3 <= MAX_JOBS
        if not queue and not reruns and not waiting_for_mac and not waiting_for_run and not retrying:
            if batch or any(map(awaits_rerun, records.values())):
                idle_since = None
            elif idle_since is None:
                # A review that failed since the last scan may still need a re-run
                idle_since = now
            elif scanned > idle_since:
                if not DRY_RUN:
                    log("all candidates handled and no batch review in flight, disabling batch-review")
                    gh(["workflow", "disable", "batch-review.yml", "-R", REPO])
                chain = False
                break
            time.sleep(60)
            fetched = 0.0
            continue
        idle_since = None
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
            kind = records[n]["rerun"]
            if int(n) in in_flight:
                # A review dispatched by hand or by another session is still running
                records[n]["rerun_after"] = iso(time.time() + RERUN_DELAY)
                log(f"#{n} re-run waits for the fork run in flight")
                save()
                continue
            try:
                started, what = rerun(int(n), records[n], candidate_heads.get(int(n)))
            except LookupError as e:
                started, what = False, f"re-run lookup failed: {e}"
                records[n]["final"] = what
            except RuntimeError as e:
                # GitHub refused or kept failing a request, so try again later
                started, what = False, postpone(records[n], kind, f"request failed: {e}")
            log(f"#{n} {what}")
            save()
            if started:
                last = time.time()
                fetched = 0.0
                reran += 1
            continue
        if not queue and not (darwin_room and waiting_for_mac):
            time.sleep(60)
            fetched = 0.0
            continue
        if time.time() - seen_at > SEEN_INTERVAL:
            seen |= {pr_of(r) for r in fork_runs()} - {None}
            seen_at = time.time()
        # While no Darwin review fits, a candidate that needs a Mac moves to waiting_for_mac once checked
        cand = waiting_for_mac.popleft() if darwin_room and waiting_for_mac else queue.pop(0)
        n = cand["number"]
        requeue = cand.get("requeue", False)
        old = records.get(str(n), {})
        if n in seen and not requeue:
            result = not_dispatched("skipped", "fork already has a run", cand["title"])
        elif n in in_flight:
            # A review dispatched by hand or by another session is still running
            if requeue:
                waiting_for_run.append(cand)
                log(f"#{n} re-review waits for the fork run in flight")
                continue
            result = not_dispatched("skipped", "fork run in flight", cand["title"])
        else:
            try:
                result = check_pr(cand, ignore_own=requeue)
            except LookupError as e:
                result = not_dispatched("skipped", f"lookup failed: {e}", cand["title"])
            except RuntimeError as e:
                # GitHub refused or kept failing a request, so try again later
                result = not_dispatched("retry", f"check failed: {e}", cand["title"])
        status, reason, title, darwin, on_success, extra_args, head = result
        if status == "ok" and darwin and not darwin_room:
            waiting_for_mac.append(cand)
            continue
        if status == "ok" and not DRY_RUN:
            try:
                run_id = dispatch(n, on_success, darwin, extra_args)
            except (RuntimeError, LookupError) as e:
                # GitHub refused or kept failing the dispatch, so try again later
                status, reason = "retry", f"dispatch failed: {e}"
            last = time.time()
            fetched = 0.0
        tries = old.get("tries", 0) + (status == "retry")
        if status != "ok":
            if requeue and status == "retry" and tries < MAX_TRIES:
                # A requested re-review keeps its record while it waits, so that the request survives the run
                old["tries"] = tries
                log(f"#{n} re-review postponed: {reason}")
            else:
                records[str(n)] = {"status": status, "reason": reason, "title": title, "tries": tries}
                log(f"#{n} {status}: {reason} ({title})")
            save()
            # Give GitHub time to compute mergeability, or the CI time to evaluate, even at the end of the queue
            if status == "retry" and tries < MAX_TRIES:
                retrying.append((time.time() + RETRY_DELAY, cand))
            continue
        flags = ("" if darwin else " without Darwin") + ("" if on_success == "approve" else " comment only")
        flags += f" ({extra_args})" if extra_args else ""
        dispatched += 1
        if DRY_RUN:
            log(f"#{n} would dispatch{flags}: {title}")
            continue
        records[str(n)] = {
            "status": "dispatched",
            "run_id": run_id,
            "title": title,
            "time": iso(time.time()),
            "head": head,
            "darwin": darwin,
            "on_success": on_success,
            **({"requeued": True, "previous_run_id": old.get("previous_run_id")} if requeue else {}),
            **({"extra_args": extra_args} if extra_args else {}),
        }
        log(f"#{n} dispatched run {run_id}{flags}: {title}")
        save()

    remaining, limit = rate()
    log(f"dispatched {dispatched} and re-ran {reran}, {len(queue)} left in queue, rate limit {remaining}/{limit}")
    return chain


def start_next_run():
    """Dispatch the next batch-review run.

    The next run waits in the batch-review concurrency group until the save
    state step of the run before it has finished.  A scheduled run has a
    concurrency group of its own, though, so its successor starts at once
    and would check out an older state file.  A scheduled run therefore
    commits the state file itself first, and leaves the restart to the
    schedule when the push fails, since a successor working from the older
    state could never push its own.  Nothing is dispatched once the
    workflow has been disabled, since GitHub would refuse it.
    """
    if not workflow_enabled():
        log("batch-review is disabled, so no next run starts")
        return
    if EVENT == "schedule" and not commit_state(sys.argv[2]):
        log("state not pushed, so the schedule restarts the chain instead")
        return
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
