#!/usr/bin/env python3
"""Render NotebookLM-style Studio YouTube Shorts (9:16, 1080x1920, 30fps) from any NFL Game Scout slate JSON.

Features:
  - Data-driven from any `docs/data/<slate>.json` (with flagship Thanksgiving 2026 scripts built-in
    and automatic 5-scene dual-host storyboard generation for any future weekly/playoff slate).
  - Bespoke 9:16 editorial artwork plates (cached or generated via `gemini-nano-banana-2.1`).
  - Dual-host sports-desk voiceover via `gemini-3.8-flash-tts` -> `gemini-3.8-flash-lite-tts`
    (optional macOS `say` fallback via `--allow-say-fallback` / `--tts-backend say`).
  - Instrumental primetime broadcast underscore (`lyria-3.5` / `lyria-3-clip-preview` -> 118-BPM synth).
  - Transparent RGBA Chrome HUD overlays (`1080x1920`, Shorts-safe margins, HTML-escaped).
  - Smooth Ken Burns `zoompan`, `xfade` transitions, exact post-`atempo` timing (zero WSOLA drift),
    2-pass linear `loudnorm` (-14 LUFS) + post-master fade-out (no abrupt EOF cutoff).
  - Automated post-render QA gate asserting resolution, FPS, V/A sync, zero voice overlaps,
    tail fade RMS <= -40 dB, and size <= 20 MB.
  - Auto-generates `shorts/YOUTUBE_STUDIO_UPLOAD_KIT.md` (100-char title enforced) and JSON manifest.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import html
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request
import wave

try:
    from tts_pronunciation import apply_tts_pronunciations
except ImportError:
    from scripts.tts_pronunciation import apply_tts_pronunciations

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SLATE = REPO_ROOT / "docs" / "data" / "2026-thanksgiving.json"
OUTPUT_DIR = REPO_ROOT / "shorts" / "output"
ASSETS_ROOT = OUTPUT_DIR / "studio_assets"
SHARED_MUSIC_DIR = ASSETS_ROOT / "00-thanksgiving-2026"
DEFAULT_STUDIO_KIT = REPO_ROOT / "shorts" / "YOUTUBE_STUDIO_UPLOAD_KIT.md"
DEFAULT_MANIFEST = OUTPUT_DIR / "studio_manifest.json"
DEFAULT_CHROME_BIN = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"

SAFE_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SAFE_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
MAX_YT_TITLE_LEN = 100

WIDTH = 1080
HEIGHT = 1920
FPS = 30
XFADE_SEC = 0.30
VOICE_LEAD_SEC = 0.14
VOICE_TAIL_SEC = 0.55  # Gives +0.39s breathing room between scenes (0.55 - 0.30 + 0.14)
FINAL_TAIL_SEC = 1.45  # Clean breath + 0.95s master music fade + 0.15s silence at EOF
MASTER_FADE_OUT_SEC = 0.95
MASTER_SILENCE_TAIL_SEC = 0.18


@dataclass(frozen=True)
class DialogueTurn:
    speaker: str
    style: str
    text: str


@dataclass(frozen=True)
class StudioScene:
    index: int
    slug: str
    top_badge: str
    headline: str
    call_pill: str
    caption: str
    accent_hex: str
    motion: str  # zoom_in | pan_right | zoom_in_high | pan_left | zoom_out
    transition_out: str  # fade | smoothleft | smoothright
    art_prompt: str
    dialogue: tuple[DialogueTurn, ...]


@dataclass(frozen=True)
class StudioCut:
    cut_id: str
    output_filename: str
    asset_subdir: str
    default_tempo: float
    yt_title: str
    yt_description: str
    scenes: tuple[StudioScene, ...]


def _safe_hex_color(value: str, fallback: str = "#22d3ee") -> str:
    if isinstance(value, str) and SAFE_HEX_RE.fullmatch(value.strip()):
        return value.strip()
    return fallback


def _safe_slug(value: str) -> str:
    if not isinstance(value, str) or not SAFE_SLUG_RE.fullmatch(value):
        raise ValueError(f"Unsafe or invalid slug: {value!r} (expected ^[a-z0-9-]+$)")
    return value


def _truncate_yt_title(title: str) -> str:
    clean = " ".join(title.split())
    if len(clean) <= MAX_YT_TITLE_LEN:
        return clean
    suffix = " #shorts"
    if clean.lower().endswith("#shorts"):
        base = clean[: -len("#shorts")].rstrip()
        return base[: MAX_YT_TITLE_LEN - len(suffix) - 1].rstrip() + "…" + suffix
    return clean[: MAX_YT_TITLE_LEN - 1].rstrip() + "…"


def validate_runtime_args(tempo: float | None, tts_workers: int, max_mb: float) -> None:
    if tempo is not None and not (0.5 <= tempo <= 2.0):
        raise ValueError(f"--tempo must be within [0.5, 2.0] for ffmpeg atempo, got {tempo}")
    if not (1 <= tts_workers <= 8):
        raise ValueError(f"--tts-workers must be within [1, 8], got {tts_workers}")
    if max_mb <= 0:
        raise ValueError(f"--max-mb must be > 0, got {max_mb}")


def resolve_chrome_bin(override: Path | None = None) -> Path:
    candidates: list[Path] = []
    if override is not None:
        candidates.append(override)
    env_bin = os.environ.get("CHROME_BIN", "").strip()
    if env_bin:
        candidates.append(Path(env_bin))
    candidates.append(DEFAULT_CHROME_BIN)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError(
        "Headless Chrome/Chromium binary not found. Set CHROME_BIN or pass --chrome-bin."
    )


SUPERCUT_SCENES: tuple[StudioScene, ...] = (
    StudioScene(
        index=1,
        slug="01-cold-open",
        top_badge="WEEK 11 RECEIPTS",
        headline="71-YD FREE KICK<br>+ BLACKOUT NIGHT",
        call_pill="LOCKED BEFORE KICKOFF",
        caption="Called a 70-yd free kick (1 yd short). Then MNF went dark on \"Blackout Night.\"",
        accent_hex="#22d3ee",
        motion="zoom_in",
        transition_out="smoothleft",
        art_prompt=(
            "Dramatic vertical 9:16 split-composition sports magazine editorial illustration. "
            "Top half: a pro football soaring high through thin crisp night air in Mexico City "
            "toward glowing golden stadium goalposts, barely one yard short of the crossbar. "
            "Bottom half: an American football stadium plunging into dramatic blackout darkness "
            "with flickering floodlights and silhouetted crowd. No text, no logos, no watermarks."
        ),
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="energetic sports anchor, fast-paced viral opening hook",
                text=(
                    "Sunday in Mexico City, our AI called a seventy-yard free kick—"
                    "and a seventy-one-yarder missed by one yard!"
                ),
            ),
            DialogueTurn(
                speaker="Maya",
                style="amused, sharp, punchy sports analyst",
                text=(
                    "Then Monday night, Washington billed a Blackout game—and the stadium "
                    "lights literally died! So here are three Thanksgiving freak plays locked "
                    "on GitHub before kickoff."
                ),
            ),
        ),
    ),
    StudioScene(
        index=2,
        slug="02-chi-at-det",
        top_badge="1:00 PM ET • CBS • CHI @ DET",
        headline="ELIGIBLE-TACKLE<br>TRICK-PLAY WAR",
        call_pill="~14% FREAK CALL • PICK: DET 30–27",
        caption="Ben Johnson returns to Detroit. Expect non-QB trick plays + Hochuli reporting chaos.",
        accent_hex="#38bdf8",
        motion="pan_right",
        transition_out="smoothleft",
        art_prompt=(
            "Dramatic vertical 9:16 sports editorial illustration inside a Honolulu-blue domed "
            "football stadium: massive lineman raising hand eligible beside a striped referee "
            "holding a glowing yellow flag, two rival coaches facing off. No text, no logos."
        ),
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="excited sports anchor, crisp pacing",
                text=(
                    "Game one: eight-and-two Bears at eight-and-two Lions. "
                    "Ben Johnson comes back to Detroit against Dan Campbell."
                ),
            ),
            DialogueTurn(
                speaker="Maya",
                style="sharp, knowing sports analyst, brisk",
                text=(
                    "With Shawn Hochuli throwing fourteen flags a game, expect an eligible-tackle "
                    "trick-play war—and total reporting-flag chaos. Lions, thirty to twenty-seven."
                ),
            ),
        ),
    ),
    StudioScene(
        index=3,
        slug="03-phi-at-dal",
        top_badge="4:30 PM ET • FOX • PHI @ DAL",
        headline="SUN GLARE +<br>68-YD RECORD THREAT",
        call_pill="~9–12% FREAK CALL • PICK: DAL 31–20",
        caption="Blinding 3:30 PM west-wall sun muffs a punt—setting up a 66–69 yd Aubrey kick.",
        accent_hex="#f59e0b",
        motion="zoom_in_high",
        transition_out="smoothright",
        art_prompt=(
            "Dramatic vertical 9:16 sports editorial illustration inside a glass-walled Texas "
            "football dome at golden hour: blazing amber sunbeams blinding a punt returner while "
            "a navy-and-silver kicker winds up deep midfield. No text, no logos."
        ),
        dialogue=(
            DialogueTurn(
                speaker="Maya",
                style="dramatic sports storyteller, high energy",
                text=(
                    "Four-thirty in Dallas—and the real defender is that blinding three-thirty "
                    "west-wall sun slicing straight through the stadium glass."
                ),
            ),
            DialogueTurn(
                speaker="Alex",
                style="hyped sports anchor, enthusiastic",
                text=(
                    "One sun-glare punt muff sets up Brandon Aubrey lining up from sixty-eight yards "
                    "for the NFL record. Cowboys roll, thirty-one to twenty."
                ),
            ),
        ),
    ),
    StudioScene(
        index=4,
        slug="04-kc-at-buf",
        top_badge="8:20 PM ET • NBC • KC @ BUF",
        headline="REVERSE<br>13 SECONDS",
        call_pill="~10% FREAK CALL • PICK: BUF 27–24",
        caption="Freezing Buffalo air + Alex Kemp play-clock chaos + 55+ yd cold-weather kick drama.",
        accent_hex="#f43f5e",
        motion="pan_left",
        transition_out="fade",
        art_prompt=(
            "Moody vertical 9:16 sports editorial illustration in a freezing snow-dusted Buffalo "
            "night stadium: kicker blasting a long field goal through snow toward yellow uprights "
            "with crimson wind ribbons. No text, no logos."
        ),
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="intense primetime sports anchor",
                text=(
                    "Primetime in Buffalo: eight-and-two Chiefs, nine-and-one Bills, "
                    "freezing air, and ghosts of Thirteen Seconds."
                ),
            ),
            DialogueTurn(
                speaker="Maya",
                style="dramatic, punchy sports analyst",
                text=(
                    "Alex Kemp's crew sparks late play-clock chaos, setting up a brutal "
                    "fifty-five-yard cold-weather kick to flip the script—Reverse Thirteen Seconds. "
                    "Bills, twenty-seven to twenty-four."
                ),
            ),
        ),
    ),
    StudioScene(
        index=5,
        slug="05-outro-arena",
        top_badge="GEMINI VS. CLAUDE • 100-PT ARENA",
        headline="RECEIPTS LOCKED<br>BEFORE KICKOFF",
        call_pill="thescottyb.github.io/nfl-game-scout",
        caption="Timestamped on GitHub before kickoff. Which Thanksgiving freak play hits?",
        accent_hex="#34d399",
        motion="zoom_out",
        transition_out="fade",
        art_prompt=(
            "Sleek futuristic vertical 9:16 editorial sports-tech illustration of a glowing "
            "holographic football field flanked by emerald-cyan and amber-gold AI neural cores "
            "in a dark studio. No text, no logos."
        ),
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="fast, punchy sports anchor sign-off",
                text=(
                    "All three calls are timestamped on GitHub before kickoff—"
                    "Gemini versus Claude graded out of a hundred tonight."
                ),
            ),
            DialogueTurn(
                speaker="Maya",
                style="warm, energetic call-to-action",
                text="Link's in the description—which freak play hits Thursday?",
            ),
        ),
    ),
)

BEARS_LIONS_SCENES: tuple[StudioScene, ...] = (
    StudioScene(
        index=1,
        slug="01-hook",
        top_badge="1:00 PM ET • CBS • CHI @ DET",
        headline="THE ARCHITECT<br>RETURNS HOME",
        call_pill="8–2 VS. 8–2 • NFC NORTH CROWN",
        caption="Ex-Lions Ben Johnson, Randle El & J.T. Barrett walk back into Ford Field tied 8–2.",
        accent_hex="#38bdf8",
        motion="zoom_in",
        transition_out="smoothleft",
        art_prompt="Vertical 9:16 editorial sports illustration: visiting head coach emerging into roaring blue dome. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="energetic sports anchor, punchy kickoff hook",
                text="Thanksgiving opener at Ford Field: eight-and-two Bears at eight-and-two Lions for the NFC North crown!",
            ),
            DialogueTurn(
                speaker="Maya",
                style="sharp, delighted sports analyst",
                text="And the script writes itself—former Lions mastermind Ben Johnson walks back into Detroit against Dan Campbell.",
            ),
        ),
    ),
    StudioScene(
        index=2,
        slug="02-trenches",
        top_badge="HIDDEN COACHING MISMATCH",
        headline="TRENCHES &<br>ELIGIBLE TRICKS",
        call_pill="DET OL HANK FRALEY VS. CHI DL",
        caption="Fraley keeps Goff (20 TDs, 4 INTs) clean indoors—plus ex-tackle Dan Skipper now coaches DET.",
        accent_hex="#fb923c",
        motion="pan_right",
        transition_out="smoothleft",
        art_prompt="Vertical 9:16 editorial sports illustration: blue offensive linemen wall blocking dark orange defenders. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="crisp sports analyst, brisk",
                text="The hidden mismatch sits below the coordinators: Lions O-line coach Hank Fraley against Chicago's defensive front.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="knowing, upbeat sports analyst",
                text="Keep Jared Goff clean indoors and he shreds—plus Detroit literally put eligible-tackle legend Dan Skipper on its coaching staff!",
            ),
        ),
    ),
    StudioScene(
        index=3,
        slug="03-refs",
        top_badge="REF CREW & 30-DAY SENTIMENT",
        headline="HOCHULI FLAGS<br>IN A 55.5 SHOOTOUT",
        call_pill="SHAWN HOCHULI • 14+ FLAGS/GM",
        caption="Noisy dome + strict pre-snap whistles vs. euphoric locker rooms (CHI +4.0, DET +4.5).",
        accent_hex="#facc15",
        motion="zoom_in_high",
        transition_out="smoothright",
        art_prompt="Vertical 9:16 editorial sports illustration: striped ref tossing yellow flag under dome crowd shockwaves. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="intense sports anchor, crisp",
                text="Here's the landmine: referee Shawn Hochuli averages up to fourteen-and-a-half flags a game—especially false starts and illegal formations.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="dramatic sports analyst",
                text="In a deafening Ford Field shootout with a fifty-five-and-a-half total, one sloppy trick set blows up a red-zone drive.",
            ),
        ),
    ),
    StudioScene(
        index=4,
        slug="04-freak-play",
        top_badge="~14% CRAZY SURPRISE CALL",
        headline="ELIGIBLE-TACKLE<br>TRICK-PLAY WAR",
        call_pill="NON-QB TD OR REPORTING CHAOS",
        caption="Ben Johnson & Randle El vs. Campbell & Skipper—plus twin 55+ yd dome kicks late.",
        accent_hex="#38bdf8",
        motion="pan_left",
        transition_out="fade",
        art_prompt="Vertical 9:16 editorial sports illustration: lineman signaling eligible beside striped referee with glowing flag. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="hyped sports anchor",
                text="So our locked freak call at fourteen percent: an eligible-tackle or non-QB trick-play touchdown war—or Hochuli wiping one out on a reporting flag!",
            ),
            DialogueTurn(
                speaker="Maya",
                style="energetic analyst, quick tag",
                text="Bonus sleeper: Cairo Santos and Jake Bates trading fifty-five-yard dome bombs in the final four minutes.",
            ),
        ),
    ),
    StudioScene(
        index=5,
        slug="05-pick-arena",
        top_badge="GEMINI VS. CLAUDE • 100-PT ARENA",
        headline="PICK: LIONS<br>30 – 27",
        call_pill="LIONS +2.5 • OVER 55.5",
        caption="Timestamped on GitHub before kickoff. Does Ben Johnson steal one in Detroit?",
        accent_hex="#34d399",
        motion="zoom_out",
        transition_out="fade",
        art_prompt="Vertical 9:16 futuristic AI arena holographic football field. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="fast, punchy sports anchor sign-off",
                text="Locked pick before kickoff: Lions thirty, Bears twenty-seven—Detroit plus two-and-a-half and the over.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="warm, energetic call-to-action",
                text="Gemini versus Claude gets graded out of a hundred tonight—link's in the description, drop your score below!",
            ),
        ),
    ),
)

EAGLES_COWBOYS_SCENES: tuple[StudioScene, ...] = (
    StudioScene(
        index=1,
        slug="01-hook",
        top_badge="4:30 PM ET • FOX • PHI @ DAL",
        headline="DAK'S SURGE VS.<br>PHILLY GRIT",
        call_pill="PHI 3–7 AT DAL 5–6 • DAL -8.5",
        caption="Tanner McKee fills in for IR-bound Jalen Hurts vs. Dak Prescott fresh off a 5-TD win.",
        accent_hex="#f59e0b",
        motion="zoom_in",
        transition_out="smoothleft",
        art_prompt="Vertical 9:16 editorial sports illustration: green underdog QB facing navy-silver QB in sunlit Texas dome. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="energetic sports anchor, strong hook",
                text="Four-thirty in Dallas: backup quarterback Tanner McKee leads the gritty three-and-seven Eagles into Jerry World.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="dramatic, punchy sports analyst",
                text="Against Dak Prescott fresh off a five-touchdown, fifty-one-point explosion—and a Cowboys defense that just swapped play-callers!",
            ),
        ),
    ),
    StudioScene(
        index=2,
        slug="02-mismatch",
        top_badge="HIDDEN COACHING MISMATCH",
        headline="LAMB & PICKENS<br>VS. HURT CORNERS",
        call_pill="KEN DORSEY + ADAMS VS. PHI CBS",
        caption="Dallas handed calls to Mike Pettine while Fangio protégé Christian Parker knows Philly's checks.",
        accent_hex="#38bdf8",
        motion="pan_right",
        transition_out="smoothleft",
        art_prompt="Vertical 9:16 editorial sports illustration: navy receiver leaping over green DB with two rival coordinator silhouettes. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="sharp sports analyst",
                text="Dallas handed defensive play-calling to Mike Pettine, while ex-Eagles assistant Christian Parker knows Vic Fangio's checks cold.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="emphatic, crisp sports analyst",
                text="Even bigger: Ken Dorsey and Junior Adams unleashing CeeDee Lamb and George Pickens on a banged-up Eagles corner room.",
            ),
        ),
    ),
    StudioScene(
        index=3,
        slug="03-refs",
        top_badge="REF CREW & 30-DAY SENTIMENT",
        headline="ADRIAN HILL<br>DPI MINEFIELD",
        call_pill="13+ FLAGS/GM • PHI -2.0 VS. DAL +1.5",
        caption="Top-tier DPI crew forces hurting Philly CBs (Mitchell & Jones) to risk free Dallas yards.",
        accent_hex="#facc15",
        motion="zoom_in_high",
        transition_out="smoothright",
        art_prompt="Vertical 9:16 editorial sports illustration: striped ref tossing yellow flag as receiver and DB battle downfield. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="crisp sports anchor",
                text="Referee Adrian Hill throws one of the highest defensive pass interference rates in the entire league.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="sharp, knowing sports analyst",
                text="With Quinyon Mitchell and Jonathan Jones hurting, Philly's secondary either gives up deep explosives—or hands Dallas free red-zone flags.",
            ),
        ),
    ),
    StudioScene(
        index=4,
        slug="04-freak-play",
        top_badge="~9–12% CRAZY SURPRISE CALL",
        headline="SUN GLARE +<br>68-YD RECORD THREAT",
        call_pill="3:30 PM WEST GLASS • AUBREY 66–69 YDS",
        caption="Blinding west-wall sun muffs a return—teeing up Brandon Aubrey chasing the 68-yd NFL record.",
        accent_hex="#f59e0b",
        motion="pan_left",
        transition_out="fade",
        art_prompt="Vertical 9:16 editorial sports illustration: blazing amber sunbeams blinding returner while kicker winds up. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Maya",
                style="dramatic storyteller, high energy",
                text="Our Thanksgiving freak call: three-thirty west-wall sun blasting through the stadium glass triggers a muffed punt or drop!",
            ),
            DialogueTurn(
                speaker="Alex",
                style="hyped sports anchor",
                text="And zero wind tees up Brandon Aubrey—already good from sixty-five—to attempt a sixty-six to sixty-nine yard NFL-record kick!",
            ),
        ),
    ),
    StudioScene(
        index=5,
        slug="05-pick-arena",
        top_badge="GEMINI VS. CLAUDE • 100-PT ARENA",
        headline="PICK: COWBOYS<br>31 – 20",
        call_pill="COWBOYS -8.5 • OVER 47.5",
        caption="Timestamped on GitHub before kickoff. Does Brandon Aubrey break the NFL record?",
        accent_hex="#34d399",
        motion="zoom_out",
        transition_out="fade",
        art_prompt="Vertical 9:16 futuristic AI arena holographic football field. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="fast, punchy sports anchor sign-off",
                text="Locked pick: Cowboys thirty-one, Eagles twenty—Dallas minus eight-and-a-half, though watch for a sneaky Vic Fangio defensive score.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="warm, energetic call-to-action",
                text="Check the timestamped Gemini versus Claude receipts in the description—does Aubrey smash the record?",
            ),
        ),
    ),
)

CHIEFS_BILLS_SCENES: tuple[StudioScene, ...] = (
    StudioScene(
        index=1,
        slug="01-hook",
        top_badge="8:20 PM ET • NBC • KC @ BUF",
        headline="AFC #1 SEED<br>IN THE FREEZE",
        call_pill="KC 8–2 AT BUF 9–1 • BUF -2.5",
        caption="Mahomes & Andy Reid invade first-year HC Joe Brady & MVP-pace Josh Allen in 36°F air.",
        accent_hex="#f43f5e",
        motion="zoom_in",
        transition_out="smoothleft",
        art_prompt="Vertical 9:16 editorial sports illustration: red QB vs royal-blue QB in snowy misty Buffalo night stadium. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="intense primetime sports anchor",
                text="Thanksgiving primetime in freezing Orchard Park: eight-and-two Chiefs at nine-and-one Bills for the AFC one-seed tiebreaker!",
            ),
            DialogueTurn(
                speaker="Maya",
                style="dramatic, energized sports analyst",
                text="Patrick Mahomes and Andy Reid walking into thirty-six-degree drizzle against Josh Allen and first-year head coach Joe Brady.",
            ),
        ),
    ),
    StudioScene(
        index=2,
        slug="02-run",
        top_badge="HIDDEN COACHING MISMATCH",
        headline="KC'S REINVENTED<br>GROUND ATTACK",
        call_pill="BIENIEMY + DEMARCO MURRAY + WALKER III",
        caption="KC's 3rd-youngest roster (25.7 avg) hammers Kenneth Walker III into new Bills DC Jim Leonhard.",
        accent_hex="#ef4444",
        motion="pan_right",
        transition_out="smoothleft",
        art_prompt="Vertical 9:16 editorial sports illustration: crimson RB exploding downhill through snow spray past royal-blue defenders. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="sharp sports analyst",
                text="Don't sleep on Kansas City's reinvention: Eric Bieniemy is back, DeMarco Murray coaches running backs, and Kenneth Walker the Third is rolling.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="punchy, insightful sports analyst",
                text="Plus KC owns the third-youngest roster in football—fresh legs pounding downhill into new Bills defensive coordinator Jim Leonhard.",
            ),
        ),
    ),
    StudioScene(
        index=3,
        slug="03-clock",
        top_badge="REF CREW & COLD PHYSICS",
        headline="PLAY-CLOCK TRAP<br>& HEAVY AIR",
        call_pill="ALEX KEMP • LEAGUE-HIGH DELAY FLAGS",
        caption="Kemp lets DBs play physical downfield while policing the 40-second clock in deafening Highmark.",
        accent_hex="#38bdf8",
        motion="zoom_in_high",
        transition_out="smoothright",
        art_prompt="Vertical 9:16 editorial sports illustration: striped ref blowing whistle in frosty cold air tossing yellow flag. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="crisp sports anchor",
                text="Watch referee Alex Kemp: he lets corners hand-fight downfield, helping Buffalo—but he leads the NFL in delay-of-game flags.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="dramatic sports analyst",
                text="Add deafening Bills Mafia noise plus cold damp air shaving three yards off sixty-yard kickers Harrison Butker and Tyler Bass.",
            ),
        ),
    ),
    StudioScene(
        index=4,
        slug="04-freak-play",
        top_badge="~10% CRAZY SURPRISE CALL",
        headline="REVERSE<br>13 SECONDS",
        call_pill="CLOCK CHAOS + 55+ YD COLD KICK",
        caption="Late Kemp clock flag stretches the winner into a 55+ yd heavy-air kick—or KC red-zone pitch TD.",
        accent_hex="#f43f5e",
        motion="pan_left",
        transition_out="fade",
        art_prompt="Vertical 9:16 editorial sports illustration: snow-spraying kicker driving long FG toward crimson-ribbon goalposts. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="hyped primetime sports anchor",
                text="Our locked freak call at ten percent: late play-clock chaos forces a brutal fifty-five-plus-yard cold-weather kick to decide it—Reverse Thirteen Seconds!",
            ),
            DialogueTurn(
                speaker="Maya",
                style="knowing, upbeat sports analyst",
                text="And watch our sleeper: Andy Reid dialing up a Kenneth Walker red-zone pitch-pass trick touchdown.",
            ),
        ),
    ),
    StudioScene(
        index=5,
        slug="05-pick-arena",
        top_badge="GEMINI VS. CLAUDE • 100-PT ARENA",
        headline="PICK: BILLS<br>27 – 24",
        call_pill="BILLS -2.5 • OVER 50.5",
        caption="Timestamped on GitHub before kickoff. Who takes the AFC #1 seed Thursday night?",
        accent_hex="#34d399",
        motion="zoom_out",
        transition_out="fade",
        art_prompt="Vertical 9:16 futuristic AI arena holographic football field. No text.",
        dialogue=(
            DialogueTurn(
                speaker="Alex",
                style="fast, punchy sports anchor sign-off",
                text="Locked pick: Bills twenty-seven, Chiefs twenty-four—Josh Allen answers KC's ground game in the cold.",
            ),
            DialogueTurn(
                speaker="Maya",
                style="warm, energetic call-to-action",
                text="All receipts are live in the Gemini versus Claude Arena—link's in the description, who owns the AFC?",
            ),
        ),
    ),
)

THANKSGIVING_2026_CUTS: dict[str, StudioCut] = {
    "supercut": StudioCut(
        cut_id="supercut",
        output_filename="00-thanksgiving-2026-all-3-freak-plays-studio.mp4",
        asset_subdir="00-thanksgiving-2026",
        default_tempo=1.06,
        yt_title="71-Yd Free Kick → MNF Blackout → 3 NFL Thanksgiving Freak Plays! #shorts",
        yt_description=(
            "Sunday in Mexico City we called a 70-yard free kick right before a 71-yarder missed by 1 yard. "
            "Monday night, Washington billed a \"Blackout\" game and the stadium lights literally died.\n"
            "Here are 3 Thanksgiving Day freak-play calls timestamped on GitHub before kickoff (Studio Edition):\n"
            "1) CHI @ DET: Eligible-Tackle / Non-QB Trick-Play War + Shawn Hochuli Reporting Chaos (~14%)\n"
            "2) PHI @ DAL: 3:30 PM West-Wall Sun-Glare Muff + Brandon Aubrey 66-69 Yd NFL-Record Threat (~9-12%)\n"
            "3) KC @ BUF: Alex Kemp Play-Clock Chaos + Cold-Weather 55+ Yd Kick Drama (\"Reverse 13 Seconds\", ~10%)\n\n"
            "Full Multi-Model Arena (Gemini vs. Claude) & 100-pt receipts:\n"
            "https://thescottyb.github.io/nfl-game-scout/\n\n"
            "#NFL #Thanksgiving #NFLPredictions #AI #SportsAnalytics #Shorts"
        ),
        scenes=SUPERCUT_SCENES,
    ),
    "bears-at-lions": StudioCut(
        cut_id="bears-at-lions",
        output_filename="01-bears-at-lions-thanksgiving-2026-studio.mp4",
        asset_subdir="01-bears-at-lions",
        default_tempo=1.07,
        yt_title="CHI vs DET Studio Scout: Eligible-Tackle Trick-Play War (~14%)! #shorts",
        yt_description=(
            "Ben Johnson, Antwaan Randle El & J.T. Barrett walk back into Ford Field tied 8-2 atop the NFC North "
            "against Dan Campbell & newly minted Lions offensive assistant Dan Skipper.\n"
            "Pre-kickoff AI Studio Scout for Chicago Bears (8-2) at Detroit Lions (8-2):\n"
            "- Pick: DET (CHI 27-DET 30) | Lions +2.5 | Over 55.5\n"
            "- Hidden Mismatch: DET OL Coach Hank Fraley vs. CHI DL Jeremy Garrett & Bill Johnson\n"
            "- Ref Crew: Shawn Hochuli (13.2-14.5 accepted penalties/gm)\n"
            "- Crazy Surprise Call (~14%): Eligible-Tackle / Non-QB Trick-Play War + Hochuli Reporting Flag Chaos\n\n"
            "Verify the pre-kickoff timestamp & watch Gemini vs. Claude get graded out of 100 tonight:\n"
            "https://thescottyb.github.io/nfl-game-scout/\n\n"
            "#NFL #ThanksgivingFootball #CHIvsDET #SportsAnalytics #AI #Shorts"
        ),
        scenes=BEARS_LIONS_SCENES,
    ),
    "eagles-at-cowboys": StudioCut(
        cut_id="eagles-at-cowboys",
        output_filename="02-eagles-at-cowboys-thanksgiving-2026-studio.mp4",
        asset_subdir="02-eagles-at-cowboys",
        default_tempo=1.07,
        yt_title="PHI vs DAL Studio Scout: Sun-Glare Muff + 68-Yd Record Kick! #shorts",
        yt_description=(
            "Backup QB Tanner McKee leads the gritty Eagles (3-7) into AT&T Stadium against Dak Prescott fresh "
            "off a 5-TD, 51-point outburst—while Cowboys defense hands play-calling to Mike Pettine alongside "
            "Vic Fangio protégé Christian Parker.\n"
            "Pre-kickoff AI Studio Scout for Philadelphia Eagles (3-7) at Dallas Cowboys (5-6):\n"
            "- Pick: DAL (PHI 20-DAL 31) | Cowboys -8.5 | Over 47.5\n"
            "- Hidden Mismatch: DAL Ken Dorsey & Junior Adams (Lamb/Pickens) vs. injured PHI CBs\n"
            "- Ref Crew: Adrian Hill (top-tier DPI & offensive holding)\n"
            "- Crazy Surprise Call (~9-12%): 3:30 PM West-Wall Sun-Glare Turnover + Brandon Aubrey 66-69 Yd NFL-Record-Threat FG\n\n"
            "Verify the pre-kickoff timestamp & watch Gemini vs. Claude get graded out of 100 tonight:\n"
            "https://thescottyb.github.io/nfl-game-scout/\n\n"
            "#NFL #ThanksgivingFootball #PHIvsDAL #SportsAnalytics #AI #Shorts"
        ),
        scenes=EAGLES_COWBOYS_SCENES,
    ),
    "chiefs-at-bills": StudioCut(
        cut_id="chiefs-at-bills",
        output_filename="03-chiefs-at-bills-thanksgiving-2026-studio.mp4",
        asset_subdir="03-chiefs-at-bills",
        default_tempo=1.07,
        yt_title="KC vs BUF Studio Scout: Reverse 13 Seconds in the Freeze (~10%)! #shorts",
        yt_description=(
            "AFC #1-seed tiebreaker in freezing 36°F Orchard Park: Patrick Mahomes, Andy Reid & returning OC "
            "Eric Bieniemy (+ RB Coach DeMarco Murray & Kenneth Walker III) invade Josh Allen & first-year HC Joe Brady's house.\n"
            "Pre-kickoff AI Studio Scout for Kansas City Chiefs (8-2) at Buffalo Bills (9-1):\n"
            "- Pick: BUF (KC 24-BUF 27) | Bills -2.5 | Over 50.5\n"
            "- Hidden Mismatch: KC's #3-youngest roster (25.7 avg) & Kenneth Walker III vs. new BUF DC Jim Leonhard\n"
            "- Ref Crew: Alex Kemp (league-high Delay of Game hotspot; lets DBs play physical downfield)\n"
            "- Crazy Surprise Call (~10%): Alex Kemp Play-Clock Chaos + Cold-Weather 55+ Yard Kick Drama (Reverse 13 Seconds)\n\n"
            "Verify the pre-kickoff timestamp & watch Gemini vs. Claude get graded out of 100 tonight:\n"
            "https://thescottyb.github.io/nfl-game-scout/\n\n"
            "#NFL #ThanksgivingFootball #KCvsBUF #SportsAnalytics #AI #Shorts"
        ),
        scenes=CHIEFS_BILLS_SCENES,
    ),
}

# Preserve top-level CUTS alias for backwards compatibility
CUTS = THANKSGIVING_2026_CUTS


def _active_predictions(game: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in game.get("predictions", []) if p.get("status") in {"locked", "graded"}]


def _primary_locked_pred(game: dict[str, Any]) -> dict[str, Any]:
    active = _active_predictions(game)
    if active:
        return active[0]
    preds = game.get("predictions", [])
    return preds[0] if preds else {}


def _short_model_name(pred: dict[str, Any]) -> str:
    label = str(pred.get("modelLabel") or pred.get("modelMeta", {}).get("exactModelId") or pred.get("modelId") or "Model")
    return label.split(" (", 1)[0].strip()


def _build_arena_faceoff_copy(active: list[dict[str, Any]], away_abbr: str, home_abbr: str) -> tuple[str, str, str, str, str]:
    """Return (top_badge, headline, call_pill, caption, dialogue_summary) for Scene 5."""
    if not active:
        return (
            "100-PT ARENA • PENDING",
            "AWAITING<br>MODEL PICKS",
            "NO LOCKED PREDICTIONS YET",
            "Run arena_cli.py run before kickoff to lock models.",
            "No locked model picks are recorded yet.",
        )

    if len(active) == 1:
        only = active[0]
        name = _short_model_name(only)
        proj = only.get("projectedScore") or {"away": 0, "home": 0}
        winner = only.get("winnerAbbr") or home_abbr
        spread_total = f"{only.get('spreadLean', winner)} • {only.get('totalLean', '')}".strip(" •")
        return (
            f"{name.upper()} • 100-PT ARENA",
            f"PICK: {winner}<br>{proj['away']} – {proj['home']}",
            spread_total,
            f"Locked by {name} on GitHub before kickoff. Graded out of 100 after final.",
            f"Locked pick from {name}: {winner}, {away_abbr} {proj['away']} to {home_abbr} {proj['home']}.",
        )

    winners = [str(p.get("winnerAbbr") or "") for p in active]
    unique_winners = {w for w in winners if w}
    lead = active[0]
    lead_proj = lead.get("projectedScore") or {"away": 0, "home": 0}
    if len(unique_winners) == 1:
        winner = next(iter(unique_winners))
        headline = f"CONSENSUS: {winner}<br>{len(active)} MODELS LOCKED"
    else:
        counts = {w: winners.count(w) for w in unique_winners}
        top_w = max(counts, key=lambda w: counts[w])
        headline = f"MODELS SPLIT!<br>EDGE: {top_w} ({counts[top_w]}/{len(active)})"

    pill_parts = [f"{_short_model_name(p).upper()}: {p.get('winnerAbbr', '?')}" for p in active[:3]]
    score_parts = []
    spoken_parts = []
    for p in active[:3]:
        name = _short_model_name(p)
        proj = p.get("projectedScore") or {"away": 0, "home": 0}
        w = p.get("winnerAbbr") or home_abbr
        score_parts.append(f"{name}: {w} ({proj['away']}–{proj['home']})")
        spoken_parts.append(f"{name} takes {w}, {proj['away']} to {proj['home']}")

    top_badge = f"{len(active)}-MODEL ARENA • 100-PT RECEIPTS"
    call_pill = " • ".join(pill_parts)[:46]
    caption = " | ".join(score_parts)[:115]
    dialogue = ". ".join(spoken_parts) + "."
    return top_badge, headline, call_pill, caption, dialogue


def validate_cut_models(cut: StudioCut, slate: dict[str, Any]) -> list[str]:
    """Ensure pending models are never advertised as active participants in Scene 5."""
    errors: list[str] = []
    games = slate.get("games", [])
    target_game = next((g for g in games if g.get("id") == cut.cut_id), None)
    check_games = [target_game] if target_game is not None else games

    pending_terms: set[str] = set()
    active_terms: set[str] = set()
    for g in check_games:
        for p in g.get("predictions", []):
            terms = {
                str(p.get("modelId", "")).lower(),
                _short_model_name(p).lower(),
            }
            terms.discard("")
            if p.get("status") == "pending":
                pending_terms.update(terms)
            elif p.get("status") in {"locked", "graded"}:
                active_terms.update(terms)

    unresolved_pending = pending_terms - active_terms
    if not unresolved_pending or not cut.scenes:
        return errors

    outro = cut.scenes[-1]
    outro_text = " ".join(
        [outro.top_badge, outro.headline, outro.call_pill, outro.caption, *(t.text for t in outro.dialogue)]
    ).lower()
    for term in sorted(unresolved_pending):
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", outro_text):
            errors.append(
                f"Cut {cut.cut_id!r} references pending model {term!r} in Scene {outro.index} while status is 'pending'"
            )
    return errors


def _build_generic_game_cut(slate_id: str, idx: int, game: dict[str, Any]) -> StudioCut:
    gid = _safe_slug(str(game["id"]))
    away = game["away"]
    home = game["home"]
    active_preds = _active_predictions(game)
    pred = _primary_locked_pred(game)
    sc = pred.get("surpriseCall") or {}
    bs = pred.get("bonusSleeper") or {}
    proj = pred.get("projectedScore") or {"away": 0, "home": 0}
    winner = pred.get("winnerAbbr") or home["abbr"]
    prob = sc.get("probability") or "~10%"
    sc_title = sc.get("title") or "Late Special-Teams Chaos"
    sc_detail = sc.get("detail") or "Watch for rare-rule chaos in the fourth quarter."
    mismatch = game.get("orgChart", {}).get("hiddenMismatch") or pred.get("hiddenMismatch") or "Trench coaching battle"
    ref_name = game.get("referee", {}).get("headRef", "Assigned Crew")
    ref_flags = game.get("referee", {}).get("flagsPerGame", "NFL average")
    ref_impact = game.get("referee", {}).get("tonightImpact", "Watch high-leverage late flags.")
    sent = game.get("sentiment30d", {})
    away_s = sent.get("awayScore", 0)
    home_s = sent.get("homeScore", 0)
    accent_a = _safe_hex_color(home.get("color", ""), "#38bdf8")
    accent_b = _safe_hex_color(away.get("color", ""), "#f59e0b")

    faceoff_badge, faceoff_headline, faceoff_pill, faceoff_caption, faceoff_spoken = _build_arena_faceoff_copy(
        active_preds, away["abbr"], home["abbr"]
    )
    arena_summary_lines = "\n".join(
        f"- {_short_model_name(p)} ({p.get('modelMeta', {}).get('exactModelId', p.get('modelId'))}): "
        f"{p.get('winnerAbbr')} ({away['abbr']} {p['projectedScore']['away']}-{home['abbr']} {p['projectedScore']['home']}) | "
        f"{p.get('spreadLean', '')} | {p.get('totalLean', '')}"
        for p in active_preds
        if p.get("projectedScore")
    )

    fallback_summary = f"- Pick: {winner} ({away['abbr']} {proj['away']}-{home['abbr']} {proj['home']})"
    yt_title = _truncate_yt_title(
        f"{away['abbr']} vs {home['abbr']} Studio Scout: {sc_title[:42]} ({prob})! #shorts"
    )
    yt_desc = (
        f"Pre-kickoff AI Studio Scout for {away['name']} ({away['record']}) at {home['name']} ({home['record']}):\n"
        f"{arena_summary_lines or fallback_summary}\n"
        f"- Hidden Mismatch: {mismatch}\n"
        f"- Ref Crew: {ref_name} ({ref_flags})\n"
        f"- Crazy Surprise Call ({prob}): {sc_title}\n\n"
        "Full Multi-Model Arena & 100-pt receipts:\n"
        "https://thescottyb.github.io/nfl-game-scout/\n\n"
        f"#NFL #{away['abbr']}vs{home['abbr']} #NFLPredictions #AI #SportsAnalytics #Shorts"
    )

    scenes = (
        StudioScene(
            index=1,
            slug="01-hook",
            top_badge=f"{game['label']} • {away['abbr']} @ {home['abbr']}",
            headline=f"{away['abbr']} ({away['record']})<br>AT {home['abbr']} ({home['record']})",
            call_pill=f"{game['odds']['spread']} • O/U {game['odds']['total']}",
            caption=f"{game['venue']['stadium']} ({game['venue']['city']}). {game['weather']['summary']}.",
            accent_hex=accent_a,
            motion="zoom_in",
            transition_out="smoothleft",
            art_prompt=(
                f"Vertical 9:16 ESPN graphic-novel editorial sports illustration at {game['venue']['stadium']} "
                f"in {game['venue']['city']} ({game['weather']['summary']}), two blank-jersey rival quarterbacks "
                "facing off under dramatic stadium lights, dark top and bottom vignette, no text, no logos."
            ),
            dialogue=(
                DialogueTurn(
                    speaker="Alex",
                    style="energetic sports anchor, punchy kickoff hook",
                    text=(
                        f"{game['label']}: {away['name']} at {home['name']} inside "
                        f"{game['venue']['stadium']}. Line sits at {game['odds']['spread']}."
                    ),
                ),
                DialogueTurn(
                    speaker="Maya",
                    style="sharp sports analyst",
                    text=f"Conditions: {game['weather']['summary']}. And {pred.get('primaryKey', 'the trenches decide this one.')}",
                ),
            ),
        ),
        StudioScene(
            index=2,
            slug="02-mismatch",
            top_badge="HIDDEN COACHING MISMATCH",
            headline="BELOW THE<br>COORDINATORS",
            call_pill=f"{away['abbr']} VS. {home['abbr']} ORG CHART",
            caption=mismatch[:115],
            accent_hex=accent_b,
            motion="pan_right",
            transition_out="smoothleft",
            art_prompt=(
                "Vertical 9:16 ESPN graphic-novel editorial sports illustration of an explosive "
                "line-of-scrimmage trench clash with rival coach silhouettes on the sidelines, "
                "high contrast, dark top and bottom vignette, no text, no logos."
            ),
            dialogue=(
                DialogueTurn(
                    speaker="Alex",
                    style="crisp sports analyst",
                    text=f"Here is our hidden coaching mismatch below the coordinators: {mismatch}",
                ),
                DialogueTurn(
                    speaker="Maya",
                    style="knowing sports analyst",
                    text=f"Thirty-day locker-room sentiment sits at {away['abbr']} {away_s:+.1f} versus {home['abbr']} {home_s:+.1f}.",
                ),
            ),
        ),
        StudioScene(
            index=3,
            slug="03-refs",
            top_badge="REF CREW & PHYSICS",
            headline=f"REF CREW:<br>{ref_name.upper()}",
            call_pill=ref_flags[:42],
            caption=ref_impact[:115],
            accent_hex="#facc15",
            motion="zoom_in_high",
            transition_out="smoothright",
            art_prompt=(
                "Vertical 9:16 ESPN graphic-novel editorial sports illustration of a black-and-white "
                "striped football referee throwing a glowing yellow penalty flag under intense stadium lights, "
                "dark top and bottom vignette, no text, no logos."
            ),
            dialogue=(
                DialogueTurn(
                    speaker="Alex",
                    style="intense sports anchor",
                    text=f"Watch referee {ref_name}, averaging {ref_flags}.",
                ),
                DialogueTurn(
                    speaker="Maya",
                    style="dramatic sports analyst",
                    text=ref_impact,
                ),
            ),
        ),
        StudioScene(
            index=4,
            slug="04-freak-play",
            top_badge=f"{prob} CRAZY SURPRISE CALL",
            headline=sc_title[:34].upper(),
            call_pill=f"LOCKED FREAK CALL • {prob}",
            caption=sc_detail[:115],
            accent_hex=accent_a,
            motion="pan_left",
            transition_out="fade",
            art_prompt=(
                f"Vertical 9:16 ESPN graphic-novel editorial sports illustration capturing dramatic "
                f"football special-teams and trick-play chaos at {game['venue']['stadium']}, "
                "high contrast rim lighting, dark top and bottom vignette, no text, no logos."
            ),
            dialogue=(
                DialogueTurn(
                    speaker="Alex",
                    style="hyped sports anchor",
                    text=f"Our locked freak-play call at {prob}: {sc_title}!",
                ),
                DialogueTurn(
                    speaker="Maya",
                    style="energetic sports analyst",
                    text=(
                        f"{sc_detail} "
                        + (f"Bonus sleeper: {bs.get('title', '')}." if bs.get("title") else "")
                    ).strip(),
                ),
            ),
        ),
        StudioScene(
            index=5,
            slug="05-pick-arena",
            top_badge=faceoff_badge,
            headline=faceoff_headline,
            call_pill=faceoff_pill,
            caption=faceoff_caption,
            accent_hex="#34d399",
            motion="zoom_out",
            transition_out="fade",
            art_prompt=(
                "Vertical 9:16 futuristic sports-tech illustration of a glowing holographic football field "
                "flanked by emerald-cyan and amber-gold AI neural cores in a dark studio, no text, no logos."
            ),
            dialogue=(
                DialogueTurn(
                    speaker="Alex",
                    style="fast, punchy sports anchor sign-off",
                    text=faceoff_spoken,
                ),
                DialogueTurn(
                    speaker="Maya",
                    style="warm, energetic call-to-action",
                    text="Every model is graded out of a hundred after the final whistle—link is in the description!",
                ),
            ),
        ),
    )

    return StudioCut(
        cut_id=gid,
        output_filename=f"{idx:02d}-{gid}-{slate_id}-studio.mp4",
        asset_subdir=f"{idx:02d}-{gid}",
        default_tempo=1.07,
        yt_title=yt_title,
        yt_description=yt_desc,
        scenes=scenes,
    )


def _build_generic_supercut(slate_id: str, slate: dict[str, Any]) -> StudioCut:
    games = slate.get("games", [])
    scenes: list[StudioScene] = [
        StudioScene(
            index=1,
            slug="01-cold-open",
            top_badge=str(slate.get("week", "NFL SLATE")).upper(),
            headline=f"{len(games)} FREAK PLAYS<br>LOCKED PRE-KICKOFF",
            call_pill="TIMESTAMPED ON GITHUB",
            caption=str(slate.get("subtitle", "Pre-kickoff AI scouting receipts."))[:115],
            accent_hex="#22d3ee",
            motion="zoom_in",
            transition_out="smoothleft",
            art_prompt=(
                "Vertical 9:16 ESPN graphic-novel editorial sports illustration of an electric night "
                "NFL stadium under glowing floodlights, dark top and bottom vignette, no text, no logos."
            ),
            dialogue=(
                DialogueTurn(
                    speaker="Alex",
                    style="energetic sports anchor, fast-paced hook",
                    text=f"{slate.get('title', 'NFL Game Scout')}—we locked {len(games)} freak-play calls on GitHub before kickoff!",
                ),
                DialogueTurn(
                    speaker="Maya",
                    style="punchy sports analyst",
                    text="Here are the wildest grounded scenarios on the board.",
                ),
            ),
        )
    ]

    motions = ("pan_right", "zoom_in_high", "pan_left")
    transitions = ("smoothleft", "smoothright", "fade")
    bullet_lines: list[str] = []
    for g_idx, game in enumerate(games[:3], start=1):
        pred = _primary_locked_pred(game)
        sc = pred.get("surpriseCall") or {}
        proj = pred.get("projectedScore") or {"away": 0, "home": 0}
        winner = pred.get("winnerAbbr") or game["home"]["abbr"]
        sc_title = sc.get("title") or "Rare-Rule Chaos"
        prob = sc.get("probability") or "~10%"
        bullet_lines.append(f"{g_idx}) {game['away']['abbr']} @ {game['home']['abbr']}: {sc_title} ({prob})")
        scenes.append(
            StudioScene(
                index=g_idx + 1,
                slug=f"{g_idx + 1:02d}-{_safe_slug(game['id'])}",
                top_badge=f"{game['away']['abbr']} @ {game['home']['abbr']} • {game['broadcast']}",
                headline=sc_title[:32].upper(),
                call_pill=f"{prob} • PICK: {winner} {proj['away']}–{proj['home']}",
                caption=str(sc.get("detail", ""))[:115],
                accent_hex=_safe_hex_color(game["home"].get("color", ""), "#38bdf8"),
                motion=motions[(g_idx - 1) % len(motions)],
                transition_out=transitions[(g_idx - 1) % len(transitions)],
                art_prompt=(
                    f"Vertical 9:16 ESPN graphic-novel sports editorial illustration at {game['venue']['stadium']} "
                    "highlighting dramatic football action, dark top and bottom vignette, no text, no logos."
                ),
                dialogue=(
                    DialogueTurn(
                        speaker="Alex",
                        style="excited sports anchor",
                        text=f"Game {g_idx}: {game['away']['name']} at {game['home']['name']}.",
                    ),
                    DialogueTurn(
                        speaker="Maya",
                        style="sharp sports analyst",
                        text=f"Freak call at {prob}: {sc_title}. Pick: {winner}, {proj['away']} to {proj['home']}.",
                    ),
                ),
            )
        )

    active_models: list[str] = []
    seen_ids: set[str] = set()
    for game in games:
        for p in _active_predictions(game):
            mid = str(p.get("modelId") or "")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                active_models.append(_short_model_name(p))

    outro_badge = (
        f"{len(active_models)}-MODEL ARENA • 100-PT RECEIPTS"
        if len(active_models) >= 2
        else f"{(active_models[0].upper() if active_models else 'AI SCOUT')} • 100-PT ARENA"
    )
    outro_spoken = (
        f"Every call across {', '.join(active_models[:3])} is timestamped on GitHub before kickoff and graded out of a hundred."
        if active_models
        else "Every call is timestamped on GitHub before kickoff and graded out of a hundred."
    )

    scenes.append(
        StudioScene(
            index=len(scenes) + 1,
            slug=f"{len(scenes) + 1:02d}-outro-arena",
            top_badge=outro_badge,
            headline="RECEIPTS LOCKED<br>BEFORE KICKOFF",
            call_pill="thescottyb.github.io/nfl-game-scout",
            caption="Timestamped on GitHub before kickoff. Which freak play hits?",
            accent_hex="#34d399",
            motion="zoom_out",
            transition_out="fade",
            art_prompt=(
                "Vertical 9:16 futuristic editorial sports-tech illustration of a glowing holographic "
                "football field flanked by rival AI neural cores, no text, no logos."
            ),
            dialogue=(
                DialogueTurn(
                    speaker="Alex",
                    style="fast sports anchor sign-off",
                    text=outro_spoken,
                ),
                DialogueTurn(
                    speaker="Maya",
                    style="warm call-to-action",
                    text="Link's in the description—which freak play hits?",
                ),
            ),
        )
    )

    yt_title = _truncate_yt_title(
        f"{len(games)} NFL {slate.get('week', '')} Freak Plays Locked Before Kickoff! #shorts"
    )
    yt_desc = (
        f"Pre-kickoff AI Studio Supercut for {slate.get('title', slate_id)}:\n"
        + "\n".join(bullet_lines)
        + "\n\nFull Multi-Model Arena & 100-pt receipts:\n"
        "https://thescottyb.github.io/nfl-game-scout/\n\n"
        "#NFL #NFLPredictions #AI #SportsAnalytics #Shorts"
    )

    return StudioCut(
        cut_id="supercut",
        output_filename=f"00-{slate_id}-all-freak-plays-studio.mp4",
        asset_subdir=f"00-{slate_id}",
        default_tempo=1.06,
        yt_title=yt_title,
        yt_description=yt_desc,
        scenes=tuple(scenes),
    )


def build_cuts_from_slate(slate_path: Path) -> dict[str, StudioCut]:
    slate = json.loads(slate_path.read_text(encoding="utf-8"))
    slate_id = _safe_slug(str(slate.get("slateId", slate_path.stem)))
    if slate_id == "2026-thanksgiving":
        return dict(THANKSGIVING_2026_CUTS)

    cuts: dict[str, StudioCut] = {"supercut": _build_generic_supercut(slate_id, slate)}
    for idx, game in enumerate(slate.get("games", []), start=1):
        cut = _build_generic_game_cut(slate_id, idx, game)
        cuts[cut.cut_id] = cut
    return cuts


def write_studio_upload_kit(
    cuts: list[StudioCut],
    kit_path: Path,
    qa_reports: dict[str, dict[str, Any]] | None = None,
) -> Path:
    lines = [
        "# NFL Game Scout — Studio v2 YouTube Shorts Upload Kit",
        "",
        "Generated automatically by `scripts/render_studio_short.py`. All titles are enforced `<= 100` characters and ready to copy-paste into YouTube Studio (`#shorts`).",
        "",
    ]
    for idx, cut in enumerate(cuts, start=1):
        title = _truncate_yt_title(cut.yt_title)
        rel_file = f"shorts/output/{cut.output_filename}"
        qa = (qa_reports or {}).get(cut.cut_id)
        qa_note = ""
        if qa:
            qa_note = (
                f" · `{qa['duration_sec']:.2f}s` · `{qa['size_mb']:.2f} MB` · "
                f"tail RMS `{qa['tail_rms_db']:.1f} dB` (QA PASS)"
            )
        lines.extend(
            [
                f"## {idx}. `{cut.cut_id}` (`{rel_file}`{qa_note})",
                "",
                f"- **Suggested YouTube Short Title:** `{title}` ({len(title)} chars)",
                "- **Audience:** No, it's not made for kids",
                "- **Visibility:** Public",
                "- **Description:**",
                "```text",
                cut.yt_description.strip(),
                "```",
                "",
            ]
        )
    kit_path.parent.mkdir(parents=True, exist_ok=True)
    kit_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return kit_path


MUSIC_PROMPT = (
    "Energetic primetime American football television broadcast underscore theme, "
    "crisp modern trap-influenced sports percussion, punchy orchestral brass hits, "
    "deep driving bassline, high-stakes stadium anticipation, 118 BPM, clean studio mix. "
    "Strictly instrumental only, no vocals, no lyrics, no chanting."
)


def require_env_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
    return key


def call_interactions(api_key: str, payload: dict, timeout_sec: int = 180) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "Api-Revision": API_REVISION,
    }
    backoffs = (3, 8, 18, 30)
    last_err: Exception | None = None
    for attempt in range(len(backoffs) + 1):
        req = urllib.request.Request(INTERACTIONS_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:600]
            last_err = RuntimeError(f"HTTP {exc.code} from Interactions API ({payload.get('model')}): {err_body}")
            if exc.code == 429 and ("limit: 0 " in err_body or "requests per day" in err_body):
                raise last_err from None
            if exc.code in (429, 500, 502, 503, 504) and attempt < len(backoffs):
                time.sleep(backoffs[attempt])
                continue
            raise last_err from None
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = RuntimeError(f"Network error calling Interactions API ({payload.get('model')}): {exc}")
            if attempt < len(backoffs):
                time.sleep(backoffs[attempt])
                continue
            raise last_err from None
    assert last_err is not None
    raise last_err


def extract_media_block(resp: dict, expected_type: str) -> tuple[bytes, str]:
    candidates: list[dict] = []
    for step in resp.get("steps", []):
        if step.get("type") == "model_output":
            candidates.extend(step.get("content", []))
    for out in resp.get("outputs", []):
        if isinstance(out, dict):
            candidates.append(out)

    for item in reversed(candidates):
        if item.get("type") == expected_type and item.get("data"):
            return base64.b64decode(item["data"]), str(item.get("mime_type", ""))

    summary_types = [
        (s.get("type"), [c.get("type") for c in s.get("content", [])])
        for s in resp.get("steps", [])
    ]
    raise RuntimeError(
        f"No '{expected_type}' media block found in Interactions response "
        f"(status={resp.get('status')}, steps={summary_types})"
    )


def save_audio_bytes(raw: bytes, mime_type: str, out_path: Path) -> Path:
    mt = mime_type.lower()
    if mt.startswith("audio/l16") or mt.startswith("audio/pcm"):
        rate_match = re.search(r"rate=(\d+)", mt)
        sample_rate = int(rate_match.group(1)) if rate_match else 24000
        ch_match = re.search(r"channels=(\d+)", mt)
        channels = int(ch_match.group(1)) if ch_match else 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(raw)
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return out_path


def generate_scene_art(api_key: str | None, scene: StudioScene, asset_dir: Path, force: bool) -> Path:
    out_png = asset_dir / f"{scene.slug}_art.png"
    meta_file = asset_dir / f"{scene.slug}_art.source.txt"
    if out_png.exists() and not force and out_png.stat().st_size > 20_000:
        print(f"[cache] art  {asset_dir.name}/{scene.slug} -> {out_png.name}")
        return out_png

    if not api_key:
        raise RuntimeError(
            f"Missing cached artwork {out_png} and GEMINI_API_KEY is unset."
        )

    print(f"[gen]   art  {asset_dir.name}/{scene.slug} (gemini-nano-banana-2.1, 9:16 2K)...")
    payload = {
        "model": "gemini-nano-banana-2.1",
        "input": scene.art_prompt,
        "response_format": {
            "type": "image",
            "aspect_ratio": "9:16",
            "image_size": "2K",
        },
    }
    resp = call_interactions(api_key, payload, timeout_sec=180)
    raw, mime_type = extract_media_block(resp, "image")

    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        out_png.write_bytes(raw)
    else:
        tmp_raw = asset_dir / f"{scene.slug}_art_raw.bin"
        tmp_raw.write_bytes(raw)
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp_raw), str(out_png)],
            check=True,
        )
        tmp_raw.unlink(missing_ok=True)
    meta_file.write_text("gemini-nano-banana-2.1", encoding="utf-8")
    print(f"[done]  art  {asset_dir.name}/{scene.slug} ({mime_type or 'image/png'}, {out_png.stat().st_size // 1024} KB)")
    return out_png


def synthesize_say_dialogue(scene: StudioScene, out_wav: Path) -> Path:
    if not shutil.which("say"):
        raise RuntimeError("macOS `say` binary not found for local TTS fallback.")
    voice_map = {"Alex": "Daniel", "Maya": "Samantha"}
    with tempfile.TemporaryDirectory(prefix="say_fallback_") as td:
        tmp = Path(td)
        turn_wavs: list[Path] = []
        for idx, turn in enumerate(scene.dialogue):
            aiff = tmp / f"t{idx}.aiff"
            wav_p = tmp / f"t{idx}.wav"
            voice = voice_map.get(turn.speaker, "Samantha")
            spoken_text = apply_tts_pronunciations(turn.text)
            subprocess.run(
                ["say", "-v", voice, "-r", "195", "-o", str(aiff), "--", spoken_text],
                check=True,
            )
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(aiff), "-ar", "48000", "-ac", "1", str(wav_p)],
                check=True,
            )
            turn_wavs.append(wav_p)

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for p in turn_wavs:
            cmd.extend(["-i", str(p)])
        inputs = "".join(f"[{i}:a]" for i in range(len(turn_wavs)))
        cmd.extend(["-filter_complex", f"{inputs}concat=n={len(turn_wavs)}:v=0:a=1[a]", "-map", "[a]", str(out_wav)])
        subprocess.run(cmd, check=True)
    return out_wav


def generate_scene_tts(
    api_key: str | None,
    scene: StudioScene,
    asset_dir: Path,
    force: bool,
    tts_backend: str = "auto",
    allow_say_fallback: bool = False,
) -> Path:
    out_wav = asset_dir / f"{scene.slug}_vo.wav"
    meta_file = asset_dir / f"{scene.slug}_vo.source.txt"
    if out_wav.exists() and not force and out_wav.stat().st_size > 10_000:
        print(f"[cache] tts  {asset_dir.name}/{scene.slug} -> {out_wav.name}")
        return out_wav

    if tts_backend == "say":
        synthesize_say_dialogue(scene, out_wav)
        meta_file.write_text("macos-say", encoding="utf-8")
        dur = probe_duration(out_wav)
        print(f"[done]  tts  {asset_dir.name}/{scene.slug} (macos-say, {dur:.2f}s)")
        return out_wav

    if not api_key:
        if allow_say_fallback:
            synthesize_say_dialogue(scene, out_wav)
            meta_file.write_text("macos-say", encoding="utf-8")
            return out_wav
        raise RuntimeError(f"Missing cached TTS {out_wav} and GEMINI_API_KEY is unset.")

    content_items = [
        {
            "type": "text",
            "text": apply_tts_pronunciations(turn.text),
            "annotations": [
                {
                    "type": "speech_metadata",
                    "speaker": turn.speaker,
                    "style": turn.style,
                }
            ],
        }
        for turn in scene.dialogue
    ]
    last_err: Exception | None = None
    resp = None
    used_model = "gemini-3.8-flash-tts"
    for tts_model in ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"):
        print(f"[gen]   tts  {asset_dir.name}/{scene.slug} ({tts_model}, Puck + Kore)...")
        payload = {
            "model": tts_model,
            "input": [{"type": "user_input", "content": content_items}],
            "response_format": {"type": "audio"},
            "generation_config": {
                "speech_config": {
                    "speakers": [
                        {"speaker": "Alex", "voice": "Puck"},
                        {"speaker": "Maya", "voice": "Kore"},
                    ]
                }
            },
        }
        try:
            resp = call_interactions(api_key, payload, timeout_sec=180)
            used_model = tts_model
            break
        except Exception as exc:
            last_err = exc
            print(f"[warn]  tts model {tts_model} failed for {scene.slug}: {exc}", file=sys.stderr)

    if resp is None:
        if allow_say_fallback:
            print(f"[warn]  falling back to macOS `say` for {scene.slug}", file=sys.stderr)
            synthesize_say_dialogue(scene, out_wav)
            meta_file.write_text("macos-say", encoding="utf-8")
            return out_wav
        assert last_err is not None
        raise last_err

    raw, mime_type = extract_media_block(resp, "audio")
    if "mpeg" in mime_type.lower() or "mp3" in mime_type.lower():
        tmp_mp3 = asset_dir / f"{scene.slug}_vo.mp3"
        tmp_mp3.write_bytes(raw)
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(tmp_mp3), "-ar", "48000", "-ac", "1", str(out_wav),
            ],
            check=True,
        )
        tmp_mp3.unlink(missing_ok=True)
    else:
        save_audio_bytes(raw, mime_type, out_wav)
    meta_file.write_text(used_model, encoding="utf-8")
    dur = probe_duration(out_wav)
    print(f"[done]  tts  {asset_dir.name}/{scene.slug} ({used_model}, {mime_type or 'audio/wav'}, {dur:.2f}s)")
    return out_wav


def synthesize_broadcast_bed(out_music: Path, duration_sec: float = 96.0) -> None:
    """Synthesize a crisp 118-BPM primetime NFL broadcast groove (kick, snare, hats, bass, brass pad)."""
    sr = 48000
    bpm = 118.0
    beat_sec = 60.0 / bpm
    step_sec = beat_sec / 4.0
    n_samples = int(sr * duration_sec)

    chords_hz = [
        (73.42, 146.83, 220.00, 293.66, 349.23),  # Dm
        (58.27, 116.54, 174.61, 233.08, 293.66),  # Bb
        (65.41, 130.81, 196.00, 261.63, 329.63),  # C
        (55.00, 110.00, 164.81, 220.00, 277.18),  # A
    ]

    rng = random.Random(20261126)
    two_pi = 2.0 * math.pi
    tmp_wav = out_music.with_suffix(".synth.wav")
    raw_frames = bytearray(n_samples * 4)
    bar_sec = beat_sec * 4.0

    for i in range(n_samples):
        t = i / sr
        bar_idx = int(t / bar_sec)
        chord = chords_hz[bar_idx % len(chords_hz)]
        bar_t = t % bar_sec
        step_f = bar_t / step_sec
        step_idx = int(step_f) % 16
        step_local = (step_f - int(step_f)) * step_sec

        kick = 0.0
        if step_idx in (0, 6, 8, 10) and step_local < 0.14:
            env = math.exp(-step_local * 26.0)
            freq = 44.0 + 115.0 * math.exp(-step_local * 42.0)
            kick = 0.52 * env * math.sin(two_pi * freq * step_local)

        snare = 0.0
        if step_idx in (4, 12) and step_local < 0.11:
            env = math.exp(-step_local * 34.0)
            noise = rng.uniform(-1.0, 1.0)
            body = math.sin(two_pi * 190.0 * step_local)
            snare = 0.28 * env * (0.72 * noise + 0.28 * body)

        hat = 0.0
        if step_local < 0.025:
            h_amp = 0.085 if step_idx % 2 == 0 else 0.045
            hat = h_amp * math.exp(-step_local * 140.0) * rng.uniform(-1.0, 1.0)

        bass_f = chord[0]
        eighth_local = t % (beat_sec / 2.0)
        bass_env = 0.35 + 0.65 * math.exp(-eighth_local * 9.0)
        bass = 0.22 * bass_env * (
            0.75 * math.sin(two_pi * bass_f * t)
            + 0.25 * math.sin(two_pi * (bass_f * 2.0) * t)
        )

        pad_env = 0.45 + 0.55 * math.sin(math.pi * min(1.0, bar_t / bar_sec))
        pad_l = 0.0
        pad_r = 0.0
        for h_idx, f_hz in enumerate(chord[1:]):
            w = 0.075 / (1.0 + 0.22 * h_idx)
            det = 0.0025 * (h_idx + 1)
            s_l = math.sin(two_pi * f_hz * (1.0 - det) * t) + 0.28 * math.sin(two_pi * 2.0 * f_hz * (1.0 - det) * t)
            s_r = math.sin(two_pi * f_hz * (1.0 + det) * t) + 0.28 * math.sin(two_pi * 2.0 * f_hz * (1.0 + det) * t)
            pad_l += w * s_l
            pad_r += w * s_r
        pad_l *= pad_env
        pad_r *= pad_env

        stab = 0.0
        if bar_t < 0.32:
            s_env = math.exp(-bar_t * 7.5)
            stab = 0.14 * s_env * (
                math.sin(two_pi * chord[2] * t) + 0.5 * math.sin(two_pi * chord[3] * t)
            )

        left = math.tanh((kick + snare + hat * 0.8 + bass + pad_l + stab) * 0.92)
        right = math.tanh((kick + snare + hat * 1.15 + bass + pad_r + stab) * 0.92)
        struct.pack_into("<hh", raw_frames, i * 4, int(left * 30000), int(right * 30000))

    with wave.open(str(tmp_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(raw_frames)

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(tmp_wav),
            "-af", "highpass=f=36,lowpass=f=11000,aecho=0.8:0.7:28|42:0.18|0.12",
            "-c:a", "libmp3lame", "-b:a", "192k",
            str(out_music),
        ],
        check=True,
    )
    tmp_wav.unlink(missing_ok=True)


def generate_music_bed(api_key: str | None, music_dir: Path, force: bool) -> tuple[Path, str]:
    music_dir.mkdir(parents=True, exist_ok=True)
    out_music = music_dir / "music_bed.mp3"
    meta_file = music_dir / "music_bed.source.txt"
    if out_music.exists() and not force and out_music.stat().st_size > 20_000:
        src = meta_file.read_text(encoding="utf-8").strip() if meta_file.exists() else "cached"
        print(f"[cache] music -> {out_music.name} ({src})")
        return out_music, src

    if api_key:
        for model_name in ("lyria-3.5", "lyria-3-clip-preview"):
            try:
                print(f"[gen]   music ({model_name})...")
                payload = {"model": model_name, "input": MUSIC_PROMPT}
                resp = call_interactions(api_key, payload, timeout_sec=240)
                raw, mime_type = extract_media_block(resp, "audio")
                if "wav" in mime_type.lower() or raw[:4] == b"RIFF":
                    tmp_wav = music_dir / "music_bed_raw.wav"
                    save_audio_bytes(raw, mime_type, tmp_wav)
                    subprocess.run(
                        [
                            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(tmp_wav), "-c:a", "libmp3lame", "-b:a", "192k", str(out_music),
                        ],
                        check=True,
                    )
                    tmp_wav.unlink(missing_ok=True)
                else:
                    out_music.write_bytes(raw)
                dur = probe_duration(out_music)
                meta_file.write_text(model_name, encoding="utf-8")
                print(f"[done]  music ({model_name}, {dur:.2f}s, {out_music.stat().st_size // 1024} KB)")
                return out_music, model_name
            except Exception as exc:
                print(f"[warn]  music model {model_name} failed: {exc}", file=sys.stderr)

    print("[synth] generating 118-BPM primetime NFL broadcast underscore bed...")
    synthesize_broadcast_bed(out_music, duration_sec=96.0)
    meta_file.write_text("python-broadcast-synth-118bpm", encoding="utf-8")
    return out_music, "python-broadcast-synth-118bpm"


def probe_json(path: Path) -> dict[str, Any]:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        text=True,
    )
    return json.loads(out)


def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    ).strip()
    return float(out)


def probe_pix_fmt(path: Path) -> str:
    return subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=pix_fmt",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    ).strip()


def build_hud_html(scene: StudioScene, total_scenes: int) -> str:
    safe_badge = html.escape(scene.top_badge)
    safe_headline = "<br>".join(html.escape(part) for part in scene.headline.split("<br>"))
    safe_pill = html.escape(scene.call_pill)
    safe_caption = html.escape(scene.caption)
    accent = scene.accent_hex
    if not SAFE_HEX_RE.fullmatch(accent):
        raise ValueError(f"Invalid accent hex color: {accent}")

    seg_html = "".join(
        f'<div class="{"seg active" if i <= scene.index else "seg"}"></div>'
        for i in range(1, total_scenes + 1)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    width: {WIDTH}px;
    height: {HEIGHT}px;
    overflow: hidden;
    background: transparent !important;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
    color: #f8fafc;
    -webkit-font-smoothing: antialiased;
  }}
  .frame {{
    position: relative;
    width: {WIDTH}px;
    height: {HEIGHT}px;
    background: transparent;
  }}
  .top-scrim {{
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 320px;
    background: linear-gradient(
      to bottom,
      rgba(5, 9, 18, 0.88) 0%,
      rgba(5, 9, 18, 0.56) 52%,
      rgba(5, 9, 18, 0.0) 100%
    );
  }}
  .bottom-scrim {{
    position: absolute;
    left: 0; right: 0; bottom: 0;
    height: 820px;
    background: linear-gradient(
      to top,
      rgba(5, 9, 18, 0.96) 0%,
      rgba(5, 9, 18, 0.86) 44%,
      rgba(5, 9, 18, 0.42) 76%,
      rgba(5, 9, 18, 0.0) 100%
    );
  }}
  .top-bar {{
    position: absolute;
    top: 72px;
    left: 44px;
    right: 44px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }}
  .progress {{
    display: flex;
    gap: 10px;
    width: 100%;
  }}
  .seg {{
    flex: 1;
    height: 6px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.22);
  }}
  .seg.active {{
    background: {accent};
    box-shadow: 0 0 14px {accent}aa;
  }}
  .brand-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
  }}
  .brand-pill {{
    display: inline-flex;
    align-items: center;
    gap: 12px;
    padding: 12px 22px;
    border-radius: 999px;
    background: rgba(9, 15, 28, 0.78);
    border: 1.5px solid rgba(255, 255, 255, 0.18);
    font-size: 24px;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }}
  .live-dot {{
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: {accent};
    box-shadow: 0 0 12px {accent};
  }}
  .scene-badge {{
    padding: 12px 22px;
    border-radius: 999px;
    background: rgba(9, 15, 28, 0.78);
    border: 1.5px solid {accent}88;
    color: {accent};
    font-size: 22px;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }}
  .lower-third {{
    position: absolute;
    left: 44px;
    right: 162px;
    bottom: 292px;
    display: flex;
    flex-direction: column;
    gap: 18px;
  }}
  .call-pill {{
    align-self: flex-start;
    padding: 10px 20px;
    border-radius: 12px;
    background: rgba(9, 15, 28, 0.86);
    border-left: 7px solid {accent};
    border-top: 1px solid rgba(255,255,255,0.14);
    border-right: 1px solid rgba(255,255,255,0.14);
    border-bottom: 1px solid rgba(255,255,255,0.14);
    color: {accent};
    font-size: 25px;
    font-weight: 900;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    box-shadow: 0 10px 28px rgba(0,0,0,0.45);
  }}
  .headline {{
    font-size: 66px;
    font-weight: 900;
    line-height: 1.02;
    letter-spacing: -0.02em;
    text-transform: uppercase;
    color: #ffffff;
    text-shadow: 0 6px 26px rgba(0, 0, 0, 0.78), 0 2px 6px rgba(0, 0, 0, 0.9);
  }}
  .caption-card {{
    padding: 20px 24px;
    border-radius: 20px;
    background: rgba(8, 13, 25, 0.84);
    border: 1.5px solid rgba(255, 255, 255, 0.16);
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.55);
  }}
  .host-label {{
    font-size: 19px;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: {accent};
    margin-bottom: 8px;
  }}
  .caption-text {{
    font-size: 31px;
    font-weight: 700;
    line-height: 1.28;
    color: #f1f5f9;
  }}
  .footer-receipt {{
    position: absolute;
    left: 44px;
    bottom: 232px;
    font-size: 21px;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: rgba(226, 232, 240, 0.78);
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.85);
  }}
</style>
</head>
<body>
  <div class="frame">
    <div class="top-scrim"></div>
    <div class="bottom-scrim"></div>
    <div class="top-bar">
      <div class="progress">{seg_html}</div>
      <div class="brand-row">
        <div class="brand-pill"><span class="live-dot"></span>NFL GAME SCOUT • STUDIO</div>
        <div class="scene-badge">{safe_badge}</div>
      </div>
    </div>
    <div class="lower-third">
      <div class="call-pill">{safe_pill}</div>
      <div class="headline">{safe_headline}</div>
      <div class="caption-card">
        <div class="host-label">ALEX &amp; MAYA • AI BROADCAST DESK</div>
        <div class="caption-text">{safe_caption}</div>
      </div>
    </div>
    <div class="footer-receipt">TIMESTAMPED RECEIPTS • thescottyb.github.io/nfl-game-scout</div>
  </div>
</body>
</html>
"""


