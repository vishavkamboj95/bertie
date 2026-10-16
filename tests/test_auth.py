"""Keychain storage, the scope check, login, logout, and the per-service check."""
import json

import pytest

import auth
import common
import google_cli
import registry
from conftest import ACCOUNT, OTHER


def stored(scopes=None, **extra):
    info = {"token": "ACCESS-TOKEN-VALUE", "refresh_token": "REFRESH-TOKEN-VALUE",
            "client_id": "cid", "client_secret": "csec",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": list(registry.all_scopes() if scopes is None else scopes)}
    info.update(extra)
    return info


# ------------------------------------------------------------------ scopes

def test_scope_list_is_exactly_what_was_asked_for():
    g = "https://www.googleapis.com/auth/"
    assert sorted(registry.all_scopes()) == sorted(g + s for s in [
        "gmail.modify", "calendar.events", "calendar.readonly", "chat.messages.create",
        "chat.messages.readonly", "chat.spaces.readonly", "chat.spaces.create",
        "chat.memberships.readonly", "drive"])


def test_the_full_mail_scope_is_never_requested():
    assert not any("mail.google.com" in s for s in registry.all_scopes())
    from pathlib import Path
    for f in Path(auth.__file__).parent.glob("*.py"):
        assert "https://mail.google.com/" not in f.read_text(), f.name


def test_missing_scopes_are_read_from_the_stored_token():
    info = stored(scopes=[s for s in registry.all_scopes() if not s.endswith("/drive")])
    assert auth.missing_scopes(info) == ["https://www.googleapis.com/auth/drive"]
    assert auth.missing_scopes(stored()) == []
    assert auth.missing_scopes({}) == sorted(registry.all_scopes())


def test_credentials_dies_with_the_login_hint_when_a_scope_is_missing(install_dir, keychain, capsys):
    old = stored(scopes=[s for s in registry.all_scopes() if "calendar" not in s])
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps(old))
    with pytest.raises(SystemExit):
        auth.credentials()
    err = capsys.readouterr().err
    assert "token is missing scopes" in err and "calendar.events" in err
    assert "bertie login" in err
    assert "ACCESS-TOKEN-VALUE" not in err and "REFRESH-TOKEN-VALUE" not in err


def test_not_signed_in_says_login(install_dir, capsys):
    with pytest.raises(SystemExit):
        auth.credentials()
    assert "bertie login" in capsys.readouterr().err


def test_a_valid_stored_token_loads(install_dir, keychain):
    future = "2099-01-01T00:00:00Z"
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps(stored(expiry=future)))
    creds = auth.credentials()
    assert creds.valid and creds.token == "ACCESS-TOKEN-VALUE"


