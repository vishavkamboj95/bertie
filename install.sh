#!/bin/sh
# Installs Bertie: the bertie-mail, bertie-calendar, bertie-chat and bertie-drive
# commands, the `bertie` account command, and the Claude Code skill that uses them.
# Safe to run again: it updates the tools and keeps an existing login.
#
#   sh install.sh you@example.com --client-secret ~/Downloads/client_secret_XXXX.json
#
# The client secret is the "Desktop app" OAuth client you made in your own Google
# Cloud project (the README walks you through it). It is needed the first time
# only; a later run keeps the one already installed unless you give a new one.
#
# Options (for testing and repair):
#   --skip-deps    do not install the Python packages
#   --skip-login   do not sign in and do not run the final checks
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
# The tool's name lives in one place: NAME in tools/registry.py.
NAME="$(sed -n 's/^NAME = "\([a-z][a-z0-9-]*\)".*/\1/p' "$HERE/tools/registry.py")"
if [ -z "$NAME" ]; then
  echo "This setup folder looks damaged (tools/registry.py has no NAME). Download it again."
  exit 1
fi
ENVNAME="$(printf '%s' "$NAME" | tr 'a-z-' 'A-Z_')"

ACCOUNT="$(eval "printf '%s' \"\${${ENVNAME}_ACCOUNT:-}\"")"
CLIENT=""
SKIP_DEPS=0
SKIP_LOGIN=0

usage() {
  echo "usage: sh install.sh <email-address> [--client-secret <file>] [--skip-deps] [--skip-login]"
  echo "  <email-address>  the Google account the tools will act as."
  echo "  --client-secret  your own OAuth client file (type 'Desktop app'), from Google Cloud."
  echo "                   Needed the first time; see the README, 'Set up your Google project'."
}

WANT_CLIENT=0
GAVE_CLIENT=0
for arg in "$@"; do
  if [ "$WANT_CLIENT" = 1 ]; then CLIENT="$arg"; WANT_CLIENT=0; continue; fi
  case "$arg" in
    --client-secret)   WANT_CLIENT=1; GAVE_CLIENT=1 ;;
    --client-secret=*) CLIENT="${arg#--client-secret=}"; GAVE_CLIENT=1 ;;
    --skip-deps)  SKIP_DEPS=1 ;;
    --skip-login) SKIP_LOGIN=1 ;;
    -h|--help)    usage; exit 0 ;;
    -*)           echo "unknown option: $arg"; usage; exit 2 ;;
    *)            ACCOUNT="$arg" ;;
  esac
done
if [ "$WANT_CLIENT" = 1 ] || { [ "$GAVE_CLIENT" = 1 ] && [ -z "$CLIENT" ]; }; then
  echo "--client-secret needs a file name after it"; usage; exit 2
fi

case "$ACCOUNT" in
  "")  echo "Which account? Give the email address, e.g.  sh install.sh you@example.com"; usage; exit 2 ;;
  *" "*|*@*@*|@*|*@) echo "That does not look like an email address: $ACCOUNT"; exit 2 ;;
  *@*.*) ;;
  *)   echo "That does not look like an email address: $ACCOUNT"; exit 2 ;;
esac

# Touch ID, the native confirmation window and the Keychain are Mac features.
if [ "$(uname -s)" != "Darwin" ]; then
  echo "Bertie works on macOS only: it relies on Touch ID (or the Mac password), the macOS"
  echo "confirmation window and the macOS Keychain. Nothing was installed."
  exit 1
fi

DEST="$HOME/.config/$NAME"
BIN="$HOME/.local/bin"
PY="$DEST/.venv/bin/python"

# The OAuth client: a new one given now, or the one installed by an earlier run.
if [ -n "$CLIENT" ]; then
  case "$CLIENT" in "~/"*) CLIENT="$HOME/${CLIENT#"~/"}" ;; esac
  if [ ! -f "$CLIENT" ] || [ -L "$CLIENT" ]; then
    echo "No such file (or it is a link, not the file itself): $CLIENT"
    exit 2
  fi
  client_field() { plutil -extract "$1" raw -o - "$CLIENT" 2>/dev/null; }
  if ! CLIENT_ID="$(client_field installed.client_id)"; then
    if client_field web.client_id >/dev/null; then
      echo "That is a 'Web application' OAuth client. Bertie needs a 'Desktop app' client:"
      echo "in Google Cloud, Credentials, Create credentials, OAuth client ID, Application type 'Desktop app'."
    else
      echo "That file is not a Google OAuth client file: $CLIENT"
      echo "Download it from Google Cloud: Credentials, then the download button next to your Desktop app client."
    fi
    exit 2
  fi
  case "$CLIENT_ID" in
    *.apps.googleusercontent.com) ;;
    *) echo "That file's client_id does not look like a Google OAuth client id: $CLIENT"; exit 2 ;;
  esac
  if [ -z "$(client_field installed.client_secret)" ]; then
    echo "That file has no client_secret: $CLIENT. Download it again from Google Cloud."
    exit 2
  fi
elif [ ! -f "$DEST/client_secret.json" ]; then
  echo "The first install needs your own Google OAuth client file:"
  echo "    sh install.sh $ACCOUNT --client-secret <path-to-client_secret.json>"
  echo "The README, 'Set up your Google project', shows how to make one (about 10 minutes)."
  exit 2
fi

say() { printf '\n==> %s\n' "$1"; }

# The folder and the skill share a common name; never take over someone else's.
MARK="$DEST/.installed-by-$NAME"
if [ -e "$DEST" ] && [ ! -e "$MARK" ] && [ ! -e "$DEST/install-manifest.json" ]; then
  echo "$DEST already exists and was not made by this installer (or is left over from an"
  echo "install that stopped early). Move it out of the way and run this again."
  exit 1