def render_hud_png(scene: StudioScene, total_scenes: int, asset_dir: Path, chrome_bin: Path) -> Path:
    hud_html = asset_dir / f"{scene.slug}_hud.html"
    hud_png = asset_dir / f"{scene.slug}_hud.png"
    hud_html.write_text(build_hud_html(scene, total_scenes), encoding="utf-8")

    subprocess.run(
        [
            str(chrome_bin),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--default-background-color=00000000",
            f"--window-size={WIDTH},{HEIGHT}",
            f"--screenshot={hud_png}",
            hud_html.as_uri(),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pix_fmt = probe_pix_fmt(hud_png)
    if "a" not in pix_fmt.lower():
        raise RuntimeError(
            f"Chrome HUD PNG {hud_png.name} lacks alpha channel (pix_fmt={pix_fmt})."
        )
    print(f"[hud]   {asset_dir.name}/{scene.slug} -> {hud_png.name} ({pix_fmt})")
    return hud_png


def zoompan_expr(motion: str, frames: int) -> str:
    n = max(frames - 1, 1)
    if motion == "zoom_in":
        z = f"1.0+0.10*(on/{n})"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "pan_right":
        z = "1.12"
        x = f"(iw-iw/zoom)*(on/{n})"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "zoom_in_high":
        z = f"1.0+0.12*(on/{n})"
        x = "iw/2-(iw/zoom/2)"
        y = "(ih/2-(ih/zoom/2))*0.82"
    elif motion == "pan_left":
        z = "1.12"
        x = f"(iw-iw/zoom)*(1-on/{n})"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "zoom_out":
        z = f"1.10-0.10*(on/{n})"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    else:
        raise ValueError(f"Unsupported motion preset: {motion}")
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:fps={FPS}:s={WIDTH}x{HEIGHT}"


def prepare_timed_voice(src_wav: Path, dst_wav: Path, tempo: float) -> float:
    """Apply highpass + exact tempo scaling to a 48kHz stereo WAV and return its exact duration."""
    tempo_filter = f",atempo={tempo:.3f}" if abs(tempo - 1.0) > 0.005 else ""
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src_wav),
            "-af", f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,highpass=f=75{tempo_filter}",
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
            str(dst_wav),
        ],
        check=True,
    )
    return probe_duration(dst_wav)


