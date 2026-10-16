#!/usr/bin/env python3
"""bertie-calendar: Google Calendar for the signed-in account. Times are local.

Free (read only):
    bertie-calendar agenda [--from 2026-10-15] [--to 2026-10-17]   (default: today)
    bertie-calendar freebusy 2026-10-15T10:00 2026-10-15T18:00 [<email> ...]
    bertie-calendar event <event-id>

Asks first (Claude Code shows an approval prompt):
    bertie-calendar create --title "Sync" --start 2026-10-15T15:00 --end 2026-10-15T15:30 \\
        [--attendees a@x.com b@y.com] [--meet]
    bertie-calendar move <event-id> --start ... --end ...
    bertie-calendar cancel <event-id>

When an event has attendees, Google EMAILS them (invites, changes, cancellations).
--dry-run says so and lists who. Times without an offset are read as local time.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import common  # noqa: E402
from common import die  # noqa: E402



def service(write: bool = False):
    return auth.build_service("calendar", "v3", write=write)


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2))


# ---------------------------------------------------------------- helpers

def _rfc(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _event_when(ev: Dict[str, Any]) -> str:
    s, e = ev.get("start", {}), ev.get("end", {})
    if "dateTime" in s and "dateTime" in e:
        return common.fmt_range(common.parse_rfc3339(s["dateTime"]), common.parse_rfc3339(e["dateTime"]))
    if "date" in s:
        return f"all day {s['date']}"
    return "(no time)"


def _attendee_emails(ev: Dict[str, Any], skip_self: bool = True) -> List[str]:
    out = []
    for a in ev.get("attendees", []) or []:
        if skip_self and a.get("self"):
            continue
        if a.get("email"):
            out.append(a["email"])
    return out


def _window(args) -> tuple:
    now = common.to_local(common.now_utc())
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = common.parse_when(args.from_) if args.from_ else midnight
    if args.to:
        end = common.parse_when(args.to, end_of_day=True)
    elif args.from_:
        end = start + timedelta(days=1)
    else:
        end = midnight + timedelta(days=1)
    if end <= start:
        die("--to must be after --from")
    return start, end


# ---------------------------------------------------------------- free reads

def cmd_agenda(args) -> None:
    start, end = _window(args)
    res = service().events().list(
        calendarId="primary", timeMin=_rfc(start), timeMax=_rfc(end), singleEvents=True,
        orderBy="startTime", maxResults=args.max, timeZone=common.ZONE_NAME).execute()
    rows = []
    for ev in res.get("items", []):
        rows.append({"id": ev["id"], "title": ev.get("summary", "(no title)"),
                     "when": _event_when(ev), "start": ev.get("start", {}),
                     "attendees": len(ev.get("attendees", []) or []),
                     "meet": ev.get("hangoutLink", ""), "status": ev.get("status", "")})
    if args.json:
        _print_json(rows)
        return
    print(f"{common.fmt_local(start)} to {common.fmt_local(end)}")
    if not rows:
        print("nothing scheduled")
        return
    for r in rows:
        extra = f"  ({r['attendees']} attendees)" if r["attendees"] else ""
        meet = "  [Meet]" if r["meet"] else ""
        print(f"{r['when']:<28} {r['title']}{extra}{meet}")
        print(f"    id: {r['id']}")


def cmd_freebusy(args) -> None:
    start = common.parse_when(args.start)
    end = common.parse_when(args.end)
    if end <= start:
        die("the end must be after the start")
    ids = args.emails or ["primary"]
    res = service().freebusy().query(body={
        "timeMin": _rfc(start), "timeMax": _rfc(end), "timeZone": common.ZONE_NAME,
        "items": [{"id": i} for i in ids]}).execute()
    cals = res.get("calendars", {})
    if args.json:
        _print_json(cals)
        return
    print(f"{common.fmt_local(start)} to {common.fmt_local(end)}")
    for cid in ids:
        c = cals.get(cid, {})
        if c.get("errors"):
            reason = c["errors"][0].get("reason", "error")
            print(f"{cid}: cannot see this calendar ({reason})")
            continue
        busy = c.get("busy", [])
        if not busy:
            print(f"{cid}: free")
            continue
        print(f"{cid}: busy")
        for b in busy:
            print("    " + common.fmt_range(common.parse_rfc3339(b["start"]), common.parse_rfc3339(b["end"])))


def cmd_event(args) -> None:
    ev = service().events().get(calendarId="primary", eventId=args.event_id).execute()
    if args.json:
        _print_json(ev)
        return
    print(f"{ev.get('summary', '(no title)')}   [{ev.get('status', '')}]")
    print(f"when:      {_event_when(ev)}")
    if ev.get("location"):
        print(f"where:     {ev['location']}")
    if ev.get("hangoutLink"):
        print(f"meet:      {ev['hangoutLink']}")
    org = ev.get("organizer", {}).get("email")
    if org:
        print(f"organiser: {org}")
    for a in ev.get("attendees", []) or []:
        print(f"attendee:  {a.get('email', '?')}  ({a.get('responseStatus', '?')})")
    if ev.get("description"):
        print()
        print(ev["description"])
    if ev.get("htmlLink"):
        print()
        print(ev["htmlLink"])


# ---------------------------------------------------------------- approval-gated

def build_event_body(title: str, start: datetime, end: datetime, attendees: List[str],
                     meet: bool, description: str = "", location: str = "") -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": _rfc(start), "timeZone": common.ZONE_NAME},
        "end": {"dateTime": _rfc(end), "timeZone": common.ZONE_NAME},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    if meet:
        body["conferenceData"] = {"createRequest": {
            "requestId": uuid.uuid4().hex,
            "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    return body


def send_updates_for(attendees: List[str], no_invites: bool = False) -> str:
    return "all" if attendees and not no_invites else "none"


def _invite_line(attendees: List[str], send_updates: str, verb: str) -> str:
    if attendees and send_updates == "all":
        return f"  {verb} will be emailed to: {', '.join(attendees)}"
    if attendees:
        return f"  attendees ({', '.join(attendees)}) will NOT be emailed (--no-invites)"
    return "  no attendees, so no email is sent"


def _invite_text(attendees: List[str], send_updates: str, what: str) -> str:
    """One plain sentence for the confirmation window. The attendees
    themselves go in their own list row, shown in full."""
    if attendees and send_updates == "all":
        return f"Google WILL EMAIL {what} to every attendee listed"
    if attendees:
        return "attendees are NOT emailed (--no-invites)"
    return "no attendees, no email is sent"


def cmd_create(args) -> None:
    start = common.parse_when(args.start)
    end = common.parse_when(args.end)
    if end <= start:
        die("--end must be after --start")
    attendees = common.addresses(args.attendees)
    for a in attendees:
        if not common.is_email(a):
            die(f"'{a}' is not an email address")
    su = send_updates_for(attendees, args.no_invites)
    body = build_event_body(args.title, start, end, attendees, args.meet,
                            args.description or "", args.location or "")
    svc = service(write=True)
    if args.dry_run:
        print("DRY RUN - would create this event. Nothing was created.")
        print(f"  title: {args.title}")
        print(f"  when:  {common.fmt_range(start, end)}")
        if args.location:
            print(f"  where: {args.location}")
        print(f"  Google Meet link: {'yes' if args.meet else 'no'}")
        print(_invite_line(attendees, su, "invites"))
        return
    common.confirm("Create", "Create this calendar event?", [
        ("Title", args.title), ("When", common.fmt_range(start, end)), ("Where", args.location or ""),
        ("Google Meet link", "yes" if args.meet else "no"),
        ("Invites", _invite_text(attendees, su, "invitations")), ("Attendees", attendees),
    ], args.description or "", reason=common.reason_for("create event", common.first_of(attendees), args.title, link=" with "))
    ev = svc.events().insert(
        calendarId="primary", body=body, sendUpdates=su,
        conferenceDataVersion=1 if args.meet else 0).execute()
    common.log_action("calendar", "create", attendees, args.title, ev["id"])
    if args.json:
        _print_json({"id": ev["id"], "htmlLink": ev.get("htmlLink", ""),
                     "meet": ev.get("hangoutLink", "")})
        return
    print(f"created: {args.title}  {common.fmt_range(start, end)}")
    print(f"id: {ev['id']}")
    if ev.get("hangoutLink"):
        print(f"meet: {ev['hangoutLink']}")
    if ev.get("htmlLink"):
        print(ev["htmlLink"])
    if su == "all":
        print(f"invites emailed to: {', '.join(attendees)}")


def cmd_move(args) -> None:
    start = common.parse_when(args.start)
    end = common.parse_when(args.end)
    if end <= start:
        die("--end must be after --start")
    svc = service(write=True)
    ev = svc.events().get(calendarId="primary", eventId=args.event_id).execute()
    attendees = _attendee_emails(ev)
    su = send_updates_for(attendees, args.no_invites)
    title = ev.get("summary", "(no title)")
    if args.dry_run:
        print("DRY RUN - would move this event. Nothing was changed.")
        print(f"  title: {title}")
        print(f"  now:   {_event_when(ev)}")
        print(f"  to:    {common.fmt_range(start, end)}")
        print(_invite_line(attendees, su, "the update"))
        return
    common.confirm("Move", "Move this calendar event?", [
        ("Title", title), ("Now", _event_when(ev)), ("New time", common.fmt_range(start, end)),
        ("Update emails", _invite_text(attendees, su, "the update")), ("Attendees", attendees),
    ], reason=common.reason_for("move event", common.first_of(attendees), title, link=" with "))
    body = {"start": {"dateTime": _rfc(start), "timeZone": common.ZONE_NAME},
            "end": {"dateTime": _rfc(end), "timeZone": common.ZONE_NAME}}
    res = svc.events().patch(calendarId="primary", eventId=args.event_id, body=body,
                             sendUpdates=su).execute()
    common.log_action("calendar", "move", attendees, title, res.get("id", args.event_id))
    print(f"moved: {title}  {common.fmt_range(start, end)}")
    if su == "all":
        print(f"update emailed to: {', '.join(attendees)}")


def cmd_cancel(args) -> None:
    svc = service(write=True)
    ev = svc.events().get(calendarId="primary", eventId=args.event_id).execute()
    attendees = _attendee_emails(ev)
    su = send_updates_for(attendees, args.no_invites)
    title = ev.get("summary", "(no title)")
    if args.dry_run:
        print("DRY RUN - would CANCEL this event. Nothing was changed.")
        print(f"  title: {title}")
        print(f"  when:  {_event_when(ev)}")
        print(_invite_line(attendees, su, "the cancellation"))
        return
    common.confirm("Cancel event", "Cancel this calendar event?", [
        ("Title", title), ("When", _event_when(ev)),
        ("Cancellation emails", _invite_text(attendees, su, "the cancellation")), ("Attendees", attendees),
    ], reason=common.reason_for("cancel event", common.first_of(attendees), title, link=" with "))
    # Calendar's cancel is events.delete: Google keeps the event in the calendar
    # Bin for a while. It is the only call Calendar offers for cancelling.
    svc.events().delete(calendarId="primary", eventId=args.event_id, sendUpdates=su).execute()
    common.log_action("calendar", "cancel", attendees, title, args.event_id)
    print(f"cancelled: {title}  ({_event_when(ev)})")
    if su == "all":
        print(f"cancellation emailed to: {', '.join(attendees)}")


def smoke() -> str:
    svc = service()
    now = common.to_local(common.now_utc())
    svc.events().list(calendarId="primary", timeMin=_rfc(now), maxResults=1,
                      singleEvents=True).execute()
    return "Calendar: OK"


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    top_p, common_p, wr = common.option_parents()
    ap = argparse.ArgumentParser(prog=common.registry.command_for("calendar"), parents=[top_p], description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("agenda", parents=[common_p], help="what is on the calendar (default today, local time)")
    a.add_argument("--from", dest="from_", help="start: 2026-10-15 or 2026-10-15T09:00 (local time)")
    a.add_argument("--to", help="end: a date includes that whole day")
    a.add_argument("--max", type=int, default=50)

    f = sub.add_parser("freebusy", parents=[common_p], help="when are these people busy")
    f.add_argument("start")
    f.add_argument("end")
    f.add_argument("emails", nargs="*", help="default: your own calendar")

    e = sub.add_parser("event", parents=[common_p], help="show one event")
    e.add_argument("event_id")

    c = sub.add_parser("create", parents=[common_p, wr], help="ASKS FIRST: create an event")
    c.add_argument("--title", required=True)
    c.add_argument("--start", required=True)
    c.add_argument("--end", required=True)
    c.add_argument("--attendees", nargs="+", help="emails; Google sends them invites")
    c.add_argument("--meet", action="store_true", help="add a Google Meet link")
    c.add_argument("--description")
    c.add_argument("--location")
    c.add_argument("--no-invites", action="store_true",
                   help="add attendees but do NOT email them")

    m = sub.add_parser("move", parents=[common_p, wr], help="ASKS FIRST: change an event's time")
    m.add_argument("event_id")
    m.add_argument("--start", required=True)
    m.add_argument("--end", required=True)
    m.add_argument("--no-invites", action="store_true")

    x = sub.add_parser("cancel", parents=[common_p, wr], help="ASKS FIRST: cancel an event")
    x.add_argument("event_id")
    x.add_argument("--no-invites", action="store_true")
    return ap


COMMANDS = {"agenda": cmd_agenda, "freebusy": cmd_freebusy, "event": cmd_event,
            "create": cmd_create, "move": cmd_move, "cancel": cmd_cancel}


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    COMMANDS[args.cmd](args)


if __name__ == "__main__":
    main()
