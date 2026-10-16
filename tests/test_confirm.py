"""The confirmation window: the gate no command phrasing can get round.

Every gated action calls common.confirm() immediately before its API call. The
window is a Mac pop-up; only a click on the action button goes ahead. These
tests replace the process that would show it (nothing real ever opens) and
check what it would have shown and what happens for every other answer."""
import argparse
import ast
import inspect
import json
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import auth
import common
import google_cli
import registry
import svc_calendar
import svc_chat
import svc_drive
import svc_mail
from conftest import ACCOUNT, REAL_DIALOG_AVAILABLE, REAL_RUN_DIALOG, TOOLS, read_log
from test_calendar_chat import EVENT, SOLO
from test_mail import DRAFT, ORIGINAL


# ------------------------------------------------------------------ the real confirm(), process mocked

@pytest.fixture
def real_window(monkeypatch):
    """Use the real _run_dialog, with subprocess.run replaced."""
    monkeypatch.setattr(common, "_run_dialog", REAL_RUN_DIALOG)
    monkeypatch.setattr(common, "_dialog_available", lambda: True)
    monkeypatch.setattr(common, "_new_code", lambda: "4821")
    calls = []
    answer = {"stdout": "Continue 4821\n", "rc": 0, "raise": None}

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if answer["raise"]:
            raise answer["raise"]
        return subprocess.CompletedProcess(argv, answer["rc"], answer["stdout"], "")

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    return calls, answer


def test_a_click_on_the_action_button_returns(real_window, install_dir):
    calls, _ = real_window
    common.confirm("Send", "Send it?", [("To", "x@test.invalid")])
    assert len(calls) == 1


def test_the_text_goes_in_as_arguments_never_inside_the_script(real_window, install_dir):
    calls, _ = real_window
    hostile = '" & (do shell script "touch /tmp/pwned") & "'
    common.confirm("Send", "Send it?", [("Subject", hostile)], text=hostile)
    argv, kw = calls[0]
    assert argv[0] == "osascript"
    scripts = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert scripts == common._DIALOG_SCRIPT                       # the script is a fixed constant
    assert all("pwned" not in line and "x@test" not in line for line in scripts)
    assert "pwned" in argv[-2]                                     # the text rides in argv
    assert argv[-1] == "Continue 4821"                                 # and so does the button label
    assert kw["timeout"] > common.DIALOG_TIMEOUT_SECONDS


def test_the_window_defaults_to_cancel_and_times_out_in_about_two_minutes():
    script = " ".join(common._DIALOG_SCRIPT)
    assert 'default button "Cancel"' in script and 'cancel button "Cancel"' in script
    assert "giving up after 120" in script and "gave up" in script
    assert common.DIALOG_TIMEOUT_SECONDS == 120


def test_the_window_lists_the_account_the_details_and_a_short_preview(real_window, install_dir):
    calls, _ = real_window
    body = "A" * 290 + "BEYOND-THE-PREVIEW" + "C" * 200
    common.confirm("Send", "Send this email now?", [("To", "x@test.invalid"), ("Cc", ""), ("Subject", "Hi")], body)
    msg = calls[0][0][-2]
    assert f"From account: {ACCOUNT}" in msg and "To: x@test.invalid" in msg and "Subject: Hi" in msg
    assert "Cc:" not in msg                                         # empty rows are left out
    head, _, preview = msg.partition("\n\nMessage preview:\n")
    assert preview.startswith("A" * 290) and "BEYOND-THE-PREVIEW" not in msg
    assert preview.endswith(f"(preview, {len(body) - 300} more characters)")   # a cut is always marked
    assert "Click the button 'Continue 4821' to go on" in head


def test_control_characters_are_stripped(real_window, install_dir):
    calls, _ = real_window
    common.confirm("Send", "S", [("Subject", "a\x00b\x1bc\x07d")], "x\ty")
    msg = calls[0][0][-2]
    assert "\x00" not in msg and "\x1b" not in msg and "\x07" not in msg and "\t" not in msg


