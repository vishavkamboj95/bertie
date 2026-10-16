"""Shared test setup. Everything runs offline against fakes.

Safety nets that apply to EVERY test automatically:
  * HOME points at an empty temporary folder, never the real one;
  * times are shown in one fixed zone (Asia/Kolkata), not the test Mac's own;
  * the Keychain is replaced by an in-memory fake, so no test can read or
    write a real Keychain entry;
  * no network: Google clients are replaced by FakeService.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
TOOLS = KIT / "tools"
sys.path.insert(0, str(TOOLS))

import common  # noqa: E402

REAL_RUN_DIALOG = common._run_dialog          # kept so test_confirm can exercise the real one
REAL_DIALOG_AVAILABLE = common._dialog_available
REAL_VERIFY_HELPER = common._verify_helper
REAL_RUN_HELPER = common._run_helper

ACCOUNT = "acct@test.invalid"       # synthetic test addresses only
OTHER = "other@test.invalid"

# A stand-in for the person's own OAuth client file ("Desktop app" type). Not a real secret.
FAKE_CLIENT = {"installed": {"client_id": "000000000000-test.apps.googleusercontent.com",
                             "client_secret": "test-not-a-real-secret",
                             "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                             "token_uri": "https://oauth2.googleapis.com/token",
                             "redirect_uris": ["http://localhost"]}}


def write_fake_client(directory, data=None, name="client_secret_test.json"):
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text(json.dumps(FAKE_CLIENT if data is None else data))
    return p


# ---------------------------------------------------------------- fake Keychain

class FakeKeyring:
    """Same surface the code uses: get/set/delete_password."""

    def __new__(cls):
        import keyring.backend

        class _Backend(keyring.backend.KeyringBackend):
            priority = 1

            def __init__(self):
                self.store = {}

            def get_password(self, service, username):
                return self.store.get((service, username))

            def set_password(self, service, username, password):
                self.store[(service, username)] = password

            def delete_password(self, service, username):
                import keyring.errors
                if (service, username) not in self.store:
                    raise keyring.errors.PasswordDeleteError("not found")
                del self.store[(service, username)]

        return _Backend()


@pytest.fixture(autouse=True)
def isolated_world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # every time assertion is written for Asia/Kolkata (IST), whatever zone the test Mac is in
    monkeypatch.setattr(common, "ZONE_NAME", "Asia/Kolkata")
    import keyring
    import keyring.core
    fake = FakeKeyring()
    monkeypatch.setattr(keyring.core, "_keyring_backend", fake)
    assert keyring.get_keyring() is fake
    return fake


@pytest.fixture
def keychain(isolated_world):
    return isolated_world


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def install_dir(tmp_path, monkeypatch):
    """A stand-in install folder with the account configured."""
    import common
    d = tmp_path / "installdir"
    d.mkdir()
    (d / "account").write_text(ACCOUNT + "\n")
    monkeypatch.setattr(common, "HOME", d)
    return d


# ---------------------------------------------------------------- fake Google client

class _Node:
    def __init__(self, root, names, kwargs=None):
        self._root, self._names, self._kw = root, names, kwargs or {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        kw = self._kw if name == "execute" else {}
        return _Node(self._root, self._names + [name], kw)

    def __call__(self, *args, **kwargs):
        if self._names[-1] == "execute":
            return self._root._respond(".".join(self._names[:-1]), self._kw)
        return _Node(self._root, self._names, kwargs)


class FakeService:
    """svc.users().messages().get(id=1).execute() looks up the key
    'users.messages.get' in `responses` and records the call.

    A response may be a value, a callable taking the kwargs, or an Exception
    instance (raised)."""

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__") or name in ("responses", "calls"):
            raise AttributeError(name)
        return _Node(self, [name])

    def _respond(self, key, kwargs):
        self.calls.append((key, kwargs))
        if key not in self.responses:
            raise AssertionError(f"unexpected API call: {key} {kwargs}")
        r = self.responses[key]
        if isinstance(r, Exception):
            raise r
        return r(kwargs) if callable(r) else r

    def called(self, key):
        return [kw for k, kw in self.calls if k == key]

    def keys(self):
        return [k for k, _ in self.calls]


@pytest.fixture
def google(install_dir, monkeypatch):
    """Patch the auth layer: a signed-in dummy, a chosen mailbox, a FakeService.

    Returns a function: google(responses, email=ACCOUNT) -> FakeService."""
    import auth

    def make(responses, email=ACCOUNT):
        fake = FakeService(responses)
        monkeypatch.setattr(auth, "credentials", lambda: object())
        monkeypatch.setattr(auth, "signed_in_email", lambda creds: email)
        monkeypatch.setattr(auth, "_discovery_build", lambda api, ver, creds: fake)
        return fake

    return make


def read_log(install_dir):
    p = install_dir / "actions.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------- a stand-in swiftc for installer tests

def make_stub_swiftc(directory, fail=False, payload="#!/bin/sh\nexit 0\n"):
    """A fake swiftc in `directory`: it writes `payload` to the -o path (or fails),
    and records its arguments in swiftc-args.log next to itself."""
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "swiftc-args.log"
    if fail:
        body = 'echo "$@" >> "%s"\necho "swiftc: error: boom" >&2\nexit 1\n' % log
    else:
        body = ('echo "$@" >> "%s"\nout=""\nwhile [ $# -gt 0 ]; do\n'
                '  if [ "$1" = "-o" ]; then out="$2"; shift; fi\n  shift\ndone\n'
                "printf '%%s' '%s' > \"$out\"\nchmod +x \"$out\"\n" % (log, payload.replace("'", "'\\''")))
    p = directory / "swiftc"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return log


# ---------------------------------------------------------------- the confirmation window

class Dialogs:
    """Stands in for the macOS window. `answer` decides what the person 'clicks':
    approve, cancel, timeout, other (a button that is not the action button)
    or error (osascript fails)."""

    def __init__(self):
        self.shown = []
        self.answer = "approve"
        self.touch = "approve"       # the fingerprint/password step: approve cancel timeout unavailable error hang
        self.reasons = []
        self.order = []

    def __call__(self, argv, timeout):
        self.order.append("details")
        message, label = argv[-2], argv[-1]
        button = label.rsplit(" ", 1)[0]                  # "Send 4821" -> "Send"
        self.shown.append({"message": message, "button": button, "label": label, "argv": list(argv)})
        if self.answer == "error":
            return subprocess.CompletedProcess(argv, 1, "", "no window server")
        out = {"approve": label, "cancel": "CANCEL", "timeout": "TIMEOUT", "other": "Something else"}[self.answer]
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")

    def helper(self, argv, timeout):
        """Stands in for the compiled fingerprint/password helper."""
        self.order.append("touch")
        self.reasons.append(argv[-1])
        if self.touch == "error":
            raise OSError("cannot start")
        if self.touch == "hang":
            raise subprocess.TimeoutExpired(argv, timeout)
        rc = {"approve": 0, "cancel": 1, "timeout": 1, "unavailable": 3}[self.touch]
        return subprocess.CompletedProcess(argv, rc, "", "")

    @property
    def last(self):
        return self.shown[-1]["message"]


@pytest.fixture(autouse=True)
def dialog(monkeypatch):
    """Every test gets a fake window that approves by default; a test that wants
    a different answer sets dialog.answer. Nothing ever opens a real window."""
    d = Dialogs()
    monkeypatch.setattr(common, "_run_dialog", d)
    monkeypatch.setattr(common, "_dialog_available", lambda: True)
    monkeypatch.setattr(common, "_run_helper", d.helper)
    monkeypatch.setattr(common, "_verify_helper", lambda: Path("/fake/bertie-approve"))
    return d
