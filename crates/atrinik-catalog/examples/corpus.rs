// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

use std::{
    collections::BTreeMap,
    env,
    error::Error,
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
    sync::Arc,
};

use atrinik_catalog::{
    Catalog, CatalogLimits, Domain, EvidenceReferences, FieldRule, LineDocumentLoader,
    ReferenceKind,
};
use atrinik_diagnostics::{DiagnosticLimits, SuppressionPolicy};
use atrinik_source::{Document, Limits, SourceId};

const MAXIMUM_FILES: usize = 10_000;
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
        return Err("usage: corpus --root PATH".into());
    }
    let root = PathBuf::from(arguments.next().ok_or("missing corpus root")?);
    if arguments.next().is_some() {
        return Err("unexpected trailing argument".into());
    }

    let archetypes = LineDocumentLoader::new(
        Domain::Archetype,
        "classic",
        1,
        [
            reference(
                "other_arch",
                Domain::Archetype,
                ReferenceKind::Generic,
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
            ReferenceKind::Generic,
            false,
        )],
    )?;
    let mut documents = Vec::new();
    let mut paths = authored_paths(&root)?;
    paths.sort();
    for path in paths {
        let relative = normalized_relative(path.strip_prefix(&root)?)?;
        let source = Arc::<[u8]>::from(read_bounded(&path, Limits::default().maximum_file_bytes)?);
        let document = Document::parse(
            SourceId::new(format!("content:{relative}"))?,
            source,
            Limits::default(),
        )?;
        let evidence = EvidenceReferences {
            provenance: Some("provenance/reuse.json".to_owned()),
            license: Some(
                if relative.starts_with("arch/") {
                    "content:arch/COPYING"
                } else {
                    "content:maps/COPYING"
                }
                .to_owned(),
            ),
        };
        let catalog_document = if path.extension().and_then(|value| value.to_str()) == Some("arc") {
            archetypes.load_objects(&document, evidence)?
        } else {
            maps.load_single(&document, stable_path_id(&relative), evidence)?
        };
        documents.push(catalog_document);
    }
    let catalog = Catalog::build(
        documents,
        CatalogLimits {
            diagnostic_limits: DiagnosticLimits {
                maximum_diagnostics: MAXIMUM_DIAGNOSTICS,
                ..DiagnosticLimits::default()
            },
            ..CatalogLimits::default()
        },
        SuppressionPolicy::default(),
    )?;
    if catalog.diagnostics().truncated() {
        return Err("catalog corpus diagnostics were truncated".into());
    }
    let mut codes = BTreeMap::<&str, usize>::new();
    for diagnostic in catalog.diagnostics().values() {
        *codes.entry(diagnostic.code).or_default() += 1;
    }
    print!(
        "{{\"schema_version\":1,\"documents\":{},\"definitions\":{},\"diagnostics\":{},\"codes\":{{",
        catalog.documents().count(),
        catalog.definitions().count(),
        catalog.diagnostics().values().len()
    );
    for (index, (code, count)) in codes.into_iter().enumerate() {
        if index != 0 {
            print!(",");
        }
        print!("\"{code}\":{count}");
    }
    println!("}}}}");
    Ok(())
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

fn authored_paths(root: &Path) -> Result<Vec<PathBuf>, Box<dyn Error>> {
    let mut directories = vec![root.join("arch"), root.join("maps")];
    let mut paths = Vec::new();
    let mut entries_seen = 0_usize;
    while let Some(directory) = directories.pop() {
        let mut entries = fs::read_dir(directory)?.collect::<Result<Vec<_>, _>>()?;
        entries.sort_by_key(fs::DirEntry::file_name);
        for entry in entries {
            entries_seen = entries_seen.checked_add(1).ok_or("entry count overflow")?;
            if entries_seen > MAXIMUM_ENTRIES {
                return Err("corpus entry limit exceeded".into());
            }
            let file_type = entry.file_type()?;
            if file_type.is_symlink() {
                return Err(
                    format!("corpus symlink is not allowed: {}", entry.path().display()).into(),
                );
            }
            if file_type.is_dir() {
                directories.push(entry.path());
            } else if file_type.is_file() && is_supported(&entry.path(), root) {
                if paths.len() >= MAXIMUM_FILES {
                    return Err("corpus file limit exceeded".into());
                }
                paths.push(entry.path());
            }
        }
    }
    Ok(paths)
}

fn is_supported(path: &Path, root: &Path) -> bool {
    let Ok(relative) = path.strip_prefix(root) else {
        return false;
    };
    if relative.starts_with("arch") {
        return path.extension().and_then(|value| value.to_str()) == Some("arc");
    }
    path.extension().is_none()
        || matches!(
            path.extension().and_then(|value| value.to_str()),
            Some("arena" | "art" | "factions" | "reg" | "trs")
        )
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