@pytest.mark.parametrize("stdout,rc,exc,words", [
    ("CANCEL\n", 0, None, "cancelled"),
    ("TIMEOUT\n", 0, None, "no answer"),
    ("Cancel\n", 0, None, "cancelled"),
    ("", 0, None, "cancelled"),                                    # window closed, nothing returned
    ("Something else\n", 0, None, "cancelled"),
    ("sEnD 4821\n", 0, None, "cancelled"),                        # only the exact label counts
    ("Send\n", 0, None, "cancelled"),                              # the word without the number
    ("Send 4822\n", 0, None, "cancelled"),                         # the wrong number
    ("", 1, None, "could not be shown"),                           # osascript failed / no desktop session
    ("", 0, OSError("no such file"), "could not be shown"),
    ("", 0, subprocess.TimeoutExpired("osascript", 1), "could not be shown"),
])
def test_anything_but_a_click_aborts_with_a_nonzero_exit(real_window, install_dir, capsys, stdout, rc, exc, words):
    _, answer = real_window
    answer.update(stdout=stdout, rc=rc)
    answer["raise"] = exc
    with pytest.raises(SystemExit) as e:
        common.confirm("Send", "Send it?", [])
    assert e.value.code == 1
    assert words in capsys.readouterr().err


def test_no_osascript_or_no_mac_means_refuse_without_running_anything(real_window, install_dir, monkeypatch, capsys):
    calls, _ = real_window
    monkeypatch.setattr(common, "_dialog_available", lambda: False)
    with pytest.raises(SystemExit) as e:
        common.confirm("Send", "Send it?", [])
    assert e.value.code == 1 and calls == []
    assert "nothing was done" in capsys.readouterr().err


def test_dialog_availability_check_needs_a_mac_with_osascript(monkeypatch):
    monkeypatch.setattr(common, "_dialog_available", REAL_DIALOG_AVAILABLE)
    monkeypatch.setattr(common.sys, "platform", "linux")
    assert common._dialog_available() is False
    monkeypatch.setattr(common.sys, "platform", "darwin")
    monkeypatch.setattr(common.shutil, "which", lambda name: None)
    assert common._dialog_available() is False
    monkeypatch.setattr(common.shutil, "which", lambda name: "/usr/bin/osascript")
    assert common._dialog_available() is True


def test_the_action_button_cannot_be_called_cancel():
    with pytest.raises(ValueError):
        common.confirm("Cancel", "x", [])


def test_there_is_no_switch_that_skips_the_window(monkeypatch, install_dir, real_window):
    sig = inspect.signature(common.confirm)
    assert list(sig.parameters) == ["button", "summary", "rows", "text", "reason"]
    tree = ast.parse(textwrap.dedent(inspect.getsource(common.confirm)))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not used & {"environ", "getenv", "environb"}
    # an environment variable that sounds helpful changes nothing
    for var in ("BERTIE_YES", "BERTIE_ASSUME_YES", "CI", "FORCE", "YES"):
        monkeypatch.setenv(var, "1")
    calls, answer = real_window
    answer["stdout"] = "CANCEL\n"
    with pytest.raises(SystemExit):
        common.confirm("Send", "x", [])
    assert len(calls) == 1


# ------------------------------------------------------------------ every gated action

@pytest.fixture
def a_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    return str(f)


