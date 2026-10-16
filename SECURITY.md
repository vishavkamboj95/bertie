# Security

## What Bertie protects against

Bertie is built for one threat: **an AI agent, working on your behalf, making a mistake
with your Google account.** For example:

- it sends a draft you had not finished;
- it replies to the wrong person, or to everyone;
- it shares a file too widely;
- it acts on instructions planted in an email or a document it was asked to read.

Against that, every action that changes something outside your Mac must pass:

1. **Claude Code's permission prompt.** It is triggered by "ask" rules in
   `~/.claude/settings.json`, and by a PreToolUse hook that reads the whole command
   text. The hook also asks about the ways round the tools: running their Python files,
   importing their modules, touching the install folder, the Keychain entry or the
   approval helper, building names at run time, and scripting or clicking the
   confirmation window.
2. **A native macOS window** that shows exactly what will go out:
   - every recipient, attendee, share target and attachment, in full, never cut;
   - the real paths of files;
   - a preview of the text.

   The default button is Cancel. The other button carries a fresh random number each
   time, so a script written in advance cannot know which button to press. The details
   are passed to the window as data, never pasted into a script, and line breaks or
   hidden characters in a subject or name cannot fake a line of their own.
3. **Touch ID or the Mac password**, asked by a small helper that the installer compiles
   on your Mac. Its sha256 is recorded at install time and checked before every use. A
   missing, changed or unavailable helper means the action is refused. It never falls
   back to the window alone.

Steps 2 and 3 live inside the tools. No flag, setting or environment variable skips
them, and a command phrased in a way no rule recognises still stops at the window.

Other design choices that shrink the blast radius:

- **Mail is drafts first.** Only an existing draft can be sent.
- **There is no permanent delete anywhere.** The mail permission is `gmail.modify`,
  which cannot permanently delete.
- **There is no Drive delete or trash command.**
- **"Anyone with the link" sharing needs an explicit `--anyone`.**
- **Before every change, Bertie asks Google whose sign-in it is**, and refuses if it is
  not the account configured at install time.
- **The sign-in token lives in the macOS Keychain**, never in a plain file.
- **Python packages are pinned to exact, hash-checked versions.**
- **The action log records what changed** (time, tool, action, target, subject, id),
  never message bodies.

## What it does not protect against

Bertie protects against an AI agent's mistakes. **It does not protect against hostile
code already running as you.** Plainly:

- **Code running under your login can go round the hook.** The hook reads command text,
  so it is a deny-list, and a deny-list has holes. A command built at run time (through
  variables, `eval`, encoded text or obfuscated `python3 -c`) can hide what it does.
- **Such code can swap the approval helper.** The helper and its recorded sha256 are both
  files you own. Code running as you can replace the two together, and the check would
  then pass.
- **Such code can read the Keychain token and call Google directly.** macOS lets a
  program read the Keychain entries that its own interpreter created. Code using
  Bertie's Python can do this and skip the window entirely. The window only guards the
  tools, not the token.
- **Names pieced together at run time slip past the hook.** For example, a Keychain entry
  name built from `$(echo bert)ie`. macOS may still ask before the `security` tool reads an
  entry it did not create, but do not rely on that.
- **The hook sees one command at a time.** A `cd` into the install's command folder in one
  command, then a bare wildcard such as `./??????` in the next, is not linked up. Wildcard
  spellings of a tool name inside a single command do ask.
- **Skipping Claude Code's prompts switches off step 1.** If you run Claude Code in a
  mode that skips permission prompts, the rules and the hook do not ask. Steps 2 and 3
  still apply.
- **Accessibility or screen-control permission lets a program click the window.** If you
  give the Claude app (or any agent) that permission, a program could click the button
  for you. The fingerprint step still stands, but do not grant that permission for
  this.

These are **accepted residual risks** for version 0.1. The deeper fix would split the
token: keep the write-capable token in a Keychain item protected by
`SecAccessControl(.userPresence)`, so that even Bertie's own Python could not read it
without your fingerprint. Whether that works without a signed binary and a
keychain-access-group entitlement is unverified, so it is not in this release.

The read-only files, the recorded helper sha256 and the hook's checks raise the bar for
an agent that wanders off course. They are not a lock against an attacker who already
has your user account.

## The Google permissions Bertie holds

| Scope | Why |
|---|---|
| `gmail.modify` | read, draft, send a draft, move to and from the Bin (not permanent delete) |
| `calendar.events`, `calendar.readonly` | read the diary; create, move and cancel events |
| `chat.messages.create`, `chat.messages.readonly`, `chat.spaces.readonly`, `chat.spaces.create`, `chat.memberships.readonly` | read your own conversations; post as you |
| `drive` | find, download, upload, overwrite and share files (no delete command exists) |

All of these go to an OAuth client in **your own** Google Cloud project. Nothing is sent
anywhere else.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's
**Report a vulnerability** button on this repository's **Security** tab, which opens a
private advisory. We aim to reply within a week.

Reports we especially want:

- a way to make a gated action happen without the window and the fingerprint, from
  inside a Claude Code session;
- a way to make the window show something other than what will actually be sent.
