"""Exercise rerun() paths against stubbed GitHub calls, under CPython."""

import os
import sys

os.environ.update(REPO="yzhou216/nixpkgs-review-gha", OWNER="yzhou216", DRY_RUN="false")
sys.path.insert(0, sys.argv[1])
import dispatch as d  # noqa: E402

HEAD_OLD, HEAD_NEW = "a" * 40, "b" * 40
ALL = ",".join(d.SYSTEMS)
calls = []


real_check_pr = d.check_pr


def setup(head, author="r-ryantm", comments=(), evaluation="available", reviewed=HEAD_OLD, mergeable=True):
    calls.clear()
    d.check_pr = real_check_pr
    pr = {"state": "open", "user": {"login": author}, "head": {"sha": head}, "title": "foo: 1 -> 2",
          "labels": [{"name": "10.rebuild-linux: 1-10"}, {"name": "10.rebuild-darwin: 1-10"}],
          "draft": False, "base": {"ref": "master"}, "mergeable": mergeable, "body": ""}

    def api(path, paginate=False):
        calls.append(path)
        if path.startswith(f"repos/{d.REPO}/actions/runs/"):
            return {"status": "completed", "conclusion": "failure"}
        if path.startswith("repos/NixOS/nixpkgs/pulls/"):
            return pr
        if path.startswith("repos/NixOS/nixpkgs/issues/"):
            return list(comments)
        raise AssertionError(path)

    d.api = api
    d.ci_eval = lambda h: evaluation
    d.reviewed_head = lambda run_id: reviewed
    d.gh = lambda args: calls.append(" ".join(args[:4])) or ""
    d.comparison_rebuilds = lambda h: None
    d.dispatch = lambda n, on_success, systems, extra: calls.append(f"dispatch {n} {on_success} {','.join(systems)} {extra}") or 99


def result_comment(sha):
    return {"body": f"nixpkgs-review-gha\nCommit: [`{sha}`](x)"}


# A: no runner, head unchanged, comment-only: re-run the failed jobs
setup(HEAD_OLD)
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True, "tries": 2}
started, what = d.rerun(5, rec, None)
assert started and rec["reran"] == "no runner" and "tries" not in rec, (started, what, rec)
assert any("rerun-failed-jobs" in c for c in calls), calls

# B: head changed and someone posted a result for it: no duplicate review
setup(HEAD_NEW, comments=[result_comment(HEAD_NEW)])
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["final"] == "not re-run: has a result for the current head", rec
assert not any(c.startswith("dispatch") for c in calls), calls

# C: head changed, CI evaluation still running: postponed, then given up after MAX_TRIES
setup(HEAD_NEW, evaluation="pending")
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["tries"] == 1 and rec["rerun"] == "no runner" and "rerun_after" in rec, rec
rec["tries"] = d.MAX_TRIES
started, what = d.rerun(5, rec, None)
assert not started and rec["final"].startswith("not re-run"), rec

# D: head changed, evaluation expired: fresh comment-only review with --eval local, new head recorded
setup(HEAD_NEW, evaluation="missing")
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True, "reran": "prepare"}
started, what = d.rerun(5, rec, None)
assert started and rec["run_id"] == 99 and rec["head"] == HEAD_NEW and rec["extra_args"] == "--eval local", rec
assert "reran" not in rec and f"dispatch 5 nothing {ALL} --eval local" in calls, (rec, calls)

# E: prepare failure, approve record of another author: check_pr decides, same inputs re-run the jobs
setup(HEAD_OLD, author="someone")
d.check_pr = lambda cand, ignore_own=False: ("ok", None, "foo: 1 -> 2", d.SYSTEMS, "approve", None, HEAD_OLD)
rec = {"rerun": "prepare", "run_id": 1, "on_success": "approve", "darwin": True}
started, what = d.rerun(5, rec, None)
assert started and rec["reran"] == "prepare", (what, rec)

