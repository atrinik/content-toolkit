"use strict";

const fs = require("node:fs");
const path = require("node:path");

// A reviewed pending crate set always receives a source release, even if the
// repository's ordinary commit rules would produce no release for this change.
module.exports.analyzeCommits = (_config, context) => {
  const manifestPath = path.join(context.cwd, "build/release/crate-release.json");
  if (!fs.existsSync(manifestPath)) return null;
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  if (manifest.schema_version !== 1 || !Array.isArray(manifest.crates)) {
    throw new Error("invalid prepared crate release manifest");
  }
  return manifest.crates.length > 0 ? "patch" : null;
};
