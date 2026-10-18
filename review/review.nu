use gha.nu *
use relay.nu *

let inputs = gha review-inputs
let pushToAttic = $inputs.push-to-cache and $env.ATTIC_SERVER != '' and $env.ATTIC_CACHE != ''
let pushToCachix = $inputs.push-to-cache and $env.CACHIX_CACHE != ''
let pr = $env.PR_JSON | from json
let head = $pr.head.sha
let base = $pr.base.sha
let merge = $pr.merge_commit_sha
let jobsArg = if $inputs.builders == "remote" { "-j0" } else { "" }
let system = nix config show system
let buildArgs = --build-args=($"-L ($jobsArg) ($inputs.extra-build-args)")
let relayLeg = $env.RELAY_LEG | into int
# The last leg has no successor, so it keeps the job's full time limit
let relayDeadline = if $inputs.relay-after > 0 and $env.RELAY_LAST_LEG != "true" {
  ($env.JOB_STARTED_AT | into int) + $inputs.relay-after * 60
}

if $inputs.allow-unfree { $env.NIXPKGS_ALLOW_UNFREE = 1 }
if $inputs.allow-insecure { $env.NIXPKGS_ALLOW_INSECURE = 1 }

let installSystem = match $system {
  "x86_64-darwin" => "aarch64-darwin"
  _ => $system
}

gha group "install packages" {
  [ nixpkgs-review ]
  | if $pushToAttic { $in ++ [ attic-client ] } else { }
  | if $pushToCachix { $in ++ [ cachix ] } else { }
  | each { $".#($in)" }
  | nix profile add --system $installSystem ...$in --builders ''
}

if ("relay-in" | path exists) {
  gha group "import builds from earlier legs" {
    try {
      relay load ("relay-in" | path expand)
    } catch {|err|
      gha warning $"Could not import builds from earlier legs, rebuilding them instead: ($err.msg)"
    }
  }
}

let review = [
  nixpkgs-review "--" pr ($inputs.pr | into string)
  --no-shell
  --no-exit-status
  --no-headers
  --print-result
  $buildArgs
  --pr-json=($env.PR_JSON)
  ...$inputs.extra-args
]

# GNU timeout, which macOS does not ship
let timeout = if $relayDeadline != null {
  nix build --system $installSystem --no-link --print-out-paths .#coreutils
  | lines
  | first
  | path join bin timeout
}

let reviewStartedAt = (date now | into int) // 1_000_000_000

