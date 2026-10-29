# Automatic context handoffs

The context-handoff integration prepares a small checkpoint near 350,000 active
tokens and a fresh, preloaded chat near 400,000. It does not change the model's
context window, compaction threshold, reasoning effort, or service tier.

## What it does

- `PostToolUse` and `UserPromptSubmit` use the last request's input/output usage,
  not lifetime or cumulative token totals. Below the threshold the hooks are silent.
- At 350k the agent writes a concise structured checkpoint. It refreshes after
  newer user instructions or approximately another 20k tokens of work.
- Near 400k the agent finalizes the checkpoint and invokes the local helper.
  The helper verifies the source chat, user-request epoch, working directory,
  declared files, Git state, and absence of unfinished operations.
- A persistent chat is created in the same workspace and project. Its initial
  read-only turn loads the checkpoint and must reproduce the exact goal,
  restrictions, and next action before the handoff is considered ready.
- The old agent then finishes its turn. The original chat, transcript, files,
  uncommitted changes, evidence, and checkpoint remain available.
- A `PreToolUse` guard prevents that old turn from continuing tool actions after
  successful transfer. A genuine newer user request in the old chat remains
  usable and is not silently forwarded or discarded.

In ready-only mode, the new chat waits for the user's next message after
initialization. With `auto_continue: true` and `continuation_backend: "desktop"`,
the handoff is registered as `waiting_for_source_stop`. The source Stop hook
persists confirmation and starts a per-handoff dispatcher, which rechecks
the checkpoint, workspace and latest user request. It submits continuation
through the app's bundled `send_message_to_thread` MCP action and exits after
the receipt. Desktop owns the actual turn, its live progress and its controls.
No extra click or Continue message is needed.

Waiting for the source to finish starts no worker and has no 180-second expiry.
A delayed Stop also restarts an older `source_stop_not_confirmed` handoff.
Each handoff has a short-lived supervisor that retries pre-dispatch worker
failures up to three times with one- and two-second backoff. Coordinator launch
failures get three short attempts inside the Stop hook. Locks and process start
identities suppress duplicate coordinators and distinguish dead or reused PIDs.
Exhausted recovery is recorded as `pre_dispatch_retries_exhausted` and requires
inspection; repeated Stop callbacks do not create an endless retry loop.

Accepted or uncertain sends are never retried. New source input, changed
checkpoints, approvals and a successor used manually stop recovery. Initialization
records the successor's prompt markers so a retry can detect that manual use.
Scheduling means registered or launched; only `desktop_dispatched` with the
native receipt confirms acceptance by Desktop. Task completion is separate.

With `auto_open: true`, the dispatcher uses the bundled
`navigate_to_codex_page` action to show the verified chat. It invokes the shipped
MCP server through its declared stdio transport and forwards genuine source
thread/turn routing metadata. It does not implement private app IPC or automate
ChatGPT's UI. The temporary read-only bootstrap app-server has already released
the new chat before Desktop starts work.

The dispatcher never approves a permission request. Actual task approvals and
input remain in Desktop's normal interactive flow. A bridge failure is recorded
without silently falling back to an external worker. Ambiguous dispatches are
not automatically retried, because the first request may already have started
work. Completed checkpoints open the fresh chat without inventing a pending
task. Disabling the integration prevents future dispatches; it does not cancel a
Desktop-owned turn that has already been accepted.

This integration does not promise identical recall of every old message.
Original history is retained for selective recovery.

Successors prefix the original task title with `Part 2 · `, `Part 3 · `, and so
on. Desktop prefixes the original chat with `Part 1 · ` at its first automatic
handoff. Private state stores the original title, root chat, part number and
previous chat; ancestry comes from recorded handoff edges, never title parsing.
Both initialization and continuation prompts begin with clickable **Original
chat** and **Previous part** links. Ambiguous or cyclic ancestry stops transfer.
Existing chains can be repaired with native background title updates and
passive `lineage.md` panel tabs, without sending messages or owning their turns.

The older `continuation_backend: "worker"` mode supervises a separate app-server
and can make Desktop show "This is open in another app". It is retained for
compatibility, and is not the configured Desktop mode. Disabling automatic
continuation interrupts a legacy worker at its next event check; changing the
backend alone leaves an already-running legacy worker intact.

## Preservation and failure behavior

- No stash, reset, checkout, commit, deployment, original-chat archive, or deletion.
- Root chats only; subagents and ambiguous transcript identities are ignored.
- A completed chat can also roll over at 400k. Its checkpoint keeps the completed
  status and empty remaining-work list; the new chat does not invent more work.
- Running operations defer transfer. Finish them or establish a deliberate,
  recoverable job handoff before marking the checkpoint safe.
- New user instructions invalidate an older checkpoint, including instructions
  arriving while initialization is running. Workspace changes or a
  failed initialization keep the work in the original chat.
- A persisted successor ID and file lock prevent duplicate successors on retries.
- Bootstrap does not approve requests or broaden task authority. A source runtime
  explicitly reporting full-access mode retains that mode; otherwise the new
  chat uses the user's normal defaults. Checkpoint text cannot grant permissions.
- If a handoff cannot finish before automatic compaction, normal compaction is
  allowed rather than discarding work or trapping the agent in a continuation loop.

Hooks have bounded transcript reads and perform no Git commands or model requests
on routine tool callbacks. Git validation and new-chat initialization happen only
when the agent explicitly finalizes a checkpoint. The helper uses the supported
stdio app-server protocol; it never writes into the Desktop process's pipes.

