// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

//! Bounded Linux publication into an already provisioned private root.
//!
//! This composes the released World's rooted primitives. The root must be
//! provisioned by the caller on persistent local ext4/XFS/Btrfs/ZFS, outside
//! authored input, before opening. Volatile/overlay/unknown mounts refuse
//! durability even if fsync succeeds. A retained read-only FD binds bounded
//! Linux mount evidence at opening and publication. Advisory
//! locks exclude cooperating writers; hostile processes with the same user ID
//! are outside this prototype's isolation guarantee. Opening-path identity
//! checks are deployment evidence, not a race-free child-root constructor.
//! No filesystem writes reopen an absolute path. A failed publication retains
//! any surviving stage/final and blocks further publication on this handle.

use std::{
    collections::{BTreeMap, BTreeSet},
    fmt,
    fs::File,
    io::Read as _,
    os::fd::AsRawFd as _,
    path::{Path, PathBuf},
};
use tilewright_source::{Access, AccessError, Mounts, SourceDirectory, SourceLock, SourceRoot};

/// Maximum files or directory entries inspected by this prototype.
pub const MAX_ENTRIES: usize = 4096;
/// Maximum individual file size.
pub const MAX_FILE_BYTES: u64 = 8 * 1024 * 1024;
/// Maximum bytes read in an exact tree check, or published by one handle.
pub const MAX_TOTAL_BYTES: u64 = 64 * 1024 * 1024;
const MAX_PATH_BYTES: usize = 1024;
const MAX_DEPTH: usize = 16;
const STAGE_SUFFIX: &str = ".adapter-stage";

/// What is known about the final name after a publication attempt.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Applied {
    No,
    Yes,
    Unknown,
}

/// Failure details deliberately retain both names for inspection.
#[derive(Debug)]
pub struct StorageError {
    pub message: String,
    pub stage: Option<String>,
    pub final_path: Option<String>,
    pub applied: Applied,
    pub durable: bool,
}

impl StorageError {
    fn refused(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            stage: None,
            final_path: None,
            applied: Applied::No,
            durable: false,
        }
    }
}

impl fmt::Display for StorageError {
    fn fmt(&self, out: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(out, "{}", self.message)?;
        if let (Some(stage), Some(final_path)) = (&self.stage, &self.final_path) {
            write!(
                out,
                "; stage={stage}, final={final_path}, applied={:?}, durable={}; surviving files retained",
                self.applied, self.durable
            )?;
        }
        Ok(())
    }
}

impl std::error::Error for StorageError {}

fn failure(error: impl fmt::Display) -> StorageError {
    StorageError::refused(error.to_string())
}

/// Exact bytes have been reread after a durable rename.
#[derive(Debug, PartialEq, Eq)]
pub struct PublishReceipt {
    pub path: String,
    pub bytes: u64,
    pub durable: bool,
}

/// Authority retaining the root and its exclusive lock for its whole lifetime.
pub struct PrivateRoot {
    root: SourceRoot,
    directory: SourceDirectory,
    _lock: SourceLock,
    access: Access,
    canonical: PathBuf,
    identity: (u64, u64),
    persistence_authority: File,
    started: bool,
    blocked: bool,
    published_files: usize,
    published_bytes: u64,
    published_entries: BTreeSet<String>,
    #[cfg(test)]
    fault: Option<Step>,
}

