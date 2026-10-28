# Codex Context Handoff

Continue a long-running Codex task in a fresh chat, with a small verified
checkpoint, the same workspace, numbered titles, and links back to its history.

[![External tests (WSL)](https://img.shields.io/github/checks-status/boxedge/codex-context-handoff/main?label=External%20tests%20%28WSL%29)](https://github.com/boxedge/codex-context-handoff/tree/test-results)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Experimental community integration for Linux/WSL.** Fully automatic mode
depends on the Codex Desktop build exposing its bundled app-tools connection.
This is not an official OpenAI extension or a universally compatible Desktop
feature. Start in ready-only mode and verify your build before enabling automatic
continuation.

## What it does

- Near **350,000 active context tokens**, asks the agent to write a concise
  checkpoint: goal, constraints, completed work, remaining work, evidence, and
  next action.
- Near **400,000**, validates that checkpoint against the latest user request
  and workspace, then initializes a fresh persistent chat in the same project.
- Has the new chat read the checkpoint and reproduce its goal, constraints and
  next action exactly before accepting the handoff.
- In automatic Desktop mode, waits for the source Stop event, then submits the
  continuation through Desktop so progress and controls stay in the app.
- Uses titles such as `Improve CI reliability · Part 1`, `· Part 2`, `· Part 3`.
  Successor prompts link to the **Original chat** and **Previous part**.
- Preserves the original chat, transcript, dirty files, credentials, and Git
  history. It does not commit, push, stash, reset, or move your workspace.

It does **not** enlarge a model's context window, change compaction settings,
guarantee perfect recall, or prove token/cost savings. Initializing a new chat
uses a model turn and consumes normal Codex usage. Choose thresholds that fit
the model and runtime you actually use; 350k/400k are defaults, not universal
recommendations.

## Prerequisites

| Requirement | Details |
| --- | --- |
| Linux or WSL2 | Uses `fcntl` locks and Linux `/proc` process identities. Native Windows and macOS are not supported. |
| Python **3.11+** | Standard library only. No `pip install` or Node package install is needed for this project. |
| Git | Used for read-only workspace identity checks. |
| Authenticated Codex CLI | `codex` must be executable from the hook environment and support the lifecycle hooks and app-server methods below. |
| Writable Codex home | Installer modifies only its hook definitions/trust entries, with backups, in the homes you select. |
| Context headroom | The source must reach your thresholds and finish initialization before normal compaction. |
| Desktop automatic mode | A compatible running Codex Desktop, installed bundled `codex-app-tools` manifest/runtime, and a genuine app-tools connection inherited by hooks. |

Required lifecycle events: `PreToolUse`, `PostToolUse`, `UserPromptSubmit`,
`Stop`, `PreCompact`. Required app-server operations include `hooks/list`,
`thread/read`, `thread/start`, `thread/name/set`, `thread/list`, and `turn/start`.
Automatic Desktop mode also requires `send_message_to_thread`,
`navigate_to_codex_page` when auto-open is enabled, and `set_thread_title` for
the original title. Bundled Desktop tools can change independently of the CLI.

The originating environment used `codex-cli 0.165.0-alpha.1.2` with Python
3.12 on WSL. Compatibility is capability-based; this version is an observation,
not a minimum-version guarantee. Linux-only ready mode is possible; fully
automatic Desktop acceptance was exercised in a Windows/WSL environment.

## Quick start

### 1. Get the code and check the runtime

```bash
git clone https://github.com/boxedge/codex-context-handoff.git
cd codex-context-handoff
python3 --version
codex --version
codex login status
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Review `scripts/` before installing. The installer backs up configuration,
registers five hook definitions, and **marks those definitions trusted** using
the hashes returned by Codex. The integration itself starts **disabled**.
Existing unrelated hooks and model settings are preserved.

### 2. Install into the correct Codex home

For `$CODEX_HOME`, or `~/.codex` when that variable is unset:

```bash
python3 scripts/install_codex_context_handoff.py
```

For Windows Codex Desktop using a WSL workspace, explicitly select the Windows
Codex home mounted in WSL. Replace `YOUR_WINDOWS_USER`:

```bash
python3 scripts/install_codex_context_handoff.py \
  --home /mnt/c/Users/YOUR_WINDOWS_USER/.codex
```

Use `--home` more than once only if you intentionally use multiple homes. On a
fresh install, the **first home** supplies bootstrap authentication/configuration
and bundled Desktop-tool discovery. Do not copy authentication files into this
repository. All selected paths must be accessible from the Linux hook runtime.

Optional locations:

```bash
python3 scripts/install_codex_context_handoff.py \
  --home "$HOME/.codex" \
  --root "$HOME/.local/share/codex-context-handoff" \
  --library "$HOME/.local/lib/codex-context-handoff"
```

The installer reports helper paths, hook metadata, and backup locations. It does
not enable handoffs or run a model task. On an initial installation, open a new
chat or reload Codex when convenient so cached hook definitions are refreshed;
do not interrupt running work just to do this.

### 3. Enable ready-only mode first

This preserves installer-generated paths and enables checkpointing while leaving
continuation to you:

```bash
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from codex_context_handoff import atomic_json, load_json
path = Path.home() / '.local/share/codex-context-handoff/settings.json'
settings = load_json(path)
settings.update(enabled=True, auto_continue=False, auto_open=False)
atomic_json(path, settings)
PY
```

If you installed with a custom `--root`, use that directory's `settings.json` in
this example and pass the same `--root` to later commands.

Work normally in a Codex chat. At the thresholds, the helper asks the agent for a
checkpoint and prepares the successor. Follow its chat link and ask it to
continue. Existing task restrictions and approval requirements still apply.

### 4. Enable fully automatic Desktop continuation

After verifying your Desktop build and hook loading, change these settings:

```bash
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from codex_context_handoff import atomic_json, load_json
path = Path.home() / '.local/share/codex-context-handoff/settings.json'
settings = load_json(path)
settings.update(enabled=True, auto_continue=True, auto_open=True,
                continuation_backend='desktop')
atomic_json(path, settings)
PY
```

Enabling automatic continuation authorizes transfer of the same task; it grants
no additional authority to the task itself. The new chat stays in the same
workspace, waits for the source stop confirmation, and continues through the
native app action without another click or message. Genuine approvals and
missing blocking input remain in Desktop's normal flow.

`CODEX_APP_TOOLS_PIPE_PATH` must be inherited from a genuine Desktop task.
An ordinary terminal may not have it. Do not fabricate a pipe path or thread
identity to force a connection. Missing Desktop tools produce a recorded failure;
the helper does not silently switch to an externally owned worker.

## Resilience and safety

- Waiting for source Stop starts no worker and has **no 180-second expiry**.
  A delayed Stop can recover an older `source_stop_not_confirmed` outcome.
- A short-lived per-handoff supervisor retries pre-dispatch worker failures up
  to three times with one- and two-second backoff. Launch failures get three
  short attempts; exhaustion is recorded for inspection.
- File locks and process start identities suppress duplicate coordinators and
  distinguish dead processes, reused PIDs, and zombies.
- Dispatch intent is persisted **before** sending. Accepted or uncertain sends
  are never blindly retried because the task may already be running.
- New source input, changed checkpoints/workspaces, running operations, or
  manual use of a newly prepared successor stop automatic recovery.
- Subagents are ignored. Completed tasks do not acquire invented remaining work.
  Checkpoint text is reference data and cannot expand permissions.

**Scheduled is not dispatched.** `waiting_for_source_stop` means registered;
`scheduled` means a coordinator was launched; `desktop_dispatched` with a native
receipt means Desktop accepted the send. None of these means the task completed.

## Configuration and status

Default private state: `~/.local/share/codex-context-handoff/`.

| Setting | Default on fresh install | Purpose |
| --- | --- | --- |
| `enabled` | `false` | Master switch for future handoffs. |
| `prepare_tokens` | `350000` | Start checkpoint preparation. |
| `rollover_tokens` | `400000` | Prepare a successor. Must exceed preparation threshold. |
| `refresh_tokens` | `20000` | Refresh checkpoint as context grows. |
| `bootstrap_timeout` | `90` | Seconds allowed for read-only initialization. |
| `auto_continue` | `false` | Submit continuation after confirmed source stop. |
| `auto_open` | `false` | Navigate to the verified successor in automatic mode. |
| `continuation_backend` | `"desktop"` | Desktop owns the task's live turn. |
| `allowed_sessions` | `[]` | Empty permits all eligible root chats; otherwise restricts to listed IDs. |
| `config_home` | First installed home | Bootstrap authentication and Desktop manifest discovery. |
| `codex_binary` | Detected executable | App-server binary. |
| `hook_entrypoint` | Generated stable loader | Keeps hook commands stable across reviewed upgrades. |

`source_stop_timeout` is retained for compatibility with direct/legacy worker
invocation; normal Stop-triggered scheduling does not wait on that timeout.
The older `"worker"` backend is retained for compatibility and is not recommended
for Desktop use because it owns a separate app-server turn.

```bash
python3 scripts/codex_context_handoff.py status --session YOUR_SOURCE_CHAT_ID
```

Inspect `sessions/<source-id>/state.json` and `continuation-worker.log` locally.
They contain private paths and task metadata: redact before sharing. See
[troubleshooting and protocol details](docs/context-handoff.md) and the
[synthetic checkpoint example](examples/checkpoint.json).

## Disable, update, or uninstall

Disable future handoffs immediately:

```bash
python3 scripts/codex_context_handoff.py disable
```

This does not cancel a turn already accepted by Desktop; use the app's normal
controls for that turn.

To update, review changes, pull the repo, run tests, then rerun the installer with
the same `--root`, `--library`, and `--home` selections. The versioned helper and
stable compatibility loader allow installed hooks to use the new version without
restarting ongoing tasks. The trust hash identifies the hook definition, not a
promise that the loader's target code is unchanged; review each update.

To uninstall, disable first, then remove only this helper's command entries from
each selected home's `hooks.json` and their matching `[hooks.state."..."]` trust
entries in `config.toml`. Backups are under the private state's `backups/`.
Do not overwrite newer unrelated edits by restoring whole files blindly. Keep
private checkpoints, original chats, and versioned helpers until no running
process or retained hook references them. Nothing prunes history automatically.

## Testing and limitations

The test suite uses temporary workspaces and recording transports; no Codex
login or real model calls are required. It includes a detached-process crash
drill: the first worker exits hard, the second sends once, and replay sends
nothing further. The GitHub Actions workflow defines a Python 3.11–3.13 matrix;
its hosted runs are currently unavailable. The badge instead shows the latest
commit status published by the external Linux/WSL test runner.

Run the committed `HEAD` in a clean temporary checkout outside GitHub Actions:

```bash
python3 scripts/run_external_tests.py
```

To publish the result, push the source commit first, authenticate `gh` with write
access to this repository, then run:

```bash
python3 scripts/run_external_tests.py --publish
```

This publishes the `external/tests` status for the exact tested commit, with a
link to a sanitized report and test log on the `test-results` branch. The badge
uses the status of current `main`; a new untested commit does not inherit an
older success. Reports identify the actual Python version used. This is a
manually invoked external check, not an automatic service or a claim that the
whole Python matrix or live Desktop rollover passed. No billing change is needed.
Only publish logs from trusted tests after reviewing them for sensitive data.

Earlier real 350k/400k Desktop rollover acceptance was performed in the
originating environment. The latest resilience path has automated crash tests,
but a full live Desktop rollover was not rerun for that patch. This repository
does not include private transcripts, checkpoint state, account configuration,
or local acceptance logs. No claim of universal Desktop compatibility is made.

For a **model-access smoke test** only (uses Codex usage and creates/archives one
synthetic CLI chat):

```bash
mkdir -p .local
python3 scripts/codex_context_handoff.py probe \
  --cwd "$PWD" --output .local/probe.json
```

That smoke test does not establish Desktop dispatch or a real context rollover.

## Further reading

- [OpenAI lifecycle hook documentation](https://learn.chatgpt.com/docs/hooks)
- [OpenAI app-server documentation](https://learn.chatgpt.com/docs/app-server)
- [Detailed operation and troubleshooting](docs/context-handoff.md)
- [Contributing](CONTRIBUTING.md) · [Security reports](SECURITY.md) · [MIT license](LICENSE)
