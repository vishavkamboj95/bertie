"""Bertie for other coding agents: one small adapter module per agent.

Bertie's commands, the Mac window and Touch ID protect every agent. An adapter only
teaches the installer and the safety hook one agent's file locations and formats.
Claude Code is the first adapter and its behaviour is the original one.

THE ADAPTER INTERFACE. tools/agents/<key>.py exposes exactly this (standard library only,
Python 3.9 compatible; `import hook` only inside hook_input, never at the top):

  KEY: str          the key, equal to the file name ("kimi")
  LABEL: str        what people read ("Kimi Code")
  HOOK_STYLE: str   "ask" if the agent's hook can ask the person, "block" if it can only
                    allow or deny

  detect(home: Path) -> bool
      True when the agent's config folder exists under `home`. Looks at nothing else (not
      PATH, not /Applications), so tests in a temporary HOME are deterministic.

  install(ctx, kit_dir: Path) -> List[str]
      Set the agent up and return plain-English report lines for the installer to print.
      Start each line with LABEL and a colon (Claude Code's own lines are the original
      ones and have no label). End with one line saying how the agent is protected and
      what the person still has to do. Record every file and every edit under
      manifest["agents"][KEY] with the helpers below. Raise SetupError, before changing
      anything of its own where it can, if a file it would write is not its own.

  uninstall(ctx) -> List[str]
      Undo exactly what the manifest records for KEY and nothing else. Call
      drop_section(ctx, KEY) at the end. Return plain-English report lines.

  hook_input(data: dict) -> Tuple[str, str]
      The agent's stdin JSON as (kind, text). kind is "command" (text is the shell command),
      "file" (text is what is being written), "none" (nothing to check; the tool is not one
      of ours to watch) or "ask" (always ask or block, text is the reason; only Claude Code's
      screen-control tools use this). Raise on anything unreadable (a command tool with no
      command text at all, say): hook.py then answers with hook_failure. Never guess. A
      command that is present but blank is not unreadable; it is simply nothing to check.

  hook_answer(reason: Optional[str]) -> Tuple[str, int]
      What to print for a decision: reason is None for "no opinion", else the text to show.
      Returns (stdout text, exit code). The exit code must be 0: hook.py always exits 0 and
      speaks a block in JSON, because hook.sh retries the next Python on any other exit.

  hook_failure(reason: str) -> Tuple[str, int]
      What to print when anything goes wrong. Always the safe answer: ask for an ask-style
      agent, deny for a block-style one. Exit code 0.

  rules_cover(command: str) -> bool        (block-style agents only)
      True only when the agent's own ask rule surely matches this exact command string, so
      the agent will prompt (and the Mac window and Touch ID follow). Must be conservative.

  hook_scan_text(data: dict, command: str) -> Optional[str]       (optional; agents that say where a command runs)
      The text the checks read for a command instead of the command itself: `cd <folder> && ` put in
      front by hook.cwd_prefix (only where the folder can matter), then the command. None when the
      command alone is what to read. Raise on a folder that is not text.

  hook_cwd_known(data: dict) -> bool                              (optional, with hook_scan_text)
      True when the agent gave the folder the command runs in as a full path (hook.folder_given with
      the same folders hook_scan_text reads). A bare `.cursor` in the command then means that
      folder's own, not the one in the home folder (until the command changes folder itself).

Which functions hook.py runs: hook.py --agent <key> reads stdin, calls hook_input, runs
decide() (command) or decide_file_text() (file), applies the block-only policy for
HOOK_STYLE "block" (a gated action alone, covered by rules_cover, is left to the agent's
rule; everything else is blocked), then calls hook_answer. Any exception calls hook_failure.

What this module provides to the adapters:

  get(key), detect_present(home), default_selection(home), parse_agents(text), labels(keys), join_labels(keys)
  option(ctx, name, default), set_options(ctx, **opts)           installer choices, e.g. codex_sends
  hook_command(ctx, key)                                         "sh <quoted path>/hook.sh --agent <key>"
  pretooluse_answer(decision, reason)                            the Claude-style JSON line (Claude, Codex, Kimi)
  section / peek / drop_section / recorded_keys                  manifest["agents"][key]
  json_merge / json_unmerge with Append, SetKey, Require         edits to a user's JSON file, undone exactly
  write_owned_file / remove_owned_file                           a file wholly Bertie's, with an ownership check
  install_skill_copy / remove_skill_copy                         a neutral SKILL.md of Bertie's own
  acquire_shared_skill / release_shared_skill                    ~/.agents/skills/bertie, counted per agent
  backup_file, write_text, make_dirs, prune_dirs                 for edits no helper covers (a TOML block)
  neutral_skill_text(text)                                       SKILL.md without the Claude-specific words

The rule behind all of them: never overwrite a file Bertie did not create (SetupError
instead), back up a file before editing it, and take back out exactly what was put in.
"""
from __future__ import annotations

import copy
import glob
import hashlib
import importlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kit_setup  # noqa: E402
import registry  # noqa: E402
from kit_setup import SetupError, load_manifest, save_manifest  # noqa: E402,F401

ADAPTERS = ("claude", "antigravity", "cursor", "codex", "kimi")
SHARED_SKILL_REL = ".agents/skills/" + registry.NAME + "/SKILL.md"

write_text = kit_setup._write_text            # atomic write with a mode: temp file, then rename


# ---------------------------------------------------------------- finding adapters

