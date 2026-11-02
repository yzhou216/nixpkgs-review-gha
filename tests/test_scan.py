"""Check failure_kind, scan_failures and ci_eval against stubbed GitHub calls, under CPython."""

import os
import sys

os.environ.update(REPO="yzhou216/nixpkgs-review-gha", OWNER="yzhou216", DRY_RUN="true")
sys.path.insert(0, sys.argv[1])
import dispatch as d  # noqa: E402

d.gh = lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh must not be called"))
NOT_ACQ = "The job was not acquired by Runner of type hosted even after multiple attempts"


def stub_jobs(failed_names):
    jobs = [{"id": i, "name": name, "conclusion": "failure", "completed_at": "2026-11-02T10:00:00Z"}
            for i, name in enumerate(failed_names)]

    def api(path, paginate=False, key=None):
        if path.endswith("/jobs?per_page=50"):
            return {"jobs": jobs + [{"id": 99, "name": "prepare", "conclusion": "success", "completed_at": None}]}
        if "/check-runs/" in path:
            return [{"annotation_level": "failure", "message": NOT_ACQ}]
        if "/actions/workflows/review.yml/runs" in path:
            assert paginate and key == "workflow_runs" and "status=failure" in path, (path, paginate, key)
            return [{"id": 7, "run_attempt": 1}]
        raise AssertionError(path)

    d.api = api


stub_jobs(["review (aarch64-linux)"])
kind, when, reason, mac = d.failure_kind(7)
assert kind == "no runner" and mac is False, (kind, mac)
stub_jobs(["review (aarch64-darwin)"])
assert d.failure_kind(7)[0] == "no runner" and d.failure_kind(7)[3] is True

# A Linux arm runner shortage schedules a re-run but does not constrain the Darwin cap
stub_jobs(["review (aarch64-linux)"])
records = {"5": {"status": "dispatched", "run_id": 7, "title": "x"}}
assert d.scan_failures(records, "2026-11-01") == 0.0 and records["5"]["rerun"] == "no runner"
assert "no_runner_at" not in records["5"], records
stub_jobs(["review (aarch64-darwin)"])
records = {"5": {"status": "dispatched", "run_id": 7, "title": "x"}}
assert d.scan_failures(records, "2026-11-01") > 0 and records["5"]["no_runner_at"]


# ci_eval: a run deleted between the listing and its artifacts request holds no evaluation
def api_eval(path, paginate=False):
    if "actions/runs?head_sha=" in path:
        return {"workflow_runs": [{"id": 1, "name": "PR", "status": "completed"},
                                  {"id": 2, "name": "PR", "status": "completed"}]}
    if "/runs/1/artifacts" in path:
        raise LookupError("HTTP 404")
    if "/runs/2/artifacts" in path:
        return {"artifacts": [{"name": "comparison", "expired": False}]}
    raise AssertionError(path)


d.api = api_eval
assert d.ci_eval("a" * 40) == "available"

# api: pages that are lists are joined, and pages that are objects give the items under the key
import importlib  # noqa: E402

d = importlib.reload(d)
slurped = {}
d.gh = lambda args, tries=6: slurped.setdefault("args", args) and slurped["out"]
slurped["out"] = '[[{"a": 1}], [{"a": 2}]]'
assert d.api("x", paginate=True) == [{"a": 1}, {"a": 2}] and slurped["args"] == ["api", "--paginate", "--slurp", "x"]
slurped["out"] = '[{"total_count": 3, "workflow_runs": [{"id": 1}, {"id": 2}]}, {"workflow_runs": [{"id": 3}]}]'
assert [r["id"] for r in d.api("x", paginate=True, key="workflow_runs")] == [1, 2, 3]
print("runner shortage and evaluation paths ok")
