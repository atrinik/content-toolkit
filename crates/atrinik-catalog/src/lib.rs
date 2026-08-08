// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::{collections::BTreeMap, fmt, sync::Arc};

use atrinik_source::{Document, SourceId};

#[derive(Clone, Debug, Default)]
pub struct Catalog {
    documents: BTreeMap<SourceId, Arc<Document>>,
}

impl Catalog {
    pub fn build(
        documents: impl IntoIterator<Item = Arc<Document>>,
        maximum_documents: usize,
    ) -> Result<Self, Error> {
        let mut catalog = Self::default();
        for document in documents {
            if catalog.documents.len() >= maximum_documents {
                return Err(Error::LimitExceeded);
            }
            if catalog
                .documents
                .insert(document.source_id().clone(), document)
                .is_some()
            {
                return Err(Error::DuplicateSource);
            }
        }
        Ok(catalog)
    }

    #[must_use]
    pub fn get(&self, source: &SourceId) -> Option<&Arc<Document>> {
        self.documents.get(source)
    }

    pub fn iter(&self) -> impl Iterator<Item = (&SourceId, &Arc<Document>)> {
        self.documents.iter()
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.documents.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.documents.is_empty()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    DuplicateSource,
    LimitExceeded,
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateSource => write!(formatter, "catalog source identity is duplicated"),
            Self::LimitExceeded => write!(formatter, "catalog document limit is exceeded"),
        }
    }
}

impl std::error::Error for Error {}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use atrinik_source::{Document, Limits, SourceId};

    use super::{Catalog, Error};

    #[test]
    fn orders_sources_and_rejects_duplicates() {
        let source = SourceId::new("fixture:a").unwrap();
        let document = Arc::new(
            Document::parse(
                source,
                Arc::<[u8]>::from(&b"name a\n"[..]),
                Limits::default(),
            )
            .unwrap(),
        );
        assert_eq!(
            Catalog::build([document.clone(), document], 2).unwrap_err(),
            Error::DuplicateSource
        );
    }
}
