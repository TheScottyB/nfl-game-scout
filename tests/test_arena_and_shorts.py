import base64
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SCRIPTS_DIR = REPO_ROOT / "scripts"
SHORTS_DIR = REPO_ROOT / "shorts"

sys.path.insert(0, str(SCRIPTS_DIR))
import arena_cli  # noqa: E402
import render_shorts  # noqa: E402
import render_studio_short  # noqa: E402
import tts_pronunciation  # noqa: E402


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
            self.assertIn("divisionContext", game)
            self.assertIsInstance(game["divisionContext"]["isDivisional"], bool)
            self.assertGreaterEqual(game["sentiment30d"]["awayScore"], -5)
            self.assertLessEqual(game["sentiment30d"]["awayScore"], 5)
            active = [p for p in game["predictions"] if p["status"] in {"locked", "graded"}]
            self.assertGreaterEqual(len(active), 1)
            self.assertTrue(active[0]["surpriseCall"]["title"])
            self.assertIsNotNone(game.get("result"))
            self.assertEqual(active[0]["status"], "graded")
            self.assertIsNotNone(active[0].get("grade"))

        missed = tg.get("missedGames", [])
        self.assertTrue(any(m.get("id") == "packers-at-rams" for m in missed))

        bears_lions = games[0]["predictions"][0]
        self.assertEqual(bears_lions["grade"]["total"], 43)
        self.assertEqual(bears_lions["grade"]["breakdown"]["winScriptRef"], 13)

        w11 = json.loads((DOCS_DIR / "data" / "2026-week11-archive.json").read_text(encoding="utf-8"))
        snf_pred = w11["games"][0]["predictions"][0]
        self.assertIs(snf_pred["modelMeta"]["excludeFromStandings"], True)

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

            # Bad studioShortUrl rejected at both slate and game level
            bad_short = json.loads(target_file.read_text(encoding="utf-8"))
            bad_short["studioShortUrl"] = "javascript:alert(1)"
            bad_short["games"][0]["studioShortUrl"] = "http://insecure.example/short"
            short_errors = arena_cli.validate_slate_dict(bad_short, "test.json")
            self.assertTrue(any("test.json.studioShortUrl" in e for e in short_errors))
            self.assertTrue(any("bears-at-lions.studioShortUrl" in e for e in short_errors))

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

    def test_studio_short_data_driven_security_and_upload_kit(self):
        # Runtime bounds validation
        render_studio_short.validate_runtime_args(1.07, 3, 20.0)
        with self.assertRaises(ValueError):
            render_studio_short.validate_runtime_args(2.5, 3, 20.0)
        with self.assertRaises(ValueError):
            render_studio_short.validate_runtime_args(0.4, 3, 20.0)
        with self.assertRaises(ValueError):
            render_studio_short.validate_runtime_args(1.0, 0, 20.0)

        # Hex color & slug hardening
        self.assertEqual(render_studio_short._safe_hex_color("#38bdf8"), "#38bdf8")
        self.assertEqual(render_studio_short._safe_hex_color("red;url(x)"), "#22d3ee")
        with self.assertRaises(ValueError):
            render_studio_short._safe_slug("../escape")

        # Thanksgiving flagship cuts load cleanly and obey 100-char YouTube title cap
        tg_cuts = render_studio_short.build_cuts_from_slate(DOCS_DIR / "data" / "2026-thanksgiving.json")
        self.assertEqual(set(tg_cuts.keys()), {"supercut", "bears-at-lions", "eagles-at-cowboys", "chiefs-at-bills"})
        for cut in tg_cuts.values():
            self.assertLessEqual(len(cut.yt_title), 100)
            self.assertEqual(len(cut.scenes), 5)

        # Generic any-slate data-driven builder works on arbitrary future slates
        with tempfile.TemporaryDirectory() as tmp:
            base = json.loads((DOCS_DIR / "data" / "2026-thanksgiving.json").read_text(encoding="utf-8"))
            base["slateId"] = "2026-week13"
            base["week"] = "Week 13"
            synthetic = Path(tmp) / "2026-week13.json"
            synthetic.write_text(json.dumps(base), encoding="utf-8")

            gen_cuts = render_studio_short.build_cuts_from_slate(synthetic)
            self.assertEqual(set(gen_cuts.keys()), {"supercut", "bears-at-lions", "eagles-at-cowboys", "chiefs-at-bills"})
            for cut in gen_cuts.values():
                self.assertLessEqual(len(cut.yt_title), 100)
                self.assertGreaterEqual(len(cut.scenes), 5)

            # HUD HTML escapes script injection safely
            evil_scene = render_studio_short.StudioScene(
                index=1,
                slug="01-test",
                top_badge="<script>alert(1)</script>",
                headline="A<br><b>B</b>",
                call_pill="TEST",
                caption="<img src=x onerror=alert(1)>",
                accent_hex="#22d3ee",
                motion="zoom_in",
                transition_out="fade",
                art_prompt="prompt",
                dialogue=(render_studio_short.DialogueTurn("Alex", "crisp", "hi"),),
            )
            hud_html = render_studio_short.build_hud_html(evil_scene, 5)
            self.assertNotIn("<script>alert(1)</script>", hud_html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", hud_html)
            self.assertNotIn("onerror=alert(1)>", hud_html)

            # Upload kit generation enforces clean relative paths and <=100-char titles
            kit_out = Path(tmp) / "YOUTUBE_STUDIO_UPLOAD_KIT.md"
            render_studio_short.write_studio_upload_kit(list(tg_cuts.values()), kit_out)
            kit_content = kit_out.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"/[U]sers/", kit_content))
            studio_titles = re.findall(r"Suggested YouTube Short Title:\*\* `([^`]+)`", kit_content)
            self.assertEqual(len(studio_titles), 4)
            for t in studio_titles:
                self.assertLessEqual(len(t), 100)

    def test_tts_pronunciation_lexicon_and_hud_preservation(self):
        self.assertEqual(
            tts_pronunciation.apply_tts_pronunciations("With Shawn Hochuli throwing 14 flags"),
            "With Shawn Hock-yoo-lee throwing 14 flags",
        )
        self.assertEqual(
            tts_pronunciation.apply_tts_pronunciations("Hochuli's crew and HOCHULI flags"),
            "Hock-yoo-lee's crew and Hock-yoo-lee flags",
        )
        self.assertEqual(
            tts_pronunciation.apply_tts_pronunciations("Eric Bieniemy, Eddy Piñeiro, and Rome Odunze"),
            "Eric Bee-en-uh-mee, Eddy Pin-yair-oh, and Rome Oh-doon-zay",
        )
        # Word boundaries prevent partial-word corruption
        self.assertEqual(
            tts_pronunciation.apply_tts_pronunciations("Hochulism Pukan"),
            "Hochulism Pukan",
        )
        # Visual HUD preserves exact written spelling ("HOCHULI")
        hud_html = render_studio_short.build_hud_html(render_studio_short.BEARS_LIONS_SCENES[2], 5)
        self.assertIn("HOCHULI FLAGS", hud_html)
        self.assertIn("SHAWN HOCHULI", hud_html)
        self.assertNotIn("Hock-yoo-lee", hud_html)

    def test_black_friday_multi_model_provenance(self):
        bf_path = DOCS_DIR / "data" / "2026-black-friday.json"
        self.assertTrue(bf_path.is_file())
        bf = json.loads(bf_path.read_text(encoding="utf-8"))
        self.assertEqual(bf["slateId"], "2026-black-friday")
        games = bf["games"]
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game["id"], "broncos-at-steelers")
        locked = [p for p in game["predictions"] if p["status"] in {"locked", "graded"}]
        self.assertGreaterEqual(len(locked), 2)
        for pred in locked:
            meta = pred.get("modelMeta")
            self.assertIsInstance(meta, dict)
            self.assertTrue(meta.get("provider"))
            self.assertTrue(meta.get("exactModelId"))
            self.assertTrue(meta.get("harness"))
            self.assertRegex(str(meta.get("promptSha256", "")), r"^[0-9a-f]{64}$")
            self.assertTrue(meta.get("runAtUtc"))
        self.assertEqual(arena_cli.validate_all(), [])

    def test_arena_cli_run_dry_run_and_validation(self):
        res = subprocess.run(
            [
                "python3",
                str(SCRIPTS_DIR / "arena_cli.py"),
                "run",
                "--slate",
                "2026-black-friday",
                "--models",
                "gemini:gemini-3.1-pro-preview,claude-cli:sonnet",
                "--dry-run",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        dry = json.loads(res.stdout)
        self.assertEqual(len(dry), 2)
        self.assertTrue(all(item["dryRun"] for item in dry))
        self.assertEqual(dry[0]["promptSha256"], dry[1]["promptSha256"])

        bf = json.loads((DOCS_DIR / "data" / "2026-black-friday.json").read_text(encoding="utf-8"))
        bf_bad_meta = json.loads(json.dumps(bf))
        if bf_bad_meta["games"][0]["predictions"]:
            bf_bad_meta["games"][0]["predictions"][0]["modelMeta"] = {"provider": "google"}
            errs = arena_cli.validate_slate_dict(bf_bad_meta, "bad-meta.json")
            self.assertTrue(any("exactModelId" in e for e in errs))

            bf_bad_excl = json.loads(json.dumps(bf))
            bf_bad_excl["games"][0]["predictions"][0]["modelMeta"]["excludeFromStandings"] = "yes"
            excl_errs = arena_cli.validate_slate_dict(bf_bad_excl, "bad-excl.json")
            self.assertTrue(any("excludeFromStandings" in e for e in excl_errs))

        bf_bad_missed = json.loads(json.dumps(bf))
        bf_bad_missed["missedGames"] = [{"id": "../evil", "label": "X"}]
        m_errs = arena_cli.validate_slate_dict(bf_bad_missed, "bad-missed.json")
        self.assertTrue(any("missedGames" in e for e in m_errs))

    def test_studio_short_multi_model_faceoff_and_pending_guard(self):
        bf_path = DOCS_DIR / "data" / "2026-black-friday.json"
        bf = json.loads(bf_path.read_text(encoding="utf-8"))
        cuts = render_studio_short.build_cuts_from_slate(bf_path)
        game_cut = cuts["broncos-at-steelers"]
        outro = game_cut.scenes[-1]
        outro_text = " ".join(
            [outro.top_badge, outro.headline, outro.call_pill, outro.caption, *(t.text for t in outro.dialogue)]
        )
        self.assertNotIn("GEMINI VS. CLAUDE", outro_text)
        locked = [p for p in bf["games"][0]["predictions"] if p["status"] in {"locked", "graded"}]
        if len(locked) >= 2:
            self.assertIn(f"{len(locked)}-MODEL ARENA", outro.top_badge)
            for p in locked[:3]:
                short_name = render_studio_short._short_model_name(p)
                self.assertIn(short_name, outro_text)

        # Pending guard catches advertising an un-run pending model in Scene 5
        synthetic = json.loads(json.dumps(bf))
        synthetic["games"][0]["predictions"] = [
            synthetic["games"][0]["predictions"][0]
            if synthetic["games"][0]["predictions"]
            else {
                "modelId": "gemini",
                "modelLabel": "Gemini 3.1 Pro",
                "status": "locked",
                "winnerAbbr": "DEN",
                "projectedScore": {"away": 20, "home": 13},
                "surpriseCall": {"title": "T", "detail": "D", "probability": "~8%"},
            },
            {
                "modelId": "grok",
                "modelLabel": "Grok",
                "status": "pending",
                "winnerAbbr": None,
                "projectedScore": None,
            },
        ]
        bad_cut = render_studio_short.StudioCut(
            cut_id="broncos-at-steelers",
            output_filename="test.mp4",
            asset_subdir="test",
            default_tempo=1.0,
            yt_title="Test #shorts",
            yt_description="Test",
            scenes=(
                *game_cut.scenes[:-1],
                render_studio_short.StudioScene(
                    index=5,
                    slug="05-pick-arena",
                    top_badge="GEMINI VS. GROK",
                    headline="PICK",
                    call_pill="PILL",
                    caption="Gemini vs Grok live",
                    accent_hex="#34d399",
                    motion="zoom_out",
                    transition_out="fade",
                    art_prompt="prompt",
                    dialogue=(render_studio_short.DialogueTurn("Alex", "crisp", "Grok is live"),),
                ),
            ),
        )
        errs = render_studio_short.validate_cut_models(bad_cut, synthetic)
        self.assertTrue(any("grok" in e.lower() for e in errs))


SLATE_ID = "2026-black-friday"


def _fake_pick(**overrides):
    pick = {
        "winnerAbbr": "DEN",
        "projectedScore": {"away": 23, "home": 16},
        "spreadLean": "DEN -4.5",
        "totalLean": "Over 37.5",
        "primaryKey": "key",
        "refCall": "ref",
        "sentimentEdge": "edge",
        "hiddenMismatch": "mismatch",
        "surpriseCall": {"title": "T", "detail": "D", "ingredients": ["a", "b", "c"], "probability": "~4%"},
        "bonusSleeper": {"title": "B", "detail": "D", "probability": "~11%"},
    }
    pick.update(overrides)
    return pick


class TestArenaRun(unittest.TestCase):
    """`arena_cli.py run` against a temp copy of the slate, with the model call faked."""

    def _slate_dir(self, kickoff_utc: str) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="arena-run-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        slate = json.loads((DOCS_DIR / "data" / f"{SLATE_ID}.json").read_text(encoding="utf-8"))
        slate["games"][0]["kickoffUtc"] = kickoff_utc
        (tmp / f"{SLATE_ID}.json").write_text(json.dumps(slate, indent=2) + "\n", encoding="utf-8")
        return tmp

    @staticmethod
    def _saved_predictions(data_dir: Path) -> list:
        slate = json.loads((data_dir / f"{SLATE_ID}.json").read_text(encoding="utf-8"))
        return slate["games"][0]["predictions"]

    def test_run_refuses_a_game_that_has_kicked_off(self):
        data_dir = self._slate_dir("2020-01-01T00:00:00Z")
        slate_file = data_dir / f"{SLATE_ID}.json"
        before = slate_file.read_text(encoding="utf-8")
        calls = []

        def fake(provider, model, prompt, timeout_sec):
            calls.append(model)
            return _fake_pick(), "test-model-a", {}, "test-harness"

        with mock.patch.object(arena_cli, "_invoke_provider", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "Kickoff has passed"):
                arena_cli.run_models_for_slate(SLATE_ID, ["gemini:test-model-a"], data_dir=data_dir)
            self.assertEqual(calls, [], "no model may be called for a game that has started")
            self.assertEqual(slate_file.read_text(encoding="utf-8"), before)

            dry = arena_cli.run_models_for_slate(SLATE_ID, ["gemini:test-model-a"], dry_run=True, data_dir=data_dir)
            self.assertTrue(dry[0]["dryRun"])
            self.assertEqual(calls, [])

            arena_cli.run_models_for_slate(SLATE_ID, ["gemini:test-model-a"], data_dir=data_dir, allow_late=True)

        late = next(p for p in self._saved_predictions(data_dir) if p["modelId"] == "test-model-a")
        self.assertIs(late["modelMeta"]["afterKickoff"], True)

    def test_run_keeps_other_models_when_one_fails(self):
        kickoff = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data_dir = self._slate_dir(kickoff)
        slate_file = data_dir / f"{SLATE_ID}.json"
        ids_before = [p["modelId"] for p in self._saved_predictions(data_dir)]

        def fake(provider, model, prompt, timeout_sec):
            if model == "boom":
                raise RuntimeError("Gemini API HTTP 503 for boom: overloaded")
            if model == "bad-winner":
                return _fake_pick(winnerAbbr="Denver"), "bad-winner", {}, "test-harness"
            return _fake_pick(), "test-model-a", {"inputTokens": 1}, "test-harness"

        with mock.patch.object(arena_cli, "_invoke_provider", side_effect=fake):
            out = arena_cli.run_models_for_slate(
                SLATE_ID,
                ["gemini:test-model-a", "gemini:boom", "gemini:bad-winner"],
                data_dir=data_dir,
            )
            failed = [s for s in out if "error" in s]
            recorded = [s for s in out if "error" not in s]
            self.assertEqual([s["requestedModel"] for s in failed], ["boom", "bad-winner"])
            self.assertIn("503", failed[0]["error"])
            self.assertEqual([s["modelId"] for s in recorded], ["test-model-a"])

            saved = self._saved_predictions(data_dir)
            self.assertEqual([p["modelId"] for p in saved], ids_before + ["test-model-a"])
            self.assertNotIn("afterKickoff", saved[-1]["modelMeta"])

            # Every model failing leaves the slate file untouched.
            after_first_run = slate_file.read_text(encoding="utf-8")
            all_failed = arena_cli.run_models_for_slate(SLATE_ID, ["gemini:boom"], data_dir=data_dir)
            self.assertEqual(len(all_failed), 1)
            self.assertIn("error", all_failed[0])
            self.assertEqual(slate_file.read_text(encoding="utf-8"), after_first_run)

    def test_prompts_show_placeholders_not_sample_values(self):
        slate = json.loads((DOCS_DIR / "data" / f"{SLATE_ID}.json").read_text(encoding="utf-8"))
        game = slate["games"][0]
        hint = arena_cli._prediction_schema_hint(game)
        hint_text = json.dumps(hint)
        # A sample probability, line, total or score gets echoed back as the model's own call.
        self.assertNotRegex(hint_text, r"~\d")
        self.assertNotRegex(hint_text, r"\d+\.\d")
        self.assertTrue(all(isinstance(v, str) for v in hint["projectedScore"].values()))
        self.assertIn(arena_cli.PLACEHOLDER_NOTE, arena_cli.build_game_prediction_prompt(slate, game))

        paste_prompt = arena_cli.build_multi_model_prompt(SLATE_ID)
        self.assertIn(arena_cli.PLACEHOLDER_NOTE, paste_prompt)
        example = paste_prompt.split("```json", 1)[1]
        self.assertNotRegex(example, r"~\d")
        self.assertNotRegex(example, r'"(away|home)": \d')

    def test_post_json_retries_transient_failures_only(self):
        class _Reply:
            def __init__(self, body):
                self._raw = json.dumps(body).encode("utf-8")

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        def http_error(code, text):
            return urllib.error.HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(text.encode("utf-8")))

        def run_with(replies):
            sleeps = []
            queue = list(replies)

            def fake_urlopen(req, timeout):
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

            with mock.patch.object(arena_cli.urllib.request, "urlopen", side_effect=fake_urlopen), mock.patch.object(
                arena_cli.time, "sleep", side_effect=sleeps.append
            ):
                try:
                    return arena_cli._post_json_with_retry("https://example.invalid", {}, {}, 5, "Test API", "m"), sleeps
                except RuntimeError as exc:
                    return exc, sleeps

        body, sleeps = run_with([http_error(503, "overloaded"), http_error(429, "slow down"), _Reply({"ok": True})])
        self.assertEqual(body, {"ok": True})
        self.assertEqual(sleeps, [2, 5])

        err, sleeps = run_with([http_error(429, "Quota exceeded, limit: 0"), _Reply({"ok": True})])
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("HTTP 429", str(err))
        self.assertEqual(sleeps, [], "a zero daily quota is not retried")

        err, sleeps = run_with([http_error(400, "bad request"), _Reply({"ok": True})])
        self.assertIn("HTTP 400", str(err))
        self.assertEqual(sleeps, [])

        err, sleeps = run_with([http_error(503, "down") for _ in range(4)])
        self.assertIn("HTTP 503", str(err))
        self.assertEqual(sleeps, [2, 5, 10])

    def test_claude_usage_names_the_model_that_answered(self):
        key, usage = arena_cli._primary_model_usage(
            {
                "helper-model": {"inputTokens": 400, "outputTokens": 12},
                "answering-model": {"inputTokens": 2, "outputTokens": 926},
            }
        )
        self.assertEqual(key, "answering-model")
        self.assertEqual(usage["outputTokens"], 926)
        self.assertEqual(arena_cli._primary_model_usage(None), (None, {}))
        self.assertEqual(arena_cli._primary_model_usage({}), (None, {}))


if __name__ == "__main__":
    unittest.main()
