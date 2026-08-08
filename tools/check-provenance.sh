#!/usr/bin/env bash
set -euo pipefail

repository=$(git rev-parse --show-toplevel)
cd "${repository}"

jq -e '
  .schema_version == 1
  and .audit.complete_history == true
  and .audit.shallow_repository == false
  and .audit.path_count == 50
  and .audit.touch_count == 97
  and .audit.root_registry_revision == "d64a8e958ca2adad783ad8912493d468a805f3fd"
  and ([.records[].source_paths[]] | length == 50)
  and ([.records[].source_paths[]] | unique | length == 50)
  and any(.records[]; .id == "lossless-core-model" and .decision == "admitted")
  and all(.records[];
    (.decision == "admitted" or .decision == "excluded")
    and (.source_paths | length > 0)
    and (.history_evidence | length > 0)
    and (.method | length > 0))
  and all(.records[] | select(.decision == "admitted");
    .grantor == "Zoey Rose"
    and .destination_license == "MIT"
    and ((.source_blob_ids | length) == (.source_paths | length))
    and (.destination_paths | length > 0)
    and (.identity_evidence | length > 0)
    and (.third_party_review | length > 0))
' provenance/reuse.json >/dev/null

rg -F 'provenance/reuse.json#lossless-core-model' \
  crates/atrinik-source/src/lib.rs >/dev/null

jq -e '
  .schema_version == 1
  and .source_repository == "atrinik/content"
  and .source_branch == "main"
  and .source_revision == "01b1fdb65c2243df4bafe9c8109fc93229df0121"
  and .classic_copy.branch == "1.x"
  and .classic_copy.revision == .source_revision
  and .classic_copy.terms_changed == false
  and .grant_registry.revision == "d64a8e958ca2adad783ad8912493d468a805f3fd"
  and (.records | length == 2)
  and all(.records[];
    .decision == "admitted"
    and .grantor == "Zoey Rose"
    and .destination_license == "MIT"
    and (.complete_history | length == 1)
    and .identity_evidence.github_login == "zoeyrose"
    and .identity_evidence.github_user_id == 3865595
    and .identity_evidence.signature_verified == true
    and (.originality_review | length > 0)
    and (.transformation | length > 0)
    and (.attribution | length > 0))
' provenance/linked-content-materials.json >/dev/null

while IFS=$'\t' read -r path expected; do
  test "$(sha256sum "${path}" | cut -d' ' -f1)" = "${expected}"
done < <(jq -r '.records[] | [.destination_path, .destination_sha256] | @tsv' \
  provenance/linked-content-materials.json)

rg -F 'historical MIT provenance grant' THIRD_PARTY_NOTICES.md >/dev/null
