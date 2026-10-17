"""Antigravity (the App, the IDE and the CLI): the skill, the CLI's ask rules and the PreToolUse hook.

Where things go, all under ~/.gemini:
  config/skills/bertie/SKILL.md             the skill for the App and the IDE
  antigravity-cli/skills/bertie/SKILL.md    the skill for the CLI
  antigravity-cli/settings.json             "permissions": {"ask": ["command(bertie-mail send)", ...]},
                                            only when the CLI's folder was already there (the App has
                                            no settings file, so it has nothing to put rules in)
  config/hooks.json                         the top-level key "bertie", which is wholly ours

The hook can ask the person, so it answers {"decision": "force_ask", ...} for anything it
flags, and {"decision": "allow"} (which hands the call on to the next hook or the default
policy) for everything else.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional, Tuple

import agents
import kit_setup
import registry

KEY = "antigravity"
LABEL = "Antigravity"
HOOK_STYLE = "ask"

APP_SKILL_REL = ".gemini/config/skills/" + registry.NAME + "/SKILL.md"
CLI_SKILL_REL = ".gemini/antigravity-cli/skills/" + registry.NAME + "/SKILL.md"
CLI_DIR_REL = ".gemini/antigravity-cli"
SETTINGS_REL = CLI_DIR_REL + "/settings.json"
HOOKS_REL = ".gemini/config/hooks.json"

COMMAND_TOOL = "run_command"
TASK_TOOL = "manage_task"                  # its send_input action types text into a running terminal
FILE_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content")
MATCHER = "|".join((COMMAND_TOOL,) + FILE_TOOLS + (TASK_TOOL,))
HOOK_TIMEOUT = 10
HAND_HINT = 'delete the top-level "' + registry.NAME + '" entry'


def detect(home: Path) -> bool:
    """Antigravity's own folders, not the bare ~/.gemini that Google's other tools share."""
    gemini = Path(home) / ".gemini"
    return any((gemini / sub).is_dir() for sub in ("config", "antigravity-cli", "antigravity"))


def hook_files(ctx) -> List[Path]:
    return [Path(ctx.home) / HOOKS_REL]


def ask_rules() -> List[str]:
    """The CLI's permissions.ask entries: word by word prefixes, one per gated action."""
    return [f"command({tool} {act})" for tool, acts in registry.gated_actions().items() for act in acts]


def _hooks_value(ctx) -> dict:
    return {registry.NAME: {"PreToolUse": [{
        "matcher": MATCHER,
        "hooks": [{"type": "command", "command": agents.hook_command(ctx, KEY), "timeout": HOOK_TIMEOUT}],
    }]}}


def _hook_edits(ctx) -> list:
    return [agents.SetKey([registry.NAME], _hooks_value(ctx)[registry.NAME])]


def _rule_edits() -> list:
    return [agents.Append(["permissions", "ask"], ask_rules())]


def _cli_wanted(ctx, record: bool = False) -> bool:
    """Whether the CLI's ask rules go in: its settings file is there (the CLI made it, so it is in
    use), or its folder was there before this installer ever ran. Whether the folder was there at
    the very first install is written down (`cli_before`) so a later run does not mistake the
    folder our own skill copy made for the CLI's, and a CLI installed later is noticed by its
    settings file. If that note is gone, the record of the skill copy still says whether it made
    the folder."""
    home = Path(ctx.home)
    manifest = kit_setup.load_manifest(ctx)
    sec = agents.peek(manifest, KEY)
    before = sec.get("cli_before")
    if not isinstance(before, bool):
        files = sec.get("files")
        rec = files.get(CLI_SKILL_REL) if isinstance(files, dict) else None
        made = rec.get("created_dirs") if isinstance(rec, dict) else None
        # the answer was lost, but if the record says our own skill copy made the folder it was not the CLI's
        before = False if isinstance(made, list) and CLI_DIR_REL in made else (home / CLI_DIR_REL).is_dir()
        if record:
            agents.section(manifest, KEY)["cli_before"] = before
            kit_setup.save_manifest(ctx, manifest)
    if (home / SETTINGS_REL).exists() or (home / SETTINGS_REL).is_symlink():
        return True
    recorded = sec.get("json")
    if isinstance(recorded, dict) and SETTINGS_REL in recorded:
        return True                                  # we have been editing its settings already
    return before


def preflight(ctx, kit_dir: Path) -> None:
    """Refuse, before anything is written, whatever install would refuse half way through."""
    for rel in (APP_SKILL_REL, CLI_SKILL_REL):
        agents.preflight_skill(ctx, rel, kit_dir)
    agents.preflight_json(ctx, HOOKS_REL, _hook_edits(ctx), KEY)
    if _cli_wanted(ctx):
        agents.preflight_json(ctx, SETTINGS_REL, _rule_edits(), KEY)


