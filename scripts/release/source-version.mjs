import {execFileSync} from "node:child_process";
import {createRequire} from "node:module";
import {pathToFileURL} from "node:url";

const require = createRequire(import.meta.url);
const semver = require("semver");
const {analyzeCommits} = await import(pathToFileURL(require.resolve("@semantic-release/commit-analyzer")).href);
const {currentBranch, nextVersion} = require("./release-policy.cjs");
const configuration = require("../../.releaserc.json");
const analyzer = configuration.plugins.find((plugin) => Array.isArray(plugin) && plugin[0] === "@semantic-release/commit-analyzer");
if (!analyzer) throw new Error("source release has no configured commit analyzer");
const git = (...args) => execFileSync("git", args, {encoding: "utf8", maxBuffer: 32 * 1024 * 1024}).trim();
const revision = git("rev-parse", "HEAD");
if (!/^[0-9a-f]{40}$/.test(revision)) throw new Error("invalid source revision");
const branch = currentBranch();
const maintenance = /^([0-9]+)\.([0-9]+)\.x$/.exec(branch);
const tags = git("tag", "--merged", "HEAD").split("\n").filter((tag) => /^v[0-9]+\.[0-9]+\.[0-9]+$/.test(tag) && semver.valid(tag.slice(1)));
const eligible = maintenance ? tags.filter((tag) => tag.startsWith(`v${maintenance[1]}.${maintenance[2]}.`)) : tags;
eligible.sort((left, right) => semver.rcompare(left.slice(1), right.slice(1)));
const lastTag = eligible[0];
const range = lastTag ? `${lastTag}..HEAD` : "HEAD";
const records = git("log", "--format=%H%x00%B%x00", range).split("\0");
const commits = [];
for (let index = 0; index + 1 < records.length; index += 2) {
  const hash = records[index].trim();
  if (hash) commits.push({hash, message: records[index + 1]});
}
let level = await analyzeCommits(analyzer[1], {
  cwd: process.cwd(), branch: {name: branch}, commits,
  logger: {log() {}, error() {}, success() {}},
});
const pending = JSON.parse(execFileSync("python3", ["tools/crate_prepare.py", "pending"], {encoding: "utf8", maxBuffer: 1024 * 1024})).pending;
if (pending && !level) level = "patch";
const version = level ? (lastTag ? nextVersion(lastTag.slice(1), level) : "1.0.0") : null;
process.stdout.write(JSON.stringify({revision, version, source_tag: version ? `v${version}` : null}) + "\n");
