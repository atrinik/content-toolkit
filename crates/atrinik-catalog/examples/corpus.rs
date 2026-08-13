// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    error::Error,
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
    sync::Arc,
};

use atrinik_catalog::{
    Catalog, CatalogDocument, CatalogId, CatalogLimits, Definition, Domain, EvidenceReferences,
    FieldRule, LineDocumentLoader, ReferenceKind,
};
use atrinik_diagnostics::{DiagnosticLimits, Location, Span, SuppressionPolicy};
use atrinik_source::{Document, Limits, RecordKind, SourceId};
use sha2::{Digest, Sha256};

const MAXIMUM_FILES: usize = 25_000;
const MAXIMUM_ENTRIES: usize = 25_000;
const MAXIMUM_DIAGNOSTICS: usize = 200_000;

fn main() {
    if let Err(error) = run() {
        eprintln!("content catalog corpus: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let mut arguments = env::args_os().skip(1);
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--root")) {
        return Err("usage: corpus --root PATH --revision COMMIT".into());
    }
    let root = normalize_root(PathBuf::from(
        arguments.next().ok_or("missing corpus root")?,
    ));
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--revision")) {
        return Err("usage: corpus --root PATH --revision COMMIT".into());
    }
    let revision = arguments
        .next()
        .and_then(|value| value.into_string().ok())
        .ok_or("missing UTF-8 corpus revision")?;
    if arguments.next().is_some()
        || revision.len() != 40
        || !revision.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        return Err("invalid corpus revision or trailing argument".into());
    }
    validate_root(&root)?;

    let archetypes = LineDocumentLoader::new(
        Domain::Archetype,
        "classic",
        1,
        [
            (b"arch".to_vec(), FieldRule::EmbeddedObject),
            reference(
                "other_arch",
                Domain::Archetype,
                ReferenceKind::Archetype,
                false,
            ),
            reference("face", Domain::Face, ReferenceKind::Face, true),
            reference(
                "animation",
                Domain::Animation,
                ReferenceKind::Animation,
                true,
            ),
            reference(
                "randomitems",
                Domain::Treasure,
                ReferenceKind::Treasure,
                true,
            ),
        ],
    )?;
    let maps = LineDocumentLoader::new(
        Domain::Map,
        "classic",
        1,
        [reference(
            "arch",
            Domain::Archetype,
            ReferenceKind::Archetype,
            false,
        )],
    )?;
    let catalog_limits = CatalogLimits {
        diagnostic_limits: DiagnosticLimits {
            maximum_diagnostics: MAXIMUM_DIAGNOSTICS,
            ..DiagnosticLimits::default()
        },
        ..CatalogLimits::default()
    };
    let mut documents = Vec::new();
    let mut definition_count = 0_usize;
    let mut resources = BTreeSet::new();
    let mut faces = Vec::new();
    let mut paths = authored_paths(&root)?;
    paths.sort();
    for path in paths {
        let relative = normalized_relative(path.strip_prefix(&root)?)?;
        resources.insert(relative.clone());
        if let Some(face) = face_id(&relative) {
            faces.push(face);
        }
        let Some(domain) = classify(&relative) else {
            continue;
        };
        let source = Arc::<[u8]>::from(read_bounded(&path, Limits::default().maximum_file_bytes)?);
        let document = Document::parse(
            SourceId::new(format!("content:{relative}"))?,
            source,
            Limits::default(),
        )?;
        let evidence = evidence(&relative);
        let catalog_document = (|| -> Result<CatalogDocument, Box<dyn Error>> {
            Ok(match domain {
                Domain::Archetype => archetypes.load_objects(&document, evidence)?,
                Domain::Map => maps.load_single(&document, stable_path_id(&relative), evidence)?,
                Domain::Animation => {
                    definitions_from_fields(&document, Domain::Animation, &[b"anim"], evidence)?
                }
                Domain::Treasure => definitions_from_fields(
                    &document,
                    Domain::Treasure,
                    &[b"treasure", b"treasureone"],
                    evidence,
                )?,
                Domain::Faction => {
                    definitions_from_fields(&document, Domain::Faction, &[b"faction"], evidence)?
                }
                domain @ (Domain::Interface | Domain::Quest) => CatalogDocument::new(
                    document.source_id().clone(),
                    document.revision(),
                    1,
                    vec![
                        Definition::new(
                            CatalogId::new(domain, "classic", stable_path_id(&relative))?,
                            Location::new(
                                document.source_id().as_str(),
                                Span::new(0, document.source_bytes().len()),
                            ),
                        )
                        .with_evidence(evidence),
                    ],
                ),
                Domain::Resource | Domain::Face => {
                    return Err(format!("unsupported corpus classification: {relative}").into());
                }
            })
        })()
        .map_err(|error| format!("catalog adapter failed for {relative}: {error}"))?;
        push_bounded_document(
            &mut documents,
            &mut definition_count,
            catalog_document,
            catalog_limits,
        )?;
    }
    for document in [
        synthetic_document(Domain::Resource, "resources", resources)?,
        synthetic_document(Domain::Face, "faces", faces)?,
    ] {
        push_bounded_document(
            &mut documents,
            &mut definition_count,
            document,
            catalog_limits,
        )?;
    }

    let catalog = Catalog::build(documents, catalog_limits, SuppressionPolicy::default())?;
    if catalog.diagnostics().truncated() {
        return Err("catalog corpus diagnostics were truncated".into());
    }
    let mut codes = BTreeMap::<(&str, &str), usize>::new();
    for diagnostic in catalog.diagnostics().values() {
        *codes
            .entry((diagnostic.code, severity(diagnostic.severity)))
            .or_default() += 1;
    }
    let diagnostic_digest = diagnostic_digest(catalog.diagnostics().values());
    let mut domains = Domain::ALL
        .into_iter()
        .map(|domain| (domain.as_str(), 0_usize))
        .collect::<BTreeMap<_, _>>();
    for definition in catalog.definitions() {
        *domains.entry(definition.id.domain().as_str()).or_default() += 1;
    }
    print!(
        "{{\"schema_version\":1,\"corpus_revision\":\"{revision}\",\"generation\":\"{}\",\"documents\":{},\"definitions\":{},\"diagnostics\":{},\"diagnostic_digest\":\"{diagnostic_digest}\",\"codes\":{{",
        catalog.generation(),
        catalog.documents().count(),
        catalog.definitions().count(),
        catalog.diagnostics().values().len()
    );
    for (index, ((code, severity), count)) in codes.into_iter().enumerate() {
        if index != 0 {
            print!(",");
        }
        print!("\"{code}:{severity}\":{count}");
    }
    print!("}},\"domains\":{{");
    for (index, (domain, count)) in domains.into_iter().enumerate() {
        if index != 0 {
            print!(",");
        }
        print!("\"{domain}\":{count}");
    }
    println!("}}}}");
    Ok(())
}

