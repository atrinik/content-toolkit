// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT
//! Strict, versioned JSON adapter shared by the CLI and automation consumers.
use atrinik_diagnostics::Span;
use serde::{Deserialize, Serialize};
use serde_json::json;
use crate::{Preview, ProjectLimits, ProjectPlan, ReplaceValue, TransactionError};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Plan { version: u32, expected_project_revision: String, commands: Vec<Command> }
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Command { path: String, expected_source_revision: String, record: usize, expected_span: Range, semantic_intent: String, replacement: Vec<u8> }
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Range { start: usize, end: usize }

pub fn decode_plan(bytes: &[u8], limits: ProjectLimits) -> Result<ProjectPlan, TransactionError> {
    if bytes.len() > limits.maximum_diff_bytes { return Err(TransactionError::Limit("plan JSON bytes")); }
    let plan: Plan = serde_json::from_slice(bytes).map_err(|_| TransactionError::InvalidPlan)?;
    if plan.version != 1 || plan.commands.len() > limits.maximum_commands { return Err(TransactionError::InvalidPlan); }
    Ok(ProjectPlan { version: plan.version, expected_project_revision: plan.expected_project_revision, commands: plan.commands.into_iter().map(|c| ReplaceValue { path: c.path, expected_source_revision: c.expected_source_revision, record: c.record, expected_span: Span::new(c.expected_span.start,c.expected_span.end), semantic_intent: c.semantic_intent, replacement: c.replacement }).collect() })
}
pub fn encode_plan(plan: &ProjectPlan) -> Result<Vec<u8>, TransactionError> {
    serde_json::to_vec(&wire_plan(plan)).map_err(|_| TransactionError::InvalidPlan)
}
fn wire_plan(plan: &ProjectPlan) -> Plan {
    Plan { version: plan.version, expected_project_revision: plan.expected_project_revision.clone(), commands: plan.commands.iter().map(|c| Command { path: c.path.clone(), expected_source_revision: c.expected_source_revision.clone(), record: c.record, expected_span: Range { start: c.expected_span.start,end:c.expected_span.end }, semantic_intent:c.semantic_intent.clone(),replacement:c.replacement.clone() }).collect() }
}
pub fn encode_preview(preview: &Preview) -> Result<Vec<u8>, TransactionError> {
    let changes: Vec<_> = preview.changes.iter().map(|c| json!({"path":c.path,"source_id":c.source_id,"record":c.record,"span":{"start":c.span.start,"end":c.span.end},"field":c.field,"before":c.before,"after":c.after,"semantic_intent":c.intent})).collect();
    let diagnostics: Vec<_> = preview.diagnostics.iter().map(|d| json!({"code":d.code,"severity":format!("{:?}",d.severity).to_lowercase(),"source_id":d.location.source,"span":{"start":d.location.span.start,"end":d.location.span.end},"semantic_path":d.semantic_path,"message":d.message,"suppressed":d.suppressed})).collect();
    serde_json::to_vec(&json!({"version":1,"status":if preview.is_valid(){"valid"}else{"invalid"},"dry_run":true,"original_revision":preview.original().revision(),"result_revision":preview.result().revision(),"changes":changes,"text_diff":preview.text_diff,"diagnostics":diagnostics,"inverse":wire_plan(&preview.inverse)})).map_err(|_| TransactionError::InvalidPlan)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_bounded_json() {
        let good = br#"{"version":1,"expected_project_revision":"abc","commands":[]}"#;
        assert!(decode_plan(good,ProjectLimits::default()).is_ok());
        for bad in [br#"{"version":1,"version":1,"expected_project_revision":"abc","commands":[]}"#.as_slice(), br#"{"version":1,"expected_project_revision":"abc","commands":[],"apply":true}"#, br#"{"version":2,"expected_project_revision":"abc","commands":[]}"#] { assert!(decode_plan(bad,ProjectLimits::default()).is_err()); }
        assert!(decode_plan(good,ProjectLimits { maximum_diff_bytes: 1,..ProjectLimits::default() }).is_err());
    }
}
