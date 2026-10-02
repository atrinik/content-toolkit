// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

use std::{
    collections::BTreeMap,
    error::Error,
    ffi::OsString,
    fs::File,
    io::{Read, Write},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::atomic::AtomicBool,
    time::{Duration, Instant},
};

use atrinik_catalog::{CatalogLimits, Domain, FieldRule, LineDocumentLoader, ReferenceKind};
use atrinik_diagnostics::DiagnosticLimits;
use atrinik_schema::{Schema, SchemaLimits};
use atrinik_source::{Limits, SourceId};
use atrinik_transaction::{
    json::{decode_plan, encode_preview},
    store::{CommitOutcome, GenerationStore, read_source_file},
    CatalogShape, Control, FilePolicy, ProjectLimits, ProjectPolicy, ProjectSnapshot,
    SourceIdentity, validate_path,
};
use rustix::fs::{Mode, OFlags};
use serde::Deserialize;

const MAXIMUM_CONTROL_BYTES: usize = 1024 * 1024;
const MAXIMUM_PLAN_BYTES: usize = 8 * 1024 * 1024;
const MAXIMUM_EXECUTION_MILLIS: u64 = 300_000;
const BOOTSTRAP_EXECUTION_MILLIS: u64 = 30_000;

pub fn run(arguments: &[OsString]) -> Result<(), Box<dyn Error>> {
    let Some(first) = arguments.first().and_then(|value| value.to_str()) else {
        return Err(usage().into());
    };
    let (command, option_arguments) = if first.starts_with("--") {
        ("preview", arguments)
    } else {
        (first, &arguments[1..])
    };
    let options = parse_options(option_arguments)?;
    let root = required_path(&options, "--root")?;
    let policy_path = required_path(&options, "--policy")?;
    let cancelled = AtomicBool::new(false);
    let bootstrap_control = Control {
        cancelled: &cancelled,
        deadline: Instant::now() + Duration::from_millis(BOOTSTRAP_EXECUTION_MILLIS),
    };
    let policy_config: PolicyConfig =
        read_json(&policy_path, MAXIMUM_CONTROL_BYTES, &bootstrap_control)?;
    let (policy, source_limits, timeout) = policy_config.build()?;
    let store = GenerationStore::open(&root, policy.limits, source_limits)?;
    let control = Control {
        cancelled: &cancelled,
        deadline: Instant::now() + timeout,
    };

    match command {
        "initialize" => {
            ensure_options(
                &options,
                &["--root", "--policy", "--manifest", "--input-root"],
            )?;
            let manifest: ImportManifest = read_json(
                &required_path(&options, "--manifest")?,
                MAXIMUM_CONTROL_BYTES,
                &control,
            )?;
            let snapshot = manifest.load(
                &required_path(&options, "--input-root")?,
                source_limits,
                policy.limits,
                &control,
            )?;
            let outcome = store.initialize(&snapshot, &policy, &control)?;
            write_commit_status("initialized", &outcome)
        }
        "preview" | "apply" => {
            ensure_options(&options, &["--root", "--policy", "--plan"])?;
            let plan_bytes = read_bounded(
                &required_path(&options, "--plan")?,
                plan_byte_limit(policy.limits),
                &control,
            )?;
            let plan = decode_plan(&plan_bytes, policy.limits)?;
            let snapshot = store.read(&control)?;
            let preview = atrinik_transaction::preview(&snapshot, &plan, &policy, &control)?;
            let mut output = encode_preview(&preview)?;
            if command == "apply" {
                if !preview.is_valid() {
                    std::io::stdout().write_all(&output)?;
                    std::io::stdout().write_all(b"\n")?;
                    return Err("refusing to apply a preview with active error diagnostics".into());
                }
                let outcome = store.apply(&preview, &control)?;
                let mut value: serde_json::Value = serde_json::from_slice(&output)?;
                let object = value
                    .as_object_mut()
                    .ok_or("preview encoder returned a non-object")?;
                object.insert("status".into(), serde_json::Value::String("published".into()));
                object.insert("dry_run".into(), serde_json::Value::Bool(false));
                object.insert(
                    "publication".into(),
                    serde_json::json!({
                        "revision": outcome.revision,
                        "durable": outcome.durable,
                        "warning": outcome.warning,
                    }),
                );
                output = serde_json::to_vec(&value)?;
            }
            std::io::stdout().write_all(&output)?;
            std::io::stdout().write_all(b"\n")?;
            Ok(())
        }
        _ => Err(usage().into()),
    }
}