fi
SKILLMD="$HOME/.claude/skills/$NAME/SKILL.md"
if [ -e "$SKILLMD" ] && ! grep -q "^name: $NAME\$" "$SKILLMD"; then
  echo "$SKILLMD already exists and is not this tool's skill. Move it out of the way and run this again."
  exit 1
fi

say "Installing Bertie for $ACCOUNT into ~/.config/$NAME"
mkdir -p "$DEST" "$BIN"
: > "$MARK"
chmod 700 "$DEST"
# An earlier run left these read-only; remove them so the new copies replace them.
rm -f "$DEST"/*.py "$DEST/hook.sh" "$DEST/approve.swift" "$DEST/requirements.txt"
cp "$HERE"/tools/*.py "$HERE/tools/hook.sh" "$HERE/tools/approve.swift" "$HERE/requirements.txt" "$DEST/"
if [ -n "$CLIENT" ]; then
  # copy beside it, then swap in, so giving the installed file itself is harmless
  cp "$CLIENT" "$DEST/client_secret.json.new"
  rm -f "$DEST/client_secret.json"
  mv "$DEST/client_secret.json.new" "$DEST/client_secret.json"
fi
# read-only: a small speed bump against casual edits (the owner can still change them)
chmod 400 "$DEST"/*.py "$DEST/client_secret.json" "$DEST/requirements.txt" "$DEST/approve.swift"
chmod 500 "$DEST/hook.sh"
printf '%s\n' "$ACCOUNT" > "$DEST/account"
chmod 600 "$DEST/account"

say "Building a private Python environment (one-time, about a minute)"
if command -v uv >/dev/null 2>&1; then
  [ -x "$PY" ] || uv venv --quiet "$DEST/.venv"
  if [ "$SKIP_DEPS" = 0 ]; then
    # exact, hash-checked versions; --python keeps uv from touching any other venv
    uv pip install --quiet --python "$PY" --require-hashes --no-deps -r "$DEST/requirements.txt"
  fi
else
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is missing. Run:  xcode-select --install   then run this installer again."
    exit 1
  fi
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "Python 3.9 or newer is needed. Install uv (https://docs.astral.sh/uv/) or a newer Python, then run this again."
    exit 1
  fi
  [ -x "$PY" ] || python3 -m venv "$DEST/.venv"
  if [ "$SKIP_DEPS" = 0 ]; then
    "$PY" -m pip install --quiet --disable-pip-version-check --require-hashes --no-deps -r "$DEST/requirements.txt"
  fi
fi

say "Building the fingerprint / Mac password approval helper"
HELPER="$DEST/$NAME-approve"
HELPER_OK=0
rm -f "$HELPER" "$DEST/approve.sha256" "$DEST/helper-build.log"
SWIFTC="$(command -v swiftc 2>/dev/null || true)"
if [ "$SWIFTC" = "/usr/bin/swiftc" ] && ! xcode-select -p >/dev/null 2>&1; then
  SWIFTC=""
fi
if [ -z "$SWIFTC" ]; then
  echo "    The Mac developer tools are not installed, so the approval helper cannot be built."
  echo "    Run:  xcode-select --install     then run this installer again."
  echo "    Until then reading works, but sending, deleting and sharing will refuse."
elif "$SWIFTC" -O -o "$HELPER" "$DEST/approve.swift" 2>"$DEST/helper-build.log"; then
  chmod 500 "$HELPER"
  shasum -a 256 "$HELPER" | cut -d' ' -f1 > "$DEST/approve.sha256"
  chmod 400 "$DEST/approve.sha256"
  rm -f "$DEST/helper-build.log"
  HELPER_OK=1
  echo "    Built. Every send, delete or share will now ask for your fingerprint or Mac password."
else
  rm -f "$HELPER"
  echo "    The approval helper could not be built (the reason is in $DEST/helper-build.log)."
  echo "    Try:  xcode-select --install     then run this installer again."
  echo "    Until then reading works, but sending, deleting and sharing will refuse."
fi

say "Adding the commands, the skill, the approval rules and the safety hook"
ADDPATH=""
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) ADDPATH="--add-path" ;;
esac
# shellcheck disable=SC2086
"$PY" -I "$DEST/kit_setup.py" install --kit-dir "$HERE" $ADDPATH
PATH="$BIN:$PATH"; export PATH

if [ "$SKIP_LOGIN" = 1 ]; then
  say "Skipping sign-in and checks (--skip-login)"
else
  if "$BIN/$NAME" whoami >/dev/null 2>&1; then
    say "Already signed in - keeping the existing login"
  else
    say "Sign-in: a browser window will open."
    echo "    Pick $ACCOUNT and click Allow on every box. That is the only manual step."
    "$BIN/$NAME" login
  fi

  say "Checking it works"
  FAILED=0
  "$BIN/$NAME" whoami || FAILED=1
  # A service that is simply switched off in Google Cloud is explained in
  # plain words and does not make this fail.
  "$BIN/$NAME" check || FAILED=1
  if [ "$FAILED" = 1 ]; then
    echo
    echo "The tools are installed, but a check above failed. The README's 'Troubleshooting' section explains each message."
    exit 1
  fi
fi

if [ "$HELPER_OK" = 0 ]; then
  echo
  echo "NOT READY FOR SENDING: the approval helper is missing (see above). Reading works."
  echo "Run:  xcode-select --install     then run this installer again."
fi

say "Done. Open a NEW terminal, start Claude Code, and say:"
echo '    "Bertie, what is on my calendar today?"'