# F: prepare failure whose fresh check now needs --eval local: a new review instead of a re-run
d.check_pr = lambda cand, ignore_own=False: ("ok", None, "foo: 1 -> 2", d.SYSTEMS, "approve", "--eval local", HEAD_OLD)
rec = {"rerun": "prepare", "run_id": 1, "on_success": "approve", "darwin": True}
started, what = d.rerun(5, rec, None)
assert started and rec["run_id"] == 99 and rec["extra_args"] == "--eval local", rec

# G: no runner, head unchanged, comment-only, but someone posted a result for it meanwhile: no re-run
setup(HEAD_OLD, comments=[result_comment(HEAD_OLD)])
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["final"] == "not re-run: has a result for the current head", rec
assert not any("rerun-failed-jobs" in c or c.startswith("dispatch") for c in calls), calls

# H: prepare failure after the head changed, still trivial: the re-run records the new head
setup(HEAD_NEW, author="someone")
d.check_pr = lambda cand, ignore_own=False: ("ok", None, "foo: 1 -> 2", d.SYSTEMS, "approve", None, HEAD_NEW)
rec = {"rerun": "prepare", "run_id": 1, "on_success": "approve", "darwin": True, "head": HEAD_OLD}
started, what = d.rerun(5, rec, None)
assert started and rec["head"] == HEAD_NEW and rec["reran"] == "prepare", rec

# I: GitHub refuses the re-run: final; a request that keeps failing propagates for a later try
def gh_raising(exc):
    def gh(args):
        raise exc("HTTP 422" if exc is d.Refused else "HTTP 502")
    return gh

setup(HEAD_OLD)
d.gh = gh_raising(d.Refused)
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["final"].startswith("re-run refused"), rec
setup(HEAD_OLD)
d.gh = gh_raising(RuntimeError)
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
try:
    d.rerun(5, rec, None)
except d.Refused:
    raise AssertionError("not a refusal")
except RuntimeError:
    pass
else:
    raise AssertionError("transient failure swallowed")
assert "final" not in rec, rec
assert d.postpone(rec, "no runner", "request failed").startswith("re-run postponed") and rec["rerun"] == "no runner"

# J: comment-only, head changed, the pull request now has a merge conflict: check_pr gives up, no dispatch
setup(HEAD_NEW, mergeable=False)
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["final"] == "not re-run: merge conflict", rec
assert not any(c.startswith("dispatch") for c in calls), calls

