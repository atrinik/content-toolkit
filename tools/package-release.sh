#!/usr/bin/env bash
set -euo pipefail

repository=$(git rev-parse --show-toplevel)
cd "${repository}"

output=${1:-dist}
if [[ -e ${output} ]]; then
  echo "release output already exists: ${output}" >&2
  exit 1
fi
install -d "${output}/crates"
target_directory=$(cargo metadata --locked --offline --no-deps --format-version 1 \
  | jq -r '.target_directory')

version=$(git describe --tags --always --dirty)
source_archive="atrinik-content-toolkit-${version}.tar.gz"
git archive --format=tar --prefix="atrinik-content-toolkit-${version}/" HEAD \
  | gzip -n >"${output}/${source_archive}"

cargo package --locked --offline --workspace --allow-dirty --no-verify
cp "${target_directory}"/package/*.crate "${output}/crates/"
cargo build --locked --release --package atrinik-content
cp "${target_directory}/release/atrinik-content" "${output}/"
cp -R fixtures schemas "${output}/"
cp LICENSE PROVENANCE.md THIRD_PARTY_NOTICES.md "${output}/"

SYFT_CHECK_FOR_APP_UPDATE=false syft dir:. \
  --source-name atrinik-content-toolkit --source-version "${version}" \
  --output "cyclonedx-json=${output}/sbom.cdx.json"

jq -n \
  --arg version "${version}" \
  --arg revision "$(git rev-parse HEAD)" \
  --arg rust "$(rustc --version)" \
  '{schema_version: 1, version: $version, revision: $revision,
    tools: {rust: $rust}, headless: true}' \
  >"${output}/provenance.json"

checksums=$(mktemp /tmp/atrinik-content-toolkit-checksums.XXXXXX)
trap 'rm -f -- "${checksums}"' EXIT
(
  cd "${output}"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum
) >"${checksums}"
mv "${checksums}" "${output}/SHA256SUMS"