def cases(a_file):
    """(id, module, argv, API responses, key of the call that changes things, words the window must show)"""
    ev_new = ["--title", "Kickoff", "--start", "2026-10-15T15:00", "--end", "2026-10-15T15:30"]
    return [
        ("mail send", svc_mail, ["send", "r7"],
         {"users.drafts.get": DRAFT, "users.drafts.send": {"id": "s1"}}, "users.drafts.send",
         ["x@test.invalid", "y@test.invalid", "Contract", "SECRET-BODY-TEXT", "Attachments: none"]),
        ("mail trash", svc_mail, ["trash", "m1"],
         {"users.messages.get": ORIGINAL, "users.messages.trash": {"id": "m1"}}, "users.messages.trash",
         ["Bin", "Quarterly numbers"]),
        ("mail untrash", svc_mail, ["untrash", "m1"],
         {"users.messages.get": ORIGINAL, "users.messages.untrash": {"id": "m1"}}, "users.messages.untrash",
         ["out of the Bin", "Quarterly numbers"]),
        ("chat send", svc_chat, ["send", "spaces/A", "PRIVATE-WORDS"],
         {"spaces.messages.create": {"name": "spaces/A/messages/1"}}, "spaces.messages.create",
         ["spaces/A", "PRIVATE-WORDS", "cannot be unsent"]),
        ("chat dm", svc_chat, ["dm", "guest@test.invalid", "PRIVATE-WORDS"],
         {"spaces.findDirectMessage": {"name": "spaces/DM1"}, "spaces.messages.create": {"name": "spaces/DM1/messages/1"}},
         "spaces.messages.create", ["guest@test.invalid", "PRIVATE-WORDS"]),
        ("drive upload", svc_drive, ["upload", a_file, "--folder", "FOLDERIDXXXXXXXXXX"],
         {"files.create": {"id": "N1", "name": "a.txt"}}, "files.create", ["a.txt", "FOLDERIDXXXXXXXXXX", "NEW file"]),
        ("drive update", svc_drive, ["update", "F1", a_file],
         {"files.get": {"id": "F1", "name": "Contract.pdf", "mimeType": "application/pdf"},
          "files.update": {"id": "F1", "name": "Contract.pdf"}}, "files.update", ["OVERWRITE", "Contract.pdf"]),
        ("drive share", svc_drive, ["share", "F1", "guest@test.invalid", "--role", "writer"],
         {"files.get": {"id": "F1", "name": "Plan.pdf"}, "permissions.create": {"id": "p1"}}, "permissions.create",
         ["Plan.pdf", "guest@test.invalid as writer", "notified"]),
        ("drive share anyone", svc_drive, ["share", "F1", "anyone", "--role", "reader", "--anyone"],
         {"files.get": {"id": "F1", "name": "Plan.pdf"}, "permissions.create": {"id": "p1"}}, "permissions.create",
         ["ANYONE WITH THE LINK", "NO sign-in"]),
        ("calendar create", svc_calendar, ["create", *ev_new, "--attendees", "a@test.invalid", "b@test.invalid"],
         {"events.insert": {"id": "n1"}}, "events.insert",
         ["Kickoff", "Google WILL EMAIL invitations to every attendee listed",
          "Attendees (2): a@test.invalid; b@test.invalid"]),
        ("calendar create alone", svc_calendar, ["create", *ev_new],
         {"events.insert": {"id": "n1"}}, "events.insert", ["no attendees, no email is sent"]),
        ("calendar move", svc_calendar, ["move", "ev1", "--start", "2026-10-16T10:00", "--end", "2026-10-16T10:30"],
         {"events.get": EVENT, "events.patch": {"id": "ev1"}}, "events.patch",
         ["Board sync", "Google WILL EMAIL the update to every attendee listed",
          "Attendees (2): a@test.invalid; b@test.invalid"]),
        ("calendar cancel", svc_calendar, ["cancel", "ev1"],
         {"events.get": EVENT, "events.delete": {}}, "events.delete",
         ["Board sync", "Google WILL EMAIL the cancellation to every attendee listed"]),
        ("calendar cancel alone", svc_calendar, ["cancel", "ev2"],
         {"events.get": SOLO, "events.delete": {}}, "events.delete", ["no attendees, no email is sent"]),
    ]


IDS = [c[0] for c in cases("x")]


@pytest.mark.parametrize("idx", range(len(IDS)), ids=IDS)
def test_the_window_shows_what_will_happen_before_the_call(idx, google, install_dir, dialog, a_file):
    _, mod, argv, responses, key, words = cases(a_file)[idx]
    fake = google(responses)
    mod.main(argv)
    assert len(dialog.shown) == 1
    msg = dialog.last
    assert f"From account: {ACCOUNT}" in msg
    for w in words:
        assert w in msg, (w, msg)
    assert key in fake.keys()
    assert dialog.shown[0]["button"] != "Cancel"


@pytest.mark.parametrize("answer", ["cancel", "timeout", "other", "error"])
@pytest.mark.parametrize("idx", range(len(IDS)), ids=IDS)
def test_cancel_timeout_or_failure_aborts_and_nothing_is_logged(idx, answer, google, install_dir, dialog, a_file, capsys):
    _, mod, argv, responses, key, _ = cases(a_file)[idx]
    fake = google(responses)
    dialog.answer = answer
    with pytest.raises(SystemExit) as e:
        mod.main(argv)
    assert e.value.code == 1
    assert len(dialog.shown) == 1
    assert key not in fake.keys()                                  # the changing call was never made
    assert not (install_dir / "actions.log").exists()              # and nothing is recorded as done
    assert "nothing was done" in capsys.readouterr().err


@pytest.mark.parametrize("idx", range(len(IDS)), ids=IDS)
def test_dry_run_never_shows_a_window_and_never_writes(idx, google, install_dir, dialog, a_file):
    _, mod, argv, responses, key, _ = cases(a_file)[idx]
    fake = google(responses)
    mod.main(argv + ["--dry-run"])
    assert dialog.shown == []
    assert key not in fake.keys()
    assert not (install_dir / "actions.log").exists()


def test_drafting_and_reading_show_no_window(google, install_dir, dialog):
    google({"users.drafts.create": {"id": "r8", "message": {}}, "users.messages.list": {}})
    svc_mail.main(["draft", "--to", "a@test.invalid", "--subject", "s", "--body", "b"])
    svc_mail.main(["search", "x"])
    assert dialog.shown == []


