// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

use std::{
    fs,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

#[cfg(target_os = "linux")]
use std::os::unix::fs::PermissionsExt;
#[cfg(target_os = "linux")]
use std::sync::Arc;

#[cfg(target_os = "linux")]
use atrinik_source::{Document, Limits, SourceId};

#[test]
fn validates_and_round_trips_the_public_fixture() {
    let binary = env!("CARGO_BIN_EXE_atrinik-content");
    let input = format!(
        "{}/../atrinik-testkit/fixtures/minimal.arc",
        env!("CARGO_MANIFEST_DIR")
    );
    let output = std::env::temp_dir().join(format!(
        "atrinik-content-round-trip-{}-{}.arc",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));

    let validation = Command::new(binary)
        .args([
            "validate",
            "--input",
            &input,
            "--source-id",
            "fixture:minimal",
        ])
        .output()
        .unwrap();
    assert!(
        validation.status.success(),
        "{}",
        String::from_utf8_lossy(&validation.stderr)
    );

    let round_trip = Command::new(binary)
        .args([
            "round-trip",
            "--input",
            &input,
            "--output",
            output.to_str().unwrap(),
            "--source-id",
            "fixture:minimal",
        ])
        .output()
        .unwrap();
    assert!(
        round_trip.status.success(),
        "{}",
        String::from_utf8_lossy(&round_trip.stderr)
    );
    assert_eq!(fs::read(&input).unwrap(), fs::read(&output).unwrap());

    let second = Command::new(binary)
        .args([
            "round-trip",
            "--input",
            &input,
            "--output",
            output.to_str().unwrap(),
            "--source-id",
            "fixture:minimal",
        ])
        .output()
        .unwrap();
    assert!(!second.status.success());
    assert_eq!(fs::read(&input).unwrap(), fs::read(&output).unwrap());
    fs::remove_file(output).unwrap();
}

#[test]
fn prints_the_pinned_package_version() {
    let output = Command::new(env!("CARGO_BIN_EXE_atrinik-content"))
        .arg("--version")
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "atrinik-content 0.1.0\n"
    );
}

