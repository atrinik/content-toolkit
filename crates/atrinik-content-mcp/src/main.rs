// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT
#![forbid(unsafe_code)]

mod io;
use atrinik_catalog::{CatalogLimits, Domain, FieldRule, LineDocumentLoader, ReferenceKind};
use atrinik_content_mcp::{Identity, Provider, Snapshot};
use atrinik_schema::{Schema, SchemaLimits};
use atrinik_source::{Document, Limits, SourceId};
use atrinik_transaction::project::{
    CatalogShape, FilePolicy, ProjectFile, ProjectLimits, ProjectPolicy, ProjectSnapshot,
    SourceIdentity,
};
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{Arc, atomic::AtomicBool},
    time::{Duration, Instant},
};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Configuration {
    snapshots: Vec<ConfiguredSnapshot>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfiguredSnapshot {
    root: PathBuf,
    identity: Identity,
    files: Vec<ConfiguredFile>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfiguredFile {
    path: String,
    domain: String,
    namespace: String,
    #[serde(default)]
    single_id: Option<String>,
    #[serde(default)]
    required_fields: Vec<String>,
    #[serde(default)]
    rules: BTreeMap<String, Rule>,
}
#[derive(Clone, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Rule {
    Alias,
    Inherits,
    EmbeddedObject,
    Label,
    Summary,
    Tag,
    Keyword,
    Reference {
        domain: String,
        #[serde(default)]
        optional: bool,
    },
}
fn domain(value: &str) -> Result<Domain, &'static str> {
    Domain::ALL
        .into_iter()
        .find(|d| d.as_str() == value)
        .ok_or("invalid_configuration")
}
fn field_rule(rule: Rule) -> Result<FieldRule, &'static str> {
    Ok(match rule {
        Rule::Alias => FieldRule::Alias,
        Rule::Inherits => FieldRule::Inherits,
        Rule::EmbeddedObject => FieldRule::EmbeddedObject,
        Rule::Label => FieldRule::Label,
        Rule::Summary => FieldRule::Summary,
        Rule::Tag => FieldRule::Tag,
        Rule::Keyword => FieldRule::Keyword,
        Rule::Reference {
            domain: value,
            optional,
        } => FieldRule::Reference {
            domain: domain(&value)?,
            kind: ReferenceKind::Generic,
            optional,
        },
    })
}
struct Baseline {
    directory_identity: (u64, u64),
    root: PathBuf,
    identity: Identity,
    files: BTreeMap<String, ProjectFile>,
    status: Vec<u8>,
    tracked: Vec<u8>,
    ignored: Vec<u8>,
}
struct Loaded {
    provider: Provider,
    baselines: Vec<Baseline>,
}
impl Loaded {
    fn verify(&self, cancelled: &AtomicBool, deadline: Instant) -> Result<(), &'static str> {
        for baseline in &self.baselines {
            if cancelled.load(std::sync::atomic::Ordering::Acquire) {
                return Err("cancelled");
            }
            if Instant::now() >= deadline {
                return Err("timeout");
            }
            let root = io::ConfiguredRoot::open(&baseline.root)
                .map_err(|e| e.code())?
                .with_deadline(deadline);
            if root.identity().map_err(|e| e.code())? != baseline.directory_identity {
                return Err("stale_identity");
            }
            let same = |args: &[&str], expected: &[u8]| -> Result<(), &'static str> {
                if io::git_metadata(&root, args).map_err(|e| e.code())? != expected {
                    return Err("stale_identity");
                }
                Ok(())
            };
            same(
                &["rev-parse", "--verify", "HEAD"],
                format!("{}\n", baseline.identity.commit).as_bytes(),
            )?;
            same(
                &["symbolic-ref", "--quiet", "HEAD"],
                format!("{}\n", baseline.identity.branch).as_bytes(),
            )?;
            same(&["ls-files", "--cached", "-z"], &baseline.tracked)?;
            same(
                &[
                    "ls-files",
                    "--cached",
                    "--ignored",
                    "--exclude-standard",
                    "-z",
                ],
                &baseline.ignored,
            )?;
            same(
                &["status", "--porcelain=v1", "-z", "--untracked-files=no"],
                &baseline.status,
            )?;
            io::git_metadata(
                &root,
                &[
                    "merge-base",
                    "--is-ancestor",
                    &baseline.identity.main_base_commit,
                    "refs/heads/main",
                ],
            )
            .map_err(|e| e.code())?;
            for (path, file) in &baseline.files {
                let (bytes, mode) = root.read(path, cancelled, deadline).map_err(|e| e.code())?;
                if bytes != file.document.source_bytes() || mode != file.mode {
                    return Err("stale_identity");
                }
            }
            same(
                &["status", "--porcelain=v1", "-z", "--untracked-files=no"],
                &baseline.status,
            )?;
            same(
                &["rev-parse", "--verify", "HEAD"],
                format!("{}\n", baseline.identity.commit).as_bytes(),
            )?;
        }
        Ok(())
    }
}
fn load(
    config: Configuration,
    cancelled: &AtomicBool,
    deadline: Instant,
) -> Result<Loaded, &'static str> {
    if config.snapshots.is_empty() || config.snapshots.len() > 128 {
        return Err("limit_exceeded");
    }
    let mut snapshots = Vec::new();
    let mut baselines = Vec::new();
    let mut bytes = 0usize;
    for configured in config.snapshots {
        if Instant::now() >= deadline {
            return Err("timeout");
        }
        configured.identity.validate().map_err(|e| e.code())?;
        if configured.files.is_empty() || configured.files.len() > 1000 {
            return Err("limit_exceeded");
        }
        let root = io::ConfiguredRoot::open(&configured.root)
            .map_err(|e| e.code())?
            .with_deadline(deadline);
        let actual_head =
            io::git_metadata(&root, &["rev-parse", "--verify", "HEAD"]).map_err(|e| e.code())?;
        let actual_branch =
            io::git_metadata(&root, &["symbolic-ref", "--quiet", "HEAD"]).map_err(|e| e.code())?;
        if actual_head != format!("{}\n", configured.identity.commit).as_bytes()
            || actual_branch != format!("{}\n", configured.identity.branch).as_bytes()
        {
            return Err("stale_identity");
        }
        io::git_metadata(
            &root,
            &[
                "merge-base",
                "--is-ancestor",
                &configured.identity.main_base_commit,
                &configured.identity.commit,
            ],
        )
        .map_err(|e| e.code())?;
        io::git_metadata(
            &root,
            &[
                "merge-base",
                "--is-ancestor",
                &configured.identity.main_base_commit,
                "refs/heads/main",
            ],
        )
        .map_err(|e| e.code())?;
        let tracked =
            io::git_metadata(&root, &["ls-files", "--cached", "-z"]).map_err(|e| e.code())?;
        let ignored = io::git_metadata(
            &root,
            &[
                "ls-files",
                "--cached",
                "--ignored",
                "--exclude-standard",
                "-z",
            ],
        )
        .map_err(|e| e.code())?;
        let status = io::git_metadata(
            &root,
            &["status", "--porcelain=v1", "-z", "--untracked-files=no"],
        )
        .map_err(|e| e.code())?;
        // A dirty fingerprint covers every changed tracked path. Changes outside
        // the admitted inventory cannot be read safely, so registration fails.
        for entry in status.split(|b| *b == 0).filter(|entry| !entry.is_empty()) {
            if entry.len() < 4
                || entry[2] != b' '
                || !entry[..2]
                    .iter()
                    .all(|b| matches!(*b, b' ' | b'M' | b'A' | b'D' | b'T'))
                || !configured
                    .files
                    .iter()
                    .any(|file| file.path.as_bytes() == &entry[3..])
            {
                return Err("incomplete_data");
            }
        }
        let mut dirty = Sha256::new();
        dirty.update(&status);
        let mut files = BTreeMap::new();
        let mut policies = BTreeMap::new();
        let mut selected_files = configured.files;
        selected_files.sort_by(|a, b| a.path.cmp(&b.path));
        for selected in selected_files {
            if !tracked
                .split(|b| *b == 0)
                .any(|p| p == selected.path.as_bytes())
                || ignored
                    .split(|b| *b == 0)
                    .any(|p| p == selected.path.as_bytes())
            {
                return Err("forbidden_data");
            }
            let (source, mode) = root
                .read(&selected.path, cancelled, deadline)
                .map_err(|e| e.code())?;
            dirty.update((selected.path.len() as u64).to_le_bytes());
            dirty.update(selected.path.as_bytes());
            dirty.update((source.len() as u64).to_le_bytes());
            dirty.update(&source);
            if status.is_empty() {
                let object = format!("{}:{}", configured.identity.commit, selected.path);
                if io::git_metadata(&root, &["cat-file", "blob", &object]).map_err(|e| e.code())?
                    != source
                {
                    return Err("stale_identity");
                }
            }
            bytes = bytes.checked_add(source.len()).ok_or("limit_exceeded")?;
            if bytes > 8 * 1024 * 1024 {
                return Err("limit_exceeded");
            }
            let limits = Limits {
                maximum_file_bytes: io::MAX_FILE,
                maximum_records: 10_000,
                maximum_tokens: 50_000,
                maximum_nesting: 32,
                ..Limits::default()
            };
            let source_id = SourceId::new(&selected.path).map_err(|_| "invalid_configuration")?;
            let document = Arc::new(
                Document::parse(source_id, source, limits).map_err(|_| "incomplete_data")?,
            );
            let rules = selected
                .rules
                .into_iter()
                .map(|(key, value)| Ok((key.into_bytes(), field_rule(value)?)))
                .collect::<Result<Vec<_>, &'static str>>()?;
            let loader =
                LineDocumentLoader::new(domain(&selected.domain)?, selected.namespace, 1, rules)
                    .map_err(|_| "invalid_configuration")?;
            let schema = Schema::new(
                "configured-v1",
                selected.required_fields.into_iter().map(String::into_bytes),
                SchemaLimits::default(),
            )
            .map_err(|_| "invalid_configuration")?;
            let shape = selected
                .single_id
                .map_or(CatalogShape::Objects, CatalogShape::Single);
            policies.insert(
                selected.path.clone(),
                FilePolicy {
                    schema,
                    loader,
                    shape,
                },
            );
            if files
                .insert(selected.path, ProjectFile { document, mode })
                .is_some()
            {
                return Err("invalid_configuration");
            }
        }
        if io::git_metadata(
            &root,
            &["status", "--porcelain=v1", "-z", "--untracked-files=no"],
        )
        .map_err(|e| e.code())?
            != status
        {
            return Err("stale_identity");
        }
        let expected_dirty = if status.is_empty() {
            None
        } else {
            Some(
                dirty
                    .finalize()
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>(),
            )
        };
        if configured.identity.dirty_fingerprint != expected_dirty {
            return Err("stale_identity");
        }
        for (path, file) in &files {
            if root
                .read(path, cancelled, deadline)
                .map_err(|e| e.code())?
                .0
                != file.document.source_bytes()
            {
                return Err("stale_identity");
            }
        }
        let limits = ProjectLimits {
            maximum_files: 1000,
            maximum_bytes: 8 * 1024 * 1024,
            maximum_commands: 100,
            maximum_diff_bytes: 32 * 1024,
            ..ProjectLimits::default()
        };
        let source_identity = SourceIdentity {
            repository: configured.identity.repository.clone(),
            reference: "refs/heads/main".into(),
            revision: configured.identity.commit.clone(),
            schema_version: 1,
        };
        baselines.push(Baseline {
            directory_identity: root.identity().map_err(|e| e.code())?,
            root: configured.root,
            identity: configured.identity.clone(),
            files: files.clone(),
            status,
            tracked,
            ignored,
        });
        let project = ProjectSnapshot::new(source_identity, files, limits)
            .map_err(|_| "invalid_configuration")?;
        let policy = ProjectPolicy {
            files: policies,
            limits,
            catalog_limits: CatalogLimits::default(),
        };
        snapshots.push(Snapshot::new(configured.identity, project, policy).map_err(|e| e.code())?);
    }
    Ok(Loaded {
        provider: Provider::new(snapshots).map_err(|e| e.code())?,
        baselines,
    })
}
fn run() -> Result<(), &'static str> {
    let mut arguments = std::env::args_os().skip(1);
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--config")) {
        return Err("explicit_configuration_required");
    }
    let config_path = PathBuf::from(arguments.next().ok_or("explicit_configuration_required")?);
    if arguments.next().is_some() || !config_path.is_absolute() {
        return Err("invalid_configuration");
    }
    let parent = config_path.parent().ok_or("invalid_configuration")?;
    let name = config_path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or("invalid_configuration")?;
    let cancelled = AtomicBool::new(false);
    let root = io::ConfiguredRoot::open(parent).map_err(|e| e.code())?;
    let (bytes, _) = root
        .read(name, &cancelled, Instant::now() + Duration::from_secs(5))
        .map_err(|e| e.code())?;
    let config: Configuration =
        serde_json::from_slice(&bytes).map_err(|_| "invalid_configuration")?;
    let loaded = load(
        config.clone(),
        &cancelled,
        Instant::now() + Duration::from_secs(5),
    )?;
    let tool = json!({"name":"content_query","description":"Bounded read-only semantic content queries and transaction previews over configured snapshot selectors.","inputSchema":atrinik_content_mcp::input_schema(),"outputSchema":atrinik_content_mcp::output_schema(),"annotations":{"readOnlyHint":true,"destructiveHint":false,"openWorldHint":false}});
    io::serve(
        std::io::BufReader::new(std::io::stdin()),
        std::io::stdout().lock(),
        &tool,
        |value, cancelled| {
            let request = serde_json::from_value(value).map_err(|_| "invalid_arguments")?;
            {
                let deadline = Instant::now() + Duration::from_secs(5);
                loaded.verify(cancelled, deadline)?;
                let result = loaded
                    .provider
                    .call_with_deadline(request, cancelled, deadline);
                loaded.verify(cancelled, deadline)?;
                result
            }
            .map_err(|e| e.code())
        },
    )
    .map_err(|_| "transport_unavailable")
}
fn main() {
    if let Err(code) = run() {
        eprintln!("atrinik-content-mcp: {code}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    fn git(root: &std::path::Path, args: &[&str]) -> String {
        let result = Command::new("git")
            .args(args)
            .current_dir(root)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .output()
            .unwrap();
        assert!(result.status.success());
        String::from_utf8(result.stdout).unwrap().trim().into()
    }
    #[test]
    fn registration_checks_actual_git_identity_and_fresh_source() {
        let root =
            std::env::temp_dir().join(format!("atrinik-mcp-registration-{}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        git(&root, &["init", "-b", "main"]);
        std::fs::write(
            root.join("synthetic.arc"),
            b"Object synthetic\nname Synthetic\nend\n",
        )
        .unwrap();
        git(&root, &["add", "synthetic.arc"]);
        git(
            &root,
            &[
                "-c",
                "user.name=Synthetic",
                "-c",
                "user.email=synthetic@example.invalid",
                "commit",
                "-m",
                "synthetic fixture",
            ],
        );
        let commit = git(&root, &["rev-parse", "HEAD"]);
        let value = json!({"snapshots":[{"root":root,"identity":{"repository":"atrinik/content","branch":"refs/heads/main","commit":commit,"main_base_commit":commit,"worktree":"synthetic","source_role":"main","view_role":"replacement","dirty_fingerprint":null,"authorization":"synthetic","manifest":"synthetic","profile":"synthetic","registry":"synthetic","schema_version":1,"provider_version":atrinik_content_mcp::SCHEMA_VERSION},"files":[{"path":"synthetic.arc","domain":"archetype","namespace":"synthetic","rules":{"name":{"kind":"label"}}}]}]});
        let config: Configuration = serde_json::from_value(value).unwrap();
        let cancelled = AtomicBool::new(false);
        let loaded = load(
            config.clone(),
            &cancelled,
            Instant::now() + Duration::from_secs(5),
        )
        .unwrap();
        assert!(
            loaded
                .verify(&cancelled, Instant::now() + Duration::from_secs(5))
                .is_ok()
        );
        std::fs::write(root.join("synthetic.arc"), b"Object changed\nend\n").unwrap();
        assert!(matches!(
            loaded.verify(&cancelled, Instant::now() + Duration::from_secs(5)),
            Err("stale_identity")
        ));
        assert!(matches!(
            load(config, &cancelled, Instant::now() + Duration::from_secs(5)),
            Err("stale_identity")
        ));
        std::fs::remove_dir_all(root).unwrap();
    }
}