def get(key: str):
    """The adapter module for `key`, imported on first use. SetupError for a key that is
    not in ADAPTERS, or whose file is not part of this copy of the kit."""
    if key not in ADAPTERS:
        raise SetupError(f"unknown agent '{key}'. Choose from: {', '.join(ADAPTERS)}.")
    name = f"{__name__}.{key}"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name == name:
            raise SetupError(f"the {key} adapter is missing from this copy of {registry.NAME} "
                             f"(agents/{key}.py). Download the setup folder again.")
        raise


def detect_present(home) -> List[str]:
    """Keys of the agents whose config folder exists under `home`, in ADAPTERS order.
    An adapter that is not part of this copy of the kit is skipped."""
    found: List[str] = []
    for key in ADAPTERS:
        try:
            adapter = get(key)
        except SetupError:
            continue
        if adapter.detect(Path(home)):
            found.append(key)
    return found


def default_selection(home) -> List[str]:
    """What the installer sets up when told nothing: Claude Code always (as in v0.1), plus
    every other agent found."""
    present = detect_present(home)
    return ["claude"] + [k for k in ADAPTERS if k != "claude" and k in present]


def parse_agents(text: str) -> List[str]:
    """'claude,kimi' or 'all' -> keys in ADAPTERS order, no repeats. SetupError for anything else."""
    wanted = set()
    for part in text.split(","):
        part = part.strip()
        if part == "all":
            wanted.update(ADAPTERS)
        elif part in ADAPTERS:
            wanted.add(part)
        else:
            raise SetupError(f"unknown agent '{part}'. Choose from: {', '.join(ADAPTERS)}, or 'all'.")
    return [k for k in ADAPTERS if k in wanted]


def labels(keys: Sequence[str]) -> List[str]:
    """What people read for each key ('Kimi Code'); the key itself if its adapter is missing."""
    names = []
    for k in keys:
        try:
            names.append(get(k).LABEL)
        except SetupError:
            names.append(k)
    return names


def join_labels(keys: Sequence[str]) -> str:
    """'Claude Code', 'Claude Code or Kimi Code', 'Claude Code, Kimi Code or Codex'."""
    names = labels(keys)
    return names[0] if len(names) < 2 else ", ".join(names[:-1]) + " or " + names[-1]


# ---------------------------------------------------------------- installer choices

def set_options(ctx, **opts: str) -> None:
    """Attach installer choices (for example codex_sends="allow") to a Ctx."""
    current = dict(getattr(ctx, "options", None) or {})
    current.update(opts)
    ctx.options = current


def option(ctx, name: str, default: str) -> str:
    return (getattr(ctx, "options", None) or {}).get(name, default)


# ---------------------------------------------------------------- the hook command and answers

_KEY_RE = re.compile(r"[a-z][a-z0-9-]*")


def hook_command(ctx, key: str) -> str:
    """What a non-Claude agent runs: sh <quoted real path of hook.sh> --agent <key>.
    (Claude Code keeps kit_setup.HOOK_COMMAND, which uses $HOME.)"""
    if not _KEY_RE.fullmatch(key):
        raise SetupError(f"not an agent key: {key!r}")
    return "sh " + shlex.quote(str(Path(ctx.install_dir) / "hook.sh")) + " --agent " + key


def pretooluse_answer(decision: str, reason: str) -> str:
    """The one-line JSON Claude Code, Codex and Kimi Code read from a PreToolUse hook."""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}) + "\n"


# ---------------------------------------------------------------- the manifest

def peek(manifest: Dict[str, Any], key: str) -> Dict[str, Any]:
    """manifest["agents"][key] if it is there and well formed, else an empty dict (not stored)."""
    agents_ = manifest.get("agents")
    sec = agents_.get(key) if isinstance(agents_, dict) else None
    return sec if isinstance(sec, dict) else {}


def section(manifest: Dict[str, Any], key: str) -> Dict[str, Any]:
    """manifest["agents"][key], created if missing. A damaged entry becomes an empty one:
    nothing recorded means nothing removed."""
    agents_ = manifest.get("agents")
    if not isinstance(agents_, dict):
        agents_ = manifest["agents"] = {}
    sec = agents_.get(key)
    if not isinstance(sec, dict):
        sec = agents_[key] = {}
    return sec


def recorded_keys(manifest: Dict[str, Any]) -> List[str]:
    """Keys under manifest["agents"]: ADAPTERS first in their order, then any others."""
    agents_ = manifest.get("agents")
    if not isinstance(agents_, dict):
        return []
    return [k for k in ADAPTERS if k in agents_] + [k for k in agents_ if k not in ADAPTERS]


def drop_section(ctx, key: str) -> None:
    manifest = load_manifest(ctx)
    agents_ = manifest.get("agents")
    if isinstance(agents_, dict) and key in agents_:
        del agents_[key]
        if not agents_:
            del manifest["agents"]
        save_manifest(ctx, manifest)


def backup_file(ctx, key: str, path: Path) -> Path:
    """Copy `path` beside itself (<name>.bertie-backup-<time>) and record the copy's name."""
    b = kit_setup._backup_file(path)
    manifest = load_manifest(ctx)
    section(manifest, key).setdefault("backups", []).append(b.name)
    save_manifest(ctx, manifest)
    return b


# ---------------------------------------------------------------- folders Bertie made

