// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT
#![forbid(unsafe_code)]

use atrinik_catalog::{
    Catalog, CatalogId, Definition, Domain, EvidenceReferences, Query, Resolution,
};
use atrinik_diagnostics::SuppressionPolicy;
use atrinik_transaction::project::{self, CatalogShape, Control, ProjectPolicy, ProjectSnapshot};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

pub const MAX_REQUEST: usize = 16 * 1024;
pub const MAX_OUTPUT: usize = 64 * 1024;
pub const ROUTINE_OUTPUT: usize = 32 * 1024;
pub const MAX_PAGE: usize = 50;
const MAX_PREVIEW_REPLACEMENT: usize = 4096;
pub const SCHEMA_VERSION: &str = "atrinik-content-mcp/v1";

#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Identity {
    pub repository: String,
    pub branch: String,
    pub main_base_commit: String,
    pub commit: String,
    pub worktree: String,
    pub source_role: String,
    pub view_role: String,
    pub dirty_fingerprint: Option<String>,
    pub authorization: String,
    pub manifest: String,
    pub profile: String,
    pub registry: String,
    pub schema_version: u32,
    pub provider_version: String,
}
fn hex(value: &str, len: usize) -> bool {
    value.len() == len && value.bytes().all(|b| b.is_ascii_hexdigit())
}
fn safe_text(value: &str, maximum: usize) -> bool {
    !value.is_empty() && value.len() <= maximum && !value.chars().any(char::is_control)
}
impl Identity {
    pub fn validate(&self) -> Result<(), Error> {
        if self.repository != "atrinik/content"
            || !hex(&self.commit, 40)
            || !hex(&self.main_base_commit, 40)
            || self.schema_version != 1
            || self.provider_version != SCHEMA_VERSION
            || !matches!(self.source_role.as_str(), "main" | "review")
            || !matches!(self.view_role.as_str(), "replacement" | "classic")
            || (self.source_role == "main" && self.branch != "refs/heads/main")
            || (self.source_role == "review" && !self.branch.starts_with("refs/heads/"))
            || self.dirty_fingerprint.as_ref().is_some_and(|v| !hex(v, 64))
            || [
                &self.branch,
                &self.worktree,
                &self.authorization,
                &self.manifest,
                &self.profile,
                &self.registry,
            ]
            .iter()
            .any(|v| !safe_text(v, 128))
        {
            return Err(Error::InvalidIdentity);
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum Error {
    InvalidArguments,
    InvalidIdentity,
    UnsupportedSchema,
    Limit,
    StaleCursor,
    Missing,
    Incomplete,
    Cancelled,
    Timeout,
    Internal,
}
impl Error {
    pub const fn code(self) -> &'static str {
        match self {
            Self::InvalidArguments => "INVALID_ARGUMENT",
            Self::InvalidIdentity => "STALE_COORDINATE",
            Self::UnsupportedSchema => "UNSUPPORTED_OPERATION",
            Self::Limit => "LIMIT_EXCEEDED",
            Self::StaleCursor => "STALE_CURSOR",
            Self::Missing => "INCOMPLETE",
            Self::Incomplete => "INCOMPLETE",
            Self::Cancelled => "CANCELLED",
            Self::Timeout => "TIMEOUT",
            Self::Internal => "INTERNAL",
        }
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.code())
    }
}
impl std::error::Error for Error {}

/// The caller must validate selected commits against its configured main ancestry.
/// This adapter accepts immutable canonical project snapshots, never caller paths.
pub struct Snapshot {
    identity: Identity,
    project: ProjectSnapshot,
    policy: ProjectPolicy,
    catalog: Catalog,
    fingerprint: String,
}
impl Snapshot {
    pub fn new(
        identity: Identity,
        project: ProjectSnapshot,
        mut policy: ProjectPolicy,
    ) -> Result<Self, Error> {
        identity.validate()?;
        if project.identity().repository != identity.repository
            || project.identity().revision != identity.commit
            || project.identity().schema_version != identity.schema_version
        {
            return Err(Error::InvalidIdentity);
        }
        if project.files().len() > 1000 || !project.files().keys().eq(policy.files.keys()) {
            return Err(Error::Limit);
        }
        policy.catalog_limits.maximum_definitions = 1000;
        policy.catalog_limits.maximum_query_terms = 1000;
        policy.catalog_limits.maximum_graph_work = 1000;
        policy.limits.maximum_diff_bytes = 32 * 1024;
        policy.limits.maximum_commands = 100;
        let mut docs = Vec::new();
        for (path, file) in project.files() {
            if file.document.source_bytes().len() > 256 * 1024
                || std::str::from_utf8(file.document.source_bytes()).is_err()
                || file
                    .document
                    .source_bytes()
                    .iter()
                    .any(|b| *b < 32 && !matches!(*b, 9 | 10 | 13))
            {
                return Err(Error::Incomplete);
            }
            let selected = &policy.files[path];
            let doc = match &selected.shape {
                CatalogShape::Objects => selected
                    .loader
                    .load_objects(&file.document, EvidenceReferences::default()),
                CatalogShape::Single(id) => selected.loader.load_single(
                    &file.document,
                    id.clone(),
                    EvidenceReferences::default(),
                ),
            }
            .map_err(|_| Error::Incomplete)?;
            if doc.schema_version() != identity.schema_version {
                return Err(Error::UnsupportedSchema);
            }
            docs.push(doc);
        }
        let catalog = Catalog::build(docs, policy.catalog_limits, SuppressionPolicy::default())
            .map_err(|_| Error::Incomplete)?;
        let fingerprint = digest(&json!([
            identity,
            project.revision(),
            catalog.generation().to_string()
        ]))?;
        Ok(Self {
            identity,
            project,
            policy,
            catalog,
            fingerprint,
        })
    }
    pub fn identity(&self) -> &Identity {
        &self.identity
    }
    pub fn catalog(&self) -> &Catalog {
        &self.catalog
    }
    pub fn project(&self) -> &ProjectSnapshot {
        &self.project
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    Search,
    Inspect,
    References,
    Impact,
    Validate,
    Compare,
    Preview,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub selector: String,
    pub operation: Operation,
    #[serde(default)]
    pub identity: Option<String>,
    #[serde(default)]
    pub query: Option<String>,
    #[serde(default)]
    pub domain: Option<String>,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub field: Option<String>,
    #[serde(default)]
    pub cursor: Option<String>,
    #[serde(default = "page_default")]
    pub limit: usize,
    #[serde(default = "depth_default")]
    pub depth: usize,
    #[serde(default)]
    pub compare_selector: Option<String>,
    #[serde(default)]
    pub plan: Option<Value>,
}
const fn page_default() -> usize {
    50
}
const fn depth_default() -> usize {
    1
}
pub struct Provider {
    snapshots: BTreeMap<String, Snapshot>,
}
impl Provider {
    pub fn new(snapshots: Vec<Snapshot>) -> Result<Self, Error> {
        if snapshots.is_empty() || snapshots.len() > 128 {
            return Err(Error::Limit);
        }
        let mut values = BTreeMap::new();
        let mut bytes = 0usize;
        for snapshot in snapshots {
            bytes = bytes.saturating_add(
                snapshot
                    .project
                    .files()
                    .values()
                    .map(|f| f.document.source_bytes().len())
                    .sum::<usize>(),
            );
            if bytes > 8 * 1024 * 1024
                || values
                    .insert(snapshot.identity.worktree.clone(), snapshot)
                    .is_some()
            {
                return Err(Error::Limit);
            }
        }
        Ok(Self { snapshots: values })
    }
    pub fn parse_request(bytes: &[u8]) -> Result<Request, Error> {
        if bytes.len() > MAX_REQUEST {
            return Err(Error::Limit);
        }
        serde_json::from_slice(bytes).map_err(|_| Error::InvalidArguments)
    }
    pub fn call(&self, request: Request, cancelled: &AtomicBool) -> Result<Value, Error> {
        self.call_with_deadline(request, cancelled, Instant::now() + Duration::from_secs(5))
    }
    pub fn call_with_deadline(
        &self,
        request: Request,
        cancelled: &AtomicBool,
        deadline: Instant,
    ) -> Result<Value, Error> {
        let deadline = deadline.min(Instant::now() + Duration::from_secs(5));
        check(cancelled, deadline)?;
        if serde_json::to_vec(&request)
            .map_err(|_| Error::Internal)?
            .len()
            > MAX_REQUEST
            || request.limit == 0
            || request.limit > MAX_PAGE
            || request.depth == 0
            || request.depth > 8
        {
            return Err(Error::Limit);
        }
        for v in [
            &request.query,
            &request.identity,
            &request.domain,
            &request.path,
            &request.field,
            &request.compare_selector,
        ]
        .into_iter()
        .flatten()
        {
            if !safe_text(v, 1024) {
                return Err(Error::InvalidArguments);
            }
        }
        let snapshot = self
            .snapshots
            .get(&request.selector)
            .ok_or(Error::InvalidIdentity)?;
        let mut parameters = request.clone();
        parameters.cursor = None;
        let binding = digest(&json!([
            snapshot.fingerprint,
            parameters,
            self.snapshots
                .get(request.compare_selector.as_deref().unwrap_or(""))
                .map(|s| &s.fingerprint)
        ]))?;
        let offset = decode_cursor(request.cursor.as_deref(), &binding)?;
        let id = request.identity.as_deref().map(parse_id).transpose()?;
        let mut records = Vec::new();
        let mut incomplete = false;
        match request.operation {
            Operation::Search => {
                let domain = request.domain.as_deref().map(parse_domain).transpose()?;
                let query = Query {
                    domain,
                    text: request.query.clone(),
                    ..Query::default()
                };
                for definition in snapshot
                    .catalog
                    .search(&query, 1000)
                    .map_err(|_| Error::Limit)?
                {
                    check(cancelled, deadline)?;
                    if let Some(path) = &request.path
                        && definition_path(snapshot, definition)? != path
                    {
                        continue;
                    }
                    if let Some(field) = &request.field
                        && entity_fields(snapshot, definition, Some(field))?.is_empty()
                    {
                        continue;
                    }
                    records.push(record(snapshot, definition)?);
                }
            }
            Operation::Inspect => {
                if let Some(id) = &id {
                    let definition = resolve(snapshot, id)?;
                    records.push(record(snapshot, definition)?);
                    records.extend(entity_fields(
                        snapshot,
                        definition,
                        request.field.as_deref(),
                    )?);
                } else if let Some(path) = &request.path {
                    let file = snapshot.project.files().get(path).ok_or(Error::Missing)?;
                    for (index, item) in file.document.records().iter().enumerate() {
                        check(cancelled, deadline)?;
                        if let atrinik_source::RecordKind::Field { key, value } = item.kind {
                            let name = file.document.bytes(key).map_err(|_| Error::Incomplete)?;
                            if request.field.as_ref().is_some_and(|f| f.as_bytes() != name) {
                                continue;
                            }
                            records.push(json!({"path":path,"record":index,"field_hex":bytes_hex(name),"value_hex":bytes_hex(file.document.bytes(value).map_err(|_|Error::Incomplete)?),"span":{"start":value.start,"end":value.end},"source_revision":file.document.revision().to_string()}));
                            if records.len() > 1000 {
                                return Err(Error::Limit);
                            }
                        }
                    }
                } else {
                    return Err(Error::InvalidArguments);
                }
            }
            Operation::References | Operation::Impact => {
                let start = id.as_ref().ok_or(Error::InvalidArguments)?;
                resolve(snapshot, start)?;
                let mut queue = VecDeque::from([(start.clone(), 0usize)]);
                let mut seen = BTreeSet::from([start.clone()]);
                let reverse = matches!(request.operation, Operation::Impact);
                let mut edges = 0usize;
                while let Some((current, depth)) = queue.pop_front() {
                    check(cancelled, deadline)?;
                    if depth >= request.depth {
                        continue;
                    }
                    let targets: Vec<_> = if reverse {
                        snapshot.catalog.dependents(&current).cloned().collect()
                    } else {
                        let definition = resolve(snapshot, &current)?;
                        definition
                            .references
                            .iter()
                            .chain(definition.inherits.iter())
                            .map(|r| r.target.clone())
                            .collect()
                    };
                    for target in targets {
                        edges += 1;
                        if edges > 1000 {
                            return Err(Error::Limit);
                        }
                        match snapshot.catalog.resolve(&target) {
                            Resolution::Found(value) => {
                                records.push(json!({"from":current.to_string(),"to":target.to_string(),"depth":depth+1,"entity":record(snapshot,value)?}));
                                if seen.insert(target.clone()) {
                                    queue.push_back((target, depth + 1));
                                }
                            }
                            _ => {
                                incomplete = true;
                                records.push(json!({"from":current.to_string(),"to":target.to_string(),"error":"INCOMPLETE"}));
                            }
                        }
                    }
                }
            }
            Operation::Validate => {
                let plan = project::ProjectPlan {
                    version: 1,
                    expected_project_revision: snapshot.project.revision().into(),
                    commands: vec![],
                };
                let preview = project::preview(
                    &snapshot.project,
                    &plan,
                    &snapshot.policy,
                    &Control {
                        cancelled,
                        deadline,
                    },
                )
                .map_err(transaction_error)?;
                for diagnostic in preview.diagnostics {
                    let path = project_path(snapshot, &diagnostic.location.source)?;
                    if request
                        .path
                        .as_ref()
                        .is_none_or(|requested| path == requested)
                    {
                        records.push(json!({"code":diagnostic.code,"severity":format!("{:?}",diagnostic.severity),"path":path,"span":{"start":diagnostic.location.span.start,"end":diagnostic.location.span.end}}));
                    }
                }
            }
            Operation::Compare => {
                let other = self
                    .snapshots
                    .get(
                        request
                            .compare_selector
                            .as_deref()
                            .ok_or(Error::InvalidArguments)?,
                    )
                    .ok_or(Error::InvalidIdentity)?;
                let keys: BTreeSet<_> = snapshot
                    .catalog
                    .definitions()
                    .chain(other.catalog.definitions())
                    .map(|d| d.id.clone())
                    .collect();
                if keys.len() > 1000 {
                    return Err(Error::Limit);
                }
                for key in keys {
                    check(cancelled, deadline)?;
                    if id.as_ref().is_some_and(|id| id != &key) {
                        continue;
                    }
                    if let Some(path) = &request.path {
                        let before_matches = match snapshot.catalog.resolve(&key) {
                            Resolution::Found(definition) => {
                                definition_path(snapshot, definition)? == path
                            }
                            _ => false,
                        };
                        let after_matches = match other.catalog.resolve(&key) {
                            Resolution::Found(definition) => {
                                definition_path(other, definition)? == path
                            }
                            _ => false,
                        };
                        if !before_matches && !after_matches {
                            continue;
                        }
                    }
                    let before_fields = match snapshot.catalog.resolve(&key) {
                        Resolution::Found(d) => entity_fields(snapshot, d, None)?,
                        _ => vec![],
                    };
                    let after_fields = match other.catalog.resolve(&key) {
                        Resolution::Found(d) => entity_fields(other, d, None)?,
                        _ => vec![],
                    };
                    let before = match snapshot.catalog.resolve(&key) {
                        Resolution::Found(d) => Some(record(snapshot, d)?),
                        _ => None,
                    };
                    let after = match other.catalog.resolve(&key) {
                        Resolution::Found(d) => Some(record(other, d)?),
                        _ => None,
                    };
                    let equal = match (snapshot.catalog.resolve(&key), other.catalog.resolve(&key))
                    {
                        (Resolution::Found(a), Resolution::Found(b)) => {
                            a == b
                                && before_fields
                                    .iter()
                                    .map(|v| (&v["field_hex"], &v["value_hex"]))
                                    .eq(after_fields
                                        .iter()
                                        .map(|v| (&v["field_hex"], &v["value_hex"])))
                        }
                        (Resolution::Missing, Resolution::Missing) => true,
                        _ => false,
                    };
                    if !equal {
                        records.push(json!({"identity":key.to_string(),"before":before,"after":after,"before_identity":snapshot.identity,"after_identity":other.identity,"before_fields":before_fields,"after_fields":after_fields}));
                    }
                }
            }
            Operation::Preview => {
                let plan_value = request.plan.as_ref().ok_or(Error::InvalidArguments)?;
                if plan_value
                    .get("commands")
                    .and_then(Value::as_array)
                    .is_some_and(|commands| {
                        commands.iter().any(|command| {
                            command
                                .get("replacement")
                                .and_then(Value::as_array)
                                .is_some_and(|replacement| {
                                    replacement.len() > MAX_PREVIEW_REPLACEMENT
                                })
                        })
                    })
                {
                    return Err(Error::Limit);
                }
                let bytes = serde_json::to_vec(plan_value).map_err(|_| Error::InvalidArguments)?;
                let plan = atrinik_transaction::json::decode_plan(&bytes, snapshot.policy.limits)
                    .map_err(|_| Error::InvalidArguments)?;
                let preview = project::preview(
                    &snapshot.project,
                    &plan,
                    &snapshot.policy,
                    &Control {
                        cancelled,
                        deadline,
                    },
                )
                .map_err(transaction_error)?;
                let diagnostics = preview
                    .diagnostics
                    .iter()
                    .map(|diagnostic| {
                        Ok(json!({
                            "code": diagnostic.code,
                            "path": project_path(snapshot, &diagnostic.location.source)?,
                        }))
                    })
                    .collect::<Result<Vec<_>, Error>>()?;
                records.push(json!({"valid":preview.is_valid(),"text_diff":preview.text_diff,"changes":preview.changes.iter().map(|c|json!({"path":c.path,"record":c.record,"field_hex":bytes_hex(&c.field),"before_hex":bytes_hex(&c.before),"after_hex":bytes_hex(&c.after)})).collect::<Vec<_>>(),"diagnostics":diagnostics}));
            }
        }
        if records.len() > 1000 {
            return Err(Error::Limit);
        }
        if offset > records.len() {
            return Err(Error::StaleCursor);
        }
        let end = (offset + request.limit).min(records.len());
        let next = (end < records.len()).then(|| encode_cursor(end, &binding));
        let result = json!({"schema":SCHEMA_VERSION,"identity":snapshot.identity,"generation":snapshot.catalog.generation().to_string(),"project_revision":snapshot.project.revision(),"freshness":if snapshot.identity.dirty_fingerprint.is_some(){"mutable-zero-ttl"}else{"immutable"},"classification":"untrusted-data","records":&records[offset..end],"next_cursor":next,"truncated":end<records.len(),"incomplete":incomplete});
        check(cancelled, deadline)?;
        if serde_json::to_vec(&result)
            .map_err(|_| Error::Internal)?
            .len()
            > ROUTINE_OUTPUT.min(MAX_OUTPUT)
        {
            return Err(Error::Limit);
        }
        Ok(result)
    }
}
fn transaction_error(error: project::TransactionError) -> Error {
    match error {
        project::TransactionError::Cancelled => Error::Cancelled,
        project::TransactionError::Deadline => Error::Timeout,
        project::TransactionError::Limit(_) => Error::Limit,
        _ => Error::InvalidArguments,
    }
}
fn check(cancelled: &AtomicBool, deadline: Instant) -> Result<(), Error> {
    if cancelled.load(Ordering::Acquire) {
        Err(Error::Cancelled)
    } else if Instant::now() >= deadline {
        Err(Error::Timeout)
    } else {
        Ok(())
    }
}
fn parse_domain(text: &str) -> Result<Domain, Error> {
    Domain::ALL
        .into_iter()
        .find(|d| d.as_str() == text)
        .ok_or(Error::UnsupportedSchema)
}
fn parse_id(text: &str) -> Result<CatalogId, Error> {
    let (domain, tail) = text.split_once(':').ok_or(Error::InvalidArguments)?;
    let (namespace, local) = tail.split_once('/').ok_or(Error::InvalidArguments)?;
    CatalogId::new(parse_domain(domain)?, namespace, local).map_err(|_| Error::InvalidArguments)
}
fn resolve<'a>(snapshot: &'a Snapshot, id: &CatalogId) -> Result<&'a Definition, Error> {
    match snapshot.catalog.resolve(id) {
        Resolution::Found(d) => Ok(d),
        _ => Err(Error::Missing),
    }
}
fn entity_fields(
    snapshot: &Snapshot,
    definition: &Definition,
    field: Option<&str>,
) -> Result<Vec<Value>, Error> {
    let (path, file) = snapshot
        .project
        .files()
        .iter()
        .find(|(_, file)| file.document.source_id().as_str() == definition.location.source)
        .ok_or(Error::Incomplete)?;
    let single = matches!(snapshot.policy.files[path].shape, CatalogShape::Single(_));
    let mut active = single;
    let mut nesting = 0usize;
    let mut values = Vec::new();
    for (index, record) in file.document.records().iter().enumerate() {
        match record.kind {
            atrinik_source::RecordKind::ObjectStart { name } => {
                if name == definition.location.span {
                    active = true;
                    nesting = 1;
                } else if active {
                    nesting += 1;
                }
            }
            atrinik_source::RecordKind::ObjectEnd => {
                if active && !single {
                    nesting = nesting.saturating_sub(1);
                    if nesting == 0 {
                        break;
                    }
                }
            }
            atrinik_source::RecordKind::Field { key, value }
                if active && (single || nesting == 1) =>
            {
                let name = file.document.bytes(key).map_err(|_| Error::Incomplete)?;
                if field.is_some_and(|field| field.as_bytes() != name) {
                    continue;
                }
                values.push(json!({"path":path,"record":index,"field_hex":bytes_hex(name),"value_hex":bytes_hex(file.document.bytes(value).map_err(|_|Error::Incomplete)?),"span":{"start":value.start,"end":value.end},"source_revision":file.document.revision().to_string()}));
                if values.len() > 1000 {
                    return Err(Error::Limit);
                }
            }
            _ => {}
        }
    }
    Ok(values)
}
fn definition_path<'a>(snapshot: &'a Snapshot, definition: &Definition) -> Result<&'a str, Error> {
    project_path(snapshot, &definition.location.source)
}
fn project_path<'a>(snapshot: &'a Snapshot, source_id: &str) -> Result<&'a str, Error> {
    snapshot
        .project
        .files()
        .iter()
        .find(|(_, file)| file.document.source_id().as_str() == source_id)
        .map(|(path, _)| path.as_str())
        .ok_or(Error::Incomplete)
}
fn record(snapshot: &Snapshot, d: &Definition) -> Result<Value, Error> {
    let preview = snapshot.catalog.preview(&d.id);
    Ok(
        json!({"identity":d.id.to_string(),"type":d.id.domain().as_str(),"path":definition_path(snapshot,d)?,"span":{"start":d.location.span.start,"end":d.location.span.end},"label":preview.and_then(|p|p.label.as_ref()),"summary":preview.and_then(|p|p.summary.as_ref()),"resource":format!("atrinik://content/{}/{}/{}",snapshot.identity.commit,snapshot.fingerprint,d.id),"license":d.evidence.license,"provenance":d.evidence.provenance}),
    )
}
fn digest(value: &Value) -> Result<String, Error> {
    Ok(bytes_hex(&Sha256::digest(
        serde_json::to_vec(value).map_err(|_| Error::Internal)?,
    )))
}
fn encode_cursor(offset: usize, binding: &str) -> String {
    let body = format!("v1:{offset}:{binding}");
    format!("{body}:{}", bytes_hex(&Sha256::digest(body.as_bytes())))
}
fn decode_cursor(cursor: Option<&str>, binding: &str) -> Result<usize, Error> {
    let Some(cursor) = cursor else { return Ok(0) };
    if cursor.len() > 160 {
        return Err(Error::StaleCursor);
    }
    let parts: Vec<_> = cursor.split(':').collect();
    if parts.len() != 4 || parts[0] != "v1" || parts[2] != binding {
        return Err(Error::StaleCursor);
    }
    let offset = parts[1].parse().map_err(|_| Error::StaleCursor)?;
    if offset > 1000 || encode_cursor(offset, binding) != cursor {
        return Err(Error::StaleCursor);
    }
    Ok(offset)
}
fn bytes_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn input_schema() -> Value {
    serde_json::from_str(include_str!("../schemas/input.schema.json"))
        .expect("compiled input schema")
}
pub fn output_schema() -> Value {
    serde_json::from_str(include_str!("../schemas/output.schema.json"))
        .expect("compiled output schema")
}

