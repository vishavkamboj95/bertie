"""Local-time formatting, the action log, and the rule that bodies never reach the log.

conftest pins the zone to Asia/Kolkata, so "IST" in these assertions is that zone's short name."""
import inspect
import json
import os
import stat
from datetime import datetime, timezone

import pytest

import common
from conftest import ACCOUNT, read_log

NOW = datetime(2026, 10, 14, 8, 15, tzinfo=timezone.utc)      # 13:45 IST on 14 Oct 2026


def test_today_shows_time_only_in_ist():
    assert common.fmt_local(NOW, now=NOW) == "13:45 IST"


def test_other_day_gets_a_date():
    other = datetime(2026, 10, 15, 8, 15, tzinfo=timezone.utc)
    assert common.fmt_local(other, now=NOW) == "15 Oct 13:45 IST"


def test_other_year_gets_the_year():
    other = datetime(2027, 1, 2, 8, 15, tzinfo=timezone.utc)
    assert common.fmt_local(other, now=NOW) == "2 Jan 2027 13:45 IST"


def test_today_is_judged_in_ist_not_utc():
    # 20:00 UTC on the 14th is 01:30 IST on the 15th: not "today" at 13:45 IST on the 14th
    late = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
    assert common.fmt_local(late, now=NOW) == "15 Oct 01:30 IST"


def test_naive_datetimes_are_read_as_utc():
    assert common.fmt_local(datetime(2026, 10, 14, 8, 15), now=NOW) == "13:45 IST"


def test_range_same_day_and_across_days():
    s = datetime(2026, 10, 14, 8, 15, tzinfo=timezone.utc)
    e = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    assert common.fmt_range(s, e, now=NOW) == "13:45-14:30 IST"
    nxt = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    assert common.fmt_range(s, nxt, now=NOW) == "13:45 IST to 15 Oct 14:30 IST"


def test_email_date_header_is_converted():
    assert common.fmt_header_date("Wed, 14 Oct 2026 08:15:00 +0000", now=NOW) == "13:45 IST"
    assert common.fmt_header_date("not a date") == "not a date"


def test_api_timestamp_is_converted():
    assert common.fmt_api_time("2026-10-14T08:15:00Z", now=NOW) == "13:45 IST"
    assert common.fmt_api_time("2026-10-14T13:45:00.123+05:30", now=NOW) == "13:45 IST"


def test_parse_when_defaults_to_ist():
    assert common.parse_when("2026-10-15T14:00").utcoffset().total_seconds() == 5.5 * 3600
    assert common.parse_when("2026-10-15 14:00").hour == 14
    assert common.parse_when("2026-10-15T14:00Z").utcoffset().total_seconds() == 0
    assert common.parse_when("2026-10-15T14:00+01:00").utcoffset().total_seconds() == 3600


def test_parse_when_date_only_and_end_of_day():
    a = common.parse_when("2026-10-15")
    b = common.parse_when("2026-10-15", end_of_day=True)
    assert (a.hour, a.day) == (0, 15) and (b.hour, b.day) == (0, 16)


def test_parse_when_rejects_nonsense(capsys):
    with pytest.raises(SystemExit):
        common.parse_when("next tuesday-ish")
    with pytest.raises(SystemExit):
        common.parse_when("2026-13-45")


# ------------------------------------------------------------------ the action log

def test_log_line_fields_and_mode(install_dir):
    common.log_action("mail", "send", ["a@test.invalid", "b@test.invalid"], "Subject here", "m123")
    path = install_dir / "actions.log"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    (rec,) = read_log(install_dir)
    assert set(rec) == {"ts", "account", "tool", "action", "target", "subject", "id"}
    assert rec["tool"] == "mail" and rec["action"] == "send" and rec["id"] == "m123"
    assert rec["target"] == "a@test.invalid, b@test.invalid"
    assert rec["account"] == ACCOUNT
    datetime.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%SZ")         # UTC, Z suffix


