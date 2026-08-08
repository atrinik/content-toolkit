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

test "$(git -C "${source_repository}" log "${audit_revision}" --format='%H' -- tools | wc -l)" \
  -eq "$(jq -r '.audit.touch_count' "${manifest}")"
git -C "${source_repository}" log "${audit_revision}" \
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

linked_manifest=${repository}/provenance/linked-content-materials.json
jq -r '.records[] |
  [.source_path, .source_blob_id, .complete_history[0].revision,
   .complete_history[0].author, .destination_path, .source_sha256] | @tsv' \
  "${linked_manifest}" >"${temporary}/linked-materials"
while IFS=$'\t' read -r path blob revision author destination expected_sha; do
  test "$(git -C "${source_repository}" rev-parse "${audit_revision}:${path}")" = "${blob}"
  history=$(git -C "${source_repository}" log --follow \
    --format='%H%x09%an <%ae>' "${audit_revision}" -- "${path}")
  test "${history}" = "${revision}"$'\t'"${author}"
  test "$(git -C "${source_repository}" show "${audit_revision}:${path}" | sha256sum | cut -d' ' -f1)" \
    = "${expected_sha}"
  test "$(sha256sum "${repository}/${destination}" | cut -d' ' -f1)" = "${expected_sha}"
  if ! git -C "${source_repository}" diff --quiet \
    "${audit_revision}" HEAD -- "${path}"; then
    echo "linked source changed after audit: ${path}" >&2
    exit 1
  fi
done <"${temporary}/linked-materials"

if ! git -C "${source_repository}" diff --quiet \
  "${audit_revision}" HEAD -- tools; then
  echo "content tools changed after the audited revision; re-audit before reuse" >&2
  git -C "${source_repository}" diff --name-only \
    "${audit_revision}" HEAD -- tools >&2
  exit 1
fi

echo "provenance audit evidence matches ${audit_revision}"
