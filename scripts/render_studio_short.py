#!/usr/bin/env python3
"""Render NotebookLM-style Studio YouTube Shorts (9:16, 1080x1920, 30fps).

Supports all 4 Thanksgiving 2026 cuts:
  - supercut          -> shorts/output/00-thanksgiving-2026-all-3-freak-plays-studio.mp4
  - bears-at-lions    -> shorts/output/01-bears-at-lions-thanksgiving-2026-studio.mp4
  - eagles-at-cowboys -> shorts/output/02-eagles-at-cowboys-thanksgiving-2026-studio.mp4
  - chiefs-at-bills   -> shorts/output/03-chiefs-at-bills-thanksgiving-2026-studio.mp4
  - all               -> renders all 4 cuts

Pipeline (zero external Python dependencies — stdlib + Chrome + ffmpeg + Gemini API):
  1. Load cached 9:16 editorial artwork plates (or generate via `gemini-nano-banana-2.1`).
  2. Generate dual-host sports-desk banter audio per scene via `gemini-3.8-flash-tts` (Puck + Kore).
  3. Generate/cache instrumental primetime NFL broadcast underscore (`lyria-3.5` or 118-BPM synth).
  4. Render transparent-background 1080x1920 RGBA broadcast HUD + caption PNGs in headless Chrome.
  5. Animate artwork with smooth Ken Burns `zoompan`, composite HUD overlays, stitch with `xfade`,
     and sidechain-duck the music bed beneath the hosts at -14 LUFS.
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
import urllib.error
import urllib.request
import wave

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "shorts" / "output"
ASSETS_ROOT = OUTPUT_DIR / "studio_assets"
SHARED_MUSIC_DIR = ASSETS_ROOT / "00-thanksgiving-2026"
CHROME_BIN = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"

WIDTH = 1080
HEIGHT = 1920
FPS = 30
XFADE_SEC = 0.30
VOICE_LEAD_SEC = 0.12
VOICE_TAIL_SEC = 0.35
FINAL_TAIL_SEC = 0.75


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
    scenes: tuple[StudioScene, ...]


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

CUTS: dict[str, StudioCut] = {
    "supercut": StudioCut(
        cut_id="supercut",
        output_filename="00-thanksgiving-2026-all-3-freak-plays-studio.mp4",
        asset_subdir="00-thanksgiving-2026",
        default_tempo=1.06,
        scenes=SUPERCUT_SCENES,
    ),
    "bears-at-lions": StudioCut(
        cut_id="bears-at-lions",
        output_filename="01-bears-at-lions-thanksgiving-2026-studio.mp4",
        asset_subdir="01-bears-at-lions",
        default_tempo=1.07,
        scenes=BEARS_LIONS_SCENES,
    ),
    "eagles-at-cowboys": StudioCut(
        cut_id="eagles-at-cowboys",
        output_filename="02-eagles-at-cowboys-thanksgiving-2026-studio.mp4",
        asset_subdir="02-eagles-at-cowboys",
        default_tempo=1.07,
        scenes=EAGLES_COWBOYS_SCENES,
    ),
    "chiefs-at-bills": StudioCut(
        cut_id="chiefs-at-bills",
        output_filename="03-chiefs-at-bills-thanksgiving-2026-studio.mp4",
        asset_subdir="03-chiefs-at-bills",
        default_tempo=1.07,
        scenes=CHIEFS_BILLS_SCENES,
    ),
}

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
            # Fail fast on hard 0-quota or daily Free-Tier RPD caps; retry transient RPM 429s / 5xx
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


def generate_scene_art(api_key: str, scene: StudioScene, asset_dir: Path, force: bool) -> Path:
    out_png = asset_dir / f"{scene.slug}_art.png"
    if out_png.exists() and not force and out_png.stat().st_size > 20_000:
        print(f"[cache] art  {asset_dir.name}/{scene.slug} -> {out_png.name}")
        return out_png

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
    print(f"[done]  art  {asset_dir.name}/{scene.slug} ({mime_type or 'image/png'}, {out_png.stat().st_size // 1024} KB)")
    return out_png


def generate_scene_tts(api_key: str, scene: StudioScene, asset_dir: Path, force: bool) -> Path:
    out_wav = asset_dir / f"{scene.slug}_vo.wav"
    if out_wav.exists() and not force and out_wav.stat().st_size > 10_000:
        print(f"[cache] tts  {asset_dir.name}/{scene.slug} -> {out_wav.name}")
        return out_wav

    content_items = [
        {
            "type": "text",
            "text": turn.text,
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


def generate_music_bed(api_key: str, music_dir: Path, force: bool) -> tuple[Path, str]:
    music_dir.mkdir(parents=True, exist_ok=True)
    out_music = music_dir / "music_bed.mp3"
    meta_file = music_dir / "music_bed.source.txt"
    if out_music.exists() and not force and out_music.stat().st_size > 20_000:
        src = meta_file.read_text(encoding="utf-8").strip() if meta_file.exists() else "cached"
        print(f"[cache] music -> {out_music.name} ({src})")
        return out_music, src

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
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
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


def render_hud_png(scene: StudioScene, total_scenes: int, asset_dir: Path) -> Path:
    if not CHROME_BIN.is_file():
        raise RuntimeError(f"Google Chrome binary not found at {CHROME_BIN}")
    hud_html = asset_dir / f"{scene.slug}_hud.html"
    hud_png = asset_dir / f"{scene.slug}_hud.png"
    hud_html.write_text(build_hud_html(scene, total_scenes), encoding="utf-8")

    subprocess.run(
        [
            str(CHROME_BIN),
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


def assemble_final_short(
    scene_clips: list[tuple[StudioScene, Path, Path, float]],
    music_path: Path,
    out_mp4: Path,
    tempo: float = 1.07,
) -> float:
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

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for _, clip_path, _, _ in scene_clips:
        cmd.extend(["-i", str(clip_path)])
    n = len(scene_clips)
    for _, _, vo_path, _ in scene_clips:
        cmd.extend(["-i", str(vo_path)])
    music_idx = 2 * n
    cmd.extend(["-stream_loop", "-1", "-i", str(music_path)])

    filter_parts: list[str] = []
    for i in range(n):
        filter_parts.append(f"[{i}:v]setpts=PTS-STARTPTS,fps={FPS},settb=AVTB,format=yuv420p[v{i}]")

    prev_v = "v0"
    for i in range(n - 1):
        trans = scene_clips[i][0].transition_out
        off = offsets[i + 1]
        nxt = f"vx{i}" if i < n - 2 else "vfinal"
        filter_parts.append(
            f"[{prev_v}][v{i + 1}]xfade=transition={trans}:duration={XFADE_SEC:.2f}:offset={off:.3f}[{nxt}]"
        )
        prev_v = nxt

    vo_labels: list[str] = []
    tempo_filter = f",atempo={tempo:.3f}" if abs(tempo - 1.0) > 0.005 else ""
    for i in range(n):
        delay_ms = max(0, int(round((offsets[i] + VOICE_LEAD_SEC) * 1000)))
        lbl = f"vo{i}"
        filter_parts.append(
            f"[{n + i}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"highpass=f=75{tempo_filter},adelay={delay_ms}|{delay_ms}[{lbl}]"
        )
        vo_labels.append(f"[{lbl}]")

    filter_parts.append(
        f"{''.join(vo_labels)}amix=inputs={n}:duration=longest:dropout_transition=0:normalize=0,"
        f"apad=whole_dur={total_dur:.3f},atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS,"
        f"asplit=2[vo_main][vo_sc]"
    )

    fade_out_start = max(0.0, total_dur - 1.35)
    filter_parts.append(
        f"[{music_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS,"
        f"volume=-15dB,afade=t=in:st=0:d=0.35,afade=t=out:st={fade_out_start:.3f}:d=1.35[music_pre]"
    )
    filter_parts.append(
        "[music_pre][vo_sc]sidechaincompress=threshold=0.025:ratio=6:attack=15:release=260:makeup=1[music_ducked]"
    )
    filter_parts.append(
        "[vo_main][music_ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        "loudnorm=I=-14:TP=-1.5:LRA=11[afinal]"
    )

    cmd.extend(
        [
            "-filter_complex", ";".join(filter_parts),
            "-map", "[vfinal]",
            "-map", "[afinal]",
            "-t", f"{total_dur:.3f}",
            "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(out_mp4),
        ]
    )
    subprocess.run(cmd, check=True)
    return probe_duration(out_mp4)


def render_cut(
    cut: StudioCut,
    api_key: str,
    music_path: Path,
    music_source: str,
    tempo_override: float | None,
    force: bool,
    tts_workers: int,
) -> Path:
    asset_dir = ASSETS_ROOT / cut.asset_subdir
    asset_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = OUTPUT_DIR / cut.output_filename
    tempo = tempo_override if tempo_override is not None else cut.default_tempo

    print(f"\n================ RENDERING CUT: {cut.cut_id} ================")
    art_map: dict[int, Path] = {}
    tts_map: dict[int, Path] = {}

    with ThreadPoolExecutor(max_workers=tts_workers) as pool:
        futures = {}
        for scene in cut.scenes:
            futures[pool.submit(generate_scene_art, api_key, scene, asset_dir, force)] = ("art", scene.index)
            futures[pool.submit(generate_scene_tts, api_key, scene, asset_dir, force)] = ("tts", scene.index)
        for fut in as_completed(futures):
            kind, idx = futures[fut]
            res = fut.result()
            if kind == "art":
                art_map[idx] = res
            else:
                tts_map[idx] = res

    vo_durs = [probe_duration(tts_map[s.index]) / tempo for s in cut.scenes]
    tail_pads = [VOICE_TAIL_SEC] * (len(cut.scenes) - 1) + [FINAL_TAIL_SEC]
    hud_map = {s.index: render_hud_png(s, len(cut.scenes), asset_dir) for s in cut.scenes}

    with tempfile.TemporaryDirectory(prefix=f"studio_{cut.cut_id}_") as td:
        tmp_dir = Path(td)
        scene_clips: list[tuple[StudioScene, Path, Path, float]] = []
        for scene, vo_dur, pad in zip(cut.scenes, vo_durs, tail_pads):
            clip_dur = VOICE_LEAD_SEC + vo_dur + pad
            clip_path = render_scene_clip(
                scene,
                art_map[scene.index],
                hud_map[scene.index],
                clip_dur,
                tmp_dir,
            )
            scene_clips.append((scene, clip_path, tts_map[scene.index], clip_dur))

        final_dur = assemble_final_short(scene_clips, music_path, out_mp4, tempo=tempo)

    size_mb = out_mp4.stat().st_size / (1024 * 1024)
    print(f"=== READY: {cut.cut_id} ===")
    print(f"Output:   {out_mp4}")
    print(f"Duration: {final_dur:.2f}s | Size: {size_mb:.2f} MB | Music: {music_source}")
    return out_mp4


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Studio v2 Thanksgiving NFL Game Scout Shorts")
    parser.add_argument(
        "--cut",
        choices=[*CUTS.keys(), "games", "all"],
        default="supercut",
        help="Which Studio cut(s) to render (default: supercut; 'games' renders 01-03)",
    )
    parser.add_argument("--tempo", type=float, default=None, help="Override voice playback tempo")
    parser.add_argument("--tts-workers", type=int, default=3, help="Concurrent TTS/art workers")
    parser.add_argument("--force", action="store_true", help="Regenerate cached Gemini art/TTS/music")
    args = parser.parse_args()

    for bin_name in ("ffmpeg", "ffprobe"):
        if not shutil.which(bin_name):
            raise RuntimeError(f"Required binary not found on PATH: {bin_name}")

    api_key = require_env_api_key()
    music_path, music_source = generate_music_bed(api_key, SHARED_MUSIC_DIR, args.force)

    if args.cut == "all":
        selected = list(CUTS.values())
    elif args.cut == "games":
        selected = [CUTS["bears-at-lions"], CUTS["eagles-at-cowboys"], CUTS["chiefs-at-bills"]]
    else:
        selected = [CUTS[args.cut]]

    for cut in selected:
        render_cut(cut, api_key, music_path, music_source, args.tempo, args.force, args.tts_workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