## Files and controls

- Source: `scripts/codex_context_handoff.py`
- Installer: `scripts/install_codex_context_handoff.py`
- Tests: `tests/test_context_handoff.py`
- Private state/config: `~/.local/share/codex-context-handoff/`
- Installed, versioned helper: `~/.local/lib/codex-context-handoff/`

Each checkpoint is limited to 12 KiB; the intended size is 2–4 KiB. Checkpoints
must exclude credentials. They record goal, constraints, completed/remaining work,
paths, evidence, running operations, and the next action. State is private to the
local user. Desktop mode launches a dispatcher after the source Stop event. It
submits the native app action, then exits. No permanent service,
model download, TCP listener, or scheduled polling process is installed.

Install hooks (initially disabled):

```sh
python3 scripts/install_codex_context_handoff.py
```

The installer preserves existing RTK/other hooks and their positions, backs up
the hook/config files, installs a versioned helper, and records the exact native
hook hashes. Activation uses `enabled: true` in the private `settings.json` after
verification. `allowed_sessions` is normally empty; canary tests restrict it to
one synthetic chat while using lower test thresholds.

Fully automatic Desktop mode uses `auto_continue: true`, `auto_open: true` and
`continuation_backend: "desktop"` in that private configuration. Model limits
remain unchanged. Dispatch markers and native receipts are stored in the source
session's `state.json`; `continuation-worker.log` is private.

For cached hook definitions, `hook_entrypoint` names a managed compatibility
loader. The installer backs up its original bytes and forwards it to the new
versioned helper. Retaining the existing command also retains its hook trust
hash, so running chats can use the new dispatcher without an app restart.

Disable immediately, without changing model settings:

```sh
python3 scripts/codex_context_handoff.py disable
```

Inspect a particular source chat:

```sh
python3 scripts/codex_context_handoff.py status --session CHAT_ID
```

Already-loaded chats can cache hook definitions. The compatibility loader handles
the installed definitions without restarting or interrupting active work. Chats
that predate the initial hook installation may still need a later reload.

Checkpoints and original history are not automatically pruned. Review retained
state as part of normal maintenance. Disabling the integration leaves all work
and evidence intact.

## Verification

```sh
python3 -m unittest discover -s tests -p test_context_handoff.py -v
python3 scripts/codex_hooks_list.py "$PWD"
```

Require five recognized, enabled handoff hooks with matching trusted hashes and
no load warnings. Any pre-existing RTK or other hooks must remain unchanged;
RTK is optional and is not installed by this project. Use the `CODEX_HOME`
environment variable when inspecting a non-default configuration home.
`additionalContextLimit` belongs only on events supporting
additional context; the installer removes it from its Stop and PreCompact hooks.
The supplied tests use temporary state and recording transports. They verify
initialization confirmation, source preservation, idempotent retry, worker crash
recovery and guards against an uncertain duplicate send. They do not contact a
model or prove native Desktop behavior. The optional `probe` command creates and
archives one synthetic model-access smoke-test chat; it does not test rollover.

A full live acceptance requires a separate synthetic task: verify actual context
usage at both thresholds, exact checkpoint confirmation, source-stop ordering,
native Desktop progress/control, and no interruption to unrelated tasks. A
successful CLI bootstrap or app send receipt alone does not establish all these
properties. Use only task-owned synthetic chats for that test.

## Troubleshooting

| Symptom or state | Meaning and next step |
| --- | --- |
| Hooks never run | Confirm the selected home is the one Codex loads, all five definitions are trusted, and this chat loaded them. Reload only when existing work permits. |
| `codex` unavailable | Make its executable visible in the actual hook environment. Shell startup PATH and Desktop PATH can differ. Review `codex_binary` in settings. |
| Unsupported hook/schema | Use a compatible CLI build; installation may have created disabled settings and backups. Check the reported backup directory and restore only this integration's changes. |
| `waiting_for_source_stop` | The successor is initialized but source Stop has not arrived. No worker is timing out. Do not interrupt the source to force dispatch. |
| Old `source_stop_not_confirmed` | A late Stop can schedule recovery if the source request is unchanged. New source input or manual successor work requires inspection rather than blind replay. |
| `launch_failed` | The hook could not launch the coordinator. Check Python path and permissions. A later eligible Stop can retry. |
| `pre_dispatch_retries_exhausted` | Three safe worker attempts failed. Inspect the local worker log and last error type; resolve the cause before a deliberate retry. Repeated callbacks will not loop forever. |
| App-tool connection unavailable | Automatic mode must originate from a genuine compatible Desktop task with the bundled manifest/runtime and inherited connection. An arbitrary terminal is insufficient. Use ready-only mode if unavailable. |
| `successor_already_used` | A newly prepared successor's prompt markers changed, or it already handed off again. Manual work is preserved; no second continuation is sent. |
| `cancelled_new_source_input` | A newer source request superseded this transfer. Continue in the source or inspect the existing successor. |
| `checkpoint_changed` | Files, workspace identity or checkpoint changed since acceptance. Refresh through the source; do not edit the immutable snapshot to bypass validation. |
| `desktop_dispatch_failed_or_ambiguous` | A send may already have been accepted. Check the successor in Desktop before any manual continuation. The supervisor will not retry it. |
| `desktop_dispatched` | Desktop accepted the send. Inspect the successor's actual progress and completion in the app. |

State and logs are private operational data. Redact paths, task text, IDs and
IPC details before reporting bugs. Configuration backups can contain sensitive
settings, so keep them outside the public checkout.
