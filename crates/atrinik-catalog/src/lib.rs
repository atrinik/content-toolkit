// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    fmt,
};

use atrinik_diagnostics::{
    Diagnostic, DiagnosticLimits, DiagnosticSet, Location, RelatedLocation, Severity, Span,
    SuppressionPolicy,
};
use atrinik_source::{Document, RecordKind, Revision, SourceId};
use sha2::{Digest, Sha256};

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum Domain {
    Archetype,
    Map,
    Face,
    Animation,
    Treasure,
    Faction,
    Interface,
    Quest,
    Resource,
}

impl Domain {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Archetype => "archetype",
            Self::Map => "map",
            Self::Face => "face",
            Self::Animation => "animation",
            Self::Treasure => "treasure",
            Self::Faction => "faction",
            Self::Interface => "interface",
            Self::Quest => "quest",
            Self::Resource => "resource",
        }
    }

    pub const ALL: [Self; 9] = [
        Self::Archetype,
        Self::Map,
        Self::Face,
        Self::Animation,
        Self::Treasure,
        Self::Faction,
        Self::Interface,
        Self::Quest,
        Self::Resource,
    ];
}

impl fmt::Display for Domain {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct CatalogId {
    domain: Domain,
    namespace: String,
    local: String,
}

impl CatalogId {
    pub fn new(
        domain: Domain,
        namespace: impl Into<String>,
        local: impl Into<String>,
    ) -> Result<Self, Error> {
        let namespace = namespace.into();
        let local = local.into();
        if !valid_namespace(&namespace) || !valid_local_id(&local) {
            return Err(Error::InvalidIdentifier);
        }
        Ok(Self {
            domain,
            namespace,
            local,
        })
    }

    #[must_use]
    pub const fn domain(&self) -> Domain {
        self.domain
    }

    #[must_use]
    pub fn namespace(&self) -> &str {
        &self.namespace
    }

    #[must_use]
    pub fn local(&self) -> &str {
        &self.local
    }
}

impl fmt::Display for CatalogId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{}:{}/{}",
            self.domain, self.namespace, self.local
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum ReferenceKind {
    Generic,
    Archetype,
    Inherits,
    Map,
    Face,
    Animation,
    Treasure,
    Faction,
    Interface,
    Quest,
    Resource,
}

impl ReferenceKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Generic => "generic",
            Self::Archetype => "archetype",
            Self::Inherits => "inherits",
            Self::Map => "map",
            Self::Face => "face",
            Self::Animation => "animation",
            Self::Treasure => "treasure",
            Self::Faction => "faction",
            Self::Interface => "interface",
            Self::Quest => "quest",
            Self::Resource => "resource",
        }
    }

    #[must_use]
    pub const fn expected_domain(self) -> Option<Domain> {
        match self {
            Self::Generic | Self::Inherits => None,
            Self::Archetype => Some(Domain::Archetype),
            Self::Map => Some(Domain::Map),
            Self::Face => Some(Domain::Face),
            Self::Animation => Some(Domain::Animation),
            Self::Treasure => Some(Domain::Treasure),
            Self::Faction => Some(Domain::Faction),
            Self::Interface => Some(Domain::Interface),
            Self::Quest => Some(Domain::Quest),
            Self::Resource => Some(Domain::Resource),
        }
    }
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct Reference {
    pub target: CatalogId,
    pub kind: ReferenceKind,
    pub location: Location,
    pub semantic_path: Vec<String>,
    pub optional: bool,
}

impl Reference {
    #[must_use]
    pub fn new(target: CatalogId, kind: ReferenceKind, location: Location) -> Self {
        Self {
            target,
            kind,
            location,
            semantic_path: Vec::new(),
            optional: false,
        }
    }

    #[must_use]
    pub fn with_semantic_path(mut self, path: impl IntoIterator<Item = impl Into<String>>) -> Self {
        self.semantic_path = path.into_iter().map(Into::into).collect();
        self
    }

    #[must_use]
    pub const fn optional(mut self, optional: bool) -> Self {
        self.optional = optional;
        self
    }
}

#[derive(Clone, Debug, Default, Eq, Ord, PartialEq, PartialOrd)]
pub struct PreviewMetadata {
    pub label: Option<String>,
    pub summary: Option<String>,
    pub tags: BTreeSet<String>,
    pub keywords: BTreeSet<String>,
    pub media: BTreeMap<String, Reference>,
}

#[derive(Clone, Debug, Default, Eq, Ord, PartialEq, PartialOrd)]
pub struct EvidenceReferences {
    pub provenance: Option<String>,
    pub license: Option<String>,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct Definition {
    pub id: CatalogId,
    pub location: Location,
    pub aliases: BTreeSet<CatalogId>,
    pub inherits: Option<Reference>,
    pub references: Vec<Reference>,
    pub preview: PreviewMetadata,
    pub evidence: EvidenceReferences,
}

impl Definition {
    #[must_use]
    pub fn new(id: CatalogId, location: Location) -> Self {
        Self {
            id,
            location,
            aliases: BTreeSet::new(),
            inherits: None,
            references: Vec::new(),
            preview: PreviewMetadata::default(),
            evidence: EvidenceReferences::default(),
        }
    }

    #[must_use]
    pub fn with_alias(mut self, alias: CatalogId) -> Self {
        self.aliases.insert(alias);
        self
    }

    #[must_use]
    pub fn with_inheritance(mut self, inheritance: Reference) -> Self {
        self.inherits = Some(inheritance);
        self
    }

    #[must_use]
    pub fn with_reference(mut self, reference: Reference) -> Self {
        self.references.push(reference);
        self
    }

    #[must_use]
    pub fn with_preview(mut self, preview: PreviewMetadata) -> Self {
        self.preview = preview;
        self
    }

    #[must_use]
    pub fn with_evidence(mut self, evidence: EvidenceReferences) -> Self {
        self.evidence = evidence;
        self
    }