def _under_home(ctx, rel: str) -> Path:
    p = Path(rel)
    if not rel or p.is_absolute() or ".." in p.parts:
        raise SetupError(f"refusing a path outside the home folder: {rel!r}")
    return Path(ctx.home) / p


def json_files_recorded(manifest: dict) -> List[str]:
    """Every JSON file (path relative to home) the manifest says some non-Claude adapter edited."""
    out: List[str] = []
    for key in recorded_keys(manifest):
        recs = peek(manifest, key).get("json")
        if key != "claude" and isinstance(recs, dict):
            out += [rel for rel in recs if isinstance(rel, str)]
    return out


def edited_files(manifest: dict) -> List[str]:
    """Every file some non-Claude adapter edited in place and so made a backup of: the JSON
    files, and any text file an adapter lists under its "edited" (a path relative to home,
    or an absolute one)."""
    out = json_files_recorded(manifest)
    for key in recorded_keys(manifest):
        extra = peek(manifest, key).get("edited")
        if key != "claude" and isinstance(extra, list):
            out += [rel for rel in extra if isinstance(rel, str) and rel not in out]
    return out


def _edited_path(ctx, rel: str) -> Path:
    return Path(rel) if Path(rel).is_absolute() else _under_home(ctx, rel)


def tilde(ctx, path) -> str:
    """A path as people read it: ~/.cursor/hooks.json for a file under the home folder."""
    try:
        return "~/" + str(Path(path).relative_to(ctx.home))
    except ValueError:
        return str(path)


def files_with_backups(ctx, rels: List[str]) -> List[str]:
    """Which of these files (relative to home, or absolute) have a backup made by this
    installer beside them, written the way people read paths (~/...)."""
    out: List[str] = []
    for rel in rels:
        try:
            path = _edited_path(ctx, rel)
            if any(path.parent.glob(glob.escape(path.name) + f".{registry.NAME}-backup-*")):
                out.append(tilde(ctx, path))
        except (SetupError, OSError):
            continue
    return out


def backups_beside(ctx, rels: List[str]) -> bool:
    """True if a backup made by this installer sits beside any of these files (relative to home)."""
    return bool(files_with_backups(ctx, rels))


def make_dirs(ctx, directory: Path, created: List[str]) -> None:
    """mkdir -p, adding to `created` (paths relative to home, parents first) every folder it
    had to make, so uninstall can take away exactly those if they are empty by then."""
    missing: List[Path] = []
    d = Path(directory)
    while not d.exists() and d.parent != d:
        missing.append(d)
        d = d.parent
    for p in reversed(missing):
        p.mkdir()
        rel = str(p.relative_to(ctx.home))
        if rel not in created:
            created.append(rel)


def prune_dirs(ctx, created: Any) -> None:
    """Remove the recorded folders that are empty, deepest first (by how many folders deep
    each one is, not by the order they were written down). Never anything else."""
    if not isinstance(created, list):
        return
    rels = [r for r in created if isinstance(r, str)]
    for rel in sorted(rels, key=lambda r: len(Path(r).parts), reverse=True):
        try:
            _under_home(ctx, rel).rmdir()
        except (OSError, SetupError, TypeError):
            continue


# ---------------------------------------------------------------- files wholly Bertie's

def marker_first_line(marker: str) -> Callable[[str], bool]:
    """An ownership test: the file's first line is exactly `marker` (a comment of ours)."""
    def is_ours(text: str) -> bool:
        lines = text.splitlines()
        return bool(lines) and lines[0].strip() == marker
    return is_ours