def test_a_dm_that_needs_a_new_space_is_still_confirmed_before_the_space_exists(google, install_dir, dialog):
    from googleapiclient.errors import HttpError
    import httplib2
    nf = HttpError(httplib2.Response({"status": 404}), b"{}")
    fake = google({"spaces.findDirectMessage": nf, "spaces.setup": {"name": "spaces/NEW"},
                   "spaces.messages.create": {"name": "spaces/NEW/messages/1"}})
    dialog.answer = "cancel"
    with pytest.raises(SystemExit):
        svc_chat.main(["dm", "guest@test.invalid", "hi"])
    assert "spaces.messages.create" not in fake.keys()


# ------------------------------------------------------------------ logout and uninstall

def test_logout_asks_first_and_cancel_leaves_everything_alone(install_dir, keychain, dialog, monkeypatch):
    posted = []
    monkeypatch.setattr(auth, "_post", lambda url, data: posted.append(url) or (200, "{}"))
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R", "scopes": []}))
    dialog.answer = "cancel"
    with pytest.raises(SystemExit):
        google_cli.main(["logout"])
    assert posted == [] and (registry.KEYCHAIN_SERVICE, ACCOUNT) in keychain.store
    dialog.answer = "approve"
    google_cli.main(["logout"])
    assert len(posted) == 1 and keychain.store == {}
    assert "sign out" in dialog.reasons[-1] and "revoke" in dialog.last


