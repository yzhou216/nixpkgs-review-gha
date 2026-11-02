"""Exercise gh(), dispatch() and the disable path of run() against stubs, under CPython."""

import json
import os
import subprocess
import sys
import tempfile
import types

os.environ.update(REPO="yzhou216/nixpkgs-review-gha", OWNER="yzhou216", DRY_RUN="false")
sys.path.insert(0, sys.argv[1])
import dispatch as d  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0
        self.sleeps = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


# gh: no sleep after the last attempt, and a single try means a single request
clock = d.time = Clock()
requests = []
d.subprocess = types.SimpleNamespace(
    run=lambda cmd, **kw: requests.append(cmd) or subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
)
for tries, sleeps in ((1, []), (3, [15, 30])):
    requests.clear()
    clock.sleeps.clear()
    try:
        d.gh(["api", "x"], tries=tries)
    except d.Refused:
        raise AssertionError("not a refusal")
    except RuntimeError:
        pass
    assert len(requests) == tries and clock.sleeps == sleeps, (tries, requests, clock.sleeps)
d.subprocess = subprocess

# dispatch: a failed request is not repeated, and a run it created all the same is found
calls = []


def gh_failing(exc):
    def gh(args, tries=6):
        calls.append(tries)
        raise exc("HTTP 502" if exc is RuntimeError else "HTTP 422")
    return gh


for runs, expected in (([{"id": 7, "display_title": "review #5", "created_at": d.iso(clock.t)}], 7), ([], None)):
    calls.clear()
    d.gh = gh_failing(RuntimeError)
    d.fork_runs = lambda pages=1, runs=runs: runs
    try:
        got = d.dispatch(5)
    except d.Refused:
        raise AssertionError("not a refusal")
    except RuntimeError:
        got = None
        assert not runs, "a run that exists must be returned"
    assert got == expected and calls == [1], (got, calls)
calls.clear()
d.gh = gh_failing(d.Refused)
d.fork_runs = lambda pages=1: (_ for _ in ()).throw(AssertionError("looked for a run after a refusal"))
try:
    d.dispatch(5)
except d.Refused:
    pass
else:
    raise AssertionError("refusal swallowed")


# run: with nothing left to do, a review that failed since the last scan still gets its re-run
def run_once(fails_late):
    clock = d.time = Clock()
    scratch = tempfile.mkdtemp()
    cand_path, state_path = os.path.join(scratch, "candidates.json"), os.path.join(scratch, "state.json")
    json.dump([], open(cand_path, "w"))
    record = {"status": "dispatched", "run_id": 1, "title": "foo: 1 -> 2", "time": d.iso(clock.t - 3600)}
    json.dump({"records": {"5": record}}, open(state_path, "w"))
    sys.argv[1:] = [cand_path, state_path]
    disabled = []
    scans = []

    def scan(records, since):
        scans.append(clock.t)
        if fails_late and len(scans) == 2:
            records["5"].update(rerun="no runner", rerun_after=d.iso(clock.t + 10**6), examined=1)
        return 0.0

    d.DRY_RUN, d.LIMIT, d.TIME_BUDGET = False, None, 1000
    d.fork_runs = lambda pages=1: []
    d.rate = lambda: (5000, 5000)
    d.workflow_enabled = lambda: True
    d.active_runs = lambda: []
    d.scan_failures = scan
    d.commit_state = lambda path: True
    d.gh = lambda args, tries=6: disabled.append(args[:2]) or ""
    chain = d.run(clock.t)
    return chain, disabled, scans, json.load(open(state_path))["records"]["5"]


chain, disabled, scans, rec = run_once(fails_late=True)
assert chain and not disabled and len(scans) >= 2 and rec["rerun"] == "no runner", (chain, disabled, scans, rec)
chain, disabled, scans, rec = run_once(fails_late=False)
assert not chain and disabled == [["workflow", "disable"]] and len(scans) == 2, (chain, disabled, scans)
print("run paths ok")


# run: a candidate that needs a Mac waits while the Darwin cap is full, one behind it that needs none is dispatched,
# and a dispatch whose lookup fails is retried instead of crashing the dispatcher
dispatch_times = []