def render_scene_clip(
    scene: StudioScene,
    art_png: Path,
    hud_png: Path,
    clip_dur: float,
    tmp_dir: Path,
) -> Path:
    frames = max(int(round(clip_dur * FPS)), FPS)
    out_clip = tmp_dir / f"{scene.slug}_clip.mp4"
    zp = zoompan_expr(scene.motion, frames)
    filter_complex = (
        f"[0:v]scale=2160:3840:force_original_aspect_ratio=increase,"
        f"crop=2160:3840,{zp},setsar=1,fps={FPS},format=rgba[bg];"
        f"[1:v]scale={WIDTH}:{HEIGHT},format=rgba[hud];"
        f"[bg][hud]overlay=0:0:shortest=1,format=yuv420p[vout]"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(art_png),
            "-loop", "1", "-t", f"{frames / FPS:.3f}", "-i", str(hud_png),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-t", f"{frames / FPS:.3f}",
            "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p",
            str(out_clip),
        ],
        check=True,
    )
    print(f"[clip]  {scene.slug} ({frames / FPS:.2f}s, {scene.motion})")
    return out_clip


def _parse_loudnorm_json(stderr_text: str) -> dict[str, str]:
    match = re.search(r"\{\s*\"input_i\".*?\}", stderr_text, re.DOTALL)
    if not match:
        raise RuntimeError(f"Could not parse ffmpeg loudnorm JSON stats from stderr: {stderr_text[-400:]}")
    return json.loads(match.group(0))


