// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

use atrinik_catalog::{Catalog, CatalogLimits, EvidenceReferences, LineDocumentLoader};
use atrinik_diagnostics::{Diagnostic, Span, SuppressionPolicy};
use atrinik_schema::Schema;
use atrinik_source::{Document, EditPlan, RecordKind};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fmt,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Instant,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceIdentity {
    pub repository: String,
    pub reference: String,
    pub revision: String,
    pub schema_version: u32,
}

#[derive(Clone, Copy, Debug)]
pub struct ProjectLimits {
    pub maximum_files: usize,
    pub maximum_bytes: usize,
    pub maximum_commands: usize,
    pub maximum_diff_bytes: usize,
    pub maximum_diagnostics: usize,
}
impl Default for ProjectLimits {
    fn default() -> Self {
        Self {
            maximum_files: 10_000,
            maximum_bytes: 128 * 1024 * 1024,
            maximum_commands: 4096,
            maximum_diff_bytes: 4 * 1024 * 1024,
            maximum_diagnostics: 256,
        }
    }
}

/// Cooperative cancellation/deadline checkpoints supplement parser/catalog work bounds.
pub struct Control<'a> {
    pub cancelled: &'a AtomicBool,
    pub deadline: Instant,
}
impl Control<'_> {
    pub fn check(&self) -> Result<(), TransactionError> {
        if self.cancelled.load(Ordering::Acquire) {
            return Err(TransactionError::Cancelled);
        }
        if Instant::now() >= self.deadline {
            return Err(TransactionError::Deadline);
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct ProjectFile {
    pub document: Arc<Document>,
    pub mode: u32,
}
#[derive(Clone, Debug)]
pub struct ProjectSnapshot {
    identity: SourceIdentity,
    files: BTreeMap<String, ProjectFile>,
    revision: String,
}
impl ProjectSnapshot {
    pub fn new(
        identity: SourceIdentity,
        files: BTreeMap<String, ProjectFile>,
        limits: ProjectLimits,
    ) -> Result<Self, TransactionError> {
        if identity.repository != "atrinik/content"
            || identity.reference != "refs/heads/main"
            || identity.schema_version != 1
            || identity.revision.len() != 40
            || !identity
                .revision
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(TransactionError::InvalidIdentity);
        }
        if files.len() > limits.maximum_files {
            return Err(TransactionError::Limit("files"));
        }
        let mut total = 0usize;
        let mut sources = BTreeSet::new();
        let mut hash = Sha256::new();
        hash.update(b"atrinik-project-v1\0");
        for value in [
            &identity.repository,
            &identity.reference,
            &identity.revision,
        ] {
            hash_part(&mut hash, value.as_bytes());
        }
        hash.update(identity.schema_version.to_le_bytes());
        for (path, file) in &files {
            validate_path(path)?;
            if file.mode & !0o777 != 0 || !sources.insert(file.document.source_id().as_str()) {
                return Err(TransactionError::InvalidFile(path.clone()));
            }
            total = total
                .checked_add(file.document.source_bytes().len())
                .ok_or(TransactionError::Limit("bytes"))?;
            if total > limits.maximum_bytes {
                return Err(TransactionError::Limit("bytes"));
            }
            hash_part(&mut hash, path.as_bytes());
            hash_part(&mut hash, file.document.source_id().as_str().as_bytes());
            hash.update(file.mode.to_le_bytes());
            hash_part(&mut hash, file.document.source_bytes());
        }
        let revision = hex(&hash.finalize());
        Ok(Self {
            identity,
            files,
            revision,
        })
    }
    pub fn identity(&self) -> &SourceIdentity {
        &self.identity
    }
    pub fn files(&self) -> &BTreeMap<String, ProjectFile> {
        &self.files
    }
    pub fn revision(&self) -> &str {
        &self.revision
    }
}
fn hash_part(hash: &mut Sha256, value: &[u8]) {
    hash.update((value.len() as u64).to_le_bytes());
    hash.update(value);
}
pub fn validate_path(path: &str) -> Result<(), TransactionError> {
    if path.len() > 240 {
        return Err(TransactionError::Limit("path bytes"));
    }
    if path.is_empty()
        || path.contains('\\')
        || path.bytes().any(|b| b < 32 || b == 127)
        || path
            .split('/')
            .any(|p| p.is_empty() || p == "." || p == "..")
    {
        return Err(TransactionError::InvalidFile(path.into()));
    }
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReplaceValue {
    pub path: String,
    pub expected_source_revision: String,
    pub record: usize,
    pub expected_span: Span,
    pub semantic_intent: String,
    pub replacement: Vec<u8>,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectPlan {
    pub version: u32,
    pub expected_project_revision: String,
    pub commands: Vec<ReplaceValue>,
}

#[derive(Clone, Debug)]
pub enum CatalogShape {
    Objects,
    Single(String),
}
#[derive(Clone, Debug)]
pub struct FilePolicy {
    pub schema: Schema,
    pub loader: LineDocumentLoader,
    pub shape: CatalogShape,
}
#[derive(Clone, Debug)]
pub struct ProjectPolicy {
    /// Exact complete inventory and destination allowlist; no implicit wildcard.
    pub files: BTreeMap<String, FilePolicy>,
    pub limits: ProjectLimits,
    pub catalog_limits: CatalogLimits,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SemanticChange {
    pub path: String,
    pub source_id: String,
    pub record: usize,
    pub span: Span,
    pub field: Vec<u8>,
    pub before: Vec<u8>,
    pub after: Vec<u8>,
    pub intent: String,
}
#[derive(Clone, Debug)]
pub struct Preview {
    original: ProjectSnapshot,
    result: ProjectSnapshot,
    validated: bool,
    pub changes: Vec<SemanticChange>,
    pub text_diff: String,
    pub diagnostics: Vec<Diagnostic>,
    pub inverse: ProjectPlan,
}
impl Preview {
    pub fn original(&self) -> &ProjectSnapshot {
        &self.original
    }
    pub fn result(&self) -> &ProjectSnapshot {
        &self.result
    }
    pub fn is_valid(&self) -> bool {
        self.validated
    }
}

#[derive(Debug)]
pub enum TransactionError {
    InvalidIdentity,
    InvalidFile(String),
    InvalidPlan,
    NonReversibleValue(String),
    Limit(&'static str),
    ProjectRevision {
        expected: String,
        actual: String,
    },
    SourceRevision {
        path: String,
        expected: String,
        actual: String,
    },
    SpanMismatch {
        path: String,
        expected: Span,
        actual: Span,
    },
    MissingPolicy(String),
    Cancelled,
    Deadline,
    Source(atrinik_source::Error),
    Catalog(atrinik_catalog::Error),
}
impl fmt::Display for TransactionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "transaction: {self:?}")
    }
}
impl std::error::Error for TransactionError {}
impl From<atrinik_source::Error> for TransactionError {
    fn from(value: atrinik_source::Error) -> Self {
        Self::Source(value)
    }
}
impl From<atrinik_catalog::Error> for TransactionError {
    fn from(value: atrinik_catalog::Error) -> Self {
        Self::Catalog(value)
    }
}

/// Validates caller-owned native plans before cloning or reporting preconditions.
pub fn validate_plan(plan: &ProjectPlan, limits: ProjectLimits) -> Result<(), TransactionError> {
    let digest = |value: &str| {
        value.len() == 64
            && value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    };
    if plan.version != 1 || !digest(&plan.expected_project_revision) {
        return Err(TransactionError::InvalidPlan);
    }
    if plan.commands.len() > limits.maximum_commands {
        return Err(TransactionError::Limit("commands"));
    }
    let mut bytes = 0usize;
    for command in &plan.commands {
        validate_path(&command.path)?;
        if !digest(&command.expected_source_revision)
            || command.expected_span.start > command.expected_span.end
            || command.semantic_intent.is_empty()
            || command.semantic_intent.len() > 1024
            || command.semantic_intent.chars().any(char::is_control)
        {
            return Err(TransactionError::InvalidPlan);
        }
        bytes = bytes
            .checked_add(command.replacement.len())
            .and_then(|n| n.checked_add(command.path.len() + command.semantic_intent.len() + 128))
            .ok_or(TransactionError::Limit("plan bytes"))?;
        if bytes > limits.maximum_diff_bytes {
            return Err(TransactionError::Limit("plan bytes"));
        }
    }
    Ok(())
}

/// Plans entirely in memory. Even invalid resulting projects return reviewable diagnostics;
/// only a valid Preview can be published by the store.
pub fn preview(
    snapshot: &ProjectSnapshot,
    plan: &ProjectPlan,
    policy: &ProjectPolicy,
    control: &Control<'_>,
) -> Result<Preview, TransactionError> {
    control.check()?;
    validate_plan(plan, policy.limits)?;
    if plan.expected_project_revision != snapshot.revision {
        return Err(TransactionError::ProjectRevision {
            expected: plan.expected_project_revision.clone(),
            actual: snapshot.revision.clone(),
        });
    }
    if plan.commands.len() > policy.limits.maximum_commands {
        return Err(TransactionError::Limit("commands"));
    }
    if !policy.files.keys().eq(snapshot.files.keys()) {
        return Err(TransactionError::MissingPolicy(
            "complete inventory differs".into(),
        ));
    }
    // Recheck the caller's current bounds before cloning the complete snapshot.
    ProjectSnapshot::new(
        snapshot.identity.clone(),
        snapshot.files.clone(),
        policy.limits,
    )?;
    let mut commands: Vec<_> = plan.commands.iter().collect();
    commands.sort_by(|a, b| (&a.path, a.record).cmp(&(&b.path, b.record)));
    let mut edits = BTreeMap::new();
    let mut changes = Vec::new();
    let mut diff = String::new();
    let mut diff_bytes = 0usize;
    for command in commands {
        control.check()?;
        validate_path(&command.path)?;
        if command
            .replacement
            .first()
            .is_some_and(|byte| matches!(byte, b' ' | b'\t'))
        {
            return Err(TransactionError::NonReversibleValue(command.path.clone()));
        }
        if command.semantic_intent.is_empty()
            || command.semantic_intent.len() > 1024
            || command.semantic_intent.chars().any(char::is_control)
        {
            return Err(TransactionError::InvalidPlan);
        }
        let file = snapshot
            .files
            .get(&command.path)
            .ok_or_else(|| TransactionError::InvalidFile(command.path.clone()))?;
        let document = &file.document;
        if command.expected_source_revision != document.revision().to_string() {
            return Err(TransactionError::SourceRevision {
                path: command.path.clone(),
                expected: command.expected_source_revision.clone(),
                actual: document.revision().to_string(),
            });
        }
        let record = document
            .records()
            .get(command.record)
            .ok_or(TransactionError::InvalidPlan)?;
        let RecordKind::Field { key, value } = record.kind else {
            return Err(TransactionError::InvalidPlan);
        };
        if value != command.expected_span {
            return Err(TransactionError::SpanMismatch {
                path: command.path.clone(),
                expected: command.expected_span,
                actual: value,
            });
        }
        let before = document.bytes(value)?;
        let field = document.bytes(key)?;
        // Hex encodes arbitrary authored bytes without lossy UTF-8 or terminal escapes.
        let required = before
            .len()
            .checked_add(command.replacement.len())
            .and_then(|n| n.checked_mul(2))
            .and_then(|n| {
                n.checked_add(
                    command.path.len() * 2
                        + field.len()
                        + command.semantic_intent.len()
                        + document.source_id().as_str().len()
                        + 128,
                )
            })
            .ok_or(TransactionError::Limit("diff bytes"))?;
        diff_bytes = diff_bytes
            .checked_add(required)
            .ok_or(TransactionError::Limit("diff bytes"))?;
        if diff_bytes > policy.limits.maximum_diff_bytes {
            return Err(TransactionError::Limit("diff bytes"));
        }
        edits
            .entry(command.path.clone())
            .or_insert_with(|| EditPlan::new(document.revision()))
            .replace_value(document, command.record, &command.replacement)?;
        use std::fmt::Write;
        writeln!(
            diff,
            "--- {}\n+++ {}\n@@ bytes {}..{} @@\n-{}\n+{}",
            command.path,
            command.path,
            value.start,
            value.end,
            hex(before),
            hex(&command.replacement)
        )
        .expect("String write");
        changes.push(SemanticChange {
            path: command.path.clone(),
            source_id: document.source_id().as_str().into(),
            record: command.record,
            span: value,
            field: field.into(),
            before: before.into(),
            after: command.replacement.clone(),
            intent: command.semantic_intent.clone(),
        });
    }
    let mut files = snapshot.files.clone();
    for (path, edit) in edits {
        control.check()?;
        let file = files.get_mut(&path).expect("checked file");
        file.document = Arc::new(edit.apply(&file.document)?);
    }
    let result = ProjectSnapshot::new(snapshot.identity.clone(), files, policy.limits)?;
    let mut diagnostics = Vec::new();
    let mut catalogs = Vec::new();
    for (path, file) in result.files() {
        control.check()?;
        let file_policy = &policy.files[path];
        let parsed = file.document.diagnostics();
        if parsed.truncated() {
            return Err(TransactionError::Limit("parse diagnostics"));
        }
        append_diagnostics(&mut diagnostics, parsed.values(), policy.limits)?;
        let checked = file_policy
            .schema
            .validate(&file.document, policy.limits.maximum_diagnostics);
        if checked.truncated() {
            return Err(TransactionError::Limit("schema diagnostics"));
        }
        append_diagnostics(&mut diagnostics, checked.values(), policy.limits)?;
        let loader = file_policy
            .loader
            .clone()
            .with_limits(policy.catalog_limits)?;
        let loaded = match &file_policy.shape {
            CatalogShape::Objects => {
                loader.load_objects(&file.document, EvidenceReferences::default())?
            }
            CatalogShape::Single(id) => {
                loader.load_single(&file.document, id.clone(), EvidenceReferences::default())?
            }
        };
        if loaded.schema_version() != result.identity.schema_version {
            return Err(TransactionError::InvalidIdentity);
        }
        catalogs.push(loaded);
    }
    control.check()?;
    let catalog = Catalog::build(
        catalogs,
        policy.catalog_limits,
        SuppressionPolicy::default(),
    )?;
    if catalog.diagnostics().truncated() {
        return Err(TransactionError::Limit("catalog diagnostics"));
    }
    append_diagnostics(
        &mut diagnostics,
        catalog.diagnostics().values(),
        policy.limits,
    )?;
    diagnostics.sort_by(|a, b| {
        (&a.location, a.code, &a.semantic_path, &a.message).cmp(&(
            &b.location,
            b.code,
            &b.semantic_path,
            &b.message,
        ))
    });
    let inverse = ProjectPlan {
        version: 1,
        expected_project_revision: result.revision.clone(),
        commands: changes
            .iter()
            .map(|change| {
                let document = &result.files[&change.path].document;
                let RecordKind::Field { value, .. } = document.records()[change.record].kind else {
                    unreachable!("value replacement preserves record kind")
                };
                ReplaceValue {
                    path: change.path.clone(),
                    expected_source_revision: document.revision().to_string(),
                    record: change.record,
                    expected_span: value,
                    semantic_intent: "undo value replacement".into(),
                    replacement: change.before.clone(),
                }
            })
            .collect(),
    };
    control.check()?;
    let validated = !diagnostics
        .iter()
        .any(|d| d.severity == atrinik_diagnostics::Severity::Error && d.is_active());
    Ok(Preview {
        original: snapshot.clone(),
        result,
        validated,
        changes,
        text_diff: diff,
        diagnostics,
        inverse,
    })
}
fn append_diagnostics(
    target: &mut Vec<Diagnostic>,
    source: &[Diagnostic],
    limits: ProjectLimits,
) -> Result<(), TransactionError> {
    if target.len().saturating_add(source.len()) > limits.maximum_diagnostics {
        return Err(TransactionError::Limit("diagnostics"));
    }
    target.extend_from_slice(source);
    Ok(())
}
fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8] = b"0123456789abcdef";
    let mut text = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        text.push(DIGITS[(b >> 4) as usize] as char);
        text.push(DIGITS[(b & 15) as usize] as char);
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    use atrinik_catalog::{Domain, FieldRule, ReferenceKind};
    use atrinik_schema::SchemaLimits;
    use atrinik_source::{Limits, SourceId};
    use std::time::Duration;

    fn setup() -> (ProjectSnapshot, ProjectPolicy, ProjectPlan) {
        let mut files = BTreeMap::new();
        let mut policies = BTreeMap::new();
        for (path, bytes) in [
            (
                "a.arc",
                &b"# untouched\r\nObject alpha\r\nname old\r\nunknown  odd\r\nend\r\n"[..],
            ),
            ("b.arc", &b"Object beta\nname second\nref alpha\nend\n"[..]),
        ] {
            let document = Arc::new(
                Document::parse(
                    SourceId::new(path).unwrap(),
                    Arc::<[u8]>::from(bytes),
                    Limits::default(),
                )
                .unwrap(),
            );
            files.insert(
                path.into(),
                ProjectFile {
                    document,
                    mode: 0o640,
                },
            );
            policies.insert(
                path.into(),
                FilePolicy {
                    schema: Schema::new("objects", [b"name".to_vec()], SchemaLimits::default())
                        .unwrap(),
                    loader: LineDocumentLoader::new(
                        Domain::Archetype,
                        "fixture",
                        1,
                        [
                            (b"name".to_vec(), FieldRule::Label),
                            (
                                b"ref".to_vec(),
                                FieldRule::Reference {
                                    domain: Domain::Archetype,
                                    kind: ReferenceKind::Archetype,
                                    optional: false,
                                },
                            ),
                        ],
                    )
                    .unwrap(),
                    shape: CatalogShape::Objects,
                },
            );
        }
        let snapshot = ProjectSnapshot::new(
            SourceIdentity {
                repository: "atrinik/content".into(),
                reference: "refs/heads/main".into(),
                revision: "a".repeat(40),
                schema_version: 1,
            },
            files,
            ProjectLimits::default(),
        )
        .unwrap();
        let doc = &snapshot.files()["a.arc"].document;
        let RecordKind::Field { value, .. } = doc.records()[2].kind else {
            panic!()
        };
        let plan = ProjectPlan {
            version: 1,
            expected_project_revision: snapshot.revision().into(),
            commands: vec![ReplaceValue {
                path: "a.arc".into(),
                expected_source_revision: doc.revision().to_string(),
                record: 2,
                expected_span: value,
                semantic_intent: "rename label".into(),
                replacement: b"new label".to_vec(),
            }],
        };
        (
            snapshot,
            ProjectPolicy {
                files: policies,
                limits: ProjectLimits::default(),
                catalog_limits: CatalogLimits::default(),
            },
            plan,
        )
    }
    fn run(
        snapshot: &ProjectSnapshot,
        plan: &ProjectPlan,
        policy: &ProjectPolicy,
    ) -> Result<Preview, TransactionError> {
        preview(
            snapshot,
            plan,
            policy,
            &Control {
                cancelled: &AtomicBool::new(false),
                deadline: Instant::now() + Duration::from_secs(5),
            },
        )
    }
    #[test]
    fn lossless_preview_inverse_and_repeat_revision() {
        let (snapshot, policy, plan) = setup();
        let preview = run(&snapshot, &plan, &policy).unwrap();
        assert!(preview.is_valid());
        assert_eq!(
            preview.result().files()["a.arc"].document.source_bytes(),
            b"# untouched\r\nObject alpha\r\nname new label\r\nunknown  odd\r\nend\r\n"
        );
        assert_eq!(
            snapshot.files()["a.arc"].document.source_bytes(),
            b"# untouched\r\nObject alpha\r\nname old\r\nunknown  odd\r\nend\r\n"
        );
        assert_eq!(
            run(preview.result(), &preview.inverse, &policy)
                .unwrap()
                .result()
                .revision(),
            snapshot.revision()
        );
        assert!(matches!(
            run(preview.result(), &plan, &policy),
            Err(TransactionError::ProjectRevision { .. })
        ));
        assert_eq!(
            preview.text_diff,
            run(&snapshot, &plan, &policy).unwrap().text_diff
        );
    }
    #[test]
    fn span_precondition_reports_both_coordinates() {
        let (snapshot, policy, mut plan) = setup();
        let actual = plan.commands[0].expected_span;
        plan.commands[0].expected_span = Span::new(0, 0);
        assert!(
            matches!(run(&snapshot, &plan, &policy), Err(TransactionError::SpanMismatch { expected, actual: found, .. }) if expected == Span::new(0, 0) && found == actual)
        );
    }

    #[test]
    fn native_plans_reject_unbounded_revision_metadata_before_preconditions() {
        let (snapshot, policy, mut plan) = setup();
        plan.expected_project_revision = "a".repeat(10_000);
        assert!(matches!(
            run(&snapshot, &plan, &policy),
            Err(TransactionError::InvalidPlan)
        ));
        assert!(crate::json::encode_plan(&plan).is_err());
        plan.expected_project_revision = snapshot.revision().into();
        plan.commands[0].expected_source_revision = "a".repeat(10_000);
        assert!(matches!(
            run(&snapshot, &plan, &policy),
            Err(TransactionError::InvalidPlan)
        ));
    }

    #[test]
    fn whitespace_boundary_and_empty_values_have_safe_undo() {
        let (snapshot, policy, mut plan) = setup();
        plan.commands[0].replacement = b" leading".to_vec();
        assert!(matches!(
            run(&snapshot, &plan, &policy),
            Err(TransactionError::NonReversibleValue(_))
        ));
        plan.commands[0].replacement.clear();
        let changed = run(&snapshot, &plan, &policy).unwrap();
        let restored = run(changed.result(), &changed.inverse, &policy).unwrap();
        assert_eq!(restored.result().revision(), snapshot.revision());
    }

    #[test]
    fn rejects_preconditions_conflicts_and_limits() {
        let (snapshot, mut policy, mut plan) = setup();
        plan.commands.push(plan.commands[0].clone());
        assert!(matches!(
            run(&snapshot, &plan, &policy),
            Err(TransactionError::Source(
                atrinik_source::Error::OverlappingEdits
            ))
        ));
        plan.commands.pop();
        policy.limits.maximum_diff_bytes = 1;
        assert!(matches!(
            run(&snapshot, &plan, &policy),
            Err(TransactionError::Limit(_))
        ));
        policy.limits = ProjectLimits::default();
        plan.commands[0].expected_source_revision = "0".repeat(64);
        assert!(matches!(
            run(&snapshot, &plan, &policy),
            Err(TransactionError::SourceRevision { .. })
        ));
        for path in ["../a", "/a", "a//b", "a/./b", "a\\b"] {
            assert!(validate_path(path).is_err());
        }
    }
    #[test]
    fn complete_project_reference_validation_and_cancellation() {
        let (snapshot, policy, mut plan) = setup();
        let doc = &snapshot.files()["b.arc"].document;
        let RecordKind::Field { value, .. } = doc.records()[2].kind else {
            panic!()
        };
        plan.commands = vec![ReplaceValue {
            path: "b.arc".into(),
            expected_source_revision: doc.revision().to_string(),
            record: 2,
            expected_span: value,
            semantic_intent: "change reference".into(),
            replacement: b"absent".to_vec(),
        }];
        let previewed = run(&snapshot, &plan, &policy).unwrap();
        assert!(!previewed.is_valid());
        assert!(
            previewed
                .diagnostics
                .iter()
                .any(|d| d.code == "catalog.missing_reference")
        );
        assert!(matches!(
            preview(
                &snapshot,
                &plan,
                &policy,
                &Control {
                    cancelled: &AtomicBool::new(true),
                    deadline: Instant::now() + Duration::from_secs(5)
                }
            ),
            Err(TransactionError::Cancelled)
        ));
        assert!(matches!(
            preview(
                &snapshot,
                &plan,
                &policy,
                &Control {
                    cancelled: &AtomicBool::new(false),
                    deadline: Instant::now()
                }
            ),
            Err(TransactionError::Deadline)
        ));
    }
}
