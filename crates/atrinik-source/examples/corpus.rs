// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

use std::{
    collections::BTreeSet,
    env,
    error::Error,
    fmt::Write as _,
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
    sync::Arc,
};

use atrinik_source::{Document, Limits, SourceId};
use sha2::{Digest, Sha256};

const MAXIMUM_FILES: usize = 10_000;
const MAXIMUM_CORPUS_BYTES: u64 = 512 * 1024 * 1024;
const MAXIMUM_DIAGNOSED_PATHS: usize = 256;
const EXCLUDED_PATHS: &[(&str, &str, &str)] = &[
    (
        "maps/COPYING",
        "repository license text, not authored game syntax",
        "no authored coverage effect",
    ),
    (
        "maps/Doxyfile",
        "documentation build configuration, not authored game syntax",
        "no authored coverage effect",
    ),
    (
        "maps/dev/editor/scripts/WorldMaker",
        "legacy editor script, not authored game syntax",
        "no authored coverage effect",
    ),
];

fn main() {
    if let Err(error) = run() {
        eprintln!("content corpus: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let mut arguments = env::args_os().skip(1);
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--root")) {
        return Err("usage: corpus --root PATH --revision COMMIT".into());
    }
    let root = PathBuf::from(arguments.next().ok_or("missing corpus root")?);
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

    let mut paths = authored_paths(&root)?;
    paths.sort();
    if paths.len() > MAXIMUM_FILES {
        return Err("corpus file limit exceeded".into());
    }

    let limits = Limits::default();
    let mut bytes = 0_u64;
    let mut clean = 0_usize;
    let mut diagnosed = 0_usize;
    let mut truncated = 0_usize;
    let mut diagnosed_paths = Vec::new();
    let mut digest = Sha256::new();
    for path in &paths {
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.file_type().is_file() {
            return Err(format!("corpus entry is not a regular file: {}", path.display()).into());
        }
        bytes = bytes
            .checked_add(metadata.len())
            .ok_or("corpus byte count overflow")?;
        if bytes > MAXIMUM_CORPUS_BYTES {
            return Err("corpus byte limit exceeded".into());
        }
        let relative = path.strip_prefix(&root)?;
        let identity = normalized_relative(relative)?;
        let source = read_bounded(path, limits.maximum_file_bytes)?;
        let document = Document::parse(
            SourceId::new(format!("content:{identity}"))?,
            Arc::<[u8]>::from(source.clone()),
            limits,
        )?;
        if document.source_bytes() != source {
            return Err(format!("round-trip drift for {identity}").into());
        }
        if document.diagnostics().values().is_empty() {
            clean += 1;
        } else {
            diagnosed += 1;
            if diagnosed_paths.len() >= MAXIMUM_DIAGNOSED_PATHS {
                return Err("diagnosed path report limit exceeded".into());
            }
            let codes: BTreeSet<_> = document
                .diagnostics()
                .values()
                .iter()
                .map(|diagnostic| diagnostic.code)
                .collect();
            diagnosed_paths.push((identity.clone(), codes, document.diagnostics().truncated()));
        }
        if document.diagnostics().truncated() {
            truncated += 1;
        }
        digest.update((identity.len() as u64).to_le_bytes());
        digest.update(identity.as_bytes());
        digest.update(document.revision().bytes());
    }
    let digest = format!("{:x}", digest.finalize());
    let mut report = format!(
        "{{\"schema_version\":1,\"corpus_revision\":\"{revision}\",\"files\":{},\"bytes\":{bytes},\"clean\":{clean},\"diagnosed\":{diagnosed},\"diagnostics_truncated\":{truncated},\"digest\":\"{digest}\",\"diagnosed_paths\":[",
        paths.len()
    );
    for (index, (path, codes, path_truncated)) in diagnosed_paths.iter().enumerate() {
        if index != 0 {
            report.push(',');
        }
        write!(report, "{{\"path\":\"{}\",\"codes\":[", json_escape(path))?;
        for (code_index, code) in codes.iter().enumerate() {
            if code_index != 0 {
                report.push(',');
            }
            write!(report, "\"{code}\"")?;
        }
        write!(report, "],\"truncated\":{path_truncated}}}")?;
    }
    report.push_str("],\"excluded_paths\":[");
    for (index, (path, reason, effect)) in EXCLUDED_PATHS.iter().enumerate() {
        if index != 0 {
            report.push(',');
        }
        write!(
            report,
            "{{\"path\":\"{}\",\"reason\":\"{}\",\"owner\":\"atrinik/content-toolkit#3\",\"milestone\":\"M1 - Clean-room foundations\",\"effect\":\"{}\"}}",
            json_escape(path),
            json_escape(reason),
            json_escape(effect)
        )?;
    }
    report.push_str("]}");
    println!("{report}");
    Ok(())
}

fn json_escape(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\u{08}' => escaped.push_str("\\b"),
            '\u{0c}' => escaped.push_str("\\f"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            character if character.is_control() => {
                const HEX: &[u8; 16] = b"0123456789abcdef";
                let value = character as usize;
                escaped.push_str("\\u");
                escaped.push(HEX[(value >> 12) & 0x0f] as char);
                escaped.push(HEX[(value >> 8) & 0x0f] as char);
                escaped.push(HEX[(value >> 4) & 0x0f] as char);
                escaped.push(HEX[value & 0x0f] as char);
            }
            character => escaped.push(character),
        }
    }
    escaped
}

fn authored_paths(root: &Path) -> Result<Vec<PathBuf>, Box<dyn Error>> {
    let mut directories = vec![root.join("arch"), root.join("maps")];
    let mut paths = Vec::new();
    while let Some(directory) = directories.pop() {
        let mut entries: Vec<_> = fs::read_dir(&directory)?.collect::<Result<_, _>>()?;
        entries.sort_by_key(std::fs::DirEntry::file_name);
        for entry in entries {
            let file_type = entry.file_type()?;
            if file_type.is_symlink() {
                return Err(
                    format!("corpus symlink is not allowed: {}", entry.path().display()).into(),
                );
            }
            if file_type.is_dir() {
                directories.push(entry.path());
            } else if file_type.is_file() && is_authored(&entry.path(), root) {
                paths.push(entry.path());
            }
        }
    }
    Ok(paths)
}

fn is_authored(path: &Path, root: &Path) -> bool {
    let relative = match path.strip_prefix(root) {
        Ok(relative) => relative,
        Err(_) => return false,
    };
    let under_arch = relative.starts_with("arch");
    if EXCLUDED_PATHS
        .iter()
        .any(|(excluded, _, _)| relative == Path::new(excluded))
    {
        return false;
    }
    let extension = path.extension().and_then(|value| value.to_str());
    if under_arch {
        return matches!(extension, Some("arc" | "anim" | "trs"));
    }
    extension.is_none()
        || matches!(
            extension,
            Some("arena" | "art" | "factions" | "reg" | "trs")
        )
}

fn normalized_relative(path: &Path) -> Result<String, Box<dyn Error>> {
    let mut output = String::new();
    for component in path.components() {
        let component = component
            .as_os_str()
            .to_str()
            .ok_or("corpus path is not UTF-8")?;
        if !output.is_empty() {
            output.push('/');
        }
        output.push_str(component);
    }
    Ok(output)
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
