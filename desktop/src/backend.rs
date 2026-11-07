//! Bounded, shell-free discovery of the Python bcflow backend.

use std::io::{self, Read};
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use thiserror::Error;

const OUTPUT_LIMIT: usize = 256 * 1024;
const POLL_INTERVAL: Duration = Duration::from_millis(5);
const READER_CLEANUP_GRACE: Duration = Duration::from_millis(100);
const VERSION_COMMAND: &str = "--version";
const METHODS_COMMAND: &str = "methods --json";

/// Interpreter, module search directory, and per-command time budget.
#[derive(Clone, Debug)]
pub struct BackendConfig {
    pub python: PathBuf,
    pub working_dir: PathBuf,
    pub timeout: Duration,
}

/// Backend identity, available methods, and warnings written to stderr.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackendInfo {
    pub version: String,
    pub methods: Vec<MethodInfo>,
    pub diagnostics: String,
}

/// One entry from `bcflow methods --json`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MethodInfo {
    pub name: String,
    #[serde(rename = "class")]
    pub class_name: String,
    pub summary: String,
}

/// Failures retain command context and bounded, lossily decoded stderr.
#[derive(Debug, Error)]
pub enum BackendError {
    #[error("invalid backend working directory {path:?}: {source}")]
    InvalidWorkingDirectory {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
    #[error("could not start {python:?} -m bcflow {command} in {working_dir:?}: {source}")]
    Spawn {
        python: PathBuf,
        working_dir: PathBuf,
        command: &'static str,
        #[source]
        source: io::Error,
    },
    #[error("bcflow {command}: {operation}: {source}; stderr: {stderr}")]
    Io {
        command: &'static str,
        operation: &'static str,
        #[source]
        source: io::Error,
        stderr: String,
    },
    #[error("bcflow {command} timed out after {timeout:?}; stderr: {stderr}")]
    Timeout {
        command: &'static str,
        timeout: Duration,
        stderr: String,
    },
    #[error("bcflow {command} exceeded the {limit}-byte {stream} limit; stderr: {stderr}")]
    OutputLimit {
        command: &'static str,
        stream: &'static str,
        limit: usize,
        stderr: String,
    },
    #[error("bcflow {command} exited with {status}; stderr: {stderr}")]
    NonZeroExit {
        command: &'static str,
        status: ExitStatus,
        stderr: String,
    },
    #[error("bcflow {command} stdout is not UTF-8: {source}; stderr: {stderr}")]
    InvalidUtf8 {
        command: &'static str,
        #[source]
        source: std::str::Utf8Error,
        stderr: String,
    },
    #[error(
        "invalid bcflow version output {output:?}; expected 'bcflow <version>'; stderr: {stderr}"
    )]
    InvalidVersion { output: String, stderr: String },
    #[error("invalid bcflow methods JSON: {source}; stderr: {stderr}")]
    InvalidJson {
        #[source]
        source: serde_json::Error,
        stderr: String,
    },
}

/// Run `python -m bcflow --version`, then `python -m bcflow methods --json`.
///
/// Each command has its own timeout and independent 256 KiB stdout/stderr
/// limits. No shell or process-wide environment changes are used. Successful
/// stderr is retained in `diagnostics`, labeled by command.
///
/// This probes only the direct backend child; these discovery commands must
/// not spawn workers. The direct child is killed and reaped on failure. Reader
/// threads are joined on ordinary paths, but cleanup waits at most a short
/// grace period for them: if an unexpected descendant inherits a pipe, its
/// reader is detached rather than making a timeout block indefinitely. Such a
/// reader exits when the inherited pipe closes (or exceeds the output limit).
///
/// # Errors
/// Returns a typed error for an invalid directory, process/pipe failure,
/// timeout, excessive output, nonzero exit, or invalid backend response.
pub fn probe(config: &BackendConfig) -> Result<BackendInfo, BackendError> {
    let metadata = std::fs::metadata(&config.working_dir).map_err(|source| {
        BackendError::InvalidWorkingDirectory {
            path: config.working_dir.clone(),
            source,
        }
    })?;
    if !metadata.is_dir() {
        return Err(BackendError::InvalidWorkingDirectory {
            path: config.working_dir.clone(),
            source: io::Error::new(io::ErrorKind::InvalidInput, "not a directory"),
        });
    }

    let version_output = run_command(config, VERSION_COMMAND, &["--version"])?;
    let version = parse_version(&version_output.stdout, &version_output.stderr)?;
    let methods_output = run_command(config, METHODS_COMMAND, &["methods", "--json"])?;
    let methods = parse_methods(&methods_output.stdout, &methods_output.stderr)?;
    let diagnostics = [
        (VERSION_COMMAND, version_output.stderr),
        (METHODS_COMMAND, methods_output.stderr),
    ]
    .into_iter()
    .filter(|(_, stderr)| !stderr.trim().is_empty())
    .map(|(command, stderr)| format!("bcflow {command}:\n{}", stderr.trim()))
    .collect::<Vec<_>>()
    .join("\n\n");

    Ok(BackendInfo {
        version,
        methods,
        diagnostics,
    })
}

