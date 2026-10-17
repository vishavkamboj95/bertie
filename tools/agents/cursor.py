"""Cursor: the shared skill in ~/.agents/skills and a safety hook in ~/.cursor/hooks.json.

Cursor has no file of ask rules, so the hook does that job. Two entries, both running
hook.sh --agent cursor:

  * beforeShellExecution, failClosed: sees every terminal command and can ASK the person.
    failClosed means that if the hook itself cannot run, Cursor does not run the command.
  * preToolUse for Write and Delete: sees text being written to a file, and the path of a
    file being deleted. It cannot ask, so it DENIES text that names the ways round the tools
    (or a file Bertie's hook lives in). (It has no failClosed; Cursor's own docs leave that to
    the shell hook.)

The two send different JSON, told apart by hook_event_name when it is there, else by shape:
  shell:    {"hook_event_name": "beforeShellExecution", "command": "...", "cwd": "..."}
  Write:    {"hook_event_name": "preToolUse", "tool_name": "Write", "tool_input": {...}}
Every answer is JSON, because Cursor treats output it cannot parse as a block. No opinion
is {"permission": "allow"}. The two functions that answer do not see the input, so
hook_input leaves a note of which shape it read (_SHAPE) for them. A hook process makes one
call, and every answer takes the note back out, so one call never reads another's.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

import agents
import kit_setup
import registry

KEY = "cursor"
LABEL = "Cursor"
HOOK_STYLE = "ask"

HOOKS_REL = ".cursor/hooks.json"
SHELL_EVENT = "beforeShellExecution"
WRITE_EVENT = "preToolUse"
WRITE_MATCHER = "Write|Delete"
TIMEOUT = 10

# A hook call that arrives with no input at all is a failure here: Cursor always sends something.
EMPTY_INPUT_IS_FAILURE = True

_SHAPE: Optional[str] = None            # "shell" or "tool": what the last hook_input read


def detect(home: Path) -> bool:
    return (Path(home) / ".cursor").is_dir()


# ---------------------------------------------------------------- install

def _edits(ctx) -> list:
    command = agents.hook_command(ctx, KEY)
    shell = {"command": command, "timeout": TIMEOUT, "failClosed": True}
    write = {"command": command, "matcher": WRITE_MATCHER, "timeout": TIMEOUT}
    return [
        agents.Require(["version"], 1),
        agents.Append(["hooks", SHELL_EVENT], [shell]),
        agents.Append(["hooks", WRITE_EVENT], [write]),
    ]


def hook_files(ctx) -> List[Path]:
    return [Path(ctx.home) / HOOKS_REL]


def preflight(ctx, kit_dir: Path) -> None:
    """Everything that can refuse, before anything is written, so a refusal leaves no half install."""
    agents.preflight_skill(ctx, agents.SHARED_SKILL_REL, kit_dir)
    agents.preflight_json(ctx, HOOKS_REL, _edits(ctx), KEY)        # a link, invalid JSON, a version other than 1


def install(ctx, kit_dir: Path) -> List[str]:
    preflight(ctx, kit_dir)
    skill_changed = agents.acquire_shared_skill(ctx, KEY, kit_dir)
    hooks_changed = agents.json_merge(ctx, KEY, HOOKS_REL, _edits(ctx))
    return [
        f"{LABEL}: " + ("installed" if skill_changed else "already had") + f" the skill '{registry.NAME}' in ~/{agents.SHARED_SKILL_REL}",
        f"{LABEL}: safety hook " + ("added to" if hooks_changed else "already in") + f" ~/{HOOKS_REL}",
        f"{LABEL}: skill and safety hook. The hook asks you before a send, delete or share and before any way "
        "round the commands, and refuses to write text that names such a way into a file. Restart Cursor so it "
        "loads the hook. The Mac window and Touch ID still guard every send.",
    ]


def uninstall(ctx) -> List[str]:
    u = agents.Undo(ctx, KEY, LABEL)
    if u.run(f"the safety hook in ~/{HOOKS_REL}", agents.json_unmerge, ctx, KEY, HOOKS_REL):
        u.done.append(f"{LABEL}: removed the safety hook from ~/{HOOKS_REL}")
    if u.run(f"the skill in ~/{agents.SHARED_SKILL_REL}", agents.release_shared_skill, ctx, KEY):
        u.done.append(f"{LABEL}: removed the skill '{registry.NAME}' (no other agent was using it)")
    return u.finish()


# ---------------------------------------------------------------- the hook

def hook_input(data: dict) -> Tuple[str, str]:
    """Shell: {"command": "..."} -> ("command", the command). Write or Delete: {"tool_name": ...,
    "tool_input": {...}} -> ("file", every string in tool_input). hook_event_name decides which
    when it is there (beforeShellExecution must carry a command, preToolUse a tool name); a
    call that carries both a tool and a command has both checked, as a command. Anything else:
    nothing to check."""
    global _SHAPE
    import hook
    _SHAPE = None
    event = data.get("hook_event_name")
    if event is not None and not isinstance(event, str):
        raise ValueError("event name is not text")
    if event == SHELL_EVENT and not isinstance(data.get("command"), str):
        raise ValueError("no command text")
    if event == WRITE_EVENT and not isinstance(data.get("tool_name"), str):
        raise ValueError("no tool name")
    has_tool, has_command = "tool_name" in data, "command" in data
    if has_tool and not isinstance(data["tool_name"], str):
        raise ValueError("tool name is not text")
    if has_command and not isinstance(data["command"], str):
        raise ValueError("command is not text")
    if event == SHELL_EVENT:
        _SHAPE = "shell"
    elif event == WRITE_EVENT:
        _SHAPE = "tool"
    else:
        _SHAPE = "tool" if has_tool else "shell" if has_command else None
    tool_text = "\n".join(hook.strings_in(data.get("tool_input"))) if has_tool else None
    if has_command:
        return "command", data["command"] if tool_text is None else tool_text + "\n" + data["command"]
    if has_tool:
        return "file", tool_text or ""
    return "none", ""


def hook_scan_text(data: dict, command: str) -> Optional[str]:
    """What to check for a shell command: the folder it runs in (only where it can matter, see
    hook.cwd_prefix), then the command, so a command that only does harm in the right folder is
    read as it would run. None when the command alone is what to check."""
    cwd = data.get("cwd")
    if cwd is None or "tool_name" in data:               # a mixed call is already one text
        return None
    import hook
    prefix = hook.cwd_prefix(cwd, command)               # ValueError for a folder that is not text
    return prefix + command if prefix else None


def hook_cwd_known(data: dict) -> bool:
    """Whether a full path was given for the folder the command runs in (see hook.folder_given). Not
    when the call carries a tool as well: hook_scan_text leaves the folder out of that one."""
    import hook
    return "tool_name" not in data and hook.folder_given(data.get("cwd"))


def failure_blocks() -> bool:
    """Whether hook_failure will deny (True) or ask (False) for the call just read."""
    return _SHAPE != "shell"


def _take_shape() -> Optional[str]:
    global _SHAPE
    shape, _SHAPE = _SHAPE, None
    return shape


def _answer(permission: str, reason: str) -> str:
    return json.dumps({"permission": permission, "user_message": reason, "agent_message": reason}) + "\n"


def hook_answer(reason: Optional[str]) -> Tuple[str, int]:
    shape = _take_shape()
    if not reason:
        return json.dumps({"permission": "allow"}) + "\n", 0
    return _answer("deny" if shape == "tool" else "ask", reason), 0


def hook_failure(reason: str) -> Tuple[str, int]:
    """Ask for a shell command; deny for a Write, and for input that could not be read at all
    (the write hook cannot ask, and nothing says which kind of call this was)."""
    return _answer("ask" if _take_shape() == "shell" else "deny", reason), 0
