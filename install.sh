#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_NAME="nfl-game-scout"
TARGET_ROOT="${HOME}/.gemini/config/plugins"
DST="${TARGET_ROOT}/${PLUGIN_NAME}"

mkdir -p "${TARGET_ROOT}"

if [ -L "${DST}" ] || [ ! -e "${DST}" ]; then
  ln -sfn "${REPO_DIR}" "${DST}"
  echo "Linked Antigravity plugin: ${DST} -> ${REPO_DIR}"
else
  echo "Skipped ${DST} (real directory already exists; move or remove it first)" >&2
  exit 1
fi

echo ""
echo "Next steps:"
echo "  - Antigravity: Plugin loads automatically on your next turn."
echo "  - Claude Desktop (Skill ZIP): run ./scripts/pack-claude-skill.sh and upload dist/nfl-game-scout.zip under Customize -> Skills."
echo "  - Claude Code / Desktop (Plugin Marketplace):"
echo "      claude plugin marketplace add ${REPO_DIR}"
echo "      claude plugin install ${PLUGIN_NAME}@${PLUGIN_NAME}"
