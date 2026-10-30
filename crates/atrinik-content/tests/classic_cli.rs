// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT
#![cfg(target_os = "linux")]
use atrinik_classic_adapter::{digest, hex, world_options};
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::PathBuf,
    process::{Command, Output},
    sync::atomic::{AtomicU64, Ordering},
};
static SEQUENCE: AtomicU64 = AtomicU64::new(0);
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "classic-cli-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        for name in ["input", "world", "state", "output", "moved"] {
            let p = root.join(name);
            fs::create_dir(&p).unwrap();
            fs::set_permissions(&p, fs::Permissions::from_mode(0o700)).unwrap();
        }
        Self(root)
    }
    fn path(&self, name: &str) -> String {
        self.0.join(name).to_str().unwrap().into()
    }
    fn run(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_atrinik-content"))
            .arg("classic")
            .args(args)
            .output()
            .unwrap()
    }
    fn ok(&self, args: &[&str]) -> serde_json::Value {
        let o = self.run(args);
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
        serde_json::from_slice(&o.stdout).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
const MAP:&[u8]=b"# Newly authored MIT fixture\r\narch map\nwidth 5\nheight 5\nend\narch marker\nx 1\r\ny 2\nunknown keep\nmsg\nopaque message\nendmsg\nend\n";
#[test]
fn cli_import_preview_explicit_new_export_move_and_refusals() {
    let f = Fixture::new();
    fs::write(f.0.join("input/map"), MAP).unwrap();
    let original = digest(MAP);
    let commit = "0".repeat(40);
    let imported = f.ok(&[
        "import-map",
        &f.path("input"),
        "map",
        &f.path("world"),
        &f.path("state"),
        &commit,
        &original,
    ]);
    let revision = imported["world_revision"].as_str().unwrap();
    let preview = f.ok(&[
        "export-preview",
        &f.path("state"),
        &f.path("world"),
        &original,
        revision,
    ]);
    assert_eq!(preview["output_sha256"], original);
    assert_eq!(preview["replacements"].as_array().unwrap().len(), 0);
    assert!(fs::read_dir(f.0.join("output")).unwrap().next().is_none());
    f.ok(&[
        "export-apply",
        &f.path("state"),
        &f.path("world"),
        &original,
        revision,
        &f.path("output"),
        "map",
        &original,
    ]);
    assert_eq!(fs::read(f.0.join("output/map")).unwrap(), MAP);
    assert!(
        !f.run(&[
            "export-apply",
            &f.path("state"),
            &f.path("world"),
            &original,
            revision,
            &f.path("output"),
            "map",
            &original
        ])
        .status
        .success()
    );
    assert!(
        !f.run(&[
            "export-apply",
            &f.path("state"),
            &f.path("world"),
            &original,
            revision,
            &f.path("moved"),
            "../escape",
            &original
        ])
        .status
        .success()
    );
    let chunk = f.0.join("world/planes/main/layers/0/0_0.jsonc");
    let mut value: serde_json::Value = serde_json::from_slice(&fs::read(&chunk).unwrap()).unwrap();
    value["objects"][0]["at"] = serde_json::json!([3, 4]);
    fs::write(&chunk, serde_json::to_vec_pretty(&value).unwrap()).unwrap();
    assert!(
        !f.run(&[
            "export-preview",
            &f.path("state"),
            &f.path("world"),
            &original,
            revision
        ])
        .status
        .success()
    );
    let revision = hex(
        &tilewright_source::load_with(&f.0.join("world"), &world_options())
            .unwrap()
            .source_digest(),
    );
    let moved = f.ok(&[
        "export-preview",
        &f.path("state"),
        &f.path("world"),
        &original,
        &revision,
    ]);
    let moved_sha = moved["output_sha256"].as_str().unwrap();
    f.ok(&[
        "export-apply",
        &f.path("state"),
        &f.path("world"),
        &original,
        &revision,
        &f.path("moved"),
        "map",
        moved_sha,
    ]);
    assert_eq!(
        fs::read(f.0.join("moved/map")).unwrap(),
        String::from_utf8_lossy(MAP)
            .replace("x 1\r\ny 2", "x 3\r\ny 4")
            .as_bytes()
    );
    assert_eq!(fs::read(f.0.join("input/map")).unwrap(), MAP);
    value["objects"][0]["z"] = serde_json::json!(7);
    fs::write(&chunk, serde_json::to_vec_pretty(&value).unwrap()).unwrap();
    let bad_revision = hex(
        &tilewright_source::load_with(&f.0.join("world"), &world_options())
            .unwrap()
            .source_digest(),
    );
    assert!(
        !f.run(&[
            "export-preview",
            &f.path("state"),
            &f.path("world"),
            &original,
            &bad_revision
        ])
        .status
        .success()
    );
}
#[test]
fn cli_refuses_source_links_and_tampered_manifest() {
    let f = Fixture::new();
    fs::write(f.0.join("input/map"), MAP).unwrap();
    symlink("map", f.0.join("input/link")).unwrap();
    let original = digest(MAP);
    let commit = "0".repeat(40);
    assert!(
        !f.run(&[
            "import-map",
            &f.path("input"),
            "link",
            &f.path("world"),
            &f.path("state"),
            &commit,
            &original
        ])
        .status
        .success()
    );
    let imported = f.ok(&[
        "import-map",
        &f.path("input"),
        "map",
        &f.path("world"),
        &f.path("state"),
        &commit,
        &original,
    ]);
    let revision = imported["world_revision"].as_str().unwrap();
    let p = f.0.join("state/import.json");
    let mut manifest: serde_json::Value = serde_json::from_slice(&fs::read(&p).unwrap()).unwrap();
    manifest["mappings"][0]["x"]["start"] = serde_json::json!(0);
    fs::write(p, serde_json::to_vec(&manifest).unwrap()).unwrap();
    assert!(
        !f.run(&[
            "export-preview",
            &f.path("state"),
            &f.path("world"),
            &original,
            revision
        ])
        .status
        .success()
    );
    assert_eq!(fs::read(f.0.join("input/map")).unwrap(), MAP);
}