# K: approve record, no runner, head unchanged, someone posted a result for it meanwhile: no re-run
setup(HEAD_OLD, author="someone", comments=[result_comment(HEAD_OLD)])
rec = {"rerun": "no runner", "run_id": 1, "on_success": "approve", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["final"] == "not re-run: has a result for the current head", rec
assert not any("rerun-failed-jobs" in c or c.startswith("dispatch") for c in calls), calls

# L: the owner's own comment-only re-review, head changed: no check_pr, a fresh review of the latest head
setup(HEAD_NEW, author=d.OWNER)
d.check_pr = lambda cand, ignore_own=False: (_ for _ in ()).throw(AssertionError("check_pr called"))
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert started and rec["run_id"] == 99 and rec["head"] == HEAD_NEW, rec
assert f"dispatch 5 nothing {ALL} None" in calls, calls

# L2: another author's re-review without an approval: no check_pr either, and it keeps its on-success setting
setup(HEAD_OLD, author="someone")
d.check_pr = lambda cand, ignore_own=False: (_ for _ in ()).throw(AssertionError("check_pr called"))
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert started and rec["reran"] == "no runner" and rec["on_success"] == "nothing", (what, rec)

# L3: an r-ryantm review that ran with an approval is checked, and reviewed afresh without one
setup(HEAD_OLD)
rec = {"rerun": "no runner", "run_id": 1, "on_success": "approve", "darwin": True}
started, what = d.rerun(5, rec, None)
assert started and rec["run_id"] == 99 and rec["on_success"] == "nothing", (what, rec)
assert f"dispatch 5 nothing {ALL} None" in calls and any("/pulls/5" in c for c in calls), calls

# M:a scheduled run commits the state file before it starts the next run, a chained one leaves it to the workflow,
# and a scheduled run whose push failed leaves the restart to the schedule
order = []
sys.argv[2:] = ["state.json"]
d.gh = lambda args: order.append(" ".join(args[:2])) or ""
# and a disabled workflow gets no next run at all
for event, pushed, enabled, expected in (
    ("schedule", True, True, ["commit", "workflow run"]),
    ("schedule", False, True, ["commit"]),
    ("workflow_dispatch", False, True, ["workflow run"]),
    ("schedule", True, False, []),
    ("workflow_dispatch", True, False, []),
):
    order.clear()
    d.EVENT = event
    d.workflow_enabled = lambda enabled=enabled: enabled
    d.commit_state = lambda path, pushed=pushed: order.append("commit") or pushed
    d.start_next_run()
    assert order == expected, (event, pushed, order)
# N: check_pr fetches the owner's reviews only when they count
def check_with(ignore_own, reviews):
    setup(HEAD_OLD, author="someone")
    real_api = d.api

    def api(path, paginate=False):
        if "/reviews" in path:
            calls.append(path)
            return reviews
        return real_api(path, paginate)

    d.api = api
    return d.check_pr({"number": 5, "headRefOid": HEAD_OLD}, ignore_own=ignore_own)


owner_review = [{"user": {"login": d.OWNER}}]
assert check_with(False, owner_review)[:2] == ("skipped", "already reviewed by owner")
assert check_with(True, owner_review)[0] == "ok" and not any("/reviews" in c for c in calls), calls
assert check_with(False, [{"user": None}])[0] == "ok"


# N2: a requested re-review skips a head that the owner's own result covers, as after waiting for a fork run
# in flight, but not one whose owner result is for an older head
def check_requeue(sha):
    setup(HEAD_OLD, author="someone", comments=[dict(result_comment(sha), user={"login": d.OWNER})])
    return d.check_pr({"number": 5, "headRefOid": HEAD_OLD}, ignore_own=True)


assert check_requeue(HEAD_OLD)[:2] == ("skipped", "has a result for the current head")
assert check_requeue(HEAD_NEW)[0] == "ok"
assert not any(c.startswith("dispatch") for c in calls), calls
print("rerun paths ok")


# O: approve record, no runner, head unchanged, the pull request turned draft meanwhile: no re-run
setup(HEAD_OLD, author="someone")
real_api = d.api


def api_draft(path, paginate=False):
    pr = real_api(path, paginate)
    if path.startswith("repos/NixOS/nixpkgs/pulls/") and "/reviews" not in path:
        pr = dict(pr, draft=True)
    if "/reviews" in path:
        return []
    return pr


d.api = api_draft
rec = {"rerun": "no runner", "run_id": 1, "on_success": "approve", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["final"] == "not re-run: draft", rec
assert not any("rerun-failed-jobs" in c or c.startswith("dispatch") for c in calls), calls

# P: approve record, no runner, head unchanged, the owner reviewed it by hand meanwhile: no second approval
setup(HEAD_OLD, author="someone")
real_api = d.api
d.api = lambda path, paginate=False: [{"user": {"login": d.OWNER}}] if "/reviews" in path else real_api(path, paginate)
rec = {"rerun": "no runner", "run_id": 1, "on_success": "approve", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["final"] == "not re-run: already reviewed by owner", rec

# Q: approve record, no runner, head unchanged, still qualifies: the failed jobs are re-run
setup(HEAD_OLD, author="someone")
real_api = d.api
d.api = lambda path, paginate=False: [] if "/reviews" in path else real_api(path, paginate)
rec = {"rerun": "no runner", "run_id": 1, "on_success": "approve", "darwin": True}
started, what = d.rerun(5, rec, None)
assert started and rec["reran"] == "no runner" and any("rerun-failed-jobs" in c for c in calls), (what, rec, calls)

# R: no runner, head unchanged, but the CI evaluation expired: a re-run could not evaluate, so a new review
setup(HEAD_OLD, evaluation="missing")
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert started and rec["run_id"] == 99 and rec["extra_args"] == "--eval local", (what, rec)
assert not any("rerun-failed-jobs" in c for c in calls), calls
print("re-run qualification paths ok")

# S: approve record without any known head, prepare failure: the triviality check runs, as on the main path
setup(HEAD_NEW, author="someone")
real_api = d.api
extra = [{"filename": "pkgs/foo/package.nix", "status": "modified", "patch": "@@\n+  doCheck = false;"}]


def api_files(path, paginate=False):
    if "/files" in path:
        calls.append(path)
        return extra
    if "/reviews" in path:
        return []
    return real_api(path, paginate)


d.api = api_files
rec = {"rerun": "prepare", "run_id": 1, "on_success": "approve", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["final"].startswith("not re-run: head changed, now non-trivial"), rec
assert any("/files" in c for c in calls) and not any("rerun-failed-jobs" in c for c in calls), calls

# T: reviewed_head skips a report artifact deleted since it was listed, and an unknown head means None
import importlib  # noqa: E402

fresh = importlib.reload(d)
arts = [{"name": "report_x86_64-linux.json", "expired": False, "archive_download_url": "gone"},
        {"name": "report_aarch64-linux.json", "expired": False, "archive_download_url": "ok"}]
fresh.api = lambda path, paginate=False: {"artifacts": arts}


def gh_artifact(args):
    if args[1] == "gone":
        raise LookupError("HTTP 404: Not Found")
    return '{"head": "' + HEAD_OLD + '"}'


fresh.gh = gh_artifact
assert fresh.reviewed_head(1) == HEAD_OLD
fresh.api = lambda path, paginate=False: {"artifacts": arts[:1]}
assert fresh.reviewed_head(1) is None
print("re-run head paths ok")

# U: a fresh review whose dispatch GitHub answers with Not Found propagates as a request failure, which the caller
# postpones, instead of a lookup failure, which the caller records as final
setup(HEAD_NEW, evaluation="missing")
d.dispatch = lambda n, on_success, systems, extra: (_ for _ in ()).throw(LookupError("HTTP 404: Not Found"))
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
try:
    d.rerun(5, rec, None)
except LookupError:
    raise AssertionError("a dispatch 404 must not make the re-run final")
except RuntimeError as e:
    assert "dispatch failed" in str(e), e
else:
    raise AssertionError("dispatch failure swallowed")
assert "final" not in rec and rec["run_id"] == 1, rec

# V: approve record without a head, prepare failure: the candidate head stands in, so an unchanged head needs no
# triviality check
setup(HEAD_OLD, author="someone")
real_api = d.api
d.api = lambda path, paginate=False: (
    (_ for _ in ()).throw(AssertionError("files fetched")) if "/files" in path
    else [] if "/reviews" in path else real_api(path, paginate)
)
rec = {"rerun": "prepare", "run_id": 1, "on_success": "approve", "darwin": True}
started, what = d.rerun(5, rec, HEAD_OLD)
assert started and rec["reran"] == "prepare" and rec["head"] == HEAD_OLD, (what, rec)
print("re-run dispatch paths ok")

# Systems: a review whose systems changed since it ran is dispatched afresh, and one with the same systems re-run
setup(HEAD_OLD)
d.comparison_rebuilds = lambda h: {"x86_64-linux": ["foo"], "aarch64-darwin": ["foo"]}
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert started and "dispatch 5 nothing x86_64-linux,aarch64-darwin None" in calls, (what, calls)
assert rec["systems"] == ["x86_64-linux", "aarch64-darwin"] and "darwin" not in rec, rec
setup(HEAD_OLD)
d.comparison_rebuilds = lambda h: {"x86_64-linux": ["foo"], "aarch64-darwin": ["foo"]}
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "systems": ["x86_64-linux", "aarch64-darwin"]}
started, what = d.rerun(5, rec, None)
assert started and any("rerun-failed-jobs" in c for c in calls), (what, calls)
# No rebuilds on any reviewed system: no review
setup(HEAD_NEW)
d.comparison_rebuilds = lambda h: {"x86_64-darwin": ["foo"]}
rec = {"rerun": "no runner", "run_id": 1, "on_success": "nothing", "darwin": True}
started, what = d.rerun(5, rec, None)
assert not started and rec["final"] == "not re-run: no rebuilds on the reviewed systems", rec
print("re-run systems paths ok")
