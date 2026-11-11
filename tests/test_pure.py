"""Checks of the dispatcher's pure helpers, run with CPython."""

import os
import sys

os.environ.update(REPO="yzhou216/nixpkgs-review-gha", OWNER="yzhou216", DRY_RUN="true")
sys.path.insert(0, sys.argv[1])
import dispatch as d  # noqa: E402

sha = "a" * 40
body = f"## :x: `nixpkgs-review` result\n\nGenerated using [`nixpkgs-review-gha`](x)\n\nCommand: `nixpkgs-review pr 1`\nCommit: [`{sha}`](https://x)\n"
assert d.reviewed_heads([{"body": body}]) == {sha}
assert d.reviewed_heads([{"body": body.replace("nixpkgs-review-gha", "nixpkgs-review")}]) == set()
assert d.reviewed_heads([{"body": None}]) == set()
assert d.bucket(["10.rebuild-darwin: 1", "10.rebuild-darwin: 1-10"], "10.rebuild-darwin") == "1-10"
assert d.bucket(["10.rebuild-darwin: 0"], "10.rebuild-darwin") == "0"
assert d.bucket([], "10.rebuild-darwin") is None
# Several big buckets during a relabel: the largest one, whatever the set order
assert d.bucket(["10.rebuild-linux: 11-100", "10.rebuild-linux: 101-500"], "10.rebuild-linux") == "101-500"
assert d.bucket(["10.rebuild-linux: 101-500", "10.rebuild-linux: 11-100"], "10.rebuild-linux") == "101-500"
assert d.bucket(["10.rebuild-linux: 5001+", "10.rebuild-linux: stdenv"], "10.rebuild-linux") == "5001+"
assert d.needs_darwin({"labels": [{"name": "10.rebuild-darwin: 0"}]}) is False
assert d.needs_darwin({"labels": []}) is True
assert d.not_dispatched("skipped", "x", "t") == ("skipped", "x", "t", None, None, None, None)
files = [{"filename": "pkgs/by-name/fo/foo/package.nix", "status": "modified",
          "patch": '@@\n-  version = "1.0";\n+  version = "1.1";\n-  hash = "sha256-' + "A" * 43 + '=";\n+  hash = "sha256-' + "B" * 43 + '=";'}]
assert d.classify("foo: 1.0 -> 1.1", files) == [], d.classify("foo: 1.0 -> 1.1", files)
files[0]["patch"] += "\n+  doCheck = false;"
assert d.classify("foo: 1.0 -> 1.1", files), "an extra line must make the bump non-trivial"
# review_systems: the CI comparison picks the systems, and without it the labels pick Linux and Darwin
pr = {"head": {"sha": sha}, "labels": [{"name": "10.rebuild-linux: 1-10"}, {"name": "10.rebuild-darwin: 0"}]}
d.comparison_rebuilds = lambda h: {"x86_64-linux": ["foo"], "aarch64-linux": [], "aarch64-darwin": ["foo"], "x86_64-darwin": ["foo"]}
assert d.review_systems(pr) == ("x86_64-linux", "aarch64-darwin"), d.review_systems(pr)
d.comparison_rebuilds = lambda h: {"x86_64-darwin": ["foo"]}
assert d.review_systems(pr) == ()
d.comparison_rebuilds = lambda h: None
assert d.review_systems(pr) == d.LINUX
pr["labels"] = [{"name": "10.rebuild-linux: 0"}, {"name": "10.rebuild-darwin: 1"}]
assert d.review_systems(pr) == ("aarch64-darwin",)
# record_systems: records from before systems were stored go by their darwin flag
assert d.record_systems({}) == d.SYSTEMS and d.record_systems({"darwin": False}) == d.LINUX
assert d.record_systems({"systems": ["aarch64-linux"], "darwin": True}) == ("aarch64-linux",)
# dispatch: every system outside the tuple is switched off
sent = []
d.gh = lambda args, tries=6: sent.append(args) or "https://github.com/x/actions/runs/12"
assert d.dispatch(5, "nothing", ("aarch64-linux",)) == 12
inputs = dict(a.split("=", 1) for a in sent[0][sent[0].index("-f") + 1 :: 2])
assert inputs["x86_64-linux"] == "false" and inputs["aarch64-linux"] == "true", inputs
assert inputs["aarch64-darwin"] == "no" and inputs["x86_64-darwin"] == "no" and inputs["on-success"] == "nothing", inputs
d.dispatch(5, "nothing", d.SYSTEMS)
inputs = dict(a.split("=", 1) for a in sent[1][sent[1].index("-f") + 1 :: 2])
assert inputs["x86_64-linux"] == inputs["aarch64-linux"] == "true" and inputs["aarch64-darwin"] == "yes_sandbox_true", inputs
print("pure helpers ok")