fn decode_stdout<'a>(
    stdout: &'a [u8],
    command: &'static str,
    stderr: &str,
) -> Result<&'a str, BackendError> {
    std::str::from_utf8(stdout).map_err(|source| BackendError::InvalidUtf8 {
        command,
        source,
        stderr: stderr.to_owned(),
    })
}

fn parse_version(stdout: &[u8], stderr: &str) -> Result<String, BackendError> {
    let output = decode_stdout(stdout, VERSION_COMMAND, stderr)?;
    // Strip only trailing line endings/whitespace; leading junk is not a version.
    let version = output.trim_end();
    if !version
        .strip_prefix("bcflow ")
        .is_some_and(|value| !value.trim().is_empty() && !value.contains(['\r', '\n']))
    {
        return Err(BackendError::InvalidVersion {
            output: output.to_owned(),
            stderr: stderr.to_owned(),
        });
    }
    Ok(version.to_owned())
}

fn parse_methods(stdout: &[u8], stderr: &str) -> Result<Vec<MethodInfo>, BackendError> {
    let json = decode_stdout(stdout, METHODS_COMMAND, stderr)?;
    serde_json::from_str(json).map_err(|source| BackendError::InvalidJson {
        source,
        stderr: stderr.to_owned(),
    })
}

struct CommandOutput {
    stdout: Vec<u8>,
    stderr: String,
}

#[derive(Default)]
struct Capture {
    bytes: Vec<u8>,
    exceeded: bool,
    error: Option<io::Error>,
    finished: bool,
}

struct PipeReader {
    stream: &'static str,
    capture: Arc<Mutex<Capture>>,
    handle: JoinHandle<()>,
}

impl PipeReader {
    fn start(pipe: impl Read + Send + 'static, stream: &'static str) -> io::Result<Self> {
        let capture = Arc::new(Mutex::new(Capture::default()));
        let shared = Arc::clone(&capture);
        let handle = thread::Builder::new()
            .name(format!("bcflow-{stream}"))
            .spawn(move || {
                drain_pipe(pipe, &shared);
                lock_capture(&shared).finished = true;
            })?;
        Ok(Self {
            stream,
            capture,
            handle,
        })
    }

    fn failure(&self, command: &'static str, stderr: &str) -> Option<BackendError> {
        let mut capture = lock_capture(&self.capture);
        if capture.exceeded {
            return Some(BackendError::OutputLimit {
                command,
                stream: self.stream,
                limit: OUTPUT_LIMIT,
                stderr: stderr.to_owned(),
            });
        }
        if let Some(source) = capture.error.take() {
            return Some(BackendError::Io {
                command,
                operation: self.stream,
                source,
                stderr: stderr.to_owned(),
            });
        }
        if self.handle.is_finished() && !capture.finished {
            return Some(BackendError::Io {
                command,
                operation: self.stream,
                source: io::Error::other("pipe reader panicked"),
                stderr: stderr.to_owned(),
            });
        }
        None
    }
}

