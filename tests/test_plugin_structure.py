import json
import re
import subprocess
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "nfl-game-scout"
SKILL_MD = SKILL_DIR / "SKILL.md"

ALLOWED_ANTIGRAVITY_KEYS = {
    "name",
    "displayName",
    "version",
    "author",
    "repository",
    "homepage",
    "description",
    "logo",
    "suggestedPrompts",
    "disabled",
}


class TestPluginStructure(unittest.TestCase):
    def test_antigravity_plugin_manifest(self):
        manifest_path = REPO_ROOT / "plugin.json"
        self.assertTrue(manifest_path.is_file())
        data = json.loads(manifest_path.read_text(encoding="utf-8"))

        unknown_keys = set(data.keys()) - ALLOWED_ANTIGRAVITY_KEYS
        self.assertFalse(unknown_keys, f"Unknown Antigravity plugin.json keys: {unknown_keys}")
        self.assertEqual(data["name"], "nfl-game-scout")
        self.assertIsNotNone(re.fullmatch(r"[a-z0-9-]+", data["name"]))
        self.assertLessEqual(len(data.get("suggestedPrompts", [])), 3)

        logo_rel = data.get("logo")
        self.assertTrue(logo_rel and not logo_rel.startswith("/") and "://" not in logo_rel)
        self.assertTrue((REPO_ROOT / logo_rel).is_file())

    def test_claude_plugin_manifests(self):
        plugin_json = REPO_ROOT / ".claude-plugin" / "plugin.json"
        marketplace_json = REPO_ROOT / ".claude-plugin" / "marketplace.json"
        self.assertTrue(plugin_json.is_file())
        self.assertTrue(marketplace_json.is_file())

        p_data = json.loads(plugin_json.read_text(encoding="utf-8"))
        m_data = json.loads(marketplace_json.read_text(encoding="utf-8"))

        self.assertEqual(p_data["name"], "nfl-game-scout")
        self.assertEqual(m_data["name"], "nfl-game-scout")
        self.assertTrue(any(p["name"] == "nfl-game-scout" for p in m_data["plugins"]))

    def test_skill_frontmatter_and_references(self):
        self.assertTrue(SKILL_MD.is_file())
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        parts = text.split("---\n", 2)
        self.assertEqual(len(parts), 3, "SKILL.md must have valid YAML frontmatter delimiters")

        frontmatter = parts[1]
        name_match = re.search(r"^name:\s*([a-z0-9-]+)\s*$", frontmatter, re.MULTILINE)
        self.assertIsNotNone(name_match, "Skill name must be lowercase kebab-case")
        self.assertEqual(name_match.group(1), "nfl-game-scout")
        self.assertLessEqual(len(name_match.group(1)), 64)
        self.assertIn("description:", frontmatter)

        refs = re.findall(r"\(\./(references/[a-z0-9_-]+\.md)\)", text)
        self.assertGreaterEqual(len(refs), 4)
        for rel_ref in refs:
            self.assertTrue((SKILL_DIR / rel_ref).is_file(), f"Missing reference file: {rel_ref}")

    def test_pack_claude_skill_zip(self):
        script = REPO_ROOT / "scripts" / "pack-claude-skill.sh"
        self.assertTrue(script.is_file())
        result = subprocess.run(
            ["bash", str(script)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Built Claude Desktop skill archive", result.stdout)

        zip_path = REPO_ROOT / "dist" / "nfl-game-scout.zip"
        self.assertTrue(zip_path.is_file())
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            self.assertIn("nfl-game-scout/SKILL.md", names)
            self.assertTrue(all(n.startswith("nfl-game-scout/") for n in names))
            self.assertFalse(any(".DS_Store" in n or "__MACOSX" in n for n in names))


if __name__ == "__main__":
    unittest.main()
