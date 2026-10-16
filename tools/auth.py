"""Sign-in for Bertie.

One token, kept in the macOS Keychain (service "bertie", account = the
email address), covering every scope in registry.py. If a scope is added to the
registry, the stored token no longer covers it and the code says "run
bertie login" instead of failing later with a confusing 403.

The Google libraries are imported inside the functions that need them, so the
parts that do not touch Google (help text, the action log, the safety hook)
still run when the environment is incomplete.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import registry  # noqa: E402
from common import configured_account, die  # noqa: E402,F401

SCOPES: List[str] = registry.all_scopes()
KEYCHAIN_SERVICE = registry.KEYCHAIN_SERVICE


def client_secret_path() -> Path:
    return common.HOME / "client_secret.json"


# ---------------------------------------------------------------- Keychain

def _keyring():
    try:
        import keyring
    except ImportError:
        die("the 'keyring' package is missing - run the installer again to repair the environment")
    return keyring


def load_token_info(account: str) -> Optional[Dict[str, Any]]:
    raw = _keyring().get_password(KEYCHAIN_SERVICE, account)
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        die("the stored sign-in is damaged - run 'bertie login' again")
    return info if isinstance(info, dict) else None


def store_token_info(account: str, info_json: str) -> None:
    _keyring().set_password(KEYCHAIN_SERVICE, account, info_json)


def delete_token(account: str) -> bool:
    kr = _keyring()
    try:
        kr.delete_password(KEYCHAIN_SERVICE, account)
        return True
    except Exception as e:  # keyring.errors.PasswordDeleteError and backend errors
        if e.__class__.__name__ == "PasswordDeleteError":
            return False
        raise


def missing_scopes(info: Dict[str, Any]) -> List[str]:
    """Scopes the code needs that the STORED token does not carry.

    Read off the stored token, not off the Credentials object:
    from_authorized_user_info overwrites creds.scopes with whatever we pass in,
    so checking creds.scopes would compare SCOPES against itself and always pass."""
    granted = set(info.get("scopes") or [])
    return sorted(set(SCOPES) - granted)


# ---------------------------------------------------------------- credentials

def credentials():
    account = configured_account()
    info = load_token_info(account)
    if info is None:
        die(f"not signed in as {account} - run 'bertie login'")

    missing = missing_scopes(info)
    if missing:
        die("token is missing scopes:\n  " + "\n  ".join(missing)
            + "\nrun 'bertie login' again to re-consent")

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            die(f"could not refresh the sign-in ({e.__class__.__name__}) - "
                "run 'bertie login' again")
        store_token_info(account, creds.to_json())
        return creds
    die("token unusable and cannot refresh - run 'bertie login' again")


def _discovery_build(api: str, version: str, creds):
    from googleapiclient.discovery import build
    return build(api, version, credentials=creds, cache_discovery=False)


def signed_in_email(creds) -> str:
    """Ask Google which mailbox this token really belongs to."""
    svc = _discovery_build("gmail", "v1", creds)
    return svc.users().getProfile(userId="me").execute()["emailAddress"]


def check_account(creds) -> str:
    """Abort unless the token's own email equals the configured account."""
    want = configured_account()
    got = signed_in_email(creds)
    if got.strip().lower() != want.strip().lower():
        die(f"wrong account: this machine is set up for {want} but the sign-in belongs to "
            f"{got}. Nothing was done. Run 'bertie login' and pick {want}.")
    return got


def build_service(api: str, version: str, write: bool = False):
    """The one door every command uses. write=True runs the account check first."""
    creds = credentials()
    if write:
        check_account(creds)
    return _discovery_build(api, version, creds)


# ---------------------------------------------------------------- login / logout

def login() -> str:
    account = configured_account()
    secret = client_secret_path()
    if not secret.exists():
        die(f"missing {secret} - run the installer again")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                  login_hint=account)
    if not creds.refresh_token:
        die("Google returned no refresh token - this would expire; run login again")
    info = json.loads(creds.to_json())
    missing = missing_scopes(info)
    if missing:
        die("not every permission box was ticked on the Google screen, so nothing was saved. "
            "Missing:\n  " + "\n  ".join(missing) + "\nRun 'bertie login' again and allow all.")
    got = signed_in_email(creds)
    if got.strip().lower() != account.strip().lower():
        die(f"you signed in as {got} but this machine is set up for {account}. "
            "Nothing was saved. Run 'bertie login' and pick the right account.")
    store_token_info(account, creds.to_json())
    print(f"signed in as {got}; the token is stored in the macOS Keychain "
          f"(service '{KEYCHAIN_SERVICE}')")
    return got


def _post(url: str, data: bytes) -> Tuple[int, str]:
    """POST form data. The secret goes in the request body, never on a command line."""
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def revoke(token: str) -> Tuple[bool, str]:
    """Revoke at Google. 'Already revoked' counts as success."""
    try:
        status, body = _post(registry.OAUTH_REVOKE_URL, urllib.parse.urlencode({"token": token}).encode())
    except (urllib.error.URLError, OSError) as e:
        return False, f"could not reach Google ({e.__class__.__name__})"
    if status == 200:
        return True, "revoked at Google"
    if status == 400 and "invalid_token" in body:
        return True, "Google says the token was already revoked or expired"
    return False, f"Google answered {status}"


def logout(force_local: bool = False) -> bool:
    """Ask in a Mac window, then revoke and delete. The work itself is in
    _revoke_and_delete, which has no window and is only for the uninstaller
    (which asks once, in its own window)."""
    account = configured_account()
    common.confirm("Sign out", "Sign out and revoke the Google token for this account?", [
        ("Effect", "the token is revoked at Google and deleted from the Keychain; "
                   "every bertie-* command stops working until you sign in again"),
        ("Account", account)],
        reason="sign out of Google and revoke the token")
    return _revoke_and_delete(force_local)


def _revoke_and_delete(force_local: bool = False) -> bool:
    """Revoke at Google AND delete the Keychain entry. Returns True when no
    usable sign-in is left. If Google cannot be reached the Keychain entry is
    KEPT (so you can retry) unless force_local is set."""
    account = configured_account()
    info = load_token_info(account)
    if info is None:
        print(f"no stored sign-in for {account}; nothing to revoke")
        return True
    token = info.get("refresh_token") or info.get("token")
    ok, msg = (revoke(token) if token else (True, "no token to revoke"))
    print(f"google: {msg}")
    if not ok and not force_local:
        print("The Keychain entry was kept so you can try again. "
              "To delete it anyway (the token then stays valid at Google until you remove it at "
              "myaccount.google.com/permissions), run: bertie logout --force-local",
              file=sys.stderr)
        return False
    removed = delete_token(account)
    print("keychain: entry deleted" if removed else "keychain: no entry found")
    return True
