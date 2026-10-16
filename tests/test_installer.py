"""The installer and uninstaller end to end, in a temporary HOME, with the
network (package install, browser login) skipped or stubbed.

Nothing here touches the real home folder, the real settings file or the real
Keychain: HOME is a temp folder for every subprocess and for the in-process
calls, and the Keychain is the in-memory fake from conftest."""
import json
import os
import shutil
import stat
import subprocess

import pytest

import auth
import common
import google_cli
import kit_setup
import registry
from conftest import ACCOUNT, KIT, make_stub_swiftc, write_fake_client

INSTALL = KIT / "install.sh"


def env_for(home, tmp_path, **extra):
    """A stand-in swiftc comes first on PATH, so no test compiles anything real."""
    if "PATH" not in extra:
        make_stub_swiftc(tmp_path / "stubbin")
        extra["PATH"] = str(tmp_path / "stubbin") + os.pathsep + os.environ["PATH"]
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "LANG": "C",
           "UV_CACHE_DIR": str(tmp_path / "uvcache"), "UV_NO_CONFIG": "1"}
    env.update(extra)
    return env


def run_install(home, tmp_path, *args, kit=KIT, client=True, **extra):
    """client=True passes a stand-in OAuth client file, False passes none, a path passes that file."""
    if client is True:
        client = write_fake_client(tmp_path / "client")
    if client:
        args = (*args, "--client-secret", str(client))
    return subprocess.run(["sh", str(kit / "install.sh"), *args], env=env_for(home, tmp_path, **extra),
                          capture_output=True, text=True, timeout=300)


