#!/usr/bin/env bash
# ============================================================
# meeseek — one-command interactive setup
#   ./scripts/setup.sh
# Checks prerequisites, installs deps, scaffolds config, and walks you
# through Google ADC login. Safe to re-run; never overwrites existing config.
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."

# --- pretty output --------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$(tput bold); DIM=$(tput dim); RED=$(tput setaf 1); GRN=$(tput setaf 2)
  YEL=$(tput setaf 3); BLU=$(tput setaf 4); RST=$(tput sgr0)
else
  BOLD=""; DIM=""; RED=""; GRN=""; YEL=""; BLU=""; RST=""
fi
step() { printf "\n${BOLD}${BLU}==>${RST} ${BOLD}%s${RST}\n" "$1"; }
ok()   { printf "  ${GRN}✓${RST} %s\n" "$1"; }
warn() { printf "  ${YEL}!${RST} %s\n" "$1"; }
err()  { printf "  ${RED}✗${RST} %s\n" "$1"; }
ask()  { # ask "prompt" "default" -> echoes answer
  local reply
  if [ -n "${2:-}" ]; then read -r -p "  $1 [$2]: " reply || true; echo "${reply:-$2}"
  else read -r -p "  $1: " reply || true; echo "$reply"; fi
}

printf "${BOLD}meeseek — setup${RST}\n"
printf "${DIM}This walks you through dependencies, config, and Google login.${RST}\n"

# --- 1. prerequisites -----------------------------------------------------
step "Checking prerequisites"
MISSING=0
have() { command -v "$1" >/dev/null 2>&1; }

if have uv; then ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else err "uv not found — install: ${BOLD}curl -LsSf https://astral.sh/uv/install.sh | sh${RST}"; MISSING=1; fi

if have gcloud; then ok "gcloud"
else warn "gcloud not found — needed for Google login (Vertex AI, Drive, managed MCP)."
     warn "Install: https://cloud.google.com/sdk/docs/install"; fi

if [ "$MISSING" -eq 1 ]; then
  err "Install the missing required tools above, then re-run ./scripts/setup.sh"
  exit 1
fi

# --- 2. dependencies ------------------------------------------------------
step "Installing Python dependencies (uv sync)"
uv sync
ok "dependencies installed into .venv"

# --- 3. config scaffolding ------------------------------------------------
step "Configuration"
if [ -f project.yaml ]; then
  ok "project.yaml already exists — leaving it untouched"
else
  cp project.yaml.example project.yaml
  ok "created project.yaml from project.yaml.example"
  warn "fill in your systems before the first run (README → Setup → 2)"
fi

if [ -f .env ]; then
  ok ".env already exists — leaving it untouched"
else
  cp .env.example .env
  ok "created .env from the example"
  warn "Fill in .env: JIRA_USERNAME, JIRA_API_TOKEN, GITLAB_TOKEN (README → Setup → 3)."
fi

# --- 4. Google ADC login --------------------------------------------------
step "Google authentication (ADC)"
if ! have gcloud; then
  warn "gcloud missing — skipping. Install it, then run the login command from the README."
else
  # Check the ADC's scopes, not just that it exists: a plain
  # `gcloud auth application-default login` yields a working token with no
  # Workspace scopes, and every Chat/Gmail/Drive tool would then 403.
  if uv run python -m meeseek_pack_google.auth --check >/dev/null 2>&1; then
    ok "Application Default Credentials present, with the Workspace scopes"
  else
    uv run python -m meeseek_pack_google.auth --check || true
    warn "One login covers Vertex AI, managed MCP, Drive, Gmail, Chat."
    do_login=$(ask "Run gcloud ADC login now? (y/n)" "y")
    if [ "$do_login" = "y" ] || [ "$do_login" = "Y" ]; then
      # The OAuth client the login goes through. gcloud's own is blocked by
      # Workspaces that restrict third-party apps ("This app is blocked"), so
      # the path that works everywhere is your org's own Desktop client — an
      # Internal app has no consent wall.
      CLIENT_ARG=""
      found_client() {
        [ -f client_secret.json ] && return 0
        local dl
        dl=$(ls -t "$HOME"/Downloads/client_secret_*.json 2>/dev/null | head -1 || true)
        if [ -n "$dl" ]; then
          use=$(ask "Found $(basename "$dl") in Downloads — use it? (y/n)" "y")
          if [ "$use" = "y" ] || [ "$use" = "Y" ]; then
            cp "$dl" client_secret.json && ok "copied to client_secret.json (gitignored)" && return 0
          fi
        fi
        return 1
      }
      if ! found_client; then
        GCP_PROJECT=$(uv run python -c "from meeseek.config import cfg; print(cfg.google_cloud.project)" 2>/dev/null || true)
        [ "$GCP_PROJECT" = "your-gcp-project" ] && GCP_PROJECT=""
        warn "No OAuth client of your own (client_secret.json). gcloud's built-in client is"
        warn "blocked by most company Workspaces for Gmail/Chat/Drive. Make one (2 minutes):"
        printf "    1. %s\n" "https://console.cloud.google.com/auth/overview${GCP_PROJECT:+?project=$GCP_PROJECT}"
        printf "       — the OAuth consent screen: Audience ${BOLD}Internal${RST} (no review, no consent wall)\n"
        printf "    2. %s\n" "https://console.cloud.google.com/auth/clients/create${GCP_PROJECT:+?project=$GCP_PROJECT}"
        printf "       — Application type ${BOLD}Desktop app${RST}, Create, then ${BOLD}Download JSON${RST}\n"
        printf "    3. In that project, enable the Gmail, Google Chat and Google Drive APIs\n"
        printf "    (A teammate who made one can hand you the same file.)\n"
        while ! found_client; do
          more=$(ask "Press Enter once the JSON is in Downloads (or here as client_secret.json); 'g' to try gcloud's client anyway, 's' to skip" "")
          case "$more" in
            g|G) warn "Trying gcloud's own client — if the browser says the app is blocked, come back to the steps above."; break ;;
            s|S) break ;;
          esac
        done
      fi
      if [ -f client_secret.json ]; then
        CLIENT_ARG="--client-id-file=client_secret.json"
        ok "using client_secret.json (your own OAuth client)"
      elif [ "${more:-}" = "s" ] || [ "${more:-}" = "S" ]; then
        warn "Skipped. Re-run ./scripts/setup.sh once you have the client JSON."
        do_login="n"
      fi
      if [ "$do_login" = "y" ] || [ "$do_login" = "Y" ]; then
        # One source of truth: the same list the code checks.
        SCOPES=$(uv run python -m meeseek_pack_google.auth --login-scopes)
        # shellcheck disable=SC2086
        gcloud auth application-default login $CLIENT_ARG --scopes="$SCOPES" || true
        if uv run python -m meeseek_pack_google.auth --check; then
          ok "ADC login complete"
        else
          warn "Login did not finish with every scope. 'This app is blocked' in the browser"
          warn "means gcloud's client is barred: make your own (the steps above), re-run."
        fi
      fi
    else
      warn "Skipped. Re-run this script before using Chat/Gmail/Drive tools."
    fi
  fi
fi

# --- 5. verify ------------------------------------------------------------
step "Verifying the environment"
./scripts/doctor.sh || true

printf "\n${BOLD}${GRN}Setup done.${RST} Start the assistant with:\n\n"
printf "  ${BOLD}./scripts/serve.sh${RST}   # web UI at http://localhost:8787\n"
printf "\n"