def test_an_expired_token_is_refreshed_and_stored_back(install_dir, keychain, monkeypatch):
    from google.oauth2.credentials import Credentials
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT,
                          json.dumps(stored(expiry="2000-01-01T00:00:00Z")))

    def fake_refresh(self, request):
        self.token = "NEW-ACCESS"
        import datetime
        self.expiry = datetime.datetime(2099, 1, 1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = auth.credentials()
    assert creds.token == "NEW-ACCESS"
    assert "NEW-ACCESS" in keychain.get_password(registry.KEYCHAIN_SERVICE, ACCOUNT)


# ------------------------------------------------------------------ where the token lives

def test_token_is_stored_in_the_keychain_under_the_agreed_names(install_dir, keychain, monkeypatch):
    auth.store_token_info(ACCOUNT, json.dumps(stored()))
    assert (registry.KEYCHAIN_SERVICE, ACCOUNT) in keychain.store
    assert registry.KEYCHAIN_SERVICE == "bertie"
    assert list(install_dir.glob("token*")) == []                 # no token file


def test_no_token_file_is_ever_written_by_the_code():
    from pathlib import Path
    src = Path(auth.__file__).read_text()
    assert "write_text(creds" not in src and "token-" not in src
    assert "subprocess" not in src and "'security'" not in src and '"security"' not in src


# ------------------------------------------------------------------ account check

def test_check_account_accepts_the_matching_mailbox(install_dir, monkeypatch):
    monkeypatch.setattr(auth, "signed_in_email", lambda c: ACCOUNT)
    assert auth.check_account(object()) == ACCOUNT


def test_check_account_aborts_on_a_different_mailbox(install_dir, monkeypatch, capsys):
    monkeypatch.setattr(auth, "signed_in_email", lambda c: OTHER)
    with pytest.raises(SystemExit):
        auth.check_account(object())
    err = capsys.readouterr().err
    assert "wrong account" in err and OTHER in err and ACCOUNT in err and "Nothing was done" in err


# ------------------------------------------------------------------ login

class FakeCreds:
    def __init__(self, scopes, refresh="REFRESH"):
        self.refresh_token = refresh
        self._scopes = scopes

    def to_json(self):
        return json.dumps({"token": "T", "refresh_token": self.refresh_token, "scopes": self._scopes})


def fake_flow(monkeypatch, creds):
    import google_auth_oauthlib.flow as flow_mod

    class Flow:
        seen = {}

        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            cls.seen["scopes"] = scopes
            return cls()

        def run_local_server(self, **kw):
            Flow.seen["kw"] = kw
            return creds

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", Flow)
    return Flow


def test_login_stores_the_token_when_account_and_scopes_are_right(install_dir, keychain, monkeypatch, capsys):
    (install_dir / "client_secret.json").write_text("{}")
    flow = fake_flow(monkeypatch, FakeCreds(registry.all_scopes()))
    monkeypatch.setattr(auth, "signed_in_email", lambda c: ACCOUNT)
    auth.login()
    assert flow.seen["scopes"] == registry.all_scopes()
    assert flow.seen["kw"]["login_hint"] == ACCOUNT and flow.seen["kw"]["prompt"] == "consent"
    assert (registry.KEYCHAIN_SERVICE, ACCOUNT) in keychain.store
    out = capsys.readouterr().out
    assert "REFRESH" not in out                                   # the token is never printed


def test_login_as_the_wrong_account_stores_nothing(install_dir, keychain, monkeypatch, capsys):
    (install_dir / "client_secret.json").write_text("{}")
    fake_flow(monkeypatch, FakeCreds(registry.all_scopes()))
    monkeypatch.setattr(auth, "signed_in_email", lambda c: OTHER)
    with pytest.raises(SystemExit):
        auth.login()
    assert keychain.store == {}
    assert "Nothing was saved" in capsys.readouterr().err


def test_login_with_an_unticked_permission_stores_nothing(install_dir, keychain, monkeypatch, capsys):
    (install_dir / "client_secret.json").write_text("{}")
    fake_flow(monkeypatch, FakeCreds(registry.all_scopes()[:-1]))
    monkeypatch.setattr(auth, "signed_in_email", lambda c: ACCOUNT)
    with pytest.raises(SystemExit):
        auth.login()
    assert keychain.store == {}
    assert "Missing" in capsys.readouterr().err


# ------------------------------------------------------------------ logout / revoke

@pytest.fixture
def post(monkeypatch):
    calls = []
    answer = {"v": (200, "{}")}

    def fake_post(url, data):
        calls.append((url, data))
        return answer["v"]

    monkeypatch.setattr(auth, "_post", fake_post)
    return calls, answer


def test_logout_revokes_at_google_and_deletes_the_keychain_entry(install_dir, keychain, post, capsys):
    calls, _ = post
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps(stored()))
    assert auth.logout() is True
    (url, data), = calls
    assert url == "https://oauth2.googleapis.com/revoke"
    assert data == b"token=REFRESH-TOKEN-VALUE"                    # in the body, not in the URL
    assert "REFRESH-TOKEN-VALUE" not in url
    assert keychain.store == {}
    out = capsys.readouterr()
    assert "REFRESH-TOKEN-VALUE" not in out.out + out.err


def test_logout_keeps_the_entry_when_google_cannot_be_reached(install_dir, keychain, post, capsys):
    calls, answer = post
    answer["v"] = (500, "boom")
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps(stored()))
    assert auth.logout() is False
    assert (registry.KEYCHAIN_SERVICE, ACCOUNT) in keychain.store
    assert "--force-local" in capsys.readouterr().err
    assert auth.logout(force_local=True) is True
    assert keychain.store == {}


def test_logout_treats_already_revoked_as_success(install_dir, keychain, post):
    _, answer = post
    answer["v"] = (400, '{"error": "invalid_token"}')
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps(stored()))
    assert auth.logout() is True and keychain.store == {}


