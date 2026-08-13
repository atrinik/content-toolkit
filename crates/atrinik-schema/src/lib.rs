// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::{collections::BTreeSet, fmt};

use atrinik_diagnostics::{Diagnostic, DiagnosticSet, Location, Severity, Span};
use atrinik_source::Document;

pub const FOUNDATION_ARTIFACT_SCHEMA: &str =
    include_str!("../schemas/foundation-artifact.schema.json");

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Schema {
    name: String,
    required_fields: BTreeSet<Vec<u8>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SchemaLimits {
    pub maximum_name_bytes: usize,
    pub maximum_fields: usize,
    pub maximum_field_bytes: usize,
}

impl Default for SchemaLimits {
    fn default() -> Self {
        Self {
            maximum_name_bytes: 128,
            maximum_fields: 4096,
            maximum_field_bytes: 256,
        }
    }
}

impl Schema {
    pub fn new(
        name: impl AsRef<str>,
        required_fields: impl IntoIterator<Item = Vec<u8>>,
        limits: SchemaLimits,
    ) -> Result<Self, Error> {
        let name = name.as_ref();
        if name.is_empty() || name.len() > limits.maximum_name_bytes || name.contains('\0') {
            return Err(Error::InvalidName);
        }
        let mut fields = BTreeSet::new();
        for (processed, field) in required_fields.into_iter().enumerate() {
            if processed >= limits.maximum_fields {
                return Err(Error::LimitExceeded);
            }
            if field.is_empty() || field.len() > limits.maximum_field_bytes || field.contains(&0) {
                return Err(Error::InvalidField);
            }
            fields.insert(field);
        }
        Ok(Self {
            name: name.to_owned(),
            required_fields: fields,
        })
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
                diagnostics.push(
                    Diagnostic::new(
                        "schema.required_field",
                        Severity::Error,
                        Location::new(document.source_id().as_str(), Span::new(0, 0)),
                        "a required field is absent",
                    )
                    .with_semantic_path([String::from_utf8_lossy(required).into_owned()])
                    .with_fix_hint("add the required field"),
                );
            }
        }
        diagnostics
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    InvalidName,
    InvalidField,
    LimitExceeded,
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "invalid content schema: {self:?}")
    }
}

impl std::error::Error for Error {}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use atrinik_source::{Document, Limits, SourceId};

    use super::{Schema, SchemaLimits};

    #[test]
    fn reports_required_fields_deterministically() {
        assert!(super::FOUNDATION_ARTIFACT_SCHEMA.contains("foundation-artifact-v1"));
        let document = Document::parse(
            SourceId::new("fixture:schema").unwrap(),
            Arc::<[u8]>::from(&b"name value\n"[..]),
            Limits::default(),
        )
        .unwrap();
        let schema = Schema::new(
            "object",
            [b"name".to_vec(), b"type".to_vec()],
            SchemaLimits::default(),
        )
        .unwrap();
        let diagnostics = schema.validate(&document, 8);
        assert_eq!(diagnostics.values().len(), 1);
    }
}
