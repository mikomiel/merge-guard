#!/usr/bin/env bash
set -euo pipefail

if [ "$(git rev-parse --is-shallow-repository)" != "false" ]; then
  printf 'Public-content scanning requires a full Git clone.\n' >&2
  exit 2
fi

git rev-list --objects --no-object-names HEAD |
  git cat-file --batch-check='%(objecttype) %(objectname)' |
  while read -r kind oid; do
    case "$kind" in
      blob)
        git cat-file blob "$oid"
        printf '\n'
        ;;
      commit|tree|tag) ;;
      *)
        printf 'Unable to read a reachable Git object.\n' >&2
        exit 2
        ;;
    esac
  done
