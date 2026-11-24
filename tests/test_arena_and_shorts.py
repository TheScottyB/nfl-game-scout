import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SCRIPTS_DIR = REPO_ROOT / "scripts"
SHORTS_DIR = REPO_ROOT / "shorts"

sys.path.insert(0, str(SCRIPTS_DIR))
import arena_cli  # noqa: E402
import render_shorts  # noqa: E402


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

    def test_arena_cli_ingest_and_grade_on_temp_copy(self):
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

            count = arena_cli.ingest_predictions("2026-thanksgiving", sample_pred, None, data_dir=tmp_data)
            self.assertEqual(count, 1)
            updated = json.loads((tmp_data / "2026-thanksgiving.json").read_text(encoding="utf-8"))
            claude_pred = next(p for p in updated["games"][0]["predictions"] if p["modelId"] == "claude")
            self.assertEqual(claude_pred["status"], "locked")
            self.assertEqual(claude_pred["winnerAbbr"], "CHI")

            total = arena_cli.grade_prediction(
                slate_id="2026-thanksgiving",
                game_id="bears-at-lions",
                away_score=28,
                home_score=24,
                summary="Bears took a late dome thriller 28-24.",
                freak_outcome="Throwback pass scored in Q3.",
                model_id="claude",
                breakdown={
                    "winner": 20,
                    "scoreMargin": 20,
                    "spreadTotal": 15,
                    "winScriptRef": 14,
                    "orgSentiment": 13,
                    "surpriseBonus": 15,
                },
                notes="Nailed exact score and throwback call.",
                data_dir=tmp_data,
            )
            self.assertEqual(total, 97)
            graded = json.loads((tmp_data / "2026-thanksgiving.json").read_text(encoding="utf-8"))
            self.assertEqual(graded["games"][0]["result"]["winnerAbbr"], "CHI")
            graded_pred = next(p for p in graded["games"][0]["predictions"] if p["modelId"] == "claude")
            self.assertEqual(graded_pred["status"], "graded")
            self.assertEqual(graded_pred["grade"]["total"], 97)

    def test_arena_cli_security_rejections_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_data = Path(tmp) / "data"
            shutil.copytree(DOCS_DIR / "data", tmp_data)
            target_file = tmp_data / "2026-thanksgiving.json"
            original_bytes = target_file.read_bytes()

            # Path traversal rejected
            with self.assertRaises(ValueError):
                arena_cli.build_multi_model_prompt("../slates", data_dir=tmp_data)
            with self.assertRaises(ValueError):
                arena_cli.ingest_predictions("../slates", target_file, None, data_dir=tmp_data)

            # Bad receiptUrl rejected without mutating disk
            bad_url_pred = Path(tmp) / "bad_url.json"
            bad_url_pred.write_text(
                json.dumps(
                    {
                        "gameId": "bears-at-lions",
                        "modelId": "claude",
                        "receiptUrl": "javascript:alert(1)",
                        "winnerAbbr": "CHI",
                        "projectedScore": {"away": 28, "home": 24},
                        "spreadLean": "CHI -2.5",
                        "totalLean": "Over 55.5",
                        "primaryKey": "Key",
                        "refCall": "Ref",
                        "sentimentEdge": "Sent",
                        "hiddenMismatch": "Mismatch",
                        "surpriseCall": {"title": "T", "detail": "D"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                arena_cli.ingest_predictions("2026-thanksgiving", bad_url_pred, None, data_dir=tmp_data)
            self.assertEqual(target_file.read_bytes(), original_bytes)

            # Bad hex color rejected by validator
            bad_slate = json.loads(target_file.read_text(encoding="utf-8"))
            bad_slate["games"][0]["away"]["color"] = "red; background:url(x)"
            errors = arena_cli.validate_slate_dict(bad_slate, "test.json")
            self.assertTrue(any("invalid hex color" in e for e in errors))

    def test_frontend_csp_hash_and_hardening_markers(self):
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

        # Verify CSP SHA-256 hash matches exact inline FOUC-prevention script
        script_match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertIsNotNone(script_match)
        expected_hash = base64.b64encode(hashlib.sha256(script_match.group(1).encode("utf-8")).digest()).decode("ascii")
        csp_match = re.search(r'<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html)
        self.assertIsNotNone(csp_match)
        csp = csp_match.group(1)
        self.assertIn(f"'sha256-{expected_hash}'", csp)
        self.assertIn("require-trusted-types-for 'script'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertNotIn("frame-ancestors", csp)

        self.assertIn('<meta name="referrer" content="strict-origin-when-cross-origin">', html)
        self.assertIn('<meta name="color-scheme" content="light dark">', html)
        self.assertIn('role="tablist"', html)
        self.assertIn('hidden="until-found"', html)
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("DOMParser", js)
        self.assertNotIn(".style.", js)
        self.assertIn("sanitizeHttpsUrl", js)
        self.assertIn("SAFE_SLATE_FILE_RE", js)
        self.assertIn("'setHTML' in Element.prototype", js.replace('"', "'"))

        self.assertIn("color-scheme: light dark;", css)
        self.assertIn("light-dark(", css)
        self.assertIn("container-type: inline-size;", css)
        self.assertIn("interpolate-size: allow-keywords;", css)
        self.assertIn("@container (width < 600px)", css)
        self.assertIn("@supports (animation-timeline: scroll())", css)
        self.assertIn("@supports not (scrollbar-color: auto)", css)
        self.assertIn("@media (forced-colors: active)", css)

        self.assertIsNone(re.search(r"<summary[^>]*>.*?<h[1-6]\b", html, re.DOTALL | re.IGNORECASE))

    def test_shorts_security_and_upload_kit(self):
        # CSS injection color neutralized
        self.assertEqual(render_shorts._safe_hex_color("#AA0000"), "#AA0000")
        self.assertEqual(render_shorts._safe_hex_color("#fff; background:red"), "#10b981")

        # Slug traversal rejected
        self.assertEqual(render_shorts._safe_slug("bears-at-lions"), "bears-at-lions")
        with self.assertRaises(ValueError):
            render_shorts._safe_slug("../evil")

        # Upload kit hygiene & 100-char YouTube title cap
        kit_text = (SHORTS_DIR / "YOUTUBE_UPLOAD_KIT.md").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"/[U]sers/", kit_text))
        titles = re.findall(r"Suggested YouTube Short Title:\*\* `([^`]+)`", kit_text)
        self.assertEqual(len(titles), 4)
        for t in titles:
            self.assertLessEqual(len(t), 100, f"YouTube title exceeds 100 chars ({len(t)}): {t}")


if __name__ == "__main__":
    unittest.main()
