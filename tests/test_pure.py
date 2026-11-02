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
print("pure helpers ok")