fn severity(value: atrinik_diagnostics::Severity) -> &'static str {
    match value {
        atrinik_diagnostics::Severity::Info => "info",
        atrinik_diagnostics::Severity::Warning => "warning",
        atrinik_diagnostics::Severity::Error => "error",
    }
}

fn evidence(relative: &str) -> EvidenceReferences {
    EvidenceReferences {
        provenance: Some("provenance/reuse.json".to_owned()),
        license: Some(
            if relative.starts_with("arch/") {
                "content:arch/COPYING"
            } else {
                "content:maps/COPYING"
            }
            .to_owned(),
        ),
    }
}

fn definitions_from_fields(
    document: &Document,
    domain: Domain,
    keys: &[&[u8]],
    evidence: EvidenceReferences,
) -> Result<CatalogDocument, Box<dyn Error>> {
    let mut definitions = Vec::new();
    for record in document.records() {
        let RecordKind::Field { key, value } = record.kind else {
            continue;
        };
        if keys.contains(&document.bytes(key)?) {
            let local = std::str::from_utf8(document.bytes(value)?)?;
            definitions.push(
                Definition::new(
                    CatalogId::new(domain, "classic", local)?,
                    Location::new(document.source_id().as_str(), value),
                )
                .with_evidence(evidence.clone()),
            );
        }
    }
    Ok(CatalogDocument::new(
        document.source_id().clone(),
        document.revision(),
        1,
        definitions,
    ))
}

fn synthetic_document(
    domain: Domain,
    name: &str,
    values: impl IntoIterator<Item = String>,
) -> Result<CatalogDocument, Box<dyn Error>> {
    let bytes = Arc::<[u8]>::from(format!("catalog {name}\n").into_bytes());
    let document = Document::parse(
        SourceId::new(format!("catalog:{name}"))?,
        bytes,
        Limits::default(),
    )?;
    let definitions = values
        .into_iter()
        .map(|value| {
            Ok(Definition::new(
                CatalogId::new(domain, "classic", value)?,
                Location::new(document.source_id().as_str(), Span::new(0, 0)),
            ))
        })
        .collect::<Result<Vec<_>, atrinik_catalog::Error>>()?;
    Ok(CatalogDocument::new(
        document.source_id().clone(),
        document.revision(),
        1,
        definitions,
    ))
}

fn push_bounded_document(
    documents: &mut Vec<CatalogDocument>,
    definition_count: &mut usize,
    document: CatalogDocument,
    limits: CatalogLimits,
) -> Result<(), Box<dyn Error>> {
    if documents.len() >= limits.maximum_documents {
        return Err("catalog document limit exceeded".into());
    }
    *definition_count = definition_count
        .checked_add(document.definitions().len())
        .ok_or("catalog definition count overflow")?;
    if *definition_count > limits.maximum_definitions {
        return Err("catalog definition limit exceeded".into());
    }
    documents.push(document);
    Ok(())
}

fn face_id(relative: &str) -> Option<String> {
    Path::new(relative)
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|extension| extension.eq_ignore_ascii_case("png"))
        .then(|| {
            Path::new(relative)
                .file_stem()
                .and_then(|value| value.to_str())
                .map(str::to_owned)
        })
        .flatten()
}