def test_uninstall_asks_once_and_cancel_removes_nothing(home, tmp_path, keychain, dialog, monkeypatch):
    import subprocess as sp
    import os
    from conftest import make_stub_swiftc
    make_stub_swiftc(tmp_path / "stubbin")
    env = {"HOME": str(home), "PATH": str(tmp_path / "stubbin") + os.pathsep + os.environ["PATH"], "LANG": "C",
           "UV_CACHE_DIR": str(tmp_path / "uvc"),
           "UV_NO_CONFIG": "1"}
    from conftest import write_fake_client
    client = write_fake_client(tmp_path / "client")
    r = sp.run(["sh", str(Path(__file__).resolve().parent.parent / "install.sh"), ACCOUNT, "--skip-deps",
                "--skip-login", "--client-secret", str(client)], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    dest = home / ".config" / "bertie"
    monkeypatch.setattr(common, "HOME", dest)
    posted = []
    monkeypatch.setattr(auth, "_post", lambda url, data: posted.append(url) or (200, "{}"))
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R", "scopes": []}))
    dialog.answer = "cancel"
    with pytest.raises(SystemExit):
        google_cli.main(["uninstall", "--yes"])
    assert dest.exists() and posted == [] and keychain.store
    assert (home / ".local" / "bin" / "bertie-mail").exists()
    dialog.answer = "approve"
    google_cli.main(["uninstall", "--yes"])
    assert not dest.exists() and keychain.store == {}
    assert len(dialog.shown) == 2 and "uninstall" in dialog.reasons[-1]


# ------------------------------------------------------------------ the call is in the code, not in the command line

def test_importing_a_module_and_calling_a_command_function_directly_still_asks(google, install_dir, dialog):
    google({"users.drafts.get": DRAFT, "users.drafts.send": {"id": "s1"}})
    dialog.answer = "cancel"
    ns = argparse.Namespace(draft_id="r7", dry_run=False, json=False)
    with pytest.raises(SystemExit):
        svc_mail.cmd_send(ns)                                      # what `python -c "import svc_mail; ..."` reaches
    assert len(dialog.shown) == 1


def test_running_a_module_as_a_script_still_asks(google, install_dir, dialog, monkeypatch):
    fake = google({"users.drafts.get": DRAFT, "users.drafts.send": {"id": "s1"}})
    dialog.answer = "cancel"
    monkeypatch.setattr(sys, "argv", ["svc_mail.py", "send", "r7"])
    with pytest.raises(SystemExit):
        runpy.run_module("svc_mail", run_name="__main__")          # what `python -m svc_mail send r7` runs
    assert len(dialog.shown) == 1 and "users.drafts.send" not in fake.keys()


# (module, function, text of the call that changes things). confirm() must come first.
WRITE_SITES = {
    ("bertie-mail", "send"): (svc_mail.cmd_send, ".drafts().send("),
    ("bertie-mail", "trash"): (svc_mail._bin_move, "api.trash"),
    ("bertie-mail", "untrash"): (svc_mail._bin_move, "api.untrash"),
    ("bertie-chat", "send"): (svc_chat._send, ".media().upload("),
    ("bertie-chat", "dm"): (svc_chat._send, ".spaces().setup("),
    ("bertie-drive", "upload"): (svc_drive.cmd_upload, ".files().create("),
    ("bertie-drive", "update"): (svc_drive.cmd_update, ".files().update("),
    ("bertie-drive", "share"): (svc_drive.cmd_share, ".permissions().create("),
    ("bertie-calendar", "create"): (svc_calendar.cmd_create, ".events().insert("),
    ("bertie-calendar", "move"): (svc_calendar.cmd_move, ".events().patch("),
    ("bertie-calendar", "cancel"): (svc_calendar.cmd_cancel, ".events().delete("),
    ("bertie", "logout"): (auth.logout, "_revoke_and_delete("),
    ("bertie", "uninstall"): (google_cli.cmd_uninstall, "_revoke_and_delete("),
}


def test_every_gated_action_is_covered_by_a_confirm_site():
    wanted = {(tool, act) for tool, acts in registry.gated_actions().items() for act in acts}
    assert wanted == set(WRITE_SITES)


@pytest.mark.parametrize("site", sorted(WRITE_SITES), ids=lambda s: " ".join(s))
def test_confirm_comes_before_the_call_that_changes_things(site):
    func, marker = WRITE_SITES[site]
    src = inspect.getsource(func)
    assert "confirm(" in src, site
    assert src.index("confirm(") < src.index(marker), site


def test_nothing_else_in_the_tools_calls_a_changing_api_without_a_confirm():
    """Any function that calls a Google method which changes something must
    itself call confirm(), so a new command cannot forget it. (cmd_draft only
    creates a draft, which sends nothing, so it is free by design.)"""
    changing = (".drafts().send(", ".drafts().create(", ".messages().trash(", ".messages().untrash(",
                ".messages().create(", ".spaces().setup(", ".media().upload(", ".files().create(",
                ".files().update(", ".permissions().create(", ".events().insert(", ".events().patch(",
                ".events().delete(", "api.trash", "api.untrash")
    free_by_design = {("svc_mail", "cmd_draft")}
    checked = 0
    for mod in (svc_mail, svc_chat, svc_drive, svc_calendar):
        for name, func in inspect.getmembers(mod, inspect.isfunction):
            if func.__module__ != mod.__name__:
                continue
            src = inspect.getsource(func)
            if any(c in src for c in changing) and (mod.__name__, name) not in free_by_design:
                checked += 1
                assert "confirm(" in src, f"{mod.__name__}.{name} changes something without a window"
    assert checked >= 9                                   # mail 2, chat 1, drive 3, calendar 3


# ------------------------------------------------------------------ round 2: random label, no cutting, flat rows, no symlinks

def test_the_new_code_is_four_digits_and_varies(monkeypatch):
    codes = [common._new_code() for _ in range(60)]
    assert all(len(c) == 4 and c.isdigit() and 1000 <= int(c) <= 9999 for c in codes)
    assert len(set(codes)) > 10
    tree = ast.parse(textwrap.dedent(inspect.getsource(common._new_code)))
    assert "secrets" in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}   # not the predictable random module


def test_each_window_gets_a_fresh_number_and_the_label_carries_it(dialog, install_dir):
    for _ in range(12):
        common.confirm("Send", "Send it?", [("To", "x@test.invalid")])
    labels = [d["label"] for d in dialog.shown]
    assert all(l.startswith("Continue ") and l.split(" ")[1].isdigit() and len(l.split(" ")[1]) == 4 for l in labels)
    assert len(set(labels)) > 1
    for d in dialog.shown:
        assert f"Click the button '{d['label']}' to go on" in d["message"]
        assert d["argv"][-1] == d["label"]


def test_the_label_does_not_make_cancel_any_less_the_default(real_window, install_dir):
    calls, _ = real_window
    common.confirm("Send", "Send it?", [])
    assert 'default button "Cancel"' in " ".join(calls[0][0])
    assert calls[0][0][-1] == "Continue 4821"


def test_a_click_that_returns_the_bare_word_or_a_stale_number_does_not_go_ahead(real_window, install_dir):
    _, answer = real_window
    for stale in ("Send\n", "Send 1234\n", "4821\n", "Continue\n", "Continue 1234\n", "Send 4821\n"):
        answer["stdout"] = stale
        with pytest.raises(SystemExit):
            common.confirm("Send", "Send it?", [])


def long_address(i):
    return f"recipient-number-{i:02d}-" + "x" * 80 + "@test.invalid"


