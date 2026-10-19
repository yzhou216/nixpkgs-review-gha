# batch-review

Dispatcher and state for the scheduled `batch-review` workflow on `main`.

`candidates.json` lists the open nixpkgs pull requests that were trivial version bumps on 2026-10-19.  Each one was created after 2026-04-19, targets master, changes only version, rev, tag and hash values (plus generated lock files), has at most 100 rebuilds, and had no nixpkgs-review-gha result.

Every 15 minutes the workflow runs `dispatch.py`.  It dispatches `review.yml` for the next candidates, newest first, on x86_64-linux, aarch64-linux and aarch64-darwin (sandbox = true) with on-success = approve.  It keeps at most 8 batch reviews and 12 review runs in total in flight, and checks each pull request again just before dispatching it.  `state.json` records every dispatched or skipped pull request, and the workflow commits it back to this branch after each run.

To pause or stop the batch, disable the workflow with `gh workflow disable batch-review.yml -R yzhou216/nixpkgs-review-gha`.  The dispatcher disables it on its own once every candidate has been handled.