/// Opens an existing absolute, canonical, private directory; never creates it.
/// Symlink roots/ancestors, non-Linux platforms, ACLs and unchecked filesystems
/// are refused. Root identity must remain stable throughout opening.
pub fn checked_private_root(path: &Path) -> Result<PrivateRoot, StorageError> {
    #[cfg(not(target_os = "linux"))]
    {
        let _ = path;
        Err(StorageError::refused("private publication requires Linux"))
    }
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::MetadataExt as _;
        if !path.is_absolute() {
            return Err(StorageError::refused("private root must be absolute"));
        }
        let before = std::fs::symlink_metadata(path).map_err(failure)?;
        let canonical = std::fs::canonicalize(path).map_err(failure)?;
        if !before.is_dir() || canonical != path {
            return Err(StorageError::refused(
                "private root must be a canonical directory without symlinks",
            ));
        }
        let identity = (before.dev(), before.ino());
        // Read-only filesystem authority, retained beside the World handles.
        // Canonical opening-path identity is checked before and after both
        // opens. Hostile same-UID/privileged root swapping remains excluded.
        let persistence_authority = File::from(
            rustix::fs::open(path, directory_read_flags(), rustix::fs::Mode::empty())
                .map_err(failure)?,
        );
        let authority_metadata = persistence_authority.metadata().map_err(failure)?;
        if (authority_metadata.dev(), authority_metadata.ino()) != identity {
            return Err(StorageError::refused(
                "root changed while opening persistence authority",
            ));
        }
        persistent_directory(&persistence_authority)?;
        let root = SourceRoot::new(path);
        let directory = root
            .directory("", false)
            .map_err(failure)?
            .ok_or_else(|| StorageError::refused("private root is missing"))?;
        let lock = root.lock().map_err(failure)?;
        let access = directory.own_access().map_err(failure)?;
        check_private(&directory, &access)?;
        let after = std::fs::symlink_metadata(path).map_err(failure)?;
        if !after.is_dir() || identity != (after.dev(), after.ino()) {
            return Err(StorageError::refused("private root changed while opening"));
        }
        Ok(PrivateRoot {
            root,
            directory,
            _lock: lock,
            access,
            canonical,
            identity,
            persistence_authority,
            started: false,
            blocked: false,
            published_files: 0,
            published_bytes: 0,
            published_entries: BTreeSet::new(),
            #[cfg(test)]
            fault: None,
        })
    }
}

fn check_private(directory: &SourceDirectory, access: &Access) -> Result<(), StorageError> {
    if access.mode != 0o700 || access.user != access.new_file().user {
        return Err(StorageError::refused(
            "private directory must be owned by this user with mode 0700 and no special bits",
        ));
    }
    if let Some(reason) = directory
        .unchecked_file_system(&Mounts::read())
        .map_err(failure)?
    {
        return Err(StorageError::refused(format!(
            "unchecked filesystem: {reason}"
        )));
    }
    Ok(())
}

