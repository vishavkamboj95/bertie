"""The second step of every gated action: your fingerprint or Mac password.

The real helper is a compiled Swift program (tools/approve.swift) that these
tests never run, because running it would put a real Touch ID sheet on the
screen. Here the process start is replaced and the checks around it are real:
how it is called, when it is refused, that both steps are needed, and that
nothing is logged when either fails."""
import ast
import hashlib
import inspect
import subprocess
import textwrap

import pytest

import common
import svc_mail
from conftest import REAL_VERIFY_HELPER, read_log
from test_mail import DRAFT


def put_helper(install_dir, body=b"#!/bin/sh\nexit 0\n", record=True, mode=0o700):
    h = install_dir / common.HELPER_NAME
    h.write_bytes(body)
    h.chmod(mode)
    if record:
        (install_dir / common.HELPER_HASH_NAME).write_text(hashlib.sha256(body).hexdigest() + "\n")
    return h


@pytest.fixture
def real_helper(monkeypatch, install_dir, dialog):
    """The real _verify_helper, over a helper file in the stand-in install folder,
    with only the process start replaced."""
    monkeypatch.setattr(common, "_verify_helper", REAL_VERIFY_HELPER)
    runs = []
    answer = {"rc": 0, "raise": None}

    def fake_run(argv, timeout):
        dialog.order.append("touch")
        runs.append((list(argv), timeout))
        if answer["raise"]:
            raise answer["raise"]
        return subprocess.CompletedProcess(argv, answer["rc"], "", "")

    monkeypatch.setattr(common, "_run_helper", fake_run)
    put_helper(install_dir)
    return runs, answer


# ------------------------------------------------------------------ how the helper is called

def test_the_helper_is_started_with_its_path_and_one_flat_short_reason(real_helper, install_dir):
    runs, _ = real_helper
    common.confirm("Send", "S", [("To", "x@test.invalid")],
                   reason="send mail\nto 3\x1b people: " + "q" * 300)
    argv, timeout = runs[0]
    assert argv[0] == str(install_dir / "bertie-approve") and len(argv) == 2
    assert "\n" not in argv[1] and "\x1b" not in argv[1] and 0 < len(argv[1]) <= common.REASON_CHARS
    assert argv[1].startswith("send mail to 3 people: ")
    assert timeout > common.APPROVE_TIMEOUT_SECONDS              # the helper times out first, this is the backstop


def test_a_hostile_reason_stays_one_argument_and_is_never_run(real_helper, tmp_path):
    runs, _ = real_helper
    common.confirm("Send", "S", [], reason='"; touch ' + str(tmp_path / "pwned") + '; echo "')
    assert len(runs[0][0]) == 2 and "touch" in runs[0][0][1]
    assert not (tmp_path / "pwned").exists()


def test_without_a_reason_the_action_word_is_used(real_helper):
    runs, _ = real_helper
    common.confirm("Send", "S", [])
    assert runs[0][0][1] == "send"


# ------------------------------------------------------------------ a helper that is not the installed one is refused

def break_missing(d):
    (d / "bertie-approve").unlink()


def break_no_fingerprint_file(d):
    (d / "approve.sha256").unlink()


def break_garbled_fingerprint(d):
    (d / "approve.sha256").write_text("not a fingerprint\n")


def break_changed(d):
    (d / "bertie-approve").write_bytes(b"#!/bin/sh\nexit 0\n# changed\n")


def break_symlink(d):
    real = d / "elsewhere"
    real.write_bytes(b"#!/bin/sh\nexit 0\n")
    real.chmod(0o700)
    (d / "bertie-approve").unlink()
    (d / "bertie-approve").symlink_to(real)
    (d / "approve.sha256").write_text(hashlib.sha256(real.read_bytes()).hexdigest() + "\n")


def break_not_executable(d):
    (d / "bertie-approve").chmod(0o400)


def break_a_folder(d):
    (d / "bertie-approve").unlink()
    (d / "bertie-approve").mkdir()


BREAKS = [break_missing, break_no_fingerprint_file, break_garbled_fingerprint, break_changed,
          break_symlink, break_not_executable, break_a_folder]


