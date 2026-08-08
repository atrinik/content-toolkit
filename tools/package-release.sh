#!/usr/bin/env bash
set -euo pipefail

repository=$(git rev-parse --show-toplevel)
cd "${repository}"

output=${1:-dist}
version=${2:-$(git describe --tags --always --dirty)}
if [[ ! ${version} =~ ^[0-9A-Za-z][0-9A-Za-z.+-]*$ ]]; then
  echo "invalid release version: ${version}" >&2
  exit 2
fi
if [[ -e ${output} ]]; then
  echo "release output already exists: ${output}" >&2
  exit 1
fi
install -d "${output}/crates"
target_directory=$(cargo metadata --locked --offline --no-deps --format-version 1 \
  | jq -r '.target_directory')

source_archive="atrinik-content-toolkit-${version}.tar.gz"
git archive --format=tar --prefix="atrinik-content-toolkit-${version}/" HEAD \
  | gzip -n >"${output}/${source_archive}"

cargo package --locked --offline --workspace --allow-dirty --no-verify
cp "${target_directory}"/package/*.crate "${output}/crates/"
tools/verify-packages.sh "${output}/crates"
cargo build --locked --release --package atrinik-content
cp "${target_directory}/release/atrinik-content" "${output}/"
cp -R crates/atrinik-testkit/fixtures "${output}/"
cp -R crates/atrinik-schema/schemas "${output}/"
cp -R policy schemas "${output}/"
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

tar -tf "${output}/crates/atrinik-testkit-0.1.0.crate" \
  | grep -Fx 'atrinik-testkit-0.1.0/fixtures/minimal.arc' >/dev/null
tar -tf "${output}/crates/atrinik-schema-0.1.0.crate" \
  | grep -Fx \
    'atrinik-schema-0.1.0/schemas/foundation-artifact.schema.json' >/dev/null
test -s "${output}/policy/classic-authored-limits.json"
test -s "${output}/schemas/classic-diagnostic.schema.json"