def test_logout_with_nothing_stored_is_fine(install_dir, keychain, post):
    calls, _ = post
    assert auth.logout() is True and calls == []


def test_logout_command_exit_code(install_dir, keychain, post):
    _, answer = post
    answer["v"] = (503, "")
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps(stored()))
    with pytest.raises(SystemExit) as e:
        google_cli.main(["logout"])
    assert e.value.code == 1


# ------------------------------------------------------------------ whoami

def test_whoami_not_signed_in(install_dir, capsys):
    with pytest.raises(SystemExit):
        google_cli.main(["whoami"])
    assert "bertie login" in capsys.readouterr().out


def test_whoami_match_and_mismatch(install_dir, keychain, monkeypatch, capsys):
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps(stored(expiry="2099-01-01T00:00:00Z")))
    monkeypatch.setattr(auth, "signed_in_email", lambda c: ACCOUNT)
    google_cli.main(["whoami"])
    out = capsys.readouterr().out
    assert "->  match" in out and "MISMATCH" not in out
    monkeypatch.setattr(auth, "signed_in_email", lambda c: OTHER)
    with pytest.raises(SystemExit):
        google_cli.main(["whoami"])
    assert "MISMATCH" in capsys.readouterr().out


# ------------------------------------------------------------------ "API not enabled"

def http_error(status, payload):
    from googleapiclient.errors import HttpError
    import httplib2
    return HttpError(httplib2.Response({"status": status}), json.dumps(payload).encode())


NOT_ENABLED = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                         "message": "Google Calendar API has not been used in project 123 before or it is disabled.",
                         "errors": [{"reason": "accessNotConfigured"}]}}
NOT_ENABLED_NEW = {"error": {"code": 403, "message": "Google Chat API has not been used in project 1 before or it is disabled.",
                             "details": [{"reason": "SERVICE_DISABLED"}]}}


ONLY_TEXT = {"error": {"code": 403, "message": "Gmail API has not been used in project 9 before."}}
ONLY_TEXT_2 = {"error": {"code": 403, "message": "The Drive API is disabled for this project"}}


@pytest.mark.parametrize("payload", [NOT_ENABLED, NOT_ENABLED_NEW, ONLY_TEXT, ONLY_TEXT_2])
def test_classify_not_enabled(payload):
    kind, _ = common.classify_http_error(http_error(403, payload))
    assert kind == "not_enabled"


def test_classify_chat_app_missing_and_other():
    kind, _ = common.classify_http_error(http_error(404, {"error": {"message": "Google Chat app not found."}}))
    assert kind == "chat_app"
    kind, _ = common.classify_http_error(http_error(500, {"error": {"message": "backend"}}))
    assert kind == "other"
    kind, _ = common.classify_http_error(http_error(403, {"error": {"message": "Insufficient Permission"}}))
    assert kind == "other"


def test_check_explains_a_switched_off_service_and_still_passes(install_dir, monkeypatch, capsys):
    import svc_calendar, svc_chat, svc_drive, svc_mail
    monkeypatch.setattr(svc_mail, "smoke", lambda: "Mail: OK")
    monkeypatch.setattr(svc_chat, "smoke", lambda: "Chat: OK")
    monkeypatch.setattr(svc_drive, "smoke", lambda: "Drive: OK")

    def boom():
        raise http_error(403, NOT_ENABLED)

    monkeypatch.setattr(svc_calendar, "smoke", boom)
    assert google_cli.run_check() == 0
    out = capsys.readouterr().out
    assert "Mail: OK" in out and "Chat: OK" in out and "Drive: OK" in out
    assert "Google Calendar is not switched on yet in your Google Cloud project" in out
    assert "Google Calendar API" in out


def test_check_fails_on_a_real_error(install_dir, monkeypatch, capsys):
    import svc_calendar, svc_chat, svc_drive, svc_mail
    for m, ok in [(svc_mail, "Mail"), (svc_chat, "Chat"), (svc_drive, "Drive")]:
        monkeypatch.setattr(m, "smoke", lambda ok=ok: f"{ok}: OK")

    def boom():
        raise http_error(500, {"error": {"message": "backend error"}})

    monkeypatch.setattr(svc_calendar, "smoke", boom)
    assert google_cli.run_check() == 1
    assert "FAILED" in capsys.readouterr().out
