# Security and privacy

The helper can launch Codex model turns and, when enabled, transfer an authorized
task to a fresh Desktop chat. Review its source and installer before marking
hooks trusted. Use the normal Codex approval flow for the underlying task.

Keep checkpoints and private state outside Git. The repository ignores `.local/`,
`.env` files, and logs, but ignore rules cannot protect arbitrary copied files.
The checkpoint credential-pattern check is a limited guard, not a secret scanner
or a guarantee that task text is safe to publish.

For a sensitive security issue, use the repository's **Security → Advisories →
Report a vulnerability** option when available. If private reporting is
unavailable, open an issue asking for a private contact without including the
vulnerability details, credentials, paths, IPC endpoints, transcripts, or logs.

For normal bug reports, share the helper/CLI/Python versions, operating system,
mode, and a redacted status code. Do not paste your complete `state.json`,
`config.toml`, `hooks.json`, or a continuation-worker log without reviewing it.
