"""bertie-mail: reply threading, drafts, send-only-a-draft, Bin only, no body in the log."""
import base64
import email
import email.policy
import json
from pathlib import Path

import pytest

import svc_mail
from conftest import ACCOUNT, OTHER, read_log


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def msg(mid="m1", thread="t1", headers=None, parts=None, payload_extra=None, snippet=""):
    hdrs = [{"name": k, "value": v} for k, v in (headers or {}).items()]
    payload = {"headers": hdrs, "mimeType": "text/plain", "body": {"data": b64("hello body")}}
    if parts is not None:
        payload = {"headers": hdrs, "mimeType": "multipart/mixed", "parts": parts}
    return {"id": mid, "threadId": thread, "payload": payload, "snippet": snippet}


ORIGINAL = msg(headers={
    "From": "Sender Person <sender@test.invalid>",
    "To": ACCOUNT,
    "Subject": "Quarterly numbers",
    "Date": "Wed, 14 Oct 2026 08:15:00 +0000",
    "Message-ID": "<orig-1@test.invalid>",
    "References": "<root-0@test.invalid>",
})


def decode_raw(raw: str):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
                                    policy=email.policy.default)


# ------------------------------------------------------------------ reply threading

def test_reply_headers_thread_correctly():
    rh = svc_mail.reply_headers(ORIGINAL, ACCOUNT)
    assert rh["subject"] == "Re: Quarterly numbers"
    assert rh["in_reply_to"] == "<orig-1@test.invalid>"
    assert rh["references"] == "<root-0@test.invalid> <orig-1@test.invalid>"
    assert rh["thread_id"] == "t1"
    assert rh["to"] == "Sender Person <sender@test.invalid>"


def test_reply_subject_is_not_doubled():
    o = msg(headers={"From": "a@test.invalid", "Subject": "RE: already", "Message-ID": "<x@y>"})
    assert svc_mail.reply_headers(o, ACCOUNT)["subject"] == "RE: already"


def test_reply_prefers_reply_to_header():
    o = msg(headers={"From": "a@test.invalid", "Reply-To": "desk@test.invalid", "Subject": "s",
                     "Message-ID": "<x@y>"})
    assert svc_mail.reply_headers(o, ACCOUNT)["to"] == "desk@test.invalid"


def test_reply_to_our_own_message_goes_to_the_original_recipient():
    o = msg(headers={"From": ACCOUNT, "To": "them@test.invalid", "Subject": "s", "Message-ID": "<x@y>"})
    assert svc_mail.reply_headers(o, ACCOUNT)["to"] == "them@test.invalid"


def test_reply_without_references_starts_the_chain():
    o = msg(headers={"From": "a@test.invalid", "Subject": "s", "Message-ID": "<x@y>"})
    assert svc_mail.reply_headers(o, ACCOUNT)["references"] == "<x@y>"


def test_draft_reply_end_to_end(google, install_dir, capsys):
    fake = google({
        "users.messages.get": ORIGINAL,
        "users.drafts.create": {"id": "r99", "message": {"threadId": "t1"}},
    })
    svc_mail.main(["draft", "--reply-to", "m1", "--body", "Thanks, noted."])
    (create,) = fake.called("users.drafts.create")
    message = create["body"]["message"]
    assert message["threadId"] == "t1"
    m = decode_raw(message["raw"])
    assert m["To"] == "sender@test.invalid"            # the address, without the display name
    assert m["Subject"] == "Re: Quarterly numbers"
    assert m["In-Reply-To"] == "<orig-1@test.invalid>"
    assert m["References"] == "<root-0@test.invalid> <orig-1@test.invalid>"
    assert "Thanks, noted." in m.get_content()
    assert "draft id: r99" in capsys.readouterr().out
    assert "users.drafts.send" not in fake.keys()           # a draft is not a send


