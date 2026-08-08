#!/usr/bin/env bash
set -euo pipefail

repository=$(git rev-parse --show-toplevel)
cd "${repository}"

jq -e '
  .schema_version == 1
  and (.direct_dependencies | length == 1)
  and all(.direct_dependencies[];
    [.ecosystem, .name, .version, .license, .source, .owner,
      .review_cadence, .eol_response, .validation] | all(. != ""))
' policy/dependencies.json >/dev/null
jq -e '
  .schema_version == 1
  and .rust == "1.97.1"
  and .edition == "2024"
  and .headless == true
' policy/toolchain.json >/dev/null

metadata=$(mktemp /tmp/atrinik-content-toolkit-metadata.XXXXXX)
trap 'rm -f -- "${metadata}"' EXIT
cargo metadata --locked --offline --format-version 1 >"${metadata}"

jq -e --slurpfile policy policy/dependencies.json \
  --slurpfile toolchain policy/toolchain.json '
  . as $metadata
  | all($metadata.packages[];
    (.license // "") as $expression
    | ($expression | gsub("/"; " OR ") | gsub("[()]"; " ") | split(" ")
      | map(select(. != "" and . != "AND" and . != "OR"))) as $licenses
    | ($licenses | length) > 0
      and all($licenses[];
        . as $license | ($policy[0].allowed_spdx | index($license)) != null))
  and all($policy[0].direct_dependencies[];
    . as $dependency
    | any($metadata.packages[];
      .name == $dependency.name
      and .version == $dependency.version
      and .license == $dependency.license))
  and all($metadata.packages[];
    (.source // "") | test("github.com/atrinik/(classic|client|editor|renderer|server)") | not)
  and all($metadata.packages[].name;
    . as $name | ($toolchain[0].forbidden_packages | index($name)) == null)
' "${metadata}" >/dev/null
