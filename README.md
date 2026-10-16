# Bertie

[![tests](https://github.com/vishavkamboj95/bertie/actions/workflows/test.yml/badge.svg)](https://github.com/vishavkamboj95/bertie/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/github/license/vishavkamboj95/bertie)](LICENSE)

**A butler for your Google account, for AI agents on the Mac. He reads freely, and asks before anything leaves the house.**

Bertie lets an AI agent work in one Google account: Gmail, Calendar, Chat and Drive.
It is built and tested for Claude Code, and its commands work from any agent that can
use the terminal, such as Codex, Antigravity or Kimi Code
([what works where](#using-bertie-with-other-agents)). The agent can search your mail,
read a thread, check your diary, draft a reply or fetch a file without bothering you. But nothing goes out (no email, no invitation, no Chat
message, no shared file) until you have seen exactly what it is and signed for it
with your fingerprint.

```
sh install.sh you@example.com --client-secret ~/Downloads/client_secret_XXXX.json
```

Then, in Claude Code: *"Bertie, what is on my calendar today?"*

macOS only. You bring your own Google Cloud project (about 10 minutes, once); the
steps are under [Set up your Google project](#set-up-your-google-project).

## Meet Bertie

Bertie is a discreet, unflappable English butler. He keeps to a few house rules:

- **He fetches and reads without fuss.** Searching, reading, listing, downloading and
  drafting never need your permission.
- **He never sends a letter you have not seen.** Mail is always drafted first. Only
  an existing draft can be sent, and only after you approve it.
- **He presents everything on a tray.** Before anything leaves, a Mac window shows
  you who it goes to, the subject, the files with their real paths, and the start of
  the text. Then you sign for it: Touch ID, or your Mac password.
- **He never throws anything away for good.** "Delete" means the Bin, and he can
  bring it back. There is no permanent delete anywhere.
- **He keeps a ledger.** `bertie log` shows everything he has changed, newest first.
  Message bodies are never written to it.
- **He speaks plainly when it matters.** The manners are for everyday replies. Errors,
  refusals and warnings are said straight, in full.

Inside Claude Code, Bertie is a skill: Claude picks him up whenever a task touches your
mail, calendar, Chat or Drive, or when you address him by name.

## What he does freely, and what he asks first

| Service | Free (no prompt) | Asks first (three approvals) |
|---|---|---|
| Mail (`bertie-mail`) | `whoami`, `search`, `read`, `thread`, `attachments` (saves files), `drafts`, `show-draft`, `draft` (only creates a draft) | `send` (an existing draft only), `trash` (moves to the Bin), `untrash` (brings it back) |
| Calendar (`bertie-calendar`) | `agenda`, `freebusy`, `event` | `create`, `move`, `cancel` (Google emails the attendees) |
| Chat (`bertie-chat`) | `spaces`, `read` | `send`, `dm` |
| Drive (`bertie-drive`) | `list`, `ls`, `download` | `upload` (a new file), `update` (overwrites a file's content), `share` |
| Account (`bertie`) | `login`, `whoami`, `log`, `check` | `logout`, `uninstall` |

Every command that changes something also has `--dry-run`, which prints exactly what
would happen, does nothing, and shows no window. Bertie's skill tells Claude to run
that first and show you the result before the real command.

## The three approvals

Every action in the "asks first" column passes three checks, in this order:

1. **Claude Code's own prompt.** The installer adds an "ask" rule for each of those
   actions, and a safety hook that reads the whole command (any letter case, after
   `&&` or `|`, inside `$(...)` or `sh -c`, behind redirections, through `xargs`) and
   asks whenever it sees one, or sees a way round the tools.
2. **A Mac window titled "Bertie asks first".** It lists the account, every recipient,
   attendee, share target and attachment in full, and a preview of the text. Cancel is
   the default. The other button carries a fresh random number each time
   ("Continue 4821"), so a script cannot know which button to press.
3. **Your fingerprint, or your Mac password.** A small helper, compiled on your Mac by
   the installer, asks macOS to check it is really you. If the helper is missing or
   has been changed, the action is refused. It never falls back to the window alone.

Cancel, close the window, or leave it for two minutes, and nothing happens. There is
no flag, setting or environment variable that skips steps 2 and 3.

Read [SECURITY.md](SECURITY.md) for what this protects against, and what it does not.

## Using Bertie with other agents

Bertie is built and tested for Claude Code. His commands are ordinary terminal
commands, though, so any agent that can run them (Codex, Antigravity, Kimi Code and
others) can use them too, and so can you by hand.

| | Claude Code | Any other agent |
|---|---|---|
| The commands | yes | yes |
| Approval 2: the Mac window | yes | yes |
| Approval 3: fingerprint or Mac password | yes | yes |
| Approval 1: the agent's own prompt and safety hook | yes | not yet |
| Instructions that teach the agent to use Bertie | yes (a skill) | not yet; point your agent at [skill/bertie/SKILL.md](skill/bertie/SKILL.md) |

Approvals 2 and 3 live inside the commands, so nothing goes out without the window
and your fingerprint, whichever agent asks. The installer only sets up Claude Code
for now. Support for other agents is planned.

Some agents run commands in a sandbox that blocks the Mac window or the Keychain. If a
command fails with a Keychain or window error under another agent, that agent's
sandbox is the likely cause.

## What you need

- **A Mac** running macOS 12 or later, with Touch ID or a login password.
- **Claude Code.**
- **The Mac developer tools**, to build the fingerprint helper. If you do not have
  them, run `xcode-select --install`.
- **A Google account.** A Google Workspace account (your company's) works best:
  - **Mail, Calendar and Drive** work with any Google account.
  - **Chat** works only with a Google Workspace account. Google does not let personal
    Gmail accounts use the Chat API.

## Set up your Google project

Bertie talks to Google through an OAuth client that you own, in your own Google Cloud
project. Nobody else's project is involved, and nothing is shared with the author.
You do this once.

1. **Create a project.** Go to [console.cloud.google.com](https://console.cloud.google.com),
   open the project picker at the top, and choose **New project**. Any name will do.
2. **Switch on the four APIs.** In **APIs and Services**, then **Library**, search for
   and enable each of these:
   - Gmail API
   - Google Calendar API
   - Google Drive API
   - Google Chat API (Workspace accounts only)
3. **Set up the consent screen.** Open **Google Auth Platform** (in older consoles,
   **APIs and Services**, then **OAuth consent screen**) and follow the prompts:
   - **App name:** anything, for example "Bertie". **Support email:** yours.
   - **Audience:**
     - **Google Workspace account:** choose **Internal**. This is the easy path (see the
       box below).
     - **Personal Gmail:** choose **External**, then add your own address under
       **Test users**.
   - **Contact email:** yours.
4. **Create the client.** In **Clients** (or **Credentials**, then **Create credentials**,
   then **OAuth client ID**), choose application type **Desktop app**, name it, and
   create it. Click **Download JSON**. That file is your client secret. Keep it private
   and never commit it.
5. **Chat only: configure a Chat app.** Google refuses to send Chat messages for a
   project that has no Chat app. In **APIs and Services**, open **Google Chat API**, then
   **Configuration**, and fill in:
   - **App name:** anything.
   - **Avatar URL:** any https image address.
   - **Description:** anything.

   Leave the interactive features switched off, and under **Visibility**, make it
   available to yourself (or your organisation). Save. Without this, reading Chat works,
   but sending fails with `404 Google Chat app not found`.

> **The 7-day trap, said plainly.** On a personal Gmail account the app is "External" and
> starts in "Testing" mode. In Testing mode Google expires your sign-in after **7 days**,
> so every week Bertie will say he could not refresh the sign-in, and you run
> `bertie login` again. There is a second catch: Bertie uses `gmail.modify` (to send
> drafts and move mail to the Bin) and full `drive` access. Google counts both as
> **restricted** scopes, which need Google's verification before an External app can
> be published to other people.
>
> You have two choices:
> - Live with a weekly `bertie login`.
> - Press **Publish app** to move it to "In production". Google will then warn that the
>   app is unverified each time you sign in. As its owner, you can choose **Advanced**
>   and continue.
>
> **Google Workspace users who pick "Internal" avoid both problems.** The sign-in does
> not expire weekly, and no verification is needed for use inside your own organisation.

## Install

Download or clone this repository, then run the installer with your email address and
the file you downloaded in step 4:

```
git clone https://github.com/vishavkamboj95/bertie.git
cd bertie
sh install.sh you@example.com --client-secret ~/Downloads/client_secret_XXXX.json
```

The installer:

1. Copies the tools to `~/.config/bertie` and makes them read-only.
2. Builds a private Python environment, with exact, hash-checked package versions.
3. Builds the fingerprint helper on your Mac.
4. Adds five commands to `~/.local/bin`.
5. Adds the Bertie skill to `~/.claude/skills/bertie`.
6. Merges the approval rules and the safety hook into `~/.claude/settings.json`. It makes
   a dated backup first and keeps everything already there.
7. Opens your browser once: choose your account and tick **every** box. If any box is
   left unticked, nothing is saved, and you run `bertie login` again.
8. Finishes with `bertie check`, a small read on every service. If a service is not
   switched on in your Google project, it says so in plain words.

Then open a **new** terminal window and start Claude Code. You can run the installer
again at any time. It repairs the setup and keeps your sign-in, and you only need
`--client-secret` again if you want to switch to a different client.

## Everyday use

Talk to Claude as you would to a butler:

- *"Bertie, anything urgent in my inbox this morning?"*
- *"Search my mail for the signed contract from Acme and summarise it."*
- *"Draft a reply to that, saying Thursday works. Show me before you send it."*
- *"Am I free on Friday afternoon? If so, invite Priya to a 30 minute call."*
- *"Find the Q3 deck in Drive and download it to my Desktop."*
- *"What did Bertie change today?"*

Times are shown in your Mac's time zone, and a time you give without an offset is read
in it.

## Commands

You rarely type these yourself; Claude does. They are here so you know what he can do.

```
bertie whoami | log [-n 50] | check | login | logout | uninstall --yes

bertie-mail whoami
bertie-mail search "from:billing is:unread" --max 10
bertie-mail read <message-id>          bertie-mail thread <thread-id>
bertie-mail attachments <message-id> --out DIR
bertie-mail drafts                     bertie-mail show-draft <draft-id>
bertie-mail draft --to a@example.com --subject "Hi" --body "text" [--attach FILE ...]
bertie-mail draft --reply-to <message-id> --body-file reply.txt
bertie-mail send <draft-id>            (asks first; sends only an existing draft)
bertie-mail trash <message-id>         bertie-mail untrash <message-id>     (ask first)

bertie-calendar agenda [--from 2026-10-15] [--to 2026-10-17]
bertie-calendar freebusy <start> <end> [email ...]
bertie-calendar event <event-id>
bertie-calendar create --title "Sync" --start 2026-10-15T15:00 --end 2026-10-15T15:30 \
    [--attendees a@example.com ...] [--meet] [--no-invites]                 (asks first)
bertie-calendar move <event-id> --start ... --end ...                       (asks first)
bertie-calendar cancel <event-id>                                           (asks first)

bertie-chat spaces
bertie-chat read <space-or-email> --max 20
bertie-chat send spaces/AAAA "text"    bertie-chat dm <email> "text"       (ask first)

bertie-drive list "quarterly"          bertie-drive ls <folder-id-or-url>
bertie-drive download <id-or-url> --out ~/Downloads
bertie-drive upload <path> [--folder <id-or-url>]                           (asks first)
bertie-drive update <file-id> <path>                                        (asks first)
bertie-drive share <file-id> <email> --role reader|commenter|writer         (asks first)
bertie-drive share <file-id> anyone --role reader --anyone                  (asks first)
```

`--json` works before or after the subcommand. Drive links work anywhere an id does.
Downloads never overwrite: a second copy is saved as `name (1).ext`.

## What he will not do (on purpose)

- **Send text as an email directly.** Mail is drafted first. Only an existing draft can
  be sent.
- **Permanently delete anything.** "Delete" is the Bin, and `untrash` brings a message
  back. The mail permission he holds (`gmail.modify`) cannot permanently delete either.
  Cancelling a calendar event uses Google's only cancel call.
- **Read anyone else's private conversations.** Chat covers only conversations you are
  part of.
- **Share a Drive file with "anyone with the link"** unless that is asked for with
  `--anyone`, and the dry run says so loudly.
- **Act for a different account** than the one he was installed for. Before every
  change he asks Google whose sign-in it is, and stops if it does not match.
- **Start a new email thread by accident.** A reply with a changed subject is refused
  unless `--new-thread` is added.

Drive access is the full Drive permission, so he can find and fetch any of your files.
There is no Drive delete or trash command, so that power goes unused.

## Where your sign-in is kept

In the macOS Keychain (entry `bertie`, account = your email address), never in a plain
file. The OAuth client file you downloaded lives in `~/.config/bertie`, read-only.

## Signing out and uninstalling

- **Sign out only:** `bertie logout`. The window asks first. It revokes the sign-in at
  Google and deletes the Keychain entry. If Google cannot be reached, it keeps the
  entry so you can try again. `--force-local` deletes it anyway; then remove access at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
- **Remove everything:** `bertie uninstall --yes`. The window asks once. Then Bertie
  signs out and removes exactly what the installer added:
  - the five commands
  - the skill
  - the approval rules and the hook in `~/.claude/settings.json`
  - the PATH line, if the installer added one
  - `~/.config/bertie`, which also holds the action log (run `bertie log` first if you
    want a copy)

  Nothing else is touched. Backups of your settings file are left in `~/.claude` for you
  to delete when you like, and an empty `~/.config` folder stays if the installer made it.

## Troubleshooting

| Message | What it means |
|---|---|
| `command not found: bertie-mail` | Open a new terminal window. |
| `wrong account` | The sign-in is for a different mailbox than this Mac was set up for. Nothing was done. Run `bertie login` and choose the right one. |
| `token is missing scopes` | A permission box was not ticked, or a new one was added. Run `bertie login` and tick every box. |
| `could not refresh the sign-in`, about a week after setup | Testing mode's 7-day limit. Run `bertie login`, and see the box under [Set up your Google project](#set-up-your-google-project). |
| `... is not switched on yet in your Google Cloud project` | Enable that API (step 2), then run `bertie check`. |
| `404 Google Chat app not found` | Configure the Chat app (step 5). |
| `the fingerprint/password helper is missing` | Run `xcode-select --install`, then the installer again. |
| `cancelled, so nothing was done` | The window was cancelled or left unanswered. Nothing was sent. |
| `too many recipients/attachments to show; split it` | The window shows every recipient in full or not at all. Send in smaller groups. |
| An approval prompt for a command that looks harmless | The safety hook reads the whole command and asks when in doubt, for example when "bertie" appears without an action after it. That is intended. |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `tools/registry.py` is the one
list of services, scopes and gated actions, and a new service is one file plus one entry
there. Every safety test must keep passing, and none may be weakened.

## Licence

[MIT](LICENSE).

Made by Celorus.
