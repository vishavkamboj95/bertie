#!/usr/bin/env python3
"""bertie-chat: Google Chat as the signed-in user. Times are local.

Free (read only):
    bertie-chat spaces
    bertie-chat read spaces/AAAA1234 --max 20
    bertie-chat read <email> --max 20

Asks first (Claude Code shows an approval prompt):
    bertie-chat send spaces/AAAA1234 "build is green"
    bertie-chat dm <email> "rotation finished"

Messages come FROM the person, not from a bot, and cannot be unsent. Every
send supports --dry-run, which prints exactly what would go out and sends nothing.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import common  # noqa: E402
from common import die  # noqa: E402


def service(write: bool = False):
    return auth.build_service("chat", "v1", write=write)


def _http_error():
    from googleapiclient.errors import HttpError
    return HttpError


def cmd_spaces(args) -> None:
    res = service().spaces().list(pageSize=100).execute()
    out = [{"name": s["name"], "type": s.get("spaceType", ""),
            "display": s.get("displayName", "(direct message)")}
           for s in res.get("spaces", [])]
    if args.json:
        print(json.dumps(out, indent=2))
        return
    if not out:
        print("no spaces visible to this account")
        return
    for s in out:
        print(f"{s['name']:<28} {s['type']:<16} {s['display']}")


def _send(svc, parent: Optional[str], text: str, dry_run: bool, as_json: bool, attach=None,
          log_target: Optional[str] = None, log_action_name: str = "send",
          create_dm_with: Optional[str] = None) -> None:
    """Confirm, then (if parent is None) create the DM space with `create_dm_with`,
    then send. Nothing changes before the window is answered."""
    to_label = log_target or parent
    attach_path = common.plain_file(attach, "attach") if attach else None
    if dry_run:
        print(f"DRY RUN - would send to {parent or to_label} as the signed-in person. Nothing was sent.")
        print(f"  {text}")
        if attach_path is not None:
            print(f"  [attachment: {attach_path}]")
        return

    body: Dict[str, Any] = {"text": text}
    common.confirm("Send", "Post this Chat message now? It appears as written by the account below "
                   "and cannot be unsent.", [
        ("To", to_label), ("Attachment", str(attach_path) if attach_path else "none"),
        ("New conversation", f"a direct-message space with {create_dm_with} is created first"
         if create_dm_with else ""),
    ], text, reason=common.reason_for("Chat message to", to_label, text))
    if parent is None:
        space = svc.spaces().setup(body={
            "space": {"spaceType": "DIRECT_MESSAGE"},
            "memberships": [{"member": {"name": create_dm_with, "type": "HUMAN"}}],
        }).execute()
        parent = space["name"]
    if attach_path is not None:
        from googleapiclient.http import MediaFileUpload
        p = attach_path
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        ref = svc.media().upload(
            parent=parent, body={"filename": p.name},
            media_body=MediaFileUpload(str(p), mimetype=mime, resumable=False)).execute()
        body["attachment"] = [{"attachmentDataRef": ref["attachmentDataRef"]}]

    msg = svc.spaces().messages().create(parent=parent, body=body).execute()
    common.log_action("chat", log_action_name, log_target or parent, "", msg.get("name", ""))
    rec = {"sent": True, "space": parent, "message": msg.get("name"), "text": text}
    print(json.dumps(rec, indent=2) if as_json else f"sent to {parent}: {msg.get('name')}")


def cmd_send(args) -> None:
    svc = service(write=True)
    if args.attach:
        common.plain_file(args.attach, "attach")
    _send(svc, args.space, args.text, args.dry_run, args.json, args.attach, args.space, "send")


def cmd_dm(args) -> None:
    """Find the existing DM with a person, creating one only if none exists."""
    svc = service(write=True)
    HttpError = _http_error()
    target = args.email if args.email.startswith("users/") else f"users/{args.email}"
    try:
        space = svc.spaces().findDirectMessage(name=target).execute()
        parent = space["name"]
    except HttpError as e:
        if e.resp.status != 404:
            die(f"could not find DM with {args.email}: {e}")
        if args.dry_run:
            print(f"DRY RUN - no DM exists yet with {args.email}; would create one and send. "
                  "Nothing was sent.")
            print(f"  {args.text}")
            return
        parent = None                     # _send creates the space, after the window
    _send(svc, parent, args.text, args.dry_run, args.json, args.attach, args.email, "dm",
          create_dm_with=target)


def _resolve(svc, target: str) -> str:
    """Accept a space id or a person's email and return a space resource name."""
    if target.startswith("spaces/"):
        return target
    who = target if target.startswith("users/") else f"users/{target}"
    try:
        return svc.spaces().findDirectMessage(name=who).execute()["name"]
    except _http_error() as e:
        die(f"no direct message found with {target}: {e}")