#[cfg(target_os = "linux")]
#[test]
fn project_transactions_preview_by_default_and_require_explicit_apply() {
    let binary = env!("CARGO_BIN_EXE_atrinik-content");
    let temporary = std::env::temp_dir().join(format!(
        "atrinik-content-transaction-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let input_root = temporary.join("input");
    let store_root = temporary.join("store");
    fs::create_dir_all(&input_root).unwrap();
    fs::create_dir(&store_root).unwrap();
    fs::set_permissions(&store_root, fs::Permissions::from_mode(0o700)).unwrap();

    let source = b"Object alpha\nname old\nend\n";
    let input = input_root.join("alpha.arc");
    fs::write(&input, source).unwrap();
    fs::set_permissions(&input, fs::Permissions::from_mode(0o640)).unwrap();
    let referenced_source = b"Object beta\nname second\nref alpha\nend\n";
    let referenced_input = input_root.join("beta.arc");
    fs::write(&referenced_input, referenced_source).unwrap();
    fs::set_permissions(&referenced_input, fs::Permissions::from_mode(0o640)).unwrap();
    let policy = temporary.join("policy.json");
    fs::write(&policy, policy_json()).unwrap();
    let manifest = temporary.join("manifest.json");
    fs::write(
        &manifest,
        r#"{
          "version": 1,
          "identity": {
            "repository": "atrinik/content",
            "reference": "refs/heads/main",
            "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "schema_version": 1
          },
          "files": [
            {
              "path": "arch/alpha.arc",
              "input": "alpha.arc",
              "source_id": "fixture:alpha",
              "mode": 416
            },
            {
              "path": "arch/beta.arc",
              "input": "beta.arc",
              "source_id": "fixture:beta",
              "mode": 416
            }
          ]
        }"#,
    )
    .unwrap();

    let initialized = Command::new(binary)
        .args([
            "transaction",
            "initialize",
            "--root",
            store_root.to_str().unwrap(),
            "--policy",
            policy.to_str().unwrap(),
            "--manifest",
            manifest.to_str().unwrap(),
            "--input-root",
            input_root.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        initialized.status.success(),
        "{}",
        String::from_utf8_lossy(&initialized.stderr)
    );
    let initialized_json: serde_json::Value = serde_json::from_slice(&initialized.stdout).unwrap();
    assert_eq!(initialized_json["status"], "initialized");
    let project_revision = initialized_json["project_revision"].as_str().unwrap();
    let source_revision = Document::parse(
        SourceId::new("fixture:alpha").unwrap(),
        Arc::<[u8]>::from(&source[..]),
        Limits::default(),
    )
    .unwrap()
    .revision()
    .to_string();
    let referenced_revision = Document::parse(
        SourceId::new("fixture:beta").unwrap(),
        Arc::<[u8]>::from(&referenced_source[..]),
        Limits::default(),
    )
    .unwrap()
    .revision()
    .to_string();
    let plan = temporary.join("plan.json");
    fs::write(
        &plan,
        serde_json::to_vec(&serde_json::json!({
            "version": 1,
            "expected_project_revision": project_revision,
            "commands": [{
                "path": "arch/alpha.arc",
                "expected_source_revision": source_revision,
                "record": 1,
                "expected_span": {"start": 18, "end": 21},
                "semantic_intent": "use the synthetic display name",
                "replacement": [110, 101, 119]
            }]
        }))
        .unwrap(),
    )
    .unwrap();

    let invalid_plan = temporary.join("invalid-plan.json");
    fs::write(
        &invalid_plan,
        serde_json::to_vec(&serde_json::json!({
            "version": 1,
            "expected_project_revision": project_revision,
            "commands": [{
                "path": "arch/beta.arc",
                "expected_source_revision": referenced_revision,
                "record": 2,
                "expected_span": {"start": 28, "end": 33},
                "semantic_intent": "exercise missing-reference diagnostics",
                "replacement": [97, 98, 115, 101, 110, 116]
            }]
        }))
        .unwrap(),
    )
    .unwrap();

    let invalid = Command::new(binary)
        .args([
            "transaction",
            "preview",
            "--root",
            store_root.to_str().unwrap(),
            "--policy",
            policy.to_str().unwrap(),
            "--plan",
            invalid_plan.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(!invalid.status.success());
    let invalid_json: serde_json::Value = serde_json::from_slice(&invalid.stdout).unwrap();
    assert_eq!(invalid_json["status"], "invalid");
    assert_eq!(invalid_json["dry_run"], true);
    assert!(invalid_json["diagnostics"]
        .as_array()
        .unwrap()
        .iter()
        .any(|diagnostic| diagnostic["code"] == "catalog.missing_reference"));

    let previewed = Command::new(binary)
        .args([
            "transaction",
            "--root",
            store_root.to_str().unwrap(),
            "--policy",
            policy.to_str().unwrap(),
            "--plan",
            plan.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        previewed.status.success(),
        "{}",
        String::from_utf8_lossy(&previewed.stderr)
    );
    let preview_json: serde_json::Value = serde_json::from_slice(&previewed.stdout).unwrap();
    assert_eq!(preview_json["status"], "valid");
    assert_eq!(preview_json["dry_run"], true);
    assert_eq!(preview_json["changes"][0]["span"]["start"], 18);

    let applied = Command::new(binary)
        .args([
            "transaction",
            "apply",
            "--root",
            store_root.to_str().unwrap(),
            "--policy",
            policy.to_str().unwrap(),
            "--plan",
            plan.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        applied.status.success(),
        "{}",
        String::from_utf8_lossy(&applied.stderr)
    );
    let applied_json: serde_json::Value = serde_json::from_slice(&applied.stdout).unwrap();
    assert_eq!(applied_json["status"], "published");
    assert_eq!(applied_json["dry_run"], false);
    assert_eq!(applied_json["publication"]["durable"], true);

    let stale = Command::new(binary)
        .args([
            "transaction",
            "preview",
            "--root",
            store_root.to_str().unwrap(),
            "--policy",
            policy.to_str().unwrap(),
            "--plan",
            plan.to_str().unwrap(),
        ])
        .output()
    .unwrap();
    assert!(!stale.status.success());
    let stale_json: serde_json::Value = serde_json::from_slice(&stale.stdout).unwrap();
    assert_eq!(stale_json["status"], "error");
    fs::remove_dir_all(temporary).unwrap();
}

#[cfg(target_os = "linux")]
fn policy_json() -> &'static str {
    r#"{
      "version": 1,
      "maximum_execution_millis": 30000,
      "source_limits": {
        "maximum_file_bytes": 1048576,
        "maximum_line_bytes": 65536,
        "maximum_records": 1000,
        "maximum_tokens": 4000,
        "maximum_value_bytes": 65536,
        "maximum_edits": 100,
        "maximum_nesting": 16,
        "maximum_diagnostics": 64
      },
      "project_limits": {
        "maximum_files": 10,
        "maximum_bytes": 1048576,
        "maximum_commands": 100,
        "maximum_diff_bytes": 1048576,
        "maximum_diagnostics": 64
      },
      "catalog_limits": {
        "maximum_documents": 10,
        "maximum_definitions_per_document": 100,
        "maximum_definitions": 100,
        "maximum_aliases_per_definition": 8,
        "maximum_references_per_definition": 32,
        "maximum_preview_values": 16,
        "maximum_string_bytes": 256,
        "maximum_semantic_depth": 8,
        "maximum_graph_work": 1000,
        "maximum_invalidation": 100,
        "maximum_query_terms": 16,
        "maximum_query_work": 1000,
        "diagnostic_limits": {
          "maximum_diagnostics": 64,
          "maximum_related": 8,
          "maximum_semantic_depth": 8,
          "maximum_text_bytes": 1024
        }
      },
      "files": {
        "arch/alpha.arc": {
          "schema": {"name": "object", "required_fields": ["name"]},
          "loader": {
            "domain": "archetype",
            "namespace": "fixture",
            "schema_version": 1,
            "rules": {
              "name": {"kind": "label"},
              "ref": {
                "kind": "reference",
                "domain": "archetype",
                "reference_kind": "archetype",
                "optional": false
              }
            }
          },
          "shape": {"kind": "objects"}
        },
        "arch/beta.arc": {
          "schema": {"name": "object", "required_fields": ["name"]},
          "loader": {
            "domain": "archetype",
            "namespace": "fixture",
            "schema_version": 1,
            "rules": {
              "name": {"kind": "label"},
              "ref": {
                "kind": "reference",
                "domain": "archetype",
                "reference_kind": "archetype",
                "optional": false
              }
            }
          },
          "shape": {"kind": "objects"}
        }
      }
    }"#
}
