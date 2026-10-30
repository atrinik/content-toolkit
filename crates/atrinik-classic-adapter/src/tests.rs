// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT
use super::*;
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
static SEQUENCE: AtomicU64 = AtomicU64::new(0);
struct Root(PathBuf);
impl Root {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "classic-adapter-test-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
    fn source(&self) -> SourceRoot {
        SourceRoot::new(&self.0)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
const SOURCE:&[u8]=b"# Newly authored MIT fixture\r\narch map\nwidth 5\r\nheight 6\nunknown keep\nmsg\narch text is not structure\nend\nendmsg\nend\narch marker\nx 1\r\ny\t2\nunknown  retained\r\narch coin\nquantity 3\nend\nend\narch floor\ny 1\nend\narch repeated\nx 2\nx 2\ny 2\nend\n";
fn identity(bytes: &[u8]) -> ImportIdentity {
    ImportIdentity {
        repository: "authored/fixture".into(),
        commit: "0".repeat(40),
        path: "synthetic.map".into(),
        original_sha256: digest(bytes),
        license: "MIT".into(),
    }
}
fn setup() -> (Root, Document, ImportBundle, VerifiedBaseline) {
    let root = Root::new();
    let source = parse_original(SOURCE).unwrap();
    let bundle = import_map(&source, identity(SOURCE)).unwrap();
    let baseline = VerifiedBaseline::verify(
        &source,
        &digest(SOURCE),
        &bundle.manifest_bytes().unwrap(),
        &bundle.world_files,
        &root.source(),
    )
    .unwrap();
    (root, source, bundle, baseline)
}
fn world(root: &Root, files: &BTreeMap<String, Vec<u8>>) -> World {
    load_projection(&root.source(), files).unwrap()
}
fn change(files: &mut BTreeMap<String, Vec<u8>>, path: &str, edit: impl FnOnce(&mut Value)) {
    let mut v: Value = serde_json::from_slice(&files[path]).unwrap();
    edit(&mut v);
    files.insert(path.into(), encode(&v).unwrap());
}
const CHUNK: &str = "planes/main/layers/0/0_0.jsonc";
#[test]
fn mixed_bytes_defaults_inventory_messages_and_noop_are_lossless() {
    let (r, s, b, v) = setup();
    let w = world(&r, &b.world_files);
    assert_eq!(b.manifest.mappings.len(), 3);
    assert!(b.manifest.mappings[0].movable);
    assert!(!b.manifest.mappings[1].movable);
    assert!(!b.manifest.mappings[2].movable);
    assert!(
        b.manifest
            .unsupported
            .iter()
            .any(|s| s.contains("nested inventory"))
    );
    assert_eq!(
        preview_export(&s, &v, &w, w.source_digest(), &b.world_files)
            .unwrap()
            .bytes,
        SOURCE
    );
}
#[test]
fn coordinate_move_export_reimport_undo_redo_preserves_unrelated_bytes() {
    let (r, s, b, v) = setup();
    let mut files = b.world_files.clone();
    change(&mut files, CHUNK, |j| j["objects"][0]["at"] = json!([3, 4]));
    let w = world(&r, &files);
    let moved = preview_export(&s, &v, &w, w.source_digest(), &files).unwrap();
    assert_eq!(moved.replacements.len(), 2);
    assert_eq!(
        moved.bytes,
        String::from_utf8_lossy(SOURCE)
            .replace("x 1\r\ny\t2", "x 3\r\ny\t4")
            .as_bytes()
    );
    let undo = world(&r, &b.world_files);
    assert_eq!(
        preview_export(&s, &v, &undo, undo.source_digest(), &b.world_files)
            .unwrap()
            .bytes,
        SOURCE
    );
    assert_eq!(
        preview_export(&s, &v, &w, w.source_digest(), &files)
            .unwrap()
            .bytes,
        moved.bytes
    );
}
#[test]
fn unsupported_world_changes_are_never_silently_omitted() {
    let (r, s, b, v) = setup();
    let edits: Vec<fn(&mut Value)> = vec![
        (|j| j["objects"][0]["z"] = json!(1)),
        (|j| j["objects"][0]["sub_layer"] = json!(1)),
        (|j| j["objects"][0]["proto"] = j["objects"][1]["proto"].clone()),
        (|j| j["objects"].as_array_mut().unwrap().swap(0, 1)),
        (|j| {
            j["objects"].as_array_mut().unwrap().remove(0);
        }),
        (|j| j["objects"][1]["at"] = json!([1, 1])),
        (|j| j["objects"][2]["at"] = json!([1, 1])),
        (|j| j["objects"][0]["custom"] = json!({"fixture/hidden":true})),
        (|j| j["objects"][0]["inventory"] = json!([{"proto":j["objects"][0]["proto"].clone()}])),
        (|j| j["objects"][0]["at"] = json!([9, 9])),
    ];
    for edit in edits {
        let mut f = b.world_files.clone();
        change(&mut f, CHUNK, edit);
        if let Ok(w) = load_projection(&r.source(), &f) {
            assert!(preview_export(&s, &v, &w, w.source_digest(), &f).is_err());
        }
    }
    let mut f = b.world_files.clone();
    change(&mut f, "planes/main/plane.jsonc", |j| {
        j["title"] = json!("changed")
    });
    let w = world(&r, &f);
    assert!(preview_export(&s, &v, &w, w.source_digest(), &f).is_err());
    let mut f = b.world_files.clone();
    change(&mut f, "profile/profile.jsonc", |j| {
        j["projection"]["tile_width"] = json!(64)
    });
    let w = world(&r, &f);
    assert!(preview_export(&s, &v, &w, w.source_digest(), &f).is_err());
}
#[test]
fn corrupt_state_spans_baseline_original_and_revisions_fail_closed() {
    let (r, s, b, v) = setup();
    let w = world(&r, &b.world_files);
    assert!(preview_export(&s, &v, &w, [0; 32], &b.world_files).is_err());
    let changed = parse_original(
        &String::from_utf8_lossy(SOURCE)
            .replace("unknown keep", "unknown changed")
            .into_bytes(),
    )
    .unwrap();
    assert!(preview_export(&changed, &v, &w, w.source_digest(), &b.world_files).is_err());
    let mut manifest = b.manifest.clone();
    manifest.mappings[0].x.as_mut().unwrap().start = 0;
    assert!(
        VerifiedBaseline::verify(
            &s,
            &digest(SOURCE),
            &encode(&manifest).unwrap(),
            &b.world_files,
            &r.source()
        )
        .is_err()
    );
    let mut corrupt = b.world_files.clone();
    corrupt.get_mut("world.jsonc").unwrap().push(b' ');
    assert!(
        VerifiedBaseline::verify(
            &s,
            &digest(SOURCE),
            &b.manifest_bytes().unwrap(),
            &corrupt,
            &r.source()
        )
        .is_err()
    );
    assert!(
        VerifiedBaseline::verify(
            &s,
            &"0".repeat(64),
            &b.manifest_bytes().unwrap(),
            &b.world_files,
            &r.source()
        )
        .is_err()
    );
    let mut files = b.world_files.clone();
    files.insert("extra.jsonc".into(), b"{}".to_vec());
    assert!(preview_export(&s, &v, &w, w.source_digest(), &files).is_err());
}
#[test]
fn malformed_ambiguous_structure_and_bounds_refuse_import() {
    for text in [
        "arch map\nwidth 129\nheight 1\nend\n",
        "arch map\nwidth 1\nheight 1\nend\nend\n",
        "arch map\nwidth 1\nheight 1\nend\narch marker\n",
        "arch map\nwidth 1\nheight 1\nend\narch marker\nx 0\nx 1\nend\n",
        "arch map\nwidth 1\nwidth 1\nheight 1\nend\n",
        "arch map\nwidth 1\nheight 1\nend\narch marker\nx -1\nend\n",
        "arch map\nwidth 1\nheight 1\nmsg\nunterminated",
    ] {
        let s = parse_original(text.as_bytes()).unwrap();
        assert!(import_map(&s, identity(text.as_bytes())).is_err(), "{text}");
    }
    let text = format!(
        "arch map\nwidth 1\nheight 1\nend\n{}{}",
        "arch nested\n".repeat(17),
        "end\n".repeat(17)
    );
    let s = parse_original(text.as_bytes()).unwrap();
    assert!(import_map(&s, identity(text.as_bytes())).is_err());
    assert!(parse_original(&vec![b'a'; MAX_BYTES + 1]).is_err());
    for p in ["../map", "/map", "a//b", "a\\b", "a/./b"] {
        assert!(safe_path(p).is_err());
    }
}
#[test]
fn deterministic_fresh_authored_assets_have_exact_atx1_contract() {
    let (_, _, b, _) = setup();
    for (p, bytes) in b.supplemental_files {
        if p.ends_with(".atx1") {
            assert_eq!(&bytes[..4], b"ATX1");
            assert_eq!(bytes.len(), 12 + 48 * 32 * 4);
        }
    }
}
