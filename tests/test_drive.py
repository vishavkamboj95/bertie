"""bertie-drive: query escaping, sharing refusals, no delete, overwrite warnings."""
from pathlib import Path

import pytest

import svc_drive
from conftest import ACCOUNT, OTHER, read_log


# ------------------------------------------------------------------ the q-string bug

@pytest.mark.parametrize("raw,escaped", [
    ("plain", "plain"),
    ("O'Brien", "O\\'Brien"),
    ("a\\b", "a\\\\b"),
    ("it's a\\'trap", "it\\'s a\\\\\\'trap"),            # backslash first, then the quote
    ("''", "\\'\\'"),
])
def test_escape_q(raw, escaped):
    assert svc_drive.escape_q(raw) == escaped


def test_list_query_with_an_apostrophe_is_well_formed():
    q = svc_drive.list_query("Anna's report")
    assert q == "name contains 'Anna\\'s report' and trashed=false"
    assert svc_drive.list_query(None) == "trashed=false"
    assert svc_drive.list_query("") == "trashed=false"


def test_list_sends_the_escaped_query(google, install_dir, capsys):
    fake = google({"files.list": {"files": []}})
    svc_drive.main(["list", "Anna's \\ file"])
    (call,) = fake.called("files.list")
    assert call["q"] == "name contains 'Anna\\'s \\\\ file' and trashed=false"
    assert "no files" in capsys.readouterr().out


def test_file_id_from_urls():
    assert svc_drive.file_id("1AbCdEfGhIjKl") == "1AbCdEfGhIjKl"
    assert svc_drive.file_id("https://drive.google.com/drive/folders/1RpfJDpAbCdEf?usp=sharing") == "1RpfJDpAbCdEf"
    assert svc_drive.file_id("https://docs.google.com/document/d/1ZzYyXxWwVvUu/edit") == "1ZzYyXxWwVvUu"
    assert svc_drive.file_id("https://drive.google.com/open?id=1QqPpOoNnMmLl") == "1QqPpOoNnMmLl"


def test_modified_time_is_shown_in_ist(google, install_dir, capsys, monkeypatch):
    import common
    from datetime import datetime, timezone
    monkeypatch.setattr(common, "now_utc", lambda: datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc))
    google({"files.list": {"files": [{"id": "F1", "name": "n.txt", "mimeType": "text/plain",
                                      "size": "2048", "modifiedTime": "2026-10-14T08:15:00.000Z"}]}})
    svc_drive.main(["ls", "FOLDERIDXXXXXXXXXX"])
    assert "13:45 IST" in capsys.readouterr().out


# ------------------------------------------------------------------ sharing

def test_anyone_with_the_link_is_refused_without_the_flag(google, install_dir, capsys):
    fake = google({})
    with pytest.raises(SystemExit):
        svc_drive.main(["share", "F1", "anyone", "--role", "reader"])
    assert "--anyone" in capsys.readouterr().err
    assert fake.keys() == []


def test_anyone_dry_run_is_loud_and_changes_nothing(google, install_dir, capsys):
    fake = google({"files.get": {"id": "F1", "name": "Plan.pdf"}})
    svc_drive.main(["share", "F1", "anyone", "--role", "reader", "--anyone", "--dry-run"])
    out = capsys.readouterr().out
    assert "WARNING" in out and "ANYONE WITH THE LINK" in out and "NO sign-in" in out
    assert "permissions.create" not in fake.keys()
    assert read_log(install_dir) == []


def test_anyone_share_when_allowed(google, install_dir):
    fake = google({"files.get": {"id": "F1", "name": "Plan.pdf"}, "permissions.create": {"id": "p1"}})
    svc_drive.main(["share", "F1", "anyone", "--role", "reader", "--anyone"])
    (call,) = fake.called("permissions.create")
    assert call["body"] == {"type": "anyone", "role": "reader"}
    assert "sendNotificationEmail" not in call
    (rec,) = read_log(install_dir)
    assert rec["target"] == "anyone-with-link" and rec["subject"] == "Plan.pdf"


def test_anyone_flag_with_an_email_is_a_contradiction(google, install_dir):
    google({})
    with pytest.raises(SystemExit):
        svc_drive.main(["share", "F1", "someone@test.invalid", "--role", "reader", "--anyone"])


def test_share_with_a_person(google, install_dir, capsys):
    fake = google({"files.get": {"id": "F1", "name": "Plan.pdf"}, "permissions.create": {"id": "p1"}})
    svc_drive.main(["share", "F1", "guest@elsewhere.invalid", "--role", "commenter", "--dry-run"])
    out = capsys.readouterr().out
    assert "guest@elsewhere.invalid as commenter" in out and "OUTSIDE test.invalid" in out
    assert "notification email will be sent" in out
    svc_drive.main(["share", "F1", "guest@elsewhere.invalid", "--role", "commenter"])
    (call,) = fake.called("permissions.create")
    assert call["body"] == {"type": "user", "role": "commenter", "emailAddress": "guest@elsewhere.invalid"}
    assert call["sendNotificationEmail"] is True
    assert read_log(install_dir)[0]["target"] == "guest@elsewhere.invalid"


def test_no_notify(google, install_dir):
    fake = google({"files.get": {"id": "F1", "name": "n"}, "permissions.create": {"id": "p1"}})
    svc_drive.main(["share", "F1", "guest@test.invalid", "--role", "reader", "--no-notify"])
    assert fake.called("permissions.create")[0]["sendNotificationEmail"] is False


