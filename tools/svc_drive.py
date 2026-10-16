#!/usr/bin/env python3
"""bertie-drive: Google Drive for the signed-in account. Times are local.

Free (read only):
    bertie-drive list "quarterly"
    bertie-drive ls https://drive.google.com/drive/folders/1RpfJDp...
    bertie-drive download <id-or-url> --out ~/Downloads

Asks first (Claude Code shows an approval prompt):
    bertie-drive upload ~/report.pdf --folder <id-or-url>
    bertie-drive update <file-id> ~/report-v2.pdf        (overwrites the content)
    bertie-drive share <file-id> <email> --role reader|commenter|writer
    bertie-drive share <file-id> anyone --role reader --anyone   (anyone with the link)

This tool has no delete and no trash command, by design. It accepts Drive URLs
anywhere an id is expected. Every write supports --dry-run.
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import common  # noqa: E402
from common import die  # noqa: E402

# Google-native files have no bytes to download; they must be exported.
EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}

# shared drives are opt-in on every call or results silently omit them
ALL_DRIVES = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}
FIELDS = "files(id,name,mimeType,size,modifiedTime,owners(emailAddress))"
ROLES = ("reader", "commenter", "writer")


def service(write: bool = False):
    return auth.build_service("drive", "v3", write=write)


def escape_q(value: str) -> str:
    """Escape a value for use inside single quotes in a Drive q string.
    Backslash first, then the quote, or the escape itself gets doubled."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def file_id(ref: str) -> str:
    """Accept a bare id or any Drive/Docs URL and return the id."""
    if "://" not in ref:
        return ref
    m = re.search(r"/(?:folders|d)/([A-Za-z0-9_-]{10,})", ref)
    if not m:
        m = re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", ref)
    if not m:
        die(f"could not find a file id in: {ref}")
    return m.group(1)