def test_seventeen_long_recipients_are_refused_not_cut(real_window, install_dir, capsys):
    calls, _ = real_window
    with pytest.raises(SystemExit) as e:
        common.confirm("Send", "Send it?", [("To", [long_address(i) for i in range(17)])])
    assert e.value.code == 1 and calls == []                        # no window was shown with a cut list
    assert "too many recipients/attachments to show; split it" in capsys.readouterr().err


def test_a_list_that_fits_is_shown_in_full_with_its_count(real_window, install_dir):
    calls, _ = real_window
    people = [f"person{i:02d}@test.invalid" for i in range(17)]
    files = ["/real/place/one.txt", "/real/place/" + "n" * 380 + ".txt"]          # one item far over the old 300 cut
    common.confirm("Send", "Send it?", [("To", people), ("Attachments", files), ("Cc", [])])
    msg = calls[0][0][-2]
    assert "To (17): " + "; ".join(people) in msg
    assert "Attachments (2): " + "; ".join(files) in msg
    assert "Cc" not in msg
    assert "..." not in msg


def test_a_long_single_value_is_refused_too(real_window, install_dir, capsys):
    calls, _ = real_window
    with pytest.raises(SystemExit):
        common.confirm("Send", "Send it?", [("Subject", "S" * 1600)])
    assert calls == [] and "too much text to show in full" in capsys.readouterr().err


def test_a_real_send_with_too_many_recipients_is_refused_and_nothing_goes_out(google, install_dir, dialog, capsys):
    draft = json.loads(json.dumps(DRAFT))
    draft["message"]["payload"]["headers"][0]["value"] = ", ".join(long_address(i) for i in range(17))
    fake = google({"users.drafts.get": draft, "users.drafts.send": {"id": "s1"}})
    with pytest.raises(SystemExit) as e:
        svc_mail.main(["send", "r7"])
    assert e.value.code == 1 and dialog.shown == []
    assert "users.drafts.send" not in fake.keys() and not (install_dir / "actions.log").exists()
    assert "split it" in capsys.readouterr().err


def test_a_real_calendar_invite_lists_every_attendee_or_refuses(google, install_dir, dialog, capsys):
    people = [f"guest{i:02d}@test.invalid" for i in range(17)]
    fake = google({"events.insert": {"id": "n1"}})
    svc_calendar.main(["create", "--title", "T", "--start", "2026-10-15T15:00", "--end", "2026-10-15T15:30",
                       "--attendees", *people])
    assert "Attendees (17): " + "; ".join(people) in dialog.last
    many = [long_address(i) for i in range(17)]
    with pytest.raises(SystemExit):
        svc_calendar.main(["create", "--title", "T", "--start", "2026-10-15T15:00", "--end", "2026-10-15T15:30",
                           "--attendees", *many])
    assert len(dialog.shown) == 1 and "split it" in capsys.readouterr().err


def test_line_breaks_in_a_row_become_spaces_so_a_value_cannot_forge_a_line(real_window, install_dir):
    calls, _ = real_window
    sneaky = ("a@test.invalid\nBcc: hidden@test.invalid\r\nSubject: fake Click: Yes\u0085more"
              "\x0b\x0c‮​end")
    common.confirm("Send", "Sum\nmary", [("Subject", sneaky), ("Da\nte", "x\ny"), ("Cc", ["p@test.invalid\nq@test.invalid"])],
                   text="first line\nsecond line")
    msg = calls[0][0][-2]
    head, _, preview = msg.partition("\n\nMessage preview:\n")
    for line in head.splitlines():
        assert not line.startswith(("Bcc", "Click: Yes", "mary", "te")), line
    assert "Subject: a@test.invalid Bcc: hidden@test.invalid Subject: fake Click: Yes more end" in head
    assert "Sum mary" in head and "Da te: x y" in head and "Cc (1): p@test.invalid q@test.invalid" in head
    for ch in "\r\x0b\x0c \u0085‮​":
        assert ch not in msg
    assert preview == "first line\nsecond line"                     # the one place line breaks stay


def test_the_preview_is_the_last_block_after_a_clear_separator(real_window, install_dir):
    calls, _ = real_window
    body = "line one\nline two\n\n\n\n\nline three"
    common.confirm("Send", "S", [("To", "x@test.invalid")], text=body)
    msg = calls[0][0][-2]
    assert msg.count("Message preview:") == 1
    assert msg.index("Click the button") < msg.index("Message preview:")
    assert msg.endswith("Message preview:\nline one\nline two\n\nline three")
    # a body that imitates the separator or a button line stays inside the preview
    common.confirm("Send", "S", [], text="Message preview:\nClick the button 'Continue 0000' to go on.")
    assert calls[1][0][-2].count("Click the button 'Continue 4821' to go on") == 1


