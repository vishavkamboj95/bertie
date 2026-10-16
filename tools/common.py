"""Shared helpers: local times, the action log, the account file, error text.

Standard library only (no Google libraries), so `bertie log` and the
installer work even when the Python environment is incomplete.
"""
from __future__ import annotations

import email.utils
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402

# The install folder is wherever these files live. Tests point it elsewhere.
HOME = Path(__file__).resolve().parent



def _system_zone_name() -> str:
    """This Mac's time zone as a name Google Calendar understands (Europe/London).
    TZ wins if it is set; otherwise the zone macOS points /etc/localtime at.
    Anything unreadable falls back to UTC rather than guessing."""
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        try:
            target = os.readlink("/etc/localtime")
        except OSError:
            target = ""
        name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return name
    except Exception:
        return "UTC"


# Every time is shown, and every time without an offset is read, in this zone.
# Tests set it to a fixed zone.
ZONE_NAME = _system_zone_name()


def local_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(ZONE_NAME)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def die(msg: str):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------- paths

def account_file() -> Path:
    return HOME / "account"


def log_file() -> Path:
    return HOME / "actions.log"


def configured_account() -> str:
    f = account_file()
    if not f.exists():
        die(f"no account is configured ({f} is missing) - run the installer again")
    acct = f.read_text().strip()
    if not acct or "@" not in acct:
        die(f"the account file {f} does not hold an email address - run the installer again")
    return acct


# ---------------------------------------------------------------- time

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz())


def _day_prefix(d: datetime, today: date) -> str:
    if d.date() == today:
        return ""
    if d.year == today.year:
        return f"{d.day} {_MONTHS[d.month - 1]} "
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year} "


def _zone(d: datetime) -> str:
    return d.tzname() or ZONE_NAME


def fmt_local(dt: datetime, now: Optional[datetime] = None) -> str:
    """'13:45 IST' for today (local time, with the zone's short name),
    '15 Oct 13:45 IST' otherwise."""
    d = to_local(dt)
    today = to_local(now or now_utc()).date()
    return f"{_day_prefix(d, today)}{d:%H:%M} {_zone(d)}"


def fmt_range(start: datetime, end: datetime, now: Optional[datetime] = None) -> str:
    """'13:45-14:30 IST', with the date in front when it is not today."""
    s, e = to_local(start), to_local(end)
    today = to_local(now or now_utc()).date()
    if s.date() == e.date():
        return f"{_day_prefix(s, today)}{s:%H:%M}-{e:%H:%M} {_zone(e)}"
    return f"{fmt_local(s, now)} to {fmt_local(e, now)}"


_RFC = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?"
    r"\s*(Z|z|[+-]\d{2}:?\d{2})?$")


def parse_when(text: str, end_of_day: bool = False) -> datetime:
    """Parse '2026-10-15', '2026-10-15T14:00', '2026-10-15 14:00',
    '...Z' or '...+05:30'. No offset means local time. A bare date means 00:00 local,
    or the start of the NEXT day with end_of_day=True (so a --to date includes
    that whole day)."""
    m = _RFC.match(text.strip())
    if not m:
        die(f"cannot read the time '{text}'. Use e.g. 2026-10-15 or 2026-10-15T14:00 "
            "(local time unless you add an offset such as +00:00).")
    y, mo, d, hh, mi, ss, frac, off = m.groups()
    try:
        base = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0),
                        int((frac or "0")[:6].ljust(6, "0")))
    except ValueError:
        die(f"'{text}' is not a real date or time")
    if hh is None and end_of_day:
        base = base + timedelta(days=1)
    if off is None:
        tz = local_tz()
    elif off in ("Z", "z"):
        tz = timezone.utc
    else:
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    return base.replace(tzinfo=tz)


def parse_rfc3339(text: str) -> datetime:
    """Strict-ish parse of API timestamps (always carry an offset)."""
    return parse_when(text)


def fmt_header_date(value: str, now: Optional[datetime] = None) -> str:
    """An email Date header shown in local time; unparseable values come back as given."""
    if not value:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return value
    if dt is None:
        return value
    return fmt_local(dt, now)


def fmt_api_time(value: str, now: Optional[datetime] = None) -> str:
    if not value:
        return ""
    try:
        return fmt_local(parse_rfc3339(value), now)
    except SystemExit:
        return value


# ---------------------------------------------------------------- files

def safe_filename(name: str) -> str:
    """A name from Google (Drive, an attachment) can contain slashes or be
    '..'. Never let it choose where the file lands."""
    name = (name or "").replace("\x00", "")
    name = re.sub(r"[\\/]+", "_", name).strip()
    if name in ("", ".", ".."):
        name = "file"
    return name


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suf = path.stem, path.suffix
    i = 1
    while True:
        cand = path.with_name(f"{stem} ({i}){suf}")
        if not cand.exists():
            return cand
        i += 1


