#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_NAME="nfl-game-scout"
SKILLS_PARENT="${REPO_DIR}/skills"
OUT_DIR="${REPO_DIR}/dist"
OUT_ZIP="${OUT_DIR}/${SKILL_NAME}.zip"

if [ ! -f "${SKILLS_PARENT}/${SKILL_NAME}/SKILL.md" ]; then
  echo "Error: ${SKILLS_PARENT}/${SKILL_NAME}/SKILL.md not found." >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"
rm -f "${OUT_ZIP}"

# Claude Desktop requires the skill folder itself to be at the root of the ZIP:
#   nfl-game-scout/SKILL.md
#   nfl-game-scout/references/...
(
  cd "${SKILLS_PARENT}"
  zip -rq "${OUT_ZIP}" "${SKILL_NAME}" -x "*.DS_Store" "*__MACOSX*" "*.pyc" "*/__pycache__/*"
)

echo "Built Claude Desktop skill archive: ${OUT_ZIP}"
unzip -l "${OUT_ZIP}"