def install(ctx, kit_dir: Path) -> List[str]:
    preflight(ctx, kit_dir)
    cli = _cli_wanted(ctx, record=True)
    msgs: List[str] = []
    agents.install_skill_copy(ctx, KEY, APP_SKILL_REL, kit_dir)
    agents.install_skill_copy(ctx, KEY, CLI_SKILL_REL, kit_dir)
    msgs.append(f"{LABEL}: installed the skill '{registry.NAME}' (App and IDE: ~/{APP_SKILL_REL}, "
                f"CLI: ~/{CLI_SKILL_REL})")
    if cli:
        changed = agents.json_merge(ctx, KEY, SETTINGS_REL, _rule_edits())
        msgs.append(f"{LABEL}: approval rules " + ("added to" if changed else "already in") + f" ~/{SETTINGS_REL}")
    else:
        msgs.append(f"{LABEL}: no CLI settings file (~/{SETTINGS_REL}) was found, so no approval rules were added "
                    "(the App has no settings file for them; the safety hook does that job). "
                    "If you use the Antigravity CLI later, run the installer again to add them")
    changed = agents.json_merge(ctx, KEY, HOOKS_REL, _hook_edits(ctx))
    msgs.append(f"{LABEL}: safety hook " + ("added to" if changed else "already in") + f" ~/{HOOKS_REL}")
    msgs.append(f"{LABEL}: protected by the safety hook, which asks you before a send or a way round it"
                + (", and by the approval rules in the CLI" if cli else "")
                + f". The Mac window and Touch ID still guard every send. Start a new {LABEL} session to pick it up.")
    return msgs


def uninstall(ctx) -> List[str]:
    u = agents.Undo(ctx, KEY, LABEL)
    if u.run(f"the safety hook in ~/{HOOKS_REL}", agents.json_unmerge, ctx, KEY, HOOKS_REL):
        u.done.append(f"{LABEL}: removed the safety hook from ~/{HOOKS_REL}")
    if u.run(f"the approval rules in ~/{SETTINGS_REL}", agents.json_unmerge, ctx, KEY, SETTINGS_REL):
        u.done.append(f"{LABEL}: removed the approval rules from ~/{SETTINGS_REL}")
    removed = [u.run(f"the skill at ~/{rel}", agents.remove_skill_copy, ctx, KEY, rel)
               for rel in (CLI_SKILL_REL, APP_SKILL_REL)]
    if any(removed):
        u.done.append(f"{LABEL}: removed the skill '{registry.NAME}'")
    return u.finish()


# ---------------------------------------------------------------- the hook

def hook_input(data: dict) -> Tuple[str, str]:
    """{"toolCall": {"name": "run_command", "args": {"CommandLine": "..."}}, ...}. A command
    is checked as text (its Cwd is read as the folder it runs in, see hook_scan_text, and is not
    part of the text); the file tools (write_to_file, replace_file_content,
    multi_replace_file_content) by every string in their arguments; manage_task send_input,
    which types into a running terminal, by its Input text; other tools are not ours."""
    import hook
    call = data.get("toolCall")
    if not isinstance(call, dict) or not isinstance(call.get("name"), str):
        raise ValueError("no tool call")
    name, args = call["name"], call.get("args")
    if name == COMMAND_TOOL:
        if not isinstance(args, dict) or not isinstance(args.get("CommandLine"), str):
            raise ValueError("no command text")
        rest = [s for k, v in args.items() if k not in ("CommandLine", "Cwd") for s in hook.strings_in(v)]
        return "command", "\n".join([args["CommandLine"]] + rest)       # Cwd is read as a folder: hook_scan_text
    if name in FILE_TOOLS:
        text = "\n".join(hook.strings_in(args))
        if not text:
            raise ValueError("no file arguments")           # a file tool with nothing to read is not a pass
        return "file", text
    if name == TASK_TOOL:
        if not isinstance(args, dict) or not isinstance(args.get("Action"), str):
            raise ValueError("no task action")
        if args["Action"] != "send_input":
            return "none", ""                                # starting, listing or stopping a task types nothing
        if not isinstance(args.get("Input"), str):
            raise ValueError("no input text")
        return "command", args["Input"]                      # text typed into a running terminal is a command
    return "none", ""


def _run_command_cwd(data: dict) -> Any:
    """The Cwd of a run_command call (None when it has none or the call is not one)."""
    call = data.get("toolCall")
    args = call.get("args") if isinstance(call, dict) else None
    if not isinstance(call, dict) or call.get("name") != COMMAND_TOOL or not isinstance(args, dict):
        return None
    return args.get("Cwd")


def hook_scan_text(data: dict, command: str) -> Optional[str]:
    """What the checks read for a run_command: the folder it runs in first (Cwd, only where it can
    matter, see hook.cwd_prefix), then the command and the call's other text. None when the command
    alone is what to check."""
    cwd = _run_command_cwd(data)
    if cwd is None:
        return None
    import hook
    prefix = hook.cwd_prefix(cwd, command)               # ValueError for a folder that is not text
    return prefix + command if prefix else None


def hook_cwd_known(data: dict) -> bool:
    """Whether a full path was given for the folder the command runs in (see hook.folder_given)."""
    import hook
    return hook.folder_given(_run_command_cwd(data))


def hook_answer(reason: Optional[str]) -> Tuple[str, int]:
    if not reason:
        return json.dumps({"decision": "allow"}) + "\n", 0
    return json.dumps({"decision": "force_ask", "reason": reason}) + "\n", 0


def hook_failure(reason: str) -> Tuple[str, int]:
    return json.dumps({"decision": "force_ask", "reason": reason}) + "\n", 0
