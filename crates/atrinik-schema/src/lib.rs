// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::collections::BTreeSet;

use atrinik_diagnostics::{Diagnostic, DiagnosticSet, Severity, Span};
use atrinik_source::Document;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Schema {
    name: String,
    required_fields: BTreeSet<Vec<u8>>,
}

impl Schema {
    #[must_use]
    pub fn new(
        name: impl Into<String>,
        required_fields: impl IntoIterator<Item = Vec<u8>>,
    ) -> Self {
        Self {
            name: name.into(),
            required_fields: required_fields.into_iter().collect(),
        }
    }

    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    #[must_use]
    pub fn validate(&self, document: &Document, maximum_diagnostics: usize) -> DiagnosticSet {
        let present: BTreeSet<&[u8]> = document.fields().map(|(key, _)| key).collect();
        let mut diagnostics = DiagnosticSet::new(maximum_diagnostics);
        for required in &self.required_fields {
            if !present.contains(required.as_slice()) {
                diagnostics.push(Diagnostic {
                    code: "schema.required_field",
                    severity: Severity::Error,
                    span: Span::new(0, 0),
                    message: "a required field is absent",
                });
            }
        }
        diagnostics
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use atrinik_source::{Document, Limits, SourceId};

    use super::Schema;

    #[test]
    fn reports_required_fields_deterministically() {
        let document = Document::parse(
            SourceId::new("fixture:schema").unwrap(),
            Arc::<[u8]>::from(&b"name value\n"[..]),
            Limits::default(),
        )
        .unwrap();
        let schema = Schema::new("object", [b"name".to_vec(), b"type".to_vec()]);
        let diagnostics = schema.validate(&document, 8);
        assert_eq!(diagnostics.values().len(), 1);
    }
}
