// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

//! Linux generation store: one atomic CURRENT replacement publishes an entire
//! project. Readers must use `read` once, then retain that immutable snapshot.
//! This does not update an ordinary directory of authored files in place.

use crate::project::{
    self, Control, Preview, ProjectFile, ProjectLimits, ProjectPlan, ProjectPolicy,
    ProjectSnapshot, SourceIdentity, TransactionError,
};
use atrinik_source::{Document, Limits, SourceId};
use rustix::fs::{self, FlockOperation, Mode, OFlags};
use std::{
    collections::BTreeMap,
    fmt,
    fs::File,
    io::{self, Read, Write},
    os::unix::fs::MetadataExt,
    path::{Component, Path},
    sync::Arc,
};

const MAGIC: &[u8; 8] = b"ATRGEN01";
const READ_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::CLOEXEC)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::NONBLOCK);

#[derive(Debug)]
pub enum StoreError {
    Io(io::Error),
    Transaction(TransactionError),
    InvalidStore(&'static str),
    Uninitialized,
    AlreadyInitialized,
    Busy,
    InvalidPreview,
    Stale { expected: String, actual: String },
    Injected(FaultStage),
}
impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "store: {self:?}")
    }
}
impl std::error::Error for StoreError {}
impl From<io::Error> for StoreError {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}
impl From<rustix::io::Errno> for StoreError {
    fn from(value: rustix::io::Errno) -> Self {
        Self::Io(value.into())
    }
}
impl From<TransactionError> for StoreError {
    fn from(value: TransactionError) -> Self {
        Self::Transaction(value)
    }
}
impl From<atrinik_source::Error> for StoreError {
    fn from(value: atrinik_source::Error) -> Self {
        Self::Transaction(value.into())
    }
}

/// Checkpoints occur after each named operation, except BeforePublish.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FaultStage {
    GenerationCreated,
    GenerationWritten,
    GenerationSynced,
    GenerationInstalled,
    GenerationDirectorySynced,
    PointerCreated,
    PointerWritten,
    PointerSynced,
    BeforePublish,
    Published,
    Committed,
}
pub trait FaultInjector {
    fn check(&mut self, stage: FaultStage) -> Result<(), StoreError>;
}
impl<F: FnMut(FaultStage) -> Result<(), StoreError>> FaultInjector for F {
    fn check(&mut self, stage: FaultStage) -> Result<(), StoreError> {
        self(stage)
    }
}

/// Publication has happened whenever this is returned. A false durable flag
/// means directory fsync could not be confirmed; recover/read before retrying.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommitOutcome {
    pub revision: String,
    pub durable: bool,
    pub warning: Option<String>,
}

/// Root must be an existing absolute directory owned by this effective user,
/// mode 0700. Every component is opened relative to a pinned descriptor without
/// following symlinks. Writers and readers coordinate on the directory inode.
/// Callers must keep the root private and use this protocol for every writer.
pub struct GenerationStore {
    root: File,
    limits: ProjectLimits,
    source_limits: Limits,
}
impl GenerationStore {
    pub fn open(
        root: &Path,
        limits: ProjectLimits,
        source_limits: Limits,
    ) -> Result<Self, StoreError> {
        let directory = open_directory(root)?;
        check_root(&directory)?;
        Ok(Self {
            root: directory,
            limits,
            source_limits,
        })
    }

    pub fn initialize(
        &self,
        snapshot: &ProjectSnapshot,
        policy: &ProjectPolicy,
        control: &Control<'_>,
    ) -> Result<CommitOutcome, StoreError> {
        self.initialize_with_faults(snapshot, policy, control, &mut |_| Ok(()))
    }
    pub fn initialize_with_faults(
        &self,
        snapshot: &ProjectSnapshot,
        policy: &ProjectPolicy,
        control: &Control<'_>,
        faults: &mut impl FaultInjector,
    ) -> Result<CommitOutcome, StoreError> {
        let plan = ProjectPlan {
            version: 1,
            expected_project_revision: snapshot.revision().into(),
            commands: Vec::new(),
        };
        let validated = project::preview(snapshot, &plan, policy, control)?;
        if !validated.is_valid() {
            return Err(StoreError::InvalidPreview);
        }
        let _lock = self.lock(true, control)?;
        match self.current(control) {
            Err(StoreError::Uninitialized) => (),
            Ok(_) => return Err(StoreError::AlreadyInitialized),
            Err(error) => return Err(error),
        }
        self.publish(validated.result(), None, control, faults)
    }

    /// Recovery is identical to read: only CURRENT is authoritative. Temporary
    /// files and unreferenced generations are ignored and retained for audit.
    pub fn read(&self, control: &Control<'_>) -> Result<ProjectSnapshot, StoreError> {
        let _lock = self.lock(false, control)?;
        Ok(self.current(control)?.snapshot)
    }
    pub fn recover(&self, control: &Control<'_>) -> Result<ProjectSnapshot, StoreError> {
        self.read(control)
    }

