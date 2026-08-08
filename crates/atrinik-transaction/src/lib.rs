// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::sync::Arc;

use atrinik_source::{Document, EditPlan, Error};

#[derive(Clone, Debug)]
pub struct Transaction {
    original: Arc<Document>,
    plan: EditPlan,
}

impl Transaction {
    #[must_use]
    pub fn new(original: Arc<Document>) -> Self {
        let plan = EditPlan::new(original.revision());
        Self { original, plan }
    }

    pub fn replace_value(&mut self, record: usize, replacement: &[u8]) -> Result<(), Error> {
        self.plan.replace_value(&self.original, record, replacement)
    }

    pub fn preview(&self) -> Result<Document, Error> {
        self.plan.apply(&self.original)
    }

    #[must_use]
    pub fn original(&self) -> &Arc<Document> {
        &self.original
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use atrinik_source::{Document, Limits, SourceId};

    use super::Transaction;

    #[test]
    fn previews_without_mutating_the_snapshot() {
        let source = Arc::new(
            Document::parse(
                SourceId::new("fixture:transaction").unwrap(),
                Arc::<[u8]>::from(&b"name old\n"[..]),
                Limits::default(),
            )
            .unwrap(),
        );
        let mut transaction = Transaction::new(source.clone());
        transaction.replace_value(0, b"new").unwrap();
        assert_eq!(transaction.preview().unwrap().source_bytes(), b"name new\n");
        assert_eq!(source.source_bytes(), b"name old\n");
    }
}
