"""Whole-kit checks: every gated action is wired everywhere, nothing destructive
exists, the files stay clean and Python 3.9 can still read the code."""
import ast
import importlib
import re
from pathlib import Path

import pytest

import registry
from conftest import KIT, TOOLS

TEXT_FILES = [p for p in KIT.rglob("*")
              if p.is_file() and p.suffix in {".py", ".sh", ".md", ".txt", ".in", ".ini"}
              and ".pytest_cache" not in p.parts and "__pycache__" not in p.parts and ".venv" not in p.parts]


def subcommands(module):
    ap = module.build_parser()
    return ap._subparsers._group_actions[0].choices


@pytest.mark.parametrize("svc", registry.SERVICES, ids=lambda s: s.key)
def test_every_gated_action_exists_and_supports_dry_run(svc):
    mod = importlib.import_module(svc.module)
    subs = subcommands(mod)
    for act in svc.gated:
        assert act in subs, (svc.key, act)
        flags = {o for a in subs[act]._actions for o in a.option_strings}
        assert "--dry-run" in flags, (svc.key, act)


@pytest.mark.parametrize("svc", registry.SERVICES, ids=lambda s: s.key)
def test_every_write_command_supports_dry_run(svc):
    """A command is a write if its help starts with ASKS FIRST, or it is mail draft/untrash."""
    mod = importlib.import_module(svc.module)
    subs = subcommands(mod)
    helps = {a.dest: a.help for a in mod.build_parser()._subparsers._group_actions[0]._choices_actions}
    for name, sp in subs.items():
        h = helps.get(name) or ""
        if h.startswith("ASKS FIRST") or (svc.key == "mail" and name in {"draft"}):
            assert "--dry-run" in {o for a in sp._actions for o in a.option_strings}, (svc.key, name)


@pytest.mark.parametrize("svc", registry.SERVICES, ids=lambda s: s.key)
def test_service_modules_are_uniform(svc):
    mod = importlib.import_module(svc.module)
    assert callable(mod.main) and callable(mod.smoke) and callable(mod.build_parser)
    assert (TOOLS / f"{svc.module}.py").is_file()


def test_commands_match_the_agreed_names():
    assert [c for c, _ in registry.wrappers()] == [
        "bertie-mail", "bertie-calendar", "bertie-chat", "bertie-drive", "bertie"]


def test_every_name_is_made_from_the_one_name_constant():
    n = registry.NAME
    assert n == "bertie" and registry.DISPLAY_NAME == "Bertie"
    assert [c for c, _ in registry.wrappers()] == [f"{n}-mail", f"{n}-calendar", f"{n}-chat", f"{n}-drive", n]
    assert registry.KEYCHAIN_SERVICE == n and registry.GOOGLE_COMMAND == n
    assert registry.INSTALL_REL == f".config/{n}" and registry.SKILL_REL == f".claude/skills/{n}"
    assert registry.HELPER_NAME == f"{n}-approve" and n in registry.WRAPPER_MARKER
    assert (KIT / "skill" / n / "SKILL.md").is_file()
    skill = (KIT / "skill" / n / "SKILL.md").read_text()
    assert skill.startswith(f"---\nname: {n}\n")
    # the installer reads the name out of registry.py rather than repeating it
    sh = (KIT / "install.sh").read_text()
    assert 'NAME="$(sed -n' in sh and ".config/bertie" not in sh and "bertie-approve" not in sh


COMPANY = "celo" + "rus"            # spelled in pieces so this file is not itself a mention


def _repo_text_files():
    skip = {"NOTES.md"}             # the local working notes are never committed
    for p in sorted(KIT.rglob("*")):
        if (p.is_file() and p.name not in skip and ".git" not in p.parts and ".venv" not in p.parts
                and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts):
            try:
                yield p, p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue


def test_the_company_name_appears_once_as_the_made_by_credit():
    hits = [(p.relative_to(KIT), line) for p, text in _repo_text_files()
            for line in text.splitlines() if COMPANY in line.lower()]
    assert len(hits) == 1, hits
    path, line = hits[0]
    assert str(path) == "README.md" and line.lower().startswith("made by"), hits