    pub fn apply(
        &self,
        preview: &Preview,
        control: &Control<'_>,
    ) -> Result<CommitOutcome, StoreError> {
        self.apply_with_faults(preview, control, &mut |_| Ok(()))
    }
    pub fn apply_with_faults(
        &self,
        preview: &Preview,
        control: &Control<'_>,
        faults: &mut impl FaultInjector,
    ) -> Result<CommitOutcome, StoreError> {
        control.check()?;
        if !preview.is_valid() {
            return Err(StoreError::InvalidPreview);
        }
        // The preview owns complete validation and an exact destination allowlist.
        if !preview
            .original()
            .files()
            .keys()
            .eq(preview.result().files().keys())
        {
            return Err(StoreError::InvalidPreview);
        }
        let _lock = self.lock(true, control)?;
        let current = self.current(control)?;
        if current.snapshot.revision() != preview.original().revision() {
            return Err(StoreError::Stale {
                expected: preview.original().revision().into(),
                actual: current.snapshot.revision().into(),
            });
        }
        self.publish(preview.result(), Some(&current), control, faults)
    }

    fn lock(&self, exclusive: bool, control: &Control<'_>) -> Result<File, StoreError> {
        control.check()?;
        check_root(&self.root)?;
        // A new open file description is essential: dup/try_clone would share
        // the lock and permit two threads using this store to bypass each other.
        let lock = File::from(fs::openat(
            &self.root,
            ".",
            READ_FLAGS | OFlags::DIRECTORY,
            Mode::empty(),
        )?);
        let operation = if exclusive {
            FlockOperation::NonBlockingLockExclusive
        } else {
            FlockOperation::NonBlockingLockShared
        };
        match fs::flock(&lock, operation) {
            Ok(()) => Ok(lock),
            Err(rustix::io::Errno::WOULDBLOCK) => Err(StoreError::Busy),
            Err(error) => Err(error.into()),
        }
    }

    fn current(&self, control: &Control<'_>) -> Result<Current, StoreError> {
        let (pointer, pointer_stamp) = match self.read_file("CURRENT", 65, control) {
            Err(StoreError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
                return Err(StoreError::Uninitialized);
            }
            result => result?,
        };
        if pointer.len() != 65
            || pointer[64] != b'\n'
            || !pointer[..64]
                .iter()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
        {
            return Err(StoreError::InvalidStore("invalid CURRENT"));
        }
        let revision = std::str::from_utf8(&pointer[..64])
            .map_err(|_| StoreError::InvalidStore("invalid revision"))?;
        let (bytes, generation_stamp) =
            self.read_file(&format!("gen-{revision}"), self.bundle_limit()?, control)?;
        let snapshot = decode(&bytes, self.limits, self.source_limits, control)?;
        if snapshot.revision() != revision {
            return Err(StoreError::InvalidStore("generation digest mismatch"));
        }
        Ok(Current {
            snapshot,
            pointer_stamp,
            generation_stamp,
        })
    }

    fn bundle_limit(&self) -> Result<usize, StoreError> {
        self.limits
            .maximum_files
            .checked_mul(528)
            .and_then(|n| n.checked_add(self.limits.maximum_bytes))
            .and_then(|n| n.checked_add(256))
            .ok_or(StoreError::InvalidStore("bundle limit overflow"))
    }

    fn read_file(
        &self,
        name: &str,
        limit: usize,
        control: &Control<'_>,
    ) -> Result<(Vec<u8>, Stamp), StoreError> {
        control.check()?;
        let file = File::from(fs::openat(&self.root, name, READ_FLAGS, Mode::empty())?);
        let metadata = file.metadata()?;
        check_regular(&metadata, 0o400)?;
        let stamp = Stamp::from(&metadata);
        let size = usize::try_from(metadata.len())
            .map_err(|_| StoreError::InvalidStore("file length overflow"))?;
        if size > limit {
            return Err(StoreError::InvalidStore("file exceeds limit"));
        }
        let mut bytes = Vec::with_capacity(size);
        (&file).take(limit as u64 + 1).read_to_end(&mut bytes)?;
        if bytes.len() != size || Stamp::from(&file.metadata()?) != stamp {
            return Err(StoreError::InvalidStore("file changed during read"));
        }
        control.check()?;
        Ok((bytes, stamp))
    }

