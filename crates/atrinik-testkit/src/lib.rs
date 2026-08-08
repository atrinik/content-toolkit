// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::sync::Arc;

use atrinik_source::{Document, Error, Limits, SourceId};

pub const MINIMAL_AUTHORED_DOCUMENT: &[u8] = include_bytes!("../fixtures/minimal.arc");

pub fn minimal_document() -> Result<Document, Error> {
    Document::parse(
        SourceId::new("fixture:minimal")?,
        Arc::<[u8]>::from(MINIMAL_AUTHORED_DOCUMENT),
        Limits::default(),
    )
}

#[cfg(test)]
mod tests {
    use super::{MINIMAL_AUTHORED_DOCUMENT, minimal_document};

    #[test]
    fn fixture_is_valid_and_byte_lossless() {
        let document = minimal_document().unwrap();
        assert!(!document.diagnostics().has_errors());
        assert_eq!(document.source_bytes(), MINIMAL_AUTHORED_DOCUMENT);
    }
}
