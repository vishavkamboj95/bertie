#!/usr/bin/env python3
"""bertie: account-level actions (sign in, check, the action log, sign out, uninstall).

    bertie login                 sign in (opens a browser; one click)
    bertie whoami                which account is signed in, and does it match
    bertie log [-n 50]           what the tools have changed, newest first (local time)
    bertie check                 try a small read on every service
    bertie logout                ASKS FIRST: revoke the token at Google and delete the Keychain entry
    bertie uninstall --yes       ASKS FIRST: sign out, then remove everything the installer added
"""
from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import registry  # noqa: E402
from common import die  # noqa: E402


def cmd_login(args) -> None:
    import auth
    auth.login()


def cmd_logout(args) -> None:
    import auth
    if not auth.logout(force_local=args.force_local):
        raise SystemExit(1)


def cmd_whoami(args) -> None:
    import auth
    account = common.configured_account()
    info = auth.load_token_info(account)
    if info is None:
        print(f"configured account: {account}")
        print("signed in: NO (nothing in the Keychain). Run: bertie login")
        raise SystemExit(1)
    missing = auth.missing_scopes(info)
    if missing:
        print(f"configured account: {account}")
        print("signed in: yes, but the permissions are out of date. Run: bertie login")
        for m in missing:
            print(f"  missing: {m}")
        raise SystemExit(1)
    creds = auth.credentials()
    got = auth.signed_in_email(creds)
    match = got.strip().lower() == account.strip().lower()
    print(f"signed in as {got}")
    print(f"configured account: {account}  ->  {'match' if match else 'MISMATCH - writes will be refused'}")
    if not match:
        raise SystemExit(1)


def cmd_log(args) -> None:
    recs = common.read_log(args.n)
    if not recs:
        print("the action log is empty")
        return
    for r in recs:
        print(common.format_log_record(r))


NOT_ENABLED = ("{label} is not switched on yet in your Google Cloud project "
               "(the '{api}' needs enabling: APIs and Services, then Library). "
               "Nothing is wrong on this Mac. Switch it on, then run: "
               + registry.GOOGLE_COMMAND + " check")
CHAT_APP = ("Google Chat answered 'Chat app not found'. Your Google Cloud project needs a Chat app "
            "set up (Google Chat API, then Configuration); the README lists the minimum. "
            "Nothing is wrong on this Mac.")


def run_check() -> int:
    """Smoke-read every service. Returns 0 unless something is genuinely wrong
    here; a service that is simply switched off in Google Cloud is explained
    in plain words and does NOT fail the check."""
    rc = 0
    for s in registry.SERVICES:
        mod = importlib.import_module(s.module)
        try:
            print(mod.smoke())
        except SystemExit:
            raise
        except Exception as e:
            kind, msg = common.classify_http_error(e)
            if kind == "not_enabled":
                print(NOT_ENABLED.format(label=s.label, api=s.api_name))
            elif kind == "chat_app":
                print(CHAT_APP)
            else:
                print(f"{s.label}: FAILED ({e.__class__.__name__}: {msg})")
                rc = 1
    return rc


def cmd_check(args) -> None:
    raise SystemExit(run_check())


def cmd_uninstall(args) -> None:
    import auth
    import kit_setup
    install_dir = (Path.home() / registry.INSTALL_REL).resolve()
    if common.HOME.resolve() != install_dir:
        die(f"refusing to uninstall: these files are not running from {install_dir}")
    if not args.yes:
        die("this removes Bertie from this Mac. Add --yes to go ahead. "
            "The action log lives in the install folder and is deleted with it; "
            "run 'bertie log' first if you want to keep a copy.")
    common.confirm("Uninstall", "Sign out and REMOVE Bertie from this Mac?", [
        ("Removes", "the five commands (bertie and bertie-*), the skill, the approval rules and safety hook in "
                    "~/.claude/settings.json, and the install folder with its action log"),
        ("Also", "the Google token is revoked and deleted from the Keychain")],
        reason="uninstall Bertie")
    if not auth._revoke_and_delete(force_local=args.force_local):
        die("sign-out did not complete, so nothing was removed. Fix the problem above, "
            "or use --force-local to delete the Keychain entry anyway.")
    ctx = kit_setup.Ctx(Path.home(), common.HOME)
    for m in kit_setup.uninstall_local(ctx):
        print(m)
    # only the install folder itself: its parent is ~/.config, which is never ours to remove
    shutil.rmtree(str(common.HOME), ignore_errors=True)
    print(f"removed the install folder ~/{registry.INSTALL_REL}")
    print(f"done. Settings backups named settings.json.{registry.NAME}-backup-* were left in ~/.claude "
          "for you to delete when you like.")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=registry.GOOGLE_COMMAND, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="sign in with Google (opens a browser)")
    lo = sub.add_parser("logout", help="ASKS FIRST: revoke the token and delete the Keychain entry")
    lo.add_argument("--force-local", action="store_true",
                    help="delete the Keychain entry even if Google could not be reached")
    sub.add_parser("whoami", help="which account is signed in")
    lg = sub.add_parser("log", help="the action log, newest first, in local time")
    lg.add_argument("-n", type=int, default=50, help="how many lines (0 = all)")
    sub.add_parser("check", help="try a small read on every service")
    un = sub.add_parser("uninstall", help="ASKS FIRST: remove everything the installer added")
    un.add_argument("--yes", action="store_true")
    un.add_argument("--force-local", action="store_true")
    return ap


COMMANDS = {"login": cmd_login, "logout": cmd_logout, "whoami": cmd_whoami, "log": cmd_log,
            "check": cmd_check, "uninstall": cmd_uninstall}


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    COMMANDS[args.cmd](args)


if __name__ == "__main__":
    main()