    fn temporary(&self, prefix: &str) -> Result<Temporary<'_>, StoreError> {
        // Locked writers use a bounded deterministic search; crash remnants are
        // never truncated. No implicit process-global counter or mutable state.
        for index in 0..1024 {
            let name = format!(".tmp-{prefix}-{index}");
            match fs::openat(
                &self.root,
                name.as_str(),
                OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::CLOEXEC | OFlags::NOFOLLOW,
                Mode::RUSR | Mode::WUSR,
            ) {
                Ok(fd) => {
                    return Ok(Temporary {
                        root: &self.root,
                        name: Some(name),
                        file: File::from(fd),
                    });
                }
                Err(rustix::io::Errno::EXIST) => (),
                Err(error) => return Err(error.into()),
            }
        }
        Err(StoreError::InvalidStore(
            "temporary slot limit; inspect crash remnants",
        ))
    }

    fn publish(
        &self,
        snapshot: &ProjectSnapshot,
        expected: Option<&Current>,
        control: &Control<'_>,
        faults: &mut impl FaultInjector,
    ) -> Result<CommitOutcome, StoreError> {
        ProjectSnapshot::new(
            snapshot.identity().clone(),
            snapshot.files().clone(),
            self.limits,
        )?;
        let bytes = encode(snapshot);
        // Ensure the stored generation can be decoded under this store's limits.
        decode(&bytes, self.limits, self.source_limits, control)?;
        if bytes.len() > self.bundle_limit()? {
            return Err(StoreError::InvalidStore("bundle exceeds limit"));
        }
        let generation = format!("gen-{}", snapshot.revision());
        match self.read_file(&generation, self.bundle_limit()?, control) {
            Ok((existing, _)) if existing == bytes => (),
            Ok(_) => return Err(StoreError::InvalidStore("existing generation was modified")),
            Err(StoreError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
                let mut temporary = self.temporary("generation")?;
                checkpoint(control, faults, FaultStage::GenerationCreated)?;
                temporary.file.write_all(&bytes)?;
                checkpoint(control, faults, FaultStage::GenerationWritten)?;
                fs::fchmod(&temporary.file, Mode::RUSR)?;
                temporary.file.sync_all()?;
                checkpoint(control, faults, FaultStage::GenerationSynced)?;
                temporary.install(&generation)?;
                checkpoint(control, faults, FaultStage::GenerationInstalled)?;
            }
            Err(error) => return Err(error),
        }
        self.root.sync_all()?;
        checkpoint(control, faults, FaultStage::GenerationDirectorySynced)?;
        let mut pointer = self.temporary("current")?;
        checkpoint(control, faults, FaultStage::PointerCreated)?;
        writeln!(pointer.file, "{}", snapshot.revision())?;
        checkpoint(control, faults, FaultStage::PointerWritten)?;
        fs::fchmod(&pointer.file, Mode::RUSR)?;
        pointer.file.sync_all()?;
        checkpoint(control, faults, FaultStage::PointerSynced)?;
        checkpoint(control, faults, FaultStage::BeforePublish)?;
        // Recheck bytes and metadata immediately before the single publication
        // rename. Cooperative writers are fenced by the directory lock.
        match (expected, self.current(control)) {
            (Some(old), Ok(now)) if old.same(&now) => (),
            (None, Err(StoreError::Uninitialized)) => (),
            (_, Err(error)) => return Err(error),
            _ => {
                return Err(StoreError::InvalidStore(
                    "CURRENT changed before publication",
                ));
            }
        }
        let (installed, _) = self.read_file(&generation, self.bundle_limit()?, control)?;
        if installed != bytes {
            return Err(StoreError::InvalidStore(
                "new generation changed before publication",
            ));
        }
        let (pointer_bytes, pointer_stamp) = self.read_file(
            pointer.name.as_deref().expect("temporary pointer"),
            65,
            control,
        )?;
        if pointer_bytes != format!("{}\n", snapshot.revision()).as_bytes()
            || pointer_stamp != Stamp::from(&pointer.file.metadata()?)
        {
            return Err(StoreError::InvalidStore(
                "temporary pointer changed before publication",
            ));
        }
        check_root(&self.root)?;
        control.check()?;
        pointer.install("CURRENT")?;
        // Past the linearization point rollback would itself be a second
        // publication. Always report the committed revision, even after faults.
        let result = faults
            .check(FaultStage::Published)
            .and_then(|()| self.root.sync_all().map_err(StoreError::from));
        if let Err(error) = result {
            return Ok(CommitOutcome {
                revision: snapshot.revision().into(),
                durable: false,
                warning: Some(error.to_string()),
            });
        }
        let warning = faults
            .check(FaultStage::Committed)
            .err()
            .map(|error| error.to_string());
        Ok(CommitOutcome {
            revision: snapshot.revision().into(),
            durable: true,
            warning,
        })
    }
}

/// Explicit bounded import of one allowlisted authored file. Every component
/// uses descriptor-relative no-follow opens. The caller supplies the complete
/// inventory and validates the assembled project before initialization.
pub fn read_source_file(
    root: &Path,
    relative: &str,
    source_id: SourceId,
    limits: Limits,
    control: &Control<'_>,
) -> Result<ProjectFile, StoreError> {
    control.check()?;
    project::validate_path(relative)?;
    let mut directory = open_directory(root)?;
    let mut parts = relative.split('/').peekable();
    while let Some(part) = parts.next() {
        if parts.peek().is_none() {
            let file = File::from(fs::openat(&directory, part, READ_FLAGS, Mode::empty())?);
            let metadata = file.metadata()?;
            if !metadata.is_file() || metadata.nlink() != 1 || metadata.mode() & 0o7000 != 0 {
                return Err(StoreError::InvalidStore(
                    "source must be a regular singly-linked file without special mode bits",
                ));
            }
            let length = usize::try_from(metadata.len())
                .map_err(|_| StoreError::InvalidStore("source length overflow"))?;
            if length > limits.maximum_file_bytes {
                return Err(StoreError::InvalidStore("source exceeds byte limit"));
            }
            let mut bytes = Vec::with_capacity(length);
            (&file)
                .take((limits.maximum_file_bytes as u64).saturating_add(1))
                .read_to_end(&mut bytes)?;
            if bytes.len() != length || Stamp::from(&file.metadata()?) != Stamp::from(&metadata) {
                return Err(StoreError::InvalidStore("source changed during read"));
            }
            control.check()?;
            return Ok(ProjectFile {
                document: Arc::new(Document::parse(
                    source_id,
                    Arc::<[u8]>::from(bytes),
                    limits,
                )?),
                mode: metadata.mode() & 0o777,
            });
        }
        directory = File::from(fs::openat(
            &directory,
            part,
            READ_FLAGS | OFlags::DIRECTORY,
            Mode::empty(),
        )?);
        control.check()?;
    }
    Err(StoreError::InvalidStore("empty source path"))
}