def _rows(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for f in res.get("files", []):
        out.append({
            "id": f["id"],
            "name": f.get("name", ""),
            "type": f.get("mimeType", "").replace("application/vnd.google-apps.", "g-"),
            "size_kb": round(int(f["size"]) / 1024, 1) if f.get("size") else None,
            "modified": common.fmt_api_time(f.get("modifiedTime", "")),
        })
    return out


def _print(rows, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("no files")
        return
    for r in rows:
        size = f"{r['size_kb']}kb" if r["size_kb"] is not None else "-"
        print(f"{r['id']:<45} {r['modified']:<18} {size:>10}  {r['name']}")


def list_query(query: Optional[str]) -> str:
    return f"name contains '{escape_q(query)}' and trashed=false" if query else "trashed=false"


def cmd_list(args) -> None:
    res = service().files().list(
        q=list_query(args.query), pageSize=args.max, fields=FIELDS,
        orderBy="modifiedTime desc", **ALL_DRIVES).execute()
    _print(_rows(res), args.json)


def cmd_ls(args) -> None:
    fid = file_id(args.folder)
    res = service().files().list(
        q=f"'{escape_q(fid)}' in parents and trashed=false", pageSize=args.max,
        fields=FIELDS, orderBy="name", **ALL_DRIVES).execute()
    _print(_rows(res), args.json)


def cmd_download(args) -> None:
    from googleapiclient.http import MediaIoBaseDownload
    svc = service()
    fid = file_id(args.file)
    meta = svc.files().get(fileId=fid, fields="name,mimeType,size", supportsAllDrives=True).execute()
    name, mime = meta["name"], meta.get("mimeType", "")

    if mime in EXPORTS:
        target_mime, ext = EXPORTS[mime]
        req = svc.files().export_media(fileId=fid, mimeType=target_mime)
        if not name.endswith(ext):
            name += ext
    elif mime.startswith("application/vnd.google-apps"):
        die(f"'{name}' is a Google {mime.split('.')[-1]} with no export format")
    else:
        req = svc.files().get_media(fileId=fid, supportsAllDrives=True)

    out_dir = Path(args.out).expanduser()
    out = common.unique_path(out_dir / common.safe_filename(name))
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    out.write_bytes(buf.getvalue())
    kb = round(out.stat().st_size / 1024, 1)
    print(json.dumps({"saved": str(out), "size_kb": kb}, indent=2)
          if args.json else f"saved {out} ({kb}kb)")


# ---------------------------------------------------------------- approval-gated

def _need_file(path: str) -> Path:
    """The real path of a plain file (a symbolic link is refused)."""
    return common.plain_file(path, "upload")


def _kb(p: Path) -> float:
    return round(p.stat().st_size / 1024, 1)


def _media(p: Path):
    from googleapiclient.http import MediaFileUpload
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return MediaFileUpload(str(p), mimetype=mime, resumable=True)


def cmd_upload(args) -> None:
    p = _need_file(args.path)
    name = args.name or p.name
    body: Dict[str, Any] = {"name": name}
    folder = file_id(args.folder) if args.folder else ""
    if folder:
        body["parents"] = [folder]
    svc = service(write=True)
    if args.dry_run:
        print("DRY RUN - would upload a NEW file. Nothing was uploaded.")
        print(f"  file:   {p} ({_kb(p)}kb)")
        print(f"  as:     {name}")
        print(f"  into:   {folder or 'My Drive (top level)'}")
        return
    common.confirm("Upload", "Upload this file to Drive as a NEW file?", [
        ("File", f"{p} ({_kb(p)}kb)"), ("Saved as", name), ("Into folder", folder or "My Drive (top level)"),
    ], reason=common.reason_for("upload to", folder or "My Drive", p.name))
    f = svc.files().create(body=body, media_body=_media(p), fields="id,name,webViewLink",
                           supportsAllDrives=True).execute()
    common.log_action("drive", "upload", folder or "my-drive", f.get("name", name), f["id"])
    print(json.dumps(f, indent=2) if args.json else f"uploaded {f['name']}: {f.get('webViewLink', f['id'])}")


def cmd_update(args) -> None:
    p = _need_file(args.path)
    fid = file_id(args.file)
    svc = service(write=True)
    meta = svc.files().get(fileId=fid, fields="id,name,mimeType,size,modifiedTime",
                           supportsAllDrives=True).execute()
    if meta.get("mimeType", "").startswith("application/vnd.google-apps"):
        die(f"'{meta.get('name')}' is a Google-native file; update only works on ordinary "
            "uploaded files (PDF, Word, images and so on)")
    if args.dry_run:
        size = f"{round(int(meta['size']) / 1024, 1)}kb" if meta.get("size") else "unknown size"
        print("DRY RUN - would OVERWRITE the content of an existing file. Nothing was changed.")
        print(f"  file:       {meta.get('name')}  [{fid}]")
        print(f"  now:        {size}, last changed {common.fmt_api_time(meta.get('modifiedTime', ''))}")
        print(f"  new content: {p} ({_kb(p)}kb)")
        return
    common.confirm("Overwrite", "OVERWRITE the content of this existing Drive file?", [
        ("File", f"{meta.get('name')} [{fid}]"), ("New content from", f"{p} ({_kb(p)}kb)"),
        ("Warning", "the file's current content is replaced"),
    ], reason=common.reason_for("overwrite Drive file", meta.get("name") or fid, p.name))
    f = svc.files().update(fileId=fid, media_body=_media(p),
                           fields="id,name,modifiedTime,webViewLink", supportsAllDrives=True).execute()
    common.log_action("drive", "update", fid, f.get("name", meta.get("name", "")), f["id"])
    print(json.dumps(f, indent=2) if args.json else f"updated {f.get('name')}: {f.get('webViewLink', f['id'])}")


def build_permission(who: str, role: str, anyone_flag: bool) -> Dict[str, str]:
    """Validate and build the Drive permission. 'anyone' needs --anyone."""
    if role not in ROLES:
        die(f"--role must be one of: {', '.join(ROLES)}")
    if who.lower() == "anyone":
        if not anyone_flag:
            die("refusing to share with 'anyone with the link'. That makes the file open to "
                "anyone who has the link, with no sign-in. If that is really intended, add --anyone.")
        return {"type": "anyone", "role": role}
    if anyone_flag:
        die("--anyone only goes with the word 'anyone' in place of an email address")
    if not common.is_email(who):
        die(f"'{who}' is not an email address (or the word 'anyone' with --anyone)")
    return {"type": "user", "role": role, "emailAddress": who}


def cmd_share(args) -> None:
    perm = build_permission(args.email, args.role, args.anyone)
    fid = file_id(args.file)
    svc = service(write=True)
    meta = svc.files().get(fileId=fid, fields="id,name,webViewLink", supportsAllDrives=True).execute()
    name = meta.get("name", fid)
    is_anyone = perm["type"] == "anyone"
    if args.dry_run:
        print("DRY RUN - would change who can open this file. Nothing was changed.")
        print(f"  file: {name}  [{fid}]")
        if is_anyone:
            print("  !!! WARNING: ANYONE WITH THE LINK will be able to open this file as "
                  f"{perm['role'].upper()}, with NO sign-in. Anyone the link is passed to gets in. !!!")
        else:
            print(f"  who:  {perm['emailAddress']} as {perm['role']}")
            domain = common.configured_account().split("@")[-1].lower()
            if not perm["emailAddress"].lower().endswith("@" + domain):
                print(f"  note: this address is OUTSIDE {domain}")
            print("  a notification email " + ("will NOT be sent (--no-notify)" if args.no_notify
                                               else f"will be sent to {perm['emailAddress']}"))
        return
    if is_anyone:
        who = f"ANYONE WITH THE LINK, as {perm['role'].upper()}, with NO sign-in"
    else:
        who = f"{perm['emailAddress']} as {perm['role']}" + (
            "" if args.no_notify else " (they are notified by email)")
    common.confirm("Share", "Change who can open this Drive file?", [("File", name), ("Share with", who)],
                   reason=common.reason_for("share with", "ANYONE WITH LINK" if is_anyone else perm["emailAddress"], name))
    kwargs: Dict[str, Any] = {"fileId": fid, "body": perm, "supportsAllDrives": True,
                              "fields": "id,type,role,emailAddress"}
    if not is_anyone:
        kwargs["sendNotificationEmail"] = not args.no_notify
    res = svc.permissions().create(**kwargs).execute()
    target = "anyone-with-link" if is_anyone else perm["emailAddress"]
    common.log_action("drive", "share", target, name, res.get("id", fid))
    if args.json:
        print(json.dumps(res, indent=2))
    elif is_anyone:
        print(f"shared {name} with ANYONE WITH THE LINK as {perm['role']}")
    else:
        print(f"shared {name} with {perm['emailAddress']} as {perm['role']}")


def smoke() -> str:
    service().files().list(pageSize=1, fields="files(id)", **ALL_DRIVES).execute()
    return "Drive: OK"


def build_parser() -> argparse.ArgumentParser:
    top_p, common_p, wr = common.option_parents()
    ap = argparse.ArgumentParser(prog=common.registry.command_for("drive"), parents=[top_p], description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("list", parents=[common_p], help="search your Drive by name")
    l.add_argument("query", nargs="?")
    l.add_argument("--max", type=int, default=25)

    s = sub.add_parser("ls", parents=[common_p], help="list a folder's contents (id or URL)")
    s.add_argument("folder")
    s.add_argument("--max", type=int, default=100)

    d = sub.add_parser("download", parents=[common_p], help="download a file (id or URL)")
    d.add_argument("file")
    d.add_argument("--out", default=".")

    u = sub.add_parser("upload", parents=[common_p, wr], help="ASKS FIRST: upload a new file")
    u.add_argument("path")
    u.add_argument("--folder", help="destination folder id or URL")
    u.add_argument("--name", help="override the uploaded filename")

    up = sub.add_parser("update", parents=[common_p, wr],
                        help="ASKS FIRST: overwrite the content of an existing file")
    up.add_argument("file", help="file id or URL")
    up.add_argument("path", help="the new content")

    sh = sub.add_parser("share", parents=[common_p, wr], help="ASKS FIRST: share a file")
    sh.add_argument("file", help="file id or URL")
    sh.add_argument("email", help="who to share with, or the word 'anyone' (needs --anyone)")
    sh.add_argument("--role", required=True, choices=ROLES)
    sh.add_argument("--anyone", action="store_true",
                    help="allow 'anyone with the link' sharing (otherwise refused)")
    sh.add_argument("--no-notify", action="store_true", help="do not email the person")
    return ap


COMMANDS = {"list": cmd_list, "ls": cmd_ls, "download": cmd_download,
            "upload": cmd_upload, "update": cmd_update, "share": cmd_share}


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    COMMANDS[args.cmd](args)


if __name__ == "__main__":
    main()
