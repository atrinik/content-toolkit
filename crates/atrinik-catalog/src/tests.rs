// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

use std::{collections::BTreeSet, sync::Arc};

use atrinik_diagnostics::{DiagnosticLimits, Location, Span, SuppressionPolicy};
use atrinik_source::{Document, Limits, SourceId};

use super::{
    Catalog, CatalogDocument, CatalogId, CatalogLimits, Definition, Domain, EvidenceReferences,
    FieldRule, LineDocumentLoader, PreviewMetadata, Query, Reference, ReferenceKind, Resolution,
};

fn source(name: &str, bytes: &[u8]) -> Document {
    Document::parse(
        SourceId::new(format!("fixture:{name}")).unwrap(),
        Arc::<[u8]>::from(bytes),
        Limits::default(),
    )
    .unwrap()
}

fn id(domain: Domain, name: &str) -> CatalogId {
    CatalogId::new(domain, "core", name).unwrap()
}

fn definition(document: &Document, domain: Domain, name: &str) -> Definition {
    Definition::new(
        id(domain, name),
        Location::new(document.source_id().as_str(), Span::new(0, 1)),
    )
}

fn input(document: &Document, definitions: Vec<Definition>) -> CatalogDocument {
    CatalogDocument::new(
        document.source_id().clone(),
        document.revision(),
        1,
        definitions,
    )
}

fn build(documents: Vec<CatalogDocument>) -> Catalog {
    Catalog::build(
        documents,
        CatalogLimits::default(),
        SuppressionPolicy::default(),
    )
    .unwrap()
}

#[test]
fn covers_every_owned_domain_with_stable_order_and_evidence() {
    let mut documents = Vec::new();
    for domain in Domain::ALL.into_iter().rev() {
        let document = source(domain.as_str(), b"name localized\n");
        let mut preview = PreviewMetadata {
            label: Some(format!("Localized {}", domain.as_str())),
            ..PreviewMetadata::default()
        };
        preview.tags.insert("public".to_owned());
        documents.push(input(
            &document,
            vec![
                definition(&document, domain, "stable-id")
                    .with_preview(preview)
                    .with_evidence(EvidenceReferences {
                        provenance: Some("registry:synthetic".to_owned()),
                        license: Some("LicenseRef-Synthetic".to_owned()),
                    }),
            ],
        ));
    }
    let catalog = build(documents);
    let domains: Vec<Domain> = catalog
        .definitions()
        .map(|value| value.id.domain())
        .collect();
    assert_eq!(domains, Domain::ALL);
    assert!(catalog.diagnostics().values().is_empty());
    assert_eq!(
        catalog
            .definitions()
            .next()
            .unwrap()
            .evidence
            .license
            .as_deref(),
        Some("LicenseRef-Synthetic")
    );
}

#[test]
fn reports_duplicate_alias_missing_and_ambiguous_references() {
    let first = source("first", b"name first\n");
    let second = source("second", b"name second\n");
    let missing = id(Domain::Archetype, "missing");
    let shared_alias = id(Domain::Archetype, "shared");
    let duplicate = id(Domain::Archetype, "duplicate");
    let one = definition(&first, Domain::Archetype, "one")
        .with_alias(shared_alias.clone())
        .with_reference(
            Reference::new(
                missing,
                ReferenceKind::Generic,
                Location::new(first.source_id().as_str(), Span::new(2, 3)),
            )
            .with_semantic_path(["references", "missing"]),
        )
        .with_reference(
            Reference::new(
                duplicate.clone(),
                ReferenceKind::Generic,
                Location::new(first.source_id().as_str(), Span::new(3, 4)),
            )
            .with_semantic_path(["references", "ambiguous"]),
        );
    let two = definition(&second, Domain::Archetype, "two").with_alias(shared_alias.clone());
    let duplicate_first = definition(&first, Domain::Archetype, "duplicate");
    let duplicate_second = definition(&second, Domain::Archetype, "duplicate");
    let catalog = build(vec![
        input(&second, vec![two, duplicate_second]),
        input(&first, vec![one, duplicate_first]),
    ]);
    let codes: Vec<&str> = catalog
        .diagnostics()
        .values()
        .iter()
        .map(|value| value.code)
        .collect();
    assert_eq!(
        codes,
        [
            "catalog.duplicate_id",
            "catalog.ambiguous_alias",
            "catalog.ambiguous_reference",
            "catalog.missing_reference",
        ]
    );
    assert!(matches!(
        catalog.resolve(&shared_alias),
        Resolution::Ambiguous
    ));
    assert!(matches!(catalog.resolve(&duplicate), Resolution::Ambiguous));
    assert!(catalog.diagnostics().has_errors());
}