fn open_directory(root: &Path) -> Result<File, StoreError> {
    if !root.is_absolute()
        || root
            .as_os_str()
            .as_encoded_bytes()
            .split(|b| *b == b'/')
            .any(|part| part == b".." || part == b".")
    {
        return Err(StoreError::InvalidStore(
            "absolute normalized root required",
        ));
    }
    let mut directory = File::from(fs::open(
        "/",
        READ_FLAGS | OFlags::DIRECTORY,
        Mode::empty(),
    )?);
    for component in root.components() {
        match component {
            Component::RootDir => (),
            Component::Normal(name) => {
                directory = File::from(fs::openat(
                    &directory,
                    name,
                    READ_FLAGS | OFlags::DIRECTORY,
                    Mode::empty(),
                )?)
            }
            _ => return Err(StoreError::InvalidStore("invalid root component")),
        }
    }
    Ok(directory)
}

fn checkpoint(
    control: &Control<'_>,
    faults: &mut impl FaultInjector,
    stage: FaultStage,
) -> Result<(), StoreError> {
    faults.check(stage)?;
    control.check()?;
    Ok(())
}
fn check_root(file: &File) -> Result<(), StoreError> {
    let metadata = file.metadata()?;
    if !metadata.is_dir()
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.mode() & 0o7777 != 0o700
    {
        return Err(StoreError::InvalidStore(
            "root must be owned private mode 0700 directory",
        ));
    }
    Ok(())
}
fn check_regular(metadata: &std::fs::Metadata, mode: u32) -> Result<(), StoreError> {
    if !metadata.is_file()
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.nlink() != 1
        || metadata.mode() & 0o7777 != mode
    {
        return Err(StoreError::InvalidStore(
            "invalid store file type, ownership, links or permissions",
        ));
    }
    Ok(())
}
#[derive(Eq, PartialEq)]
struct Stamp {
    dev: u64,
    ino: u64,
    size: u64,
    mode: u32,
    uid: u32,
    gid: u32,
    links: u64,
    modified: (i64, i64),
    changed: (i64, i64),
}
impl From<&std::fs::Metadata> for Stamp {
    fn from(m: &std::fs::Metadata) -> Self {
        Self {
            dev: m.dev(),
            ino: m.ino(),
            size: m.len(),
            mode: m.mode(),
            uid: m.uid(),
            gid: m.gid(),
            links: m.nlink(),
            modified: (m.mtime(), m.mtime_nsec()),
            changed: (m.ctime(), m.ctime_nsec()),
        }
    }
}
struct Current {
    snapshot: ProjectSnapshot,
    pointer_stamp: Stamp,
    generation_stamp: Stamp,
}
impl Current {
    fn same(&self, other: &Self) -> bool {
        self.snapshot.revision() == other.snapshot.revision()
            && self.pointer_stamp == other.pointer_stamp
            && self.generation_stamp == other.generation_stamp
    }
}
struct Temporary<'a> {
    root: &'a File,
    name: Option<String>,
    file: File,
}
impl Temporary<'_> {
    fn install(&mut self, destination: &str) -> Result<(), StoreError> {
        fs::renameat(
            self.root,
            self.name.as_deref().expect("temporary name"),
            self.root,
            destination,
        )?;
        self.name = None;
        Ok(())
    }
}
impl Drop for Temporary<'_> {
    fn drop(&mut self) {
        if let Some(name) = &self.name {
            let _ = fs::unlinkat(self.root, name.as_str(), fs::AtFlags::empty());
        }
    }
}

