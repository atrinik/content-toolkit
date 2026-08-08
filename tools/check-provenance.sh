#!/usr/bin/env bash
set -euo pipefail

repository=$(git rev-parse --show-toplevel)
cd "${repository}"

jq -e '
  .schema_version == 1
  and .audit.complete_history == true
  and .audit.shallow_repository == false
  and .audit.path_count == 50
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
