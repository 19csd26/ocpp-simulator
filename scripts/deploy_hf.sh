#!/usr/bin/env bash
# Publish this repo to a Hugging Face Space (Docker SDK).
#   HF_TOKEN=hf_xxx scripts/deploy_hf.sh <hf-username>/<space-name>
#   DRY_RUN=1 scripts/deploy_hf.sh <hf-username>/<space-name>     # build the bundle, push nothing
# HF_TOKEN needs "write" access. The Space is created if it does not exist (private unless VISIBILITY=public).
# Spaces reads its settings from a YAML header at the top of README.md, so this pushes a copy of the repo with that
# header added, leaving the README in this repo (and on GitHub) untouched.
set -euo pipefail
SPACE="${1:?usage: HF_TOKEN=hf_xxx scripts/deploy_hf.sh <hf-username>/<space-name>}"
OWNER="${SPACE%%/*}"; NAME="${SPACE#*/}"
[ "$OWNER" != "$SPACE" ] && [ -n "$NAME" ] || { echo "Give the Space as <username>/<name>"; exit 1; }
[ -n "${DRY_RUN:-}" ] || : "${HF_TOKEN:?set HF_TOKEN to a Hugging Face token with write access}"
cd "$(dirname "$0")/.."

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
BUNDLE="$TMP/bundle"; mkdir -p "$BUNDLE"

# Tracked and new-but-not-ignored files (never .venv, node_modules, dist, logs...).
git ls-files -z --cached --others --exclude-standard | tar --null -T - -cf - | tar -xf - -C "$BUNDLE"
{
  printf -- '---\ntitle: OCPP 2.0.1 Charge Point Simulator\nemoji: ⚡\ncolorFrom: blue\ncolorTo: green\nsdk: docker\napp_port: 8100\npinned: false\n---\n\n'
  cat README.md
} > "$BUNDLE/README.md"

if [ -n "${DRY_RUN:-}" ]; then
  echo "Bundle for $SPACE ($(find "$BUNDLE" -type f | wc -l | tr -d ' ') files):"; (cd "$BUNDLE" && find . -type f | sort | head -80)
  echo; head -9 "$BUNDLE/README.md"; exit 0
fi

PRIVATE=true; [ "${VISIBILITY:-private}" = "public" ] && PRIVATE=false
code=$(curl -s -o "$TMP/create.json" -w "%{http_code}" https://huggingface.co/api/repos/create \
  -H "Authorization: Bearer $HF_TOKEN" -H "Content-Type: application/json" \
  -d "{\"type\":\"space\",\"name\":\"$NAME\",\"organization\":\"$OWNER\",\"sdk\":\"docker\",\"private\":$PRIVATE}")
case "$code" in 200|409) ;; *) echo "Could not create the Space (HTTP $code):"; cat "$TMP/create.json"; exit 1 ;; esac

cd "$BUNDLE"
git init -q -b main
git config user.name "$(git -C "$OLDPWD" config user.name || echo "$OWNER")"
git config user.email "$(git -C "$OLDPWD" config user.email || echo "$OWNER@users.noreply.huggingface.co")"
git add -A && git commit -q -m "Deploy OCPP simulator"
git push -q --force "https://$OWNER:$HF_TOKEN@huggingface.co/spaces/$SPACE" main

HOST="$(printf '%s-%s' "$OWNER" "$NAME" | tr '[:upper:]_' '[:lower:]-')"
echo
echo "Pushed. Build logs: https://huggingface.co/spaces/$SPACE"
echo "Next: Settings > Variables and secrets > New secret  SIM_TOKEN = a long random value (openssl rand -hex 16)."
echo "Without it the container refuses to start. Open the direct URL, not the huggingface.co page:"
echo "  https://$HOST.hf.space"
