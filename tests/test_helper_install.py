"""The installer and the fingerprint / Mac password helper.

A stand-in swiftc (see conftest) is put first on PATH, so nothing real is
compiled here; HOME is a temp folder for every run. What is checked: the helper
is built with the expected command, its sha256 is recorded, the files are made
read-only, a re-run upgrades in place, and a missing or failing compiler leaves
the read-only tools installed while every write refuses."""
import hashlib
import os
import shutil
import stat

import pytest

import common
from conftest import ACCOUNT, KIT, REAL_VERIFY_HELPER, make_stub_swiftc
from test_installer import run_install

ARGS = (ACCOUNT, "--skip-deps", "--skip-login")


def dest(home):
    return home / ".config" / "bertie"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def path_with(directory):
    return str(directory) + os.pathsep + os.environ["PATH"]


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_the_helper_is_built_recorded_and_the_files_made_read_only(home, tmp_path, monkeypatch):
    log = make_stub_swiftc(tmp_path / "stubs", payload="#!/bin/sh\nexit 0\n# build one\n")
    r = run_install(home, tmp_path, *ARGS, PATH=path_with(tmp_path / "stubs"))
    assert r.returncode == 0, r.stdout + r.stderr
    d = dest(home)
    helper = d / "bertie-approve"
    assert helper.is_file() and mode(helper) == 0o500
    assert (d / "approve.sha256").read_text().strip() == sha(helper)
    assert mode(d / "approve.sha256") == 0o400
    args = log.read_text().split()
    assert args[:2] == ["-O", "-o"] and args[2] == str(helper) and args[3] == str(d / "approve.swift")
    assert (d / "approve.swift").read_bytes() == (KIT / "tools" / "approve.swift").read_bytes()
    assert mode(d / "approve.swift") == 0o400
    for f in d.glob("*.py"):
        assert mode(f) == 0o400, f
    assert mode(d / "hook.sh") == 0o500 and mode(d / "client_secret.json") == 0o400
    assert "NOT READY" not in r.stdout and not (d / "helper-build.log").exists()
    # the tools' own check accepts exactly what the installer built
    monkeypatch.setattr(common, "HOME", d)
    assert REAL_VERIFY_HELPER() == helper


def test_running_the_installer_again_upgrades_in_place_and_keeps_everything_else(home, tmp_path, monkeypatch):
    make_stub_swiftc(tmp_path / "stubs", payload="#!/bin/sh\nexit 0\n# build one\n")
    env = path_with(tmp_path / "stubs")
    assert run_install(home, tmp_path, *ARGS, PATH=env).returncode == 0
    d = dest(home)
    first = sha(d / "bertie-approve")
    # state that must survive: the action log, the private environment, the account
    (d / "actions.log").write_text("kept\n")
    (d / ".venv" / "marker").write_text("kept")
    account = (d / "account").read_text()
    # a local edit to a read-only tool file, which the upgrade must replace
    tool = d / "svc_mail.py"
    tool.chmod(0o600)
    tool.write_text(tool.read_text() + "\n# local edit\n")
    tool.chmod(0o400)

    make_stub_swiftc(tmp_path / "stubs", payload="#!/bin/sh\nexit 0\n# build two\n")
    r = run_install(home, tmp_path, *ARGS, PATH=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (d / "actions.log").read_text() == "kept\n" and (d / ".venv" / "marker").read_text() == "kept"
    assert (d / "account").read_text() == account
    assert tool.read_bytes() == (KIT / "tools" / "svc_mail.py").read_bytes() and mode(tool) == 0o400
    assert sha(d / "bertie-approve") != first
    assert (d / "approve.sha256").read_text().strip() == sha(d / "bertie-approve")
    monkeypatch.setattr(common, "HOME", d)
    assert REAL_VERIFY_HELPER() == d / "bertie-approve"


def test_the_installer_never_touches_the_login():
    text = (KIT / "install.sh").read_text()
    assert "Already signed in - keeping the existing login" in text
    for word in ("logout", "delete_token", "delete-generic", "keyring", "security "):
        assert word not in text, word


def tools_without_swiftc(directory):
    """A PATH folder with the ordinary tools the installer needs and no compiler."""
    directory.mkdir()
    for name in ("sh", "dirname", "mkdir", "chmod", "cp", "rm", "printf", "cat", "cut", "shasum", "uv",
                 "python3", "env", "uname", "tr", "sed", "grep", "ls", "mv", "ln", "date", "head", "tail",
                 "sort", "id", "mktemp", "basename", "readlink", "wc", "awk", "find", "touch", "xcode-select",
                 "plutil"):
        found = shutil.which(name)
        if found:
            (directory / name).symlink_to(found)
    assert not (directory / "swiftc").exists()
    return directory


def test_no_compiler_still_installs_the_reading_tools_and_writes_refuse(home, tmp_path, monkeypatch, capsys):
    bare = tools_without_swiftc(tmp_path / "bare")
    r = run_install(home, tmp_path, *ARGS, PATH=str(bare))
    assert r.returncode == 0, r.stdout + r.stderr
    d = dest(home)
    assert "xcode-select --install" in r.stdout and "NOT READY FOR SENDING" in r.stdout
    assert not (d / "bertie-approve").exists() and not (d / "approve.sha256").exists()
    assert (home / ".local" / "bin" / "bertie-mail").exists()            # reading tools are in
    assert (home / ".claude" / "settings.json").exists()                  # and so are the rules and hook
    monkeypatch.setattr(common, "HOME", d)
    with pytest.raises(SystemExit):
        REAL_VERIFY_HELPER()
    assert "install.sh" in capsys.readouterr().err


def test_a_compiler_that_fails_leaves_no_helper_and_says_why(home, tmp_path, monkeypatch):
    make_stub_swiftc(tmp_path / "stubs", fail=True)
    r = run_install(home, tmp_path, *ARGS, PATH=path_with(tmp_path / "stubs"))
    assert r.returncode == 0, r.stdout + r.stderr
    d = dest(home)
    assert "could not be built" in r.stdout and "NOT READY FOR SENDING" in r.stdout
    assert "boom" in (d / "helper-build.log").read_text()
    assert not (d / "bertie-approve").exists() and not (d / "approve.sha256").exists()
    assert (home / ".local" / "bin" / "bertie-mail").exists()


def test_a_failed_rebuild_removes_the_old_helper_instead_of_leaving_a_stale_one(home, tmp_path):
    make_stub_swiftc(tmp_path / "stubs")
    env = path_with(tmp_path / "stubs")
    assert run_install(home, tmp_path, *ARGS, PATH=env).returncode == 0
    d = dest(home)
    assert (d / "bertie-approve").exists()
    make_stub_swiftc(tmp_path / "stubs", fail=True)
    assert run_install(home, tmp_path, *ARGS, PATH=env).returncode == 0
    assert not (d / "bertie-approve").exists() and not (d / "approve.sha256").exists()
