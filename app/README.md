# bcflow Streamlit app

A local-first interface for the existing Python CLI, inspired by the thin
UI/service structure in `swat-preprocessing`. The app is a sibling of `src/`:

```text
app/
  Home.py              # Navigation and application shell
  launch.py            # Source-checkout launcher; keeps workspace cwd unchanged
  runtime.py           # Session drafts and the shared local job manager
  app_pages/           # Thin Configure, Run & monitor, and Results scripts
  ui/                  # Native Streamlit views
  services/            # Config I/O, subprocess jobs, and artifact readers; no Streamlit
  .streamlit/          # Native theme and localhost defaults
src/bcflow/             # Scientific backend; never imports app
learning/rust-desktop/  # Preserved source-only Rust learning snapshot
```

The duplicate `desktop/` folder was removed; the Rust learning snapshot is the
retained copy. This Streamlit frontend is currently run from a source checkout;
the backend wheel does not bundle the root `app/`.

## Start locally

Use Python **3.14+**. Run from the repository root:

```sh
uv sync --extra web
uv run --extra web python -m app.launch
```

Open **http://localhost:8501**. The default address is `127.0.0.1`; this launcher
is headless (does not automatically open a browser). To select another port:

```sh
uv run --extra web python -m app.launch --server.port=8502
```

Direct Streamlit invocation also works:

```sh
uv run --extra web streamlit run app/Home.py --server.address=127.0.0.1
```

On native Windows use PowerShell and a Windows Python environment, not a Linux
virtualenv copied from WSL. To select a separate environment:

```powershell
$env:UV_PROJECT_ENVIRONMENT = '.venv-windows'
uv sync --extra web
uv run --extra web python -m app.launch
```

The optional `web` extra installs the tested Streamlit API generation (1.67+).
The command-line backend remains usable without installing Streamlit.

## Workflow

### Configure

1. Select a shipped example or enter a YAML/JSON config's local path.
2. Choose **Open config**. Paths resolve against that file's directory, not the
   app directory or the job's temporary snapshot directory.
3. Edit the draft. The original is not saved automatically. Switching files
   with an unsaved draft requires explicit permission to discard it.
4. Select **Validate config**. Validation runs in a child process; the UI stays
   responsive. Errors and warnings appear in the monitor.
5. Return to Configure after success. **Run correction** is enabled only when
   the exact resolved draft matches a completed, successful validation.
   Editing the draft invalidates that validation.

Each job gets an immutable resolved `config.json`. Later edits cannot change
an active run. YAML dates are normalized to ISO text for JSON IPC.

**Save copy** and **Download resolved JSON** are explicit actions. Copies have
absolute paths and normalized formatting; YAML comments are not retained. Save
copy refuses the original source path and does not overwrite existing copies
unless explicitly allowed. This is syntax checking, not a substitute for backend
validation. Dataset inspection also runs as a background job.

### Run & monitor

The UI polls the background manager using a timed Streamlit fragment. It shows
variable/stage, elapsed time, block/cell progress, worker limits, QA, diagnostic
logs, and terminal statuses. Cell percentage is not total runtime percentage.
Parallel workers report completion at batch boundaries.

**Request cancellation** creates the CLI's cancel file. The UI stays in
`cancelling` until the process confirms the outcome. Cancellation may wait for a
native operation or Dask batch; already completed variables are preserved by the
backend. A result is never marked successful merely because the process exited:
the manager checks the final JSON protocol and matching exit status.

One process is allowed at a time across **all browser tabs/sessions of this
local app**. Ordinary reruns and browser refreshes do not restart it. The editor
draft is session-local and can be lost on a browser refresh. Live jobs are not
resumed or reattached after a server restart. Server shutdown requests cancellation
and may force-stop remaining owned processes after a short grace period; always
wait for normal completion if saved results are important.

Job snapshots, bounded stdout/stderr captures, and status metadata live under:

```text
<workspace>/outputs/.web-jobs/<job-id>/
```

In-memory event/log histories and disk captures are bounded. The UI keeps recent
jobs in memory; older output manifests can still be opened from Results. These
local files may contain paths and diagnostics: treat them as private workspace
data and remove them manually when no longer needed.

### Results

Browse an output folder or enter a saved `manifest.json` path. The app displays
variable statuses, including failed/cancelled/skipped, QA counts and filters,
small JSON downloads, declared artifact paths, and evaluation previews.

Evaluation data is limited to 50,000 rows; charts show at most 100 locations for
one selected period and metric. A truncation warning identifies limited previews.
The app does not load full NetCDF cubes or upload large scientific datasets into
the browser. Artifact readers reject paths escaping the run directory.

## Scope and security

This is a **personal/local tool**, not a hosted multi-user service. Browser paths
refer to the server's filesystem. It has no authentication, per-user job isolation,
or general filesystem sandbox. Do not bind it to a public network or use it for
untrusted users/configurations without adding those controls. The local shared-job
model is intentional; it is not a claim of multi-tenant safety.

The backend only launches with the current environment's Python executable and
explicit argument lists, not shell command strings. Background workers never call
Streamlit APIs, and backend code never imports the frontend.

## Tests

```sh
uv run --extra web python -m pytest tests/test_web_config.py tests/test_web_results.py tests/test_web_runner.py tests/test_web_launch.py -q
uv run --extra web python -m pytest tests/test_web_app.py tests/test_web_results_ui.py -q
```

Service tests use synthetic files/processes and include a real toy backend run.
AppTest covers navigation, drafts, stale validation, run gating, cancellation
requests, QA/results, previews, and downloads. Tests cache Streamlit's unchanged
installed-component scan once; this avoids repeatedly stat-ing unrelated package
files on slow/WSL filesystems without replacing the actual discovery result.

A real local Streamlit agent-API development check has also exercised opening a
config, validation, a completed toy correction, results rendering, and cancellation
of a real gridded run. Temporary smoke servers were stopped afterward; the agent
API is not enabled in committed server configuration. This is runtime verification,
not pixel-level browser visual testing.
