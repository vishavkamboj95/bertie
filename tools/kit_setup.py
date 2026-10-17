#!/usr/bin/env python3
"""Install-time and uninstall-time work, written once in Python so the
installer and the uninstaller cannot drift apart.

What it manages (and records in install-manifest.json, so that uninstalling
removes exactly what installing added and restores nothing else):
  * the commands in ~/.local/bin
  * the Claude Code skill
  * permissions.ask rules and the PreToolUse hook in ~/.claude/settings.json
  * the PATH line in ~/.zshrc (only if the installer had to add it)
  * for any other agent that was asked for (see tools/agents/__init__.py), what
    its adapter adds, recorded under "agents" in the same manifest

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
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402

# Run as a script this file is `__main__`. Register it under its own name too, so the adapters
# (which `import kit_setup`) get this very module, and this very SetupError, not a second copy.
sys.modules.setdefault("kit_setup", sys.modules[__name__])

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


def _backup_file(path: Path) -> Path:
    """Copy `path` to <name>.bertie-backup-<UTC time> beside it; returns the copy."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    b = path.with_name(f"{path.name}.{registry.NAME}-backup-{stamp}")
    n = 1
    while b.exists():
        b = path.with_name(f"{path.name}.{registry.NAME}-backup-{stamp}-{n}")
        n += 1
    shutil.copy2(str(path), str(b))
    return b


def _backup(path: Path, manifest: Dict[str, Any]) -> Path:
    b = _backup_file(path)
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

def preflight(ctx: Ctx, kit_dir: Path, agent_keys: Optional[Sequence[str]] = None) -> None:
    """Every selected adapter's refusals, found before anything is written: a file that is not
    ours, a link, JSON that does not parse. SetupError names the first one. An adapter
    without a `preflight(ctx, kit_dir)` has nothing to check up front."""
    import agents
    keys = list(dict.fromkeys(agent_keys if agent_keys is not None else ["claude"]))
    for adapter in [agents.get(k) for k in keys]:
        check = getattr(adapter, "preflight", None)
        if check is not None:
            try:
                check(ctx, Path(kit_dir))
            except (OSError, UnicodeDecodeError) as e:
                raise SetupError(f"{adapter.LABEL}: could not look at its files ({agents.why(e)}). Nothing was changed.")


def _install_agent(adapter: Any, ctx: Ctx, kit_dir: Path) -> List[str]:
    """Run an adapter's install; a failure of the disk or of a file's text (as opposed to a
    refusal) is reported plainly, not as a traceback. Claude Code's own install is left as it was."""
    if adapter.KEY == "claude":
        return adapter.install(ctx, kit_dir)
    try:
        return adapter.install(ctx, kit_dir)
    except (OSError, UnicodeDecodeError) as e:
        import agents
        raise SetupError(f"{adapter.LABEL}: could not finish the setup ({agents.why(e)}). "
                         "Run the installer again once that is fixed; it carries on where it stopped.")


def install(ctx: Ctx, kit_dir: Path, add_path: bool, agent_keys: Optional[Sequence[str]] = None,
            options: Optional[Dict[str, str]] = None) -> List[str]:
    """Commands, then Claude Code's skill and settings (if selected), the PATH line, then every
    other selected agent's adapter. `agent_keys` defaults to Claude Code alone; `options` are
    installer choices for the adapters (for example {"codex_sends": "allow"}). Every selected
    adapter is looked over first (see preflight), so a refusal leaves nothing half done."""
    import agents
    keys = list(dict.fromkeys(agent_keys if agent_keys is not None else ["claude"]))
    adapters = [agents.get(k) for k in keys]       # an unknown or missing adapter stops everything first
    if options:
        agents.set_options(ctx, **options)
    preflight(ctx, kit_dir, keys)
    msgs: List[str] = []
    msgs += write_wrappers(ctx)
    for a in adapters:
        if a.KEY == "claude":
            msgs += _install_agent(a, ctx, kit_dir)
    if add_path and add_path_line(ctx):
        msgs.append("added ~/.local/bin to PATH in ~/.zshrc")
    for a in adapters:
        if a.KEY != "claude":
            msgs += _install_agent(a, ctx, kit_dir)
    return msgs