def run_queue(dispatch_error=None, reviewing=(), budget=1000):
    clock = d.time = Clock()
    scratch = tempfile.mkdtemp()
    cand_path, state_path = os.path.join(scratch, "candidates.json"), os.path.join(scratch, "state.json")
    cands = [{"number": n, "title": "foo: 1 -> 2", "headRefOid": "a" * 40} for n in (7, 8)]
    json.dump(cands, open(cand_path, "w"))
    busy = {str(100 + i): {"status": "dispatched", "run_id": 100 + i, "title": "bar: 1 -> 2",
                           "time": d.iso(clock.t), "darwin": True} for i in range(d.MAX_DARWIN)}
    json.dump({"records": busy}, open(state_path, "w"))
    sys.argv[1:] = [cand_path, state_path]
    checks, dispatches = [], []
    dispatch_times.clear()

    def check_pr(cand, ignore_own=False):
        checks.append(cand["number"])
        return "ok", None, cand["title"], cand["number"] == 7, "approve", None, "a" * 40

    def dispatch(n, on_success, darwin, extra_args):
        dispatches.append(n)
        dispatch_times.append(clock.t)
        if dispatch_error:
            raise dispatch_error("HTTP 404: Not Found")
        return 900 + n

    active = [{"id": 100 + i, "display_title": f"review #{100 + i}", "run_attempt": 1,
               "run_started_at": d.iso(clock.t), "created_at": d.iso(clock.t)} for i in range(d.MAX_DARWIN)]
    active += [{"id": 500 + n, "display_title": f"review #{n}", "run_attempt": 1,
                "run_started_at": d.iso(clock.t), "created_at": d.iso(clock.t)} for n in reviewing]
    d.DRY_RUN, d.LIMIT, d.TIME_BUDGET = False, None, budget
    d.fork_runs = lambda pages=1: []
    d.rate = lambda: (5000, 5000)
    d.workflow_enabled = lambda: True
    d.active_runs = lambda: active
    d.scan_failures = lambda records, since: 0.0
    d.commit_state = lambda path: True
    d.check_pr = check_pr
    d.dispatch = dispatch
    d.gh = lambda args, tries=6: (_ for _ in ()).throw(AssertionError(f"gh {args}"))
    chain = d.run(clock.t)
    return chain, checks, dispatches, json.load(open(state_path))["records"]


chain, checks, dispatches, records = run_queue()
assert chain and dispatches == [8] and checks == [7, 8], (checks, dispatches)
assert "7" not in records and records["8"]["run_id"] == 908, records

chain, checks, dispatches, records = run_queue(LookupError, budget=6 * d.RETRY_DELAY)
# Retries are spaced in time, not by queue position, so a short queue does not use them up at once
assert all(b - a >= d.RETRY_DELAY for a, b in zip(dispatch_times, dispatch_times[1:])), dispatch_times
assert chain and dispatches == [8] * d.MAX_TRIES, dispatches
assert records["8"]["status"] == "retry" and records["8"]["tries"] == d.MAX_TRIES, records["8"]
# A candidate that the fork is already reviewing is skipped without a check
chain, checks, dispatches, records = run_queue(reviewing=(8,))
assert checks == [7] and not dispatches and records["8"]["reason"] == "fork run in flight", (checks, records)
print("queue paths ok")


# run: the last candidate whose CI evaluation is still running is checked again after RETRY_DELAY, and the
# dispatcher does not disable the workflow while it waits
def run_pending_eval(pending_checks):
    clock = d.time = Clock()
    scratch = tempfile.mkdtemp()
    cand_path, state_path = os.path.join(scratch, "candidates.json"), os.path.join(scratch, "state.json")
    json.dump([{"number": 7, "title": "foo: 1 -> 2", "headRefOid": "a" * 40}], open(cand_path, "w"))
    json.dump({"records": {}}, open(state_path, "w"))
    sys.argv[1:] = [cand_path, state_path]
    check_times, dispatches, disabled = [], [], []

    def check_pr(cand, ignore_own=False):
        check_times.append(clock.t)
        if len(check_times) <= pending_checks:
            return d.not_dispatched("retry", "CI evaluation still running", cand["title"])
        return "ok", None, cand["title"], True, "approve", None, "a" * 40

    d.DRY_RUN, d.LIMIT, d.TIME_BUDGET = False, None, 10 * d.RETRY_DELAY
    d.fork_runs = lambda pages=1: []
    d.rate = lambda: (5000, 5000)
    d.workflow_enabled = lambda: True
    d.active_runs = lambda: []
    d.scan_failures = lambda records, since: 0.0
    d.commit_state = lambda path: True
    d.check_pr = check_pr
    d.dispatch = lambda n, on_success, darwin, extra_args: dispatches.append(n) or 907
    d.gh = lambda args, tries=6: disabled.append(args[:2]) or ""
    chain = d.run(clock.t)
    return chain, check_times, dispatches, disabled, json.load(open(state_path))["records"]["7"]