fn lock_capture(capture: &Mutex<Capture>) -> MutexGuard<'_, Capture> {
    // A poisoned capture can still provide its bounded diagnostic prefix.
    capture
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn drain_pipe(mut pipe: impl Read, capture: &Mutex<Capture>) {
    let mut buffer = [0_u8; 8192];
    loop {
        match pipe.read(&mut buffer) {
            Ok(0) => return,
            Ok(count) => {
                let mut capture = lock_capture(capture);
                let remaining = OUTPUT_LIMIT - capture.bytes.len();
                capture
                    .bytes
                    .extend_from_slice(&buffer[..count.min(remaining)]);
                if count > remaining {
                    capture.exceeded = true;
                    return;
                }
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => {
                lock_capture(capture).error = Some(error);
                return;
            }
        }
    }
}

/// Own the child immediately after spawning, including partial reader setup.
struct RunningCommand {
    child: Child,
    readers: Vec<PipeReader>,
}

impl RunningCommand {
    fn stderr(&self) -> String {
        self.readers
            .iter()
            .find(|reader| reader.stream == "stderr")
            .map(|reader| {
                String::from_utf8_lossy(&lock_capture(&reader.capture).bytes).into_owned()
            })
            .unwrap_or_default()
    }
}

impl Drop for RunningCommand {
    fn drop(&mut self) {
        // wait() also reaps an already-exited child. Readers remain active while
        // the child is stopped, so full pipes cannot deadlock process cleanup.
        if !matches!(self.child.try_wait(), Ok(Some(_))) {
            let _ = self.child.kill();
        }
        let _ = self.child.wait();

        let started = Instant::now();
        while self
            .readers
            .iter()
            .any(|reader| !reader.handle.is_finished())
            && started.elapsed() < READER_CLEANUP_GRACE
        {
            thread::sleep(Duration::from_millis(1));
        }
        for reader in self.readers.drain(..) {
            if reader.handle.is_finished() {
                let _ = reader.handle.join();
            }
            // Dropping an unfinished handle detaches it, never blocking on a
            // descendant that incorrectly inherited a discovery command's pipe.
        }
    }
}

fn run_command(
    config: &BackendConfig,
    command_name: &'static str,
    args: &[&str],
) -> Result<CommandOutput, BackendError> {
    let mut command = Command::new(&config.python);
    command.arg("-m").arg("bcflow").args(args);
    run_process(config, command_name, &mut command)
}

fn run_process(
    config: &BackendConfig,
    command_name: &'static str,
    command: &mut Command,
) -> Result<CommandOutput, BackendError> {
    command
        .current_dir(&config.working_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let started = Instant::now();
    let child = command.spawn().map_err(|source| BackendError::Spawn {
        python: config.python.clone(),
        working_dir: config.working_dir.clone(),
        command: command_name,
        source,
    })?;
    let mut running = RunningCommand {
        child,
        readers: Vec::with_capacity(2),
    };
    let stdout = running
        .child
        .stdout
        .take()
        .ok_or_else(|| BackendError::Io {
            command: command_name,
            operation: "open stdout pipe",
            source: io::Error::other("stdout pipe missing"),
            stderr: String::new(),
        })?;
    let stderr = running
        .child
        .stderr
        .take()
        .ok_or_else(|| BackendError::Io {
            command: command_name,
            operation: "open stderr pipe",
            source: io::Error::other("stderr pipe missing"),
            stderr: String::new(),
        })?;
    running
        .readers
        .push(
            PipeReader::start(stdout, "stdout").map_err(|source| BackendError::Io {
                command: command_name,
                operation: "start stdout reader",
                source,
                stderr: String::new(),
            })?,
        );
    running
        .readers
        .push(
            PipeReader::start(stderr, "stderr").map_err(|source| BackendError::Io {
                command: command_name,
                operation: "start stderr reader",
                source,
                stderr: String::new(),
            })?,
        );

    loop {
        let stderr = running.stderr();
        for reader in &running.readers {
            if let Some(error) = reader.failure(command_name, &stderr) {
                return Err(error);
            }
        }
        let status = running
            .child
            .try_wait()
            .map_err(|source| BackendError::Io {
                command: command_name,
                operation: "poll child process",
                source,
                stderr: stderr.clone(),
            })?;
        if let Some(status) = status {
            if running
                .readers
                .iter()
                .all(|reader| reader.handle.is_finished())
            {
                // Recheck after completion: the initial snapshot may precede
                // the readers' final writes or an output-limit violation.
                let stderr = running.stderr();
                for reader in &running.readers {
                    if let Some(error) = reader.failure(command_name, &stderr) {
                        return Err(error);
                    }
                }
                let stdout = running
                    .readers
                    .iter()
                    .find(|reader| reader.stream == "stdout")
                    .map(|reader| std::mem::take(&mut lock_capture(&reader.capture).bytes))
                    .unwrap_or_default();
                if !status.success() {
                    return Err(BackendError::NonZeroExit {
                        command: command_name,
                        status,
                        stderr,
                    });
                }
                return Ok(CommandOutput { stdout, stderr });
            }
        }
        let elapsed = started.elapsed();
        if elapsed >= config.timeout {
            return Err(BackendError::Timeout {
                command: command_name,
                timeout: config.timeout,
                stderr: running.stderr(),
            });
        }
        thread::sleep(POLL_INTERVAL.min(config.timeout.saturating_sub(elapsed)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    // Reuse the Rust test executable so process tests need neither Python nor
    // platform-specific shell scripts. Environment changes affect only children.
    fn fixture(mode: &str, timeout: Duration) -> Result<CommandOutput, BackendError> {
        let directory = tempfile::tempdir().unwrap();
        let executable = std::env::current_exe().unwrap();
        let config = BackendConfig {
            python: executable.clone(),
            working_dir: directory.path().to_owned(),
            timeout,
        };
        let mut command = Command::new(executable);
        // module_path! includes the crate name; libtest's test names do not.
        let fixture_path = concat!(module_path!(), "::subprocess_fixture");
        let (_, fixture_name) = fixture_path.split_once("::").unwrap();
        command.args(["--exact", fixture_name, "--ignored", "--nocapture"]);
        command.env("BCFLOW_BACKEND_TEST_MODE", mode);
        run_process(&config, VERSION_COMMAND, &mut command)
    }

    #[test]
    #[ignore = "subprocess fixture, invoked by the process tests"]
    fn subprocess_fixture() {
        let mode = std::env::var("BCFLOW_BACKEND_TEST_MODE").unwrap_or_default();
        match mode.as_str() {
            "both-pipes" => {
                io::stdout().write_all(&vec![b'o'; 128 * 1024]).unwrap();
                io::stderr().write_all(&vec![b'e'; 128 * 1024]).unwrap();
                io::stdout().write_all(b"stdout end").unwrap();
                io::stderr().write_all(b"stderr end").unwrap();
            }
            "nonzero" => {
                io::stderr().write_all(b"backend fixture failed").unwrap();
                std::process::exit(7);
            }
            "invalid-stderr" => {
                io::stderr().write_all(b"warning: \xff").unwrap();
            }
            "timeout" => {
                io::stderr().write_all(b"backend is waiting").unwrap();
                io::stderr().flush().unwrap();
                thread::sleep(Duration::from_secs(30));
            }
            "stdout-limit" | "stderr-limit" => {
                let bytes = vec![b'x'; OUTPUT_LIMIT + 8192];
                if mode == "stdout-limit" {
                    let _ = io::stdout().write_all(&bytes);
                } else {
                    let _ = io::stderr().write_all(&bytes);
                }
                thread::sleep(Duration::from_secs(30));
            }
            _ => panic!("unknown subprocess fixture mode: {mode:?}"),
        }
        // Skip test-harness output following the fixture's own output.
        std::process::exit(0);
    }

    #[test]
    fn process_drains_both_pipes_concurrently_without_losing_final_bytes() {
        let output = fixture("both-pipes", Duration::from_secs(5)).unwrap();
        assert!(output.stdout.ends_with(b"stdout end"));
        assert!(output.stderr.ends_with("stderr end"));
    }

    #[test]
    fn process_retains_stderr_and_exit_status_on_failure() {
        assert!(matches!(
            fixture("nonzero", Duration::from_secs(5)),
            Err(BackendError::NonZeroExit { status, stderr, .. })
                if status.code() == Some(7) && stderr.contains("backend fixture failed")
        ));
    }

    #[test]
    fn process_lossily_decodes_diagnostic_stderr() {
        let output = fixture("invalid-stderr", Duration::from_secs(5)).unwrap();
        assert_eq!(output.stderr, "warning: \u{fffd}");
    }

    #[test]
    fn process_times_out_and_cleans_up_promptly() {
        let started = Instant::now();
        assert!(matches!(
            fixture("timeout", Duration::from_millis(300)),
            Err(BackendError::Timeout { stderr, .. }) if stderr.contains("backend is waiting")
        ));
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn process_enforces_each_output_limit_independently() {
        for (mode, expected_stream) in [("stdout-limit", "stdout"), ("stderr-limit", "stderr")] {
            let started = Instant::now();
            assert!(matches!(
                fixture(mode, Duration::from_secs(5)),
                Err(BackendError::OutputLimit { stream, limit: OUTPUT_LIMIT, stderr, .. })
                    if stream == expected_stream && stderr.len() <= OUTPUT_LIMIT
            ));
            assert!(started.elapsed() < Duration::from_secs(5));
        }
    }

    #[test]
    fn version_accepts_backend_prefix_and_crlf() {
        assert_eq!(
            parse_version(b"bcflow 1.2.3\r\n", "").unwrap(),
            "bcflow 1.2.3"
        );
    }

    #[test]
    fn version_rejects_missing_or_empty_prefix_and_extra_lines() {
        for output in [
            "",
            "bcflow ",
            "bcflow",
            "other 1.0",
            " bcflow 1.0",
            "bcflow 1\nnoise",
        ] {
            assert!(
                matches!(
                    parse_version(output.as_bytes(), "warning"),
                    Err(BackendError::InvalidVersion { .. })
                ),
                "{output:?}"
            );
        }
    }

    #[test]
    fn version_rejects_invalid_utf8_and_retains_stderr() {
        assert!(
            matches!(parse_version(b"bcflow \xff", "warning"), Err(BackendError::InvalidUtf8 { stderr, .. }) if stderr == "warning")
        );
    }

    #[test]
    fn methods_maps_class_field_and_round_trips() {
        let json = br#"[{"name":"qm","class":"QuantileMapping","summary":"Adjust quantiles"}]"#;
        let methods = parse_methods(json, "").unwrap();
        assert_eq!(methods[0].class_name, "QuantileMapping");
        assert_eq!(
            serde_json::to_value(methods).unwrap(),
            serde_json::from_slice::<serde_json::Value>(json).unwrap()
        );
    }

    #[test]
    fn methods_accepts_empty_array() {
        assert!(parse_methods(b"[]", "").unwrap().is_empty());
    }

    #[test]
    fn methods_rejects_invalid_json_and_missing_fields() {
        for json in ["not json", "{}", "[{}]", "[] trailing"] {
            assert!(
                matches!(parse_methods(json.as_bytes(), "warning"), Err(BackendError::InvalidJson { stderr, .. }) if stderr == "warning")
            );
        }
    }

    #[test]
    fn methods_rejects_invalid_utf8_before_json() {
        assert!(matches!(
            parse_methods(b"[\xff]", "warning"),
            Err(BackendError::InvalidUtf8 {
                command: METHODS_COMMAND,
                ..
            })
        ));
    }

    #[test]
    fn pipe_capture_accepts_exact_limit() {
        let capture = Mutex::new(Capture::default());
        drain_pipe(io::Cursor::new(vec![b'x'; OUTPUT_LIMIT]), &capture);
        let capture = lock_capture(&capture);
        assert_eq!(capture.bytes.len(), OUTPUT_LIMIT);
        assert!(!capture.exceeded);
    }

    #[test]
    fn pipe_capture_bounds_excess_output() {
        let capture = Mutex::new(Capture::default());
        drain_pipe(io::Cursor::new(vec![b'x'; OUTPUT_LIMIT + 1]), &capture);
        let capture = lock_capture(&capture);
        assert_eq!(capture.bytes.len(), OUTPUT_LIMIT);
        assert!(capture.exceeded);
    }

    #[test]
    fn probe_reports_missing_interpreter() {
        let directory = tempfile::tempdir().unwrap();
        let config = BackendConfig {
            python: directory.path().join("nonexistent-python"),
            working_dir: directory.path().to_owned(),
            timeout: Duration::from_secs(1),
        };
        assert!(matches!(probe(&config), Err(BackendError::Spawn { .. })));
    }

    #[test]
    fn probe_reports_invalid_working_directory_before_spawn() {
        let directory = tempfile::tempdir().unwrap();
        let config = BackendConfig {
            python: directory.path().join("nonexistent-python"),
            working_dir: directory.path().join("nonexistent-directory"),
            timeout: Duration::from_secs(1),
        };
        assert!(matches!(
            probe(&config),
            Err(BackendError::InvalidWorkingDirectory { .. })
        ));
    }
}