def test_bad_role_or_address_is_refused(google, install_dir):
    google({})
    with pytest.raises(SystemExit):
        svc_drive.main(["share", "F1", "guest@test.invalid", "--role", "owner"])
    with pytest.raises(SystemExit):
        svc_drive.main(["share", "F1", "not-an-address", "--role", "reader"])


# ------------------------------------------------------------------ upload / update

def test_upload_dry_run_then_real(google, install_dir, tmp_path, capsys):
    f = tmp_path / "r.pdf"
    f.write_bytes(b"x" * 2048)
    fake = google({"files.create": {"id": "N1", "name": "r.pdf", "webViewLink": "https://example.invalid/N1"}})
    svc_drive.main(["upload", str(f), "--folder", "FOLDERIDXXXXXXXXXX", "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "FOLDERIDXXXXXXXXXX" in out and "2.0kb" in out
    assert fake.keys() == []
    svc_drive.main(["upload", str(f), "--folder", "FOLDERIDXXXXXXXXXX", "--name", "renamed.pdf"])
    (call,) = fake.called("files.create")
    assert call["body"] == {"name": "renamed.pdf", "parents": ["FOLDERIDXXXXXXXXXX"]}
    (rec,) = read_log(install_dir)
    assert rec["action"] == "upload" and rec["id"] == "N1" and rec["target"] == "FOLDERIDXXXXXXXXXX"


def test_update_overwrites_only_after_showing_what_changes(google, install_dir, tmp_path, capsys):
    f = tmp_path / "v2.pdf"
    f.write_bytes(b"y" * 4096)
    meta = {"id": "F1", "name": "Contract.pdf", "mimeType": "application/pdf", "size": "1024",
            "modifiedTime": "2026-10-01T08:00:00Z"}
    fake = google({"files.get": meta, "files.update": {"id": "F1", "name": "Contract.pdf"}})
    svc_drive.main(["update", "F1", str(f), "--dry-run"])
    out = capsys.readouterr().out
    assert "OVERWRITE" in out and "Contract.pdf" in out and "4.0kb" in out
    assert "files.update" not in fake.keys()
    svc_drive.main(["update", "F1", str(f)])
    assert fake.called("files.update")[0]["fileId"] == "F1"
    (rec,) = read_log(install_dir)
    assert rec["action"] == "update" and rec["target"] == "F1" and rec["subject"] == "Contract.pdf"


def test_update_refuses_google_native_files(google, install_dir, tmp_path):
    f = tmp_path / "v2.docx"
    f.write_bytes(b"z")
    fake = google({"files.get": {"id": "F1", "name": "Doc", "mimeType": "application/vnd.google-apps.document"}})
    with pytest.raises(SystemExit):
        svc_drive.main(["update", "F1", str(f)])
    assert "files.update" not in fake.keys()


@pytest.mark.parametrize("argv", [
    ["upload", "{f}"], ["update", "F1", "{f}"], ["share", "F1", "guest@test.invalid", "--role", "reader"]])
def test_wrong_account_aborts_every_drive_write(google, install_dir, tmp_path, capsys, argv):
    f = tmp_path / "a.txt"
    f.write_text("a")
    fake = google({}, email=OTHER)
    with pytest.raises(SystemExit):
        svc_drive.main([a.replace("{f}", str(f)) for a in argv])
    assert "wrong account" in capsys.readouterr().err
    assert fake.keys() == []


# ------------------------------------------------------------------ download and the rest

def test_download_cannot_escape_the_target_folder(google, install_dir, tmp_path, monkeypatch):
    import googleapiclient.http as http

    class FakeDL:
        def __init__(self, buf, req):
            buf.write(b"DATA")

        def next_chunk(self):
            return None, True

    monkeypatch.setattr(http, "MediaIoBaseDownload", FakeDL)
    google({"files.get": {"name": "../../outside.txt", "mimeType": "text/plain"}, "files.get_media": object()})
    out = tmp_path / "dl"
    svc_drive.main(["download", "FILEIDXXXXXXXXXX", "--out", str(out)])
    assert [p.name for p in out.iterdir()] == [".._.._outside.txt"]
    assert not (tmp_path / "outside.txt").exists()


def test_no_delete_or_trash_and_no_false_docstring():
    src = Path(svc_drive.__file__).read_text()
    assert ".delete(" not in src
    assert "trashed" in src                                        # only inside the q filters
    assert "emptyTrash" not in src and "trash(" not in src.replace("trashed", "")
    assert "writes only files this tool created" not in src
    choices = set(svc_drive.build_parser()._subparsers._group_actions[0].choices)
    assert choices == {"list", "ls", "download", "upload", "update", "share"}


def test_a_second_download_does_not_overwrite_the_first(google, install_dir, tmp_path, monkeypatch):
    import googleapiclient.http as http

    class FakeDL:
        def __init__(self, buf, req):
            buf.write(b"DATA")

        def next_chunk(self):
            return None, True

    monkeypatch.setattr(http, "MediaIoBaseDownload", FakeDL)
    google({"files.get": {"name": "report.pdf", "mimeType": "application/pdf"}, "files.get_media": object()})
    out = tmp_path / "dl"
    svc_drive.main(["download", "FILEIDXXXXXXXXXX", "--out", str(out)])
    (out / "report.pdf").write_bytes(b"MINE")
    svc_drive.main(["download", "FILEIDXXXXXXXXXX", "--out", str(out)])
    assert (out / "report.pdf").read_bytes() == b"MINE"
    assert sorted(p.name for p in out.iterdir()) == ["report (1).pdf", "report.pdf"]
