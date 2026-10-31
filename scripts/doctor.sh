#!/usr/bin/env bash
# ============================================================
# meeseek — environment health check
#   ./scripts/doctor.sh
# Verifies tools, config, credentials, and that the app imports cleanly.
# Exits non-zero if anything required is missing.
# ============================================================
set -uo pipefail

cd "$(dirname "$0")/.."

if [ -t 1 ]; then
  BOLD=$(tput bold); RED=$(tput setaf 1); GRN=$(tput setaf 2); YEL=$(tput setaf 3); RST=$(tput sgr0)
else BOLD=""; RED=""; GRN=""; YEL=""; RST=""; fi
ok()   { printf "  ${GRN}✓${RST} %s\n" "$1"; }
warn() { printf "  ${YEL}!${RST} %s\n" "$1"; }
err()  { printf "  ${RED}✗${RST} %s\n" "$1"; FAIL=1; }
FAIL=0
have() { command -v "$1" >/dev/null 2>&1; }

printf "${BOLD}meeseek — doctor${RST}\n"

printf "\n${BOLD}Tools${RST}\n"
have uv && ok "uv" || err "uv missing (required)"
have gcloud && ok "gcloud" || warn "gcloud missing (needed for Google auth)"
have npm && ok "node $(node --version 2>/dev/null)" || err "node/npm missing (required to build the web console)"

printf "\n${BOLD}Config${RST}\n"
[ -f project.yaml ] && ok "project.yaml present" || err "project.yaml missing — run ./scripts/setup.sh"
if [ -f .env ]; then
  ok ".env present"
  # A token left at its example value fails only when the first Jira or GitLab
  # call is made, so name it here.
  unfilled=$(grep -E '^(JIRA_USERNAME|JIRA_API_TOKEN|GITLAB_TOKEN)=' .env \
             | grep -E '=$|=your[_-]' | cut -d= -f1 | tr '\n' ' ')
  for v in JIRA_USERNAME JIRA_API_TOKEN GITLAB_TOKEN; do
    grep -qE "^$v=" .env || unfilled="$unfilled$v "
  done
  [ -z "$unfilled" ] && ok ".env tokens filled" \
    || warn ".env still needs: ${unfilled}(README → Setup → 3) — Jira/GitLab tools fail without them"
else
  err ".env missing — run ./scripts/setup.sh"
fi

printf "\n${BOLD}Credentials${RST}\n"
if ! have gcloud; then
  warn "gcloud missing — cannot check Google ADC"
elif ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  warn "Google ADC missing — run ./scripts/setup.sh"
elif uv run python -m meeseek_pack_google.auth --check >/dev/null 2>&1; then
  ok "Google ADC present, with the Workspace scopes"
else
  # Present but under-scoped is the common broken state: GCP/Vertex work,
  # only Workspace tools 403.
  warn "Google ADC present but missing Workspace scopes — Chat/Gmail/Drive will 403"
  uv run python -m meeseek_pack_google.auth --check 2>&1 | sed 's/^/    /' || true
fi

printf "\n${BOLD}App import + config load${RST}\n"
if uv run python -c "
import sys
from meeseek.config import cfg
missing = []
if not cfg.project.name or cfg.project.name == 'Your Project': missing.append('project.name')
for key, why in (('project', 'the model runs on it'), ('quota_project', 'Google API quota')):
    if getattr(cfg.google_cloud, key, '') == 'your-gcp-project': missing.append(f'google_cloud.{key} ({why})')
from meeseek.runtime.skills import discover_skills
skills = discover_skills()
print(f'  loaded {len(skills)} skill(s): ' + ', '.join(s.name for s in skills))
if missing:
    print('  UNFILLED: ' + ', '.join(missing)); sys.exit(3)
" 2>/tmp/doctor_err; then
  ok "config loads and skills discovered"
else
  code=$?
  if [ "$code" = "3" ]; then warn "config loads but still has placeholder values — edit project.yaml"
  else err "app failed to import:"; sed 's/^/    /' /tmp/doctor_err; fi
fi
rm -f /tmp/doctor_err

printf "\n"
if [ "$FAIL" -eq 0 ]; then printf "${GRN}${BOLD}All required checks passed.${RST}\n"; exit 0
else printf "${RED}${BOLD}Some required checks failed.${RST} Fix the ✗ items above.\n"; exit 1; fi
