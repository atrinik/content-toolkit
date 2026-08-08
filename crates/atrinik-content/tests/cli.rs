// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

use std::{
    fs,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

#[test]
fn validates_and_round_trips_the_public_fixture() {
    let binary = env!("CARGO_BIN_EXE_atrinik-content");
    let input = format!(
        "{}/../../fixtures/corpus/minimal.arc",
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
