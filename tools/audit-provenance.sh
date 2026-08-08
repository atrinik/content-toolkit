#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 || $1 != --source ]]; then
  echo "usage: tools/audit-provenance.sh --source /absolute/content/checkout" >&2
  exit 2
fi

repository=$(git rev-parse --show-toplevel)
manifest="${repository}/provenance/reuse.json"
source_repository=$(realpath -- "$2")
audit_revision=$(jq -r '.audit.source_revision' "${manifest}")

test "$(git -C "${source_repository}" rev-parse --is-shallow-repository)" = false
git -C "${source_repository}" cat-file -e "${audit_revision}^{commit}"

temporary=$(mktemp -d /tmp/atrinik-content-provenance.XXXXXX)
trap 'rm -rf -- "${temporary}"' EXIT

git -C "${source_repository}" ls-tree -r --name-only \
  "${audit_revision}" tools | sort >"${temporary}/actual-paths"
jq -r '[.records[].source_paths[]] | sort[]' "${manifest}" \
  >"${temporary}/expected-paths"
cmp "${temporary}/expected-paths" "${temporary}/actual-paths"

test "$(git -C "${source_repository}" log --all --format='%H' -- tools | wc -l)" \
  -eq "$(jq -r '.audit.touch_count' "${manifest}")"
git -C "${source_repository}" log --all \
  --format='%an <%ae>%x09%cn <%ce>' -- tools | sort -u \
  >"${temporary}/actual-identities"
jq -r '.audit.author_committer_identities[]' "${manifest}" | sort \
  >"${temporary}/expected-identities"
cmp "${temporary}/expected-identities" "${temporary}/actual-identities"

while IFS=$'\t' read -r path blob; do
  test "$(git -C "${source_repository}" rev-parse "${audit_revision}:${path}")" = "${blob}"
  case "${path}" in
    tools/content_core/*)
      introduction=96073eeff1854fc29347fdafd32e622394f24c07
      ;;
    tools/syntax_evaluation/*)
      introduction=4aa4aebc5c88dffdf57657a34ae20306a57fbebd
      ;;
    *)
      echo "unexpected admitted source path: ${path}" >&2
      exit 1
      ;;
  esac
  history=$(git -C "${source_repository}" log --follow \
    --format='%H%x09%an <%ae>' "${audit_revision}" -- "${path}")
  test "${history}" = "${introduction}"$'\t'\
'Zoey Rose <3865595+zoeyrose@users.noreply.github.com>'
done < <(jq -r '
  .records[] | select(.decision == "admitted")
  | .source_paths[] as $path | [$path, .source_blob_ids[$path]] | @tsv
' "${manifest}")

if ! git -C "${source_repository}" diff --quiet \
  "${audit_revision}" HEAD -- tools; then
  echo "content tools changed after the audited revision; re-audit before reuse" >&2
  git -C "${source_repository}" diff --name-only \
    "${audit_revision}" HEAD -- tools >&2
  exit 1
fi

echo "provenance audit evidence matches ${audit_revision}"