@pytest.mark.parametrize("breaker", BREAKS, ids=[b.__name__ for b in BREAKS])
def test_a_missing_or_changed_helper_refuses_before_any_window_and_never_downgrades(
        breaker, real_helper, install_dir, dialog, capsys):
    runs, _ = real_helper
    breaker(install_dir)
    with pytest.raises(SystemExit) as e:
        common.confirm("Send", "S", [("To", "x@test.invalid")])
    assert e.value.code == 1
    assert dialog.shown == [] and runs == []                    # no click-only window, nothing started
    err = capsys.readouterr().err
    assert "nothing was done" in err and "install.sh" in err


def test_a_helper_swapped_while_the_window_is_open_is_caught(real_helper, install_dir, dialog, monkeypatch):
    runs, _ = real_helper

    def swap_then_answer(argv, timeout):
        (install_dir / "bertie-approve").write_bytes(b"#!/bin/sh\nexit 0\n# evil\n")
        return dialog(argv, timeout)

    monkeypatch.setattr(common, "_run_dialog", swap_then_answer)
    with pytest.raises(SystemExit):
        common.confirm("Send", "S", [])
    assert len(dialog.shown) == 1 and runs == []


# ------------------------------------------------------------------ the helper says no

@pytest.mark.parametrize("rc,words", [(1, "not passed"), (2, "not passed"), (139, "not passed"),
                                      (3, "cannot ask")])
def test_a_helper_that_does_not_say_yes_refuses(rc, words, real_helper, dialog, capsys):
    runs, answer = real_helper
    answer["rc"] = rc
    with pytest.raises(SystemExit) as e:
        common.confirm("Send", "S", [])
    assert e.value.code == 1 and len(runs) == 1
    err = capsys.readouterr().err
    assert words in err and "nothing was done" in err
    if rc == 3:
        assert "install.sh" in err


@pytest.mark.parametrize("exc", [OSError("cannot start"), subprocess.TimeoutExpired("h", 90)])
def test_a_helper_that_cannot_run_or_hangs_refuses(exc, real_helper, capsys):
    _, answer = real_helper
    answer["raise"] = exc
    with pytest.raises(SystemExit):
        common.confirm("Send", "S", [])
    assert "could not run" in capsys.readouterr().err


# ------------------------------------------------------------------ both steps, in order

def test_both_steps_are_needed_and_come_in_order(real_helper, dialog):
    runs, answer = real_helper
    common.confirm("Send", "S", [])
    assert dialog.order == ["details", "touch"]

    for window_answer in ("cancel", "timeout", "other", "error"):
        dialog.order.clear()
        runs.clear()
        dialog.answer = window_answer
        with pytest.raises(SystemExit):
            common.confirm("Send", "S", [])
        assert dialog.order == ["details"] and runs == []       # the second step never starts

    dialog.answer = "approve"
    answer["rc"] = 1
    dialog.order.clear()
    with pytest.raises(SystemExit):
        common.confirm("Send", "S", [])
    assert dialog.order == ["details", "touch"]                 # window passed, check failed: still refused


def test_confirm_has_no_way_to_return_before_the_helper_has_said_yes():
    tree = ast.parse(textwrap.dedent(inspect.getsource(common.confirm)))
    fn = tree.body[0]
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    last = fn.body[-1]
    assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call) and last.value.func.id == "_approve"
    src = inspect.getsource(common.confirm)
    assert src.index("_verify_helper()") < src.index("_run_dialog") < src.index("_approve(")


def test_a_real_send_without_the_helper_is_refused_not_downgraded(google, install_dir, dialog, monkeypatch, capsys):
    monkeypatch.setattr(common, "_verify_helper", REAL_VERIFY_HELPER)      # nothing is installed here
    fake = google({"users.drafts.get": DRAFT, "users.drafts.send": {"id": "s1"}})
    with pytest.raises(SystemExit):
        svc_mail.main(["send", "r7"])
    assert dialog.shown == [] and "users.drafts.send" not in fake.keys()
    assert read_log(install_dir) == [] and "install.sh" in capsys.readouterr().err