def _uninstall_other_agents(ctx: Ctx) -> List[str]:
    """Remove what each recorded non-Claude adapter added. Only what the manifest records
    is ever touched: an entry for an unknown agent, or one whose adapter is missing, is left
    as it is and said so."""
    import agents
    done: List[str] = []
    manifest = load_manifest(ctx)
    edited = agents.edited_files(manifest)
    for key in agents.recorded_keys(manifest):
        if key == "claude":
            continue
        try:
            adapter = agents.get(key)
        except SetupError as e:
            done.append(f"left the '{key}' entries alone: {e}")
            continue
        try:
            done += adapter.uninstall(ctx)
        except (SetupError, OSError, ValueError) as e:       # ValueError includes UnicodeDecodeError
            done.append(f"{adapter.LABEL}: could not remove everything ({e}); left as it is")
    try:
        if agents.remove_shared_skill(ctx):
            done.append(f"removed the shared skill ~/{agents.SHARED_SKILL_REL}")
    except (SetupError, OSError, ValueError) as e:
        done.append(f"could not remove the shared skill ({e}); left as it is")
    if agents.backups_beside(ctx, edited):
        done.append(f"backups named *.{registry.NAME}-backup-* were left beside the files they belong to")
    return done


def uninstall_local(ctx: Ctx) -> List[str]:
    """Everything except the install folder itself (the caller removes that last)."""
    import agents
    done: List[str] = []
    manifest = load_manifest(ctx)
    # Claude Code is cleaned as it always was (settings, PATH line, commands, skill), unless the
    # manifest shows a newer install that never set it up: then its files are not ours to touch
    claude = "settings" in manifest or "agents" not in manifest
    if claude:
        done += agents.get("claude").uninstall_settings(ctx)
    if remove_path_line(ctx):
        done.append("removed the PATH line from ~/.zshrc")
    cmds = remove_wrappers(ctx)
    if cmds:
        done.append("removed the commands: " + ", ".join(cmds))
    if claude:
        done += agents.get("claude").uninstall_skill(ctx)
    done += _uninstall_other_agents(ctx)
    return done


def leftover_notes(ctx: Ctx) -> List[str]:
    """Plain steps for finishing by hand, or nothing. Call after uninstall_local: any file that
    still tells an agent to run this install's hook.sh means the install folder must stay (the
    agent would look for the hook there and not find it), so these say what to delete and then
    how to remove the folder."""
    import agents
    left = agents.hook_leftovers(ctx)
    if not left:
        return []
    folder = agents.tilde(ctx, ctx.install_dir)
    lines = [f"Not everything could be taken out, so the install folder {folder} was KEPT. "
             "These still tell an agent to run its safety hook:"]
    lines += [f"  {label}: {where}  ({hint})" for label, where, hint in left]
    lines.append(f"Take those out by hand, then delete the folder {folder} (in Terminal: rm -r {folder}).")
    return lines


def main(argv: Optional[List[str]] = None) -> None:
    import agents
    ap = argparse.ArgumentParser(prog="kit_setup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--kit-dir", required=True)
    i.add_argument("--add-path", action="store_true")
    i.add_argument("--agents", default=None,
                   help="comma-separated agent keys or 'all' (default: claude plus every agent found)")
    i.add_argument("--codex-sends", choices=("prompt", "allow"), default=None,
                   help="what Codex does before a gated send (default: prompt)")
    r = sub.add_parser("resolve", help="print the agents an install would set up")
    r.add_argument("--agents", default=None)
    r.add_argument("--names", action="store_true", help="print their names, not their keys")
    c = sub.add_parser("check", help="look over what an install would touch; changes nothing")
    c.add_argument("--kit-dir", required=True)
    c.add_argument("--agents", default=None)
    c.add_argument("--codex-sends", choices=("prompt", "allow"), default=None)
    sub.add_parser("uninstall-local")
    args = ap.parse_args(argv)
    home = Path(os.path.expanduser("~"))
    # check runs from the downloaded folder, before the install folder exists: the install is where it will be
    ctx = Ctx(home) if args.cmd == "check" else Ctx(home, Path(__file__).resolve().parent)
    try:
        if args.cmd in ("install", "resolve", "check"):
            try:
                keys = (agents.parse_agents(args.agents) if args.agents is not None
                        else agents.default_selection(ctx.home))
            except SetupError as e:
                ap.error(str(e))                    # usage and exit 2, nothing touched
        if args.cmd == "install":
            options = {"codex_sends": args.codex_sends} if args.codex_sends else None
            for m in install(ctx, Path(args.kit_dir), args.add_path, keys, options):
                print("  " + m)
        elif args.cmd == "resolve":
            print(agents.join_labels(keys) if args.names else ",".join(keys))
        elif args.cmd == "check":
            if args.codex_sends:
                agents.set_options(ctx, codex_sends=args.codex_sends)
            preflight(ctx, Path(args.kit_dir), keys)
        else:
            for m in uninstall_local(ctx) or ["nothing to remove"]:
                print("  " + m)
            for m in leftover_notes(ctx):
                print("  " + m)
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
