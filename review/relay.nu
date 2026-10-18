# Hand-off of unfinished reviews between jobs
#
# GitHub-hosted runners stop a job after six hours.  If nixpkgs-review is still
# running shortly before that, the review job exports every store path that it
# built itself, and the workflow uploads the export as an artifact.  The next
# leg imports those paths before it runs nixpkgs-review again, so builds that
# already finished are not repeated.

# Store paths that only exist on this runner and that builds since `since`
# need, dependencies first
def local-closure [since: int] {
  let store = nix path-info --all --json --json-format 2 | from json
  let local = $store.info
  | transpose name info
  | where { $in.info.ultimate and not ($in.name | str ends-with ".drv") }
  | insert path {|row| $store.storeDir | path join $row.name }
  let outputs = $local
  | where { $in.info.deriver != null and $in.info.registrationTime >= $since }
  | get path
  if ($outputs | is-empty) { return [] }

  nix-store --query --requisites ...$outputs
  | lines
  | enumerate
  | rename order path
  | join ($local | select path) path
  | sort-by order
  | get path
}

# Export the store paths built on this runner since `since` to `dir` for the
# given leg
export def "relay save" [dir: path, leg: int, since: int] {
  mkdir $dir
  let paths = local-closure $since
  let nar = $dir | path join $"relay-($leg).nar"
  let refs = $dir | path join $"relay-($leg).refs"
  print $"exporting ($paths | length) store paths:" ...$paths

  if ($paths | is-empty) {
    touch $nar $refs
    return
  }

  # References that the next runner has to substitute before the import
  let info = nix path-info --json --json-format 2 ...$paths | from json
  $info.info
  | values
  | get references
  | flatten
  | uniq
  | each {|ref| $info.storeDir | path join $ref }
  | wrap path
  | join --left ($paths | wrap path | insert exported true) path
  | where exported == null
  | get path
  | str join "\n"
  | save -f $refs

  nix-store --export ...$paths | save -f $nar
  ls $nar | select name size | print
}

# Import the store paths that earlier legs exported to `dir`
export def "relay load" [dir: path] {
  glob ($dir | path join "relay-*.nar")
  | sort-by { path basename | parse "relay-{leg}.nar" | get 0.leg | into int }
  | each {|nar|
    let refs = $nar | str replace -r '\.nar$' '.refs' | open --raw | lines | where { is-not-empty }
    print $"importing ($nar | path basename) after fetching ($refs | length) references"
    if ($refs | is-not-empty) { nix-store --realise ...$refs | ignore }
    open --raw $nar | nix-store --import | lines | length | print $"imported ($in) store paths"
  }
  | ignore
}