def test_no_private_identifiers_anywhere():
    """The handed-over kit's Google project id and home-folder paths never come back."""
    for p, text in _repo_text_files():
        low = text.lower()
        assert "agent" + "-mail" not in low, p
        assert "/Use" + "rs/" not in text, p
        assert "@" + COMPANY not in low, p


def test_ask_rules_cover_the_gated_actions_named_in_the_brief():
    wanted = {"Bash(bertie-mail send:*)", "Bash(bertie-mail trash:*)", "Bash(bertie-calendar create:*)",
              "Bash(bertie-calendar move:*)", "Bash(bertie-calendar cancel:*)", "Bash(bertie-chat send:*)",
              "Bash(bertie-chat dm:*)", "Bash(bertie-drive upload:*)", "Bash(bertie-drive update:*)",
              "Bash(bertie-drive share:*)"}
    assert wanted <= set(registry.ask_rules())


def test_the_read_commands_are_not_gated():
    gated = registry.gated_actions()
    for tool, free in {"bertie-mail": ["whoami", "search", "read", "thread", "attachments", "drafts",
                                        "show-draft", "draft"],
                       "bertie-calendar": ["agenda", "freebusy", "event"],
                       "bertie-chat": ["spaces", "read"],
                       "bertie-drive": ["list", "ls", "download"],
                       "bertie": ["login", "whoami", "log", "check"]}.items():
        for act in free:
            assert act not in gated[tool], (tool, act)


def test_nothing_deletes_permanently_anywhere():
    total = 0
    for f in TOOLS.glob("*.py"):
        src = f.read_text()
        assert "batchDelete" not in src and "emptyTrash" not in src, f.name
        assert not re.search(r"messages\(\)\s*\.\s*(delete|batchDelete)", src), f.name
        assert not re.search(r"files\(\)\s*\.\s*(delete|emptyTrash)", src), f.name
        total += src.count(".delete(")
    assert total == 1                                              # Calendar's cancel, nothing else


def test_no_permanent_delete_command_names():
    for svc in registry.SERVICES:
        names = set(subcommands(importlib.import_module(svc.module)))
        assert not [n for n in names if any(w in n for w in ("delete", "purge", "remove", "empty", "erase"))], svc.key


@pytest.mark.parametrize("path", TEXT_FILES, ids=lambda p: str(p.relative_to(KIT)))
def test_files_hold_no_home_folder_path_and_no_em_dash(path):
    text = path.read_text(encoding="utf-8")
    assert ("/Use" + "rs/") not in text, "a home-folder path"
    assert chr(0x2014) not in text, "an em dash"


@pytest.mark.parametrize("path", sorted(TOOLS.glob("*.py")) + sorted(KIT.glob("tests/*.py")), ids=lambda p: p.name)
def test_code_parses_as_python_3_9(path):
    ast.parse(path.read_text(), feature_version=(3, 9))


def test_requirements_are_exact_and_hash_checked():
    lines = [l for l in (KIT / "requirements.txt").read_text().splitlines()
             if l and not l.startswith(("#", " "))]
    assert lines
    for l in lines:
        pkg = l.split(";")[0]
        assert "==" in pkg and ">=" not in pkg and "~=" not in pkg, l
    text = (KIT / "requirements.txt").read_text()
    assert text.count("--hash=sha256:") >= len(lines)
    names = {l.split("==")[0].lower() for l in lines}
    assert {"google-auth", "google-auth-oauthlib", "google-api-python-client", "keyring"} <= names


def test_the_installer_uses_hash_checking_and_is_posix_sh():
    sh = (KIT / "install.sh").read_text()
    assert sh.startswith("#!/bin/sh")
    assert sh.count("--require-hashes") == 2                        # uv path and venv path
    for bashism in ["[[", "local ", "function ", "<<<", "source "]:
        assert bashism not in sh, bashism


def test_no_oauth_client_or_token_ships_with_the_kit():
    """Everyone brings their own Google project: the kit holds no client file and no token."""
    found = [p for p in KIT.rglob("*") if p.is_file() and ".venv" not in p.parts
             and (p.name.startswith("client_secret") or p.name.startswith("token"))]
    assert found == []
    ignore = (KIT / ".gitignore").read_text()
    assert "client_secret*.json" in ignore and "token*.json" in ignore