def test_log_timestamp_is_utc_not_ist(install_dir, monkeypatch):
    monkeypatch.setattr(common, "now_utc", lambda: NOW)         # 08:15 UTC = 13:45 IST
    common.log_action("mail", "draft", "a@test.invalid", "S", "d1")
    (rec,) = read_log(install_dir)
    assert rec["ts"] == "2026-10-14T08:15:00Z"


def test_log_function_has_no_body_parameter():
    params = set(inspect.signature(common.log_action).parameters)
    assert not any("body" in p or "text" in p or "content" in p for p in params)


def test_log_appends_one_line_each(install_dir):
    for i in range(3):
        common.log_action("chat", "send", "spaces/X", "", f"id{i}")
    lines = (install_dir / "actions.log").read_text().splitlines()
    assert len(lines) == 3 and all(json.loads(l) for l in lines)


def test_log_keeps_existing_file_private(install_dir):
    p = install_dir / "actions.log"
    p.write_text("")
    os.chmod(p, 0o644)
    common.log_action("chat", "send", "spaces/X", "", "i")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_read_log_is_newest_first_and_skips_junk(install_dir):
    p = install_dir / "actions.log"
    p.write_text('{"ts":"2026-10-14T08:00:00Z","tool":"mail","action":"draft","id":"1"}\n'
                 "not json\n"
                 '{"ts":"2026-10-14T09:00:00Z","tool":"mail","action":"send","id":"2"}\n')
    recs = common.read_log(10)
    assert [r["id"] for r in recs] == ["2", "1"]


def test_log_command_shows_ist_newest_first(install_dir, capsys, monkeypatch):
    import google_cli
    monkeypatch.setattr(common, "now_utc", lambda: NOW)         # 13:45 IST on 14 Oct 2026
    p = install_dir / "actions.log"
    p.write_text('{"ts":"2026-10-13T08:00:00Z","tool":"mail","action":"draft","target":"a@test.invalid","subject":"First","id":"1"}\n'
                 '{"ts":"2026-10-14T06:30:00Z","tool":"drive","action":"upload","target":"my-drive","subject":"Report","id":"2"}\n')
    google_cli.main(["log"])
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("12:00 IST  drive upload")          # today: time only, newest first
    assert out[1].startswith("13 Oct 13:30 IST  mail draft")     # earlier day: with the date
    assert "Z" not in out[0] and "T06" not in out[0]             # the UTC form is not shown


def test_log_command_on_empty_log(install_dir, capsys):
    import google_cli
    google_cli.main(["log"])
    assert "empty" in capsys.readouterr().out


# ------------------------------------------------------------------ the Mac's own time zone

def test_times_follow_the_configured_zone_with_its_own_short_name(monkeypatch):
    monkeypatch.setattr(common, "ZONE_NAME", "Europe/London")
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert common.fmt_local(summer, now=summer) == "13:00 BST"
    assert common.fmt_local(winter, now=winter) == "12:00 GMT"
    assert common.parse_when("2026-07-01T13:00") == summer        # no offset: read in the local zone


def test_the_zone_comes_from_tz_then_the_system_link_then_utc(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    assert common._system_zone_name() == "America/New_York"
    monkeypatch.setenv("TZ", ":Europe/Paris")                      # the leading colon form
    assert common._system_zone_name() == "Europe/Paris"
    monkeypatch.delenv("TZ")
    monkeypatch.setattr(common.os, "readlink", lambda p: "/var/db/timezone/zoneinfo/Asia/Tokyo")
    assert common._system_zone_name() == "Asia/Tokyo"
    monkeypatch.setattr(common.os, "readlink", lambda p: "/somewhere/odd")
    assert common._system_zone_name() == "UTC"
    monkeypatch.setenv("TZ", "Not/A_Zone")
    assert common._system_zone_name() == "UTC"                     # never a guess


def test_calendar_requests_carry_the_configured_zone_name(monkeypatch):
    import svc_calendar
    monkeypatch.setattr(common, "ZONE_NAME", "Europe/London")
    start = common.parse_when("2026-07-01T15:00")
    body = svc_calendar.build_event_body("T", start, start, [], False)
    assert body["start"]["timeZone"] == "Europe/London"
