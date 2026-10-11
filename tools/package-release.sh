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

# The workspace crates depend on one another by path. Cargo 1.97 verifies each
# package before it has checksums for later workspace packages, which makes a
# workspace-wide offline package operation fail inside Cargo's temporary
# registry. Assemble without Cargo's broken per-package pass, then compile the
# exact staged compatibility set below.
cargo package --locked --offline --allow-dirty --no-verify \
  --package atrinik-artifact --package atrinik-catalog \
  --package atrinik-diagnostics --package atrinik-schema --package atrinik-source \
  --package atrinik-testkit --package atrinik-transaction \
  --package atrinik-content --package atrinik-content-mcp
cp "${target_directory}"/package/*.crate "${output}/crates/"
tools/verify-packages.sh "${output}/crates"

cargo build --locked --release --package atrinik-content --package atrinik-content-mcp
cp "${target_directory}/release/atrinik-content" "${output}/"
cp "${target_directory}/release/atrinik-content-mcp" "${output}/"
install -d "${output}/mcp-schemas"
cp crates/atrinik-content-mcp/schemas/*.json "${output}/mcp-schemas/"
cp crates/atrinik-content-mcp/STDIO.md "${output}/mcp-schemas/"
cp -R crates/atrinik-testkit/fixtures "${output}/"
cp -R crates/atrinik-schema/schemas "${output}/"
cp -R policy schemas "${output}/"
cp LICENSE PROVENANCE.md THIRD_PARTY_NOTICES.md "${output}/"
python3 tools/package-licenses.py "${output}/third-party-licenses"

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

cargo_version=$(cargo metadata --locked --offline --no-deps --format-version 1 | jq -er '[.packages[].version] | unique | if length == 1 then .[0] else error("workspace versions differ") end')
tar -tf "${output}/crates/atrinik-testkit-${cargo_version}.crate" \
  | grep -Fx "atrinik-testkit-${cargo_version}/fixtures/minimal.arc" >/dev/null
tar -tf "${output}/crates/atrinik-schema-${cargo_version}.crate" \
  | grep -Fx \
    "atrinik-schema-${cargo_version}/schemas/foundation-artifact.schema.json" >/dev/null
test -s "${output}/policy/classic-authored-limits.json"
test -s "${output}/schemas/classic-diagnostic.schema.json"

test -s "${output}/atrinik-content-mcp"
test -s "${output}/mcp-schemas/input.schema.json"
test -s "${output}/mcp-schemas/output.schema.json"
test -s "${output}/third-party-licenses/manifest.json"

# Registry preparation binds the exact already-compiled archives before any
# GitHub source release is created. Local aggregate validation remains offline.
crate_prepare_arguments=(prepare --source-tag "v${version}"
  --output "${output}/crates" --archives "${output}/crates")
if [[ $# -lt 2 ]]; then
  crate_prepare_arguments+=(--dry-run)
fi
python3 tools/crate_prepare.py "${crate_prepare_arguments[@]}"
mv "${output}/crates/crate-release.json" "${output}/crate-release.json"
# GitHub release assets form a flat inventory. Preserve authored runtime data
# together in one deterministic archive and publish each crate as an exact file.
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
  -cf - -C "${output}" fixtures mcp-schemas schemas policy third-party-licenses \
  | gzip -n >"${output}/content-assets-${version}.tar.gz"
for archive in "${output}/crates/"*.crate; do
  mv "${archive}" "${output}/"
done
rmdir "${output}/crates"
rm -rf -- "${output}/fixtures" "${output}/mcp-schemas" "${output}/schemas" \
  "${output}/policy" "${output}/third-party-licenses"

checksums=$(mktemp /tmp/atrinik-content-toolkit-checksums.XXXXXX)
trap 'rm -f -- "${checksums}"' EXIT
python3 tools/release-checksums.py "${output}" >"${checksums}"
mv "${checksums}" "${output}/SHA256SUMS"