#[test]
fn detects_inheritance_cycles_and_resolves_aliases() {
    let document = source("cycles", b"name cycles\n");
    let alias = id(Domain::Archetype, "former-a");
    let a = definition(&document, Domain::Archetype, "a")
        .with_alias(alias.clone())
        .with_inheritance(Reference::new(
            id(Domain::Archetype, "b"),
            ReferenceKind::Inherits,
            Location::new(document.source_id().as_str(), Span::new(1, 2)),
        ));
    let b = definition(&document, Domain::Archetype, "b").with_inheritance(Reference::new(
        id(Domain::Archetype, "a"),
        ReferenceKind::Inherits,
        Location::new(document.source_id().as_str(), Span::new(2, 3)),
    ));
    let catalog = build(vec![input(&document, vec![b, a])]);
    assert!(matches!(catalog.resolve(&alias), Resolution::Found(value) if value.id.local() == "a"));
    assert_eq!(
        catalog
            .diagnostics()
            .values()
            .iter()
            .filter(|value| value.code == "catalog.inheritance_cycle")
            .count(),
        1
    );
}

#[test]
fn incremental_rename_invalidates_only_changed_ids_and_dependents() {
    let target = source("target", b"name target\n");
    let dependent = source("dependent", b"name dependent\n");
    let unrelated = source("unrelated", b"name unrelated\n");
    let target_id = id(Domain::Quest, "old-name");
    let dependent_definition =
        definition(&dependent, Domain::Interface, "journal").with_reference(Reference::new(
            target_id.clone(),
            ReferenceKind::Quest,
            Location::new(dependent.source_id().as_str(), Span::new(0, 1)),
        ));
    let original = build(vec![
        input(
            &target,
            vec![definition(&target, Domain::Quest, "old-name")],
        ),
        input(&dependent, vec![dependent_definition.clone()]),
        input(
            &unrelated,
            vec![definition(&unrelated, Domain::Map, "elsewhere")],
        ),
    ]);
    let edited = source("target", b"name target changed\n");
    let replacement = input(
        &edited,
        vec![definition(&edited, Domain::Quest, "new-name")],
    );
    let update = original.update_document(replacement.clone()).unwrap();
    assert_eq!(
        update.invalidation.changed,
        BTreeSet::from([id(Domain::Quest, "new-name"), id(Domain::Quest, "old-name"),])
    );
    assert_eq!(
        update.invalidation.affected,
        BTreeSet::from([
            id(Domain::Interface, "journal"),
            id(Domain::Quest, "new-name"),
            id(Domain::Quest, "old-name"),
        ])
    );
    let clean = build(vec![
        replacement,
        input(&dependent, vec![dependent_definition]),
        input(
            &unrelated,
            vec![definition(&unrelated, Domain::Map, "elsewhere")],
        ),
    ]);
    assert_eq!(update.catalog, clean);
    assert_eq!(update.catalog.generation(), clean.generation());
}

