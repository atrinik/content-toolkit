// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT
//! Explicit Linux prototype commands; no input overwrites or script execution.
use atrinik_classic_adapter::{
    self as adapter, ImportBundle, ImportIdentity, VerifiedBaseline,
    storage::{PrivateRoot, checked_private_root},
};
use atrinik_source::{Limits, SourceId};
use atrinik_transaction::{Control, store::read_source_file};
use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    ffi::OsString,
    path::Path,
};
use tilewright_source::Overlay;

type Result<T> = std::result::Result<T, Box<dyn Error>>;
const USAGE: &str = "classic import-map INPUT_ROOT REL_MAP WORLD_ROOT STATE_ROOT SOURCE_COMMIT EXPECTED_ORIGINAL_SHA | classic export-preview STATE_ROOT WORLD_ROOT EXPECTED_ORIGINAL_SHA EXPECTED_WORLD_SHA | classic export-apply STATE_ROOT WORLD_ROOT EXPECTED_ORIGINAL_SHA EXPECTED_WORLD_SHA OUTPUT_ROOT NEW_REL_FILE EXPECTED_OUTPUT_SHA";

pub fn run(args: &[OsString]) -> Result<()> {
    let args: Vec<&str> = args
        .iter()
        .map(|a| a.to_str().ok_or("Classic arguments must be UTF-8"))
        .collect::<std::result::Result<_, _>>()?;
    match args.as_slice() {
        [
            "import-map",
            input,
            relative,
            world,
            state,
            commit,
            original,
        ] => import(input, relative, world, state, commit, original),
        ["export-preview", state, world, original, revision] => {
            export(state, world, original, revision, None)
        }
        [
            "export-apply",
            state,
            world,
            original,
            revision,
            output,
            name,
            expected,
        ] => export(
            state,
            world,
            original,
            revision,
            Some((output, name, expected)),
        ),
        _ => Err(USAGE.into()),
    }
}
fn import(
    input: &str,
    relative: &str,
    world_path: &str,
    state_path: &str,
    commit: &str,
    original: &str,
) -> Result<()> {
    adapter::safe_path(relative)?;
    let file = read_source_file(
        Path::new(input),
        relative,
        SourceId::new("classic:external")?,
        Limits::default(),
        &Control {
            cancelled: &std::sync::atomic::AtomicBool::new(false),
            deadline: std::time::Instant::now() + std::time::Duration::from_secs(30),
        },
    )?;
    let source = adapter::parse_original(file.document.source_bytes())?;
    let bundle = adapter::import_map(
        &source,
        ImportIdentity {
            repository: "atrinik/content".into(),
            commit: commit.into(),
            path: relative.into(),
            original_sha256: original.into(),
            license: "GPL-2.0-only; external authored map, preserve its original notices".into(),
        },
    )?;
    let mut world = checked_private_root(Path::new(world_path))?;
    let mut state = checked_private_root(Path::new(state_path))?;
    world.ensure_separate(&state)?;
    // Canonical source is a read-only external tree. Neither destination may
    // overlap it; private root admission separately rejects aliases/symlinks.
    let input = std::fs::canonicalize(input)?;
    for destination in [world_path, state_path] {
        let p = Path::new(destination);
        if p.starts_with(&input) || input.starts_with(p) {
            return Err("output root overlaps external authored source".into());
        }
    }
    world.require_empty()?;
    state.require_empty()?;
    let validated = tilewright_source::load_edited_in(
        world.source_root(),
        &adapter::world_options(),
        &bundle.overlay(),
    )
    .map_err(|d| format!("generated World failed validation: {d:?}"))?;
    // Immutable state gets its commit marker last. World manifest is also
    // last: a failed initialization leaves an inert, retained partial root.
    state.publish_fresh("original.map", source.source_bytes())?;
    for (p, b) in &bundle.world_files {
        state.publish_fresh(&format!("baseline/{p}"), b)?;
    }
    state.publish_fresh("import.json", &bundle.manifest_bytes()?)?;
    for (p, b) in bundle
        .world_files
        .iter()
        .filter(|(p, _)| p.as_str() != "world.jsonc")
        .chain(bundle.supplemental_files.iter())
    {
        world.publish_fresh(p, b)?;
    }
    world.publish_fresh("world.jsonc", &bundle.world_files["world.jsonc"])?;
    let loaded = tilewright_source::load_edited_in(
        world.source_root(),
        &adapter::world_options(),
        &Overlay::new(),
    )
    .map_err(|d| format!("published World verification failed; retain roots: {d:?}"))?;
    if loaded.source_digest() != validated.source_digest() {
        return Err("published World revision mismatch; retain roots".into());
    }
    println!(
        "{}",
        serde_json::to_string_pretty(
            &serde_json::json!({"format":"atrinik.classic-import-result/1","world_revision":adapter::hex(&loaded.source_digest()),"original_sha256":original,"world_root":world_path,"adapter_state":state_path,"dimensions":bundle.manifest.dimensions,"objects":bundle.manifest.mappings.len(),"movable":bundle.manifest.mappings.iter().filter(|m|m.movable).count(),"unsupported":bundle.manifest.unsupported,"durable":true})
        )?
    );
    Ok(())
}
fn files(
    root: &PrivateRoot,
    paths: impl IntoIterator<Item = String>,
) -> Result<BTreeMap<String, Vec<u8>>> {
    let mut result = BTreeMap::new();
    let mut total = 0usize;
    for path in paths {
        let bytes = root.read(&path, 8 * 1024 * 1024)?;
        total = total
            .checked_add(bytes.len())
            .ok_or("source byte overflow")?;
        if total > 32 * 1024 * 1024 {
            return Err("source total exceeds 32 MiB".into());
        }
        result.insert(path, bytes);
    }
    Ok(result)
}
fn export(
    state_path: &str,
    world_path: &str,
    original: &str,
    revision: &str,
    destination: Option<(&str, &str, &str)>,
) -> Result<()> {
    let state = checked_private_root(Path::new(state_path))?;
    let world = checked_private_root(Path::new(world_path))?;
    state.ensure_separate(&world)?;
    let source = adapter::parse_original(&state.read("original.map", 8 * 1024 * 1024)?)?;
    let manifest = state.read("import.json", 4 * 1024 * 1024)?;
    let state_names = state.list_files()?;
    if state_names
        .iter()
        .any(|p| p != "original.map" && p != "import.json" && !p.starts_with("baseline/"))
    {
        return Err("unexpected adapter state file".into());
    }
    let baseline_files = files(
        &state,
        state_names
            .iter()
            .filter(|p| p.starts_with("baseline/"))
            .cloned(),
    )?
    .into_iter()
    .map(|(p, b)| (p[9..].to_owned(), b))
    .collect();
    // Use the retained World root and a complete regenerated overlay: no
    // baseline-root path reopening or untrusted manifest path authority.
    let baseline = VerifiedBaseline::verify(
        &source,
        original,
        &manifest,
        &baseline_files,
        world.source_root(),
    )?;
    let names = world.list_files()?;
    // Supplemental assets/catalog are immutable import material. Comparing
    // them catches tampering even though World rightly excludes them from its
    // authored-source digest.
    let identity: adapter::ImportManifest = serde_json::from_slice(&manifest)?;
    let regenerated = adapter::import_map(&source, identity.identity)?;
    verify_supplements(&world, &names, &regenerated)?;
    let documents = files(
        &world,
        names
            .into_iter()
            .filter(|p| p != "preview.jsonc" && !p.starts_with("preview-assets/")),
    )?;
    let loaded = tilewright_source::load_edited_in(
        world.source_root(),
        &adapter::world_options(),
        &Overlay::new(),
    )
    .map_err(|d| format!("current World invalid: {d:?}"))?;
    let expected = adapter::parse_digest(revision)?;
    let preview = adapter::preview_export(&source, &baseline, &loaded, expected, &documents)?;
    if let Some((output, name, expected_output)) = destination {
        adapter::safe_path(name)?;
        if expected_output != preview.output_sha256 {
            return Err("output digest differs from explicitly accepted preview".into());
        }
        let mut output = checked_private_root(Path::new(output))?;
        output.ensure_separate(&world)?;
        output.ensure_separate(&state)?;
        // Locks remain held; reload at publication use, including full file
        // bytes/catalog. No optimistic retry or stale revision rebase.
        let reread = files(&world, documents.keys().cloned())?;
        if reread != documents {
            return Err("World changed before export publication".into());
        }
        let current = tilewright_source::load_edited_in(
            world.source_root(),
            &adapter::world_options(),
            &Overlay::new(),
        )
        .map_err(|d| format!("World changed before publication: {d:?}"))?;
        if current.source_digest() != expected {
            return Err("World revision changed before publication".into());
        }
        if state.read("original.map", 8 * 1024 * 1024)? != source.source_bytes()
            || state.read("import.json", 4 * 1024 * 1024)? != manifest
        {
            return Err("adapter state changed before publication".into());
        }
        verify_supplements(&world, &world.list_files()?, &regenerated)?;
        output.publish_fresh(name, &preview.bytes)?;
        println!(
            "{}",
            serde_json::to_string_pretty(
                &serde_json::json!({"format":"atrinik.classic-export-result/1","output_sha256":preview.output_sha256,"path":name,"applied":true,"durable":true,"replacements":preview.replacements})
            )?
        );
    } else {
        println!("{}", serde_json::to_string_pretty(&preview)?);
    }
    Ok(())
}
fn verify_supplements(
    root: &PrivateRoot,
    names: &BTreeSet<String>,
    bundle: &ImportBundle,
) -> Result<()> {
    let actual: BTreeSet<_> = names
        .iter()
        .filter(|p| p.as_str() == "preview.jsonc" || p.starts_with("preview-assets/"))
        .cloned()
        .collect();
    let expected: BTreeSet<_> = bundle.supplemental_files.keys().cloned().collect();
    if actual != expected {
        return Err("preview supplemental inventory changed".into());
    }
    for (p, b) in &bundle.supplemental_files {
        if &root.read(p, 8 * 1024 * 1024)? != b {
            return Err(format!("preview supplemental bytes changed: {p}").into());
        }
    }
    Ok(())
}