def assemble_final_short(
    scene_clips: list[tuple[StudioScene, Path, Path, float]],
    music_path: Path,
    out_mp4: Path,
    tmp_dir: Path,
) -> tuple[float, list[tuple[float, float]]]:
    """Assemble stitched video + 2-pass linear loudnorm audio with clean post-master fade-out."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    actual_durs = [probe_duration(clip) for _, clip, _, _ in scene_clips]
    offsets: list[float] = []
    running = 0.0
    for i, dur in enumerate(actual_durs):
        offsets.append(running)
        if i < len(actual_durs) - 1:
            running += dur - XFADE_SEC
        else:
            running += dur
    total_dur = running
    n = len(scene_clips)

    # Track exact voice intervals for automated overlap verification
    vo_spans: list[tuple[float, float]] = []
    for i, (_, _, timed_vo_path, _) in enumerate(scene_clips):
        vo_start = offsets[i] + VOICE_LEAD_SEC
        vo_end = vo_start + probe_duration(timed_vo_path)
        vo_spans.append((vo_start, vo_end))

    # --- Step A: Stitch xfade video first so we lock audio to exact probed video duration ---
    stitched_video = tmp_dir / "stitched_video.mp4"
    cmd_v = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for _, clip_path, _, _ in scene_clips:
        cmd_v.extend(["-i", str(clip_path)])

    v_parts: list[str] = []
    for i in range(n):
        v_parts.append(f"[{i}:v]setpts=PTS-STARTPTS,fps={FPS},settb=AVTB,format=yuv420p[v{i}]")
    prev_v = "v0"
    for i in range(n - 1):
        trans = scene_clips[i][0].transition_out
        off = offsets[i + 1]
        nxt = f"vx{i}" if i < n - 2 else "vfinal"
        v_parts.append(
            f"[{prev_v}][v{i + 1}]xfade=transition={trans}:duration={XFADE_SEC:.2f}:offset={off:.3f}[{nxt}]"
        )
        prev_v = nxt

    cmd_v.extend(
        [
            "-filter_complex", ";".join(v_parts),
            "-map", "[vfinal]",
            "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-maxrate", "2200k", "-bufsize", "4400k",
            "-pix_fmt", "yuv420p",
            str(stitched_video),
        ]
    )
    subprocess.run(cmd_v, check=True)
    exact_v_dur = probe_duration(stitched_video)
    target_samples = int(round(exact_v_dur * 48000))

    # --- Step B: Audio Pass 1 (voice bus + sidechain-ducked music bed, NO end fade yet) ---
    premix_wav = tmp_dir / "premix.wav"
    cmd_mix = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for _, _, timed_vo_path, _ in scene_clips:
        cmd_mix.extend(["-i", str(timed_vo_path)])
    cmd_mix.extend(["-stream_loop", "-1", "-i", str(music_path)])

    mix_parts: list[str] = []
    vo_labels: list[str] = []
    for i in range(n):
        delay_ms = max(0, int(round((offsets[i] + VOICE_LEAD_SEC) * 1000)))
        lbl = f"vo{i}"
        mix_parts.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[{lbl}]")
        vo_labels.append(f"[{lbl}]")

    mix_parts.append(
        f"{''.join(vo_labels)}amix=inputs={n}:duration=longest:dropout_transition=0:normalize=0,"
        f"apad=whole_len={target_samples},atrim=end_sample={target_samples},asetpts=PTS-STARTPTS,"
        f"asplit=2[vo_main][vo_sc]"
    )
    mix_parts.append(
        f"[{n}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"atrim=end_sample={target_samples},asetpts=PTS-STARTPTS,"
        f"volume=-15dB,afade=t=in:st=0:d=0.35[music_pre]"
    )
    mix_parts.append(
        "[music_pre][vo_sc]sidechaincompress=threshold=0.025:ratio=6:attack=15:release=260:makeup=1[music_ducked]"
    )
    mix_parts.append(
        "[vo_main][music_ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[amix_out]"
    )

    cmd_mix.extend(
        [
            "-filter_complex", ";".join(mix_parts),
            "-map", "[amix_out]",
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
            str(premix_wav),
        ]
    )
    subprocess.run(cmd_mix, check=True)

    # Measure integrated loudness stats for 2-pass linear loudnorm
    measure = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(premix_wav),
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    ln = _parse_loudnorm_json(measure.stderr)

    # --- Step C: Audio Pass 2 (linear loudnorm -> post-master fade-out -> exact sample lock) ---
    last_vo_end = vo_spans[-1][1]
    fade_start = max(last_vo_end + 0.15, exact_v_dur - MASTER_SILENCE_TAIL_SEC - MASTER_FADE_OUT_SEC)
    fade_dur = max(0.35, (exact_v_dur - MASTER_SILENCE_TAIL_SEC) - fade_start)

    master_wav = tmp_dir / "master_audio.wav"
    ln_filter = (
        f"loudnorm=I=-14:TP=-1.5:LRA=11:"
        f"measured_I={ln['input_i']}:measured_TP={ln['input_tp']}:"
        f"measured_LRA={ln['input_lra']}:measured_thresh={ln['input_thresh']}:"
        f"offset={ln['target_offset']}:linear=true,"
        f"aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"afade=t=out:st={fade_start:.3f}:d={fade_dur:.3f}:curve=qsin,"
        f"apad=whole_len={target_samples},atrim=end_sample={target_samples},asetpts=PTS-STARTPTS"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(premix_wav),
            "-af", ln_filter,
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
            str(master_wav),
        ],
        check=True,
    )

    # --- Step D: Stream-copy stitched video + encode AAC audio to exact locked duration ---
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(stitched_video),
            "-i", str(master_wav),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(out_mp4),
        ],
        check=True,
    )
    return probe_duration(out_mp4), vo_spans


def _measure_tail_volume_db(mp4_path: Path, tail_sec: float = 0.25) -> tuple[float, float]:
    res = subprocess.run(
        [
            "ffmpeg", "-hide_banner",
            "-sseof", f"-{tail_sec:.2f}",
            "-i", str(mp4_path),
            "-vn", "-af", "volumedetect",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    mean_m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", res.stderr)
    max_m = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", res.stderr)
    mean_db = float(mean_m.group(1)) if mean_m else -91.0
    max_db = float(max_m.group(1)) if max_m else -91.0
    return mean_db, max_db


def verify_rendered_short(
    mp4_path: Path,
    vo_spans: list[tuple[float, float]] | None = None,
    max_mb: float = 20.0,
    max_duration_sec: float = 180.0,
    max_va_delta_sec: float = 0.05,
    max_tail_rms_db: float = -40.0,
) -> dict[str, Any]:
    """Automated post-render QA gate validating stream specs, sync, voice spacing, and tail fade."""
    info = probe_json(mp4_path)
    v_stream = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a_stream = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v_stream or not a_stream:
        raise RuntimeError(f"QA FAIL ({mp4_path.name}): missing video or audio stream")

    failures: list[str] = []
    width = int(v_stream["width"])
    height = int(v_stream["height"])
    pix_fmt = str(v_stream.get("pix_fmt", ""))
    v_codec = str(v_stream.get("codec_name", ""))
    a_codec = str(a_stream.get("codec_name", ""))
    sample_rate = int(a_stream.get("sample_rate", 0))
    channels = int(a_stream.get("channels", 0))
    v_dur = float(v_stream["duration"])
    a_dur = float(a_stream["duration"])
    va_delta = abs(v_dur - a_dur)
    size_mb = mp4_path.stat().st_size / (1024 * 1024)
    tail_mean_db, tail_max_db = _measure_tail_volume_db(mp4_path, tail_sec=0.25)

    if (width, height) != (WIDTH, HEIGHT):
        failures.append(f"resolution {width}x{height} != {WIDTH}x{HEIGHT}")
    if v_codec != "h264" or pix_fmt != "yuv420p":
        failures.append(f"video codec/pix_fmt {v_codec}/{pix_fmt} != h264/yuv420p")
    if a_codec != "aac" or sample_rate != 48000 or channels != 2:
        failures.append(f"audio {a_codec}/{sample_rate}Hz/{channels}ch != aac/48000Hz/2ch")
    if v_dur > max_duration_sec:
        failures.append(f"duration {v_dur:.2f}s exceeds cap {max_duration_sec:.1f}s")
    if size_mb > max_mb:
        failures.append(f"size {size_mb:.2f} MB exceeds cap {max_mb:.1f} MB")
    if va_delta > max_va_delta_sec:
        failures.append(f"|video_dur - audio_dur| = {va_delta:.3f}s > {max_va_delta_sec:.3f}s")
    if tail_mean_db > max_tail_rms_db:
        failures.append(f"tail 0.25s mean_volume {tail_mean_db:.1f} dB > {max_tail_rms_db:.1f} dB (abrupt EOF)")

    min_voice_gap: float | None = None
    if vo_spans:
        for i in range(len(vo_spans) - 1):
            gap = vo_spans[i + 1][0] - vo_spans[i][1]
            min_voice_gap = gap if min_voice_gap is None else min(min_voice_gap, gap)
            if gap < 0.0:
                failures.append(f"voice overlap between scene {i + 1} and {i + 2} ({gap:+.3f}s)")
        end_clearance = v_dur - vo_spans[-1][1]
        if end_clearance < 0.25:
            failures.append(f"final voice ends too close to EOF ({end_clearance:+.3f}s)")
    else:
        end_clearance = None

    report = {
        "file": mp4_path.name,
        "resolution": f"{width}x{height}",
        "video_codec": f"{v_codec}/{pix_fmt}",
        "audio_codec": f"{a_codec}/{sample_rate}Hz/{channels}ch",
        "duration_sec": round(v_dur, 3),
        "audio_duration_sec": round(a_dur, 3),
        "va_delta_sec": round(va_delta, 4),
        "size_mb": round(size_mb, 2),
        "tail_rms_db": round(tail_mean_db, 1),
        "tail_peak_db": round(tail_max_db, 1),
        "min_voice_gap_sec": round(min_voice_gap, 3) if min_voice_gap is not None else None,
        "end_voice_clearance_sec": round(end_clearance, 3) if end_clearance is not None else None,
        "passed": not failures,
        "failures": failures,
    }
    if failures:
        raise RuntimeError(f"QA GATE FAILED for {mp4_path.name}: " + "; ".join(failures))
    print(
        f"[qa]    PASS {mp4_path.name} | {v_dur:.2f}s | {size_mb:.2f} MB | "
        f"V/A delta {va_delta * 1000:.0f}ms | tail {tail_mean_db:.1f} dB | "
        f"min gap {min_voice_gap if min_voice_gap is not None else 0:.2f}s"
    )
    return report


def render_cut(
    cut: StudioCut,
    api_key: str | None,
    music_path: Path,
    music_source: str,
    chrome_bin: Path,
    tempo_override: float | None,
    force: bool,
    tts_workers: int,
    tts_backend: str,
    allow_say_fallback: bool,
    max_mb: float,
    slate_data: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    asset_dir = ASSETS_ROOT / cut.asset_subdir
    asset_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = OUTPUT_DIR / cut.output_filename
    tempo = tempo_override if tempo_override is not None else cut.default_tempo
    validate_runtime_args(tempo, tts_workers, max_mb)
    if slate_data is not None and slate_data.get("slateId") != "2026-thanksgiving":
        model_errors = validate_cut_models(cut, slate_data)
        if model_errors:
            raise RuntimeError("QA GATE FAILED (model attribution): " + "; ".join(model_errors))

    print(f"\n================ RENDERING CUT: {cut.cut_id} ================")
    art_map: dict[int, Path] = {}
    tts_map: dict[int, Path] = {}

    with ThreadPoolExecutor(max_workers=tts_workers) as pool:
        futures = {}
        for scene in cut.scenes:
            futures[pool.submit(generate_scene_art, api_key, scene, asset_dir, force)] = ("art", scene.index)
            futures[
                pool.submit(
                    generate_scene_tts,
                    api_key,
                    scene,
                    asset_dir,
                    force,
                    tts_backend,
                    allow_say_fallback,
                )
            ] = ("tts", scene.index)
        for fut in as_completed(futures):
            kind, idx = futures[fut]
            res = fut.result()
            if kind == "art":
                art_map[idx] = res
            else:
                tts_map[idx] = res

    hud_map = {s.index: render_hud_png(s, len(cut.scenes), asset_dir, chrome_bin) for s in cut.scenes}
    tail_pads = [VOICE_TAIL_SEC] * (len(cut.scenes) - 1) + [FINAL_TAIL_SEC]

    with tempfile.TemporaryDirectory(prefix=f"studio_{cut.cut_id}_") as td:
        tmp_dir = Path(td)
        timed_specs: list[tuple[StudioScene, Path, float]] = []
        for scene, pad in zip(cut.scenes, tail_pads):
            timed_vo = tmp_dir / f"{scene.slug}_vo_timed.wav"
            exact_vo_dur = prepare_timed_voice(tts_map[scene.index], timed_vo, tempo=tempo)
            clip_dur = VOICE_LEAD_SEC + exact_vo_dur + pad
            timed_specs.append((scene, timed_vo, clip_dur))

        clip_paths: dict[int, Path] = {}
        with ThreadPoolExecutor(max_workers=3) as clip_pool:
            cfuts = {
                clip_pool.submit(
                    render_scene_clip,
                    scene,
                    art_map[scene.index],
                    hud_map[scene.index],
                    clip_dur,
                    tmp_dir,
                ): scene.index
                for scene, _, clip_dur in timed_specs
            }
            for fut in as_completed(cfuts):
                clip_paths[cfuts[fut]] = fut.result()

        scene_clips = [
            (scene, clip_paths[scene.index], timed_vo, clip_dur)
            for scene, timed_vo, clip_dur in timed_specs
        ]
        final_dur, vo_spans = assemble_final_short(scene_clips, music_path, out_mp4, tmp_dir)

    qa_report = verify_rendered_short(out_mp4, vo_spans=vo_spans, max_mb=max_mb)
    qa_report["music_source"] = music_source
    qa_report["tempo"] = tempo

    tts_sources: set[str] = set()
    art_sources: set[str] = set()
    for scene in cut.scenes:
        vo_src = asset_dir / f"{scene.slug}_vo.source.txt"
        art_src = asset_dir / f"{scene.slug}_art.source.txt"
        tts_sources.add(vo_src.read_text(encoding="utf-8").strip() if vo_src.exists() else "cached")
        art_sources.add(art_src.read_text(encoding="utf-8").strip() if art_src.exists() else "cached")

    pred_models: list[dict[str, Any]] = []
    if slate_data is not None:
        games = slate_data.get("games", [])
        target_g = next((g for g in games if g.get("id") == cut.cut_id), None)
        for g in ([target_g] if target_g is not None else games):
            for p in _active_predictions(g):
                meta = p.get("modelMeta") or {}
                pred_models.append(
                    {
                        "gameId": g.get("id"),
                        "modelId": p.get("modelId"),
                        "modelLabel": p.get("modelLabel"),
                        "exactModelId": meta.get("exactModelId", p.get("modelId")),
                        "provider": meta.get("provider"),
                        "harness": meta.get("harness"),
                    }
                )
    qa_report["production_models"] = {
        "image_models": sorted(art_sources),
        "tts_models": sorted(tts_sources),
        "music_model": music_source,
        "prediction_models": pred_models,
    }
    print(f"=== READY: {cut.cut_id} ({final_dur:.2f}s, {qa_report['size_mb']:.2f} MB) ===")
    return out_mp4, qa_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Studio v2 NFL Game Scout Shorts from any Arena slate JSON")
    parser.add_argument("--slate", type=Path, default=DEFAULT_SLATE, help="Path to Arena slate JSON")
    parser.add_argument(
        "--cut",
        default="supercut",
        help="Cut id from slate (e.g. 'supercut', 'bears-at-lions'), 'games' (all non-supercut), or 'all'",
    )
    parser.add_argument("--tempo", type=float, default=None, help="Override voice playback tempo [0.5..2.0]")
    parser.add_argument("--tts-workers", type=int, default=3, help="Concurrent TTS/art workers [1..8]")
    parser.add_argument("--tts-backend", choices=["auto", "gemini", "say"], default="auto")
    parser.add_argument("--allow-say-fallback", action="store_true", help="Fallback to macOS `say` if Gemini TTS RPD is exhausted")
    parser.add_argument("--chrome-bin", type=Path, default=None, help="Path to Chrome/Chromium binary")
    parser.add_argument("--max-mb", type=float, default=20.0, help="QA gate max file size in MB (default: 20.0)")
    parser.add_argument("--upload-kit", type=Path, default=DEFAULT_STUDIO_KIT, help="Path to Studio upload kit Markdown")
    parser.add_argument("--force", action="store_true", help="Regenerate cached Gemini art/TTS/music")
    args = parser.parse_args()

    validate_runtime_args(args.tempo, args.tts_workers, args.max_mb)
    for bin_name in ("ffmpeg", "ffprobe"):
        if not shutil.which(bin_name):
            raise RuntimeError(f"Required binary not found on PATH: {bin_name}")

    chrome_bin = resolve_chrome_bin(args.chrome_bin)
    slate_data = json.loads(args.slate.read_text(encoding="utf-8"))
    cuts_map = build_cuts_from_slate(args.slate)

    if args.cut == "all":
        selected = list(cuts_map.values())
    elif args.cut == "games":
        selected = [c for cid, c in cuts_map.items() if cid != "supercut"]
    elif args.cut in cuts_map:
        selected = [cuts_map[args.cut]]
    else:
        raise ValueError(f"Unknown --cut {args.cut!r}; available: {sorted(cuts_map.keys()) + ['games', 'all']}")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip() or None
    music_path, music_source = generate_music_bed(api_key, SHARED_MUSIC_DIR, args.force)

    qa_reports: dict[str, dict[str, Any]] = {}
    for cut in selected:
        _, qa = render_cut(
            cut=cut,
            api_key=api_key,
            music_path=music_path,
            music_source=music_source,
            chrome_bin=chrome_bin,
            tempo_override=args.tempo,
            force=args.force,
            tts_workers=args.tts_workers,
            tts_backend=args.tts_backend,
            allow_say_fallback=args.allow_say_fallback,
            max_mb=args.max_mb,
            slate_data=slate_data,
        )
        qa_reports[cut.cut_id] = qa

    write_studio_upload_kit(list(cuts_map.values()), args.upload_kit, qa_reports=qa_reports)
    DEFAULT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_MANIFEST.write_text(json.dumps(qa_reports, indent=2) + "\n", encoding="utf-8")
    print(f"\n[kit]   Wrote Studio upload kit -> {args.upload_kit}")
    print(f"[qa]    Wrote QA manifest       -> {DEFAULT_MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
