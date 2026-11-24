import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SCRIPTS_DIR = REPO_ROOT / "scripts"


class TestArenaAndShorts(unittest.TestCase):
    def test_arena_cli_validate_and_prompt(self):
        res = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "arena_cli.py"), "validate"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("OK:", res.stdout)

        prompt_res = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "arena_cli.py"), "prompt", "--slate", "2026-thanksgiving"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("bears-at-lions", prompt_res.stdout)
        self.assertIn("eagles-at-cowboys", prompt_res.stdout)
        self.assertIn("chiefs-at-bills", prompt_res.stdout)

    def test_thanksgiving_slate_completeness(self):
        tg = json.loads((DOCS_DIR / "data" / "2026-thanksgiving.json").read_text(encoding="utf-8"))
        games = tg["games"]
        self.assertEqual(len(games), 3)
        self.assertEqual([g["id"] for g in games], ["bears-at-lions", "eagles-at-cowboys", "chiefs-at-bills"])

        for game in games:
            self.assertEqual(len(game["orgChart"]["tiers"]), 4)
            self.assertTrue(game["orgChart"]["hiddenMismatch"])
            self.assertTrue(game["referee"]["headRef"])
            self.assertGreaterEqual(game["sentiment30d"]["awayScore"], -5)
            self.assertLessEqual(game["sentiment30d"]["awayScore"], 5)
            locked = [p for p in game["predictions"] if p["status"] == "locked"]
            self.assertGreaterEqual(len(locked), 1)
            self.assertTrue(locked[0]["surpriseCall"]["title"])

    def test_arena_cli_ingest_on_temp_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_data = Path(tmp) / "data"
            shutil.copytree(DOCS_DIR / "data", tmp_data)
            sample_pred = Path(tmp) / "claude.json"
            sample_pred.write_text(
                json.dumps(
                    [
                        {
                            "gameId": "bears-at-lions",
                            "modelId": "claude",
                            "modelLabel": "Claude (Claude Desktop)",
                            "winnerAbbr": "CHI",
                            "projectedScore": {"away": 28, "home": 24},
                            "spreadLean": "Bears -2.5",
                            "totalLean": "Under 55.5",
                            "primaryKey": "Ben Johnson outschemes Detroit's defensive front.",
                            "refCall": "Shawn Hochuli — holding slows Detroit early.",
                            "sentimentEdge": "CHI +4.0 vs DET +4.5.",
                            "hiddenMismatch": "Antwaan Randle El vs Deshea Townsend.",
                            "surpriseCall": {
                                "title": "Caleb Williams WR-Throwback TD",
                                "detail": "Ben Johnson dials up a throwback pass to Williams in the red zone.",
                                "ingredients": ["Ex-Lions OC", "Indoor dome", "55.5 shootout"],
                                "probability": "~10%",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            import sys
            sys.path.insert(0, str(SCRIPTS_DIR))
            import arena_cli  # noqa: E402

            count = arena_cli.ingest_predictions("2026-thanksgiving", sample_pred, None, data_dir=tmp_data)
            self.assertEqual(count, 1)
            updated = json.loads((tmp_data / "2026-thanksgiving.json").read_text(encoding="utf-8"))
            claude_pred = next(p for p in updated["games"][0]["predictions"] if p["modelId"] == "claude")
            self.assertEqual(claude_pred["status"], "locked")
            self.assertEqual(claude_pred["winnerAbbr"], "CHI")

    def test_frontend_syntax_and_modern_web_guidance_markers(self):
        js_check = subprocess.run(
            ["node", "--check", str(DOCS_DIR / "app.js")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(js_check.returncode, 0, js_check.stderr)

        html = (DOCS_DIR / "index.html").read_text(encoding="utf-8")
        css = (DOCS_DIR / "styles.css").read_text(encoding="utf-8")
        js = (DOCS_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn('<meta name="color-scheme" content="light dark">', html)
        self.assertIn('role="tablist"', html)
        self.assertIn('hidden="until-found"', html)
        self.assertNotIn("innerHTML", js)
        self.assertIn("'setHTML' in Element.prototype", js.replace('"', "'"))

        self.assertIn("color-scheme: light dark;", css)
        self.assertIn("light-dark(", css)
        self.assertIn("container-type: inline-size;", css)
        self.assertIn("interpolate-size: allow-keywords;", css)
        self.assertIn("@container (width < 600px)", css)

        # Ensure no heading elements inside <summary>
        self.assertIsNone(re.search(r"<summary[^>]*>.*?<h[1-6]\b", html, re.DOTALL | re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
