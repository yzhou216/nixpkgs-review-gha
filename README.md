# batch-review

Dispatcher and state for the `batch-review` workflow on `main`.

`candidates.json` lists the open nixpkgs pull requests that were trivial version bumps on 2026-10-19.  Each one was created after 2026-04-19, targets master, changes only version, rev, tag and hash values (plus generated lock files), has at most 100 rebuilds, and had no nixpkgs-review-gha result.

Each workflow run executes `dispatch.py` for up to about five hours.  It dispatches `review.yml` for the next candidates, newest first, on x86_64-linux, aarch64-linux and aarch64-darwin (sandbox = true) with on-success = approve.  It keeps at most 8 batch reviews and 12 review runs in total in flight, and checks each pull request again just before dispatching it.  `state.json` records every dispatched or skipped pull request and is committed back to this branch every 30 minutes and at the end of the run.  When a run ends, it starts the next one, and an hourly schedule restarts the chain if it ever breaks.

To pause, run `gh workflow disable batch-review.yml -R yzhou216/nixpkgs-review-gha`.  The running dispatcher stops within about two minutes and does not start another run.  To resume, run `gh workflow enable batch-review.yml -R yzhou216/nixpkgs-review-gha` followed by `gh workflow run batch-review.yml -R yzhou216/nixpkgs-review-gha`.  The dispatcher disables the workflow itself once every candidate has been handled.