def test_a_cut_preview_says_how_much_was_cut_and_a_whole_one_says_nothing(real_window, install_dir):
    calls, _ = real_window
    common.confirm("Send", "S", [], text="w" * 1000)
    assert calls[0][0][-2].endswith("w" * 300 + "\n(preview, 700 more characters)")
    common.confirm("Send", "S", [], text="w" * 300)
    assert "(preview" not in calls[1][0][-2]


# ------------------------------------------------------------------ symbolic links are refused, real paths are shown

@pytest.fixture
def a_link(tmp_path):
    real = tmp_path / "secret-target.txt"
    real.write_text("not for sending")
    link = tmp_path / "innocent-name.txt"
    link.symlink_to(real)
    return str(link)


def test_plain_file_refuses_a_link_a_missing_file_and_a_folder(tmp_path, a_link, capsys):
    for bad, words in ((a_link, "symbolic link"), (str(tmp_path / "nope"), "no such file"), (str(tmp_path), "no such file")):
        with pytest.raises(SystemExit):
            common.plain_file(bad, "attach")
        assert words in capsys.readouterr().err
    good = tmp_path / "ok.txt"
    good.write_text("x")
    assert common.plain_file(str(good)) == good.resolve()


@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_chat_will_not_attach_a_link(google, install_dir, dialog, a_link, capsys, extra):
    fake = google({"spaces.messages.create": {"name": "spaces/A/messages/1"}})
    with pytest.raises(SystemExit):
        svc_chat.main(["send", "spaces/A", "hi", "--attach", a_link, *extra])
    assert dialog.shown == [] and "symbolic link" in capsys.readouterr().err
    assert "spaces.messages.create" not in fake.keys()


@pytest.mark.parametrize("argv_tail", [["upload"], ["update", "F1"]])
def test_drive_will_not_upload_or_overwrite_from_a_link(google, install_dir, dialog, a_link, capsys, argv_tail):
    fake = google({"files.get": {"id": "F1", "name": "C.pdf"}, "files.create": {"id": "N"}, "files.update": {"id": "F1"}})
    with pytest.raises(SystemExit):
        svc_drive.main([*argv_tail, a_link])
    assert dialog.shown == [] and "symbolic link" in capsys.readouterr().err
    assert not {"files.create", "files.update"} & set(fake.keys())


@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_mail_will_not_attach_a_link(google, install_dir, a_link, capsys, extra):
    fake = google({"users.drafts.create": {"id": "r8", "message": {}}})
    with pytest.raises(SystemExit):
        svc_mail.main(["draft", "--to", "a@test.invalid", "--subject", "s", "--body", "b", "--attach", a_link, *extra])
    assert "symbolic link" in capsys.readouterr().err and "users.drafts.create" not in fake.keys()


def test_the_window_shows_the_full_real_path_of_what_is_sent_or_uploaded(google, install_dir, dialog, tmp_path):
    real_dir = tmp_path / "deep" / "folder"
    real_dir.mkdir(parents=True)
    (real_dir / "report.txt").write_text("hello")
    via = tmp_path / "shortcut"                                      # a folder that is a link: the FILE is plain
    via.symlink_to(real_dir, target_is_directory=True)
    given = str(via / "report.txt")
    shown = str((real_dir / "report.txt").resolve())
    assert given != shown
    google({"spaces.messages.create": {"name": "spaces/A/messages/1"}, "media.upload": {"attachmentDataRef": {}}})
    svc_chat.main(["send", "spaces/A", "hi", "--attach", given])
    assert f"Attachment: {shown}" in dialog.shown[0]["message"]
    google({"files.create": {"id": "N1", "name": "report.txt"}})
    svc_drive.main(["upload", given])
    assert f"File: {shown} (" in dialog.last
    google({"files.get": {"id": "F1", "name": "C.pdf"}, "files.update": {"id": "F1", "name": "C.pdf"}})
    svc_drive.main(["update", "F1", given])
    assert f"New content from: {shown} (" in dialog.last


# ------------------------------------------------------------------ the fingerprint / password step on every gated action

@pytest.mark.parametrize("idx", range(len(IDS)), ids=IDS)
def test_each_gated_action_asks_the_helper_once_with_a_short_one_line_reason(idx, google, install_dir, dialog, a_file):
    _, mod, argv, responses, key, _ = cases(a_file)[idx]
    google(responses)
    mod.main(argv)
    assert dialog.order == ["details", "touch"] and len(dialog.reasons) == 1
    reason = dialog.reasons[0]
    assert reason and len(reason) <= common.REASON_CHARS and "\n" not in reason