def self_id(svc) -> str:
    """Work out this account's own Chat user id, and remember it.

    Chat returns membership ids with no displayName, so the only way to say
    "you" instead of users/1234 is to know which id is ours. Our id is the one
    present in every space we belong to, so intersect a few memberships.
    Cached, because it never changes."""
    cache = common.HOME / f"self-{common.configured_account()}.json"
    if cache.exists():
        return json.loads(cache.read_text())["id"]

    common_ids = None
    for sp in svc.spaces().list(pageSize=6).execute().get("spaces", []):
        try:
            ms = svc.spaces().members().list(parent=sp["name"], pageSize=50).execute()
        except _http_error():
            continue
        ids = {m["member"]["name"] for m in ms.get("memberships", [])
               if m.get("member", {}).get("type") == "HUMAN"}
        common_ids = ids if common_ids is None else (common_ids & ids)
        if common_ids is not None and len(common_ids) == 1:
            break

    if not common_ids or len(common_ids) != 1:
        return ""  # cosmetic only - never worth failing a read over
    me = common_ids.pop()
    cache.write_text(json.dumps({"id": me}))
    cache.chmod(0o600)
    return me


def cmd_read(args) -> None:
    svc = service()
    parent = _resolve(svc, args.target)

    names: Dict[str, str] = {}
    me = self_id(svc)
    if me:
        names[me] = "you"
    if not args.target.startswith("spaces/"):
        other = args.target.replace("users/", "")
        for m in svc.spaces().members().list(parent=parent, pageSize=10).execute().get("memberships", []):
            uid = m.get("member", {}).get("name", "")
            if uid and uid != me:
                names[uid] = other

    res = svc.spaces().messages().list(
        parent=parent, pageSize=args.max, orderBy="createTime desc").execute()
    msgs = list(reversed(res.get("messages", [])))
    out = []
    for m in msgs:
        snd = m.get("sender", {})
        out.append({
            "time": common.fmt_api_time(m.get("createTime", "")),
            "from": snd.get("displayName") or names.get(snd.get("name", ""), snd.get("name", "?")),
            "text": m.get("text", ""),
        })

    if args.json:
        print(json.dumps(out, indent=2))
        return
    if not out:
        print("no messages")
        return
    for m in out:
        print(f"[{m['time']}] {m['from']}: {m['text']}")


def smoke() -> str:
    service().spaces().list(pageSize=1).execute()
    return "Chat: OK"


def build_parser() -> argparse.ArgumentParser:
    top_p, common_p, wr = common.option_parents()
    ap = argparse.ArgumentParser(prog=common.registry.command_for("chat"), parents=[top_p], description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("spaces", parents=[common_p], help="list spaces and DMs this account can see")

    s = sub.add_parser("send", parents=[common_p, wr], help="ASKS FIRST: send a message to a space")
    s.add_argument("space", help="e.g. spaces/AAAA1234")
    s.add_argument("text")
    s.add_argument("--attach", help="path to a file to attach")

    d = sub.add_parser("dm", parents=[common_p, wr], help="ASKS FIRST: send a direct message")
    d.add_argument("email")
    d.add_argument("text")
    d.add_argument("--attach", help="path to a file to attach")

    r = sub.add_parser("read", parents=[common_p], help="read a thread: a space id or a person's email")
    r.add_argument("target", help="spaces/AAAA1234 or a colleague's email")
    r.add_argument("--max", type=int, default=20)
    return ap


COMMANDS = {"spaces": cmd_spaces, "send": cmd_send, "dm": cmd_dm, "read": cmd_read}


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    COMMANDS[args.cmd](args)


if __name__ == "__main__":
    main()