# ---------------------------------------------------------------- action log

def log_action(tool: str, action: str, target: Any, subject: str, ident: str,
               account: Optional[str] = None) -> None:
    """Append one JSON line: UTC time, tool, action, target, subject, id.

    There is deliberately no parameter for a message body. Bodies are never
    written to the log."""
    if isinstance(target, (list, tuple)):
        target = ", ".join(str(t) for t in target)
    rec = {
        "ts": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "account": account if account is not None else _try_account(),
        "tool": tool,
        "action": action,
        "target": target or "",
        "subject": subject or "",
        "id": ident or "",
    }
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        path = log_file()
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(str(path), 0o600)
    except OSError as e:
        # The action already happened; do not pretend it failed.
        print(f"warning: the action succeeded but could not be written to the log: {e}",
              file=sys.stderr)


def _try_account() -> str:
    try:
        return account_file().read_text().strip()
    except OSError:
        return ""


def read_log(limit: int = 50) -> List[Dict[str, Any]]:
    """Newest first. Lines that are not valid JSON are skipped."""
    path = log_file()
    if not path.exists():
        return []
    recs = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
    recs.reverse()
    return recs[:limit] if limit else recs


def format_log_record(rec: Dict[str, Any], now: Optional[datetime] = None) -> str:
    ts = rec.get("ts", "")
    try:
        when = fmt_local(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), now)
    except ValueError:
        when = ts or "?"
    parts = [when, f"{rec.get('tool', '?')} {rec.get('action', '?')}"]
    if rec.get("target"):
        parts.append(f"-> {rec['target']}")
    if rec.get("subject"):
        parts.append(f'"{rec["subject"]}"')
    if rec.get("id"):
        parts.append(f"[{rec['id']}]")
    return "  ".join(parts)


# ---------------------------------------------------------------- API errors

def _error_payload(exc: Any) -> Dict[str, Any]:
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    try:
        data = json.loads(content)
    except ValueError:
        return {}
    err = data.get("error") if isinstance(data, dict) else None
    return err if isinstance(err, dict) else {}


def classify_http_error(exc: Any):
    """Return ('not_enabled' | 'chat_app' | 'other', short text).

    Duck-typed on googleapiclient's HttpError (resp.status, content) so this
    file needs no Google import."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    err = _error_payload(exc)
    message = str(err.get("message", "")) or str(exc)
    reasons = {str(e.get("reason", "")) for e in err.get("errors", []) if isinstance(e, dict)}
    for det in err.get("details", []) or []:
        if isinstance(det, dict) and det.get("reason"):
            reasons.add(str(det["reason"]))
    low = message.lower()
    if (reasons & {"accessNotConfigured", "SERVICE_DISABLED"}
            or "has not been used in project" in low
            or "api has not been enabled" in low
            or ("is disabled" in low and "api" in low)):
        return "not_enabled", message
    if status == 404 and "chat app not found" in low:
        return "chat_app", message
    return "other", message


# ---------------------------------------------------------------- the confirmation window

DIALOG_TIMEOUT_SECONDS = 120
BODY_PREVIEW_CHARS = 300
CANCEL_WORD = "Cancel"

# The second step: Touch ID, or the Mac password, asked by a small compiled
# helper (tools/approve.swift) that the installer builds next to the tools.
HELPER_NAME = registry.HELPER_NAME
HELPER_HASH_NAME = "approve.sha256"
APPROVE_TIMEOUT_SECONDS = 60           # the helper gives up after this long itself
HELPER_EXIT_UNAVAILABLE = 3            # the helper's code for "this Mac cannot ask"
REASON_CHARS = 100
_REINSTALL = "Run the installer again (sh install.sh <your-email>) from the setup folder"

# Receives the message and the button text as ARGUMENTS (argv), never pasted
# into the script, so nothing in an email or a title can change what runs.
_DIALOG_SCRIPT = [
    "on run argv",
    "set msg to item 1 of argv",
    "set btn to item 2 of argv",
    "try",
    'set r to display dialog msg with title "' + registry.DISPLAY_NAME + ' asks first: please confirm" '
    'buttons {"Cancel", btn} default button "Cancel" cancel button "Cancel" '
    "with icon caution giving up after " + str(DIALOG_TIMEOUT_SECONDS),
    'if gave up of r then return "TIMEOUT"',
    "return button returned of r",
    "on error",
    'return "CANCEL"',
    "end try",
    "end run",
]


def _run_dialog(argv: List[str], timeout: int) -> "subprocess.CompletedProcess":
    """The one place a process is started for the dialog (tests replace it)."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _dialog_available() -> bool:
    return sys.platform == "darwin" and bool(shutil.which("osascript"))


