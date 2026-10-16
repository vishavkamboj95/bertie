"""bertie-calendar and bertie-chat."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

import common
import svc_calendar
import svc_chat
from conftest import ACCOUNT, OTHER, read_log

NOW = datetime(2026, 10, 14, 8, 15, tzinfo=timezone.utc)       # 13:45 IST, 14 Oct 2026


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(common, "now_utc", lambda: NOW)


EVENT = {"id": "ev1", "summary": "Board sync", "status": "confirmed",
         "start": {"dateTime": "2026-10-15T10:00:00+05:30"}, "end": {"dateTime": "2026-10-15T10:30:00+05:30"},
         "attendees": [{"email": ACCOUNT, "self": True}, {"email": "a@test.invalid"}, {"email": "b@test.invalid"}]}
SOLO = dict(EVENT, id="ev2", attendees=[])


# ------------------------------------------------------------------ calendar reads

def test_agenda_defaults_to_today_in_ist(google, install_dir, capsys):
    fake = google({"events.list": {"items": [dict(EVENT, start={"dateTime": "2026-10-14T09:30:00Z"},
                                                  end={"dateTime": "2026-10-14T10:00:00Z"})]}})
    svc_calendar.main(["agenda"])
    (call,) = fake.called("events.list")
    assert call["timeMin"] == "2026-10-14T00:00:00+05:30"
    assert call["timeMax"] == "2026-10-15T00:00:00+05:30"
    out = capsys.readouterr().out
    assert "15:00-15:30 IST" in out and "Board sync" in out and "(3 attendees)" in out


def test_agenda_with_a_date_range_includes_the_last_day(google, install_dir):
    fake = google({"events.list": {"items": []}})
    svc_calendar.main(["agenda", "--from", "2026-10-15", "--to", "2026-10-16"])
    (call,) = fake.called("events.list")
    assert call["timeMin"] == "2026-10-15T00:00:00+05:30"
    assert call["timeMax"] == "2026-10-17T00:00:00+05:30"


def test_freebusy_shows_ist_and_unshared_calendars(google, install_dir, capsys):
    fake = google({"freebusy.query": {"calendars": {
        "a@test.invalid": {"busy": [{"start": "2026-10-15T04:30:00Z", "end": "2026-10-15T05:00:00Z"}]},
        "b@test.invalid": {"errors": [{"reason": "notFound"}]},
        "c@test.invalid": {"busy": []}}}})
    svc_calendar.main(["freebusy", "2026-10-15T09:00", "2026-10-15T18:00",
                       "a@test.invalid", "b@test.invalid", "c@test.invalid"])
    out = capsys.readouterr().out
    assert "15 Oct 10:00-10:30 IST" in out
    assert "b@test.invalid: cannot see this calendar (notFound)" in out
    assert "c@test.invalid: free" in out
    assert fake.called("freebusy.query")[0]["body"]["timeZone"] == "Asia/Kolkata"


def test_event_shows_attendees(google, install_dir, capsys):
    google({"events.get": dict(EVENT, hangoutLink="https://meet.example.invalid/x")})
    svc_calendar.main(["event", "ev1"])
    out = capsys.readouterr().out
    assert "Board sync" in out and "a@test.invalid" in out and "meet.example.invalid" in out


# ------------------------------------------------------------------ calendar writes

def test_create_with_attendees_emails_invites_and_says_so(google, install_dir, capsys):
    fake = google({"events.insert": {"id": "new1", "htmlLink": "https://cal.example.invalid/new1"}})
    argv = ["create", "--title", "Kickoff", "--start", "2026-10-15T15:00", "--end", "2026-10-15T15:30",
            "--attendees", "a@test.invalid", "b@test.invalid"]
    svc_calendar.main(argv + ["--dry-run"])
    out = capsys.readouterr().out
    assert "invites will be emailed to: a@test.invalid, b@test.invalid" in out
    assert "15 Oct 15:00-15:30 IST" in out
    assert fake.keys() == []
    svc_calendar.main(argv)
    (call,) = fake.called("events.insert")
    assert call["sendUpdates"] == "all"
    assert call["body"]["attendees"] == [{"email": "a@test.invalid"}, {"email": "b@test.invalid"}]
    assert call["body"]["start"] == {"dateTime": "2026-10-15T15:00:00+05:30", "timeZone": "Asia/Kolkata"}
    assert call["conferenceDataVersion"] == 0
    (rec,) = read_log(install_dir)
    assert rec["tool"] == "calendar" and rec["action"] == "create" and rec["id"] == "new1"
    assert rec["subject"] == "Kickoff" and rec["target"] == "a@test.invalid, b@test.invalid"


def test_create_without_attendees_sends_no_updates_and_says_so(google, install_dir, capsys):
    fake = google({"events.insert": {"id": "new2"}})
    svc_calendar.main(["create", "--title", "Focus", "--start", "2026-10-15T15:00", "--end", "2026-10-15T16:00",
                       "--dry-run"])
    assert "no attendees, so no email is sent" in capsys.readouterr().out
    svc_calendar.main(["create", "--title", "Focus", "--start", "2026-10-15T15:00", "--end", "2026-10-15T16:00"])
    assert fake.called("events.insert")[0]["sendUpdates"] == "none"


def test_create_no_invites_option(google, install_dir, capsys):
    fake = google({"events.insert": {"id": "new3"}})
    svc_calendar.main(["create", "--title", "T", "--start", "2026-10-15T15:00", "--end", "2026-10-15T16:00",
                       "--attendees", "a@test.invalid", "--no-invites", "--dry-run"])
    assert "will NOT be emailed" in capsys.readouterr().out
    svc_calendar.main(["create", "--title", "T", "--start", "2026-10-15T15:00", "--end", "2026-10-15T16:00",
                       "--attendees", "a@test.invalid", "--no-invites"])
    assert fake.called("events.insert")[0]["sendUpdates"] == "none"


def test_create_with_meet_asks_for_a_conference(google, install_dir):
    fake = google({"events.insert": {"id": "new4", "hangoutLink": "https://meet.example.invalid/q"}})
    svc_calendar.main(["create", "--title", "T", "--start", "2026-10-15T15:00", "--end", "2026-10-15T16:00",
                       "--meet"])
    (call,) = fake.called("events.insert")
    assert call["conferenceDataVersion"] == 1
    cd = call["body"]["conferenceData"]["createRequest"]
    assert cd["conferenceSolutionKey"] == {"type": "hangoutsMeet"} and cd["requestId"]


def test_create_validates_times_and_addresses(google, install_dir):
    google({})
    with pytest.raises(SystemExit):
        svc_calendar.main(["create", "--title", "T", "--start", "2026-10-15T16:00", "--end", "2026-10-15T15:00"])
    with pytest.raises(SystemExit):
        svc_calendar.main(["create", "--title", "T", "--start", "2026-10-15T15:00", "--end", "2026-10-15T16:00",
                           "--attendees", "nope"])


def test_move_tells_who_will_be_emailed(google, install_dir, capsys):
    fake = google({"events.get": EVENT, "events.patch": {"id": "ev1"}})
    svc_calendar.main(["move", "ev1", "--start", "2026-10-16T10:00", "--end", "2026-10-16T10:30", "--dry-run"])
    out = capsys.readouterr().out
    assert "the update will be emailed to: a@test.invalid, b@test.invalid" in out
    assert "15 Oct 10:00-10:30 IST" in out and "16 Oct 10:00-10:30 IST" in out   # now / to
    assert "events.patch" not in fake.keys()
    svc_calendar.main(["move", "ev1", "--start", "2026-10-16T10:00", "--end", "2026-10-16T10:30"])
    call = fake.called("events.patch")[0]
    assert call["sendUpdates"] == "all" and call["eventId"] == "ev1"
    assert read_log(install_dir)[0]["action"] == "move"


def test_cancel_dry_run_then_real(google, install_dir, capsys):
    fake = google({"events.get": EVENT, "events.delete": {}})
    svc_calendar.main(["cancel", "ev1", "--dry-run"])
    assert "the cancellation will be emailed to: a@test.invalid, b@test.invalid" in capsys.readouterr().out
    assert "events.delete" not in fake.keys()
    svc_calendar.main(["cancel", "ev1"])
    assert fake.called("events.delete")[0]["sendUpdates"] == "all"
    assert read_log(install_dir)[0]["action"] == "cancel"


def test_cancel_of_a_private_event_sends_nothing(google, install_dir):
    fake = google({"events.get": SOLO, "events.delete": {}})
    svc_calendar.main(["cancel", "ev2"])
    assert fake.called("events.delete")[0]["sendUpdates"] == "none"


@pytest.mark.parametrize("argv", [
    ["create", "--title", "T", "--start", "2026-10-15T15:00", "--end", "2026-10-15T16:00"],
    ["move", "ev1", "--start", "2026-10-16T10:00", "--end", "2026-10-16T10:30"],
    ["cancel", "ev1"],
])
def test_wrong_account_aborts_every_calendar_write(google, install_dir, capsys, argv):
    fake = google({}, email=OTHER)
    with pytest.raises(SystemExit):
        svc_calendar.main(argv)
    assert "wrong account" in capsys.readouterr().err
    assert fake.keys() == []


def test_only_one_delete_call_exists_and_it_is_the_cancel():
    src = Path(svc_calendar.__file__).read_text()
    assert src.count(".delete(") == 1
    assert src.index(".delete(") > src.index("def cmd_cancel")


# ------------------------------------------------------------------ chat

def test_chat_send_dry_run_then_real(google, install_dir, capsys):
    fake = google({"spaces.messages.create": {"name": "spaces/A/messages/1"}})
    svc_chat.main(["send", "spaces/A", "PRIVATE-WORDS", "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "PRIVATE-WORDS" in out and "as the signed-in person" in out
    assert fake.keys() == []
    svc_chat.main(["send", "spaces/A", "PRIVATE-WORDS"])
    assert fake.called("spaces.messages.create")[0]["body"] == {"text": "PRIVATE-WORDS"}
    (rec,) = read_log(install_dir)
    assert rec["tool"] == "chat" and rec["action"] == "send" and rec["target"] == "spaces/A"
    assert rec["id"] == "spaces/A/messages/1"
    assert "PRIVATE-WORDS" not in (install_dir / "actions.log").read_text()


def test_chat_dm_finds_the_existing_space(google, install_dir):
    fake = google({"spaces.findDirectMessage": {"name": "spaces/DM1"},
                   "spaces.messages.create": {"name": "spaces/DM1/messages/9"}})
    svc_chat.main(["dm", "guest@test.invalid", "hello"])
    assert fake.called("spaces.findDirectMessage")[0]["name"] == "users/guest@test.invalid"
    assert fake.called("spaces.messages.create")[0]["parent"] == "spaces/DM1"
    rec = read_log(install_dir)[0]
    assert rec["action"] == "dm" and rec["target"] == "guest@test.invalid"


def test_chat_dm_creates_a_space_only_when_none_exists(google, install_dir, capsys):
    from googleapiclient.errors import HttpError
    import httplib2
    nf = HttpError(httplib2.Response({"status": 404}), b'{"error": {"message": "not found"}}')
    fake = google({"spaces.findDirectMessage": nf, "spaces.setup": {"name": "spaces/NEW"},
                   "spaces.messages.create": {"name": "spaces/NEW/messages/1"}})
    svc_chat.main(["dm", "guest@test.invalid", "hello", "--dry-run"])
    assert "would create one" in capsys.readouterr().out
    assert "spaces.setup" not in fake.keys()
    svc_chat.main(["dm", "guest@test.invalid", "hello"])
    assert "spaces.setup" in fake.keys()


@pytest.mark.parametrize("argv", [["send", "spaces/A", "hi"], ["dm", "guest@test.invalid", "hi"]])
def test_wrong_account_aborts_chat_writes(google, install_dir, capsys, argv):
    fake = google({}, email=OTHER)
    with pytest.raises(SystemExit):
        svc_chat.main(argv)
    assert fake.keys() == []


def test_chat_read_shows_ist_and_names(google, install_dir, capsys):
    google({
        "spaces.list": {"spaces": [{"name": "spaces/DM1"}]},
        "spaces.members.list": {"memberships": [{"member": {"name": "users/1", "type": "HUMAN"}},
                                                {"member": {"name": "users/2", "type": "HUMAN"}}]},
        "spaces.findDirectMessage": {"name": "spaces/DM1"},
        "spaces.messages.list": {"messages": [
            {"createTime": "2026-10-14T08:00:00Z", "sender": {"name": "users/2"}, "text": "second"},
            {"createTime": "2026-10-13T08:00:00Z", "sender": {"name": "users/1"}, "text": "first"}]},
    })
    (install_dir / "self-acct@test.invalid.json").write_text('{"id": "users/1"}')
    svc_chat.main(["read", "guest@test.invalid"])
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[13 Oct 13:30 IST] you: first"
    assert out[1] == "[13:30 IST] guest@test.invalid: second"