#[test]
fn incremental_edit_excludes_unchanged_siblings_and_enforces_bounds() {
    let document = source("multi", b"name multi\n");
    let original = build(vec![input(
        &document,
        vec![
            definition(&document, Domain::Quest, "changed"),
            definition(&document, Domain::Quest, "untouched"),
        ],
    )]);
    let replacement = input(
        &source("multi", b"name changed revision\n"),
        vec![
            definition(&document, Domain::Quest, "changed").with_preview(PreviewMetadata {
                summary: Some("changed semantics".to_owned()),
                ..PreviewMetadata::default()
            }),
            definition(&document, Domain::Quest, "untouched"),
        ],
    );
    let update = original.update_document(replacement).unwrap();
    assert_eq!(
        update.invalidation.changed,
        BTreeSet::from([id(Domain::Quest, "changed")])
    );
    assert!(
        !update
            .invalidation
            .affected
            .contains(&id(Domain::Quest, "untouched"))
    );

    let limits = CatalogLimits {
        maximum_documents: 1,
        maximum_invalidation: 0,
        ..CatalogLimits::default()
    };
    let bounded = Catalog::build(
        [input(
            &document,
            vec![definition(&document, Domain::Quest, "one")],
        )],
        limits,
        SuppressionPolicy::default(),
    )
    .unwrap();
    let added = source("added", b"name added\n");
    assert!(
        bounded
            .update_document(input(
                &added,
                vec![definition(&added, Domain::Quest, "two")]
            ))
            .is_err()
    );
    assert!(bounded.remove_document(document.source_id()).is_err());
}

#[test]
fn same_digest_and_semantics_are_an_incremental_noop() {
    let document = source("noop", b"name noop\n");
    let input = input(
        &document,
        vec![definition(&document, Domain::Resource, "noop")],
    );
    let catalog = build(vec![input.clone()]);
    let update = catalog.update_document(input).unwrap();
    assert!(update.invalidation.changed.is_empty());
    assert!(update.invalidation.affected.is_empty());
    assert_eq!(update.catalog.generation(), catalog.generation());
}

#[test]
fn schema_evolution_and_semantics_change_generation() {
    let document = source("schema", b"name schema\n");
    let definition = definition(&document, Domain::Resource, "schema");
    let first = build(vec![CatalogDocument::new(
        document.source_id().clone(),
        document.revision(),
        1,
        vec![definition.clone()],
    )]);
    let second = build(vec![CatalogDocument::new(
        document.source_id().clone(),
        document.revision(),
        2,
        vec![definition],
    )]);
    assert_ne!(first.generation(), second.generation());
}

#[test]
fn generation_is_canonical_for_tied_definitions_and_references() {
    let document = source("canonical", b"name canonical\n");
    let reference = |path: &str, optional: bool| {
        Reference::new(
            id(Domain::Resource, "target"),
            ReferenceKind::Resource,
            Location::new(document.source_id().as_str(), Span::new(0, 1)),
        )
        .with_semantic_path([path])
        .optional(optional)
    };
    let first_definition = definition(&document, Domain::Resource, "duplicate")
        .with_reference(reference("one", false))
        .with_reference(reference("two", true));
    let second_definition =
        definition(&document, Domain::Resource, "duplicate").with_preview(PreviewMetadata {
            label: Some("different".to_owned()),
            ..PreviewMetadata::default()
        });
    let target = definition(&document, Domain::Resource, "target");
    let first = build(vec![input(
        &document,
        vec![
            first_definition.clone(),
            second_definition.clone(),
            target.clone(),
        ],
    )]);
    let second = build(vec![input(
        &document,
        vec![second_definition, first_definition, target],
    )]);
    assert_eq!(first.generation(), second.generation());
}