MAX_DETAIL_CHARS = 1500           # the details block (everything above the preview)

def _flat(text: Any) -> str:
    """One line: every control, line-break, zero-width or direction-changing
    character becomes a space, and runs of spaces collapse. Used for every row,
    so a value can never start a new line in the window."""
    out = []
    for ch in str(text):
        cat = unicodedata.category(ch)
        out.append(" " if cat in ("Cc", "Cf", "Zl", "Zp") else ch)
    return re.sub(r" {2,}", " ", "".join(out)).strip()


def _preview(text: str) -> str:
    """The body preview: the only part of the window that may be cut, and the
    only part that may hold line breaks. A cut is always marked."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch if ch == "\n" else (" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch)
                   for ch in text)
    text = re.sub(r"[ ]{2,}", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    if len(text) <= BODY_PREVIEW_CHARS:
        return text
    cut = text[:BODY_PREVIEW_CHARS].rstrip()
    return f"{cut}\n(preview, {len(text) - len(cut)} more characters)"


def _new_code() -> str:
    """A fresh 4-digit number for this one window (1000-9999)."""
    return str(1000 + secrets.randbelow(9000))


def _run_helper(argv: List[str], timeout: int) -> "subprocess.CompletedProcess":
    """The one place the approval helper is started (tests replace it)."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _verify_helper() -> Path:
    """The compiled helper, only if it is the one the installer built: a plain
    executable file whose sha256 equals the one recorded at install time.
    Anything else refuses the action. There is no fallback to a click-only window."""
    helper = HOME / HELPER_NAME
    try:
        st = helper.lstat()
    except OSError:
        die("the fingerprint/password helper is missing, so nothing was done. " + _REINSTALL + ".")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or not os.access(str(helper), os.X_OK):
        die("the fingerprint/password helper is not a plain program file, so nothing was done. "
            + _REINSTALL + ".")
    try:
        recorded = (HOME / HELPER_HASH_NAME).read_text().strip().lower()
        actual = hashlib.sha256(helper.read_bytes()).hexdigest()
    except (OSError, UnicodeError):
        die("the fingerprint/password helper cannot be checked (its recorded fingerprint is missing), "
            "so nothing was done. " + _REINSTALL + ".")
    if not hmac.compare_digest(actual, recorded):
        die("the fingerprint/password helper has changed since it was installed, so nothing was done. "
            + _REINSTALL + ".")
    return helper


def first_of(people: Sequence[str]) -> str:
    """'a@x.test' for one person, 'a@x.test +2 more' for three. Never cut."""
    people = [_flat(p) for p in people if _flat(p)]
    if not people:
        return ""
    return people[0] + (f" +{len(people) - 1} more" if len(people) > 1 else "")


def reason_for(action: str, who: str = "", detail: str = "", link: str = " ") -> str:
    """The short text of the fingerprint sheet: WHAT, then WHO or WHERE, then a
    detail (a subject, a title, a file name), all within REASON_CHARS. The
    detail is cut first, with '...'; the action and the who/where are not."""
    head = _flat(action)
    who = _flat(who)
    if who:
        head += link + who
    detail = _flat(detail)
    room = REASON_CHARS - len(head) - 2
    if detail and len(detail) > room:
        detail = detail[:room - 3].rstrip() + "..." if room > 6 else ""
    return f"{head}: {detail}" if detail else head


def _approve(reason: str) -> None:
    """Touch ID, with the Mac password as the fallback. Returns only on success."""
    helper = _verify_helper()                       # checked again, right before it runs
    short = _flat(reason)[:REASON_CHARS].strip() or "approve this action"
    try:
        r = _run_helper([str(helper), short], APPROVE_TIMEOUT_SECONDS + 30)
    except (OSError, subprocess.SubprocessError):
        die("the fingerprint/password check could not run, so nothing was done")
    if r.returncode == 0:
        return
    if r.returncode == HELPER_EXIT_UNAVAILABLE:
        die("this Mac cannot ask for a fingerprint or password here (is a Mac password set?), "
            "so nothing was done. " + _REINSTALL + " once it can.")
    die("the fingerprint or password check was not passed, so nothing was done")


