// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::{
    env,
    error::Error,
    ffi::OsString,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};

use atrinik_source::{Document, Limits, SourceId};

fn main() {
    if let Err(error) = run(env::args_os().skip(1).collect()) {
        eprintln!("atrinik-content: {error}");
        std::process::exit(1);
    }
}

fn run(arguments: Vec<std::ffi::OsString>) -> Result<(), Box<dyn Error>> {
    if arguments.len() == 1 && arguments[0] == "--version" {
        println!("atrinik-content {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let Some(command) = arguments.first().and_then(|value| value.to_str()) else {
        return Err(usage().into());
    };
    let options = parse_options(&arguments[1..])?;
    let input = required_path(&options, "--input")?;
    let source_id = required_string(&options, "--source-id")?;
    let limits = Limits::default();
    let bytes = read_bounded(&input, limits.maximum_file_bytes)?;
    let document = Document::parse(SourceId::new(source_id)?, Arc::<[u8]>::from(bytes), limits)?;

    match command {
        "validate" => {
            ensure_options(&options, &["--input", "--source-id"])?;
            for diagnostic in document.diagnostics().values() {
                eprintln!("{diagnostic}");
            }
            if document.diagnostics().truncated() {
                return Err("diagnostic limit reached".into());
            }
            if document.diagnostics().has_errors() {
                return Err("document validation failed".into());
            }
            println!(
                "valid source={} revision={} records={}",
                document.source_id().as_str(),
                document.revision(),
                document.records().len()
            );
            Ok(())
        }
        "round-trip" => {
            ensure_options(&options, &["--input", "--output", "--source-id"])?;
            if document.diagnostics().has_errors() {
                return Err("refusing to write a document with parse diagnostics".into());
            }
            let output = required_path(&options, "--output")?;
            write_new(&output, document.source_bytes())?;
            println!(
                "wrote {} bytes to {}",
                document.source_bytes().len(),
                output.display()
            );
            Ok(())
        }
        _ => Err(usage().into()),
    }
}

fn parse_options(
    arguments: &[std::ffi::OsString],
) -> Result<Vec<(String, std::ffi::OsString)>, Box<dyn Error>> {
    if !arguments.len().is_multiple_of(2) {
        return Err(usage().into());
    }
    let mut options = Vec::with_capacity(arguments.len() / 2);
    for pair in arguments.chunks_exact(2) {
        let name = pair[0].to_str().ok_or("option name is not UTF-8")?;
        if !matches!(name, "--input" | "--output" | "--source-id")
            || options.iter().any(|(existing, _)| existing == name)
        {
            return Err(usage().into());
        }
        options.push((name.to_owned(), pair[1].clone()));
    }
    Ok(options)
}

fn required_path(
    options: &[(String, std::ffi::OsString)],
    name: &str,
) -> Result<PathBuf, Box<dyn Error>> {
    options
        .iter()
        .find(|(candidate, _)| candidate == name)
        .map(|(_, value)| PathBuf::from(value))
        .ok_or_else(|| format!("missing required {name}").into())
}

fn required_string(
    options: &[(String, std::ffi::OsString)],
    name: &str,
) -> Result<String, Box<dyn Error>> {
    options
        .iter()
        .find(|(candidate, _)| candidate == name)
        .and_then(|(_, value)| value.to_str())
        .map(str::to_owned)
        .ok_or_else(|| format!("missing or non-UTF-8 {name}").into())
}

fn ensure_options(options: &[(String, OsString)], expected: &[&str]) -> Result<(), Box<dyn Error>> {
    if options.len() != expected.len()
        || expected
            .iter()
            .any(|name| !options.iter().any(|(candidate, _)| candidate == name))
    {
        return Err(usage().into());
    }
    Ok(())
}

fn read_bounded(path: &Path, maximum: usize) -> Result<Vec<u8>, Box<dyn Error>> {
    let mut file = File::open(path)?;
    if file.metadata()?.len() > maximum as u64 {
        return Err("input exceeds the maximum file size".into());
    }
    let mut bytes = Vec::with_capacity(file.metadata()?.len() as usize);
    (&mut file)
        .take((maximum as u64).saturating_add(1))
        .read_to_end(&mut bytes)?;
    if bytes.len() > maximum {
        return Err("input exceeds the maximum file size".into());
    }
    Ok(bytes)
}

fn write_new(path: &Path, bytes: &[u8]) -> Result<(), Box<dyn Error>> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let file_name = path.file_name().ok_or("output path has no file name")?;
    let mut temporary = None;
    for _ in 0..32 {
        let sequence = OUTPUT_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let mut temporary_name = OsString::from(".");
        temporary_name.push(file_name);
        temporary_name.push(format!(".atrinik-{}-{sequence}.tmp", std::process::id()));
        let temporary_path = parent.join(temporary_name);
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary_path)
        {
            Ok(file) => {
                temporary = Some((RemoveOnDrop(temporary_path), file));
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error.into()),
        }
    }
    let (temporary_path, mut output) =
        temporary.ok_or("could not allocate a temporary output name")?;
    output.write_all(bytes)?;
    output.sync_all()?;
    drop(output);
    fs::hard_link(&temporary_path.0, path)?;
    fs::remove_file(&temporary_path.0)?;
    drop(temporary_path);
    #[cfg(unix)]
    File::open(parent)?.sync_all()?;
    Ok(())
}

struct RemoveOnDrop(PathBuf);

impl Drop for RemoveOnDrop {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

static OUTPUT_SEQUENCE: AtomicU64 = AtomicU64::new(0);

const fn usage() -> &'static str {
    "usage: atrinik-content --version | validate --input PATH --source-id ID | round-trip --input PATH --output NEW_PATH --source-id ID"
}