def snapshot(home, skip=()):
    out = {}
    for p in sorted(home.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and ".bertie-backup-" not in p.name and not any(str(p).startswith(str(s)) for s in skip):
            out[str(p.relative_to(home))] = p.read_bytes()
    return out


@pytest.fixture
def installed(home, tmp_path):
    """A finished install (no dependencies, no sign-in)."""
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login")
    assert r.returncode == 0, r.stdout + r.stderr
    return home / ".config" / "bertie"


# ------------------------------------------------------------------ arguments

@pytest.mark.parametrize("args", [[], ["not-an-email"], ["a b@test.invalid"], ["@test.invalid"], ["--bogus"]])
def test_bad_arguments_stop_before_touching_anything(home, tmp_path, args):
    r = run_install(home, tmp_path, *args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (home / ".config").exists()


def test_help_exits_zero(home, tmp_path):
    r = run_install(home, tmp_path, "--help")
    assert r.returncode == 0 and "usage" in r.stdout
    assert not (home / ".config").exists()


# ------------------------------------------------------------------ what gets installed

def test_install_layout_and_permissions(installed, home, tmp_path):
    assert stat.S_IMODE(installed.stat().st_mode) == 0o700
    for f in ["registry.py", "common.py", "auth.py", "google_cli.py", "kit_setup.py", "hook.py", "hook.sh",
              "svc_mail.py", "svc_calendar.py", "svc_chat.py", "svc_drive.py", "requirements.txt",
              "client_secret.json", "account", "install-manifest.json"]:
        assert (installed / f).is_file(), f
    assert (installed / ".venv" / "bin" / "python").exists()
    assert (installed / "account").read_text().strip() == ACCOUNT
    assert (installed / "client_secret.json").read_bytes() == (tmp_path / "client" / "client_secret_test.json").read_bytes()
    assert stat.S_IMODE((installed / "client_secret.json").stat().st_mode) == 0o400
    assert stat.S_IMODE((installed / "account").stat().st_mode) == 0o600
    assert not list(installed.glob("token*"))                      # tokens never touch the disk


def test_commands_skill_rules_hook_and_path(installed, home):
    for cmd in ["bertie-mail", "bertie-calendar", "bertie-chat", "bertie-drive", "bertie"]:
        p = home / ".local" / "bin" / cmd
        text = p.read_text()
        assert os.access(p, os.X_OK) and registry.WRAPPER_MARKER in text
        assert '-I -W ignore' in text and '"$@"' in text
    assert (home / ".claude" / "skills" / "bertie" / "SKILL.md").read_bytes() == \
        (KIT / "skill" / "bertie" / "SKILL.md").read_bytes()
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert sorted(settings["permissions"]["ask"]) == sorted(registry.ask_rules())
    groups = settings["hooks"]["PreToolUse"]
    assert [g["matcher"] for g in groups] == list(kit_setup.HOOK_MATCHERS)
    for g in groups:
        assert g["hooks"] == [{"type": "command", "command": kit_setup.HOOK_COMMAND}]
    rc = (home / ".zshrc").read_text()
    assert rc == kit_setup.PATH_MARKER + "\n" + kit_setup.PATH_LINE + "\n"


def test_the_installed_commands_start_without_the_google_libraries(installed, home, tmp_path):
    env = env_for(home, tmp_path)
    r = subprocess.run([str(home / ".local" / "bin" / "bertie"), "log"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "empty" in r.stdout, r.stdout + r.stderr
    r = subprocess.run([str(home / ".local" / "bin" / "bertie-mail"), "--help"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "bertie-mail" in r.stdout


def test_the_installed_hook_answers_like_the_source_hook(installed, home, tmp_path):
    env = env_for(home, tmp_path)

    def hook(cmd):
        return subprocess.run(["sh", str(installed / "hook.sh")], env=env, capture_output=True, text=True,
                              input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
                              timeout=60)

    r = hook("true && bertie-mail send r1")
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "ask"
    r = hook("ls -la")
    assert r.returncode == 0 and r.stdout == ""


def test_the_hook_fails_closed_when_no_python_can_run_it(home, tmp_path):
    d = home / ".config" / "bertie"
    d.mkdir(parents=True)
    shutil.copy(KIT / "tools" / "hook.sh", d / "hook.sh")          # no hook.py, no venv
    r = subprocess.run(["sh", str(d / "hook.sh")], env=env_for(home, tmp_path), capture_output=True, text=True,
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}), timeout=60)
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


# ------------------------------------------------------------------ re-running

def test_rerun_changes_nothing(installed, home, tmp_path):
    before = snapshot(home)
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login")
    assert r.returncode == 0, r.stdout + r.stderr
    assert snapshot(home) == before
    assert list((home / ".claude").glob("*.bertie-backup-*")) == []
    assert (home / ".zshrc").read_text().count(kit_setup.PATH_MARKER) == 1


def test_install_keeps_existing_settings_and_hooks(home, tmp_path):
    existing = {"model": "opus",
                "permissions": {"allow": ["Bash(ls:*)"], "ask": ["Bash(rm:*)"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]}],
                          "Stop": [{"hooks": [{"type": "command", "command": "bye"}]}]}}
    p = home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(existing, indent=2) + "\n")
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login")
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads(p.read_text())
    assert got["permissions"]["allow"] == ["Bash(ls:*)"] and got["permissions"]["ask"][0] == "Bash(rm:*)"
    assert got["hooks"]["Stop"] == existing["hooks"]["Stop"]
    assert got["hooks"]["PreToolUse"][0] == existing["hooks"]["PreToolUse"][0]
    assert len(got["hooks"]["PreToolUse"]) == 4        # theirs, then our three
    assert len(list((home / ".claude").glob("settings.json.bertie-backup-*"))) == 1


def test_an_unrelated_command_with_the_same_name_is_not_overwritten(home, tmp_path):
    (home / ".local" / "bin").mkdir(parents=True)
    mine = home / ".local" / "bin" / "bertie-chat"
    mine.write_text("#!/bin/sh\necho somebody else's tool\n")
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login")
    assert r.returncode != 0
    assert "already exists" in r.stderr
    assert mine.read_text() == "#!/bin/sh\necho somebody else's tool\n"
    assert not (home / ".claude" / "settings.json").exists()       # stopped before any settings change


# ------------------------------------------------------------------ uninstall

def test_uninstall_removes_exactly_what_install_added(home, tmp_path, keychain, monkeypatch, capsys):
    before = snapshot(home)                                        # an empty home
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login")
    assert r.returncode == 0
    dest = home / ".config" / "bertie"
    monkeypatch.setattr(common, "HOME", dest)
    calls = []
    monkeypatch.setattr(auth, "_post", lambda url, data: calls.append((url, data)) or (200, "{}"))
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R-TOKEN", "scopes": []}))
    google_cli.main(["uninstall", "--yes"])
    out = capsys.readouterr().out
    assert calls and calls[0][1] == b"token=R-TOKEN"              # revoked before anything was deleted
    assert keychain.store == {}
    assert not dest.exists()
    assert snapshot(home) == before                               # every file added is gone, none else touched
    assert not (home / ".config" / "bertie").exists()
    assert "removed the commands" in out and "removed the skill" in out


def test_uninstall_restores_a_preexisting_settings_file_and_rc(home, tmp_path, keychain, monkeypatch):
    existing = {"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]}]}}
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(existing, indent=2) + "\n")
    (home / ".zshrc").write_text("alias ll='ls -l'\n")
    before = snapshot(home)
    assert run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login").returncode == 0
    assert snapshot(home) != before
    monkeypatch.setattr(common, "HOME", home / ".config" / "bertie")
    monkeypatch.setattr(auth, "_post", lambda url, data: (200, "{}"))
    google_cli.main(["uninstall", "--yes"])
    assert snapshot(home) == before


def test_uninstall_stops_if_the_token_cannot_be_revoked(home, tmp_path, keychain, monkeypatch, capsys):
    assert run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login").returncode == 0
    dest = home / ".config" / "bertie"
    monkeypatch.setattr(common, "HOME", dest)
    monkeypatch.setattr(auth, "_post", lambda url, data: (500, "no"))
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R", "scopes": []}))
    with pytest.raises(SystemExit):
        google_cli.main(["uninstall", "--yes"])
    assert dest.exists() and (registry.KEYCHAIN_SERVICE, ACCOUNT) in keychain.store
    assert (home / ".local" / "bin" / "bertie-mail").exists()      # nothing was removed
    google_cli.main(["uninstall", "--yes", "--force-local"])
    assert not dest.exists() and keychain.store == {}


def test_uninstall_needs_yes_and_refuses_to_run_from_a_source_checkout(home, tmp_path, keychain, monkeypatch):
    assert run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login").returncode == 0
    monkeypatch.setattr(common, "HOME", home / ".config" / "bertie")
    with pytest.raises(SystemExit):
        google_cli.main(["uninstall"])                              # no --yes
    monkeypatch.setattr(common, "HOME", KIT / "tools")              # running from the kit's own folder
    with pytest.raises(SystemExit):
        google_cli.main(["uninstall", "--yes"])
    assert (KIT / "tools" / "registry.py").exists()


# ------------------------------------------------------------------ the sign-in / check tail of install.sh

STUB = '''#!/usr/bin/env python3
import os, sys
home = os.path.expanduser("~")
signed = os.path.join(home, "stub-signed-in")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "whoami":
    if os.path.exists(signed):
        print("signed in as stub"); sys.exit(0)
    sys.exit(1)
if cmd == "login":
    open(signed, "w").write("1"); print("STUB-LOGIN-RAN"); sys.exit(0)
if cmd == "check":
    print("Mail: OK")
    print("Google Calendar is not switched on yet in your Google Cloud project")
    rcfile = os.path.join(home, "stub-check-rc")
    sys.exit(int(open(rcfile).read()) if os.path.exists(rcfile) else 0)
sys.exit(0)
'''


@pytest.fixture
def stub_kit(tmp_path):
    k = tmp_path / "stubkit"
    shutil.copytree(KIT, k, ignore=shutil.ignore_patterns("tests", ".pytest_cache", "__pycache__", ".git"))
    (k / "tools" / "google_cli.py").write_text(STUB)
    return k


def test_login_happens_once_and_a_switched_off_service_does_not_fail_the_install(home, tmp_path, stub_kit):
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", kit=stub_kit)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "STUB-LOGIN-RAN" in r.stdout and "signed in as stub" in r.stdout
    assert "Google Calendar is not switched on yet in your Google Cloud project" in r.stdout
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", kit=stub_kit)        # second run keeps the login
    assert r.returncode == 0
    assert "Already signed in" in r.stdout and "STUB-LOGIN-RAN" not in r.stdout


def test_a_real_check_failure_makes_the_installer_exit_nonzero(home, tmp_path, stub_kit):
    (home).mkdir(exist_ok=True)
    (home / "stub-check-rc").write_text("1")
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", kit=stub_kit)
    assert r.returncode == 1
    assert "a check above failed" in r.stdout


# ------------------------------------------------------------------ bring your own Google project

def test_the_first_install_needs_a_client_file_and_says_how_to_get_one(home, tmp_path):
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", client=False)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--client-secret" in r.stdout and "Set up your Google project" in r.stdout
    assert not (home / ".config").exists() and not (home / ".local").exists()


@pytest.mark.parametrize("data,words", [
    ({"web": {"client_id": "1-x.apps.googleusercontent.com", "client_secret": "s"}}, "Desktop app"),
    ({"installed": {"client_id": "not-a-google-id", "client_secret": "s"}}, "does not look like"),
    ({"installed": {"client_id": "1-x.apps.googleusercontent.com"}}, "no client_secret"),
    ({"something": "else"}, "not a Google OAuth client file"),
])
def test_a_wrong_client_file_is_refused_before_anything_is_installed(home, tmp_path, data, words):
    bad = write_fake_client(tmp_path / "bad", data)
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", client=bad)
    assert r.returncode == 2, r.stdout + r.stderr
    assert words in r.stdout
    assert not (home / ".config").exists()


def test_a_client_file_that_is_not_json_or_is_a_link_or_is_missing_is_refused(home, tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text("this is not json")
    link = tmp_path / "link.json"
    link.symlink_to(write_fake_client(tmp_path / "real"))
    for client in (junk, link, tmp_path / "missing.json"):
        r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", client=client)
        assert r.returncode == 2, (client, r.stdout + r.stderr)
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", "--client-secret", client=False)
    assert r.returncode == 2 and "needs a file name" in r.stdout
    assert not (home / ".config").exists()


def test_a_rerun_keeps_the_installed_client_unless_a_new_one_is_given(installed, home, tmp_path):
    first = (installed / "client_secret.json").read_bytes()
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", client=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (installed / "client_secret.json").read_bytes() == first
    new = dict(conftest_client())
    new["installed"] = dict(new["installed"], client_id="111111111111-other.apps.googleusercontent.com")
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login",
                    client=write_fake_client(tmp_path / "new", new))
    assert r.returncode == 0, r.stdout + r.stderr
    assert b"111111111111-other" in (installed / "client_secret.json").read_bytes()
    assert stat.S_IMODE((installed / "client_secret.json").stat().st_mode) == 0o400


def conftest_client():
    from conftest import FAKE_CLIENT
    return json.loads(json.dumps(FAKE_CLIENT))


# ------------------------------------------------------------------ macOS only

def test_any_system_but_macos_is_refused_plainly(home, tmp_path):
    stubs = tmp_path / "linux"
    stubs.mkdir()
    (stubs / "uname").write_text("#!/bin/sh\necho Linux\n")
    (stubs / "uname").chmod(0o755)
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login",
                    PATH=str(stubs) + os.pathsep + os.environ["PATH"])
    assert r.returncode == 1
    assert "macOS only" in r.stdout and "Nothing was installed" in r.stdout
    assert not (home / ".config").exists()


