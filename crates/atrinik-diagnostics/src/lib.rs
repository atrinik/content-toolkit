// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::fmt;

#[derive(Clone, Copy, Debug, Default, Eq, Ord, PartialEq, PartialOrd)]
pub struct Span {
    pub start: usize,
    pub end: usize,
}

impl Span {
    #[must_use]
    pub const fn new(start: usize, end: usize) -> Self {
        Self { start, end }
    }

    #[must_use]
    pub const fn len(self) -> usize {
        self.end.saturating_sub(self.start)
    }

    #[must_use]
    pub const fn is_empty(self) -> bool {
        self.start >= self.end
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum Severity {
    Warning,
    Error,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Diagnostic {
    pub code: &'static str,
    pub severity: Severity,
    pub span: Span,
    pub message: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DiagnosticSet {
    maximum: usize,
    values: Vec<Diagnostic>,
    truncated: bool,
}

impl DiagnosticSet {
    #[must_use]
    pub fn new(maximum: usize) -> Self {
        Self {
            maximum,
            values: Vec::with_capacity(maximum.min(64)),
            truncated: false,
        }
    }

    pub fn push(&mut self, diagnostic: Diagnostic) {
        if self.values.len() < self.maximum {
            self.values.push(diagnostic);
        } else {
            self.truncated = true;
        }
    }

    #[must_use]
    pub fn values(&self) -> &[Diagnostic] {
        &self.values
    }

    #[must_use]
    pub const fn truncated(&self) -> bool {
        self.truncated
    }

    #[must_use]
    pub fn has_errors(&self) -> bool {
        self.values
            .iter()
            .any(|diagnostic| diagnostic.severity == Severity::Error)
    }
}

impl fmt::Display for Diagnostic {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} {:?} at {}..{}: {}",
            self.code, self.severity, self.span.start, self.span.end, self.message
        )
    }
}

#[cfg(test)]
mod tests {
    use super::{Diagnostic, DiagnosticSet, Severity, Span};

    #[test]
    fn bounds_diagnostics_without_reordering() {
        let mut diagnostics = DiagnosticSet::new(1);
        let value = Diagnostic {
            code: "test",
            severity: Severity::Error,
            span: Span::new(1, 2),
            message: "test diagnostic",
        };
        diagnostics.push(value.clone());
        diagnostics.push(value.clone());
        assert_eq!(diagnostics.values(), &[value]);
        assert!(diagnostics.truncated());
        assert!(diagnostics.has_errors());
    }
}
