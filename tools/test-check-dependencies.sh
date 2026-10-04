#!/usr/bin/env bash
set -euo pipefail

repository=$(git rev-parse --show-toplevel)
cd "${repository}"

temporary=$(mktemp -d /tmp/atrinik-content-toolkit-dependency-tests.XXXXXX)
trap 'rm -rf -- "${temporary}"' EXIT

cat >"${temporary}/cargo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cat "${MOCK_CARGO_METADATA:?}"
EOF
chmod +x "${temporary}/cargo"

write_metadata() {
  local destination=$1
  local dependency_name=$2
  local dependency_version=$3
  local dependency_license=$4
  local dependency_source=$5

  jq -n \
    --arg name "${dependency_name}" \
    --arg version "${dependency_version}" \
    --arg license "${dependency_license}" \
    --arg source "${dependency_source}" '
    {
      packages: [
        {
          name: "atrinik-content",
          version: "0.1.0",
          license: "MIT",
          source: null
        },
        {
          name: $name,
          version: $version,
          license: $license,
          source: $source
        }
      ]
    }
  ' >"${destination}"
}

run_check() {
  PATH="${temporary}:${PATH}" MOCK_CARGO_METADATA=$1 tools/check-dependencies.sh
}

expect_rejection() {
  local fixture=$1
  local description=$2

  if run_check "${fixture}"; then
    printf 'dependency check unexpectedly accepted %s\n' "${description}" >&2
    return 1
  fi
}

compatible="${temporary}/compatible.json"
write_metadata \
  "${compatible}" \
  sha2 \
  0.11.42 \
  'MIT OR Apache-2.0' \
  'registry+https://github.com/rust-lang/crates.io-index'
run_check "${compatible}"

unlisted_dependency="${temporary}/unlisted-dependency.json"
write_metadata \
  "${unlisted_dependency}" \
  new-mit-crate \
  2.0.0 \
  MIT \
  'registry+https://github.com/rust-lang/crates.io-index'
run_check "${unlisted_dependency}"

missing_license="${temporary}/missing-license.json"
write_metadata \
  "${missing_license}" \
  undocumented-crate \
  1.2.3 \
  '' \
  'registry+https://github.com/rust-lang/crates.io-index'
expect_rejection "${missing_license}" 'a dependency without license metadata'

disallowed_license="${temporary}/disallowed-license.json"
write_metadata \
  "${disallowed_license}" \
  external-crate \
  1.2.3 \
  'GPL-3.0-only' \
  'registry+https://github.com/rust-lang/crates.io-index'
expect_rejection "${disallowed_license}" 'a forbidden license'

forbidden_source="${temporary}/forbidden-source.json"
write_metadata \
  "${forbidden_source}" \
  internal-classic-code \
  1.2.3 \
  MIT \
  'git+https://github.com/atrinik/classic?rev=0123456789abcdef'
expect_rejection "${forbidden_source}" 'a forbidden Atrinik source repository'

forbidden_package="${temporary}/forbidden-package.json"
write_metadata \
  "${forbidden_package}" \
  tokio \
  1.2.3 \
  MIT \
  'registry+https://github.com/rust-lang/crates.io-index'
expect_rejection "${forbidden_package}" 'a forbidden package'
