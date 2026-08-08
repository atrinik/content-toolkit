// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT
// Provenance: provenance/reuse.json#lossless-core-model

#![forbid(unsafe_code)]

use std::{fmt, sync::Arc};

use atrinik_diagnostics::{Diagnostic, DiagnosticSet, Severity, Span};
use sha2::{Digest, Sha256};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Limits {
    pub maximum_file_bytes: usize,
    pub maximum_line_bytes: usize,
    pub maximum_records: usize,
    pub maximum_tokens: usize,
    pub maximum_value_bytes: usize,
    pub maximum_edits: usize,
    pub maximum_nesting: usize,
    pub maximum_diagnostics: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            maximum_file_bytes: 8 * 1024 * 1024,
            maximum_line_bytes: 64 * 1024,
            maximum_records: 250_000,
            maximum_tokens: 1_000_000,
            maximum_value_bytes: 1024 * 1024,
            maximum_edits: 4096,
            maximum_nesting: 64,
            maximum_diagnostics: 256,
        }
    }
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct SourceId(String);

impl SourceId {
    pub fn new(value: impl AsRef<str>) -> Result<Self, Error> {
        let value = value.as_ref();
        if value.is_empty() || value.len() > 256 || value.contains('\0') {
            return Err(Error::InvalidSourceId);
        }
        Ok(Self(value.to_owned()))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Revision([u8; 32]);

impl Revision {
    #[must_use]
    pub const fn bytes(self) -> [u8; 32] {
        self.0
    }
}

impl fmt::Display for Revision {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        for byte in self.0 {
            write!(formatter, "{byte:02x}")?;
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NewlineStyle {
    None,
    Lf,
    CrLf,
    Mixed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TokenKind {
    Key,
    Whitespace,
    Value,
    Comment,
    Directive,
    ObjectStart,
    ObjectEnd,
    Invalid,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Token {
    pub kind: TokenKind,
    pub span: Span,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RecordKind {
    Blank,
    Comment,
    Directive,
    Field { key: Span, value: Span },
    ObjectStart { name: Span },
    ObjectEnd,
    RawBlockStart,
    RawBlock,
    RawBlockEnd,
    Invalid,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Record {
    pub span: Span,
    pub content: Span,
    pub kind: RecordKind,
    pub tokens: Vec<Token>,
}

#[derive(Clone, Debug)]
pub struct Document {
    source_id: SourceId,
    source: Arc<[u8]>,
    revision: Revision,
    newline_style: NewlineStyle,
    records: Vec<Record>,
    diagnostics: DiagnosticSet,
    limits: Limits,
}

impl Document {
    pub fn parse(
        source_id: SourceId,
        source: impl Into<Arc<[u8]>>,
        limits: Limits,
    ) -> Result<Self, Error> {
        let source = source.into();
        let preflight = preflight(&source, limits)?;
        let mut records = Vec::with_capacity(preflight.records);
        let mut diagnostics = DiagnosticSet::new(limits.maximum_diagnostics);
        let mut nesting = 0_usize;
        let mut raw_block = false;
        let mut tokens = 0_usize;
        let mut start = 0_usize;

        while start < source.len() {
            let newline = source[start..]
                .iter()
                .position(|byte| *byte == b'\n')
                .map_or(source.len(), |relative| start + relative + 1);
            let content_end = if newline > start && source[newline - 1] == b'\n' {
                if newline > start + 1 && source[newline - 2] == b'\r' {
                    newline - 2
                } else {
                    newline - 1
                }
            } else {
                newline
            };
            let record = parse_record(
                &source,
                start,
                content_end,
                newline,
                &mut nesting,
                &mut raw_block,
                &mut diagnostics,
            );
            let value_bytes = match record.kind {
                RecordKind::Field { value, .. } => value.len(),
                RecordKind::ObjectStart { name } => name.len(),
                _ => 0,
            };
            if value_bytes > limits.maximum_value_bytes {
                return Err(Error::LimitExceeded("value bytes"));
            }
            tokens = tokens
                .checked_add(record.tokens.len())
                .ok_or(Error::LimitExceeded("tokens"))?;
            if tokens > limits.maximum_tokens {
                return Err(Error::LimitExceeded("tokens"));
            }
            if nesting > limits.maximum_nesting {
                return Err(Error::LimitExceeded("nesting"));
            }
            records.push(record);
            start = newline;
        }

        if raw_block {
            diagnostics.push(Diagnostic {
                code: "source.unclosed_raw_block",
                severity: Severity::Error,
                span: Span::new(source.len(), source.len()),
                message: "msg block has no matching endmsg record",
            });
        }

        let revision = Revision(Sha256::digest(&source).into());
        Ok(Self {
            source_id,
            source,
            revision,
            newline_style: preflight.newline_style,
            records,
            diagnostics,
            limits,
        })
    }

    #[must_use]
    pub fn source_id(&self) -> &SourceId {
        &self.source_id
    }

    #[must_use]
    pub const fn revision(&self) -> Revision {
        self.revision
    }

    #[must_use]
    pub const fn newline_style(&self) -> NewlineStyle {
        self.newline_style
    }

    #[must_use]
    pub fn records(&self) -> &[Record] {
        &self.records
    }

    #[must_use]
    pub fn diagnostics(&self) -> &DiagnosticSet {
        &self.diagnostics
    }

    #[must_use]
    pub fn source_bytes(&self) -> &[u8] {
        &self.source
    }

    #[must_use]
    pub const fn limits(&self) -> Limits {
        self.limits
    }

    pub fn bytes(&self, span: Span) -> Result<&[u8], Error> {
        self.source
            .get(span.start..span.end)
            .ok_or(Error::InvalidSpan)
    }

    pub fn fields(&self) -> impl Iterator<Item = (&[u8], &[u8])> {
        self.records.iter().filter_map(|record| match record.kind {
            RecordKind::Field { key, value } => Some((
                &self.source[key.start..key.end],
                &self.source[value.start..value.end],
            )),
            _ => None,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Edit {
    pub span: Span,
    pub replacement: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EditPlan {
    expected_revision: Revision,
    edits: Vec<Edit>,
}

impl EditPlan {
    #[must_use]
    pub const fn new(expected_revision: Revision) -> Self {
        Self {
            expected_revision,
            edits: Vec::new(),
        }
    }

    pub fn replace_value(
        &mut self,
        document: &Document,
        record: usize,
        replacement: &[u8],
    ) -> Result<(), Error> {
        if document.revision != self.expected_revision {
            return Err(Error::RevisionMismatch);
        }
        if self.edits.len() >= document.limits.maximum_edits {
            return Err(Error::LimitExceeded("edits"));
        }
        if replacement.len() > document.limits.maximum_value_bytes
            || replacement
                .iter()
                .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n'))
        {
            return Err(Error::InvalidEdit);
        }
        let record = document.records.get(record).ok_or(Error::InvalidEdit)?;
        let RecordKind::Field { value, .. } = record.kind else {
            return Err(Error::InvalidEdit);
        };
        if self.edits.iter().any(|edit| {
            edit.span == value || (edit.span.start < value.end && value.start < edit.span.end)
        }) {
            return Err(Error::OverlappingEdits);
        }
        self.edits.push(Edit {
            span: value,
            replacement: replacement.to_vec(),
        });
        Ok(())
    }

    pub fn apply(&self, document: &Document) -> Result<Document, Error> {
        if self.expected_revision != document.revision {
            return Err(Error::RevisionMismatch);
        }
        let mut edits: Vec<&Edit> = self.edits.iter().collect();
        edits.sort_by_key(|edit| (edit.span.start, edit.span.end));
        let mut previous_end = 0_usize;
        let mut output_length = document.source.len();
        for edit in &edits {
            if edit.span.start < previous_end
                || edit.span.end > document.source.len()
                || edit.span.start > edit.span.end
            {
                return Err(Error::OverlappingEdits);
            }
            output_length = output_length
                .checked_sub(edit.span.len())
                .and_then(|length| length.checked_add(edit.replacement.len()))
                .ok_or(Error::LimitExceeded("output bytes"))?;
            previous_end = edit.span.end;
        }
        if output_length > document.limits.maximum_file_bytes {
            return Err(Error::LimitExceeded("output bytes"));
        }

        let mut output = Vec::with_capacity(output_length);
        previous_end = 0;
        for edit in edits {
            output.extend_from_slice(&document.source[previous_end..edit.span.start]);
            output.extend_from_slice(&edit.replacement);
            previous_end = edit.span.end;
        }
        output.extend_from_slice(&document.source[previous_end..]);
        Document::parse(
            document.source_id.clone(),
            Arc::<[u8]>::from(output),
            document.limits,
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    InvalidSourceId,
    InvalidSpan,
    InvalidEdit,
    RevisionMismatch,
    OverlappingEdits,
    LimitExceeded(&'static str),
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidSourceId => write!(
                formatter,
                "source identity is empty, too long, or contains NUL"
            ),
            Self::InvalidSpan => write!(formatter, "source span is outside the document"),
            Self::InvalidEdit => write!(formatter, "edit target or replacement is invalid"),
            Self::RevisionMismatch => {
                write!(formatter, "edit plan revision does not match the document")
            }
            Self::OverlappingEdits => {
                write!(formatter, "edit spans overlap or are outside the document")
            }
            Self::LimitExceeded(limit) => {
                write!(formatter, "authored document exceeds the {limit} limit")
            }
        }
    }
}

impl std::error::Error for Error {}

struct Preflight {
    records: usize,
    newline_style: NewlineStyle,
}

fn preflight(source: &[u8], limits: Limits) -> Result<Preflight, Error> {
    if source.len() > limits.maximum_file_bytes {
        return Err(Error::LimitExceeded("file bytes"));
    }
    if source.is_empty() {
        return Ok(Preflight {
            records: 0,
            newline_style: NewlineStyle::None,
        });
    }

    let mut records = 1_usize;
    let mut line_bytes = 0_usize;
    let mut lf = 0_usize;
    let mut crlf = 0_usize;
    for (index, byte) in source.iter().copied().enumerate() {
        line_bytes += 1;
        if byte == b'\n' {
            let ending_bytes = if index > 0 && source[index - 1] == b'\r' {
                2
            } else {
                1
            };
            if line_bytes - ending_bytes > limits.maximum_line_bytes {
                return Err(Error::LimitExceeded("line bytes"));
            }
            if index > 0 && source[index - 1] == b'\r' {
                crlf += 1;
            } else {
                lf += 1;
            }
            line_bytes = 0;
            if index + 1 < source.len() {
                records = records
                    .checked_add(1)
                    .ok_or(Error::LimitExceeded("records"))?;
            }
        }
    }
    if line_bytes > limits.maximum_line_bytes {
        return Err(Error::LimitExceeded("line bytes"));
    }
    if records > limits.maximum_records {
        return Err(Error::LimitExceeded("records"));
    }
    let newline_style = match (lf, crlf) {
        (0, 0) => NewlineStyle::None,
        (_, 0) => NewlineStyle::Lf,
        (0, _) => NewlineStyle::CrLf,
        _ => NewlineStyle::Mixed,
    };
    Ok(Preflight {
        records,
        newline_style,
    })
}

fn parse_record(
    source: &[u8],
    start: usize,
    content_end: usize,
    end: usize,
    nesting: &mut usize,
    raw_block: &mut bool,
    diagnostics: &mut DiagnosticSet,
) -> Record {
    let content = Span::new(start, content_end);
    let span = Span::new(start, end);
    if *raw_block {
        if &source[start..content_end] == b"endmsg" {
            *raw_block = false;
            return Record {
                span,
                content,
                kind: RecordKind::RawBlockEnd,
                tokens: vec![Token {
                    kind: TokenKind::Directive,
                    span: content,
                }],
            };
        }
        return Record {
            span,
            content,
            kind: RecordKind::RawBlock,
            tokens: vec![Token {
                kind: TokenKind::Value,
                span: content,
            }],
        };
    }
    let leading = source[start..content_end]
        .iter()
        .take_while(|byte| matches!(byte, b' ' | b'\t'))
        .count();
    let first = start + leading;
    if first == content_end {
        return Record {
            span,
            content,
            kind: RecordKind::Blank,
            tokens: Vec::new(),
        };
    }
    if source[first] == b'#' {
        return Record {
            span,
            content,
            kind: RecordKind::Comment,
            tokens: vec![Token {
                kind: TokenKind::Comment,
                span: Span::new(first, content_end),
            }],
        };
    }
    if &source[first..content_end] == b"end" {
        if *nesting != 0 {
            *nesting -= 1;
        }
        return Record {
            span,
            content,
            kind: RecordKind::ObjectEnd,
            tokens: vec![Token {
                kind: TokenKind::ObjectEnd,
                span: Span::new(first, content_end),
            }],
        };
    }

    let key_end = source[first..content_end]
        .iter()
        .position(|byte| matches!(byte, b' ' | b'\t'))
        .map_or(content_end, |relative| first + relative);
    let whitespace_end = source[key_end..content_end]
        .iter()
        .take_while(|byte| matches!(byte, b' ' | b'\t'))
        .count()
        + key_end;
    if !valid_key(&source[first..key_end]) {
        diagnostics.push(Diagnostic {
            code: "source.invalid_record",
            severity: Severity::Error,
            span: content,
            message: "record must contain an ASCII key and a separated value",
        });
        return Record {
            span,
            content,
            kind: RecordKind::Invalid,
            tokens: vec![Token {
                kind: TokenKind::Invalid,
                span: content,
            }],
        };
    }

    if key_end == content_end {
        let key = Span::new(first, key_end);
        let kind = if &source[first..key_end] == b"msg" {
            *raw_block = true;
            RecordKind::RawBlockStart
        } else if &source[first..key_end] == b"Object" {
            *nesting += 1;
            RecordKind::ObjectStart {
                name: Span::new(content_end, content_end),
            }
        } else if &source[first..key_end] == b"endmsg" {
            diagnostics.push(Diagnostic {
                code: "source.unexpected_endmsg",
                severity: Severity::Error,
                span: key,
                message: "endmsg has no matching msg record",
            });
            RecordKind::RawBlockEnd
        } else {
            RecordKind::Directive
        };
        return Record {
            span,
            content,
            kind,
            tokens: vec![Token {
                kind: TokenKind::Directive,
                span: key,
            }],
        };
    }

    let key = Span::new(first, key_end);
    let value = Span::new(whitespace_end, content_end);
    let mut tokens = vec![
        Token {
            kind: TokenKind::Key,
            span: key,
        },
        Token {
            kind: TokenKind::Whitespace,
            span: Span::new(key_end, whitespace_end),
        },
        Token {
            kind: TokenKind::Value,
            span: value,
        },
    ];
    let kind = if &source[first..key_end] == b"Object" {
        *nesting += 1;
        tokens[0].kind = TokenKind::ObjectStart;
        RecordKind::ObjectStart { name: value }
    } else {
        RecordKind::Field { key, value }
    };
    Record {
        span,
        content,
        kind,
        tokens,
    }
}

fn valid_key(value: &[u8]) -> bool {
    let Some((first, remainder)) = value.split_first() else {
        return false;
    };
    (first.is_ascii_alphabetic() || *first == b'_')
        && remainder
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-'))
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::{Document, EditPlan, Error, Limits, NewlineStyle, SourceId};

    fn parse(source: &[u8]) -> Document {
        Document::parse(
            SourceId::new("fixture:test").unwrap(),
            Arc::<[u8]>::from(source),
            Limits::default(),
        )
        .unwrap()
    }

    #[test]
    fn round_trips_comments_unknown_fields_repeats_and_newlines() {
        let source = b"# comment\r\nObject map\nunknown  first\r\nunknown\tsecond\nmsg\nfree form: bytes\nendmsg\nMore\nend";
        let document = parse(source);
        assert_eq!(document.source_bytes(), source);
        assert_eq!(document.newline_style(), NewlineStyle::Mixed);
        assert_eq!(document.fields().count(), 2);
        assert!(!document.diagnostics().has_errors());
    }

    #[test]
    fn semantic_edit_changes_only_the_value_span() {
        let document = parse(b"name old\n# untouched\r\nvalue keep\n");
        let mut plan = EditPlan::new(document.revision());
        plan.replace_value(&document, 0, b"new").unwrap();
        let edited = plan.apply(&document).unwrap();
        assert_eq!(
            edited.source_bytes(),
            b"name new\n# untouched\r\nvalue keep\n"
        );
        assert_ne!(edited.revision(), document.revision());
        assert_eq!(
            document.source_bytes(),
            b"name old\n# untouched\r\nvalue keep\n"
        );
    }

    #[test]
    fn rejects_stale_or_overlapping_edits() {
        let document = parse(b"name old\n");
        let stale = parse(b"name newer\n");
        let mut plan = EditPlan::new(document.revision());
        plan.replace_value(&document, 0, b"new").unwrap();
        assert!(matches!(plan.apply(&stale), Err(Error::RevisionMismatch)));
        assert_eq!(
            plan.replace_value(&document, 0, b"again"),
            Err(Error::OverlappingEdits)
        );
    }

    #[test]
    fn applies_bounds_before_accepting_hostile_input() {
        let limits = Limits {
            maximum_file_bytes: 4,
            ..Limits::default()
        };
        assert!(matches!(
            Document::parse(
                SourceId::new("fixture:large").unwrap(),
                Arc::<[u8]>::from(&b"12345"[..]),
                limits,
            ),
            Err(Error::LimitExceeded("file bytes"))
        ));

        let limits = Limits {
            maximum_nesting: 1,
            ..Limits::default()
        };
        assert!(matches!(
            Document::parse(
                SourceId::new("fixture:deep").unwrap(),
                Arc::<[u8]>::from(&b"Object one\nObject two\nend\nend\n"[..]),
                limits,
            ),
            Err(Error::LimitExceeded("nesting"))
        ));

        let limits = Limits {
            maximum_line_bytes: 3,
            ..Limits::default()
        };
        assert!(matches!(
            Document::parse(
                SourceId::new("fixture:long-line").unwrap(),
                Arc::<[u8]>::from(&b"1234\n"[..]),
                limits,
            ),
            Err(Error::LimitExceeded("line bytes"))
        ));

        let limits = Limits {
            maximum_records: 1,
            ..Limits::default()
        };
        assert!(matches!(
            Document::parse(
                SourceId::new("fixture:records").unwrap(),
                Arc::<[u8]>::from(&b"name one\nname two"[..]),
                limits,
            ),
            Err(Error::LimitExceeded("records"))
        ));
    }

    #[test]
    fn bounds_recovery_and_edit_sequences() {
        let limits = Limits {
            maximum_diagnostics: 1,
            maximum_edits: 1,
            ..Limits::default()
        };
        let invalid = Document::parse(
            SourceId::new("fixture:recovery").unwrap(),
            Arc::<[u8]>::from(&b"endmsg\nendmsg\n"[..]),
            limits,
        )
        .unwrap();
        assert_eq!(invalid.diagnostics().values().len(), 1);
        assert!(invalid.diagnostics().truncated());

        let document = Document::parse(
            SourceId::new("fixture:edits").unwrap(),
            Arc::<[u8]>::from(&b"name one\ntype two\n"[..]),
            limits,
        )
        .unwrap();
        let mut plan = EditPlan::new(document.revision());
        plan.replace_value(&document, 0, b"first").unwrap();
        assert_eq!(
            plan.replace_value(&document, 1, b"second"),
            Err(Error::LimitExceeded("edits"))
        );
    }

    #[test]
    fn deterministic_property_smoke_preserves_every_accepted_byte() {
        let mut state = 0x9e37_79b9_u32;
        for length in 0..512_usize {
            let mut input = Vec::with_capacity(length);
            for _ in 0..length {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                input.push(state as u8);
            }
            if let Ok(document) = Document::parse(
                SourceId::new("fuzz:deterministic").unwrap(),
                Arc::<[u8]>::from(input.clone()),
                Limits::default(),
            ) {
                assert_eq!(document.source_bytes(), input);
            }
        }
    }
}