fn parse_options(arguments: &[OsString]) -> Result<Vec<(String, OsString)>, Box<dyn Error>> {
    if !arguments.len().is_multiple_of(2) {
        return Err(usage().into());
    }
    let mut options = Vec::with_capacity(arguments.len() / 2);
    for pair in arguments.chunks_exact(2) {
        let name = pair[0].to_str().ok_or("option name is not UTF-8")?;
        if !matches!(
            name,
            "--root" | "--policy" | "--plan" | "--manifest" | "--input-root"
        ) || options.iter().any(|(existing, _)| existing == name)
        {
            return Err(usage().into());
        }
        options.push((name.to_owned(), pair[1].clone()));
    }
    Ok(options)
}

fn required_path(options: &[(String, OsString)], name: &str) -> Result<PathBuf, Box<dyn Error>> {
    options
        .iter()
        .find(|(candidate, _)| candidate == name)
        .map(|(_, value)| PathBuf::from(value))
        .ok_or_else(|| format!("missing required {name}").into())
}

fn ensure_options(options: &[(String, OsString)], expected: &[&str]) -> Result<(), Box<dyn Error>> {
    if options.len() != expected.len()
        || expected
            .iter()
            .any(|name| !options.iter().any(|(candidate, _)| candidate == name))
    {
        return Err(usage().into());
    }
    Ok(())
}

fn read_json<T: for<'de> Deserialize<'de>>(
    path: &Path,
    maximum: usize,
    control: &Control<'_>,
) -> Result<T, Box<dyn Error>> {
    Ok(serde_json::from_slice(&read_bounded(
        path, maximum, control,
    )?)?)
}

fn read_bounded(
    path: &Path,
    maximum: usize,
    control: &Control<'_>,
) -> Result<Vec<u8>, Box<dyn Error>> {
    control.check()?;
    let mut file = File::from(rustix::fs::open(
        path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
        Mode::empty(),
    )?);
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.nlink() != 1 {
        return Err(format!("{} is not a regular singly-linked file", path.display()).into());
    }
    let stamp = FileStamp::from(&metadata);
    let length = metadata.len();
    if length > maximum as u64 {
        return Err(format!("{} exceeds the maximum input size", path.display()).into());
    }
    let capacity = usize::try_from(length).map_err(|_| "input length does not fit in memory")?;
    let mut bytes = Vec::with_capacity(capacity);
    let mut reader = (&mut file).take((maximum as u64).saturating_add(1));
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        control.check()?;
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        bytes.extend_from_slice(&buffer[..count]);
        if bytes.len() > maximum {
            return Err(format!("{} exceeds the maximum input size", path.display()).into());
        }
    }
    control.check()?;
    if bytes.len() > maximum
        || bytes.len() != capacity
        || FileStamp::from(&file.metadata()?) != stamp
    {
        return Err(format!("{} exceeded its bound or changed during read", path.display()).into());
    }
    Ok(bytes)
}

#[derive(Eq, PartialEq)]
struct FileStamp {
    device: u64,
    inode: u64,
    size: u64,
    mode: u32,
    links: u64,
    modified: (i64, i64),
    changed: (i64, i64),
}

impl From<&std::fs::Metadata> for FileStamp {
    fn from(metadata: &std::fs::Metadata) -> Self {
        Self {
            device: metadata.dev(),
            inode: metadata.ino(),
            size: metadata.len(),
            mode: metadata.mode(),
            links: metadata.nlink(),
            modified: (metadata.mtime(), metadata.mtime_nsec()),
            changed: (metadata.ctime(), metadata.ctime_nsec()),
        }
    }
}