fn diagnostic_digest(diagnostics: &[atrinik_diagnostics::Diagnostic]) -> String {
    let mut identities = diagnostics
        .iter()
        .map(|diagnostic| {
            format!(
                "{}\0{}\0{}\0{}\0{}\0{}\0{}",
                diagnostic.code,
                severity(diagnostic.severity),
                diagnostic.location.source,
                diagnostic.location.span.start,
                diagnostic.location.span.end,
                diagnostic.semantic_path.join("\0"),
                diagnostic.message
            )
        })
        .collect::<Vec<_>>();
    identities.sort();
    let mut digest = Sha256::new();
    for identity in identities {
        digest.update(identity.len().to_le_bytes());
        digest.update(identity.as_bytes());
    }
    digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn reference(
    field: &str,
    domain: Domain,
    kind: ReferenceKind,
    optional: bool,
) -> (Vec<u8>, FieldRule) {
    (
        field.as_bytes().to_vec(),
        FieldRule::Reference {
            domain,
            kind,
            optional,
        },
    )
}

fn classify(relative: &str) -> Option<Domain> {
    if relative.ends_with(".arc") {
        Some(Domain::Archetype)
    } else if relative.ends_with(".anim") {
        Some(Domain::Animation)
    } else if relative == "arch/treasures.trs" || relative == "arch/artifacts.art" {
        Some(Domain::Treasure)
    } else if relative.ends_with(".factions") {
        Some(Domain::Faction)
    } else if relative.starts_with("maps/interfaces/quests/") && relative.ends_with(".xml") {
        Some(Domain::Quest)
    } else if relative.starts_with("maps/interfaces/") && relative.ends_with(".xml") {
        Some(Domain::Interface)
    } else if relative.starts_with("maps/")
        && (Path::new(relative).extension().is_none()
            || matches!(
                Path::new(relative)
                    .extension()
                    .and_then(|value| value.to_str()),
                Some("arena" | "art" | "reg" | "trs")
            ))
    {
        Some(Domain::Map)
    } else {
        None
    }
}

fn validate_root(root: &Path) -> Result<(), Box<dyn Error>> {
    let canonical = fs::canonicalize(root)?;
    if fs::symlink_metadata(root)?.file_type().is_symlink() {
        return Err("corpus root cannot be a symlink".into());
    }
    for directory in [root.join("arch"), root.join("maps")] {
        if fs::symlink_metadata(&directory)?.file_type().is_symlink()
            || !fs::canonicalize(&directory)?.starts_with(&canonical)
        {
            return Err("corpus top-level directory escapes the root".into());
        }
    }
    Ok(())
}

fn normalize_root(root: PathBuf) -> PathBuf {
    root.components().collect()
}

fn authored_paths(root: &Path) -> Result<Vec<PathBuf>, Box<dyn Error>> {
    let mut directories = vec![root.join("arch"), root.join("maps")];
    let mut paths = Vec::new();
    let mut entries_seen = 0_usize;
    while let Some(directory) = directories.pop() {
        let mut entries = Vec::new();
        for entry in fs::read_dir(directory)? {
            entries_seen = entries_seen.checked_add(1).ok_or("entry count overflow")?;
            if entries_seen > MAXIMUM_ENTRIES {
                return Err("corpus entry limit exceeded".into());
            }
            entries.push(entry?);
        }
        entries.sort_by_key(fs::DirEntry::file_name);
        for entry in entries {
            let file_type = entry.file_type()?;
            if file_type.is_symlink() {
                return Err(
                    format!("corpus symlink is not allowed: {}", entry.path().display()).into(),
                );
            }
            if file_type.is_dir() {
                directories.push(entry.path());
            } else if file_type.is_file() {
                if paths.len() >= MAXIMUM_FILES {
                    return Err("corpus file limit exceeded".into());
                }
                paths.push(entry.path());
            }
        }
    }
    Ok(paths)
}

fn stable_path_id(path: &str) -> String {
    path.bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.' | b'/') {
                byte as char
            } else {
                '_'
            }
        })
        .collect()
}

fn normalized_relative(path: &Path) -> Result<String, Box<dyn Error>> {
    let parts = path
        .components()
        .map(|component| {
            component
                .as_os_str()
                .to_str()
                .ok_or("corpus path is not UTF-8")
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(parts.join("/"))
}

fn read_bounded(path: &Path, maximum: usize) -> Result<Vec<u8>, Box<dyn Error>> {
    if fs::symlink_metadata(path)?.file_type().is_symlink() {
        return Err("corpus file cannot be a symlink".into());
    }
    let mut file = File::open(path)?;
    let mut source = Vec::with_capacity((file.metadata()?.len() as usize).min(maximum));
    (&mut file)
        .take((maximum as u64).saturating_add(1))
        .read_to_end(&mut source)?;
    if source.len() > maximum {
        return Err(format!("file byte limit exceeded: {}", path.display()).into());
    }
    Ok(source)
}