#[test]
fn query_filter_and_preview_do_not_require_payload_access() {
    let document = source("query", b"opaque payload\n");
    let mut preview = PreviewMetadata {
        label: Some("Localized Silver Sword".to_owned()),
        summary: Some("A preview only".to_owned()),
        ..PreviewMetadata::default()
    };
    preview.tags.insert("weapon".to_owned());
    preview.keywords.insert("blade".to_owned());
    let catalog = build(vec![input(
        &document,
        vec![definition(&document, Domain::Archetype, "silver_sword").with_preview(preview)],
    )]);
    let query = Query {
        domain: Some(Domain::Archetype),
        namespace: Some("core".to_owned()),
        text: Some("blade".to_owned()),
        tags: BTreeSet::from(["weapon".to_owned()]),
    };
    let results = catalog.search(&query, 1);
    assert_eq!(results[0].id.local(), "silver_sword");
    assert_eq!(
        catalog.preview(&results[0].id).unwrap().label.as_deref(),
        Some("Localized Silver Sword")
    );
}

#[test]
fn media_references_are_resolved_diagnosed_and_invalidate_consumers() {
    let document = source("media", b"name media\n");
    let target = id(Domain::Face, "portrait");
    let mut preview = PreviewMetadata::default();
    preview.media.insert(
        "portrait".to_owned(),
        Reference::new(
            target.clone(),
            ReferenceKind::Face,
            Location::new(document.source_id().as_str(), Span::new(0, 1)),
        )
        .with_semantic_path(["preview", "media", "portrait"]),
    );
    let consumer = definition(&document, Domain::Quest, "consumer").with_preview(preview);
    let missing = build(vec![input(&document, vec![consumer.clone()])]);
    assert_eq!(
        missing.diagnostics().values()[0].code,
        "catalog.missing_reference"
    );

    let face = source("face-media", b"name face\n");
    let catalog = build(vec![
        input(&document, vec![consumer]),
        input(&face, vec![definition(&face, Domain::Face, "portrait")]),
    ]);
    assert_eq!(
        catalog.dependents(&target).next(),
        Some(&id(Domain::Quest, "consumer"))
    );
    let update = catalog.remove_document(face.source_id()).unwrap();
    assert!(
        update
            .invalidation
            .affected
            .contains(&id(Domain::Quest, "consumer"))
    );
    assert_eq!(
        update.catalog.diagnostics().values()[0].code,
        "catalog.missing_reference"
    );
}

#[test]
fn rejects_cross_domain_aliases_inheritance_and_typed_references() {
    let document = source("types", b"name types\n");
    let invalid_alias =
        definition(&document, Domain::Archetype, "value").with_alias(id(Domain::Map, "alias"));
    assert!(
        Catalog::build(
            [input(&document, vec![invalid_alias])],
            CatalogLimits::default(),
            SuppressionPolicy::default(),
        )
        .is_err()
    );
    let invalid_reference =
        definition(&document, Domain::Archetype, "value").with_reference(Reference::new(
            id(Domain::Map, "target"),
            ReferenceKind::Face,
            Location::new(document.source_id().as_str(), Span::new(0, 1)),
        ));
    assert!(
        Catalog::build(
            [input(&document, vec![invalid_reference])],
            CatalogLimits::default(),
            SuppressionPolicy::default(),
        )
        .is_err()
    );
}

#[test]
fn filesystem_order_and_localized_labels_do_not_control_ids_or_order() {
    let a = source("z-path", b"name Zulu\n");
    let b = source("a-path", b"name Alpha\n");
    let with_label = |document: &Document, name: &str, label: &str| {
        definition(document, Domain::Face, name).with_preview(PreviewMetadata {
            label: Some(label.to_owned()),
            ..PreviewMetadata::default()
        })
    };
    let first = build(vec![
        input(&a, vec![with_label(&a, "a", "Zulu")]),
        input(&b, vec![with_label(&b, "b", "Alpha")]),
    ]);
    let second = build(vec![
        input(&b, vec![with_label(&b, "b", "Different")]),
        input(&a, vec![with_label(&a, "a", "Other")]),
    ]);
    let ids: Vec<&str> = first.definitions().map(|value| value.id.local()).collect();
    assert_eq!(ids, ["a", "b"]);
    let second_ids: Vec<&str> = second.definitions().map(|value| value.id.local()).collect();
    assert_eq!(second_ids, ["a", "b"]);
}