chain, check_times, dispatches, disabled, rec = run_pending_eval(2)
assert len(check_times) == 3 and dispatches == [7] and rec["status"] == "dispatched", (check_times, dispatches, rec)
assert all(b - a >= d.RETRY_DELAY for a, b in zip(check_times, check_times[1:])), check_times
assert disabled == [["workflow", "disable"]] and not chain, disabled
chain, check_times, dispatches, disabled, rec = run_pending_eval(d.MAX_TRIES)
assert len(check_times) == d.MAX_TRIES and not dispatches and rec["tries"] == d.MAX_TRIES, (check_times, rec)
assert check_times[-1] - check_times[0] >= (d.MAX_TRIES - 1) * d.RETRY_DELAY, check_times
print("retry delay paths ok")


# run: a requested re-review whose check stays undecided is checked MAX_TRIES times, as a candidate is, and a
# requeue record that still carries a re-run of its old run is reviewed afresh, without re-running the old run
def run_requeue(record, check_status, budget=10 * d.RETRY_DELAY):
    clock = d.time = Clock()
    scratch = tempfile.mkdtemp()
    cand_path, state_path = os.path.join(scratch, "candidates.json"), os.path.join(scratch, "state.json")
    json.dump([], open(cand_path, "w"))
    json.dump({"records": {"7": dict(record, status="requeue", title="foo: 1 -> 2", head="a" * 40)}},
              open(state_path, "w"))
    sys.argv[1:] = [cand_path, state_path]
    checks, dispatches, reruns = [], [], []

    def check_pr(cand, ignore_own=False):
        checks.append(ignore_own)
        if check_status == "retry":
            return d.not_dispatched("retry", "CI evaluation still running", cand["title"])
        return "ok", None, cand["title"], True, "approve", None, "a" * 40

    d.DRY_RUN, d.LIMIT, d.TIME_BUDGET = False, None, budget
    d.fork_runs = lambda pages=1: []
    d.rate = lambda: (5000, 5000)
    d.workflow_enabled = lambda: True
    d.active_runs = lambda: []
    d.scan_failures = lambda records, since: 0.0
    d.commit_state = lambda path: True
    d.check_pr = check_pr
    d.dispatch = lambda n, on_success, darwin, extra_args: dispatches.append(n) or 907
    d.rerun = lambda n, rec, candidate_head=None: reruns.append(n) or (True, "re-ran")
    d.gh = lambda args, tries=6: ""
    d.run(clock.t)
    return checks, dispatches, reruns, json.load(open(state_path))["records"]["7"]


checks, dispatches, reruns, rec = run_requeue({}, "retry")
assert len(checks) == d.MAX_TRIES and all(checks) and not dispatches, checks
assert rec["status"] == "retry" and rec["tries"] == d.MAX_TRIES, rec
# Before MAX_TRIES, the requeue record survives the end of the run with its head and the tries so far
checks, dispatches, reruns, rec = run_requeue({}, "retry", budget=d.RETRY_DELAY + 60)
assert len(checks) == 2 and rec["status"] == "requeue" and rec["tries"] == 2 and rec["head"] == "a" * 40, rec
stale = {"run_id": 1, "rerun": "no runner", "rerun_after": d.iso(0), "darwin": True}
checks, dispatches, reruns, rec = run_requeue(stale, "ok")
assert not reruns and dispatches == [7] and rec["status"] == "dispatched" and rec["run_id"] == 907, (reruns, rec)
assert "rerun" not in rec and rec["requeued"], rec
print("requeue paths ok")