#[cfg(test)]
mod tests {
    use super::*;
    use atrinik_catalog::{CatalogLimits, FieldRule, LineDocumentLoader};
    use atrinik_schema::{Schema, SchemaLimits};
    use atrinik_source::{Document, Limits, SourceId};
    use project::{FilePolicy, ProjectFile, ProjectLimits, SourceIdentity};
    use std::sync::Arc;
    fn fixture(worktree: &str, commit: &str) -> Snapshot {
        let mut bytes = String::new();
        for index in 0..300 {
            bytes.push_str(&format!(
                "Object item{index:03}\nname Display {index}\nend\n"
            ));
        }
        fixture_bytes(worktree, commit, bytes, Domain::Archetype)
    }
    fn fixture_bytes(worktree: &str, commit: &str, bytes: String, domain: Domain) -> Snapshot {
        fixture_with_source_path(worktree, commit, bytes, domain, "items.arc", "items.arc")
    }
    fn fixture_with_source_path(
        worktree: &str,
        commit: &str,
        bytes: String,
        domain: Domain,
        path: &str,
        source_id: &str,
    ) -> Snapshot {
        let doc = Arc::new(
            Document::parse(
                SourceId::new(source_id).unwrap(),
                Arc::<[u8]>::from(bytes.into_bytes()),
                Limits::default(),
            )
            .unwrap(),
        );
        let project = ProjectSnapshot::new(
            SourceIdentity {
                repository: "atrinik/content".into(),
                reference: "refs/heads/main".into(),
                revision: commit.into(),
                schema_version: 1,
            },
            BTreeMap::from([(
                path.into(),
                ProjectFile {
                    document: doc,
                    mode: 0o644,
                },
            )]),
            ProjectLimits::default(),
        )
        .unwrap();
        let policy = ProjectPolicy {
            files: BTreeMap::from([(
                path.into(),
                FilePolicy {
                    schema: Schema::new("objects", [b"name".to_vec()], SchemaLimits::default())
                        .unwrap(),
                    loader: LineDocumentLoader::new(
                        domain,
                        "fixture",
                        1,
                        [
                            (b"name".to_vec(), FieldRule::Label),
                            (
                                b"ref".to_vec(),
                                FieldRule::Reference {
                                    domain,
                                    kind: atrinik_catalog::ReferenceKind::Generic,
                                    optional: false,
                                },
                            ),
                        ],
                    )
                    .unwrap(),
                    shape: CatalogShape::Objects,
                },
            )]),
            limits: ProjectLimits::default(),
            catalog_limits: CatalogLimits::default(),
        };
        Snapshot::new(
            Identity {
                repository: "atrinik/content".into(),
                branch: "refs/heads/main".into(),
                main_base_commit: commit.into(),
                commit: commit.into(),
                worktree: worktree.into(),
                source_role: "main".into(),
                view_role: "replacement".into(),
                dirty_fingerprint: None,
                authorization: "public".into(),
                manifest: "v1".into(),
                profile: "default".into(),
                registry: "v1".into(),
                schema_version: 1,
                provider_version: SCHEMA_VERSION.into(),
            },
            project,
            policy,
        )
        .unwrap()
    }
    fn request() -> Request {
        Provider::parse_request(br#"{"selector":"fixture","operation":"search"}"#).unwrap()
    }
    #[test]
    fn canonical_parity_and_complete_300_record_pagination() {
        let snapshot = fixture("fixture", &"a".repeat(40));
        let expected: Vec<_> = snapshot
            .catalog()
            .definitions()
            .map(|d| d.id.to_string())
            .collect();
        let provider = Provider::new(vec![snapshot]).unwrap();
        let mut request = request();
        let mut found = Vec::new();
        loop {
            let result = provider
                .call(request.clone(), &AtomicBool::new(false))
                .unwrap();
            assert!(result["records"].as_array().unwrap().len() <= 50);
            for record in result["records"].as_array().unwrap() {
                found.push(record["identity"].as_str().unwrap().to_string());
            }
            request.cursor = result["next_cursor"].as_str().map(str::to_string);
            if request.cursor.is_none() {
                break;
            }
        }
        assert_eq!(found, expected);
        assert_eq!(found.len(), 300);
    }
    #[test]
    fn strict_bounds_cancellation_and_snapshot_cursor_binding() {
        assert!(Provider::parse_request(br#"{"selector":"fixture","operation":"apply"}"#).is_err());
        assert!(
            Provider::parse_request(
                br#"{"selector":"fixture","operation":"search","root":"/etc"}"#
            )
            .is_err()
        );
        let provider = Provider::new(vec![
            fixture("fixture", &"a".repeat(40)),
            fixture("other", &"b".repeat(40)),
        ])
        .unwrap();
        let mut req = request();
        let page = provider.call(req.clone(), &AtomicBool::new(false)).unwrap();
        req.cursor = page["next_cursor"].as_str().map(str::to_string);
        req.selector = "other".into();
        assert_eq!(
            provider.call(req, &AtomicBool::new(false)),
            Err(Error::StaleCursor)
        );
        assert_eq!(
            provider.call(request(), &AtomicBool::new(true)),
            Err(Error::Cancelled)
        );
        let mut req = request();
        req.limit = 51;
        assert_eq!(
            provider.call(req, &AtomicBool::new(false)),
            Err(Error::Limit)
        );
        let mut req = request();
        req.query = Some("evil\nvalue".into());
        assert_eq!(
            provider.call(req, &AtomicBool::new(false)),
            Err(Error::InvalidArguments)
        );
        assert!(
            serde_json::to_vec(&input_schema()).unwrap().len()
                + serde_json::to_vec(&output_schema()).unwrap().len()
                < 32 * 1024
        );
    }
    #[test]
    fn preview_and_inspect_preserve_source_bytes() {
        let snapshot = fixture("fixture", &"a".repeat(40));
        let original = snapshot.project().files()["items.arc"]
            .document
            .source_bytes()
            .to_vec();
        let revision = snapshot.project().revision().to_string();
        let provider = Provider::new(vec![snapshot]).unwrap();
        let mut req = request();
        req.operation = Operation::Preview;
        req.plan = Some(json!({"version":1,"expected_project_revision":revision,"commands":[]}));
        let result = provider.call(req, &AtomicBool::new(false)).unwrap();
        assert_eq!(result["records"][0]["valid"], true);
        assert_eq!(
            provider.snapshots["fixture"].project().files()["items.arc"]
                .document
                .source_bytes(),
            original
        );
        let mut req = request();
        req.operation = Operation::Inspect;
        req.path = Some("items.arc".into());
        let result = provider.call(req, &AtomicBool::new(false)).unwrap();
        assert_eq!(result["records"].as_array().unwrap().len(), 50);
        assert!(result["records"][0]["span"]["end"].is_number());
    }
    #[test]
    fn preview_enforces_replacement_schema_item_limit() {
        let snapshot = fixture_bytes(
            "fixture",
            &"a".repeat(40),
            "Object item\nname x\nend\n".into(),
            Domain::Archetype,
        );
        let file = &snapshot.project().files()["items.arc"];
        let record = &file.document.records()[1];
        let atrinik_source::RecordKind::Field { value, .. } = record.kind else {
            panic!("fixture field record");
        };
        let revision = snapshot.project().revision().to_string();
        let source_revision = file.document.revision().to_string();
        let provider = Provider::new(vec![snapshot]).unwrap();
        let plan = |replacement: Vec<u8>| {
            json!({
                "version": 1,
                "expected_project_revision": revision,
                "commands": [{
                    "path": "items.arc",
                    "expected_source_revision": source_revision,
                    "record": 1,
                    "expected_span": {"start": value.start, "end": value.end},
                    "semantic_intent": "exercise replacement bound",
                    "replacement": replacement,
                }],
            })
        };

        let mut preview_request = request();
        preview_request.operation = Operation::Preview;
        preview_request.plan = Some(plan(vec![b'a'; MAX_PREVIEW_REPLACEMENT]));
        assert!(
            provider
                .call(preview_request, &AtomicBool::new(false))
                .is_ok()
        );

        let mut preview_request = request();
        preview_request.operation = Operation::Preview;
        preview_request.plan = Some(plan(vec![b'a'; MAX_PREVIEW_REPLACEMENT + 1]));
        assert_eq!(
            provider.call(preview_request, &AtomicBool::new(false)),
            Err(Error::Limit)
        );
    }
    #[test]
    fn project_paths_resolve_from_distinct_source_ids() {
        let path = "content/items.arc";
        let source_id = "logical-items-source";
        let before = fixture_with_source_path(
            "fixture",
            &"a".repeat(40),
            "Object item\nname Before\nunknown original\nend\n".into(),
            Domain::Archetype,
            path,
            source_id,
        );
        let after = fixture_with_source_path(
            "after",
            &"b".repeat(40),
            "Object item\nname Before\nunknown changed\nend\n".into(),
            Domain::Archetype,
            path,
            source_id,
        );
        let provider = Provider::new(vec![before, after]).unwrap();

        let mut search_request = request();
        search_request.path = Some(path.into());
        let result = provider
            .call(search_request, &AtomicBool::new(false))
            .unwrap();
        assert_eq!(result["records"].as_array().unwrap().len(), 1);
        assert_eq!(result["records"][0]["path"], path);

        let mut compare_request = request();
        compare_request.operation = Operation::Compare;
        compare_request.compare_selector = Some("after".into());
        compare_request.path = Some(path.into());
        let result = provider
            .call(compare_request, &AtomicBool::new(false))
            .unwrap();
        assert_eq!(result["records"].as_array().unwrap().len(), 1);
        assert_eq!(result["records"][0]["before"]["path"], path);
        assert_eq!(result["records"][0]["after"]["path"], path);

        let mut inspect_request = request();
        inspect_request.operation = Operation::Inspect;
        inspect_request.identity = Some("archetype:fixture/item".into());
        inspect_request.path = Some(path.into());
        let result = provider
            .call(inspect_request, &AtomicBool::new(false))
            .unwrap();
        assert!(!result["records"].as_array().unwrap().is_empty());
        assert!(
            result["records"]
                .as_array()
                .unwrap()
                .iter()
                .all(|record| record["path"] == path)
        );
    }
    #[test]
    #[ignore = "explicit offline benchmark; run release with --ignored --nocapture"]
    fn warm_query_benchmark() {
        let commit = "a".repeat(40);
        let provider = Provider::new(vec![fixture("fixture", &commit)]).unwrap();
        let mut req = request();
        req.query = Some("Display 123".into());
        let start = Instant::now();
        for _ in 0..30 {
            std::hint::black_box(provider.call(req.clone(), &AtomicBool::new(false)).unwrap());
        }
        let warm = start.elapsed();
        let start = Instant::now();
        for _ in 0..30 {
            let cold = Provider::new(vec![fixture("fixture", &commit)]).unwrap();
            std::hint::black_box(cold.call(req.clone(), &AtomicBool::new(false)).unwrap());
        }
        let cold = start.elapsed();
        println!(
            "{{\"iterations\":30,\"warm_ns\":{},\"cold_index_and_query_ns\":{},\"external_network\":false,\"records\":300}}",
            warm.as_nanos(),
            cold.as_nanos()
        );
        assert!(
            warm < cold,
            "warm canonical-index reuse must beat rebuilding canonical index"
        );
    }

    #[test]
    fn known_answers_cover_all_canonical_domains() {
        for domain in Domain::ALL {
            let snapshot = fixture_bytes(
                "fixture",
                &"a".repeat(40),
                "Object synthetic_id\nname Display text\nend\n".into(),
                domain,
            );
            let expected = snapshot
                .catalog()
                .definitions()
                .next()
                .unwrap()
                .id
                .to_string();
            let provider = Provider::new(vec![snapshot]).unwrap();
            let mut req = request();
            req.operation = Operation::Inspect;
            req.identity = Some(expected.clone());
            let result = provider.call(req, &AtomicBool::new(false)).unwrap();
            assert_eq!(result["records"][0]["identity"], expected);
            assert_eq!(result["records"][0]["label"], "Display text");
        }
    }
    #[test]
    fn graph_direction_depth_and_unknown_field_comparison() {
        let bytes = "Object a\nname Alpha\nref b\nunknown original\nend\nObject b\nname Beta\nref c\nend\nObject c\nname Gamma\nend\n";
        let before = fixture_bytes("fixture", &"a".repeat(40), bytes.into(), Domain::Map);
        let after = fixture_bytes(
            "after",
            &"b".repeat(40),
            bytes.replace("original", "changed"),
            Domain::Map,
        );
        let provider = Provider::new(vec![before, after]).unwrap();
        let mut req = request();
        req.operation = Operation::References;
        req.identity = Some("map:fixture/a".into());
        req.depth = 2;
        let result = provider.call(req, &AtomicBool::new(false)).unwrap();
        assert_eq!(result["records"].as_array().unwrap().len(), 2);
        assert_eq!(result["records"][1]["to"], "map:fixture/c");
        let mut req = request();
        req.operation = Operation::Impact;
        req.identity = Some("map:fixture/c".into());
        req.depth = 2;
        let result = provider.call(req, &AtomicBool::new(false)).unwrap();
        assert_eq!(result["records"][1]["to"], "map:fixture/a");
        let mut req = request();
        req.operation = Operation::Compare;
        req.compare_selector = Some("after".into());
        req.identity = Some("map:fixture/a".into());
        let result = provider.call(req, &AtomicBool::new(false)).unwrap();
        assert_eq!(result["records"].as_array().unwrap().len(), 1);
        assert_ne!(
            result["records"][0]["before_fields"],
            result["records"][0]["after_fields"]
        );
    }
    #[test]
    fn stale_effective_parameters_timeout_and_unsupported_schema() {
        let provider = Provider::new(vec![fixture("fixture", &"a".repeat(40))]).unwrap();
        let req = request();
        let page = provider.call(req.clone(), &AtomicBool::new(false)).unwrap();
        let mut changed = req.clone();
        changed.cursor = page["next_cursor"].as_str().map(str::to_string);
        changed.limit = 25;
        assert_eq!(
            provider.call(changed, &AtomicBool::new(false)),
            Err(Error::StaleCursor)
        );
        assert_eq!(
            provider.call_with_deadline(req, &AtomicBool::new(false), Instant::now()),
            Err(Error::Timeout)
        );
        let mut req = request();
        req.domain = Some("unknown-format".into());
        assert_eq!(
            provider.call(req, &AtomicBool::new(false)),
            Err(Error::UnsupportedSchema)
        );
        let mut req = request();
        req.query = Some("x".repeat(1025));
        assert_eq!(
            provider.call(req, &AtomicBool::new(false)),
            Err(Error::InvalidArguments)
        );
        assert!(Provider::parse_request(&vec![b' '; MAX_REQUEST + 1]).is_err());
    }
    #[test]
    fn common_error_codes_and_routine_output_ceiling() {
        assert_eq!(Error::InvalidArguments.code(), "INVALID_ARGUMENT");
        assert_eq!(Error::InvalidIdentity.code(), "STALE_COORDINATE");
        assert_eq!(Error::UnsupportedSchema.code(), "UNSUPPORTED_OPERATION");
        assert_eq!(Error::Limit.code(), "LIMIT_EXCEEDED");
        assert_eq!(Error::StaleCursor.code(), "STALE_CURSOR");
        assert_eq!(Error::Incomplete.code(), "INCOMPLETE");
        assert_eq!(Error::Cancelled.code(), "CANCELLED");
        assert_eq!(Error::Timeout.code(), "TIMEOUT");
        assert_eq!(Error::Internal.code(), "INTERNAL");
        let bytes = format!(
            "Object a\nname label\nunknown {}\nend\n",
            "x".repeat(17 * 1024)
        );
        let snapshot = fixture_bytes("fixture", &"a".repeat(40), bytes, Domain::Archetype);
        let provider = Provider::new(vec![snapshot]).unwrap();
        let mut req = request();
        req.operation = Operation::Inspect;
        req.path = Some("items.arc".into());
        assert_eq!(
            provider.call(req, &AtomicBool::new(false)),
            Err(Error::Limit)
        );
    }
}
