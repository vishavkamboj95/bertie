#!/usr/bin/env python3
"""bertie-mail: Gmail for the signed-in account.

Free (read only):
    bertie-mail whoami
    bertie-mail search "from:aws is:unread" --max 10
    bertie-mail read <message-id>
    bertie-mail thread <thread-id>
    bertie-mail attachments <message-id> --out DIR
    bertie-mail drafts
    bertie-mail show-draft <draft-id>

Free (only creates a draft, sends nothing):
    bertie-mail draft --to a@x.com --subject "Hi" --body "text" [--attach FILE ...]
    bertie-mail draft --reply-to <message-id> --body-file reply.txt

Asks first (Claude Code shows an approval prompt, and a Mac window shows exactly
what will happen; only a click on its action button goes ahead):
    bertie-mail send <draft-id>        sends ONLY an existing draft
    bertie-mail trash <message-id>     moves to the Bin
    bertie-mail untrash <message-id>   takes it back out of the Bin

There is no permanent delete anywhere. Every write supports --dry-run, checks
the signed-in account first, and adds one line to the action log (never the body).
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import common  # noqa: E402
from common import die  # noqa: E402

TOOL = "mail"
HEADER_NAMES = ["From", "To", "Cc", "Reply-To", "Subject", "Date",
                "Message-ID", "Message-Id", "References", "In-Reply-To"]


def service(write: bool = False):
    return auth.build_service("gmail", "v1", write=write)


# ---------------------------------------------------------------- parsing

def _header(msg: Dict[str, Any], name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _body(payload: Dict[str, Any]) -> str:
    """Walk the MIME tree and prefer text/plain, falling back to text/html."""
    plain: List[str] = []
    html: List[str] = []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and not part.get("filename"):
            text = _b64(data).decode("utf-8", "replace")
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                html.append(text)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(plain) or "\n".join(html)


def _attachments(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(part):
        fn = part.get("filename")
        body = part.get("body", {}) or {}
        if fn and (body.get("attachmentId") or body.get("data")):
            out.append({"filename": fn, "mime": part.get("mimeType", ""),
                        "size": body.get("size", 0),
                        "attachmentId": body.get("attachmentId"),
                        "data": body.get("data")})
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return out


def _when(msg: Dict[str, Any]) -> str:
    return common.fmt_header_date(_header(msg, "Date"))


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2))


# ---------------------------------------------------------------- free reads

def cmd_whoami(args) -> None:
    creds = auth.credentials()
    p = auth._discovery_build("gmail", "v1", creds).users().getProfile(userId="me").execute()
    want = common.configured_account()
    match = p["emailAddress"].lower() == want.lower()
    if args.json:
        _print_json({**p, "configured": want, "match": match})
        return
    print(f"{p['emailAddress']}  ({p['messagesTotal']} messages)")
    print(f"configured account: {want}  ->  {'match' if match else 'MISMATCH - writes will be refused'}")


def cmd_search(args) -> None:
    svc = service()
    res = svc.users().messages().list(userId="me", q=args.query, maxResults=args.max).execute()
    out = []
    for ref in res.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute()
        out.append({"id": msg["id"], "threadId": msg.get("threadId", ""),
                    "date": _when(msg), "from": _header(msg, "From"),
                    "subject": _header(msg, "Subject"), "snippet": msg.get("snippet", "")})
    if args.json:
        _print_json(out)
        return
    if not out:
        print("no matches")
        return
    for m in out:
        print(f"{m['id']}  {m['date']}")
        print(f"  from: {m['from']}")
        print(f"  subj: {m['subject']}")
        print(f"  {m['snippet'][:160]}")
        print()


def _record(msg: Dict[str, Any]) -> Dict[str, Any]:
    payload = msg.get("payload", {})
    return {
        "id": msg["id"], "threadId": msg.get("threadId", ""),
        "date": _when(msg), "from": _header(msg, "From"), "to": _header(msg, "To"),
        "cc": _header(msg, "Cc"), "subject": _header(msg, "Subject"),
        "body": _body(payload),
        "attachments": [a["filename"] for a in _attachments(payload)],
    }


def _print_record(rec: Dict[str, Any]) -> None:
    print(f"from: {rec['from']}\nto:   {rec['to']}")
    if rec.get("cc"):
        print(f"cc:   {rec['cc']}")
    print(f"date: {rec['date']}\nsubj: {rec['subject']}")
    if rec["attachments"]:
        print("attachments: " + ", ".join(rec["attachments"]))
    print()
    print(rec["body"])


def cmd_read(args) -> None:
    msg = service().users().messages().get(userId="me", id=args.message_id, format="full").execute()
    rec = _record(msg)
    if args.json:
        _print_json(rec)
    else:
        _print_record(rec)


def cmd_thread(args) -> None:
    th = service().users().threads().get(userId="me", id=args.thread_id, format="full").execute()
    recs = [_record(m) for m in th.get("messages", [])]
    if args.json:
        _print_json(recs)
        return
    if not recs:
        print("empty thread")
        return
    for i, rec in enumerate(recs, 1):
        print(f"===== message {i} of {len(recs)}  [{rec['id']}] =====")
        _print_record(rec)
        print()


def cmd_attachments(args) -> None:
    svc = service()
    msg = svc.users().messages().get(userId="me", id=args.message_id, format="full").execute()
    atts = _attachments(msg.get("payload", {}))
    if not atts:
        print("no attachments on that message")
        return
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for a in atts:
        data = a["data"]
        if not data:
            data = svc.users().messages().attachments().get(
                userId="me", messageId=args.message_id, id=a["attachmentId"]).execute()["data"]
        target = common.unique_path(out_dir / common.safe_filename(a["filename"]))
        target.write_bytes(_b64(data))
        saved.append({"saved": str(target), "size_kb": round(target.stat().st_size / 1024, 1)})
    if args.json:
        _print_json(saved)
    else:
        for s in saved:
            print(f"saved {s['saved']} ({s['size_kb']}kb)")


def cmd_drafts(args) -> None:
    svc = service()
    res = svc.users().drafts().list(userId="me", maxResults=args.max).execute()
    out = []
    for ref in res.get("drafts", []):
        d = svc.users().drafts().get(userId="me", id=ref["id"], format="metadata").execute()
        msg = d.get("message", {})
        out.append({"id": d["id"], "to": _header(msg, "To"),
                    "subject": _header(msg, "Subject"), "snippet": msg.get("snippet", "")})
    if args.json:
        _print_json(out)
        return
    if not out:
        print("no drafts")
        return
    for d in out:
        print(f"{d['id']}")
        print(f"  to:   {d['to']}")
        print(f"  subj: {d['subject']}")
        print(f"  {d['snippet'][:120]}")
        print()


def _draft_view(d: Dict[str, Any]) -> Dict[str, Any]:
    msg = d.get("message", {})
    payload = msg.get("payload", {})
    return {
        "id": d["id"], "threadId": msg.get("threadId", ""),
        "to": _header(msg, "To"), "cc": _header(msg, "Cc"), "bcc": _header(msg, "Bcc"),
        "subject": _header(msg, "Subject"), "body": _body(payload),
        "attachments": [f"{a['filename']} ({round(int(a['size'] or 0) / 1024, 1)}kb)"
                        for a in _attachments(payload)],
    }


def _people(header: str) -> List[str]:
    """Every address in a header, for the window. If the header holds text that
    does not parse as addresses, that text is shown as it is rather than hidden."""
    found = common.addresses([header]) if header else []
    return found or ([header] if header.strip() else [])


def _print_draft(v: Dict[str, Any]) -> None:
    print(f"draft: {v['id']}")
    print(f"to:      {v['to']}")
    print(f"cc:      {v['cc']}")
    print(f"bcc:     {v['bcc']}")
    print(f"subject: {v['subject']}")
    print("attachments: " + (", ".join(v["attachments"]) if v["attachments"] else "none"))
    print()
    print(v["body"])


def cmd_show_draft(args) -> None:
    d = service().users().drafts().get(userId="me", id=args.draft_id, format="full").execute()
    v = _draft_view(d)
    if args.json:
        _print_json(v)
    else:
        _print_draft(v)


# ---------------------------------------------------------------- draft (free write)

def reply_headers(orig: Dict[str, Any], account: str) -> Dict[str, str]:
    """Threading headers for a reply to `orig` (a message fetched with headers)."""
    mid = _header(orig, "Message-ID") or _header(orig, "Message-Id")
    refs = _header(orig, "References")
    subject = _header(orig, "Subject")
    if not re.match(r"(?i)^\s*re:", subject):
        subject = f"Re: {subject}".rstrip()
    sender = _header(orig, "Reply-To") or _header(orig, "From")
    from_addr = common.addresses([_header(orig, "From")])
    if from_addr and from_addr[0].lower() == account.lower():
        # replying to something we sent: the other party is the original To
        sender = _header(orig, "To") or sender
    return {
        "to": sender,
        "subject": subject,
        "in_reply_to": mid,
        "references": (refs + " " + mid).strip() if mid else refs,
        "thread_id": orig.get("threadId", ""),
    }


def _plain_subject(subject: str) -> str:
    """The subject without any leading 'Re:' prefixes, for comparing."""
    return re.sub(r"(?i)^(\s*re:\s*)+", "", subject or "").strip().lower()


def build_mime(to: List[str], cc: List[str], bcc: List[str], subject: str, body: str,
               attach: List[str], in_reply_to: str = "", references: str = "") -> EmailMessage:
    m = EmailMessage()
    if to:
        m["To"] = ", ".join(to)
    if cc:
        m["Cc"] = ", ".join(cc)
    if bcc:
        m["Bcc"] = ", ".join(bcc)
    m["Subject"] = subject
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    m.set_content(body)
    for path in attach:
        p = common.plain_file(path, "attach")
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        maintype, _, subtype = mime.partition("/")
        m.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype or "octet-stream",
                         filename=p.name)
    return m


def cmd_draft(args) -> None:
    to = common.addresses(args.to)
    cc = common.addresses(args.cc)
    bcc = common.addresses(args.bcc)

    if bool(args.body) == bool(args.body_file):
        die("give the text with exactly one of --body or --body-file")
    if args.body_file:
        bp = Path(args.body_file).expanduser()
        if not bp.is_file():
            die(f"no such file: {bp}")
        body = bp.read_text(encoding="utf-8")
    else:
        body = args.body

    svc = service(write=True)
    account = common.configured_account()
    in_reply_to = references = thread_id = ""
    subject = args.subject
    if args.reply_to:
        orig = svc.users().messages().get(
            userId="me", id=args.reply_to, format="metadata", metadataHeaders=HEADER_NAMES).execute()
        rh = reply_headers(orig, account)
        if (args.subject and not args.new_thread
                and _plain_subject(args.subject) != _plain_subject(_header(orig, "Subject"))):
            die("--subject differs from the subject of the message you are replying to, which would "
                "break the conversation thread. Leave --subject off, or add --new-thread if a "
                "new subject is really intended.")
        in_reply_to, references, thread_id = rh["in_reply_to"], rh["references"], rh["thread_id"]
        subject = subject or rh["subject"]
        if not to:
            to = common.addresses([rh["to"]])
    if not to:
        die("a draft needs a recipient: give --to (or --reply-to to answer a message)")
    if not subject:
        die("a draft needs --subject")

    try:
        mime = build_mime(to, cc, bcc, subject, body, args.attach or [], in_reply_to, references)
    except ValueError as e:
        die(f"that header is not allowed ({e})")

    if args.dry_run:
        print("DRY RUN - would create a DRAFT only. Nothing is sent and no one is notified.")
        print(f"  to:      {', '.join(to)}")
        print(f"  cc:      {', '.join(cc)}")
        print(f"  bcc:     {', '.join(bcc)}")
        print(f"  subject: {subject}")
        if thread_id:
            print(f"  reply in thread {thread_id} (In-Reply-To {in_reply_to})")
        print("  attachments: " + (", ".join(str(common.plain_file(a, "attach")) for a in args.attach)
                                   if args.attach else "none"))
        print("  ---- body ----")
        print(body)
        return

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    message: Dict[str, Any] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    d = svc.users().drafts().create(userId="me", body={"message": message}).execute()
    common.log_action("mail", "draft", to, subject, d["id"], account)
    if args.json:
        _print_json({"draftId": d["id"], "threadId": d.get("message", {}).get("threadId", "")})
    else:
        print(f"draft id: {d['id']}")
        print("Nothing was sent. Review it with: bertie-mail show-draft " + d["id"])


# ---------------------------------------------------------------- approval-gated

def cmd_send(args) -> None:
    svc = service(write=True)
    d = svc.users().drafts().get(userId="me", id=args.draft_id, format="full").execute()
    v = _draft_view(d)
    recipients = common.addresses([v["to"], v["cc"], v["bcc"]])
    if args.dry_run:
        print("DRY RUN - would SEND this existing draft now. Nothing was sent.")
        _print_draft(v)
        return
    common.confirm("Send", "Send this email now? It goes out as the account below and cannot be unsent.", [
        ("To", _people(v["to"])), ("Cc", _people(v["cc"])), ("Bcc", _people(v["bcc"])),
        ("Subject", v["subject"]), ("Attachments", v["attachments"] or "none"),
    ], v["body"], reason=common.reason_for("send mail to", common.first_of(recipients), v["subject"]))
    sent = svc.users().drafts().send(userId="me", body={"id": args.draft_id}).execute()
    sid = sent.get("id", "")
    common.log_action("mail", "send", recipients, v["subject"], sid)
    if args.json:
        _print_json({"sent": True, "messageId": sid, "threadId": sent.get("threadId", "")})
    else:
        print(f"sent to {', '.join(recipients)}: \"{v['subject']}\"  (message id {sid})")


def _describe(msg: Dict[str, Any]) -> str:
    return (f"from {_header(msg, 'From')} | {_when(msg)} | "
            f"subject: {_header(msg, 'Subject')}")


def _bin_move(args, action: str) -> None:
    svc = service(write=True)
    msg = svc.users().messages().get(
        userId="me", id=args.message_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"]).execute()
    verb = "move to the Bin" if action == "trash" else "take out of the Bin"
    if args.dry_run:
        print(f"DRY RUN - would {verb}: {_describe(msg)}")
        print("Nothing was changed.")
        return
    if action == "trash":
        common.confirm("Trash", "Move this email to the Bin?", [
            ("From", _header(msg, "From")), ("Subject", _header(msg, "Subject")), ("Date", _when(msg))],
            reason=common.reason_for("move to the Bin, from", common.first_of(_people(_header(msg, "From"))[:1]),
                                     _header(msg, "Subject")))
    else:
        common.confirm("Restore", "Take this email out of the Bin?", [
            ("From", _header(msg, "From")), ("Subject", _header(msg, "Subject")), ("Date", _when(msg))],
            reason=common.reason_for("restore from the Bin, from", common.first_of(_people(_header(msg, "From"))[:1]),
                                     _header(msg, "Subject")))
    api = svc.users().messages()
    res = (api.trash if action == "trash" else api.untrash)(userId="me", id=args.message_id).execute()
    common.log_action("mail", action, _header(msg, "From"), _header(msg, "Subject"),
                      res.get("id", args.message_id))
    print(("moved to the Bin: " if action == "trash" else "restored from the Bin: ") + _describe(msg))


def cmd_trash(args) -> None:
    _bin_move(args, "trash")


def cmd_untrash(args) -> None:
    _bin_move(args, "untrash")


def smoke() -> str:
    svc = service()
    p = svc.users().getProfile(userId="me").execute()
    return f"Mail: OK ({p['emailAddress']})"


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    top_p, common_p, wr = common.option_parents()

    ap = argparse.ArgumentParser(prog=common.registry.command_for("mail"), parents=[top_p],
                                 description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", parents=[common_p], help="show which mailbox this token reads")

    s = sub.add_parser("search", parents=[common_p], help="search the mailbox (Gmail query syntax)")
    s.add_argument("query")
    s.add_argument("--max", type=int, default=20)

    r = sub.add_parser("read", parents=[common_p], help="print one message in full")
    r.add_argument("message_id")

    t = sub.add_parser("thread", parents=[common_p], help="print a whole conversation")
    t.add_argument("thread_id")

    a = sub.add_parser("attachments", parents=[common_p], help="download a message's attachments")
    a.add_argument("message_id")
    a.add_argument("--out", required=True, help="folder to save into")

    dl = sub.add_parser("drafts", parents=[common_p], help="list drafts")
    dl.add_argument("--max", type=int, default=20)

    sd = sub.add_parser("show-draft", parents=[common_p], help="show one draft in full")
    sd.add_argument("draft_id")

    d = sub.add_parser("draft", parents=[common_p, wr], help="create a draft (sends nothing)")
    d.add_argument("--to", action="append", help="recipient (repeat or use commas)")
    d.add_argument("--cc", action="append")
    d.add_argument("--bcc", action="append")
    d.add_argument("--subject")
    d.add_argument("--body", help="the text of the email")
    d.add_argument("--body-file", help="read the text of the email from a file")
    d.add_argument("--attach", action="append", metavar="PATH", help="attach a file (repeatable)")
    d.add_argument("--reply-to", metavar="MESSAGE_ID",
                   help="reply in the same thread to this message")
    d.add_argument("--new-thread", action="store_true",
                   help="with --reply-to: allow a --subject that differs from the original")

    sn = sub.add_parser("send", parents=[common_p, wr], help="ASKS FIRST: send an existing draft")
    sn.add_argument("draft_id")

    tr = sub.add_parser("trash", parents=[common_p, wr], help="ASKS FIRST: move a message to the Bin")
    tr.add_argument("message_id")

    ut = sub.add_parser("untrash", parents=[common_p, wr], help="ASKS FIRST: take a message out of the Bin")
    ut.add_argument("message_id")
    return ap


COMMANDS = {
    "whoami": cmd_whoami, "search": cmd_search, "read": cmd_read, "thread": cmd_thread,
    "attachments": cmd_attachments, "drafts": cmd_drafts, "show-draft": cmd_show_draft,
    "draft": cmd_draft, "send": cmd_send, "trash": cmd_trash, "untrash": cmd_untrash,
}


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    COMMANDS[args.cmd](args)


if __name__ == "__main__":
    main()
