"""Claude Code: the skill in ~/.claude/skills and, in ~/.claude/settings.json, the ask
rules and the PreToolUse hook.

A thin wrapper. The work is done by the functions kit_setup has always had, so the files,
the messages, their order and the manifest keys are the ones Bertie always made. The
hook command is kit_setup.HOOK_COMMAND (with $HOME), not the --agent form the others use.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import agents
import kit_setup
import registry

KEY = "claude"
LABEL = "Claude Code"
HOOK_STYLE = "ask"


def detect(home: Path) -> bool:
    return (Path(home) / ".claude").is_dir()


def preflight(ctx, kit_dir: Path) -> None:
    """The refusals install_skill and merge_settings would make, found without writing anything."""
    src = Path(kit_dir) / "skill" / registry.NAME / "SKILL.md"
    if not src.is_file():
        raise kit_setup.SetupError(f"missing {src}")
    try:
        data = kit_setup._read_settings(ctx.settings_path)[0]
    except (OSError, UnicodeDecodeError) as e:
        raise kit_setup.SetupError(f"{ctx.settings_path} could not be read ({agents.why(e)}); left untouched.")
    for keys, kind, word in ((("permissions",), dict, "an object"), (("permissions", "ask"), list, "a list"),
                             (("hooks",), dict, "an object"), (("hooks", "PreToolUse"), list, "a list")):
        cur: object = data
        for k in keys:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if cur is not None and not isinstance(cur, kind):
            raise kit_setup.SetupError(f"settings.json: '{'.'.join(keys)}' is not {word}; left untouched.")


def install(ctx, kit_dir: Path) -> List[str]:
    kit_setup.install_skill(ctx, kit_dir)
    msgs = [f"installed the skill '{registry.NAME}'"]
    changed = kit_setup.merge_settings(ctx)
    msgs.append("approval rules and safety hook " + ("added to" if changed else "already in")
                + " ~/.claude/settings.json")
    return msgs


def uninstall_settings(ctx) -> List[str]:
    if kit_setup.unmerge_settings(ctx):
        return ["removed the approval rules and safety hook from ~/.claude/settings.json"]
    return []


def uninstall_skill(ctx) -> List[str]:
    if kit_setup.remove_skill(ctx):
        return [f"removed the skill '{registry.NAME}'"]
    return []


def uninstall(ctx) -> List[str]:
    return uninstall_settings(ctx) + uninstall_skill(ctx)


# ---------------------------------------------------------------- the hook

def hook_input(data: dict) -> Tuple[str, str]:
    """{"tool_name": "Bash", "tool_input": {"command": "..."}} as hook.py has always read it."""
    import hook
    tool, tool_input = data.get("tool_name"), data.get("tool_input")
    if tool in hook.COMMAND_TOOLS:
        if not isinstance(tool_input, dict):
            raise ValueError("tool input is not an object")
        if not hook.strings_in(tool_input):
            raise ValueError("no command text")
        return "command", "\n".join(hook.strings_in(tool_input))
    if tool in hook.FILE_TOOLS:
        return "file", "\n".join(hook.strings_in(tool_input))
    reason = hook.decide_tool(tool, tool_input)         # only the screen-control tools have an opinion here
    return ("ask", reason) if reason else ("none", "")


def hook_answer(reason: Optional[str]) -> Tuple[str, int]:
    if not reason:
        return "", 0
    return agents.pretooluse_answer("ask", reason), 0


def hook_failure(reason: str) -> Tuple[str, int]:
    return agents.pretooluse_answer("ask", reason), 0