def test_draft_with_cc_bcc_attachment_and_body_file(google, install_dir, tmp_path):
    fake = google({"users.drafts.create": {"id": "r5", "message": {}}})
    att = tmp_path / "report.pdf"
    att.write_bytes(b"%PDF-1.4 fake")
    body = tmp_path / "body.txt"
    body.write_text("Line one\nLine two\n")
    svc_mail.main(["draft", "--to", "a@test.invalid,b@test.invalid", "--cc", "c@test.invalid",
                   "--bcc", "d@test.invalid", "--subject", "Hello", "--body-file", str(body),
                   "--attach", str(att)])
    (create,) = fake.called("users.drafts.create")
    assert "threadId" not in create["body"]["message"]
    m = decode_raw(create["body"]["message"]["raw"])
    assert m["To"] == "a@test.invalid, b@test.invalid"
    assert m["Cc"] == "c@test.invalid" and m["Bcc"] == "d@test.invalid"
    assert m["Subject"] == "Hello"
    names = [p.get_filename() for p in m.iter_attachments()]
    assert names == ["report.pdf"]
    assert "Line two" in m.get_body().get_content()


def test_draft_needs_exactly_one_body_source_and_a_recipient(google, install_dir, tmp_path):
    google({})
    with pytest.raises(SystemExit):
        svc_mail.main(["draft", "--to", "a@test.invalid", "--subject", "s"])
    with pytest.raises(SystemExit):
        svc_mail.main(["draft", "--to", "a@test.invalid", "--subject", "s", "--body", "x",
                       "--body-file", str(tmp_path / "f")])
    with pytest.raises(SystemExit):
        svc_mail.main(["draft", "--subject", "s", "--body", "x"])
    with pytest.raises(SystemExit):
        svc_mail.main(["draft", "--to", "a@test.invalid", "--body", "x"])


def test_header_injection_is_refused(google, install_dir):
    google({})
    with pytest.raises(SystemExit):
        svc_mail.main(["draft", "--to", "a@test.invalid", "--subject", "hi\nBcc: evil@test.invalid",
                       "--body", "x"])