fn encode(snapshot: &ProjectSnapshot) -> Vec<u8> {
    let mut bytes = MAGIC.to_vec();
    for value in [
        &snapshot.identity().repository,
        &snapshot.identity().reference,
        &snapshot.identity().revision,
    ] {
        push(&mut bytes, value.as_bytes());
    }
    bytes.extend_from_slice(&snapshot.identity().schema_version.to_le_bytes());
    bytes.extend_from_slice(&(snapshot.files().len() as u64).to_le_bytes());
    for (path, file) in snapshot.files() {
        push(&mut bytes, path.as_bytes());
        push(&mut bytes, file.document.source_id().as_str().as_bytes());
        bytes.extend_from_slice(&file.mode.to_le_bytes());
        push(&mut bytes, file.document.source_bytes());
    }
    bytes
}
fn push(target: &mut Vec<u8>, bytes: &[u8]) {
    target.extend_from_slice(&(bytes.len() as u64).to_le_bytes());
    target.extend_from_slice(bytes);
}
struct Decoder<'a> {
    bytes: &'a [u8],
}
impl<'a> Decoder<'a> {
    fn take(&mut self, length: usize) -> Result<&'a [u8], StoreError> {
        if length > self.bytes.len() {
            return Err(StoreError::InvalidStore("truncated generation"));
        }
        let (value, rest) = self.bytes.split_at(length);
        self.bytes = rest;
        Ok(value)
    }
    fn number(&mut self) -> Result<usize, StoreError> {
        usize::try_from(u64::from_le_bytes(
            self.take(8)?.try_into().expect("8 bytes"),
        ))
        .map_err(|_| StoreError::InvalidStore("length overflow"))
    }
    fn part(&mut self, limit: usize) -> Result<&'a [u8], StoreError> {
        let length = self.number()?;
        if length > limit {
            return Err(StoreError::InvalidStore("generation field exceeds limit"));
        }
        self.take(length)
    }
    fn string(&mut self, limit: usize) -> Result<String, StoreError> {
        String::from_utf8(self.part(limit)?.to_vec())
            .map_err(|_| StoreError::InvalidStore("invalid UTF-8 metadata"))
    }
    fn u32(&mut self) -> Result<u32, StoreError> {
        Ok(u32::from_le_bytes(
            self.take(4)?.try_into().expect("4 bytes"),
        ))
    }
}
fn decode(
    bytes: &[u8],
    limits: ProjectLimits,
    source_limits: Limits,
    control: &Control<'_>,
) -> Result<ProjectSnapshot, StoreError> {
    let mut reader = Decoder { bytes };
    if reader.take(8)? != MAGIC {
        return Err(StoreError::InvalidStore("unsupported generation version"));
    }
    let identity = SourceIdentity {
        repository: reader.string(64)?,
        reference: reader.string(64)?,
        revision: reader.string(40)?,
        schema_version: reader.u32()?,
    };
    let count = reader.number()?;
    if count > limits.maximum_files {
        return Err(StoreError::InvalidStore("file count exceeds limit"));
    }
    let mut files = BTreeMap::new();
    let mut total = 0usize;
    let mut previous = String::new();
    for _ in 0..count {
        control.check()?;
        let path = reader.string(240)?;
        if path <= previous {
            return Err(StoreError::InvalidStore("unordered or duplicate path"));
        }
        previous.clone_from(&path);
        let source = SourceId::new(reader.string(256)?)?;
        let mode = reader.u32()?;
        let contents = reader.part(source_limits.maximum_file_bytes)?;
        total = total
            .checked_add(contents.len())
            .ok_or(StoreError::InvalidStore("byte length overflow"))?;
        if total > limits.maximum_bytes {
            return Err(StoreError::InvalidStore("project bytes exceed limit"));
        }
        let document = Arc::new(Document::parse(
            source,
            Arc::<[u8]>::from(contents),
            source_limits,
        )?);
        files.insert(path, ProjectFile { document, mode });
    }
    if !reader.bytes.is_empty() {
        return Err(StoreError::InvalidStore("trailing generation bytes"));
    }
    Ok(ProjectSnapshot::new(identity, files, limits)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::project::{CatalogShape, FilePolicy, ReplaceValue};
    use atrinik_catalog::{CatalogLimits, Domain, FieldRule, LineDocumentLoader};
    use atrinik_schema::{Schema, SchemaLimits};
    use atrinik_source::RecordKind;
    use std::{
        fs as stdfs,
        os::unix::fs::{PermissionsExt, symlink},
        path::PathBuf,
        sync::atomic::{AtomicBool, AtomicU64, Ordering},
        time::{Duration, Instant},
    };

    struct Fixture {
        path: PathBuf,
        store: GenerationStore,
        snapshot: ProjectSnapshot,
        policy: ProjectPolicy,
    }
    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "atrinik-store-{}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            stdfs::create_dir(&path).unwrap();
            stdfs::set_permissions(&path, stdfs::Permissions::from_mode(0o700)).unwrap();
            let mut files = BTreeMap::new();
            let mut policies = BTreeMap::new();
            for (path, bytes, mode) in [
                ("first.arc", &b"Object first\nname old\nend\n"[..], 0o640),
                (
                    "nested/second.arc",
                    &b"Object second\r\nname old\r\nend\r\n"[..],
                    0o600,
                ),
            ] {
                files.insert(
                    path.into(),
                    ProjectFile {
                        document: Arc::new(
                            Document::parse(
                                SourceId::new(path).unwrap(),
                                Arc::<[u8]>::from(bytes),
                                Limits::default(),
                            )
                            .unwrap(),
                        ),
                        mode,
                    },
                );
                policies.insert(
                    path.into(),
                    FilePolicy {
                        schema: Schema::new("fixture", [b"name".to_vec()], SchemaLimits::default())
                            .unwrap(),
                        loader: LineDocumentLoader::new(
                            Domain::Archetype,
                            "fixture",
                            1,
                            [(b"name".to_vec(), FieldRule::Label)],
                        )
                        .unwrap(),
                        shape: CatalogShape::Objects,
                    },
                );
            }
            let limits = ProjectLimits::default();
            let snapshot = ProjectSnapshot::new(
                SourceIdentity {
                    repository: "atrinik/content".into(),
                    reference: "refs/heads/main".into(),
                    revision: "a".repeat(40),
                    schema_version: 1,
                },
                files,
                limits,
            )
            .unwrap();
            let policy = ProjectPolicy {
                files: policies,
                limits,
                catalog_limits: CatalogLimits::default(),
            };
            let store = GenerationStore::open(&path, limits, Limits::default()).unwrap();
            Self {
                path,
                store,
                snapshot,
                policy,
            }
        }
        fn initialize(&self) {
            assert!(
                self.store
                    .initialize(&self.snapshot, &self.policy, &control())
                    .unwrap()
                    .durable
            );
        }
        fn preview(&self) -> Preview {
            let commands = self
                .snapshot
                .files()
                .iter()
                .map(|(path, file)| {
                    let RecordKind::Field { value, .. } = file.document.records()[1].kind else {
                        panic!()
                    };
                    ReplaceValue {
                        path: path.clone(),
                        expected_source_revision: file.document.revision().to_string(),
                        record: 1,
                        expected_span: value,
                        semantic_intent: "synthetic label change".into(),
                        replacement: b"new".to_vec(),
                    }
                })
                .collect();
            project::preview(
                &self.snapshot,
                &ProjectPlan {
                    version: 1,
                    expected_project_revision: self.snapshot.revision().into(),
                    commands,
                },
                &self.policy,
                &control(),
            )
            .unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = stdfs::set_permissions(&self.path, stdfs::Permissions::from_mode(0o700));
            let _ = stdfs::remove_dir_all(&self.path);
        }
    }
    fn control() -> Control<'static> {
        static CANCEL: AtomicBool = AtomicBool::new(false);
        Control {
            cancelled: &CANCEL,
            deadline: Instant::now() + Duration::from_secs(30),
        }
    }

    #[test]
    fn publish_preserves_modes_bytes_and_reader_snapshot() {
        let fixture = Fixture::new();
        fixture.initialize();
        let before = fixture.store.read(&control()).unwrap();
        let preview = fixture.preview();
        let outcome = fixture.store.apply(&preview, &control()).unwrap();
        assert!(outcome.durable);
        assert_eq!(outcome.revision, preview.result().revision());
        let after = fixture.store.read(&control()).unwrap();
        for (path, file) in after.files() {
            assert_eq!(file.mode, before.files()[path].mode);
            assert_eq!(
                file.document.source_bytes(),
                preview.result().files()[path].document.source_bytes()
            );
            assert!(
                before.files()[path]
                    .document
                    .source_bytes()
                    .windows(3)
                    .any(|w| w == b"old")
            );
        }
        assert!(matches!(
            fixture.store.apply(&preview, &control()),
            Err(StoreError::Stale { .. })
        ));
        assert!(matches!(
            fixture
                .store
                .initialize(&fixture.snapshot, &fixture.policy, &control()),
            Err(StoreError::AlreadyInitialized)
        ));
    }

    #[test]
    fn rejects_invalid_seed_and_invalid_preview() {
        let mut fixture = Fixture::new();
        fixture.policy.files.get_mut("first.arc").unwrap().schema =
            Schema::new("invalid", [b"absent".to_vec()], SchemaLimits::default()).unwrap();
        assert!(matches!(
            fixture
                .store
                .initialize(&fixture.snapshot, &fixture.policy, &control()),
            Err(StoreError::InvalidPreview)
        ));
        assert!(matches!(
            fixture.store.read(&control()),
            Err(StoreError::Uninitialized)
        ));
        fixture.policy.files.get_mut("first.arc").unwrap().schema =
            Schema::new("valid", [b"name".to_vec()], SchemaLimits::default()).unwrap();
        fixture.initialize();
        fixture.policy.files.get_mut("first.arc").unwrap().schema =
            Schema::new("invalid", [b"absent".to_vec()], SchemaLimits::default()).unwrap();
        let preview = fixture.preview();
        assert!(!preview.is_valid());
        assert!(matches!(
            fixture.store.apply(&preview, &control()),
            Err(StoreError::InvalidPreview)
        ));
        assert_eq!(
            fixture.store.read(&control()).unwrap().revision(),
            fixture.snapshot.revision()
        );
    }

    #[test]
    fn cancellation_after_publication_reports_committed_revision() {
        let fixture = Fixture::new();
        fixture.initialize();
        let cancelled = AtomicBool::new(false);
        let operation = Control {
            cancelled: &cancelled,
            deadline: Instant::now() + Duration::from_secs(30),
        };
        let preview = fixture.preview();
        let outcome = fixture
            .store
            .apply_with_faults(&preview, &operation, &mut |stage| {
                if stage == FaultStage::Published {
                    cancelled.store(true, Ordering::Release);
                }
                Ok(())
            })
            .unwrap();
        assert!(outcome.durable);
        assert_eq!(
            fixture.store.read(&control()).unwrap().revision(),
            preview.result().revision()
        );
    }

    #[test]
    fn every_fault_checkpoint_recovers_exactly_old_or_new() {
        for stage in [
            FaultStage::GenerationCreated,
            FaultStage::GenerationWritten,
            FaultStage::GenerationSynced,
            FaultStage::GenerationInstalled,
            FaultStage::GenerationDirectorySynced,
            FaultStage::PointerCreated,
            FaultStage::PointerWritten,
            FaultStage::PointerSynced,
            FaultStage::BeforePublish,
            FaultStage::Published,
            FaultStage::Committed,
        ] {
            let fixture = Fixture::new();
            fixture.initialize();
            let preview = fixture.preview();
            let mut reached = false;
            let result = fixture
                .store
                .apply_with_faults(&preview, &control(), &mut |current| {
                    if current == stage {
                        reached = true;
                        Err(StoreError::Injected(stage))
                    } else {
                        Ok(())
                    }
                });
            assert!(reached, "{stage:?}");
            let recovered =
                GenerationStore::open(&fixture.path, ProjectLimits::default(), Limits::default())
                    .unwrap()
                    .recover(&control())
                    .unwrap();
            if matches!(stage, FaultStage::Published | FaultStage::Committed) {
                let outcome = result.unwrap();
                assert_eq!(outcome.durable, stage == FaultStage::Committed);
                assert!(outcome.warning.is_some());
                assert_eq!(recovered.revision(), preview.result().revision());
            } else {
                assert!(result.is_err());
                assert_eq!(recovered.revision(), fixture.snapshot.revision());
                // Retained installed generations can be safely reused on retry.
                assert!(fixture.store.apply(&preview, &control()).unwrap().durable);
            }
        }
    }

    #[test]
    fn failed_initialization_and_orphans_never_become_authoritative() {
        let fixture = Fixture::new();
        assert!(
            fixture
                .store
                .initialize_with_faults(
                    &fixture.snapshot,
                    &fixture.policy,
                    &control(),
                    &mut |stage| if stage == FaultStage::GenerationInstalled {
                        Err(StoreError::Injected(stage))
                    } else {
                        Ok(())
                    }
                )
                .is_err()
        );
        assert!(matches!(
            fixture.store.recover(&control()),
            Err(StoreError::Uninitialized)
        ));
        stdfs::write(fixture.path.join(".tmp-current-crash"), b"garbage").unwrap();
        fixture.initialize();
        assert_eq!(
            fixture.store.recover(&control()).unwrap().revision(),
            fixture.snapshot.revision()
        );
    }

    #[test]
    fn cancellation_at_all_precommit_stages_keeps_old_state() {
        for stage in [
            FaultStage::GenerationCreated,
            FaultStage::GenerationWritten,
            FaultStage::GenerationSynced,
            FaultStage::GenerationInstalled,
            FaultStage::GenerationDirectorySynced,
            FaultStage::PointerCreated,
            FaultStage::PointerWritten,
            FaultStage::PointerSynced,
            FaultStage::BeforePublish,
        ] {
            let fixture = Fixture::new();
            fixture.initialize();
            let cancelled = AtomicBool::new(false);
            let control = Control {
                cancelled: &cancelled,
                deadline: Instant::now() + Duration::from_secs(30),
            };
            let result =
                fixture
                    .store
                    .apply_with_faults(&fixture.preview(), &control, &mut |current| {
                        if current == stage {
                            cancelled.store(true, Ordering::Release);
                        }
                        Ok(())
                    });
            assert!(matches!(
                result,
                Err(StoreError::Transaction(TransactionError::Cancelled))
            ));
            cancelled.store(false, Ordering::Release);
            assert_eq!(
                fixture.store.read(&control).unwrap().revision(),
                fixture.snapshot.revision()
            );
        }
    }

    #[test]
    fn rejects_deadline_and_lock_contention_without_waiting() {
        let fixture = Fixture::new();
        fixture.initialize();
        let mut expired = control();
        expired.deadline = Instant::now();
        assert!(matches!(
            fixture.store.read(&expired),
            Err(StoreError::Transaction(TransactionError::Deadline))
        ));
        let _lock = fixture.store.lock(true, &control()).unwrap();
        assert!(matches!(
            fixture.store.read(&control()),
            Err(StoreError::Busy)
        ));
        assert!(matches!(
            fixture.store.apply(&fixture.preview(), &control()),
            Err(StoreError::Busy)
        ));
    }

    #[test]
    fn external_changes_are_detected_at_apply_and_publication() {
        let fixture = Fixture::new();
        fixture.initialize();
        let preview = fixture.preview();
        let pointer = fixture.path.join("CURRENT");
        assert!(
            fixture
                .store
                .apply_with_faults(&preview, &control(), &mut |stage| {
                    if stage == FaultStage::BeforePublish {
                        stdfs::set_permissions(&pointer, stdfs::Permissions::from_mode(0o600))
                            .unwrap();
                    }
                    Ok(())
                })
                .is_err()
        );
        assert_eq!(
            stdfs::read_to_string(&pointer).unwrap().trim(),
            fixture.snapshot.revision()
        );
        stdfs::set_permissions(&pointer, stdfs::Permissions::from_mode(0o400)).unwrap();
        let generation = fixture
            .path
            .join(format!("gen-{}", fixture.snapshot.revision()));
        stdfs::set_permissions(&generation, stdfs::Permissions::from_mode(0o600)).unwrap();
        let mut bytes = stdfs::read(&generation).unwrap();
        let last = bytes.len() - 1;
        bytes[last] ^= 1;
        stdfs::write(&generation, bytes).unwrap();
        stdfs::set_permissions(&generation, stdfs::Permissions::from_mode(0o400)).unwrap();
        assert!(fixture.store.apply(&preview, &control()).is_err());
        assert!(fixture.store.recover(&control()).is_err());
    }

    #[test]
    fn staged_generation_tampering_does_not_publish() {
        let fixture = Fixture::new();
        fixture.initialize();
        let preview = fixture.preview();
        let staged = fixture
            .path
            .join(format!("gen-{}", preview.result().revision()));
        assert!(
            fixture
                .store
                .apply_with_faults(&preview, &control(), &mut |stage| {
                    if stage == FaultStage::BeforePublish {
                        stdfs::set_permissions(&staged, stdfs::Permissions::from_mode(0o600))
                            .unwrap();
                        stdfs::write(&staged, b"corrupt").unwrap();
                        stdfs::set_permissions(&staged, stdfs::Permissions::from_mode(0o400))
                            .unwrap();
                    }
                    Ok(())
                })
                .is_err()
        );
        assert_eq!(
            fixture.store.read(&control()).unwrap().revision(),
            fixture.snapshot.revision()
        );
    }

    #[test]
    fn rejects_symlinks_hardlinks_wrong_permissions_and_nonregular_files() {
        let fixture = Fixture::new();
        fixture.initialize();
        let alias = fixture.path.join("alias");
        symlink(&fixture.path, &alias).unwrap();
        assert!(
            GenerationStore::open(&alias, ProjectLimits::default(), Limits::default()).is_err()
        );
        let original = fixture.path.join("CURRENT");
        let moved = fixture.path.join("saved");
        stdfs::rename(&original, &moved).unwrap();
        symlink(&moved, &original).unwrap();
        assert!(fixture.store.read(&control()).is_err());
        stdfs::remove_file(&original).unwrap();
        stdfs::hard_link(&moved, &original).unwrap();
        assert!(fixture.store.read(&control()).is_err());
        stdfs::remove_file(&original).unwrap();
        stdfs::create_dir(&original).unwrap();
        assert!(fixture.store.read(&control()).is_err());
        stdfs::remove_dir(&original).unwrap();
        stdfs::rename(&moved, &original).unwrap();
        stdfs::set_permissions(&fixture.path, stdfs::Permissions::from_mode(0o750)).unwrap();
        assert!(fixture.store.apply(&fixture.preview(), &control()).is_err());
        assert!(
            GenerationStore::open(&fixture.path, ProjectLimits::default(), Limits::default())
                .is_err()
        );
    }

    #[test]
    fn malformed_pointer_and_bounded_generation_fail_closed() {
        let fixture = Fixture::new();
        fixture.initialize();
        let pointer = fixture.path.join("CURRENT");
        stdfs::set_permissions(&pointer, stdfs::Permissions::from_mode(0o600)).unwrap();
        stdfs::write(&pointer, b"../outside\n").unwrap();
        stdfs::set_permissions(&pointer, stdfs::Permissions::from_mode(0o400)).unwrap();
        assert!(fixture.store.read(&control()).is_err());
        let bytes = encode(&fixture.snapshot);
        for length in 0..bytes.len() {
            assert!(
                decode(
                    &bytes[..length],
                    ProjectLimits::default(),
                    Limits::default(),
                    &control()
                )
                .is_err()
            );
        }
        let mut trailing = bytes.clone();
        trailing.push(0);
        assert!(
            decode(
                &trailing,
                ProjectLimits::default(),
                Limits::default(),
                &control()
            )
            .is_err()
        );
        let limits = ProjectLimits {
            maximum_files: 1,
            ..ProjectLimits::default()
        };
        assert!(decode(&bytes, limits, Limits::default(), &control()).is_err());
    }

    #[test]
    fn secure_import_is_bounded_and_preserves_mode() {
        let fixture = Fixture::new();
        let nested = fixture.path.join("authored");
        stdfs::create_dir(&nested).unwrap();
        let file = nested.join("fixture.arc");
        stdfs::write(&file, b"name fixture\r\n").unwrap();
        stdfs::set_permissions(&file, stdfs::Permissions::from_mode(0o640)).unwrap();
        let import = |path, limits| {
            read_source_file(
                &fixture.path,
                path,
                SourceId::new("fixture:import").unwrap(),
                limits,
                &control(),
            )
        };
        let read = import("authored/fixture.arc", Limits::default()).unwrap();
        assert_eq!(read.mode, 0o640);
        assert_eq!(read.document.source_bytes(), b"name fixture\r\n");
        assert!(import("../outside", Limits::default()).is_err());
        assert!(
            import(
                "authored/fixture.arc",
                Limits {
                    maximum_file_bytes: 1,
                    ..Limits::default()
                }
            )
            .is_err()
        );
        symlink(&nested, fixture.path.join("link")).unwrap();
        assert!(import("link/fixture.arc", Limits::default()).is_err());
        symlink(&file, nested.join("link.arc")).unwrap();
        assert!(import("authored/link.arc", Limits::default()).is_err());
    }
}