def test_the_install_folder_is_ours_but_its_parent_is_left_alone(home, tmp_path):
    (home / ".config").mkdir()
    (home / ".config").chmod(0o755)
    assert run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login").returncode == 0
    assert stat.S_IMODE((home / ".config").stat().st_mode) == 0o755
    assert stat.S_IMODE((home / ".config" / registry.NAME).stat().st_mode) == 0o700


def test_someone_elses_folder_or_skill_with_the_same_name_is_not_taken_over(home, tmp_path):
    theirs = home / ".config" / registry.NAME
    theirs.mkdir(parents=True)
    (theirs / "notes.py").write_text("mine\n")
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login")
    assert r.returncode == 1 and "was not made by this installer" in r.stdout
    assert (theirs / "notes.py").read_text() == "mine\n" and not (home / ".claude").exists()
    shutil.rmtree(str(theirs))
    skill = home / ".claude" / "skills" / registry.NAME / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: something-else\n---\n")
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login")
    assert r.returncode == 1 and "is not this tool's skill" in r.stdout
    assert skill.read_text() == "---\nname: something-else\n---\n"


def test_giving_the_installed_client_file_back_is_harmless(installed, home, tmp_path):
    first = (installed / "client_secret.json").read_bytes()
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", client=installed / "client_secret.json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (installed / "client_secret.json").read_bytes() == first
    assert stat.S_IMODE((installed / "common.py").stat().st_mode) == 0o400


def test_an_empty_client_secret_is_refused(home, tmp_path):
    bad = write_fake_client(tmp_path / "bad", {"installed": {
        "client_id": "1-x.apps.googleusercontent.com", "client_secret": ""}})
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", client=bad)
    assert r.returncode == 2 and "no client_secret" in r.stdout