fn plan_byte_limit(limits: ProjectLimits) -> usize {
    limits
        .maximum_diff_bytes
        .saturating_add(limits.maximum_commands.saturating_mul(2048))
        .min(MAXIMUM_PLAN_BYTES)
}

fn write_commit_status(status: &str, outcome: &CommitOutcome) -> Result<(), Box<dyn Error>> {
    serde_json::to_writer(
        std::io::stdout(),
        &serde_json::json!({
            "status": status,
            "project_revision": outcome.revision,
            "durable": outcome.durable,
            "warning": outcome.warning,
        }),
    )?;
    println!();
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ImportManifest {
    version: u32,
    identity: IdentityConfig,
    files: Vec<ImportFile>,
}

impl ImportManifest {
    fn load(
        self,
        input_root: &Path,
        source_limits: Limits,
        project_limits: ProjectLimits,
        control: &Control<'_>,
    ) -> Result<ProjectSnapshot, Box<dyn Error>> {
        if self.version != 1 || self.files.len() > project_limits.maximum_files {
            return Err("invalid or oversized import manifest".into());
        }
        let mut files = BTreeMap::new();
        let mut total_bytes = 0usize;
        for entry in self.files {
            validate_path(&entry.path)?;
            validate_path(&entry.input)?;
            let remaining = project_limits.maximum_bytes.saturating_sub(total_bytes);
            let import_limits = Limits {
                maximum_file_bytes: source_limits.maximum_file_bytes.min(remaining),
                ..source_limits
            };
            let file = read_source_file(
                input_root,
                &entry.input,
                SourceId::new(&entry.source_id)?,
                import_limits,
                control,
            )?;
            total_bytes = total_bytes
                .checked_add(file.document.source_bytes().len())
                .ok_or("project import byte total overflow")?;
            if total_bytes > project_limits.maximum_bytes {
                return Err("project import exceeds its aggregate byte limit".into());
            }
            if file.mode != entry.mode {
                return Err(format!("mode precondition failed for {}", entry.path).into());
            }
            if files.insert(entry.path.clone(), file).is_some() {
                return Err(format!("duplicate manifest path: {}", entry.path).into());
            }
        }
        ProjectSnapshot::new(self.identity.into_native(), files, project_limits)
            .map_err(|error| Box::new(error) as Box<dyn Error>)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct IdentityConfig {
    repository: String,
    reference: String,
    revision: String,
    schema_version: u32,
}

impl IdentityConfig {
    fn into_native(self) -> SourceIdentity {
        SourceIdentity {
            repository: self.repository,
            reference: self.reference,
            revision: self.revision,
            schema_version: self.schema_version,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ImportFile {
    path: String,
    input: String,
    source_id: String,
    mode: u32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PolicyConfig {
    version: u32,
    maximum_execution_millis: u64,
    source_limits: SourceLimitsConfig,
    project_limits: ProjectLimitsConfig,
    catalog_limits: CatalogLimitsConfig,
    files: BTreeMap<String, FilePolicyConfig>,
}

impl PolicyConfig {
    fn build(self) -> Result<(ProjectPolicy, Limits, Duration), Box<dyn Error>> {
        if self.version != 1
            || self.maximum_execution_millis == 0
            || self.maximum_execution_millis > MAXIMUM_EXECUTION_MILLIS
        {
            return Err("invalid transaction policy version or execution bound".into());
        }
        let source_limits = self.source_limits.build()?;
        let limits = self.project_limits.build()?;
        let catalog_limits = self.catalog_limits.build()?;
        if self.files.len() > limits.maximum_files {
            return Err("policy file inventory exceeds its project bound".into());
        }
        let mut files = BTreeMap::new();
        for (path, config) in self.files {
            validate_path(&path)?;
            files.insert(path, config.build(catalog_limits)?);
        }
        Ok((
            ProjectPolicy {
                files,
                limits,
                catalog_limits,
            },
            source_limits,
            Duration::from_millis(self.maximum_execution_millis),
        ))
    }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
struct SourceLimitsConfig {
    maximum_file_bytes: usize,
    maximum_line_bytes: usize,
    maximum_records: usize,
    maximum_tokens: usize,
    maximum_value_bytes: usize,
    maximum_edits: usize,
    maximum_nesting: usize,
    maximum_diagnostics: usize,
}

impl SourceLimitsConfig {
    fn build(self) -> Result<Limits, Box<dyn Error>> {
        let requested = Limits {
            maximum_file_bytes: self.maximum_file_bytes,
            maximum_line_bytes: self.maximum_line_bytes,
            maximum_records: self.maximum_records,
            maximum_tokens: self.maximum_tokens,
            maximum_value_bytes: self.maximum_value_bytes,
            maximum_edits: self.maximum_edits,
            maximum_nesting: self.maximum_nesting,
            maximum_diagnostics: self.maximum_diagnostics,
        };
        let ceiling = Limits::default();
        ensure_bounded(
            &[
                (requested.maximum_file_bytes, ceiling.maximum_file_bytes),
                (requested.maximum_line_bytes, ceiling.maximum_line_bytes),
                (requested.maximum_records, ceiling.maximum_records),
                (requested.maximum_tokens, ceiling.maximum_tokens),
                (requested.maximum_value_bytes, ceiling.maximum_value_bytes),
                (requested.maximum_edits, ceiling.maximum_edits),
                (requested.maximum_nesting, ceiling.maximum_nesting),
                (requested.maximum_diagnostics, ceiling.maximum_diagnostics),
            ],
            "source limits",
        )?;
        Ok(requested)
    }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProjectLimitsConfig {
    maximum_files: usize,
    maximum_bytes: usize,
    maximum_commands: usize,
    maximum_diff_bytes: usize,
    maximum_diagnostics: usize,
}

impl ProjectLimitsConfig {
    fn build(self) -> Result<ProjectLimits, Box<dyn Error>> {
        let requested = ProjectLimits {
            maximum_files: self.maximum_files,
            maximum_bytes: self.maximum_bytes,
            maximum_commands: self.maximum_commands,
            maximum_diff_bytes: self.maximum_diff_bytes,
            maximum_diagnostics: self.maximum_diagnostics,
        };
        let ceiling = ProjectLimits::default();
        ensure_bounded(
            &[
                (requested.maximum_files, ceiling.maximum_files),
                (requested.maximum_bytes, ceiling.maximum_bytes),
                (requested.maximum_commands, ceiling.maximum_commands),
                (requested.maximum_diff_bytes, ceiling.maximum_diff_bytes),
                (
                    requested.maximum_diagnostics,
                    ceiling.maximum_diagnostics,
                ),
            ],
            "project limits",
        )?;
        Ok(requested)
    }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogLimitsConfig {
    maximum_documents: usize,
    maximum_definitions_per_document: usize,
    maximum_definitions: usize,
    maximum_aliases_per_definition: usize,
    maximum_references_per_definition: usize,
    maximum_preview_values: usize,
    maximum_string_bytes: usize,
    maximum_semantic_depth: usize,
    maximum_graph_work: usize,
    maximum_invalidation: usize,
    maximum_query_terms: usize,
    maximum_query_work: usize,
    diagnostic_limits: DiagnosticLimitsConfig,
}

impl CatalogLimitsConfig {
    fn build(self) -> Result<CatalogLimits, Box<dyn Error>> {
        let diagnostics = self.diagnostic_limits.build()?;
        let requested = CatalogLimits {
            maximum_documents: self.maximum_documents,
            maximum_definitions_per_document: self.maximum_definitions_per_document,
            maximum_definitions: self.maximum_definitions,
            maximum_aliases_per_definition: self.maximum_aliases_per_definition,
            maximum_references_per_definition: self.maximum_references_per_definition,
            maximum_preview_values: self.maximum_preview_values,
            maximum_string_bytes: self.maximum_string_bytes,
            maximum_semantic_depth: self.maximum_semantic_depth,
            maximum_graph_work: self.maximum_graph_work,
            maximum_invalidation: self.maximum_invalidation,
            maximum_query_terms: self.maximum_query_terms,
            maximum_query_work: self.maximum_query_work,
            diagnostic_limits: diagnostics,
        };
        let ceiling = CatalogLimits::default();
        ensure_bounded(
            &[
                (requested.maximum_documents, ceiling.maximum_documents),
                (
                    requested.maximum_definitions_per_document,
                    ceiling.maximum_definitions_per_document,
                ),
                (
                    requested.maximum_definitions,
                    ceiling.maximum_definitions,
                ),
                (
                    requested.maximum_aliases_per_definition,
                    ceiling.maximum_aliases_per_definition,
                ),
                (
                    requested.maximum_references_per_definition,
                    ceiling.maximum_references_per_definition,
                ),
                (
                    requested.maximum_preview_values,
                    ceiling.maximum_preview_values,
                ),
                (
                    requested.maximum_string_bytes,
                    ceiling.maximum_string_bytes,
                ),
                (
                    requested.maximum_semantic_depth,
                    ceiling.maximum_semantic_depth,
                ),
                (requested.maximum_graph_work, ceiling.maximum_graph_work),
                (
                    requested.maximum_invalidation,
                    ceiling.maximum_invalidation,
                ),
                (requested.maximum_query_terms, ceiling.maximum_query_terms),
                (requested.maximum_query_work, ceiling.maximum_query_work),
            ],
            "catalog limits",
        )?;
        Ok(requested)
    }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
struct DiagnosticLimitsConfig {
    maximum_diagnostics: usize,
    maximum_related: usize,
    maximum_semantic_depth: usize,
    maximum_text_bytes: usize,
}

impl DiagnosticLimitsConfig {
    fn build(self) -> Result<DiagnosticLimits, Box<dyn Error>> {
        let requested = DiagnosticLimits {
            maximum_diagnostics: self.maximum_diagnostics,
            maximum_related: self.maximum_related,
            maximum_semantic_depth: self.maximum_semantic_depth,
            maximum_text_bytes: self.maximum_text_bytes,
        };
        let ceiling = DiagnosticLimits::default();
        ensure_bounded(
            &[
                (
                    requested.maximum_diagnostics,
                    ceiling.maximum_diagnostics,
                ),
                (requested.maximum_related, ceiling.maximum_related),
                (
                    requested.maximum_semantic_depth,
                    ceiling.maximum_semantic_depth,
                ),
                (requested.maximum_text_bytes, ceiling.maximum_text_bytes),
            ],
            "diagnostic limits",
        )?;
        Ok(requested)
    }
}

fn ensure_bounded(values: &[(usize, usize)], name: &str) -> Result<(), Box<dyn Error>> {
    if values
        .iter()
        .any(|(requested, ceiling)| *requested == 0 || requested > ceiling)
    {
        return Err(format!("{name} must be positive and no greater than built-in ceilings").into());
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FilePolicyConfig {
    schema: SchemaConfig,
    loader: LoaderConfig,
    shape: ShapeConfig,
}

impl FilePolicyConfig {
    fn build(self, limits: CatalogLimits) -> Result<FilePolicy, Box<dyn Error>> {
        Ok(FilePolicy {
            schema: self.schema.build()?,
            loader: self.loader.build(limits)?,
            shape: self.shape.build(),
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SchemaConfig {
    name: String,
    required_fields: Vec<String>,
}

impl SchemaConfig {
    fn build(self) -> Result<Schema, Box<dyn Error>> {
        Ok(Schema::new(
            self.name,
            self.required_fields
                .into_iter()
                .map(String::into_bytes),
            SchemaLimits::default(),
        )?)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LoaderConfig {
    domain: DomainConfig,
    namespace: String,
    schema_version: u32,
    rules: BTreeMap<String, RuleConfig>,
}

impl LoaderConfig {
    fn build(self, limits: CatalogLimits) -> Result<LineDocumentLoader, Box<dyn Error>> {
        let rules = self
            .rules
            .into_iter()
            .map(|(field, rule)| (field.into_bytes(), rule.build()))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(LineDocumentLoader::new(
            self.domain.build(),
            self.namespace,
            self.schema_version,
            rules,
        )?
        .with_limits(limits)?)
    }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
enum DomainConfig {
    Archetype,
    Map,
    Face,
    Animation,
    Treasure,
    Faction,
    Interface,
    Quest,
    Resource,
}

impl DomainConfig {
    const fn build(self) -> Domain {
        match self {
            Self::Archetype => Domain::Archetype,
            Self::Map => Domain::Map,
            Self::Face => Domain::Face,
            Self::Animation => Domain::Animation,
            Self::Treasure => Domain::Treasure,
            Self::Faction => Domain::Faction,
            Self::Interface => Domain::Interface,
            Self::Quest => Domain::Quest,
            Self::Resource => Domain::Resource,
        }
    }
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum RuleConfig {
    Alias,
    Inherits,
    EmbeddedObject,
    Reference {
        domain: DomainConfig,
        reference_kind: ReferenceKindConfig,
        optional: bool,
    },
    Label,
    Summary,
    Tag,
    Keyword,
}

impl RuleConfig {
    fn build(self) -> Result<FieldRule, Box<dyn Error>> {
        Ok(match self {
            Self::Alias => FieldRule::Alias,
            Self::Inherits => FieldRule::Inherits,
            Self::EmbeddedObject => FieldRule::EmbeddedObject,
            Self::Reference {
                domain,
                reference_kind,
                optional,
            } => {
                let domain = domain.build();
                let kind = reference_kind.build();
                if kind.expected_domain().is_some_and(|expected| expected != domain) {
                    return Err("reference kind does not match its domain".into());
                }
                FieldRule::Reference {
                    domain,
                    kind,
                    optional,
                }
            }
            Self::Label => FieldRule::Label,
            Self::Summary => FieldRule::Summary,
            Self::Tag => FieldRule::Tag,
            Self::Keyword => FieldRule::Keyword,
        })
    }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
enum ReferenceKindConfig {
    Generic,
    Archetype,
    Inherits,
    Map,
    Face,
    Animation,
    Treasure,
    Faction,
    Interface,
    Quest,
    Resource,
}

impl ReferenceKindConfig {
    const fn build(self) -> ReferenceKind {
        match self {
            Self::Generic => ReferenceKind::Generic,
            Self::Archetype => ReferenceKind::Archetype,
            Self::Inherits => ReferenceKind::Inherits,
            Self::Map => ReferenceKind::Map,
            Self::Face => ReferenceKind::Face,
            Self::Animation => ReferenceKind::Animation,
            Self::Treasure => ReferenceKind::Treasure,
            Self::Faction => ReferenceKind::Faction,
            Self::Interface => ReferenceKind::Interface,
            Self::Quest => ReferenceKind::Quest,
            Self::Resource => ReferenceKind::Resource,
        }
    }
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum ShapeConfig {
    Objects,
    Single { local_id: String },
}

impl ShapeConfig {
    fn build(self) -> CatalogShape {
        match self {
            Self::Objects => CatalogShape::Objects,
            Self::Single { local_id } => CatalogShape::Single(local_id),
        }
    }
}

const fn usage() -> &'static str {
    "usage: atrinik-content transaction initialize --root ROOT --policy POLICY --manifest MANIFEST --input-root INPUT_ROOT | atrinik-content transaction (preview | apply) --root ROOT --policy POLICY --plan PLAN"
}
