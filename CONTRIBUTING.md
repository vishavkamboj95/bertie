# Contributing to Bertie

Thank you for helping. Bertie's whole point is that nothing leaves your Google account
without the person's say-so, so the safety layer is the product. Changes are welcome as
long as they keep every guarantee in [SECURITY.md](SECURITY.md).

## Ground rules

- **Never weaken a safety test.** You may add tests and update strings, but never loosen
  an assertion, delete an attack string, or mark a safety test as skipped. If a test is
  wrong, say why in the pull request.
- **Never add a way round the approvals.** That means no flag, setting or environment
  variable that skips the window or the fingerprint, and no permanent-delete command.
- **Never commit a client secret or a token.** `.gitignore` covers `client_secret*.json`
  and `token*.json`. CI runs a secret scan on every pull request.
- **Keep the name in one place.** The tool's name is `NAME` in `tools/registry.py`.
  Commands, folders, the Keychain entry, the skill and the hook's patterns are all made
  from it.
- **Use plain English** in messages and docs, and no em dashes.
- **Keep Python 3.9 compatibility.** That is what macOS ships as `/usr/bin/python3`.
- **Use the standard library only** in `registry.py`, `common.py`, `hook.py` and
  `kit_setup.py`. The hook must start even when the Google libraries are missing.

## Run the tests

The tests run offline. Google, the Keychain, the Mac window and the fingerprint helper
are all replaced by fakes, and `HOME` points at a temporary folder, so nothing touches
your real setup. Keep the virtual environment **outside** the repository: one test scans
every file in the tree.

```
uv venv ../bertie-venv --python 3.9
uv pip install --python ../bertie-venv/bin/python --require-hashes --no-deps -r requirements.txt
uv pip install --python ../bertie-venv/bin/python -r requirements-test.txt
TZ=UTC ../bertie-venv/bin/python -I -m pytest -p no:cacheprovider tests
```

Check that the helper still compiles (do not run it):

```
swiftc -O -o /tmp/bertie-approve tools/approve.swift
```

## Add a Google service

Docs, Sheets, Slides, Forms, Meet, Contacts and Tasks are natural next steps. Each one is:

1. `tools/svc_<name>.py` with `build_parser()`, `main(argv=None)` and `smoke()`. Every
   command that changes something calls `common.confirm(...)` immediately before the API
   call, supports `--dry-run`, and starts its help text with `ASKS FIRST`.
2. One `Service(...)` entry in `tools/registry.py`, with its scopes and its gated actions.
3. Tests, in the style of `tests/test_drive.py`.

The command, the permission rules, the safety hook, the scope check and `bertie check`
all follow from the registry entry. After a new scope is added, each person runs
`bertie login` once more, and the code tells them so.

## Update the Python packages

`requirements.in` lists the direct packages. `requirements.txt` is the exact,
hash-checked lock; the command to rebuild it is at its top.

## Pull requests

- Keep each pull request to one change, and explain the why.
- CI must be green: pytest on Python 3.9 and the newest 3.x on macOS, the helper compile,
  and the secret scan.
- News goes in the GitHub release notes, not in the README.