gha group $"run nixpkgs-review ($inputs.extra-args-raw) ($buildArgs)" {
  cd nixpkgs
  if $relayDeadline == null {
    run-external ...$review
    return 0
  }

  let remaining = [($relayDeadline - (date now | into int) // 1_000_000_000) 1] | math max
  print $"handing off to leg ($relayLeg + 1) if nixpkgs-review is still running in ($remaining)s"
  try {
    run-external $timeout "--signal=INT" "--kill-after=5m" $"($remaining)s" ...$review
    0
  } catch {|err|
    $err.exit_code? | default 1
  }
} | let reviewExitCode

# timeout exits with 124, or 137 if nixpkgs-review ignored the interrupt
if $reviewExitCode in [124 137] {
  gha group $"hand off to leg ($relayLeg + 1)" {
    relay save ("relay-out" | path expand) $relayLeg $reviewStartedAt
  }
  "true" | gha output relay
  exit
}
if $reviewExitCode != 0 { exit $reviewExitCode }

let reviewDir = $"~/.cache/nixpkgs-review/pr-($inputs.pr)" | path expand
let reportJson = $"($reviewDir)/report.json"

if $pushToAttic or $pushToCachix {
  gha group "push results to cache" {
    let paths = glob $"($reviewDir)/results/*" | path expand
    if ($paths | is-empty) { return }

    let cache = if $pushToAttic {
      attic login default $env.ATTIC_SERVER $env.ATTIC_TOKEN
      try {
        attic cache info $env.ATTIC_CACHE
      } catch {
        gha error "attic returned an error"
        return
      }
      $paths | str join "\n" | attic push --stdin $env.ATTIC_CACHE
      http get -H { Authorization: $"Bearer ($env.ATTIC_TOKEN)" } $"($env.ATTIC_SERVER)_api/v1/cache-config/($env.ATTIC_CACHE)"
      | select substituter_endpoint public_key is_public
    } else if $pushToCachix {
      with-env { CACHIX_SIGNING_KEY: ($env.CACHIX_SIGNING_KEY | default -e null) } {
        $paths | str join "\n" | cachix push $env.CACHIX_CACHE
        http get -H { Authorization: $"Bearer ($env.ATTIC_TOKEN)" } $"https://app.cachix.org/api/v1/cache/($env.CACHIX_CACHE)"
        | select uri publicSigningKeys isPublic
        | update publicSigningKeys { first }
        | rename substituter_endpoint public_key is_public
      }
    }

    if not $cache.is_public { return }

    $cache | to json | print

    let binaryCaches = [
      "https://cache.nixos.org/"
      $cache.substituter_endpoint
    ]
    let publicKeys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      $cache.public_key
    ]

    [
      $"nix-store -r"
      $"--option binary-caches '($binaryCaches | str join ' ')'"
      $"--option trusted-public-keys '($publicKeys | each { $'(char lf)    ($in)' } | str join)\n  '"
      ...$paths
    ]
    | str join " \\\n  "
    | let fetchCmd
    | print
    $fetchCmd
  }
} | let fetchCmd

open $reportJson
| reject systems
| insert system $system
| insert fetchCmd $fetchCmd
| insert nixConfig { sandbox: (nix config show sandbox) }
| insert head $head
| insert base $base
| insert merge $merge
| update result {
  get -o $system
  | default {}
  | upsert non-existent { default [] }
  | rename -c { non-existent: non_existent }
  | upsert broken { default [] }
  | upsert non_existent { default [] }
  | upsert blacklisted { default [] }
  | upsert failed { default [] }
  | upsert still_failing { default [] }
  | upsert tests { default [] }
  | upsert built { default [] }
  | upsert unsupported { default [] }
}
| let report
| get result
| let result

if $env.IDENTIFY_UNSUPPORTED_PACKAGES != '0' {
  gha group "identify unsupported packages" {
    cd nixpkgs
    if ($result.failed | is-empty) { return $result }

    git fetch origin $merge
    git switch -d $merge

    nix config show --json | save -rf ../nix-config.json
    nix derivation show --recursive -f. ...$result.failed.name | save -rf ../drv-graph.json
    let buildSupport = nix-instantiate ...[
      --eval --strict --json ("../drv-build-support.nix" | path expand)
      --argstr configPath ("../nix-config.json" | path expand)
      --argstr drvGraphPath ("../drv-graph.json" | path expand)
    ] | from json

    $result.failed
    | insert drv { nix eval -f. $"($in.name).drvPath" --raw | path basename }
    | where {|pkg| $buildSupport | get $pkg.drv | not $in.supported }
    | select name aliases
    | let unsupported
    | get name
    | let unsupportedNames

    $result
    | update failed { where name not-in $unsupportedNames }
    | update unsupported { append $unsupported }
  }
} else { $result } | let result
let report = $report | update result $result

if $env.IDENTIFY_STILL_FAILING_PACKAGES == '1' {
  gha group "identify still failing packages" {
    cd nixpkgs
    if ($result.failed | is-empty) { return $result }

    git fetch origin $base
    git switch -d $base

    $result.failed
    | insert path { try { nix eval -f. $"($in.name).outPath" --raw } }
    | where path != null
    | let candidates

    if ($candidates | is-not-empty) {
      try { nix build --keep-going -L ...([$jobsArg] | compact -e) -f. ...$candidates.name }
    }

    $candidates
    | where { nix store verify --no-contents --no-trust $in.path | complete | $in.exit_code != 0 }
    | select name aliases
    | let stillFailing
    | get name
    | let stillFailingNames

    $result
    | update failed { where name not-in $stillFailingNames }
    | update still_failing { append $stillFailing }
  }
} else { $result } | let result
let report = $report | update result $result

gha group "report" {
  $report | save -f report_($system).json
  $report | to json | print
}