@pytest.mark.parametrize("touch", ["cancel", "timeout", "unavailable", "error", "hang"])
@pytest.mark.parametrize("idx", range(len(IDS)), ids=IDS)
def test_a_failed_fingerprint_or_password_step_does_nothing_and_logs_nothing(
        idx, touch, google, install_dir, dialog, a_file, capsys):
    _, mod, argv, responses, key, _ = cases(a_file)[idx]
    fake = google(responses)
    dialog.touch = touch
    with pytest.raises(SystemExit) as e:
        mod.main(argv)
    assert e.value.code == 1
    assert dialog.order == ["details", "touch"]
    assert key not in fake.keys()
    assert not (install_dir / "actions.log").exists()
    assert "nothing was done" in capsys.readouterr().err


@pytest.mark.parametrize("idx", range(len(IDS)), ids=IDS)
def test_dry_run_never_asks_for_a_fingerprint(idx, google, install_dir, dialog, a_file):
    _, mod, argv, responses, key, _ = cases(a_file)[idx]
    google(responses)
    mod.main(argv + ["--dry-run"])
    assert dialog.order == []


# ------------------------------------------------------------------ the fingerprint sheet names WHO or WHERE

REASON_MUST_NAME = {
    "mail send": ["x@test.invalid +1 more", "Contract"],
    "mail trash": ["sender@test.invalid", "Quarterly numbers"],
    "mail untrash": ["sender@test.invalid", "Quarterly numbers"],
    "chat send": ["spaces/A", "PRIVATE-WORDS"],
    "chat dm": ["guest@test.invalid", "PRIVATE-WORDS"],
    "drive upload": ["FOLDERIDXXXXXXXXXX", "a.txt"],
    "drive update": ["Contract.pdf", "a.txt"],
    "drive share": ["guest@test.invalid", "Plan.pdf"],
    "drive share anyone": ["ANYONE WITH LINK", "Plan.pdf"],
    "calendar create": ["a@test.invalid +1 more", "Kickoff"],
    "calendar create alone": ["Kickoff"],
    "calendar move": ["a@test.invalid +1 more", "Board sync"],
    "calendar cancel": ["a@test.invalid +1 more", "Board sync"],
    "calendar cancel alone": ["Board sync"],
}


def test_every_gated_action_has_a_who_or_where_expectation():
    assert set(REASON_MUST_NAME) == set(IDS)


@pytest.mark.parametrize("idx", range(len(IDS)), ids=IDS)
def test_the_reason_passed_to_the_helper_names_who_or_where(idx, google, install_dir, dialog, a_file):
    name, mod, argv, responses, key, _ = cases(a_file)[idx]
    google(responses)
    mod.main(argv)
    reason = dialog.reasons[0]
    for part in REASON_MUST_NAME[name]:
        assert part in reason, (part, reason)
    assert len(reason) <= common.REASON_CHARS


def test_a_long_subject_is_cut_first_and_the_address_is_never_cut():
    r = common.reason_for("send mail to", common.first_of(["a.person@test.invalid", "b@test.invalid"]), "S" * 300)
    assert r.startswith("send mail to a.person@test.invalid +1 more: S") and r.endswith("...")
    assert len(r) <= common.REASON_CHARS
    r = common.reason_for("send mail to", "w" * 70 + "@test.invalid", "A long subject here")
    assert ("w" * 70 + "@test.invalid") in r and len(r) <= common.REASON_CHARS      # the detail goes, not the address
    assert common.reason_for("move event", "", "Board", link=" with ") == "move event: Board"
    assert common.reason_for("move event", "a@test.invalid", "Board", link=" with ") == "move event with a@test.invalid: Board"
    assert common.first_of([]) == "" and common.first_of(["a@test.invalid"]) == "a@test.invalid"


def test_a_real_send_with_a_long_subject_still_names_the_first_recipient(google, install_dir, dialog):
    draft = json.loads(json.dumps(DRAFT))
    for h in draft["message"]["payload"]["headers"]:
        if h["name"] == "Subject":
            h["value"] = "Z" * 250
    google({"users.drafts.get": draft, "users.drafts.send": {"id": "s1"}})
    svc_mail.main(["send", "r7"])
    assert dialog.reasons[0].startswith("send mail to x@test.invalid +1 more: ZZ")
    assert len(dialog.reasons[0]) <= common.REASON_CHARS
