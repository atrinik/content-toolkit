// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT
//! Strict, versioned JSON adapter shared by the CLI and automation consumers.
use crate::{Preview, ProjectLimits, ProjectPlan, ReplaceValue, TransactionError, validate_plan};
use atrinik_diagnostics::Span;
use serde::{Deserialize, Serialize};
use serde_json::json;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Plan {
    version: u32,
    expected_project_revision: String,
    commands: Vec<Command>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Command {
    path: String,
    expected_source_revision: String,
    record: usize,
    expected_span: Range,
    semantic_intent: String,
    replacement: Vec<u8>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Range {
    start: usize,
    end: usize,
}

pub fn decode_plan(bytes: &[u8], limits: ProjectLimits) -> Result<ProjectPlan, TransactionError> {
    if bytes.len() > limits.maximum_diff_bytes {
        return Err(TransactionError::Limit("plan JSON bytes"));
    }
    let plan: Plan = serde_json::from_slice(bytes).map_err(|_| TransactionError::InvalidPlan)?;
    if plan.version != 1
        || plan.commands.len() > limits.maximum_commands
        || !revision(&plan.expected_project_revision)
        || plan.commands.iter().any(|c| {
            !revision(&c.expected_source_revision) || c.expected_span.start > c.expected_span.end
        })
    {
        return Err(TransactionError::InvalidPlan);
    }
    let plan = ProjectPlan {
        version: plan.version,
        expected_project_revision: plan.expected_project_revision,
        commands: plan
            .commands
            .into_iter()
            .map(|c| ReplaceValue {
                path: c.path,
                expected_source_revision: c.expected_source_revision,
                record: c.record,
                expected_span: Span::new(c.expected_span.start, c.expected_span.end),
                semantic_intent: c.semantic_intent,
                replacement: c.replacement,
            })
            .collect(),
    };
    validate_plan(&plan, limits)?;
    Ok(plan)
}
fn revision(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn encode_plan(plan: &ProjectPlan) -> Result<Vec<u8>, TransactionError> {
    validate_plan(plan, ProjectLimits::default())?;
    bounded_json(&wire_plan(plan))
}
fn wire_plan(plan: &ProjectPlan) -> Plan {
    Plan {
        version: plan.version,
        expected_project_revision: plan.expected_project_revision.clone(),
        commands: plan
            .commands
            .iter()
            .map(|c| Command {
                path: c.path.clone(),
                expected_source_revision: c.expected_source_revision.clone(),
                record: c.record,
                expected_span: Range {
                    start: c.expected_span.start,
                    end: c.expected_span.end,
                },
                semantic_intent: c.semantic_intent.clone(),
                replacement: c.replacement.clone(),
            })
            .collect(),
    }
}
pub fn encode_preview(preview: &Preview) -> Result<Vec<u8>, TransactionError> {
    validate_preview_output(preview)?;
    let changes: Vec<_> = preview.changes.iter().map(|c| json!({"path":c.path,"source_id":c.source_id,"record":c.record,"span":{"start":c.span.start,"end":c.span.end},"field":c.field,"before":c.before,"after":c.after,"semantic_intent":c.intent})).collect();
    let diagnostics: Vec<_> = preview.diagnostics.iter().map(|d| json!({"code":d.code,"severity":format!("{:?}",d.severity).to_lowercase(),"source_id":d.location.source,"span":{"start":d.location.span.start,"end":d.location.span.end},"semantic_path":d.semantic_path,"message":d.message,"suppressed":d.suppressed,"suppressible":d.suppressible,"fix_hint":d.fix_hint,"related":d.related.iter().map(|r|json!({"source_id":r.location.source,"span":{"start":r.location.span.start,"end":r.location.span.end},"message":r.message})).collect::<Vec<_>>()})).collect();
    bounded_json(
        &json!({"version":1,"status":if preview.is_valid(){"valid"}else{"invalid"},"dry_run":true,"original_revision":preview.original().revision(),"result_revision":preview.result().revision(),"changes":changes,"text_diff":preview.text_diff,"diagnostics":diagnostics,"inverse":wire_plan(&preview.inverse)}),
    )
}

fn validate_preview_output(preview: &Preview) -> Result<(), TransactionError> {
    let limits = ProjectLimits::default();
    validate_plan(&preview.inverse, limits)?;
    if preview.changes.len() > limits.maximum_commands
        || preview.diagnostics.len() > limits.maximum_diagnostics
    {
        return Err(TransactionError::Limit("JSON output records"));
    }
    let mut total = preview.text_diff.len();
    let mut add = |bytes: usize| {
        total = total
            .checked_add(bytes)
            .ok_or(TransactionError::Limit("JSON output bytes"))?;
        if total > limits.maximum_diff_bytes {
            return Err(TransactionError::Limit("JSON output bytes"));
        }
        Ok(())
    };
    add(0)?;
    for change in &preview.changes {
        for size in [
            change.path.len(),
            change.source_id.len(),
            change.field.len(),
            change.before.len(),
            change.after.len(),
            change.intent.len(),
            128,
        ] {
            add(size)?;
        }
    }
    for diagnostic in &preview.diagnostics {
        if diagnostic.related.len() > 16 || diagnostic.semantic_path.len() > 32 {
            return Err(TransactionError::Limit("JSON diagnostic records"));
        }
        for size in [
            diagnostic.code.len(),
            diagnostic.location.source.len(),
            diagnostic.message.len(),
            diagnostic.fix_hint.as_ref().map_or(0, String::len),
        ] {
            add(size)?;
        }
        for part in &diagnostic.semantic_path {
            add(part.len())?;
        }
        for related in &diagnostic.related {
            add(related.location.source.len())?;
            add(related.message.len())?;
        }
    }
    Ok(())
}

struct BoundedOutput(Vec<u8>);
impl std::io::Write for BoundedOutput {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > ProjectLimits::default().maximum_diff_bytes {
            return Err(std::io::Error::other("JSON output limit"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn bounded_json(value: &impl Serialize) -> Result<Vec<u8>, TransactionError> {
    let mut output = BoundedOutput(Vec::new());
    serde_json::to_writer(&mut output, value)
        .map_err(|_| TransactionError::Limit("JSON output bytes"))?;
    Ok(output.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_bounded_json() {
        let good = br#"{"version":1,"expected_project_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","commands":[]}"#;
        assert!(decode_plan(good, ProjectLimits::default()).is_ok());
        for bad in [br#"{"version":1,"version":1,"expected_project_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","commands":[]}"#.as_slice(), br#"{"version":1,"expected_project_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","commands":[],"apply":true}"#, br#"{"version":2,"expected_project_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","commands":[]}"#] { assert!(decode_plan(bad,ProjectLimits::default()).is_err()); }
        assert!(
            decode_plan(
                good,
                ProjectLimits {
                    maximum_diff_bytes: 1,
                    ..ProjectLimits::default()
                }
            )
            .is_err()
        );
    }
}