    fn all_references(&self) -> impl Iterator<Item = &Reference> {
        self.inherits
            .iter()
            .chain(&self.references)
            .chain(self.preview.media.values())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CatalogDocument {
    source_id: SourceId,
    revision: Revision,
    schema_version: u32,
    definitions: Vec<Definition>,
}

impl CatalogDocument {
    #[must_use]
    pub fn new(
        source_id: SourceId,
        revision: Revision,
        schema_version: u32,
        definitions: Vec<Definition>,
    ) -> Self {
        Self {
            source_id,
            revision,
            schema_version,
            definitions,
        }
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
    pub const fn schema_version(&self) -> u32 {
        self.schema_version
    }

    #[must_use]
    pub fn definitions(&self) -> &[Definition] {
        &self.definitions
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CatalogLimits {
    pub maximum_documents: usize,
    pub maximum_definitions_per_document: usize,
    pub maximum_definitions: usize,
    pub maximum_aliases_per_definition: usize,
    pub maximum_references_per_definition: usize,
    pub maximum_preview_values: usize,
    pub maximum_string_bytes: usize,
    pub maximum_semantic_depth: usize,
    pub maximum_graph_work: usize,
    pub maximum_invalidation: usize,
    pub maximum_query_terms: usize,
    pub maximum_query_work: usize,
    pub diagnostic_limits: DiagnosticLimits,
}

impl Default for CatalogLimits {
    fn default() -> Self {
        Self {
            maximum_documents: 100_000,
            maximum_definitions_per_document: 250_000,
            maximum_definitions: 1_000_000,
            maximum_aliases_per_definition: 64,
            maximum_references_per_definition: 4096,
            maximum_preview_values: 256,
            maximum_string_bytes: 4096,
            maximum_semantic_depth: 32,
            maximum_graph_work: 8_000_000,
            maximum_invalidation: 1_000_000,
            maximum_query_terms: 256,
            maximum_query_work: 1_000_000,
            diagnostic_limits: DiagnosticLimits::default(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Generation([u8; 32]);

impl Generation {
    #[must_use]
    pub const fn bytes(self) -> [u8; 32] {
        self.0
    }
}

impl fmt::Display for Generation {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        for byte in self.0 {
            write!(formatter, "{byte:02x}")?;
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Catalog {
    documents: BTreeMap<SourceId, CatalogDocument>,
    candidates: BTreeMap<CatalogId, Vec<Definition>>,
    aliases: BTreeMap<CatalogId, BTreeSet<CatalogId>>,
    aliases_by_target: BTreeMap<CatalogId, BTreeSet<CatalogId>>,
    dependents: BTreeMap<CatalogId, BTreeSet<CatalogId>>,
    diagnostics: DiagnosticSet,
    generation: Generation,
    limits: CatalogLimits,
    suppressions: SuppressionPolicy,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Resolution<'a> {
    Found(&'a Definition),
    Missing,
    Ambiguous,
}

impl Catalog {
    pub fn build(
        documents: impl IntoIterator<Item = CatalogDocument>,
        limits: CatalogLimits,
        suppressions: SuppressionPolicy,
    ) -> Result<Self, Error> {
        let mut document_index = BTreeMap::new();
        let mut total_definitions = 0_usize;
        let mut total_index_entries = 0_usize;
        for document in documents {
            if document_index.contains_key(document.source_id()) {
                return Err(Error::DuplicateSource);
            }
            if document_index.len() >= limits.maximum_documents {
                return Err(Error::LimitExceeded("documents"));
            }
            validate_document(&document, limits)?;
            total_definitions = total_definitions
                .checked_add(document.definitions.len())
                .ok_or(Error::LimitExceeded("definitions"))?;
            if total_definitions > limits.maximum_definitions {
                return Err(Error::LimitExceeded("definitions"));
            }
            total_index_entries = checked_index_entries(
                total_index_entries,
                document.definitions(),
                limits.maximum_graph_work,
            )?;
            document_index.insert(document.source_id().clone(), document);
        }
        Self::from_index(document_index, limits, suppressions)
    }

    fn from_index(
        documents: BTreeMap<SourceId, CatalogDocument>,
        limits: CatalogLimits,
        suppressions: SuppressionPolicy,
    ) -> Result<Self, Error> {
        if documents.len() > limits.maximum_documents {
            return Err(Error::LimitExceeded("documents"));
        }
        let mut candidates: BTreeMap<CatalogId, Vec<Definition>> = BTreeMap::new();
        let mut aliases: BTreeMap<CatalogId, BTreeSet<CatalogId>> = BTreeMap::new();
        let mut total_definitions = 0_usize;
        let mut total_index_entries = 0_usize;
        for document in documents.values() {
            validate_document(document, limits)?;
            total_definitions = total_definitions
                .checked_add(document.definitions.len())
                .ok_or(Error::LimitExceeded("definitions"))?;
            if total_definitions > limits.maximum_definitions {
                return Err(Error::LimitExceeded("definitions"));
            }
            total_index_entries = checked_index_entries(
                total_index_entries,
                document.definitions(),
                limits.maximum_graph_work,
            )?;
            for definition in &document.definitions {
                candidates
                    .entry(definition.id.clone())
                    .or_default()
                    .push(definition.clone());
                for alias in &definition.aliases {
                    aliases
                        .entry(alias.clone())
                        .or_default()
                        .insert(definition.id.clone());
                }
            }
        }
        for values in candidates.values_mut() {
            values.sort();
        }
        let aliases_by_target = reverse_aliases(&aliases);

        let mut catalog = Self {
            generation: generation(&documents),
            documents,
            candidates,
            aliases,
            aliases_by_target,
            dependents: BTreeMap::new(),
            diagnostics: DiagnosticSet::with_limits(limits.diagnostic_limits),
            limits,
            suppressions,
        };
        catalog.index_conflicts();
        catalog.index_references(true)?;
        catalog.index_cycles()?;
        Ok(catalog)
    }

    fn index_conflicts(&mut self) {
        for (id, definitions) in &self.candidates {
            if definitions.len() > 1 {
                let mut diagnostic = Diagnostic::new(
                    "catalog.duplicate_id",
                    Severity::Error,
                    definitions[0].location.clone(),
                    format!("catalog ID `{id}` has multiple definitions"),
                )
                .with_semantic_path(["definitions".to_owned(), id.to_string()])
                .with_fix_hint("rename or remove every conflicting definition");
                let maximum_related = self.limits.diagnostic_limits.maximum_related;
                for definition in definitions.iter().skip(1).take(maximum_related) {
                    diagnostic = diagnostic.with_related(RelatedLocation::new(
                        definition.location.clone(),
                        "conflicting definition",
                    ));
                }
                if definitions.len().saturating_sub(1) > maximum_related {
                    self.diagnostics.mark_truncated();
                }
                self.diagnostics
                    .push_with_policy(diagnostic, &self.suppressions);
            }
        }
        for (alias, targets) in &self.aliases {
            let shadows = self.candidates.contains_key(alias) && !targets.contains(alias);
            if targets.len() > 1 || shadows {
                let maximum_locations = self
                    .limits
                    .diagnostic_limits
                    .maximum_related
                    .saturating_add(1);
                let locations: Vec<Location> = self
                    .candidates
                    .get(alias)
                    .into_iter()
                    .flatten()
                    .chain(
                        targets
                            .iter()
                            .filter_map(|target| self.candidates.get(target))
                            .flatten(),
                    )
                    .map(|definition| definition.location.clone())
                    .take(maximum_locations)
                    .collect();
                let primary = locations
                    .first()
                    .cloned()
                    .or_else(|| {
                        self.candidates
                            .get(alias)
                            .and_then(|values| values.first())
                            .map(|definition| definition.location.clone())
                    })
                    .unwrap_or_else(|| Location::new("catalog", Span::new(0, 0)));
                let mut diagnostic = Diagnostic::new(
                    "catalog.ambiguous_alias",
                    Severity::Error,
                    primary,
                    format!("catalog alias `{alias}` resolves ambiguously"),
                )
                .with_semantic_path(["aliases".to_owned(), alias.to_string()])
                .with_fix_hint("assign each alias to exactly one non-conflicting catalog ID");
                for location in locations.into_iter().skip(1) {
                    diagnostic = diagnostic
                        .with_related(RelatedLocation::new(location, "other alias target"));
                }
                let locations_truncated = self
                    .candidates
                    .get(alias)
                    .into_iter()
                    .flatten()
                    .chain(
                        targets
                            .iter()
                            .filter_map(|target| self.candidates.get(target))
                            .flatten(),
                    )
                    .map(|definition| &definition.location)
                    .take(maximum_locations.saturating_add(1))
                    .count()
                    > maximum_locations;
                if locations_truncated {
                    self.diagnostics.mark_truncated();
                }
                self.diagnostics
                    .push_with_policy(diagnostic, &self.suppressions);
            }
        }
    }

    fn index_references(&mut self, update_dependents: bool) -> Result<(), Error> {
        let reference_count = self
            .candidates
            .values()
            .filter(|values| values.len() == 1)
            .try_fold(0_usize, |count, values| {
                count
                    .checked_add(values[0].all_references().count())
                    .ok_or(Error::LimitExceeded("graph work"))
            })?;
        if reference_count > self.limits.maximum_graph_work {
            return Err(Error::LimitExceeded("graph work"));
        }
        let definitions: Vec<(CatalogId, Vec<Reference>)> = self
            .candidates
            .values()
            .filter(|values| values.len() == 1)
            .map(|values| {
                (
                    values[0].id.clone(),
                    values[0].all_references().cloned().collect(),
                )
            })
            .collect();
        let mut graph_work = 0_usize;
        for (definition_id, mut references) in definitions {
            references.sort_by(|left, right| {
                left.target
                    .cmp(&right.target)
                    .then_with(|| left.kind.cmp(&right.kind))
                    .then_with(|| left.location.cmp(&right.location))
                    .then_with(|| left.semantic_path.cmp(&right.semantic_path))
                    .then_with(|| left.optional.cmp(&right.optional))
            });
            for reference in &references {
                graph_work = graph_work
                    .checked_add(1)
                    .ok_or(Error::LimitExceeded("graph work"))?;
                if graph_work > self.limits.maximum_graph_work {
                    return Err(Error::LimitExceeded("graph work"));
                }
                if update_dependents {
                    self.dependents
                        .entry(reference.target.clone())
                        .or_default()
                        .insert(definition_id.clone());
                }
                match self.resolve(&reference.target) {
                    Resolution::Found(_) => {}
                    Resolution::Missing => {
                        let severity = if reference.optional {
                            Severity::Warning
                        } else {
                            Severity::Error
                        };
                        let diagnostic = Diagnostic::new(
                            "catalog.missing_reference",
                            severity,
                            reference.location.clone(),
                            format!(
                                "{} reference `{}` does not resolve",
                                reference.kind.as_str(),
                                reference.target
                            ),
                        )
                        .with_semantic_path(reference.semantic_path.clone())
                        .with_fix_hint("define the target or update the stable catalog ID")
                        .suppressible(reference.optional);
                        self.diagnostics
                            .push_with_policy(diagnostic, &self.suppressions);
                    }
                    Resolution::Ambiguous => {
                        let diagnostic = Diagnostic::new(
                            "catalog.ambiguous_reference",
                            Severity::Error,
                            reference.location.clone(),
                            format!(
                                "{} reference `{}` has multiple targets",
                                reference.kind.as_str(),
                                reference.target
                            ),
                        )
                        .with_semantic_path(reference.semantic_path.clone())
                        .with_fix_hint("remove the duplicate ID or conflicting alias");
                        self.diagnostics
                            .push_with_policy(diagnostic, &self.suppressions);
                    }
                }
            }
        }
        Ok(())
    }

    fn index_cycles(&mut self) -> Result<(), Error> {
        let ids: Vec<CatalogId> = self
            .candidates
            .iter()
            .filter(|(_, values)| values.len() == 1)
            .map(|(id, _)| id.clone())
            .collect();
        let mut state: BTreeMap<CatalogId, u8> = BTreeMap::new();
        let mut graph_work = 0_usize;
        for start in ids {
            if state.get(&start).copied().unwrap_or(0) != 0 {
                continue;
            }
            let mut path = Vec::new();
            let mut positions = BTreeMap::new();
            let mut current = start;
            loop {
                graph_work = graph_work
                    .checked_add(1)
                    .ok_or(Error::LimitExceeded("graph work"))?;
                if graph_work > self.limits.maximum_graph_work {
                    return Err(Error::LimitExceeded("graph work"));
                }
                match state.get(&current).copied().unwrap_or(0) {
                    2 => break,
                    1 => {
                        if let Some(position) = positions.get(&current).copied() {
                            self.push_cycle(&path[position..]);
                        }
                        break;
                    }
                    _ => {}
                }
                state.insert(current.clone(), 1);
                positions.insert(current.clone(), path.len());
                path.push(current.clone());
                let Some(definition) = self.unique_definition(&current) else {
                    break;
                };
                let Some(inheritance) = &definition.inherits else {
                    break;
                };
                let Resolution::Found(target) = self.resolve(&inheritance.target) else {
                    break;
                };
                current = target.id.clone();
            }
            for id in path {
                state.insert(id, 2);
            }
        }
        Ok(())
    }

    fn push_cycle(&mut self, cycle: &[CatalogId]) {
        let Some(first) = cycle.first() else {
            return;
        };
        let Some(definition) = self.unique_definition(first) else {
            return;
        };
        let mut diagnostic = Diagnostic::new(
            "catalog.inheritance_cycle",
            Severity::Error,
            definition.location.clone(),
            format!("inheritance cycle contains `{first}`"),
        )
        .with_semantic_path(["inherits"])
        .with_fix_hint("remove an inheritance edge from the cycle");
        let maximum_related = self.limits.diagnostic_limits.maximum_related;
        for id in cycle.iter().skip(1).take(maximum_related) {
            if let Some(definition) = self.unique_definition(id) {
                diagnostic = diagnostic.with_related(RelatedLocation::new(
                    definition.location.clone(),
                    format!("cycle member `{id}`"),
                ));
            }
        }
        if cycle.len().saturating_sub(1) > maximum_related {
            self.diagnostics.mark_truncated();
        }
        self.diagnostics
            .push_with_policy(diagnostic, &self.suppressions);
    }

    #[must_use]
    pub fn resolve(&self, id: &CatalogId) -> Resolution<'_> {
        let direct = self.candidates.get(id);
        let aliases = self.aliases.get(id);
        match direct {
            Some(values) if values.len() > 1 => Resolution::Ambiguous,
            Some(values) => {
                let conflicting_alias = aliases
                    .is_some_and(|targets| targets.iter().any(|target| target != &values[0].id));
                if conflicting_alias {
                    Resolution::Ambiguous
                } else {
                    Resolution::Found(&values[0])
                }
            }
            None => {
                let Some(targets) = aliases else {
                    return Resolution::Missing;
                };
                if targets.len() != 1 {
                    return Resolution::Ambiguous;
                }
                let target = targets.first().expect("one alias target");
                match self.candidates.get(target) {
                    Some(values) if values.len() == 1 => Resolution::Found(&values[0]),
                    Some(_) => Resolution::Ambiguous,
                    None => Resolution::Missing,
                }
            }
        }
    }

    fn unique_definition(&self, id: &CatalogId) -> Option<&Definition> {
        self.candidates
            .get(id)
            .filter(|values| values.len() == 1)
            .map(|values| &values[0])
    }

    pub fn definitions(&self) -> impl Iterator<Item = &Definition> {
        self.candidates
            .values()
            .filter(|values| values.len() == 1)
            .map(|values| &values[0])
    }

    pub fn documents(&self) -> impl Iterator<Item = &CatalogDocument> {
        self.documents.values()
    }

    #[must_use]
    pub fn diagnostics(&self) -> &DiagnosticSet {
        &self.diagnostics
    }

    #[must_use]
    pub const fn generation(&self) -> Generation {
        self.generation
    }

    #[must_use]
    pub fn preview(&self, id: &CatalogId) -> Option<&PreviewMetadata> {
        match self.resolve(id) {
            Resolution::Found(definition) => Some(&definition.preview),
            Resolution::Missing | Resolution::Ambiguous => None,
        }
    }

    pub fn dependents(&self, id: &CatalogId) -> impl Iterator<Item = &CatalogId> {
        self.dependents.get(id).into_iter().flatten()
    }

    pub fn search<'a>(
        &'a self,
        query: &Query,
        maximum: usize,
    ) -> Result<Vec<&'a Definition>, Error> {
        if maximum == 0 {
            return Ok(Vec::new());
        }
        if maximum > self.limits.maximum_query_terms
            || query.tags.len() > self.limits.maximum_query_terms
        {
            return Err(Error::LimitExceeded("query terms"));
        }
        for value in query
            .namespace
            .iter()
            .chain(query.text.iter())
            .chain(query.tags.iter())
        {
            validate_text(value, self.limits)?;
        }
        let query_bytes = query
            .namespace
            .iter()
            .chain(query.text.iter())
            .chain(query.tags.iter())
            .try_fold(0_usize, |total, term| total.checked_add(term.len()))
            .ok_or(Error::LimitExceeded("query work"))?;
        if query_bytes > self.limits.maximum_query_work {
            return Err(Error::LimitExceeded("query work"));
        }
        let needle = query.text.as_ref().map(|value| value.to_lowercase());
        let mut work = query_bytes;
        let mut results = Vec::with_capacity(maximum.min(64));
        for definition in self.definitions() {
            let searchable_bytes = definition
                .id
                .namespace()
                .len()
                .checked_add(definition.id.local().len())
                .and_then(|value| value.checked_add(definition.id.domain().as_str().len()))
                .and_then(|value| {
                    definition
                        .preview
                        .label
                        .iter()
                        .chain(definition.preview.summary.iter())
                        .chain(definition.preview.tags.iter())
                        .chain(definition.preview.keywords.iter())
                        .try_fold(value, |total, term| total.checked_add(term.len()))
                })
                .ok_or(Error::LimitExceeded("query work"))?;
            work = work
                .checked_add(searchable_bytes.max(1))
                .ok_or(Error::LimitExceeded("query work"))?;
            if work > self.limits.maximum_query_work {
                return Err(Error::LimitExceeded("query work"));
            }
            if query
                .domain
                .is_none_or(|domain| definition.id.domain() == domain)
                && query
                    .namespace
                    .as_ref()
                    .is_none_or(|namespace| definition.id.namespace() == namespace)
                && query.tags.is_subset(&definition.preview.tags)
                && needle.as_ref().is_none_or(|needle| {
                    definition.id.to_string().to_lowercase().contains(needle)
                        || definition
                            .preview
                            .label
                            .as_ref()
                            .is_some_and(|value| value.to_lowercase().contains(needle))
                        || definition
                            .preview
                            .summary
                            .as_ref()
                            .is_some_and(|value| value.to_lowercase().contains(needle))
                        || definition
                            .preview
                            .keywords
                            .iter()
                            .any(|value| value.to_lowercase().contains(needle))
                })
            {
                results.push(definition);
                if results.len() == maximum {
                    break;
                }
            }
        }
        Ok(results)
    }

    pub fn update_document(&self, document: CatalogDocument) -> Result<CatalogUpdate, Error> {
        if self.documents.get(document.source_id()) == Some(&document) {
            return Ok(CatalogUpdate {
                catalog: self.clone(),
                invalidation: Invalidation {
                    source: document.source_id().clone(),
                    changed: BTreeSet::new(),
                    affected: BTreeSet::new(),
                },
            });
        }
        validate_document(&document, self.limits)?;
        let source = document.source_id().clone();
        let old = self.documents.get(&source);
        let (changed_canonical, mut changed) =
            changed_identities(old, Some(&document), self.limits.maximum_invalidation)?;
        let catalog = self.with_replaced_document(document, &changed_canonical)?;
        expand_changed_aliases(
            &mut changed,
            &changed_canonical,
            [&self.aliases, &catalog.aliases],
            self.limits.maximum_invalidation,
        )?;
        let affected = self.collect_invalidation(&catalog, &changed)?;
        Ok(CatalogUpdate {
            catalog,
            invalidation: Invalidation {
                source,
                changed,
                affected,
            },
        })
    }

    pub fn remove_document(&self, source: &SourceId) -> Result<CatalogUpdate, Error> {
        let Some(old) = self.documents.get(source) else {
            return Err(Error::MissingSource);
        };
        let (changed_canonical, mut changed) =
            changed_identities(Some(old), None, self.limits.maximum_invalidation)?;
        let catalog = self.without_document(source, &changed_canonical)?;
        expand_changed_aliases(
            &mut changed,
            &changed_canonical,
            [&self.aliases, &catalog.aliases],
            self.limits.maximum_invalidation,
        )?;
        let affected = self.collect_invalidation(&catalog, &changed)?;
        Ok(CatalogUpdate {
            catalog,
            invalidation: Invalidation {
                source: source.clone(),
                changed,
                affected,
            },
        })
    }

    fn collect_invalidation(
        &self,
        next: &Self,
        changed: &BTreeSet<CatalogId>,
    ) -> Result<BTreeSet<CatalogId>, Error> {
        self.validate_invalidation_seed(changed)?;
        let mut affected = changed.clone();
        let mut queue = VecDeque::from_iter(changed.iter().cloned());
        while let Some(target) = queue.pop_front() {
            for dependent in self
                .dependents(&target)
                .chain(next.dependents(&target))
                .chain(self.aliases_for(&target))
                .chain(next.aliases_for(&target))
            {
                if affected.insert(dependent.clone()) {
                    if affected.len() > self.limits.maximum_invalidation {
                        return Err(Error::LimitExceeded("invalidation"));
                    }
                    queue.push_back(dependent.clone());
                }
            }
        }
        Ok(affected)
    }

    fn aliases_for(&self, id: &CatalogId) -> impl Iterator<Item = &CatalogId> {
        self.aliases_by_target.get(id).into_iter().flatten()
    }

    fn validate_invalidation_seed(&self, changed: &BTreeSet<CatalogId>) -> Result<(), Error> {
        if changed.len() > self.limits.maximum_invalidation {
            Err(Error::LimitExceeded("invalidation"))
        } else {
            Ok(())
        }
    }

    fn with_replaced_document(
        &self,
        document: CatalogDocument,
        changed_canonical: &BTreeSet<CatalogId>,
    ) -> Result<Self, Error> {
        validate_document(&document, self.limits)?;
        let mut next = self.clone();
        let old = next
            .documents
            .insert(document.source_id().clone(), document.clone());
        next.patch_document_indexes(old.as_ref(), Some(&document), changed_canonical)?;
        Ok(next)
    }

    fn without_document(
        &self,
        source: &SourceId,
        changed_canonical: &BTreeSet<CatalogId>,
    ) -> Result<Self, Error> {
        let mut next = self.clone();
        let old = next.documents.remove(source).ok_or(Error::MissingSource)?;
        next.patch_document_indexes(Some(&old), None, changed_canonical)?;
        Ok(next)
    }

    fn patch_document_indexes(
        &mut self,
        old: Option<&CatalogDocument>,
        new: Option<&CatalogDocument>,
        impacted_ids: &BTreeSet<CatalogId>,
    ) -> Result<(), Error> {
        if self.documents.len() > self.limits.maximum_documents {
            return Err(Error::LimitExceeded("documents"));
        }
        let total_definitions = self
            .documents
            .values()
            .try_fold(0_usize, |count, document| {
                count
                    .checked_add(document.definitions.len())
                    .ok_or(Error::LimitExceeded("definitions"))
            })?;
        if total_definitions > self.limits.maximum_definitions {
            return Err(Error::LimitExceeded("definitions"));
        }
        let mut total_index_entries = 0_usize;
        for document in self.documents.values() {
            total_index_entries = checked_index_entries(
                total_index_entries,
                document.definitions(),
                self.limits.maximum_graph_work,
            )?;
        }

        let old_edges = unique_edges(&self.candidates, impacted_ids);
        if let Some(old) = old {
            for definition in &old.definitions {
                if !impacted_ids.contains(&definition.id) {
                    continue;
                }
                let remove_entry = if let Some(values) = self.candidates.get_mut(&definition.id) {
                    if let Some(position) = values.iter().position(|value| value == definition) {
                        values.remove(position);
                    }
                    values.is_empty()
                } else {
                    false
                };
                if remove_entry {
                    self.candidates.remove(&definition.id);
                }
            }
        }
        if let Some(new) = new {
            for definition in &new.definitions {
                if !impacted_ids.contains(&definition.id) {
                    continue;
                }
                self.candidates
                    .entry(definition.id.clone())
                    .or_default()
                    .push(definition.clone());
            }
        }
        for id in impacted_ids {
            if let Some(values) = self.candidates.get_mut(id) {
                values.sort();
            }
        }

        let impacted_aliases = old
            .into_iter()
            .chain(new)
            .flat_map(CatalogDocument::definitions)
            .filter(|definition| impacted_ids.contains(&definition.id))
            .flat_map(|definition| definition.aliases.iter().cloned())
            .collect::<BTreeSet<_>>();
        let replacement_alias_targets = collect_alias_targets(
            &self.candidates,
            impacted_ids,
            &impacted_aliases,
            self.limits.maximum_graph_work,
        )?;
        for alias in impacted_aliases {
            let mut targets = self.aliases.remove(&alias).unwrap_or_default();
            for target in &targets {
                if let Some(values) = self.aliases_by_target.get_mut(target) {
                    values.remove(&alias);
                    if values.is_empty() {
                        self.aliases_by_target.remove(target);
                    }
                }
            }
            targets.retain(|target| !impacted_ids.contains(target));
            if let Some(replacements) = replacement_alias_targets.get(&alias) {
                targets.extend(replacements.iter().cloned());
            }
            if !targets.is_empty() {
                for target in &targets {
                    self.aliases_by_target
                        .entry(target.clone())
                        .or_default()
                        .insert(alias.clone());
                }
                self.aliases.insert(alias, targets);
            }
        }

        for (target, dependent) in old_edges {
            if let Some(values) = self.dependents.get_mut(&target) {
                values.remove(&dependent);
                if values.is_empty() {
                    self.dependents.remove(&target);
                }
            }
        }
        for (target, dependent) in unique_edges(&self.candidates, impacted_ids) {
            self.dependents.entry(target).or_default().insert(dependent);
        }

        self.generation = generation(&self.documents);
        self.diagnostics = DiagnosticSet::with_limits(self.limits.diagnostic_limits);
        self.index_conflicts();
        self.index_references(false)?;
        self.index_cycles()?;
        Ok(())
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Query {
    pub domain: Option<Domain>,
    pub namespace: Option<String>,
    pub text: Option<String>,
    pub tags: BTreeSet<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Invalidation {
    pub source: SourceId,
    pub changed: BTreeSet<CatalogId>,
    pub affected: BTreeSet<CatalogId>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CatalogUpdate {
    pub catalog: Catalog,
    pub invalidation: Invalidation,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum FieldRule {
    Alias,
    Inherits,
    EmbeddedObject,
    Reference {
        domain: Domain,
        kind: ReferenceKind,
        optional: bool,
    },
    Label,
    Summary,
    Tag,
    Keyword,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LineDocumentLoader {
    domain: Domain,
    namespace: String,
    schema_version: u32,
    rules: BTreeMap<Vec<u8>, FieldRule>,
    limits: CatalogLimits,
}

impl LineDocumentLoader {
    pub fn new(
        domain: Domain,
        namespace: impl Into<String>,
        schema_version: u32,
        rules: impl IntoIterator<Item = (Vec<u8>, FieldRule)>,
    ) -> Result<Self, Error> {
        let namespace = namespace.into();
        if !valid_namespace(&namespace) || schema_version == 0 {
            return Err(Error::InvalidDocument);
        }
        let mut accepted = BTreeMap::new();
        let limits = CatalogLimits::default();
        let maximum_rules = limits
            .maximum_aliases_per_definition
            .checked_add(limits.maximum_references_per_definition)
            .and_then(|value| value.checked_add(limits.maximum_preview_values))
            .and_then(|value| value.checked_add(2))
            .ok_or(Error::LimitExceeded("loader rules"))?;
        for (field, rule) in rules {
            if accepted.len() >= maximum_rules
                || field.len() > limits.maximum_string_bytes
                || field.is_empty()
                || !field.iter().all(u8::is_ascii)
                || accepted.insert(field, rule).is_some()
            {
                return Err(Error::InvalidDocument);
            }
        }
        Ok(Self {
            domain,
            namespace,
            schema_version,
            rules: accepted,
            limits,
        })
    }

    pub fn with_limits(mut self, limits: CatalogLimits) -> Result<Self, Error> {
        let maximum_rules = limits
            .maximum_aliases_per_definition
            .checked_add(limits.maximum_references_per_definition)
            .and_then(|value| value.checked_add(limits.maximum_preview_values))
            .and_then(|value| value.checked_add(2))
            .ok_or(Error::LimitExceeded("loader rules"))?;
        validate_text(&self.namespace, limits)?;
        if self.rules.len() > maximum_rules
            || self
                .rules
                .keys()
                .any(|field| field.len() > limits.maximum_string_bytes)
        {
            return Err(Error::LimitExceeded("loader rules"));
        }
        self.limits = limits;
        Ok(self)
    }

    pub fn load_objects(
        &self,
        document: &Document,
        evidence: EvidenceReferences,
    ) -> Result<CatalogDocument, Error> {
        validate_evidence(&evidence, self.limits)?;
        let mut stack: Vec<Definition> = Vec::new();
        let mut embedded_depth = 0_usize;
        let mut definitions = Vec::new();
        for record in document.records() {
            match &record.kind {
                RecordKind::ObjectStart { name } => {
                    if embedded_depth != 0 {
                        return Err(Error::InvalidDocument);
                    }
                    let definition_count = definitions
                        .len()
                        .checked_add(stack.len())
                        .ok_or(Error::LimitExceeded("definitions per document"))?;
                    if definition_count >= self.limits.maximum_definitions_per_document {
                        return Err(Error::LimitExceeded("definitions per document"));
                    }
                    let name_text = text(document, *name, self.limits)?;
                    let id = CatalogId::new(self.domain, self.namespace.clone(), name_text)?;
                    stack.push(
                        Definition::new(id, Location::new(document.source_id().as_str(), *name))
                            .with_evidence(evidence.clone()),
                    );
                }
                RecordKind::Field { key, value } => {
                    let Some(definition) = stack.last_mut() else {
                        continue;
                    };
                    let Some(rule) = self
                        .rules
                        .get(document.bytes(*key).map_err(|_| Error::InvalidDocument)?)
                    else {
                        continue;
                    };
                    if *rule == FieldRule::EmbeddedObject {
                        embedded_depth = embedded_depth
                            .checked_add(1)
                            .ok_or(Error::LimitExceeded("object depth"))?;
                        if embedded_depth > self.limits.maximum_semantic_depth {
                            return Err(Error::LimitExceeded("object depth"));
                        }
                        continue;
                    }
                    if embedded_depth != 0 {
                        continue;
                    }
                    let value_text = text(document, *value, self.limits)?;
                    let location = Location::new(document.source_id().as_str(), *value);
                    apply_rule(
                        definition,
                        rule,
                        &self.namespace,
                        value_text,
                        location,
                        self.limits,
                    )?;
                }
                RecordKind::ObjectEnd => {
                    if embedded_depth != 0 {
                        embedded_depth -= 1;
                        continue;
                    }
                    let definition = stack.pop().ok_or(Error::InvalidDocument)?;
                    definitions.push(definition);
                }
                _ => {}
            }
        }
        if !stack.is_empty() || embedded_depth != 0 {
            return Err(Error::InvalidDocument);
        }
        Ok(CatalogDocument::new(
            document.source_id().clone(),
            document.revision(),
            self.schema_version,
            definitions,
        ))
    }

    pub fn load_single(
        &self,
        document: &Document,
        local_id: impl Into<String>,
        evidence: EvidenceReferences,
    ) -> Result<CatalogDocument, Error> {
        if self.limits.maximum_definitions_per_document == 0 {
            return Err(Error::LimitExceeded("definitions per document"));
        }
        validate_evidence(&evidence, self.limits)?;
        let local_id = local_id.into();
        validate_text(&local_id, self.limits)?;
        let id = CatalogId::new(self.domain, self.namespace.clone(), local_id)?;
        let mut definition = Definition::new(
            id,
            Location::new(
                document.source_id().as_str(),
                Span::new(0, document.source_bytes().len()),
            ),
        )
        .with_evidence(evidence);
        for record in document.records() {
            let RecordKind::Field { key, value } = &record.kind else {
                continue;
            };
            let Some(rule) = self
                .rules
                .get(document.bytes(*key).map_err(|_| Error::InvalidDocument)?)
            else {
                continue;
            };
            apply_rule(
                &mut definition,
                rule,
                &self.namespace,
                text(document, *value, self.limits)?,
                Location::new(document.source_id().as_str(), *value),
                self.limits,
            )?;
        }
        Ok(CatalogDocument::new(
            document.source_id().clone(),
            document.revision(),
            self.schema_version,
            vec![definition],
        ))
    }
}

fn apply_rule(
    definition: &mut Definition,
    rule: &FieldRule,
    namespace: &str,
    value: String,
    location: Location,
    limits: CatalogLimits,
) -> Result<(), Error> {
    validate_text(&value, limits)?;
    match rule {
        FieldRule::Alias => {
            let alias = CatalogId::new(definition.id.domain(), namespace, value)?;
            if !definition.aliases.contains(&alias)
                && definition.aliases.len() >= limits.maximum_aliases_per_definition
            {
                return Err(Error::LimitExceeded("aliases per definition"));
            }
            definition.aliases.insert(alias);
        }
        FieldRule::Inherits => {
            if definition.inherits.is_none()
                && definition_reference_count(definition)?
                    >= limits.maximum_references_per_definition
            {
                return Err(Error::LimitExceeded("references per definition"));
            }
            definition.inherits = Some(
                Reference::new(
                    CatalogId::new(definition.id.domain(), namespace, value)?,
                    ReferenceKind::Inherits,
                    location,
                )
                .with_semantic_path(["inherits"]),
            );
        }
        FieldRule::EmbeddedObject => return Err(Error::InvalidDocument),
        FieldRule::Reference {
            domain,
            kind,
            optional,
        } => {
            if *kind == ReferenceKind::Inherits
                || kind
                    .expected_domain()
                    .is_some_and(|expected| expected != *domain)
            {
                return Err(Error::InvalidDocument);
            }
            if definition_reference_count(definition)? >= limits.maximum_references_per_definition {
                return Err(Error::LimitExceeded("references per definition"));
            }
            definition.references.push(
                Reference::new(CatalogId::new(*domain, namespace, value)?, *kind, location)
                    .with_semantic_path(["references", kind.as_str()])
                    .optional(*optional),
            );
        }
        FieldRule::Label => definition.preview.label = Some(value),
        FieldRule::Summary => definition.preview.summary = Some(value),
        FieldRule::Tag => {
            let current = definition.preview.tags.len() + definition.preview.keywords.len();
            if !definition.preview.tags.contains(&value) && current >= limits.maximum_preview_values
            {
                return Err(Error::LimitExceeded("preview values"));
            }
            definition.preview.tags.insert(value);
        }
        FieldRule::Keyword => {
            let current = definition.preview.tags.len() + definition.preview.keywords.len();
            if !definition.preview.keywords.contains(&value)
                && current >= limits.maximum_preview_values
            {
                return Err(Error::LimitExceeded("preview values"));
            }
            definition.preview.keywords.insert(value);
        }
    }
    Ok(())
}

fn definition_reference_count(definition: &Definition) -> Result<usize, Error> {
    definition
        .references
        .len()
        .checked_add(usize::from(definition.inherits.is_some()))
        .and_then(|value| value.checked_add(definition.preview.media.len()))
        .ok_or(Error::LimitExceeded("references per definition"))
}

fn text(document: &Document, span: Span, limits: CatalogLimits) -> Result<String, Error> {
    if span.len() > limits.maximum_string_bytes {
        return Err(Error::LimitExceeded("string bytes"));
    }
    std::str::from_utf8(document.bytes(span).map_err(|_| Error::InvalidDocument)?)
        .map(str::to_owned)
        .map_err(|_| Error::InvalidDocument)
}

fn validate_evidence(evidence: &EvidenceReferences, limits: CatalogLimits) -> Result<(), Error> {
    for value in evidence.provenance.iter().chain(evidence.license.iter()) {
        validate_text(value, limits)?;
    }
    Ok(())
}

fn validate_document(document: &CatalogDocument, limits: CatalogLimits) -> Result<(), Error> {
    if document.schema_version == 0 {
        return Err(Error::InvalidDocument);
    }
    if document.definitions.len() > limits.maximum_definitions_per_document {
        return Err(Error::LimitExceeded("definitions per document"));
    }
    for definition in &document.definitions {
        let reference_count = definition
            .references
            .len()
            .checked_add(usize::from(definition.inherits.is_some()))
            .and_then(|value| value.checked_add(definition.preview.media.len()))
            .ok_or(Error::LimitExceeded("references per definition"))?;
        let preview_count = definition
            .preview
            .tags
            .len()
            .checked_add(definition.preview.keywords.len())
            .and_then(|value| value.checked_add(definition.preview.media.len()))
            .ok_or(Error::LimitExceeded("preview values"))?;
        if definition.location.source != document.source_id.as_str()
            || definition.aliases.len() > limits.maximum_aliases_per_definition
            || reference_count > limits.maximum_references_per_definition
            || preview_count > limits.maximum_preview_values
        {
            return Err(Error::InvalidDocument);
        }
        validate_text(definition.id.namespace(), limits)?;
        validate_text(definition.id.local(), limits)?;
        for alias in &definition.aliases {
            if alias.domain() != definition.id.domain() {
                return Err(Error::InvalidDocument);
            }
            validate_text(alias.namespace(), limits)?;
            validate_text(alias.local(), limits)?;
        }
        if let Some(inheritance) = &definition.inherits {
            if inheritance.kind != ReferenceKind::Inherits
                || inheritance.target.domain() != definition.id.domain()
            {
                return Err(Error::InvalidDocument);
            }
            validate_reference(inheritance, document, limits)?;
        }
        for reference in definition
            .references
            .iter()
            .chain(definition.preview.media.values())
        {
            if reference.kind == ReferenceKind::Inherits
                || reference
                    .kind
                    .expected_domain()
                    .is_some_and(|domain| domain != reference.target.domain())
            {
                return Err(Error::InvalidDocument);
            }
            validate_reference(reference, document, limits)?;
        }
        for value in definition
            .preview
            .label
            .iter()
            .chain(definition.preview.summary.iter())
            .chain(definition.preview.tags.iter())
            .chain(definition.preview.keywords.iter())
            .chain(definition.preview.media.keys())
            .chain(definition.evidence.provenance.iter())
            .chain(definition.evidence.license.iter())
        {
            validate_text(value, limits)?;
        }
    }
    Ok(())
}

fn validate_reference(
    reference: &Reference,
    document: &CatalogDocument,
    limits: CatalogLimits,
) -> Result<(), Error> {
    if reference.location.source != document.source_id.as_str()
        || reference.semantic_path.len() > limits.maximum_semantic_depth
    {
        return Err(Error::InvalidDocument);
    }
    validate_text(reference.target.namespace(), limits)?;
    validate_text(reference.target.local(), limits)?;
    for segment in &reference.semantic_path {
        validate_text(segment, limits)?;
    }
    Ok(())
}

fn validate_text(value: &str, limits: CatalogLimits) -> Result<(), Error> {
    if value.len() > limits.maximum_string_bytes || value.contains('\0') {
        Err(Error::LimitExceeded("string bytes"))
    } else {
        Ok(())
    }
}

fn unique_edges(
    candidates: &BTreeMap<CatalogId, Vec<Definition>>,
    ids: &BTreeSet<CatalogId>,
) -> BTreeSet<(CatalogId, CatalogId)> {
    ids.iter()
        .filter_map(|id| candidates.get(id).filter(|values| values.len() == 1))
        .flat_map(|values| {
            values[0]
                .all_references()
                .map(|reference| (reference.target.clone(), values[0].id.clone()))
        })
        .collect()
}

fn checked_index_entries(
    initial: usize,
    definitions: &[Definition],
    maximum: usize,
) -> Result<usize, Error> {
    let total = definitions.iter().try_fold(initial, |count, definition| {
        count
            .checked_add(definition.aliases.len())
            .and_then(|value| value.checked_add(definition.all_references().count()))
            .ok_or(Error::LimitExceeded("index entries"))
    })?;
    if total > maximum {
        Err(Error::LimitExceeded("index entries"))
    } else {
        Ok(total)
    }
}

fn collect_alias_targets(
    candidates: &BTreeMap<CatalogId, Vec<Definition>>,
    ids: &BTreeSet<CatalogId>,
    aliases: &BTreeSet<CatalogId>,
    maximum_work: usize,
) -> Result<BTreeMap<CatalogId, BTreeSet<CatalogId>>, Error> {
    let mut targets = BTreeMap::<CatalogId, BTreeSet<CatalogId>>::new();
    let mut work = 0_usize;
    for id in ids {
        let Some(definitions) = candidates.get(id) else {
            continue;
        };
        for definition in definitions {
            work = work
                .checked_add(1)
                .ok_or(Error::LimitExceeded("graph work"))?;
            if work > maximum_work {
                return Err(Error::LimitExceeded("graph work"));
            }
            for alias in &definition.aliases {
                work = work
                    .checked_add(1)
                    .ok_or(Error::LimitExceeded("graph work"))?;
                if work > maximum_work {
                    return Err(Error::LimitExceeded("graph work"));
                }
                if aliases.contains(alias) {
                    targets.entry(alias.clone()).or_default().insert(id.clone());
                }
            }
        }
    }
    Ok(targets)
}

fn reverse_aliases(
    aliases: &BTreeMap<CatalogId, BTreeSet<CatalogId>>,
) -> BTreeMap<CatalogId, BTreeSet<CatalogId>> {
    let mut reverse = BTreeMap::<CatalogId, BTreeSet<CatalogId>>::new();
    for (alias, targets) in aliases {
        for target in targets {
            reverse
                .entry(target.clone())
                .or_default()
                .insert(alias.clone());
        }
    }
    reverse
}

fn changed_identities(
    old: Option<&CatalogDocument>,
    new: Option<&CatalogDocument>,
    maximum: usize,
) -> Result<(BTreeSet<CatalogId>, BTreeSet<CatalogId>), Error> {
    let mut old_definitions: BTreeMap<&CatalogId, Vec<&Definition>> = BTreeMap::new();
    let mut new_definitions: BTreeMap<&CatalogId, Vec<&Definition>> = BTreeMap::new();
    for definition in old.into_iter().flat_map(CatalogDocument::definitions) {
        old_definitions
            .entry(&definition.id)
            .or_default()
            .push(definition);
    }
    for definition in new.into_iter().flat_map(CatalogDocument::definitions) {
        new_definitions
            .entry(&definition.id)
            .or_default()
            .push(definition);
    }
    for values in old_definitions
        .values_mut()
        .chain(new_definitions.values_mut())
    {
        values.sort();
    }
    let schema_changed = old
        .zip(new)
        .is_some_and(|(left, right)| left.schema_version() != right.schema_version());
    let ids = old_definitions.keys().chain(new_definitions.keys());
    let mut canonical = BTreeSet::new();
    let mut changed = BTreeSet::new();
    for id in ids {
        let differs = schema_changed || old_definitions.get(id) != new_definitions.get(id);
        if differs {
            insert_bounded(&mut canonical, (*id).clone(), maximum)?;
            insert_bounded(&mut changed, (*id).clone(), maximum)?;
            for alias in old_definitions
                .get(id)
                .into_iter()
                .chain(new_definitions.get(id))
                .flat_map(|values| values.iter())
                .flat_map(|definition| &definition.aliases)
            {
                insert_bounded(&mut changed, alias.clone(), maximum)?;
            }
        }
    }
    Ok((canonical, changed))
}

fn expand_changed_aliases<'a>(
    changed: &mut BTreeSet<CatalogId>,
    canonical: &BTreeSet<CatalogId>,
    indexes: impl IntoIterator<Item = &'a BTreeMap<CatalogId, BTreeSet<CatalogId>>>,
    maximum: usize,
) -> Result<(), Error> {
    for aliases in indexes {
        for (alias, targets) in aliases {
            if !targets.is_disjoint(canonical) {
                insert_bounded(changed, alias.clone(), maximum)?;
            }
        }
    }
    Ok(())
}

fn insert_bounded(
    values: &mut BTreeSet<CatalogId>,
    value: CatalogId,
    maximum: usize,
) -> Result<(), Error> {
    if !values.contains(&value) && values.len() >= maximum {
        return Err(Error::LimitExceeded("invalidation"));
    }
    values.insert(value);
    Ok(())
}

fn generation(documents: &BTreeMap<SourceId, CatalogDocument>) -> Generation {
    let mut digest = Sha256::new();
    digest.update(b"atrinik-catalog-generation-v1\0");
    digest_usize(&mut digest, documents.len());
    for document in documents.values() {
        digest_str(&mut digest, document.source_id.as_str());
        digest.update(document.revision.bytes());
        digest.update(document.schema_version.to_be_bytes());
        let mut definitions = document.definitions.clone();
        for definition in &mut definitions {
            definition.references.sort();
        }
        definitions.sort();
        digest_usize(&mut digest, definitions.len());
        for definition in definitions {
            digest_id(&mut digest, &definition.id);
            digest_location(&mut digest, &definition.location);
            digest_usize(&mut digest, definition.aliases.len());
            for alias in definition.aliases {
                digest_id(&mut digest, &alias);
            }
            digest_reference(&mut digest, definition.inherits.as_ref());
            let references = definition.references;
            digest_usize(&mut digest, references.len());
            for reference in &references {
                digest_reference(&mut digest, Some(reference));
            }
            digest_option(&mut digest, definition.preview.label.as_deref());
            digest_option(&mut digest, definition.preview.summary.as_deref());
            for values in [&definition.preview.tags, &definition.preview.keywords] {
                digest_usize(&mut digest, values.len());
                for value in values {
                    digest_str(&mut digest, value);
                }
            }
            digest_usize(&mut digest, definition.preview.media.len());
            for (name, reference) in definition.preview.media {
                digest_str(&mut digest, &name);
                digest_reference(&mut digest, Some(&reference));
            }
            digest_option(&mut digest, definition.evidence.provenance.as_deref());
            digest_option(&mut digest, definition.evidence.license.as_deref());
        }
    }
    Generation(digest.finalize().into())
}

fn digest_reference(digest: &mut Sha256, reference: Option<&Reference>) {
    let Some(reference) = reference else {
        digest.update([0]);
        return;
    };
    digest.update([1]);
    digest_id(digest, &reference.target);
    digest_str(digest, reference.kind.as_str());
    digest_location(digest, &reference.location);
    digest.update([u8::from(reference.optional)]);
    digest_usize(digest, reference.semantic_path.len());
    for segment in &reference.semantic_path {
        digest_str(digest, segment);
    }
}

fn digest_location(digest: &mut Sha256, location: &Location) {
    digest_str(digest, &location.source);
    digest.update((location.span.start as u64).to_be_bytes());
    digest.update((location.span.end as u64).to_be_bytes());
}

fn digest_id(digest: &mut Sha256, id: &CatalogId) {
    digest_str(digest, id.domain().as_str());
    digest_str(digest, id.namespace());
    digest_str(digest, id.local());
}

fn digest_option(digest: &mut Sha256, value: Option<&str>) {
    match value {
        Some(value) => {
            digest.update([1]);
            digest_str(digest, value);
        }
        None => {
            digest.update([0]);
        }
    }
}

fn digest_str(digest: &mut Sha256, value: &str) {
    digest.update((value.len() as u64).to_be_bytes());
    digest.update(value.as_bytes());
}

fn digest_usize(digest: &mut Sha256, value: usize) {
    digest.update((value as u64).to_be_bytes());
}

fn valid_namespace(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_lowercase()
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-' | b'.')
        })
}

fn valid_local_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.' | b'/'))
        && !value.starts_with('/')
        && !value.ends_with('/')
        && !value
            .split('/')
            .any(|segment| segment.is_empty() || segment == "." || segment == "..")
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    InvalidIdentifier,
    InvalidDocument,
    DuplicateSource,
    MissingSource,
    LimitExceeded(&'static str),
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidIdentifier => write!(formatter, "catalog identifier is invalid"),
            Self::InvalidDocument => write!(formatter, "catalog document is invalid"),
            Self::DuplicateSource => write!(formatter, "catalog source identity is duplicated"),
            Self::MissingSource => write!(formatter, "catalog source identity is not indexed"),
            Self::LimitExceeded(limit) => write!(formatter, "catalog {limit} limit is exceeded"),
        }
    }
}

impl std::error::Error for Error {}

#[cfg(test)]
mod tests;