#[test]
fn line_loader_exposes_one_shared_domain_loading_boundary() {
    let document = source(
        "loader",
        b"Object child\nname Child label\nalias old_child\nparent parent\nface child.101\nend\nObject parent\nname Parent label\nend\n",
    );
    let loader = LineDocumentLoader::new(
        Domain::Archetype,
        "core",
        1,
        [
            (b"name".to_vec(), FieldRule::Label),
            (b"alias".to_vec(), FieldRule::Alias),
            (b"parent".to_vec(), FieldRule::Inherits),
            (
                b"face".to_vec(),
                FieldRule::Reference {
                    domain: Domain::Face,
                    kind: ReferenceKind::Face,
                    optional: false,
                },
            ),
        ],
    )
    .unwrap();
    let loaded = loader
        .load_objects(&document, EvidenceReferences::default())
        .unwrap();
    let face_document = source("face", b"name face\n");
    let catalog = build(vec![
        loaded,
        input(
            &face_document,
            vec![definition(&face_document, Domain::Face, "child.101")],
        ),
    ]);
    assert!(matches!(
        catalog.resolve(&id(Domain::Archetype, "old_child")),
        Resolution::Found(value) if value.id.local() == "child"
    ));
    assert!(catalog.diagnostics().values().is_empty());
}

#[test]
fn handles_large_bounded_graph_and_truncates_diagnostics() {
    let document = source("large", b"name large\n");
    let mut definitions = Vec::new();
    for index in 0..4096 {
        let mut value = definition(&document, Domain::Resource, &format!("node-{index:04}"));
        if index != 0 {
            value = value.with_reference(Reference::new(
                id(Domain::Resource, &format!("node-{:04}", index - 1)),
                ReferenceKind::Resource,
                Location::new(document.source_id().as_str(), Span::new(index, index + 1)),
            ));
        }
        definitions.push(value);
    }
    let limits = CatalogLimits {
        maximum_graph_work: 20_000,
        diagnostic_limits: DiagnosticLimits {
            maximum_diagnostics: 2,
            ..DiagnosticLimits::default()
        },
        ..CatalogLimits::default()
    };
    let catalog = Catalog::build(
        [input(&document, definitions)],
        limits,
        SuppressionPolicy::default(),
    )
    .unwrap();
    assert_eq!(catalog.definitions().count(), 4096);
    assert!(catalog.diagnostics().values().is_empty());

    let invalid = source("invalid", b"name invalid\n");
    let definitions = (0..8)
        .map(|index| {
            definition(&invalid, Domain::Quest, &format!("quest-{index}")).with_reference(
                Reference::new(
                    id(Domain::Quest, &format!("missing-{index}")),
                    ReferenceKind::Quest,
                    Location::new(invalid.source_id().as_str(), Span::new(index, index + 1)),
                ),
            )
        })
        .collect();
    let catalog = Catalog::build(
        [input(&invalid, definitions)],
        limits,
        SuppressionPolicy::default(),
    )
    .unwrap();
    assert_eq!(catalog.diagnostics().values().len(), 2);
    assert!(catalog.diagnostics().truncated());
}

#[test]
fn optional_missing_references_follow_explicit_suppression_policy() {
    let document = source("suppressed", b"name suppressed\n");
    let definition = definition(&document, Domain::Quest, "quest").with_reference(
        Reference::new(
            id(Domain::Map, "optional-map"),
            ReferenceKind::Map,
            Location::new(document.source_id().as_str(), Span::new(0, 1)),
        )
        .optional(true),
    );
    let policy = SuppressionPolicy::new(["catalog.missing_reference"], 8, 64).unwrap();
    let catalog = Catalog::build(
        [input(&document, vec![definition])],
        CatalogLimits::default(),
        policy,
    )
    .unwrap();
    assert!(catalog.diagnostics().values()[0].suppressed);
    assert!(!catalog.diagnostics().has_errors());
}