def test_draft_dry_run_creates_nothing(google, install_dir, capsys):
    fake = google({})
    svc_mail.main(["draft", "--to", "a@test.invalid", "--subject", "S", "--body", "hello", "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "a@test.invalid" in out and "hello" in out
    assert not any(k.startswith("users.drafts") for k in fake.keys())
    assert read_log(install_dir) == []


# ------------------------------------------------------------------ send

DRAFT = {"id": "r7", "message": {"id": "dm1", "threadId": "t9", "payload": {
    "headers": [{"name": "To", "value": "x@test.invalid"}, {"name": "Cc", "value": "y@test.invalid"},
                {"name": "Subject", "value": "Contract"}],
    "mimeType": "text/plain", "body": {"data": b64("SECRET-BODY-TEXT")}}}}


def test_send_takes_a_draft_id_only_and_has_no_text_option():
    sub = svc_mail.build_parser()._subparsers._group_actions[0].choices["send"]
    flags = {o for a in sub._actions for o in a.option_strings}
    assert flags <= {"-h", "--help", "--json", "--dry-run"}
    with pytest.raises(SystemExit):
        svc_mail.main(["send", "--to", "a@test.invalid", "--body", "hi"])


def test_send_dry_run_shows_the_draft_and_sends_nothing(google, install_dir, capsys):
    fake = google({"users.drafts.get": DRAFT})
    svc_mail.main(["send", "r7", "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "x@test.invalid" in out and "Contract" in out and "SECRET-BODY-TEXT" in out
    assert "users.drafts.send" not in fake.keys()
    assert read_log(install_dir) == []


def test_send_sends_the_existing_draft_and_logs_without_the_body(google, install_dir, capsys):
    fake = google({"users.drafts.get": DRAFT, "users.drafts.send": {"id": "sent1", "threadId": "t9"}})
    svc_mail.main(["send", "r7"])
    (call,) = fake.called("users.drafts.send")
    assert call["body"] == {"id": "r7"}
    (rec,) = read_log(install_dir)
    assert rec["tool"] == "mail" and rec["action"] == "send" and rec["id"] == "sent1"
    assert rec["target"] == "x@test.invalid, y@test.invalid" and rec["subject"] == "Contract"
    assert "SECRET-BODY-TEXT" not in (install_dir / "actions.log").read_text()


def test_draft_log_never_contains_the_body(google, install_dir):
    google({"users.drafts.create": {"id": "r8", "message": {}}})
    svc_mail.main(["draft", "--to", "a@test.invalid", "--subject", "Plan", "--body", "TOP-SECRET-BODY"])
    text = (install_dir / "actions.log").read_text()
    assert "TOP-SECRET-BODY" not in text
    (rec,) = read_log(install_dir)
    assert rec["action"] == "draft" and rec["subject"] == "Plan" and rec["id"] == "r8"


# ------------------------------------------------------------------ Bin, never delete

def test_trash_dry_run_then_real(google, install_dir, capsys):
    fake = google({"users.messages.get": ORIGINAL, "users.messages.trash": {"id": "m1"},
                   "users.messages.untrash": {"id": "m1"}})
    svc_mail.main(["trash", "m1", "--dry-run"])
    assert "DRY RUN" in capsys.readouterr().out
    assert "users.messages.trash" not in fake.keys()
    svc_mail.main(["trash", "m1"])
    assert fake.called("users.messages.trash") == [{"userId": "me", "id": "m1"}]
    svc_mail.main(["untrash", "m1"])
    assert fake.called("users.messages.untrash") == [{"userId": "me", "id": "m1"}]
    assert [r["action"] for r in read_log(install_dir)] == ["trash", "untrash"]


def test_there_is_no_way_to_delete_permanently():
    src = Path(svc_mail.__file__).read_text()
    assert ".delete(" not in src and "batchDelete" not in src
    assert ".messages().send(" not in src                  # no direct send either
    choices = set(svc_mail.build_parser()._subparsers._group_actions[0].choices)
    assert not any("delete" in c or "purge" in c or "empty" in c for c in choices)


# ------------------------------------------------------------------ account check

@pytest.mark.parametrize("argv,extra", [
    (["send", "r7"], {"users.drafts.get": DRAFT}),
    (["trash", "m1"], {"users.messages.get": ORIGINAL}),
    (["untrash", "m1"], {"users.messages.get": ORIGINAL}),
    (["draft", "--to", "a@test.invalid", "--subject", "s", "--body", "b"], {}),
])
def test_wrong_account_aborts_every_mail_write(google, install_dir, capsys, argv, extra):
    fake = google(extra, email=OTHER)
    with pytest.raises(SystemExit) as e:
        svc_mail.main(argv)
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "wrong account" in err and ACCOUNT in err and OTHER in err
    assert fake.keys() == []                                # nothing at all was called
    assert read_log(install_dir) == []


def test_account_check_ignores_letter_case(google, install_dir):
    google({"users.drafts.create": {"id": "r1", "message": {}}}, email=ACCOUNT.upper())
    svc_mail.main(["draft", "--to", "a@test.invalid", "--subject", "s", "--body", "b"])


# ------------------------------------------------------------------ reads

def test_search_shows_ist(google, install_dir, capsys, monkeypatch):
    import common
    from datetime import datetime, timezone
    monkeypatch.setattr(common, "now_utc", lambda: datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc))
    google({"users.messages.list": {"messages": [{"id": "m1"}]}, "users.messages.get": ORIGINAL})
    svc_mail.main(["search", "from:x"])
    out = capsys.readouterr().out
    assert "m1  13:45 IST" in out and "Quarterly numbers" in out


def test_json_flag_works_before_the_subcommand(google, install_dir, capsys):
    google({"users.messages.list": {"messages": [{"id": "m1"}]}, "users.messages.get": ORIGINAL})
    svc_mail.main(["--json", "search", "x"])
    assert json.loads(capsys.readouterr().out)[0]["id"] == "m1"


def test_read_lists_attachment_names(google, install_dir, capsys):
    parts = [{"mimeType": "text/plain", "body": {"data": b64("see attached")}},
             {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "A1", "size": 2048}}]
    google({"users.messages.get": msg(headers={"Subject": "s", "From": "a@test.invalid"}, parts=parts)})
    svc_mail.main(["read", "m1"])
    out = capsys.readouterr().out
    assert "see attached" in out and "attachments: a.pdf" in out


def test_thread_prints_every_message(google, install_dir, capsys):
    google({"users.threads.get": {"messages": [ORIGINAL, msg(mid="m2", headers={"Subject": "Re: Quarterly numbers"})]}})
    svc_mail.main(["thread", "t1"])
    out = capsys.readouterr().out
    assert "message 1 of 2" in out and "message 2 of 2" in out


def test_attachments_download_safely(google, install_dir, tmp_path, capsys):
    parts = [{"mimeType": "application/pdf", "filename": "../../evil.pdf",
              "body": {"attachmentId": "A1", "size": 3}},
             {"mimeType": "text/plain", "filename": "note.txt", "body": {"data": b64("inline"), "size": 6}}]
    fake = google({"users.messages.get": msg(parts=parts),
                   "users.messages.attachments.get": {"data": b64("PDFDATA")}})
    out_dir = tmp_path / "dl"
    svc_mail.main(["attachments", "m1", "--out", str(out_dir)])
    svc_mail.main(["attachments", "m1", "--out", str(out_dir)])       # second time: no overwrite
    files = {p.name for p in out_dir.iterdir()}
    assert files == {".._.._evil.pdf", ".._.._evil (1).pdf", "note.txt", "note (1).txt"}
    assert not (tmp_path / "evil.pdf").exists()                    # the ../ did not escape
    assert (out_dir / "note.txt").read_text() == "inline"


def test_drafts_and_show_draft(google, install_dir, capsys):
    google({"users.drafts.list": {"drafts": [{"id": "r7"}]}, "users.drafts.get": DRAFT})
    svc_mail.main(["drafts"])
    assert "r7" in capsys.readouterr().out
    svc_mail.main(["show-draft", "r7"])
    out = capsys.readouterr().out
    assert "x@test.invalid" in out and "y@test.invalid" in out and "Contract" in out and "SECRET-BODY-TEXT" in out


# ------------------------------------------------------------------ replies keep their subject

def test_reply_with_a_different_subject_is_refused(google, install_dir, capsys):
    fake = google({"users.messages.get": ORIGINAL, "users.drafts.create": {"id": "r1", "message": {}}})
    with pytest.raises(SystemExit):
        svc_mail.main(["draft", "--reply-to", "m1", "--subject", "Something else", "--body", "x"])
    assert "--new-thread" in capsys.readouterr().err
    assert "users.drafts.create" not in fake.keys()


@pytest.mark.parametrize("subject", ["Quarterly numbers", "Re: Quarterly numbers", "RE: re: quarterly NUMBERS"])
def test_reply_with_the_same_subject_apart_from_re_is_fine(google, install_dir, subject):
    fake = google({"users.messages.get": ORIGINAL, "users.drafts.create": {"id": "r1", "message": {}}})
    svc_mail.main(["draft", "--reply-to", "m1", "--subject", subject, "--body", "x"])
    assert "users.drafts.create" in fake.keys()


def test_new_thread_allows_a_different_subject(google, install_dir):
    fake = google({"users.messages.get": ORIGINAL, "users.drafts.create": {"id": "r1", "message": {}}})
    svc_mail.main(["draft", "--reply-to", "m1", "--subject", "Brand new subject", "--new-thread", "--body", "x"])
    m = decode_raw(fake.called("users.drafts.create")[0]["body"]["message"]["raw"])
    assert m["Subject"] == "Brand new subject" and m["In-Reply-To"] == "<orig-1@test.invalid>"
