# Contributing

Use Python 3.11 or newer on Linux/WSL. There are no third-party Python
dependencies.

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q scripts tests
```

For an isolated run of committed code, use `python3 scripts/run_external_tests.py`.
Maintainers can add `--publish` after pushing the commit to attach the result and
sanitized logs to the `external/tests` GitHub commit status. This runs locally,
not in GitHub Actions. Dirty working-copy edits are excluded.

Keep changes focused. Add a regression for changes to dispatch ordering,
retryability, locking, prompt identity, or checkpoint validation. Test with
temporary homes and recording transports; do not use another person's running
task as a canary. Document any real model usage before asking someone to run it.

Treat initialization, native dispatch acceptance, live progress, control, and
task completion as distinct claims. Never infer one from another. Do not retry
an uncertain send, weaken source-stop checks, approve model tool requests, or
replace Desktop ownership with a background worker silently.

Never include actual checkpoints, transcripts, authentication/configuration
files, IPC addresses, or unredacted task logs in a pull request. Examples must
use synthetic task text and IDs. See SECURITY.md for private reporting options.