def skill_is_ours(text: str) -> bool:
    """An ownership test for a SKILL.md: its front matter says `name: bertie`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.strip() == f"name: {registry.NAME}":
            return True
    return False


def _check_skill_path(ctx, rel: str) -> None:
    """A skill copy goes exactly where `rel` says. Refuse when any folder between the home
    folder and the file is a link, or when the folder's real place is anywhere else (another
    agent's skill folder, say): writing or deleting there would change a file that is not
    ours. A link as the file itself is judged by the callers (it is not ours either)."""
    home = Path(ctx.home)
    cur = home
    for part in Path(rel).parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise SetupError(f"{cur} is a link to another place, so the skill file inside it would really be "
                             "somewhere else. Make it a plain folder or move the link away, then run again.")
    parent = _under_home(ctx, rel).parent
    if os.path.realpath(str(parent)) != os.path.join(os.path.realpath(str(home)), str(Path(rel).parent)):
        raise SetupError(f"{parent} does not lead where its name says, so the skill file would really be "
                         "somewhere else. Move the link away, then run again.")


def _write_owned(ctx, rec: Dict[str, Any], rel: str, text: str,
                 is_ours: Callable[[str], bool], mode: int, strict_path: bool = False) -> bool:
    path = _under_home(ctx, rel)
    if strict_path:
        _check_skill_path(ctx, rel)
    if path.exists() or path.is_symlink():
        try:
            current = path.read_text(encoding="utf-8") if path.is_file() and not path.is_symlink() else None
        except (OSError, UnicodeDecodeError):
            current = None
        if current is None or not is_ours(current):
            raise SetupError(f"{path} already exists and was not made by this installer. "
                             "Move it out of the way and run the installer again.")
        if current == text:
            return False
        write_text(path, text, mode)
        return True
    created = rec.setdefault("created_dirs", [])
    before = list(created)
    try:
        make_dirs(ctx, path.parent, created)
        write_text(path, text, mode)
    except OSError:
        prune_dirs(ctx, [d for d in created if d not in before])         # no half-made folders left behind
        rec["created_dirs"] = before
        raise
    return True


def _remove_owned(ctx, rec: Dict[str, Any], rel: str, is_ours: Callable[[str], bool],
                  strict_path: bool = False) -> bool:
    path = _under_home(ctx, rel)
    if strict_path:
        _check_skill_path(ctx, rel)
    removed = False
    if path.is_file() and not path.is_symlink():
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
        if current is not None and is_ours(current):
            path.unlink()
            removed = True
    prune_dirs(ctx, rec.get("created_dirs"))
    return removed


def write_owned_file(ctx, key: str, rel: str, text: str, is_ours: Callable[[str], bool],
                     mode: int = 0o644, strict_path: bool = False) -> bool:
    """Write a file that is wholly Bertie's (`rel` is relative to home) and record it under
    manifest["agents"][key]. If something is already there, it must pass `is_ours` or
    SetupError is raised and nothing is touched. True if the file changed."""
    manifest = load_manifest(ctx)
    files = section(manifest, key).setdefault("files", {})
    rec = files.get(rel) if isinstance(files.get(rel), dict) else {}
    changed = _write_owned(ctx, rec, rel, text, is_ours, mode, strict_path)
    files[rel] = rec
    save_manifest(ctx, manifest)
    return changed


def remove_owned_file(ctx, key: str, rel: str, is_ours: Callable[[str], bool], strict_path: bool = False) -> bool:
    """Remove a file recorded by write_owned_file, but only if it is still ours. A file that
    is not recorded is never touched. Folders Bertie made are removed if empty. True if removed."""
    manifest = load_manifest(ctx)
    files = peek(manifest, key).get("files")
    rec = files.get(rel) if isinstance(files, dict) else None
    if not isinstance(rec, dict):
        return False
    removed = _remove_owned(ctx, rec, rel, is_ours, strict_path)
    del files[rel]
    save_manifest(ctx, manifest)
    return removed


# ---------------------------------------------------------------- SKILL.md for other agents

# Claude Code's own copy is the kit's SKILL.md, byte for byte. Everything else reads this one.
_NEUTRAL = (
    (re.compile(r"a Claude\s+Code approval"), "your coding agent's approval"),
    (re.compile(r"in Claude\s+Code\b"), "in your coding agent"),
    (re.compile(r"Claude must never answer that window"), "The agent must never answer that window"),
    (re.compile(r"the Claude app"), "your agent's app"),
)


# One line the neutral copy carries, right after its front matter, so a skill file of someone
# else's that merely says `name: bertie` is never taken for ours.
NEUTRAL_MARK = "<!-- " + registry.NAME + ": a copy made by the " + registry.NAME + " installer; `" \
               + registry.GOOGLE_COMMAND + " uninstall` removes it -->"


def neutral_skill_text(text: str) -> str:
    """SKILL.md with the Claude-specific words made neutral, and (when it has front matter)
    NEUTRAL_MARK on the line after it. SetupError if a mention is left that none of the
    rewordings covers (the tests keep that from shipping)."""
    for pattern, replacement in _NEUTRAL:
        text = pattern.sub(replacement, text)
    if re.search(r"claude", text, re.I):
        raise SetupError("SKILL.md names Claude somewhere the neutral copy cannot reword; "
                         "add a rewording in agents/__init__.py")
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[:i + 1] + [NEUTRAL_MARK] + lines[i + 1:])
    return text


def neutral_skill_is_ours(text: str) -> bool:
    """An ownership test for the neutral copies: front matter with `name: bertie`, and
    NEUTRAL_MARK on the line right after it."""
    if not skill_is_ours(text):
        return False
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1 < len(lines) and lines[i + 1].strip() == NEUTRAL_MARK
    return False


def kit_skill_text(kit_dir: Path) -> str:
    src = Path(kit_dir) / "skill" / registry.NAME / "SKILL.md"
    if not src.is_file():
        raise SetupError(f"missing {src}")
    return src.read_text(encoding="utf-8")


def install_skill_copy(ctx, key: str, rel: str, kit_dir: Path) -> bool:
    """A neutral SKILL.md at `rel` (relative to home) that belongs to agent `key` alone."""
    return write_owned_file(ctx, key, rel, neutral_skill_text(kit_skill_text(kit_dir)), neutral_skill_is_ours,
                            strict_path=True)


def remove_skill_copy(ctx, key: str, rel: str) -> bool:
    return remove_owned_file(ctx, key, rel, neutral_skill_is_ours, strict_path=True)


def acquire_shared_skill(ctx, key: str, kit_dir: Path) -> bool:
    """The one copy at ~/.agents/skills/bertie/SKILL.md that Cursor, Codex and Kimi Code all
    read. Counted in manifest["shared_skill"]["users"]. True if the file changed."""
    text = neutral_skill_text(kit_skill_text(kit_dir))
    manifest = load_manifest(ctx)
    rec = manifest.get("shared_skill")
    if not isinstance(rec, dict):
        rec = {}
    changed = _write_owned(ctx, rec, SHARED_SKILL_REL, text, neutral_skill_is_ours, 0o644, strict_path=True)
    users = rec["users"] if isinstance(rec.get("users"), list) else []
    if key not in users:
        users.append(key)
    rec["users"] = users
    manifest["shared_skill"] = rec
    save_manifest(ctx, manifest)
    return changed


def release_shared_skill(ctx, key: str) -> bool:
    """Take `key` off the shared skill; remove the file (and the folders Bertie made) when
    the last agent lets go. True if the file was removed."""
    manifest = load_manifest(ctx)
    rec = manifest.get("shared_skill")
    if not isinstance(rec, dict):
        return False
    users = [u for u in rec["users"] if u != key] if isinstance(rec.get("users"), list) else []
    if users:
        rec["users"] = users
        save_manifest(ctx, manifest)
        return False
    removed = _remove_owned(ctx, rec, SHARED_SKILL_REL, neutral_skill_is_ours, strict_path=True)
    del manifest["shared_skill"]
    save_manifest(ctx, manifest)
    return removed


def remove_shared_skill(ctx) -> bool:
    """Remove the shared skill whatever the count says (the whole tool is being uninstalled)."""
    manifest = load_manifest(ctx)
    rec = manifest.get("shared_skill")
    if not isinstance(rec, dict):
        return False
    removed = _remove_owned(ctx, rec, SHARED_SKILL_REL, neutral_skill_is_ours, strict_path=True)
    del manifest["shared_skill"]
    save_manifest(ctx, manifest)
    return removed


# ---------------------------------------------------------------- a user's JSON file

def _dotted(keys: Sequence[str]) -> str:
    return ".".join(keys)


def _note_created(rec: Dict[str, Any], keys: Sequence[str]) -> None:
    created = rec.setdefault("created", [])
    if list(keys) not in created:
        created.append(list(keys))


def _parent(data: Dict[str, Any], keys: Sequence[str], rec: Dict[str, Any], name: str) -> Dict[str, Any]:
    """The object that holds keys[-1], making (and recording) any object above it that is missing."""
    cur = data
    for i, k in enumerate(keys[:-1]):
        if k not in cur:
            cur[k] = {}
            _note_created(rec, keys[:i + 1])
        elif not isinstance(cur[k], dict):
            raise SetupError(f"{name}: '{_dotted(keys[:i + 1])}' is not an object; left untouched.")
        cur = cur[k]
    return cur


def _commands(item: Any) -> List[str]:
    """The hook commands inside a hook entry: its own "command", and those of its "hooks" list."""
    if not isinstance(item, dict):
        return []
    out = [item["command"]] if isinstance(item.get("command"), str) else []
    hooks = item.get("hooks")
    if isinstance(hooks, list):
        out += [h["command"] for h in hooks if isinstance(h, dict) and isinstance(h.get("command"), str)]
    return out


class Append:
    """JSON edit: the list at `path` (keys from the top of the file) holds each of `items`.
    A missing list is made. An item already there is left as it is, and is not Bertie's to
    remove. An entry Bertie added earlier with the same hook command is replaced in place."""

    def __init__(self, path: Sequence[str], items: Sequence[Any]):
        self.path, self.items = list(path), list(items)

    def apply(self, data: Dict[str, Any], rec: Dict[str, Any], name: str) -> None:
        parent = _parent(data, self.path, rec, name)
        k = self.path[-1]
        if k not in parent:
            parent[k] = []
            _note_created(rec, self.path)
        elif not isinstance(parent[k], list):
            raise SetupError(f"{name}: '{_dotted(self.path)}' is not a list; left untouched.")
        lst = parent[k]
        entry = next((e for e in rec.setdefault("added", []) if e["path"] == self.path), None)
        if entry is None:
            entry = {"path": list(self.path), "items": []}
            rec["added"].append(entry)
        for item in self.items:
            if item in lst:
                continue
            cmds = _commands(item)
            old = next((r for r in entry["items"] if cmds and _commands(r) == cmds), None)
            if old is None:
                lst.append(copy.deepcopy(item))
                entry["items"].append(copy.deepcopy(item))
            else:                                                # ours from an earlier install: update it
                if old in lst:
                    lst[lst.index(old)] = copy.deepcopy(item)
                else:
                    lst.append(copy.deepcopy(item))
                entry["items"][entry["items"].index(old)] = copy.deepcopy(item)
        if not entry["items"]:
            rec["added"].remove(entry)


class SetKey:
    """JSON edit: the key at `path` is Bertie's and holds `value`. SetupError if the key
    already exists and Bertie did not put it there."""

    def __init__(self, path: Sequence[str], value: Any):
        self.path, self.value = list(path), value

    def apply(self, data: Dict[str, Any], rec: Dict[str, Any], name: str) -> None:
        parent = _parent(data, self.path, rec, name)
        k = self.path[-1]
        sets = rec.setdefault("set", [])
        mine = next((s for s in sets if s["path"] == self.path), None)
        if k in parent and mine is None:
            raise SetupError(f"{name}: '{_dotted(self.path)}' already exists and was not added by this "
                             "installer; left untouched. Move it out of the way, then run again.")
        parent[k] = copy.deepcopy(self.value)
        if mine is None:
            sets.append({"path": list(self.path), "value": copy.deepcopy(self.value)})
        else:
            mine["value"] = copy.deepcopy(self.value)


class Require:
    """JSON edit: the key at `path` must hold `value`. Missing, it is added (and is Bertie's
    to remove). Already equal, it is left alone. Anything else is a SetupError."""

    def __init__(self, path: Sequence[str], value: Any):
        self.path, self.value = list(path), value

    def apply(self, data: Dict[str, Any], rec: Dict[str, Any], name: str) -> None:
        parent = _parent(data, self.path, rec, name)
        k = self.path[-1]
        if k in parent:
            have = parent[k]
            if have != self.value or type(have) is not type(self.value):
                raise SetupError(f"{name}: '{_dotted(self.path)}' is {json.dumps(have)} but {registry.DISPLAY_NAME} "
                                 f"needs {json.dumps(self.value)}; left untouched.")
            return
        parent[k] = copy.deepcopy(self.value)
        sets = rec.setdefault("set", [])
        if not any(s["path"] == self.path for s in sets):
            sets.append({"path": list(self.path), "value": copy.deepcopy(self.value)})


def _plain_file(path: Path) -> None:
    """SetupError for a link: Bertie edits a file of its own location, never through a link
    (which may lead into another program's files, or nowhere)."""
    if path.is_symlink():
        raise SetupError(f"{path} is a link to another file, not a file of its own; left untouched. "
                         f"{registry.DISPLAY_NAME} does not edit through links.")


def link_problem(ctx, key: str, path: Path) -> Optional[str]:
    """For a file the install recorded that is now a link: why Bertie cannot clean it, or None when
    it needs no cleaning. A link that leads nowhere, or whose target no longer runs this install's
    hook command for agent `key`, has nothing of Bertie's behind it, so its record can simply go.
    Only a link whose target still holds Bertie's hook entry is a problem (the command as Bertie
    writes it, or another spelling of this install's hook.sh, as `~/.config/bertie/hook.sh` is):
    Bertie does not edit through a link, so the message names the real target and says what to do.
    (Anything that is not a link: None.)"""
    if not path.is_symlink():
        return None
    try:
        target = Path(os.path.realpath(str(path)))
        text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except (OSError, ValueError, RuntimeError) as e:
        return (f"{path} is a link, and the file it leads to could not be read ({why(e)}), so {registry.DISPLAY_NAME} "
                f"cannot tell whether its hook entry is there. {registry.DISPLAY_NAME} does not edit through links; "
                "look in the file it leads to, remove the entry there, or replace the link with a plain file.")
    command = hook_command(ctx, key)
    if command in text or json.dumps(command)[1:-1] in text or _names_our_hook(ctx, text):
        return (f"{path} is a link to {target}, and {target} still holds {registry.DISPLAY_NAME}'s hook entry. "
                f"{registry.DISPLAY_NAME} does not edit through links: remove the entry there, or replace the "
                "link with a plain file, then run this again.")
    return None


def why(e: BaseException) -> str:
    """A short plain reason for an OS or text error, for a SetupError message."""
    if isinstance(e, UnicodeDecodeError):
        return "it is not plain UTF-8 text"
    return (getattr(e, "strerror", None) or type(e).__name__).lower()


def _read_json(path: Path):
    """kit_setup._read_settings with the failures an agent's file can have turned into
    SetupError: a link, a file that cannot be read, text that is not UTF-8."""
    _plain_file(path)
    try:
        return kit_setup._read_settings(path)
    except (OSError, UnicodeDecodeError) as e:
        raise SetupError(f"{path} could not be read ({why(e)}); left untouched.")


def json_merge(ctx, key: str, rel: str, edits: Sequence[Any]) -> bool:
    """Apply `edits` (Append, SetKey, Require) to the user's JSON object file at `rel`
    (relative to home), keeping every other key and the file's mode. The file is written
    back with the indentation it already had, so spacing elsewhere may change; uninstall
    puts the original bytes back when nothing else changed. Refuses (SetupError, file
    untouched, no backup) a link, a file that cannot be read, is not valid JSON or is not
    an object. Backs the file up before changing it. Records in manifest["agents"][key]["json"]
    exactly what was added. Idempotent: a second run changes nothing and makes no backup.
    True if the file changed."""
    path = _under_home(ctx, rel)
    name = path.name
    data, indent, nl, existed = _read_json(path)
    raw = b""
    if existed:
        try:
            raw = path.read_bytes()
        except OSError as e:
            raise SetupError(f"{path} could not be read ({why(e)}); left untouched.")
    manifest = load_manifest(ctx)
    recs = section(manifest, key).setdefault("json", {})
    rec = recs.get(rel) if existed and isinstance(recs.get(rel), dict) else {}
    if not existed:
        rec = {"created_file": True}
    elif raw.strip() == b"":
        rec["was_empty"] = True
    before = json.dumps(data)
    for edit in edits:
        edit.apply(data, rec, name)
    changed = json.dumps(data) != before or not existed
    if changed:
        try:
            if existed:
                b = kit_setup._backup_file(path)
                rec.setdefault("backups", []).append(b.name)
                if "orig_sha256" not in rec:                     # the very first backup holds the original bytes
                    rec["orig_backup"], rec["orig_sha256"] = b.name, hashlib.sha256(raw).hexdigest()
            else:
                made = rec.setdefault("created_dirs", [])
                had = list(made)
                try:
                    make_dirs(ctx, path.parent, made)
                except OSError:
                    prune_dirs(ctx, [d for d in made if d not in had])
                    raise
            kit_setup._write_settings(path, data, indent, nl, existed)
        except OSError as e:
            raise SetupError(f"could not write {path} ({why(e)}); left as it was.")
    recs[rel] = rec
    save_manifest(ctx, manifest)
    return changed


def _walk(data: Any, keys: Sequence[str]) -> Any:
    """The value at `keys`, or None if any step is missing or not an object."""
    cur = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _strip(lst: List[Any], item: Any) -> None:
    """Take Bertie's entry out of a list: an equal entry, an entry with the same hook
    command, or just our hook out of a group that holds other people's too."""
    cmds = _commands(item)
    kept = []
    for e in lst:
        if e == item:
            continue
        if cmds and isinstance(e, dict):
            if isinstance(e.get("command"), str) and e["command"] in cmds:
                continue
            hooks = e.get("hooks")
            if isinstance(hooks, list) and any(isinstance(h, dict) and h.get("command") in cmds for h in hooks):
                e = dict(e)
                e["hooks"] = [h for h in hooks if not (isinstance(h, dict) and h.get("command") in cmds)]
                if not e["hooks"]:
                    continue
        kept.append(e)
    lst[:] = kept


def _original_text(path: Path, rec: Dict[str, Any]) -> Optional[str]:
    """The file's text as it was before the first merge, from the first backup beside it, but only
    if that backup still has the recorded checksum. None if it cannot be trusted."""
    name, digest = rec.get("orig_backup"), rec.get("orig_sha256")
    if not isinstance(name, str) or not isinstance(digest, str) or name != Path(name).name:
        return None
    try:
        raw = (path.parent / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _same_data(original: str, data: Dict[str, Any]) -> bool:
    try:
        old = json.loads(original) if original.strip() else {}
    except ValueError:
        return False
    return isinstance(old, dict) and json.dumps(old, sort_keys=True) == json.dumps(data, sort_keys=True)


def json_unmerge(ctx, key: str, rel: str) -> bool:
    """Take back out of the file at `rel` exactly what json_merge recorded, and nothing else
    (a file that was not recorded is not touched). Containers Bertie made go if they are
    empty; a file Bertie made goes if nothing else is in it. Backs the file up first. When
    what is left is the data the file held before the first merge, its original bytes are
    written back; otherwise it is written with the indentation it has. SetupError (file
    and record untouched) for a link or a file that cannot be read. True if the file changed."""
    manifest = load_manifest(ctx)
    recs = peek(manifest, key).get("json")
    rec = recs.get(rel) if isinstance(recs, dict) else None
    if not isinstance(rec, dict):
        return False
    path = _under_home(ctx, rel)
    problem = link_problem(ctx, key, path)
    if problem:                                              # a link to a file that still holds our entry: refuse
        raise SetupError(problem)
    if path.is_symlink():                                    # a link with nothing of ours behind it: leave it, forget the record
        del recs[rel]
        save_manifest(ctx, manifest)
        return False
    changed = False
    if path.exists():
        data, indent, nl, _ = _read_json(path)
        before = json.dumps(data)
        for entry in rec.get("added", []):
            lst = _walk(data, entry.get("path", []))
            if isinstance(lst, list):
                for item in entry.get("items", []):
                    _strip(lst, item)
        for s in rec.get("set", []):
            keys = s.get("path", [])
            holder = _walk(data, keys[:-1]) if keys else None
            if isinstance(holder, dict):
                holder.pop(keys[-1], None)
        for keys in sorted(rec.get("created", []), key=len, reverse=True):
            holder = _walk(data, keys[:-1]) if keys else None
            if isinstance(holder, dict) and holder.get(keys[-1]) in ([], {}):
                del holder[keys[-1]]
        changed = json.dumps(data) != before
        if changed:
            try:
                if not data and rec.get("created_file"):
                    path.unlink()                    # only ever held what Bertie put there: no backup needed
                    prune_dirs(ctx, rec.get("created_dirs"))
                else:
                    kit_setup._backup_file(path)
                    mode = path.stat().st_mode & 0o777
                    original = _original_text(path, rec)
                    if original is not None and _same_data(original, data):
                        write_text(path, original, mode)
                    elif not data and rec.get("was_empty"):
                        write_text(path, "", mode)
                    else:
                        kit_setup._write_settings(path, data, indent, nl, True)
            except OSError as e:
                raise SetupError(f"could not write {path} ({why(e)}); left as it was.")
    del recs[rel]
    save_manifest(ctx, manifest)
    return changed


# ---------------------------------------------------------------- checks before anything is written

def preflight_skill(ctx, rel: str, kit_dir: Path) -> None:
    """Everything that can refuse a neutral skill copy at `rel`, found before any file is written:
    the text cannot be made neutral, a folder on the way is a link, or something that is not ours is there."""
    neutral_skill_text(kit_skill_text(kit_dir))
    _check_skill_path(ctx, rel)
    path = _under_home(ctx, rel)
    if path.exists() or path.is_symlink():
        try:
            ours = path.is_file() and not path.is_symlink() and neutral_skill_is_ours(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            ours = False
        if not ours:
            raise SetupError(f"{path} already exists and was not made by this installer. "
                             "Move it out of the way and run the installer again.")


def preflight_json(ctx, rel: str, edits: Sequence[Any], key: Optional[str] = None) -> None:
    """json_merge's refusals, tried on a copy: a link, an unreadable file, invalid JSON, a value
    of the wrong kind where an edit needs a list or an object, a key that is not ours. Writes
    nothing. Pass the agent's `key` and what the manifest recorded for this file is used too."""
    path = _under_home(ctx, rel)
    data, _, _, existed = _read_json(path)
    rec: Dict[str, Any] = {}
    if key is not None and existed:
        recs = peek(load_manifest(ctx), key).get("json")
        if isinstance(recs, dict) and isinstance(recs.get(rel), dict):
            rec = copy.deepcopy(recs[rel])
    trial = copy.deepcopy(data)
    for edit in edits:
        edit.apply(trial, rec, path.name)


# ---------------------------------------------------------------- uninstalling without leaving a mess

class Undo:
    """An adapter's uninstall, one step at a time. A step that cannot be done is written down and
    the next one still runs, so one broken file does not leave the rest behind. `finish` takes the
    manifest section away only when nothing is left, so what remains stays on record."""

    def __init__(self, ctx, key: str, label: str):
        self.ctx, self.key, self.label = ctx, key, label
        self.done: List[str] = []
        self.left: List[str] = []
        self.first: List[str] = []              # lines an adapter wants ahead of the rest (its own wording)
        self.keep = False                       # keep the manifest section although `left` is empty

    def run(self, what: str, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except (SetupError, OSError, ValueError) as e:      # ValueError includes UnicodeDecodeError
            self.left.append(f"{what}: {e}")
            return None

    def finish(self, nothing: Optional[str] = None) -> List[str]:
        lines = self.first + self.done
        if self.left:
            lines.append(f"{self.label}: could not remove everything ({'; '.join(self.left)}); left as it is")
        if not self.left and not self.keep:
            drop_section(self.ctx, self.key)
        return lines or [nothing or f"{self.label}: nothing to remove"]


def _hook_path_spellings(ctx) -> List[str]:
    """Every way a line in an agent's file can spell the full path of this install's hook.sh: the
    path as it is, quoted for a shell, with ~ or $HOME for the home folder, and each of those as it
    reads inside a JSON or TOML string. The install folder and the home folder count as they were
    given and with links followed, and so does the standard place (~/.config/bertie)."""
    homes = list(dict.fromkeys((Path(ctx.home), Path(os.path.realpath(str(ctx.home))))))
    installs = list(dict.fromkeys((Path(ctx.install_dir), Path(os.path.realpath(str(ctx.install_dir))))))
    installs += [h / registry.INSTALL_REL for h in homes]
    paths = list(dict.fromkeys(i / "hook.sh" for i in installs))
    forms = [str(p) for p in paths] + [shlex.quote(str(p)) for p in paths]
    for path in paths:
        for home in homes:
            try:
                rel = str(path.relative_to(home))
            except ValueError:
                continue
            forms += ["~/" + rel, "$HOME/" + rel, "${HOME}/" + rel]
    forms += [json.dumps(f)[1:-1] for f in list(forms)]
    return list(dict.fromkeys(forms))


def _names_our_hook(ctx, text: str) -> bool:
    """Whether the text of an agent's file tells it to run THIS install's hook.sh (its whole path,
    not just a folder of the same name), for any agent."""
    return "--agent" in text and any(form in text for form in _hook_path_spellings(ctx))


def hook_leftovers(ctx) -> List[Tuple[str, str, str]]:
    """(agent label, where, what to delete by hand) for everything that still tells an agent to run this install's
    hook.sh. An adapter lists the files it edits as hook_files(ctx); an agent without that is
    judged by whether its manifest entry is still there. Claude Code is not looked at (its
    uninstall has always been as it is). Used to keep the install folder while any agent
    would still look for the hook in it."""
    found: List[Tuple[str, str, str]] = []
    manifest = load_manifest(ctx)
    generic = f"delete the entries that run {tilde(ctx, Path(ctx.install_dir) / 'hook.sh')}"
    for key in ADAPTERS + tuple(k for k in recorded_keys(manifest) if k not in ADAPTERS):
        if key == "claude":
            continue
        try:
            adapter = get(key)
        except SetupError:
            adapter = None
        files = getattr(adapter, "hook_files", None)
        label = getattr(adapter, "LABEL", key)
        if files is None:
            if key in recorded_keys(manifest):
                found.append((label, "its entry in the install folder's own record", "nothing to edit; the folder is kept"))
            continue
        for path in files(ctx):
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _names_our_hook(ctx, text):
                found.append((label, tilde(ctx, path), getattr(adapter, "HAND_HINT", "") or generic))
    return found


def uninstall_problems(ctx) -> List[str]:
    """Why the recorded non-Claude agents' files cannot be cleaned, found before anything is
    removed (a link, a file that cannot be read, JSON that no longer parses). Empty when all is well."""
    problems: List[str] = []
    manifest = load_manifest(ctx)
    for key in recorded_keys(manifest):
        if key == "claude":
            continue
        sec = peek(manifest, key)
        recs = sec.get("json")
        for rel in (recs if isinstance(recs, dict) else {}):
            try:
                path = _under_home(ctx, rel)
                problem = link_problem(ctx, key, path)
                if problem:
                    problems.append(problem)
                elif path.exists() and not path.is_symlink():
                    _read_json(path)
            except (SetupError, TypeError) as e:
                problems.append(str(e))
        extra = sec.get("edited")
        for rel in (extra if isinstance(extra, list) else []):
            try:
                path = _edited_path(ctx, rel)
                if path.exists():
                    path.read_text(encoding="utf-8")
            except (SetupError, TypeError):
                continue
            except (OSError, UnicodeDecodeError) as e:
                problems.append(f"{path} could not be read ({why(e)}).")
        try:
            own = getattr(get(key), "uninstall_problems", None)
        except SetupError:
            own = None
        if own is not None:
            problems += own(ctx)
    return problems
