"""The one list every other file reads: services, scopes, and which actions ask first.

Standard library only, so the safety hook and the setup tool can import it
without the Google libraries.

To add a service later (Docs, Sheets, Tasks and so on):
  1. write tools/svc_<name>.py with a main(argv=None) and a smoke() function,
  2. add one Service(...) entry to SERVICES below, with its scopes and its
     gated actions,
  3. add its tests.
The installer, the permission rules, the safety hook, the login scope check and
the smoke test all pick the new entry up from here. Adding a scope means the
person runs `bertie login` once more; the code says so by itself.

NAME is the one place the tool's name lives. Every command, folder, Keychain
entry, skill, marker and message is made from it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

NAME = "bertie"                              # lower case: commands, folders, markers
DISPLAY_NAME = NAME.capitalize()             # "Bertie": what people read
KEYCHAIN_SERVICE = NAME
GOOGLE_COMMAND = NAME                        # the account command: `bertie login`
HELPER_NAME = NAME + "-approve"              # the compiled Touch ID / password helper
INSTALL_REL = ".config/" + NAME              # under the home folder
BIN_REL = ".local/bin"
SKILL_REL = ".claude/skills/" + NAME
SETTINGS_REL = ".claude/settings.json"
OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
WRAPPER_MARKER = "# " + NAME + " wrapper"
ENV_PREFIX = NAME.upper() + "_"              # BERTIE_ACCOUNT and friends


def command_for(key: str) -> str:
    return f"{NAME}-{key}"


@dataclass(frozen=True)
class Service:
    key: str                      # "mail"
    command: str                  # "bertie-mail"
    module: str                   # "svc_mail"  (file tools/svc_mail.py)
    label: str                    # "Gmail"
    api_name: str                 # name shown in the Google Cloud console
    scopes: Tuple[str, ...]
    # action -> plain-English description, used in the permission prompt.
    gated: Dict[str, str] = field(default_factory=dict)


_G = "https://www.googleapis.com/auth/"

SERVICES: Tuple[Service, ...] = (
    Service(
        key="mail", command=command_for("mail"), module="svc_mail", label="Gmail",
        api_name="Gmail API",
        # gmail.modify: read, drafts, send, trash. NOT the full mail.google.com
        # scope, so permanent delete is impossible with this token.
        scopes=(_G + "gmail.modify",),
        gated={
            "send": "SENDS an email from the signed-in account",
            "trash": "moves an email to the Bin",
            "untrash": "takes an email out of the Bin",
        },
    ),
    Service(
        key="calendar", command=command_for("calendar"), module="svc_calendar",
        label="Google Calendar", api_name="Google Calendar API",
        scopes=(_G + "calendar.events", _G + "calendar.readonly"),
        gated={
            "create": "creates a calendar event (invites are emailed to attendees)",
            "move": "moves a calendar event (attendees are emailed)",
            "cancel": "cancels a calendar event (attendees are emailed)",
        },
    ),
    Service(
        key="chat", command=command_for("chat"), module="svc_chat",
        label="Google Chat", api_name="Google Chat API",
        scopes=(
            _G + "chat.messages.create",
            _G + "chat.spaces.readonly",
            _G + "chat.spaces.create",
            # reads the account's OWN spaces and DMs only
            _G + "chat.messages.readonly",
            # maps sender ids (users/1234) to names when printing a thread
            _G + "chat.memberships.readonly",
        ),
        gated={
            "send": "posts a Chat message as the signed-in person",
            "dm": "sends a Chat direct message as the signed-in person",
        },
    ),
    Service(
        key="drive", command=command_for("drive"), module="svc_drive",
        label="Google Drive", api_name="Google Drive API",
        # FULL drive access, by design. The code has no
        # delete or trash command, so that power stays unused.
        scopes=(_G + "drive",),
        gated={
            "upload": "uploads a new file to Drive",
            "update": "OVERWRITES the content of an existing Drive file",
            "share": "changes who can open a Drive file",
        },
    ),
)

# `bertie` itself: account-level actions. Not a Google API service.
GOOGLE_GATED: Dict[str, str] = {
    "logout": "signs out and revokes the Google token",
    "uninstall": "removes " + DISPLAY_NAME + " from this Mac",
}


def all_scopes() -> List[str]:
    seen: List[str] = []
    for s in SERVICES:
        for sc in s.scopes:
            if sc not in seen:
                seen.append(sc)
    return seen


def tool_names() -> List[str]:
    return [s.command for s in SERVICES] + [GOOGLE_COMMAND]


def gated_actions() -> Dict[str, Dict[str, str]]:
    """{'bertie-mail': {'send': '...'}, ..., 'bertie': {...}}"""
    out = {s.command: dict(s.gated) for s in SERVICES}
    out[GOOGLE_COMMAND] = dict(GOOGLE_GATED)
    return out


def ask_rules() -> List[str]:
    """The permissions.ask entries, one per gated action."""
    rules = []
    for tool, acts in gated_actions().items():
        for act in acts:
            rules.append(f"Bash({tool} {act}:*)")
    return rules


def wrappers() -> List[Tuple[str, str]]:
    """(command name, script file) for every command put in ~/.local/bin."""
    out = [(s.command, s.module + ".py") for s in SERVICES]
    out.append((GOOGLE_COMMAND, "google_cli.py"))
    return out

