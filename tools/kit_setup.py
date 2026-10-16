#!/usr/bin/env python3
"""Install-time and uninstall-time work, written once in Python so the
installer and the uninstaller cannot drift apart.

What it manages (and records in install-manifest.json, so that uninstalling
removes exactly what installing added and restores nothing else):
  * the commands in ~/.local/bin
  * the Claude Code skill
  * permissions.ask rules and the PreToolUse hook in ~/.claude/settings.json
  * the PATH line in ~/.zshrc (only if the installer had to add it)

Standard library only. Every function takes the home folder explicitly so the
tests can run against a temporary one.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402

MANIFEST_NAME = "install-manifest.json"
HOOK_COMMAND = 'sh "$HOME/' + registry.INSTALL_REL + '/hook.sh"'
# Bash and the IDE terminal tool run commands; Write and Edit could write a script that does;
# the computer-use tools could click the confirmation window, so they always ask.
HOOK_MATCHERS = ("Bash|mcp__terminal__run_in_terminal", "Write|Edit", "mcp__computer-use__.*")
PATH_MARKER = "# " + registry.NAME + ": added by the installer so the commands are found"
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'


class SetupError(Exception):
    pass


class Ctx:
    def __init__(self, home: Path, install_dir: Optional[Path] = None):
        self.home = Path(home)
        self.install_dir = Path(install_dir) if install_dir else self.home / registry.INSTALL_REL

    @property
    def settings_path(self) -> Path:
        return self.home / registry.SETTINGS_REL

    @property
    def bin_dir(self) -> Path:
        return self.home / registry.BIN_REL

    @property
    def skill_dir(self) -> Path:
        return self.home / registry.SKILL_REL

    @property
    def rc_path(self) -> Path:
        return self.home / ".zshrc"

    @property
    def manifest_path(self) -> Path:
        return self.install_dir / MANIFEST_NAME


# ---------------------------------------------------------------- manifest

def load_manifest(ctx: Ctx) -> Dict[str, Any]:
    try:
        data = json.loads(ctx.manifest_path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_manifest(ctx: Ctx, manifest: Dict[str, Any]) -> None:
    ctx.install_dir.mkdir(parents=True, exist_ok=True)
    _write_text(ctx.manifest_path, json.dumps(manifest, indent=2) + "\n", 0o600)


def _write_text(path: Path, text: str, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp-" + registry.NAME)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------- settings.json

def _detect_format(raw: str):
    m = re.search(r'^([ \t]+)"', raw, re.M)
    indent = m.group(1) if m else "  "
    return indent, raw.endswith("\n")


def _read_settings(path: Path):
    """(data, indent, trailing_newline, existed). Refuses anything it cannot
    safely round-trip, rather than guessing."""
    if not path.exists():
        return {}, "  ", True, False
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}, "  ", True, True
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise SetupError(f"{path} is not valid JSON ({e}); left untouched. Fix it, then run again.")
    if not isinstance(data, dict):
        raise SetupError(f"{path} is not a JSON object; left untouched.")
    indent, nl = _detect_format(raw)
    return data, indent, nl, True


def _backup(path: Path, manifest: Dict[str, Any]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    b = path.with_name(f"{path.name}.{registry.NAME}-backup-{stamp}")
    n = 1
    while b.exists():
        b = path.with_name(f"{path.name}.{registry.NAME}-backup-{stamp}-{n}")
        n += 1
    shutil.copy2(str(path), str(b))
    manifest.setdefault("settings", {}).setdefault("backups", []).append(b.name)
    return b


def _write_settings(path: Path, data: Dict[str, Any], indent: str, nl: bool, existed: bool) -> None:
    text = json.dumps(data, indent=indent, ensure_ascii=False) + ("\n" if nl else "")
    mode = (path.stat().st_mode & 0o777) if existed else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, text, mode)


def _group_has_hook(group: Any, command: str) -> bool:
    return isinstance(group, dict) and any(
        isinstance(h, dict) and h.get("command") == command for h in group.get("hooks", []) or [])


def merge_settings(ctx: Ctx, rules: Optional[List[str]] = None,
                   hook_command: str = HOOK_COMMAND) -> bool:
    """Add the ask rules and the hook. Keeps every existing key, rule and hook.
    Idempotent: a second run changes nothing and makes no backup."""
    rules = list(rules if rules is not None else registry.ask_rules())
    manifest = load_manifest(ctx)
    sm = manifest.setdefault("settings", {})
    created = sm.setdefault("created", {})
    added_rules: List[str] = sm.setdefault("ask_added", [])

    data, indent, nl, existed = _read_settings(ctx.settings_path)
    if not existed:
        sm["created_file"] = True
    before = json.dumps(data, sort_keys=False)

    perms = data.get("permissions")
    if perms is None:
        perms = data["permissions"] = {}
        created["permissions"] = True
    if not isinstance(perms, dict):
        raise SetupError("settings.json: 'permissions' is not an object; left untouched.")
    ask = perms.get("ask")
    if ask is None:
        ask = perms["ask"] = []
        created["ask"] = True
    if not isinstance(ask, list):
        raise SetupError("settings.json: 'permissions.ask' is not a list; left untouched.")
    for r in rules:
        if r not in ask:
            ask.append(r)
            if r not in added_rules:
                added_rules.append(r)

    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
        created["hooks"] = True
    if not isinstance(hooks, dict):
        raise SetupError("settings.json: 'hooks' is not an object; left untouched.")
    pre = hooks.get("PreToolUse")
    if pre is None:
        pre = hooks["PreToolUse"] = []
        created["PreToolUse"] = True
    if not isinstance(pre, list):
        raise SetupError("settings.json: 'hooks.PreToolUse' is not a list; left untouched.")
    # one group per wanted matcher; an older install's narrower group is replaced
    wanted = list(HOOK_MATCHERS)
    for g in list(pre):
        if _group_has_hook(g, hook_command):
            only_ours = all(isinstance(h, dict) and h.get("command") == hook_command
                            for h in g.get("hooks", []))
            if g.get("matcher") in wanted:
                wanted.remove(g.get("matcher"))
            elif only_ours:
                pre.remove(g)                   # stale matcher from an earlier version
    for matcher in wanted:
        pre.append({"matcher": matcher, "hooks": [{"type": "command", "command": hook_command}]})
        sm["hook_added"] = True

    changed = json.dumps(data, sort_keys=False) != before or not existed
    if changed:
        if existed:
            _backup(ctx.settings_path, manifest)
        _write_settings(ctx.settings_path, data, indent, nl, existed)
    save_manifest(ctx, manifest)
    return changed


def unmerge_settings(ctx: Ctx, hook_command: str = HOOK_COMMAND) -> bool:
    """Remove exactly the rules and hook the installer added. Nothing else."""
    manifest = load_manifest(ctx)
    sm = manifest.get("settings")
    if sm is None:
        # no record (manifest deleted): fall back to what this kit would add
        sm = {"ask_added": registry.ask_rules(), "hook_added": True, "created": {}}
    if not ctx.settings_path.exists():
        return False
    data, indent, nl, _ = _read_settings(ctx.settings_path)
    before = json.dumps(data)
    created = sm.get("created", {})
    ours = set(sm.get("ask_added", []))

    perms = data.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("ask"), list):
        perms["ask"] = [r for r in perms["ask"] if r not in ours]
        if not perms["ask"] and created.get("ask"):
            del perms["ask"]
        if not perms and created.get("permissions"):
            del data["permissions"]

    hooks = data.get("hooks")
    if isinstance(hooks, dict) and isinstance(hooks.get("PreToolUse"), list):
        kept = []
        for g in hooks["PreToolUse"]:
            if _group_has_hook(g, hook_command):
                g = dict(g)
                g["hooks"] = [h for h in g.get("hooks", []) if not (
                    isinstance(h, dict) and h.get("command") == hook_command)]
                if not g["hooks"]:
                    continue          # the group held only our hook
            kept.append(g)
        hooks["PreToolUse"] = kept
        if not kept and created.get("PreToolUse"):
            del hooks["PreToolUse"]
        if not hooks and created.get("hooks"):
            del data["hooks"]

    if json.dumps(data) == before:
        return False
    _backup(ctx.settings_path, manifest)
    if sm.get("created_file") and not data:
        ctx.settings_path.unlink()
    else:
        _write_settings(ctx.settings_path, data, indent, nl, True)
    return True


# ---------------------------------------------------------------- wrappers

def wrapper_text(command: str, script: str) -> str:
    base = '"$HOME/' + registry.INSTALL_REL
    return (
        "#!/bin/sh\n"
        f"{registry.WRAPPER_MARKER}: {command} (installed by the {registry.NAME} installer)\n"
        f'exec {base}/.venv/bin/python" -I -W ignore {base}/{script}" "$@"\n'
    )


def write_wrappers(ctx: Ctx) -> List[str]:
    notes: List[str] = []
    ctx.bin_dir.mkdir(parents=True, exist_ok=True)
    names: List[str] = []
    for command, script in registry.wrappers():
        p = ctx.bin_dir / command
        if p.exists() or p.is_symlink():
            try:
                existing = p.read_text()
            except (OSError, UnicodeDecodeError):
                existing = ""
            if registry.WRAPPER_MARKER not in existing:
                raise SetupError(f"{p} already exists and was not made by this installer. "
                                 "Move it out of the way and run the installer again.")
            p.unlink()
        p.write_text(wrapper_text(command, script))
        p.chmod(0o755)
        names.append(command)
    manifest = load_manifest(ctx)
    manifest["commands"] = names
    save_manifest(ctx, manifest)
    return notes


def remove_wrappers(ctx: Ctx) -> List[str]:
    removed = []
    for command, _ in registry.wrappers():
        p = ctx.bin_dir / command
        try:
            if p.is_file() and registry.WRAPPER_MARKER in p.read_text():
                p.unlink()
                removed.append(command)
        except (OSError, UnicodeDecodeError):
            continue
    return removed


# ---------------------------------------------------------------- skill

def install_skill(ctx: Ctx, kit_dir: Path) -> None:
    src = Path(kit_dir) / "skill" / registry.NAME / "SKILL.md"
    if not src.is_file():
        raise SetupError(f"missing {src}")
    ctx.skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(ctx.skill_dir / "SKILL.md"))


def remove_skill(ctx: Ctx) -> bool:
    f = ctx.skill_dir / "SKILL.md"
    if not f.exists():
        return False
    f.unlink()
    try:
        ctx.skill_dir.rmdir()      # only if nothing else is in it
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- PATH line

def add_path_line(ctx: Ctx) -> bool:
    rc = ctx.rc_path
    text = rc.read_text() if rc.exists() else ""
    if PATH_MARKER in text or ".local/bin" in text:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    text += PATH_MARKER + "\n" + PATH_LINE + "\n"
    existed = rc.exists()
    mode = (rc.stat().st_mode & 0o777) if existed else 0o644
    _write_text(rc, text, mode)
    manifest = load_manifest(ctx)
    manifest["path_line"] = {"rc": ".zshrc", "added": True, "rc_created": not existed}
    save_manifest(ctx, manifest)
    return True


def remove_path_line(ctx: Ctx) -> bool:
    rc = ctx.rc_path
    if not rc.exists():
        return False
    lines = rc.read_text().split("\n")
    for i, line in enumerate(lines):
        if line == PATH_MARKER and i + 1 < len(lines) and lines[i + 1] == PATH_LINE:
            del lines[i:i + 2]
            new_text = "\n".join(lines)
            if not new_text.strip() and load_manifest(ctx).get("path_line", {}).get("rc_created"):
                rc.unlink()          # we made the file, and nothing else is in it
            else:
                _write_text(rc, new_text, rc.stat().st_mode & 0o777)
            return True
    return False


# ---------------------------------------------------------------- orchestration

def install(ctx: Ctx, kit_dir: Path, add_path: bool) -> List[str]:
    msgs: List[str] = []
    msgs += write_wrappers(ctx)
    install_skill(ctx, kit_dir)
    msgs.append(f"installed the skill '{registry.NAME}'")
    changed = merge_settings(ctx)
    msgs.append("approval rules and safety hook " + ("added to" if changed else "already in")
                + " ~/.claude/settings.json")
    if add_path and add_path_line(ctx):
        msgs.append("added ~/.local/bin to PATH in ~/.zshrc")
    return msgs


def uninstall_local(ctx: Ctx) -> List[str]:
    """Everything except the install folder itself (the caller removes that last)."""
    done: List[str] = []
    if unmerge_settings(ctx):
        done.append("removed the approval rules and safety hook from ~/.claude/settings.json")
    if remove_path_line(ctx):
        done.append("removed the PATH line from ~/.zshrc")
    cmds = remove_wrappers(ctx)
    if cmds:
        done.append("removed the commands: " + ", ".join(cmds))
    if remove_skill(ctx):
        done.append(f"removed the skill '{registry.NAME}'")
    return done


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="kit_setup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--kit-dir", required=True)
    i.add_argument("--add-path", action="store_true")
    sub.add_parser("uninstall-local")
    args = ap.parse_args(argv)
    ctx = Ctx(Path(os.path.expanduser("~")), Path(__file__).resolve().parent)
    try:
        if args.cmd == "install":
            for m in install(ctx, Path(args.kit_dir), args.add_path):
                print("  " + m)
        else:
            for m in uninstall_local(ctx) or ["nothing to remove"]:
                print("  " + m)
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