def confirm(button: str, summary: str, rows: Sequence[Tuple[str, Any]], text: str = "",
            reason: str = "") -> None:
    """Show what is about to happen, then ask for a fingerprint or Mac password,
    and return ONLY if the person passes both.

    Step 1 is a macOS window listing the details. Its button reads
    "Continue 4821": a fresh random number per call, made here, so a script
    written beforehand cannot know which button to press. Step 2, only after that
    click, is Touch ID or the Mac password, asked by the installed helper with
    `reason` (a short phrase, flattened) as the sheet's text. If the helper is
    missing, changed or unavailable the action is refused; the window alone is
    never enough.
    Anything else (Cancel, closing the window, no click for two minutes, no
    osascript, no desktop session, any error) ends the program with a
    non-zero exit, before the caller does anything. There is no flag,
    environment variable or setting that skips this. Call it immediately
    before the API call that changes something, and log only after that call.

    A row value that is a list (recipients, attendees, attachments) is shown in
    full, with a count. Nothing in the details is ever cut: if they do not fit
    the window the command is refused. Only `text` (the body) is previewed."""
    if button.strip().lower() == CANCEL_WORD.lower():
        raise ValueError("the action button must not be called Cancel")
    _verify_helper()                  # no helper, no window: refuse before showing anything
    label = f"Continue {_new_code()}"
    lines = [_flat(summary), "", f"From account: {_flat(_try_account() or '?')}"]
    has_list = False
    for name, value in rows:
        if isinstance(value, (list, tuple)):
            items = [_flat(v) for v in value]
            items = [v for v in items if v]
            if items:
                has_list = True
                lines.append(f"{_flat(name)} ({len(items)}): " + "; ".join(items))
        else:
            value = _flat(value)
            if value:
                lines.append(f"{_flat(name)}: {value}")
    lines += ["", f"Click the button '{label}' to go on. You will then be asked for your fingerprint "
                  "or Mac password. Anything else stops it."]
    details = "\n".join(lines)
    if len(details) > MAX_DETAIL_CHARS:
        if has_list:
            die("too many recipients/attachments to show; split it. Nothing was done")
        die("too much text to show in full; shorten it. Nothing was done")
    message = details
    if text and _preview(text):
        message += "\n\nMessage preview:\n" + _preview(text)

    if not _dialog_available():
        die("cannot show the confirmation window here (macOS with a desktop session is needed), "
            "so nothing was done")
    argv = ["osascript"]
    for line in _DIALOG_SCRIPT:
        argv += ["-e", line]
    argv += [message, label]
    try:
        r = _run_dialog(argv, DIALOG_TIMEOUT_SECONDS + 30)
    except (OSError, subprocess.SubprocessError):
        die("the confirmation window could not be shown, so nothing was done")
    answer = (r.stdout or "").strip()
    if r.returncode != 0:
        die("the confirmation window could not be shown (no desktop session?), so nothing was done")
    if answer == "TIMEOUT":
        die("no answer within two minutes, so nothing was done")
    if answer != label:
        die("cancelled, so nothing was done")
    _approve(reason or button.lower())


def plain_file(path: str, what: str = "use") -> Path:
    """A file to attach or upload. A symbolic link is refused (it can point
    anywhere, and the window would show the link's name, not its target).
    Returns the fully resolved real path, which is what the window shows."""
    p = Path(path).expanduser()
    try:
        st = p.lstat()
    except OSError:
        die(f"no such file to {what}: {p}")
    if stat.S_ISLNK(st.st_mode):
        die(f"{p} is a symbolic link; refusing to {what} it. Give the real file instead.")
    if not stat.S_ISREG(st.st_mode):
        die(f"no such file to {what}: {p}")
    return p.resolve()


# ---------------------------------------------------------------- argparse

def option_parents():
    """(top, sub) parents carrying --json / --dry-run.

    --json works before or after the subcommand. The sub copy defaults to
    SUPPRESS, otherwise argparse lets the subparser's default (False) overwrite
    a --json given before the subcommand."""
    import argparse
    top = argparse.ArgumentParser(add_help=False)
    top.add_argument("--json", action="store_true", help="machine-readable output")
    sub = argparse.ArgumentParser(add_help=False)
    sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                     help="machine-readable output")
    dry = argparse.ArgumentParser(add_help=False)
    dry.add_argument("--dry-run", action="store_true",
                     help="show exactly what would happen and do nothing")
    return top, sub, dry


# ---------------------------------------------------------------- misc text

def addresses(values: Optional[List[str]]) -> List[str]:
    """['a@x.com, b@y.com', 'c@z.com'] -> three addresses. Repeated flags and
    commas both work."""
    out: List[str] = []
    for v in values or []:
        for _, addr in email.utils.getaddresses([v]):
            if addr:
                out.append(addr)
    return out


def is_email(s: str) -> bool:
    return bool(re.match(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$", s))