// World checks filesystem features/ACL behavior, but intentionally accepts
// tmpfs. Its successful fsync is not evidence of persistence across reboot.
// Admit only these persistent local filesystems, bound to the actual open FD.
fn directory_read_flags() -> rustix::fs::OFlags {
    rustix::fs::OFlags::RDONLY
        | rustix::fs::OFlags::DIRECTORY
        | rustix::fs::OFlags::NOFOLLOW
        | rustix::fs::OFlags::CLOEXEC
}
fn proc_text(path: &Path, maximum: usize) -> Result<String, StorageError> {
    let mut bytes = Vec::new();
    File::open(path)
        .map_err(failure)?
        .take(maximum as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(failure)?;
    if bytes.len() > maximum {
        return Err(StorageError::refused("filesystem evidence exceeds bound"));
    }
    String::from_utf8(bytes).map_err(failure)
}
fn persistent_directory(directory: &File) -> Result<(), StorageError> {
    let fdinfo = proc_text(
        Path::new(&format!("/proc/self/fdinfo/{}", directory.as_raw_fd())),
        4096,
    )?;
    let ids: Vec<_> = fdinfo
        .lines()
        .filter_map(|line| line.strip_prefix("mnt_id:"))
        .collect();
    if ids.len() != 1 {
        return Err(StorageError::refused(
            "directory mount ID unavailable or ambiguous",
        ));
    }
    let mount_id = ids[0].trim().parse::<u64>().map_err(failure)?;
    let stat = rustix::fs::fstat(directory).map_err(failure)?;
    let device = (
        rustix::fs::major(stat.st_dev),
        rustix::fs::minor(stat.st_dev),
    );
    persistent_mount(
        &proc_text(Path::new("/proc/self/mountinfo"), 1024 * 1024)?,
        mount_id,
        device,
    )
}
fn persistent_beneath(root: &File, relative: &str) -> Result<(), StorageError> {
    let mut directory = root.try_clone().map_err(failure)?;
    if !relative.is_empty() {
        for part in components(relative)? {
            directory = File::from(
                rustix::fs::openat(
                    &directory,
                    part,
                    directory_read_flags(),
                    rustix::fs::Mode::empty(),
                )
                .map_err(failure)?,
            );
        }
    }
    persistent_directory(&directory)
}
fn persistent_mount(text: &str, mount_id: u64, device: (u32, u32)) -> Result<(), StorageError> {
    let mut matches = 0;
    for (index, line) in text.lines().enumerate() {
        if index >= 16_384 || line.len() > 8192 {
            return Err(StorageError::refused(
                "mount evidence entries exceed bounds",
            ));
        }
        let fields: Vec<_> = line.split_whitespace().collect();
        if fields.first().and_then(|id| id.parse::<u64>().ok()) != Some(mount_id) {
            continue;
        }
        matches += 1;
        let separator = fields
            .iter()
            .position(|field| *field == "-")
            .ok_or_else(|| StorageError::refused("malformed mount evidence"))?;
        if separator < 6 || fields.len() != separator + 4 {
            return Err(StorageError::refused("malformed mount evidence fields"));
        }
        let recorded_device = format!("{}:{}", device.0, device.1);
        if fields[2] != recorded_device
            || !matches!(fields[separator + 1], "ext4" | "xfs" | "btrfs" | "zfs")
            || !fields[5].split(',').any(|flag| flag == "rw")
            || !fields[separator + 3].split(',').any(|flag| flag == "rw")
        {
            return Err(StorageError::refused(
                "durable output requires a writable persistent local ext4/xfs/btrfs/zfs filesystem; volatile, overlay, remote and unknown filesystems are refused",
            ));
        }
    }
    if matches != 1 {
        return Err(StorageError::refused(
            "directory mount evidence missing or ambiguous",
        ));
    }
    Ok(())
}

fn components(path: &str) -> Result<Vec<&str>, StorageError> {
    if path.len() > MAX_PATH_BYTES {
        return Err(StorageError::refused("relative path too long"));
    }
    let parts: Vec<_> = path.split('/').collect();
    if parts.len() > MAX_DEPTH
        || parts.iter().any(|part| {
            part.is_empty()
                || *part == "."
                || *part == ".."
                || part.len() > 200
                || part.contains(['\\', ':', '\0'])
                || part.ends_with(STAGE_SUFFIX)
        })
    {
        return Err(StorageError::refused("invalid or excessive relative path"));
    }
    Ok(parts)
}

impl PrivateRoot {
    /// Retained read authority for the released World loader. Do not reacquire
    /// its lock while this authority remains alive.
    pub fn source_root(&self) -> &SourceRoot {
        &self.root
    }

    /// Rejects overlapping/aliased provisioned roots using opening evidence.
    pub fn ensure_separate(&self, other: &Self) -> Result<(), StorageError> {
        if self.identity == other.identity
            || self.canonical.starts_with(&other.canonical)
            || other.canonical.starts_with(&self.canonical)
        {
            return Err(StorageError::refused(
                "private roots must be separate, non-nested directories",
            ));
        }
        Ok(())
    }

    /// Checks a bounded root listing. Publication starts only in an empty root.
    pub fn require_empty(&self) -> Result<(), StorageError> {
        let (_, count) = self
            .root
            .names("", MAX_ENTRIES)
            .map_err(failure)?
            .ok_or_else(|| StorageError::refused("missing root"))?;
        if count != 0 {
            return Err(StorageError::refused(
                "publication requires an empty private root",
            ));
        }
        Ok(())
    }

    /// Enumerates every regular file, refusing links, special entries,
    /// non-UTF-8 names, empty descendant directories, and excessive trees.
    /// Linux directory locks create no entries to exclude from this inventory.
    pub fn list_files(&self) -> Result<BTreeSet<String>, StorageError> {
        let mut pending = vec![String::new()];
        let mut files = BTreeSet::new();
        let mut entries = 0usize;
        let mut bytes = 0u64;
        while let Some(parent) = pending.pop() {
            let (names, count) = self
                .root
                .names(&parent, MAX_ENTRIES.saturating_sub(entries))
                .map_err(failure)?
                .ok_or_else(|| StorageError::refused("missing enumerated directory"))?;
            if names.len() != count || (count == 0 && !parent.is_empty()) {
                return Err(StorageError::refused(
                    "non-UTF-8 name or empty descendant directory",
                ));
            }
            entries += count;
            for name in names {
                let path = join(&parent, &name);
                components(&path)?;
                match self.root.directory(&path, false) {
                    Ok(Some(_)) => pending.push(path),
                    Err(AccessError::Unexpected(_)) => {
                        let data = self.read(&path, MAX_TOTAL_BYTES.saturating_sub(bytes))?;
                        bytes += data.len() as u64;
                        files.insert(path);
                    }
                    Ok(None) => {
                        return Err(StorageError::refused(
                            "entry disappeared during enumeration",
                        ));
                    }
                    Err(error) => return Err(failure(error)),
                }
            }
        }
        Ok(files)
    }

    /// Bounded read of an existing file. Read-only path metadata additionally
    /// rejects hard links; if the opening path is moved/replaced, this read
    /// refuses rather than claiming alias verification on a different tree.
    pub fn read(&self, path: &str, maximum_bytes: u64) -> Result<Vec<u8>, StorageError> {
        components(path)?;
        self.check_read_identity(path)?;
        let (parent, name) = path.rsplit_once('/').unwrap_or(("", path));
        let directory = self
            .root
            .directory(parent, false)
            .map_err(failure)?
            .ok_or_else(|| StorageError::refused("missing input parent"))?;
        if directory.access(name).map_err(failure)? != Some(self.access.new_file()) {
            return Err(StorageError::refused(
                "input file does not have expected private access",
            ));
        }
        let bytes = self
            .root
            .read(path, maximum_bytes.min(MAX_FILE_BYTES))
            .map_err(failure)?
            .ok_or_else(|| StorageError::refused(format!("missing file: {path}")))?;
        self.check_read_identity(path)?;
        Ok(bytes)
    }

    #[cfg(target_os = "linux")]
    fn check_read_identity(&self, path: &str) -> Result<(), StorageError> {
        use std::os::unix::fs::MetadataExt as _;
        let root = std::fs::symlink_metadata(&self.canonical).map_err(failure)?;
        if !root.is_dir() || (root.dev(), root.ino()) != self.identity {
            return Err(StorageError::refused(
                "root opening path changed; existing-file alias verification unavailable",
            ));
        }
        // SourceRoot independently refuses symlink traversal. Inspect each
        // path component here too so metadata cannot follow a linked parent.
        let mut full = self.canonical.clone();
        let parts = components(path)?;
        for (index, part) in parts.iter().enumerate() {
            full.push(part);
            let meta = std::fs::symlink_metadata(&full).map_err(failure)?;
            if index + 1 == parts.len() {
                if !meta.is_file() || meta.nlink() != 1 {
                    return Err(StorageError::refused(
                        "existing input must be a regular file with exactly one hard link",
                    ));
                }
            } else if !meta.is_dir() {
                return Err(StorageError::refused(
                    "existing input has a non-directory ancestor",
                ));
            }
        }
        Ok(())
    }

    #[cfg(not(target_os = "linux"))]
    fn check_read_identity(&self, _: &str) -> Result<(), StorageError> {
        Err(StorageError::refused("private reads require Linux"))
    }

    /// Validates exactly this file set, including all directory entries, with
    /// global entry and byte bounds. Empty/unexpected directories are refused.
    pub fn require_exact_files(&self, expected: &BTreeSet<String>) -> Result<(), StorageError> {
        if expected.len() > MAX_ENTRIES {
            return Err(StorageError::refused("too many expected files"));
        }
        let mut directories: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        directories.insert(String::new(), BTreeSet::new());
        for file in expected {
            let parts = components(file)?;
            let mut parent = String::new();
            for (index, part) in parts.iter().enumerate() {
                directories
                    .entry(parent.clone())
                    .or_default()
                    .insert((*part).to_owned());
                if directories.len() > MAX_ENTRIES {
                    return Err(StorageError::refused("too many expected directories"));
                }
                if index + 1 < parts.len() {
                    parent = join(&parent, part);
                    if expected.contains(&parent) {
                        return Err(StorageError::refused(
                            "file/directory collision in expected paths",
                        ));
                    }
                }
            }
        }
        let mut entries = 0usize;
        for (path, names) in directories {
            let (actual, count) = self
                .root
                .names(&path, MAX_ENTRIES.saturating_sub(entries))
                .map_err(failure)?
                .ok_or_else(|| StorageError::refused(format!("missing directory: {path}")))?;
            entries = entries
                .checked_add(count)
                .ok_or_else(|| StorageError::refused("entry count overflow"))?;
            if actual != names || actual.len() != count {
                return Err(StorageError::refused(format!(
                    "unexpected entries in directory: {path}"
                )));
            }
        }
        let mut total = 0u64;
        for file in expected {
            let bytes = self.read(file, MAX_TOTAL_BYTES.saturating_sub(total))?;
            total += bytes.len() as u64;
        }
        Ok(())
    }

    /// Publishes one fresh file. Descendant directories are created through
    /// World and assigned private access before file contents are written.
    /// For bundles, publish the root manifest last; an interrupted bundle is
    /// partial initialization, and must be preserved for inspection.
    pub fn publish_fresh(
        &mut self,
        path: &str,
        bytes: &[u8],
    ) -> Result<PublishReceipt, StorageError> {
        if self.blocked {
            return Err(StorageError::refused(
                "publication blocked after prior failure; inspect retained files and use a fresh root",
            ));
        }
        let parts = components(path)?;
        let mut entries = self.published_entries.clone();
        let mut entry = String::new();
        for part in parts {
            entry = join(&entry, part);
            entries.insert(entry.clone());
        }
        if entries.len() >= MAX_ENTRIES {
            return Err(StorageError::refused("publication entry quota exceeded"));
        }
        let size = bytes.len() as u64;
        if size > MAX_FILE_BYTES
            || self.published_files >= MAX_ENTRIES
            || self
                .published_bytes
                .checked_add(size)
                .is_none_or(|total| total > MAX_TOTAL_BYTES)
        {
            return Err(StorageError::refused("publication quota exceeded"));
        }
        if !self.started {
            self.require_empty()?;
            self.started = true;
        }
        let (parent, name) = path.rsplit_once('/').unwrap_or(("", path));
        let stage_name = format!("{name}{STAGE_SUFFIX}");
        let stage = join(parent, &stage_name);
        let mut applied = Applied::No;
        let mut durable = false;
        let result = (|| {
            let directory = self.prepare_directory(parent)?;
            absent(&self.root, path)?;
            absent(&self.root, &stage)?;
            directory
                .check_final_name(&stage_name, name)
                .map_err(failure)?;
            self.step(Step::BeforeWrite)?;
            directory
                .write(&stage_name, Some(&self.access.new_file()), bytes)
                .map_err(failure)?;
            self.step(Step::AfterWrite)?;
            verify(&self.root, &stage, bytes)?;
            self.step(Step::AfterStageRead)?;
            absent(&self.root, path)?;
            self.step(Step::BeforeRename)?;
            applied = Applied::Unknown;
            directory.rename(&stage_name, name).map_err(failure)?;
            self.step(Step::RenameOutcome)?;
            applied = Applied::Yes;
            self.step(Step::AfterRename)?;
            self.step(Step::BeforeSync)?;
            if !directory.sync().map_err(failure)? {
                return Err(StorageError::refused("directory sync unsupported"));
            }
            durable = true;
            self.step(Step::AfterSync)?;
            verify(&self.root, path, bytes)?;
            self.step(Step::AfterFinalRead)?;
            Ok(PublishReceipt {
                path: path.to_owned(),
                bytes: size,
                durable,
            })
        })();
        match result {
            Ok(receipt) => {
                self.published_files += 1;
                self.published_bytes += size;
                self.published_entries = entries;
                Ok(receipt)
            }
            Err(mut error) => {
                self.blocked = true;
                error.stage = Some(stage);
                error.final_path = Some(path.to_owned());
                error.applied = applied;
                error.durable = durable;
                Err(error)
            }
        }
    }

    fn prepare_directory(&self, path: &str) -> Result<SourceDirectory, StorageError> {
        persistent_directory(&self.persistence_authority)?;
        let access = self.directory.own_access().map_err(failure)?;
        check_private(&self.directory, &access)?;
        if access != self.access {
            return Err(StorageError::refused("private root access changed"));
        }
        let mut parent = String::new();
        if !path.is_empty() {
            for part in components(path)? {
                let current = join(&parent, part);
                persistent_beneath(&self.persistence_authority, &parent)?;
                let made = self.root.make_directory(&current).map_err(failure)?;
                persistent_beneath(&self.persistence_authority, &current)?;
                let directory = self
                    .root
                    .directory(&current, false)
                    .map_err(failure)?
                    .ok_or_else(|| StorageError::refused("missing output directory"))?;
                if made {
                    directory.set_own_access(&self.access).map_err(failure)?;
                }
                let access = directory.own_access().map_err(failure)?;
                check_private(&directory, &access)?;
                if access != self.access {
                    return Err(StorageError::refused(
                        "output directory access differs from private root",
                    ));
                }
                if made {
                    if !directory.sync().map_err(failure)? {
                        return Err(StorageError::refused("directory sync unsupported"));
                    }
                    let parent_dir = self
                        .root
                        .directory(&parent, false)
                        .map_err(failure)?
                        .ok_or_else(|| StorageError::refused("missing output parent"))?;
                    if !parent_dir.sync().map_err(failure)? {
                        return Err(StorageError::refused("parent directory sync unsupported"));
                    }
                }
                parent = current;
            }
        }
        // Also recheck root permissions before every publication.
        persistent_directory(&self.persistence_authority)?;
        let access = self.directory.own_access().map_err(failure)?;
        check_private(&self.directory, &access)?;
        if access != self.access {
            return Err(StorageError::refused("private root access changed"));
        }
        self.root
            .directory(path, false)
            .map_err(failure)?
            .ok_or_else(|| StorageError::refused("missing output directory"))
    }

    fn step(&self, step: Step) -> Result<(), StorageError> {
        #[cfg(test)]
        if self.fault == Some(step) {
            return Err(StorageError::refused(format!("injected {step:?} fault")));
        }
        let _ = step;
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Step {
    BeforeWrite,
    AfterWrite,
    AfterStageRead,
    BeforeRename,
    RenameOutcome,
    AfterRename,
    BeforeSync,
    AfterSync,
    AfterFinalRead,
}

fn join(parent: &str, name: &str) -> String {
    if parent.is_empty() {
        name.to_owned()
    } else {
        format!("{parent}/{name}")
    }
}

fn absent(root: &SourceRoot, path: &str) -> Result<(), StorageError> {
    if root.exists(path).map_err(failure)? {
        return Err(StorageError::refused(format!(
            "refusing existing output: {path}"
        )));
    }
    Ok(())
}

fn verify(root: &SourceRoot, path: &str, expected: &[u8]) -> Result<(), StorageError> {
    if root
        .read(path, expected.len() as u64)
        .map_err(failure)?
        .as_deref()
        != Some(expected)
    {
        return Err(StorageError::refused(format!(
            "exact byte verification failed: {path}"
        )));
    }
    Ok(())
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::{
        fs,
        os::unix::fs::{DirBuilderExt as _, MetadataExt as _, PermissionsExt as _, symlink},
        sync::atomic::{AtomicU64, Ordering},
    };

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let base = std::env::temp_dir().canonicalize().unwrap();
            let path = base.join(format!(
                "atrinik-adapter-storage-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
            Self(path)
        }
        fn root(&self) -> PrivateRoot {
            checked_private_root(&self.0).unwrap()
        }
        fn put(&self, name: &str, bytes: &[u8]) {
            fs::write(self.0.join(name), bytes).unwrap();
            fs::set_permissions(self.0.join(name), fs::Permissions::from_mode(0o600)).unwrap();
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn real_checked_filesystem_round_trip_and_exact_tree() {
        let fixture = Fixture::new();
        let mut root = fixture.root();
        root.publish_fresh("baseline/profile/a.jsonc", b"exact\r\nbytes")
            .unwrap();
        root.publish_fresh("original.map", b"arch map\nend\n")
            .unwrap();
        assert_eq!(
            root.read("baseline/profile/a.jsonc", 100).unwrap(),
            b"exact\r\nbytes"
        );
        root.require_exact_files(&BTreeSet::from([
            "baseline/profile/a.jsonc".into(),
            "original.map".into(),
        ]))
        .unwrap();
        assert_eq!(
            root.list_files().unwrap(),
            BTreeSet::from(["baseline/profile/a.jsonc".into(), "original.map".into()])
        );
        assert_eq!(
            fs::metadata(fixture.0.join("baseline")).unwrap().mode() & 0o7777,
            0o700
        );
        assert_eq!(
            fs::metadata(fixture.0.join("original.map")).unwrap().mode() & 0o7777,
            0o600
        );
        assert!(!fixture.0.join("original.map.adapter-stage").exists());
        assert!(root.read("original.map", 2).is_err());
        fixture.put("unexpected", b"untouched");
        assert!(
            root.require_exact_files(&BTreeSet::from([
                "baseline/profile/a.jsonc".into(),
                "original.map".into()
            ]))
            .is_err()
        );
    }

    #[test]
    fn simultaneous_sessions_refused_and_lock_released_on_drop() {
        let fixture = Fixture::new();
        let root = fixture.root();
        assert!(checked_private_root(&fixture.0).is_err());
        drop(root);
        let _reopened = fixture.root();
    }

    #[test]
    fn root_privacy_symlink_and_ancestor_symlink_refused() {
        let fixture = Fixture::new();
        for mode in [0o755, 0o770, 0o1700, 0o2700, 0o4700] {
            fs::set_permissions(&fixture.0, fs::Permissions::from_mode(mode)).unwrap();
            assert!(checked_private_root(&fixture.0).is_err(), "mode {mode:o}");
        }
        fs::set_permissions(&fixture.0, fs::Permissions::from_mode(0o700)).unwrap();
        let holder = Fixture::new();
        symlink(&fixture.0, holder.0.join("alias")).unwrap();
        assert!(checked_private_root(&holder.0.join("alias")).is_err());
        fs::DirBuilder::new()
            .mode(0o700)
            .create(fixture.0.join("child"))
            .unwrap();
        assert!(checked_private_root(&holder.0.join("alias/child")).is_err());
    }

    #[test]
    fn access_control_list_refused_when_system_tool_available() {
        let fixture = Fixture::new();
        let result = std::process::Command::new("setfacl")
            .args(["-m", "d:u::rwx,d:g::---,d:o::---"])
            .arg(&fixture.0)
            .status();
        let Ok(status) = result else {
            eprintln!("ACL provisioning unavailable; ACL case not exercised on this host");
            return;
        };
        assert!(status.success(), "could not provision test ACL");
        assert!(checked_private_root(&fixture.0).is_err());
    }

    #[test]
    fn volatile_mount_refused_before_any_publication() {
        let path = Path::new("/dev/shm").join(format!(
            "atrinik-adapter-volatile-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
        let fixture = Fixture(path);
        let error = checked_private_root(&fixture.0)
            .err()
            .expect("tmpfs must refuse durability");
        assert!(error.message.contains("persistent local"), "{error}");
        assert!(fs::read_dir(&fixture.0).unwrap().next().is_none());
    }

    #[test]
    fn persistence_evidence_binds_mount_id_device_and_read_write_policy() {
        let valid = "71 1 8:2 / /home rw,nosuid - ext4 /dev/test rw\n";
        persistent_mount(valid, 71, (8, 2)).unwrap();
        for invalid in [
            valid.replace("ext4", "tmpfs"),
            valid.replace("ext4", "overlay"),
            valid.replace("ext4", "nfs"),
            valid.replace("rw,nosuid", "ro,nosuid"),
            valid.replace("/dev/test rw", "/dev/test ro"),
            format!("{valid}{valid}"),
            "71 malformed\n".to_owned(),
        ] {
            assert!(persistent_mount(&invalid, 71, (8, 2)).is_err());
        }
        assert!(persistent_mount(valid, 72, (8, 2)).is_err());
        assert!(persistent_mount(valid, 71, (8, 3)).is_err());
    }

    #[test]
    fn roots_must_be_distinct_and_non_nested() {
        let first = Fixture::new();
        let second = Fixture::new();
        fs::DirBuilder::new()
            .mode(0o700)
            .create(first.0.join("child"))
            .unwrap();
        let a = first.root();
        let b = second.root();
        let child = checked_private_root(&first.0.join("child")).unwrap();
        a.ensure_separate(&b).unwrap();
        assert!(a.ensure_separate(&child).is_err());
        assert!(a.ensure_separate(&a).is_err());
    }

    #[test]
    fn fresh_root_refuses_existing_final_stage_and_hardlink_without_changes() {
        for name in ["out.map", "out.map.adapter-stage"] {
            let fixture = Fixture::new();
            fixture.put(name, b"prior");
            assert!(
                fixture
                    .root()
                    .publish_fresh("out.map", b"replacement")
                    .is_err()
            );
            assert_eq!(fs::read(fixture.0.join(name)).unwrap(), b"prior");
        }
        let fixture = Fixture::new();
        let outside = Fixture::new();
        outside.put("original", b"protected");
        fs::hard_link(outside.0.join("original"), fixture.0.join("out.map")).unwrap();
        let mut root = fixture.root();
        assert!(root.read("out.map", 100).is_err());
        assert!(root.publish_fresh("out.map", b"replacement").is_err());
        assert_eq!(fs::read(outside.0.join("original")).unwrap(), b"protected");
    }

    #[test]
    fn existing_final_and_stage_after_initialization_are_not_replaced() {
        for collision in ["out.map", "out.map.adapter-stage"] {
            let fixture = Fixture::new();
            let mut root = fixture.root();
            root.publish_fresh("first", b"first").unwrap();
            fixture.put(collision, b"prior");
            assert!(root.publish_fresh("out.map", b"replacement").is_err());
            assert_eq!(fs::read(fixture.0.join(collision)).unwrap(), b"prior");
            assert!(root.publish_fresh("later", b"blocked").is_err());
        }
    }

    #[test]
    fn invalid_paths_and_quotas_do_not_write() {
        let fixture = Fixture::new();
        let mut root = fixture.root();
        for path in [
            "",
            "/absolute",
            "../escape",
            "a/../escape",
            "a//b",
            "a\\b",
            "a:b",
            "a\0b",
            "a.adapter-stage",
        ] {
            assert!(root.publish_fresh(path, b"bad").is_err(), "{path:?}");
        }
        assert!(root.publish_fresh(&"a".repeat(1025), b"bad").is_err());
        assert!(root.publish_fresh(&["a"; 17].join("/"), b"bad").is_err());
        root.published_bytes = MAX_TOTAL_BYTES;
        assert!(root.publish_fresh("out", b"bad").is_err());
        root.published_bytes = 0;
        root.published_files = MAX_ENTRIES;
        assert!(root.publish_fresh("out", b"bad").is_err());
        root.require_empty().unwrap();
    }

    #[test]
    fn symlink_descendant_and_changed_root_access_fail_closed() {
        let fixture = Fixture::new();
        let outside = Fixture::new();
        let mut root = fixture.root();
        root.publish_fresh("first", b"first").unwrap();
        symlink(&outside.0, fixture.0.join("linked")).unwrap();
        assert!(root.publish_fresh("linked/out", b"bad").is_err());
        assert!(!outside.0.join("out").exists());
        let fixture = Fixture::new();
        let mut root = fixture.root();
        fs::set_permissions(&fixture.0, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(root.publish_fresh("new/out", b"bad").is_err());
        assert!(!fixture.0.join("new").exists());
    }

    #[test]
    fn root_retarget_never_redirects_publication() {
        let fixture = Fixture::new();
        let holder = Fixture::new();
        let mut root = fixture.root();
        let moved = holder.0.join("retained");
        fs::rename(&fixture.0, &moved).unwrap();
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&fixture.0)
            .unwrap();
        root.publish_fresh("out", b"retained root").unwrap();
        assert_eq!(fs::read(moved.join("out")).unwrap(), b"retained root");
        assert!(!fixture.0.join("out").exists());
        assert!(root.read("out", 100).is_err());
    }

    #[test]
    fn every_publication_boundary_preserves_evidence_and_blocks_retry() {
        for step in [
            Step::BeforeWrite,
            Step::AfterWrite,
            Step::AfterStageRead,
            Step::BeforeRename,
            Step::RenameOutcome,
            Step::AfterRename,
            Step::BeforeSync,
            Step::AfterSync,
            Step::AfterFinalRead,
        ] {
            let fixture = Fixture::new();
            let mut root = fixture.root();
            root.fault = Some(step);
            let error = root
                .publish_fresh("out", b"exact retained bytes")
                .unwrap_err();
            assert_eq!(error.stage.as_deref(), Some("out.adapter-stage"));
            assert_eq!(error.final_path.as_deref(), Some("out"));
            let renamed = matches!(
                step,
                Step::RenameOutcome
                    | Step::AfterRename
                    | Step::BeforeSync
                    | Step::AfterSync
                    | Step::AfterFinalRead
            );
            assert_eq!(
                error.applied,
                if step == Step::RenameOutcome {
                    Applied::Unknown
                } else if renamed {
                    Applied::Yes
                } else {
                    Applied::No
                }
            );
            assert_eq!(
                error.durable,
                matches!(step, Step::AfterSync | Step::AfterFinalRead)
            );
            if step != Step::BeforeWrite {
                let retained = if renamed { "out" } else { "out.adapter-stage" };
                assert_eq!(
                    fs::read(fixture.0.join(retained)).unwrap(),
                    b"exact retained bytes"
                );
            }
            root.fault = None;
            assert!(root.publish_fresh("retry", b"must not appear").is_err());
            assert!(!fixture.0.join("retry").exists());
            drop(root);
            if step != Step::BeforeWrite {
                assert!(fixture.root().require_empty().is_err());
            }
        }
    }

    #[test]
    fn non_utf8_special_entries_and_excessive_listing_are_refused() {
        use std::os::unix::ffi::OsStringExt as _;
        let fixture = Fixture::new();
        fs::write(
            fixture.0.join(std::ffi::OsString::from_vec(vec![0xff])),
            b"bad",
        )
        .unwrap();
        assert!(
            fixture
                .root()
                .require_exact_files(&BTreeSet::new())
                .is_err()
        );
        let fixture = Fixture::new();
        let _socket = std::os::unix::net::UnixListener::bind(fixture.0.join("socket")).unwrap();
        assert!(fixture.root().list_files().is_err());
        let fixture = Fixture::new();
        for i in 0..=MAX_ENTRIES {
            fixture.put(&format!("file-{i}"), b"");
        }
        assert!(fixture.root().require_empty().is_err());
    }
}
