// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT
#![cfg(target_os = "linux")]

use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

static NEXT_FIXTURE: AtomicUsize = AtomicUsize::new(0);

fn git(root: &Path, arguments: &[&str]) -> String {
    let output = Command::new("git")
        .args(arguments)
        .current_dir(root)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_TERMINAL_PROMPT", "0")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "fixture Git command failed: {arguments:?}"
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}

fn commit(root: &Path) {
    git(
        root,
        &[
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "-m",
            "synthetic fixture",
        ],
    );
}

struct Fixture {
    directory: PathBuf,
    root: PathBuf,
    config: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let directory = std::env::temp_dir().join(format!(
            "mcp-submodule-{}-{}",
            std::process::id(),
            NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
        ));
        let root = directory.join("source");
        fs::create_dir_all(&root).unwrap();
        git(&root, &["init", "-b", "main"]);
        git(
            &root,
            &[
                "remote",
                "add",
                "origin",
                "https://github.com/atrinik/content.git",
            ],
        );
        fs::write(
            root.join("content.arc"),
            "Object synthetic\nname Synthetic\nend\n",
        )
        .unwrap();
        fs::write(root.join("nested"), "ordinary tracked placeholder\n").unwrap();
        fs::write(
            root.join(".gitmodules"),
            "[submodule \"nested\"]\n\tpath = nested\n\turl = ./synthetic-source\n",
        )
        .unwrap();
        git(&root, &["add", "content.arc", "nested", ".gitmodules"]);
        commit(&root);
        let fixture = Self {
            config: directory.join("config.json"),
            directory,
            root,
        };
        fixture.write_config();
        fixture
    }
    fn write_config(&self) {
        let commit = git(&self.root, &["rev-parse", "HEAD"]);
        let value = json!({"snapshots":[{"root":self.root,"identity":{
            "repository":"atrinik/content","branch":"refs/heads/main","commit":commit,
            "main_base_commit":commit,"worktree":"synthetic","source_role":"main",
            "view_role":"replacement","dirty_fingerprint":null,"authorization":"synthetic",
            "manifest":"synthetic","profile":"synthetic","registry":"synthetic",
            "schema_version":1,"provider_version":atrinik_content_mcp::SCHEMA_VERSION
        },"files":[{"path":"content.arc","domain":"archetype","namespace":"synthetic",
            "rules":{"name":{"kind":"label"}}}]}]});
        fs::write(&self.config, serde_json::to_vec(&value).unwrap()).unwrap();
    }
    fn install_filtered_gitlink(&self, commit_parent: bool) {
        let nested = self.root.join("nested");
        fs::remove_file(&nested).unwrap();
        fs::create_dir(&nested).unwrap();
        git(&nested, &["init", "-b", "main"]);
        fs::write(nested.join("file.arc"), "original\n").unwrap();
        git(&nested, &["add", "file.arc"]);
        commit(&nested);
        let revision = git(&nested, &["rev-parse", "HEAD"]);
        git(
            &self.root,
            &[
                "update-index",
                "--add",
                "--cacheinfo",
                &format!("160000,{revision},nested"),
            ],
        );
        git(&self.root, &["config", "submodule.nested.active", "true"]);
        if commit_parent {
            commit(&self.root);
            self.write_config();
        }
        git(
            &nested,
            &[
                "config",
                "filter.fixture.clean",
                "touch owned-sentinel; cat",
            ],
        );
        fs::write(nested.join(".gitattributes"), "file.arc filter=fixture\n").unwrap();
        fs::write(nested.join("file.arc"), "changed\n").unwrap();
        assert!(!git(&self.root, &["config", "--null", "--list"]).contains("filter.fixture"));
        assert!(!self.sentinel().exists());
    }
    fn sentinel(&self) -> PathBuf {
        self.root.join("nested/owned-sentinel")
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.directory).unwrap();
    }
}

struct Server {
    child: Child,
    responses: mpsc::Receiver<Value>,
    reader: Option<thread::JoinHandle<()>>,
}
impl Server {
    fn start(config: &Path) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_atrinik-content-mcp"))
            .args(["--config", config.to_str().unwrap()])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (sender, responses) = mpsc::channel();
        let reader = thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let value = serde_json::from_str(&line.unwrap()).unwrap();
                if sender.send(value).is_err() {
                    break;
                }
            }
        });
        Self {
            child,
            responses,
            reader: Some(reader),
        }
    }
    fn query(&mut self) -> Result<Value, mpsc::RecvTimeoutError> {
        let request = json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
            "_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
                "io.modelcontextprotocol/clientCapabilities":{}},
            "name":"content_query","arguments":{"selector":"synthetic","operation":"search","query":"Synthetic"}}});
        let _ = writeln!(self.child.stdin.as_mut().unwrap(), "{request}");
        self.responses.recv_timeout(Duration::from_secs(10))
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.child.stdin.take();
        let _ = self.child.kill();
        let _ = self.child.wait();
        self.reader.take().unwrap().join().unwrap();
    }
}

#[test]
fn registration_excludes_independently_configured_submodules() {
    let fixture = Fixture::new();
    fixture.install_filtered_gitlink(true);
    let mut server = Server::start(&fixture.config);
    let response = server.query().unwrap();
    assert!(
        !fixture.sentinel().exists(),
        "submodule clean filter executed during registration"
    );
    assert_eq!(
        response["result"]["structuredContent"]["records"][0]["identity"],
        "archetype:synthetic/synthetic"
    );
}

#[test]
fn query_fences_submodule_introduced_without_changing_tracked_path_names() {
    let fixture = Fixture::new();
    let mut server = Server::start(&fixture.config);
    let initial = server.query().unwrap();
    assert_eq!(
        initial["result"]["structuredContent"]["records"][0]["identity"],
        "archetype:synthetic/synthetic"
    );
    let head = git(&fixture.root, &["rev-parse", "HEAD"]);
    let tracked = git(&fixture.root, &["ls-files", "--cached", "-z"]);
    fixture.install_filtered_gitlink(false);
    assert_eq!(git(&fixture.root, &["rev-parse", "HEAD"]), head);
    assert_eq!(git(&fixture.root, &["ls-files", "--cached", "-z"]), tracked);
    let response = server.query().unwrap();
    assert!(
        !fixture.sentinel().exists(),
        "submodule clean filter executed during query fencing"
    );
    assert_eq!(response["result"]["isError"], true);
    assert_eq!(response["result"]["content"][0]["text"], "STALE_COORDINATE");
}
