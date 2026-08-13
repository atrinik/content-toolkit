// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::{collections::BTreeSet, fmt};

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
    Info,
    Warning,
    Error,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct Location {
    pub source: String,
    pub span: Span,
}

impl Location {
    #[must_use]
    pub fn new(source: impl Into<String>, span: Span) -> Self {
        Self {
            source: source.into(),
            span,
        }
    }
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct RelatedLocation {
    pub location: Location,
    pub message: String,
}

impl RelatedLocation {
    #[must_use]
    pub fn new(location: Location, message: impl Into<String>) -> Self {
        Self {
            location,
            message: message.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Diagnostic {
    pub code: &'static str,
    pub severity: Severity,
    pub location: Location,
    pub related: Vec<RelatedLocation>,
    pub semantic_path: Vec<String>,
    pub message: String,
    pub fix_hint: Option<String>,
    pub suppressible: bool,
    pub suppressed: bool,
}

impl Diagnostic {
    #[must_use]
    pub fn new(
        code: &'static str,
        severity: Severity,
        location: Location,
        message: impl Into<String>,
    ) -> Self {
        debug_assert!(valid_code(code));
        Self {
            code,
            severity,
            location,
            related: Vec::new(),
            semantic_path: Vec::new(),
            message: message.into(),
            fix_hint: None,
            suppressible: false,
            suppressed: false,
        }
    }

    #[must_use]
    pub fn with_related(mut self, related: RelatedLocation) -> Self {
        self.related.push(related);
        self
    }

    #[must_use]
    pub fn with_semantic_path(mut self, path: impl IntoIterator<Item = impl Into<String>>) -> Self {
        self.semantic_path = path.into_iter().map(Into::into).collect();
        self
    }

    #[must_use]
    pub fn with_fix_hint(mut self, hint: impl Into<String>) -> Self {
        self.fix_hint = Some(hint.into());
        self
    }

    #[must_use]
    pub const fn suppressible(mut self, suppressible: bool) -> Self {
        self.suppressible = suppressible;
        self
    }

    #[must_use]
    pub const fn is_active(&self) -> bool {
        !self.suppressed
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DiagnosticLimits {
    pub maximum_diagnostics: usize,
    pub maximum_related: usize,
    pub maximum_semantic_depth: usize,
    pub maximum_text_bytes: usize,
}

impl Default for DiagnosticLimits {
    fn default() -> Self {
        Self {
            maximum_diagnostics: 256,
            maximum_related: 16,
            maximum_semantic_depth: 32,
            maximum_text_bytes: 4096,
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct SuppressionPolicy {
    codes: BTreeSet<String>,
}

impl SuppressionPolicy {
    pub fn new(
        codes: impl IntoIterator<Item = impl Into<String>>,
        maximum_codes: usize,
        maximum_code_bytes: usize,
    ) -> Result<Self, SuppressionError> {
        let mut accepted = BTreeSet::new();
        for (index, code) in codes.into_iter().enumerate() {
            if index >= maximum_codes {
                return Err(SuppressionError::LimitExceeded);
            }
            let code = code.into();
            if code.len() > maximum_code_bytes || !valid_code(&code) {
                return Err(SuppressionError::InvalidCode);
            }
            accepted.insert(code);
        }
        Ok(Self { codes: accepted })
    }

    #[must_use]
    pub fn is_suppressed(&self, code: &str) -> bool {
        self.codes.contains(code)
    }

    pub fn codes(&self) -> impl Iterator<Item = &str> {
        self.codes.iter().map(String::as_str)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SuppressionError {
    InvalidCode,
    LimitExceeded,
}

impl fmt::Display for SuppressionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidCode => write!(formatter, "suppression code is invalid"),
            Self::LimitExceeded => write!(formatter, "suppression code limit is exceeded"),
        }
    }
}

impl std::error::Error for SuppressionError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DiagnosticSet {
    limits: DiagnosticLimits,
    values: Vec<Diagnostic>,
    truncated: bool,
    omitted_error: bool,
    suppressed_slots: BTreeSet<usize>,
    warning_slots: BTreeSet<usize>,
}

impl DiagnosticSet {
    #[must_use]
    pub fn new(maximum: usize) -> Self {
        Self::with_limits(DiagnosticLimits {
            maximum_diagnostics: maximum,
            ..DiagnosticLimits::default()
        })
    }

    #[must_use]
    pub fn with_limits(limits: DiagnosticLimits) -> Self {
        Self {
            limits,
            values: Vec::with_capacity(limits.maximum_diagnostics.min(64)),
            truncated: false,
            omitted_error: false,
            suppressed_slots: BTreeSet::new(),
            warning_slots: BTreeSet::new(),
        }
    }

    pub fn push(&mut self, diagnostic: Diagnostic) {
        self.push_with_policy(diagnostic, &SuppressionPolicy::default());
    }

    pub fn push_with_policy(&mut self, mut diagnostic: Diagnostic, policy: &SuppressionPolicy) {
        diagnostic.suppressed = diagnostic.suppressible && policy.is_suppressed(diagnostic.code);
        if diagnostic.related.len() > self.limits.maximum_related {
            diagnostic.related.truncate(self.limits.maximum_related);
            self.truncated = true;
        }
        if diagnostic.semantic_path.len() > self.limits.maximum_semantic_depth {
            diagnostic
                .semantic_path
                .truncate(self.limits.maximum_semantic_depth);
            self.truncated = true;
        }
        if truncate_utf8(
            &mut diagnostic.location.source,
            self.limits.maximum_text_bytes,
        ) | truncate_utf8(&mut diagnostic.message, self.limits.maximum_text_bytes)
        {
            self.truncated = true;
        }
        for related in &mut diagnostic.related {
            if truncate_utf8(&mut related.location.source, self.limits.maximum_text_bytes)
                | truncate_utf8(&mut related.message, self.limits.maximum_text_bytes)
            {
                self.truncated = true;
            }
        }
        for segment in &mut diagnostic.semantic_path {
            if truncate_utf8(segment, self.limits.maximum_text_bytes) {
                self.truncated = true;
            }
        }
        if let Some(hint) = diagnostic.fix_hint.as_mut()
            && truncate_utf8(hint, self.limits.maximum_text_bytes)
        {
            self.truncated = true;
        }
        if self.values.len() >= self.limits.maximum_diagnostics {
            self.truncated = true;
            if diagnostic.suppressed {
                return;
            }
            let replacement = self.suppressed_slots.last().copied().or_else(|| {
                (diagnostic.severity == Severity::Error)
                    .then(|| self.warning_slots.last().copied())
                    .flatten()
            });
            let Some(position) = replacement else {
                if diagnostic.severity == Severity::Error {
                    self.omitted_error = true;
                }
                return;
            };
            // Once a lower-priority tier is interleaved with retained values,
            // compact that entire tier. Subsequent lower-priority values are
            // appended at the end and can be evicted without repeated shifts.
            if position + 1 == self.values.len() {
                self.values.pop();
            } else if self.values[position].suppressed {
                self.values.retain(|value| !value.suppressed);
            } else {
                self.values
                    .retain(|value| value.suppressed || value.severity == Severity::Error);
            }
            self.values.push(diagnostic);
            self.rebuild_slots();
            return;
        }
        let position = self.values.len();
        self.values.push(diagnostic);
        self.record_slot(position);
    }

    fn record_slot(&mut self, position: usize) {
        let value = &self.values[position];
        if value.suppressed {
            self.suppressed_slots.insert(position);
        } else if value.severity != Severity::Error {
            self.warning_slots.insert(position);
        }
    }

    fn rebuild_slots(&mut self) {
        self.suppressed_slots.clear();
        self.warning_slots.clear();
        for (position, value) in self.values.iter().enumerate() {
            if value.suppressed {
                self.suppressed_slots.insert(position);
            } else if value.severity != Severity::Error {
                self.warning_slots.insert(position);
            }
        }
    }

    pub fn mark_truncated(&mut self) {
        self.truncated = true;
    }

    #[must_use]
    pub fn values(&self) -> &[Diagnostic] {
        &self.values
    }

    pub fn active_values(&self) -> impl Iterator<Item = &Diagnostic> {
        self.values
            .iter()
            .filter(|diagnostic| diagnostic.is_active())
    }

    #[must_use]
    pub const fn truncated(&self) -> bool {
        self.truncated
    }

    #[must_use]
    pub fn has_errors(&self) -> bool {
        self.omitted_error
            || self
                .active_values()
                .any(|diagnostic| diagnostic.severity == Severity::Error)
    }
}

impl fmt::Display for Diagnostic {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} {:?} at {}:{}..{}: {}",
            self.code,
            self.severity,
            self.location.source,
            self.location.span.start,
            self.location.span.end,
            self.message
        )?;
        if let Some(hint) = &self.fix_hint {
            write!(formatter, " (hint: {hint})")?;
        }
        if self.suppressed {
            write!(formatter, " [suppressed]")?;
        }
        Ok(())
    }
}

fn valid_code(code: &str) -> bool {
    let mut segments = code.split('.');
    let Some(first) = segments.next() else {
        return false;
    };
    valid_code_segment(first) && segments.all(valid_code_segment)
}

fn valid_code_segment(segment: &str) -> bool {
    !segment.is_empty()
        && segment.as_bytes()[0].is_ascii_lowercase()
        && segment
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
}

fn truncate_utf8(value: &mut String, maximum: usize) -> bool {
    if value.len() <= maximum {
        return false;
    }
    let mut boundary = maximum;
    while !value.is_char_boundary(boundary) {
        boundary -= 1;
    }
    value.truncate(boundary);
    true
}

#[cfg(test)]
mod tests {
    use super::{
        Diagnostic, DiagnosticLimits, DiagnosticSet, Location, RelatedLocation, Severity, Span,
        SuppressionError, SuppressionPolicy,
    };

    fn value(code: &'static str) -> Diagnostic {
        Diagnostic::new(
            code,
            Severity::Error,
            Location::new("fixture:test", Span::new(1, 2)),
            "test diagnostic",
        )
    }

    #[test]
    fn bounds_diagnostics_without_reordering() {
        let mut diagnostics = DiagnosticSet::new(1);
        let value = value("test.error");
        diagnostics.push(value.clone());
        diagnostics.push(value.clone());
        assert_eq!(diagnostics.values(), &[value]);
        assert!(diagnostics.truncated());
        assert!(diagnostics.has_errors());
    }

    #[test]
    fn preserves_structured_context_with_deterministic_bounds() {
        let mut diagnostics = DiagnosticSet::with_limits(DiagnosticLimits {
            maximum_diagnostics: 2,
            maximum_related: 1,
            maximum_semantic_depth: 1,
            maximum_text_bytes: 8,
        });
        diagnostics.push(
            value("catalog.missing")
                .with_related(RelatedLocation::new(
                    Location::new("fixture:related-long", Span::new(3, 4)),
                    "first related message",
                ))
                .with_related(RelatedLocation::new(
                    Location::new("fixture:second", Span::new(5, 6)),
                    "second",
                ))
                .with_semantic_path(["references", "target"])
                .with_fix_hint("define the target"),
        );
        let value = &diagnostics.values()[0];
        assert_eq!(value.location.source, "fixture:");
        assert_eq!(value.related.len(), 1);
        assert_eq!(value.related[0].location.source, "fixture:");
        assert_eq!(value.semantic_path, ["referenc"]);
        assert_eq!(value.fix_hint.as_deref(), Some("define t"));
        assert!(diagnostics.truncated());
    }

    #[test]
    fn suppresses_only_diagnostics_that_explicitly_allow_it() {
        let policy = SuppressionPolicy::new(["catalog.missing"], 4, 64).unwrap();
        let mut diagnostics = DiagnosticSet::new(4);
        diagnostics.push_with_policy(value("catalog.missing").suppressible(true), &policy);
        diagnostics.push_with_policy(value("catalog.conflict"), &policy);
        assert!(diagnostics.values()[0].suppressed);
        assert!(!diagnostics.values()[1].suppressed);
        assert!(diagnostics.has_errors());
        assert_eq!(diagnostics.active_values().count(), 1);
    }

    #[test]
    fn active_error_displaces_a_suppressed_warning_at_capacity() {
        let policy = SuppressionPolicy::new(["catalog.optional"], 4, 64).unwrap();
        let mut diagnostics = DiagnosticSet::new(1);
        diagnostics.push_with_policy(value("catalog.optional").suppressible(true), &policy);
        diagnostics.push_with_policy(value("catalog.required"), &policy);
        assert_eq!(diagnostics.values()[0].code, "catalog.required");
        assert!(diagnostics.has_errors());
        assert!(diagnostics.truncated());
    }

    #[test]
    fn active_error_displaces_warning_or_fails_closed_at_zero_capacity() {
        let mut diagnostics = DiagnosticSet::new(2);
        let mut warning = value("catalog.warning");
        warning.severity = Severity::Warning;
        diagnostics.push(warning);
        diagnostics.push(value("catalog.second"));
        diagnostics.push(value("catalog.required"));
        assert_eq!(diagnostics.values()[0].code, "catalog.second");
        assert_eq!(diagnostics.values()[1].code, "catalog.required");
        assert!(diagnostics.has_errors());

        let mut zero = DiagnosticSet::new(0);
        zero.push(value("catalog.required"));
        assert!(zero.values().is_empty());
        assert!(zero.has_errors());
        assert!(zero.truncated());
    }

    #[test]
    fn sustained_error_overflow_remains_bounded_and_fails_closed() {
        let mut diagnostics = DiagnosticSet::new(2);
        for _ in 0..10_000 {
            diagnostics.push(value("catalog.required"));
        }
        assert_eq!(diagnostics.values().len(), 2);
        assert!(diagnostics.has_errors());
        assert!(diagnostics.truncated());
    }

    #[test]
    fn warning_then_error_overflow_preserves_producer_order_without_repeated_shifts() {
        const MAXIMUM: usize = 4096;
        let mut diagnostics = DiagnosticSet::new(MAXIMUM);
        for index in 0..MAXIMUM {
            let mut warning = value("catalog.warning");
            warning.severity = Severity::Warning;
            warning.message = format!("warning-{index}");
            diagnostics.push(warning);
        }
        for index in 0..MAXIMUM {
            let mut error = value("catalog.required");
            error.message = format!("error-{index}");
            diagnostics.push(error);
        }

        assert_eq!(diagnostics.values().len(), MAXIMUM);
        assert!(
            diagnostics
                .values()
                .iter()
                .all(|diagnostic| diagnostic.severity == Severity::Error)
        );
        assert_eq!(diagnostics.values()[0].message, "error-0");
        assert_eq!(
            diagnostics.values()[MAXIMUM - 1].message,
            format!("error-{}", MAXIMUM - 1)
        );
        assert!(diagnostics.has_errors());
        assert!(diagnostics.truncated());
    }

    #[test]
    fn rejects_unbounded_or_malformed_suppression_input() {
        assert_eq!(
            SuppressionPolicy::new(["Bad Code"], 1, 64),
            Err(SuppressionError::InvalidCode)
        );
        assert_eq!(
            SuppressionPolicy::new(["one.code", "two.code"], 1, 64),
            Err(SuppressionError::LimitExceeded)
        );
        assert_eq!(
            SuppressionPolicy::new(["one.code", "one.code"], 1, 64),
            Err(SuppressionError::LimitExceeded)
        );
    }
}
