#!/usr/bin/env python3
"""Render 9:16 YouTube Shorts (.mp4) and upload metadata directly from NFL Game Scout JSON slates.

Uses only Python stdlib + Google Chrome headless + macOS `say` + `ffmpeg`/`ffprobe`.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = REPO_ROOT / "docs" / "data" / "2026-thanksgiving.json"
DEFAULT_OUT_DIR = REPO_ROOT / "shorts" / "output"
DEFAULT_UPLOAD_KIT = REPO_ROOT / "shorts" / "YOUTUBE_UPLOAD_KIT.md"
CHROME_BIN = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

SAFE_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SAFE_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
SAFE_VOICE_RE = re.compile(r"^[A-Za-z0-9 ()_-]+$")
MAX_YT_TITLE_LEN = 100

WIDTH = 1080
HEIGHT = 1920
FPS = 30
XFADE_SEC = 0.35


@dataclass
class SlideSpec:
    tag: str
    headline: str
    subline: str
    body_html: str
    caption: str
    narration: str
    accent_a: str
    accent_b: str


def _esc(val: Any) -> str:
    return html.escape(str(val), quote=True)


def _safe_hex_color(raw: Any, fallback: str = "#10b981") -> str:
    if isinstance(raw, str) and SAFE_HEX_COLOR_RE.fullmatch(raw.strip()):
        return raw.strip()
    return fallback


def _safe_slug(raw: Any) -> str:
    if not isinstance(raw, str) or not SAFE_SLUG_RE.fullmatch(raw):
        raise ValueError(f"Unsafe slug identifier: {raw!r} (expected ^[a-z0-9-]+$)")
    return raw


def _safe_output_path(out_dir: Path, filename: str) -> Path:
    resolved_dir = out_dir.resolve()
    candidate = (resolved_dir / filename).resolve()
    if candidate.parent != resolved_dir:
        raise ValueError(f"Output path escapes target directory: {candidate}")
    return candidate


def _validate_cli_voice_and_rate(voice: str, rate: int) -> None:
    if not voice or voice.startswith("-") or not SAFE_VOICE_RE.fullmatch(voice):
        raise ValueError(f"Invalid voice name: {voice!r}")
    if not (120 <= rate <= 300):
        raise ValueError(f"Rate must be between 120 and 300 wpm, got {rate}")


def _check_media_tools() -> None:
    missing: list[str] = []
    if not CHROME_BIN.is_file():
        missing.append(f"Google Chrome ({CHROME_BIN})")
    for tool in ("ffmpeg", "ffprobe", "say"):
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        raise RuntimeError(
            "Missing required rendering tool(s): " + ", ".join(missing)
        )


def _pick_primary_prediction(game: dict[str, Any]) -> dict[str, Any]:
    for pred in game.get("predictions", []):
        if pred.get("status") in {"locked", "graded"} and pred.get("surpriseCall"):
            return pred
    return game["predictions"][0]


def _render_slide_html(spec: SlideSpec, slide_num: int, total_slides: int, footer_label: str) -> str:
    accent_a = _safe_hex_color(spec.accent_a, "#10b981")
    accent_b = _safe_hex_color(spec.accent_b, "#3b82f6")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    width: {WIDTH}px;
    height: {HEIGHT}px;
    overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, sans-serif;
    background: #060b16;
    color: #f8fafc;
  }}
  .canvas {{
    position: relative;
    width: {WIDTH}px;
    height: {HEIGHT}px;
    padding: 170px 145px 250px 64px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    background:
      radial-gradient(circle at 15% 15%, {accent_a}44 0%, transparent 45%),
      radial-gradient(circle at 85% 32%, {accent_b}44 0%, transparent 45%),
      linear-gradient(170deg, #070d1a 0%, #0d1629 55%, #060913 100%);
  }}
  .grid-overlay {{
    position: absolute;
    inset: 0;
    background-image:
      linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
    background-size: 48px 48px;
    pointer-events: none;
  }}
  .topbar {{
    position: relative;
    z-index: 2;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }}
  .brand-pill {{
    display: inline-flex;
    align-items: center;
    gap: 12px;
    padding: 12px 22px;
    border-radius: 999px;
    background: rgba(16, 185, 129, 0.16);
    border: 2px solid #10b981;
    color: #6ee7b7;
    font-size: 24px;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }}
  .step-pill {{
    padding: 10px 20px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.08);
    border: 2px solid rgba(255, 255, 255, 0.22);
    font-size: 24px;
    font-weight: 800;
    color: #e2e8f0;
  }}
  .main-block {{
    position: relative;
    z-index: 2;
    display: flex;
    flex-direction: column;
    gap: 28px;
    margin-block: auto;
  }}
  .tag {{
    font-size: 28px;
    font-weight: 900;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #fbbf24;
  }}
  h1 {{
    font-size: 68px;
    line-height: 1.06;
    font-weight: 900;
    letter-spacing: -0.02em;
    color: #ffffff;
    text-shadow: 0 6px 24px rgba(0, 0, 0, 0.65);
  }}
  .subline {{
    font-size: 34px;
    line-height: 1.3;
    font-weight: 600;
    color: #cbd5e1;
  }}
  .card {{
    background: rgba(15, 23, 42, 0.88);
    border: 2px solid rgba(148, 163, 184, 0.35);
    border-radius: 28px;
    padding: 32px 34px;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.55);
  }}
  .card-amber {{
    background: linear-gradient(155deg, rgba(69, 26, 3, 0.92), rgba(24, 15, 7, 0.94));
    border: 3px solid #f59e0b;
  }}
  .card-emerald {{
    background: linear-gradient(155deg, rgba(6, 78, 59, 0.9), rgba(10, 25, 41, 0.95));
    border: 3px solid #10b981;
  }}
  .vs-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
  }}
  .team-box {{
    flex: 1;
    padding: 26px 22px;
    border-radius: 22px;
    border: 3px solid rgba(255,255,255,0.2);
    text-align: center;
  }}
  .team-abbr {{
    font-size: 62px;
    font-weight: 900;
    letter-spacing: -0.02em;
  }}
  .team-rec {{
    margin-top: 6px;
    font-size: 28px;
    font-weight: 700;
    color: #e2e8f0;
  }}
  .vs-badge {{
    font-size: 36px;
    font-weight: 900;
    color: #fbbf24;
  }}
  .stat-row {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 16px;
    padding: 16px 0;
    border-bottom: 1px solid rgba(148, 163, 184, 0.25);
    font-size: 32px;
  }}
  .stat-row:last-child {{ border-bottom: 0; padding-bottom: 0; }}
  .stat-label {{ color: #94a3b8; font-weight: 700; font-size: 27px; }}
  .stat-val {{ color: #ffffff; font-weight: 800; text-align: right; }}
  .big-callout {{
    font-size: 40px;
    font-weight: 800;
    line-height: 1.24;
    color: #fef3c7;
  }}
  .bullet-list {{
    margin-top: 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    font-size: 28px;
    line-height: 1.32;
    color: #e2e8f0;
  }}
  .bullet-item {{
    padding: 14px 18px;
    border-radius: 16px;
    background: rgba(255, 255, 255, 0.06);
    border-left: 6px solid #10b981;
  }}
  .caption-bar {{
    position: relative;
    z-index: 2;
    background: rgba(2, 6, 23, 0.92);
    border: 2px solid rgba(52, 211, 153, 0.55);
    border-radius: 22px;
    padding: 22px 28px;
  }}
  .caption-text {{
    font-size: 32px;
    line-height: 1.28;
    font-weight: 800;
    color: #ffffff;
  }}
  .receipt-foot {{
    margin-top: 10px;
    font-size: 22px;
    font-weight: 700;
    color: #6ee7b7;
    letter-spacing: 0.03em;
  }}
</style>
</head>
<body>
  <div class="canvas">
    <div class="grid-overlay"></div>
    <div class="topbar">
      <div class="brand-pill">NFL Game Scout</div>
      <div class="step-pill">{slide_num} / {total_slides}</div>
    </div>
    <div class="main-block">
      <div class="tag">{_esc(spec.tag)}</div>
      <h1>{_esc(spec.headline)}</h1>
      <div class="subline">{_esc(spec.subline)}</div>
      {spec.body_html}
    </div>
    <div class="caption-bar">
      <div class="caption-text">{_esc(spec.caption)}</div>
      <div class="receipt-foot">{_esc(footer_label)}</div>
    </div>
  </div>
</body>
</html>
"""


def build_game_slides(game: dict[str, Any]) -> list[SlideSpec]:
    pred = _pick_primary_prediction(game)
    away = game["away"]
    home = game["home"]
    sc = pred["surpriseCall"]
    sent = game["sentiment30d"]
    ref = game["referee"]
    away_c = _safe_hex_color(away.get("color"), "#0f766e")
    home_c = _safe_hex_color(home.get("color"), "#1d4ed8")

    # Slide 1: Hook + Matchup
    s1_body = f"""
    <div class="card">
      <div class="vs-row">
        <div class="team-box" style="background:{away_c}55; border-color:{away_c};">
          <div class="team-abbr">{_esc(away['abbr'])}</div>
          <div class="team-rec">{_esc(away['record'])}</div>
        </div>
        <div class="vs-badge">@</div>
        <div class="team-box" style="background:{home_c}55; border-color:{home_c};">
          <div class="team-abbr">{_esc(home['abbr'])}</div>
          <div class="team-rec">{_esc(home['record'])}</div>
        </div>
      </div>
      <div style="margin-top:24px;">
        <div class="stat-row"><span class="stat-label">Market Line</span><span class="stat-val">{_esc(game['odds']['spread'])} · O/U {_esc(game['odds']['total'])}</span></div>
        <div class="stat-row"><span class="stat-label">Venue</span><span class="stat-val">{_esc(game['venue']['stadium'])}</span></div>
      </div>
    </div>
    """
    s1 = SlideSpec(
        tag=f"{game['label']}",
        headline=f"{away['abbr']} at {home['abbr']}: The Freak Play Call",
        subline=f"Last Sunday we flagged a 70-yard free kick right before a 71-yarder landed 1 yard short.",
        body_html=s1_body,
        caption=f"{away['name']} ({away['record']}) at {home['name']} ({home['record']}). Locked before kickoff.",
        narration=(
            f"Last Sunday we pinned a 70 yard free kick before a 71 yarder landed one yard short. "
            f"Here is {away['name']} at {home['name']}."
        ),
        accent_a=away_c,
        accent_b=home_c,
    )

    # Slide 2: Refs + Weather/Kicking Physics
    s2_body = f"""
    <div class="card">
      <div class="stat-row"><span class="stat-label">Head Referee</span><span class="stat-val">{_esc(ref['headRef'])}</span></div>
      <div class="stat-row"><span class="stat-label">Flag Profile</span><span class="stat-val">{_esc(ref['flagsPerGame'])}</span></div>
      <div class="stat-row"><span class="stat-label">Weather / Turf</span><span class="stat-val">{_esc(game['weather']['summary'])}</span></div>
      <div class="stat-row"><span class="stat-label">Kicker Ceilings</span><span class="stat-val">{_esc(away['kicker']['name'])} ({away['kicker']['careerLong']}) vs {_esc(home['kicker']['name'])} ({home['kicker']['careerLong']})</span></div>
      <div class="stat-row"><span class="stat-label">Roster Avg Age</span><span class="stat-val">{_esc(away['abbr'])} {away['avgAge']} vs {_esc(home['abbr'])} {home['avgAge']}</span></div>
    </div>
    """
    s2 = SlideSpec(
        tag="Refs, Weather & Physics",
        headline=f"Watch Crew Chief {ref['headRef']}",
        subline=ref["tonightImpact"],
        body_html=s2_body,
        caption=f"Ref {ref['headRef']}: {ref['flagsPerGame']} · {game['weather']['summary']}",
        narration=(
            f"Watch referee {ref['headRef']}, averaging {ref['flagsPerGame']}, "
            f"with {game['weather']['summary']}."
        ),
        accent_a=away_c,
        accent_b=home_c,
    )

    # Slide 3: 30-Day Sentiment & Hidden Coaching Mismatch
    s3_body = f"""
    <div class="card">
      <div class="stat-row"><span class="stat-label">30-Day Sentiment</span><span class="stat-val">{_esc(away['abbr'])} {sent['awayScore']:+.1f} vs {_esc(home['abbr'])} {sent['homeScore']:+.1f}</span></div>
      <div class="stat-row"><span class="stat-label">Mood Arc</span><span class="stat-val">{_esc(sent['awayTrajectory'])} / {_esc(sent['homeTrajectory'])}</span></div>
      <div class="bullet-list">
        <div class="bullet-item"><strong>Hidden Coach Mismatch:</strong> {_esc(game['orgChart']['hiddenMismatch'])}</div>
        <div class="bullet-item"><strong>Cross-Staff Tell:</strong> {_esc(game['orgChart']['crossStaffTells'][0])}</div>
      </div>
    </div>
    """
    s3 = SlideSpec(
        tag="30-Day Sentiment & Org Chart",
        headline="The Hidden Coaching Mismatch",
        subline=f"GM down to assistant position coaches — here is where the staffs clash.",
        body_html=s3_body,
        caption=f"Hidden mismatch: {game['orgChart']['hiddenMismatch']}",
        narration=(
            f"30 day sentiment is {away['abbr']} {sent['awayScore']:+.1f} versus {home['abbr']} {sent['homeScore']:+.1f}. "
            f"Hidden coaching edge: {game['orgChart']['hiddenMismatch'].split('—')[0].strip()}."
        ),
        accent_a=away_c,
        accent_b=home_c,
    )

    # Slide 4: Crazy Surprise Call
    ings_html = "".join(f'<div class="bullet-item">{i+1}. {_esc(ing)}</div>' for i, ing in enumerate(sc["ingredients"][:3]))
    s4_body = f"""
    <div class="card card-amber">
      <div class="big-callout">{_esc(sc['title'])}</div>
      <div class="bullet-list">{ings_html}</div>
    </div>
    """
    s4 = SlideSpec(
        tag=f"Crazy Surprise Call · Est. {sc['probability']}",
        headline="Tonight's Freak-Play Alert",
        subline=sc["detail"],
        body_html=s4_body,
        caption=f"SURPRISE CALL ({sc['probability']}): {sc['title']}",
        narration=(
            f"Now the freak play call at {sc['probability']}: "
            f"{sc['title']}. {sc['ingredients'][0]} meets {sc['ingredients'][1]}."
        ),
        accent_a="#f59e0b",
        accent_b=home_c,
    )

    # Slide 5: Scoreboard Pick & GitHub Receipt
    score = pred["projectedScore"]
    s5_body = f"""
    <div class="card card-emerald">
      <div class="stat-row"><span class="stat-label">Gemini Pick</span><span class="stat-val">{_esc(away['abbr'])} {score['away']} – {_esc(home['abbr'])} {score['home']}</span></div>
      <div class="stat-row"><span class="stat-label">Spread &amp; Total</span><span class="stat-val">{_esc(pred['spreadLean'])} · {_esc(pred['totalLean'])}</span></div>
      <div class="stat-row"><span class="stat-label">Claude Slot</span><span class="stat-val">Open for Multi-Model Duel</span></div>
      <div class="stat-row"><span class="stat-label">Timestamp Proof</span><span class="stat-val">{_esc(pred['publishedAtUtc'])}</span></div>
    </div>
    """
    s5 = SlideSpec(
        tag="Pre-Kickoff Receipt Locked",
        headline=f"Pick: {pred['winnerAbbr']} ({away['abbr']} {score['away']}-{home['abbr']} {score['home']})",
        subline="Every prediction is timestamped on GitHub before kickoff and graded out of 100 tonight.",
        body_html=s5_body,
        caption=f"Who covers {game['odds']['spread']}? Full Arena receipts live on GitHub Pages.",
        narration=(
            f"Final pick: {pred['winnerAbbr']}, {score['home'] if pred['winnerAbbr']==home['abbr'] else score['away']} "
            f"to {score['away'] if pred['winnerAbbr']==home['abbr'] else score['home']}. "
            f"GitHub receipt locked before kickoff."
        ),
        accent_a="#10b981",
        accent_b=home_c,
    )

    return [s1, s2, s3, s4, s5]


def build_supercut_slides(slate: dict[str, Any]) -> list[SlideSpec]:
    games = slate["games"]
    g1, g2, g3 = games[0], games[1], games[2]
    p1, p2, p3 = _pick_primary_prediction(g1), _pick_primary_prediction(g2), _pick_primary_prediction(g3)

    s1 = SlideSpec(
        tag="Thanksgiving Tripleheader · Nov 26, 2026",
        headline="3 NFL Freak Plays Our AI Calls Before Kickoff",
        subline="After pinning a 70-yd free kick before Sunday's 71-yd attempt, here are 3 Thanksgiving bombshells.",
        body_html=f"""
        <div class="card card-emerald">
          <div class="stat-row"><span class="stat-label">1:00 PM ET</span><span class="stat-val">{_esc(g1['away']['abbr'])} ({g1['away']['record']}) @ {_esc(g1['home']['abbr'])} ({g1['home']['record']})</span></div>
          <div class="stat-row"><span class="stat-label">4:30 PM ET</span><span class="stat-val">{_esc(g2['away']['abbr'])} ({g2['away']['record']}) @ {_esc(g2['home']['abbr'])} ({g2['home']['record']})</span></div>
          <div class="stat-row"><span class="stat-label">8:20 PM ET</span><span class="stat-val">{_esc(g3['away']['abbr'])} ({g3['away']['record']}) @ {_esc(g3['home']['abbr'])} ({g3['home']['record']})</span></div>
        </div>
        """,
        caption="All 3 Thanksgiving predictions timestamped on GitHub before kickoff.",
        narration=(
            "After calling a 70 yard free kick right before a 71 yarder landed one yard short on Sunday night, "
            "here are three Thanksgiving freak plays our AI scout locked on GitHub before kickoff."
        ),
        accent_a="#10b981",
        accent_b="#f59e0b",
    )

    game_slides: list[SlideSpec] = []
    for idx, (g, p) in enumerate([(g1, p1), (g2, p2), (g3, p3)], start=1):
        sc = p["surpriseCall"]
        ings = "".join(f'<div class="bullet-item">{_esc(x)}</div>' for x in sc["ingredients"][:2])
        game_slides.append(
            SlideSpec(
                tag=f"Game {idx} · {g['away']['abbr']} @ {g['home']['abbr']} · Est. {sc['probability']}",
                headline=sc["title"],
                subline=f"Ref: {g['referee']['headRef']} · Pick: {p['winnerAbbr']} ({g['away']['abbr']} {p['projectedScore']['away']}-{g['home']['abbr']} {p['projectedScore']['home']})",
                body_html=f"""
                <div class="card card-amber">
                  <div class="big-callout">{_esc(sc['detail'])}</div>
                  <div class="bullet-list">{ings}</div>
                </div>
                """,
                caption=f"Pick: {p['spreadLean']} · Ref {g['referee']['headRef']}",
                narration=(
                    f"Game {idx}, {g['away']['abbr']} at {g['home']['abbr']}: {sc['title']}. "
                    f"Estimated probability {sc['probability']}, with {p['winnerAbbr']} winning."
                ),
                accent_a=_safe_hex_color(g["away"].get("color"), "#f59e0b"),
                accent_b=_safe_hex_color(g["home"].get("color"), "#10b981"),
            )
        )

    s5 = SlideSpec(
        tag="Multi-Model Arena · Gemini vs Claude",
        headline="100-Point Live Grading After Final",
        subline="Run the exact open-source nfl-game-scout skill in Antigravity or Claude Desktop and compare scores.",
        body_html=f"""
        <div class="card card-emerald">
          <div class="stat-row"><span class="stat-label">{_esc(g1['away']['abbr'])} @ {_esc(g1['home']['abbr'])}</span><span class="stat-val">{_esc(p1['winnerAbbr'])} {p1['projectedScore']['home']}-{p1['projectedScore']['away']}</span></div>
          <div class="stat-row"><span class="stat-label">{_esc(g2['away']['abbr'])} @ {_esc(g2['home']['abbr'])}</span><span class="stat-val">{_esc(p2['winnerAbbr'])} {p2['projectedScore']['home']}-{p2['projectedScore']['away']}</span></div>
          <div class="stat-row"><span class="stat-label">{_esc(g3['away']['abbr'])} @ {_esc(g3['home']['abbr'])}</span><span class="stat-val">{_esc(p3['winnerAbbr'])} {p3['projectedScore']['home']}-{p3['projectedScore']['away']}</span></div>
        </div>
        """,
        caption="Which surprise call hits? Receipts: thescottyb.github.io/nfl-game-scout",
        narration=(
            "Every prediction is timestamped on GitHub Pages. Tell us which freak play hits first, "
            "and check back after the final whistle for the 100 point AI leaderboard."
        ),
        accent_a="#10b981",
        accent_b="#3b82f6",
    )

    return [s1, *game_slides, s5]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _ffprobe_duration(path: Path) -> float:
    res = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    return float(res.stdout.strip())


def render_short_video(
    slides: list[SlideSpec],
    out_mp4: Path,
    footer_label: str,
    voice: str,
    rate: int,
    use_music: bool,
) -> float:
    if not CHROME_BIN.is_file():
        raise FileNotFoundError(f"Google Chrome not found at {CHROME_BIN}")

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    total_slides = len(slides)

    with tempfile.TemporaryDirectory(prefix="nfl_short_") as tmp_str:
        tmp = Path(tmp_str)
        slide_clips: list[Path] = []
        slide_wavs: list[Path] = []
        durations: list[float] = []

        for idx, spec in enumerate(slides):
            html_path = tmp / f"slide_{idx}.html"
            png_path = tmp / f"slide_{idx}.png"
            aiff_path = tmp / f"slide_{idx}.aiff"
            raw_wav = tmp / f"slide_{idx}_raw.wav"
            pad_wav = tmp / f"slide_{idx}.wav"
            clip_mp4 = tmp / f"slide_{idx}.mp4"

            html_path.write_text(
                _render_slide_html(spec, idx + 1, total_slides, footer_label),
                encoding="utf-8",
            )

            _run(
                [
                    str(CHROME_BIN),
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--hide-scrollbars",
                    f"--window-size={WIDTH},{HEIGHT}",
                    "--force-device-scale-factor=1",
                    "--virtual-time-budget=1200",
                    f"--screenshot={png_path}",
                    f"file://{html_path}",
                ]
            )

            _run(["say", "-v", voice, "-r", str(rate), "-o", str(aiff_path), spec.narration])
            _run(["ffmpeg", "-y", "-i", str(aiff_path), "-ar", "48000", "-ac", "2", str(raw_wav)])
            raw_dur = _ffprobe_duration(raw_wav)
            clip_dur = max(4.0, round(raw_dur + 0.35, 2))
            frames = int(round(clip_dur * FPS))

            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(raw_wav),
                    "-af",
                    f"apad=whole_dur={clip_dur:.3f}",
                    "-t",
                    f"{clip_dur:.3f}",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    str(pad_wav),
                ]
            )

            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(png_path),
                    "-vf",
                    (
                        f"scale=2160:3840,"
                        f"zoompan=z='min(zoom+0.00035,1.04)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                        f":d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
                        f"format=yuv420p,settb=AVTB"
                    ),
                    "-t",
                    f"{clip_dur:.3f}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "16",
                    "-pix_fmt",
                    "yuv420p",
                    str(clip_mp4),
                ]
            )

            slide_clips.append(clip_mp4)
            slide_wavs.append(pad_wav)
            durations.append(clip_dur)

        # Build xfade + acrossfade filter graph
        cmd: list[str] = ["ffmpeg", "-y"]
        for c in slide_clips:
            cmd.extend(["-i", str(c)])
        for w in slide_wavs:
            cmd.extend(["-i", str(w)])

        n = len(slide_clips)
        total_dur = sum(durations) - (n - 1) * XFADE_SEC
        filters: list[str] = []

        # Video xfade chain
        prev_v = "0:v"
        cum_dur = durations[0]
        for k in range(1, n):
            offset = round(cum_dur - XFADE_SEC, 3)
            out_v = "vout" if k == n - 1 else f"vx{k}"
            filters.append(
                f"[{prev_v}][{k}:v]xfade=transition=slideleft:duration={XFADE_SEC:.2f}:offset={offset:.3f}[{out_v}]"
            )
            prev_v = out_v
            cum_dur = offset + durations[k]

        # Audio acrossfade chain
        prev_a = f"{n}:a"
        for k in range(1, n):
            a_idx = n + k
            out_a = "anar" if k == n - 1 else f"ax{k}"
            filters.append(
                f"[{prev_a}][{a_idx}:a]acrossfade=d={XFADE_SEC:.2f}:c1=tri:c2=tri[{out_a}]"
            )
            prev_a = out_a

        if use_music:
            # Subtle copyright-free synth pulse bed under narration
            bed_expr = (
                "0.045*(0.55*sin(2*PI*110*t)+0.30*sin(2*PI*164.81*t)+0.20*sin(2*PI*220*t))"
                "*(0.65+0.35*sin(2*PI*2.2*t))"
            )
            filters.append(
                f"aevalsrc='{bed_expr}':s=48000:c=stereo:d={total_dur:.3f},"
                f"lowpass=f=520,afade=t=in:st=0:d=0.8,afade=t=out:st={max(0.5, total_dur-1.2):.3f}:d=1.2[bed]"
            )
            filters.append(
                "[anar][bed]amix=inputs=2:duration=first:dropout_transition=0:weights=1 0.30,alimiter=limit=0.95[aout]"
            )
        else:
            filters.append("[anar]alimiter=limit=0.95[aout]")

        cmd.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(FPS),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                str(out_mp4),
            ]
        )
        _run(cmd)

    return _ffprobe_duration(out_mp4)


def _relative_repo_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return path.name


def _build_short_title(game: dict[str, Any], prob: str) -> str:
    away_abbr = game["away"]["abbr"]
    home_abbr = game["home"]["abbr"]
    # Compact multi-part probability descriptions so YouTube Shorts 100-char limit holds cleanly
    pcts = re.findall(r"~?\d+%", str(prob))
    compact_prob = "-".join(pcts) if len(pcts) > 1 else (pcts[0] if pcts else str(prob).strip())
    candidate = f"{away_abbr} vs {home_abbr} Freak Play Call ({compact_prob}) — Locked Before Kickoff! #shorts"
    if len(candidate) <= MAX_YT_TITLE_LEN:
        return candidate
    fallback = f"{away_abbr} vs {home_abbr} Freak Play Call — Locked Before Kickoff! #shorts"
    return fallback[:MAX_YT_TITLE_LEN]


def write_upload_kit(
    kit_path: Path,
    slate: dict[str, Any],
    rendered: list[tuple[str, str, Path, float, dict[str, Any] | None]],
) -> None:
    kit_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# YouTube Shorts Upload Kit — Thanksgiving 2026 Tripleheader",
        "",
        "Every Short below was rendered from `docs/data/2026-thanksgiving.json` and links back to the timestamped GitHub Pages Arena receipt.",
        "",
        "- **Live Receipts & Leaderboard:** https://thescottyb.github.io/nfl-game-scout/",
        "- **Public GitHub Repo:** https://github.com/TheScottyB/nfl-game-scout",
        "",
        "---",
        "",
    ]

    for slug, title, mp4_path, dur, game in rendered:
        rel_mp4 = _relative_repo_path(mp4_path)
        lines.append(f"## {title}")
        lines.append(f"- **File:** `{rel_mp4}` ({dur:.1f}s · 1080×1920 9:16)")
        if game:
            pred = _pick_primary_prediction(game)
            sc = pred["surpriseCall"]
            yt_title = _build_short_title(game, sc["probability"])
            lines.append(f"- **Suggested YouTube Short Title:** `{yt_title}`")
            lines.append("- **Description:**")
            lines.append("```text")
            lines.append(
                f"Last Sunday we pegged a 70-yard free kick right before a 71-yarder landed 1 yard short in Mexico City.\n"
                f"Here is our pre-kickoff AI scout for {game['away']['name']} ({game['away']['record']}) at {game['home']['name']} ({game['home']['record']}):\n"
                f"- Pick: {pred['winnerAbbr']} ({game['away']['abbr']} {pred['projectedScore']['away']}-{game['home']['abbr']} {pred['projectedScore']['home']}) | {pred['spreadLean']}\n"
                f"- Ref Crew: {game['referee']['headRef']} ({game['referee']['flagsPerGame']})\n"
                f"- Crazy Surprise Call ({sc['probability']}): {sc['title']}\n\n"
                f"Verify the pre-kickoff timestamp & watch Gemini vs. Claude get graded out of 100 tonight:\n"
                f"https://thescottyb.github.io/nfl-game-scout/\n\n"
                f"#NFL #ThanksgivingFootball #{game['away']['abbr']}vs{game['home']['abbr']} #SportsAnalytics #AI #Shorts"
            )
            lines.append("```")
            lines.append(f"- **Pinned Comment:** `Who covers {game['odds']['spread']} — and does \"{sc['title']}\" actually happen? Full timestamped receipts: https://thescottyb.github.io/nfl-game-scout/`")
        else:
            lines.append("- **Suggested YouTube Short Title:** `3 NFL Thanksgiving Freak Plays Our AI Locked Before Kickoff! #shorts`")
            lines.append("- **Description:**")
            lines.append("```text")
            lines.append(
                "Last Sunday we flagged a 70-yard free kick right before a 71-yard attempt missed by 1 yard.\n"
                "Here are 3 Thanksgiving Day freak-play calls timestamped on GitHub before kickoff:\n"
                "1) CHI @ DET: Eligible-Tackle / Non-QB Trick-Play War + Hochuli Reporting Chaos\n"
                "2) PHI @ DAL: West-Wall Sun-Glare Muff + Brandon Aubrey 66-69 Yd NFL-Record Threat\n"
                "3) KC @ BUF: Alex Kemp Play-Clock Chaos + Cold-Weather 55+ Yd Kick Drama\n\n"
                "Full Multi-Model Arena (Gemini vs. Claude) & 100-pt receipts:\n"
                "https://thescottyb.github.io/nfl-game-scout/\n\n"
                "#NFL #Thanksgiving #NFLPredictions #AI #SportsAnalytics #Shorts"
            )
            lines.append("```")
            lines.append("- **Pinned Comment:** `Which of these 3 Thanksgiving freak plays actually hits today? Live receipts & Gemini vs Claude grading at https://thescottyb.github.io/nfl-game-scout/`")
        lines.append("")

    kit_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render 9:16 NFL Game Scout YouTube Shorts")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--upload-kit", type=Path, default=DEFAULT_UPLOAD_KIT)
    parser.add_argument("--voice", default="Samantha")
    parser.add_argument("--rate", type=int, default=222)
    parser.add_argument("--game", default=None, help="Render a single game ID")
    parser.add_argument("--all", action="store_true", help="Render every game in the slate")
    parser.add_argument("--supercut", action="store_true", help="Render combined Thanksgiving 3-freak-play supercut")
    parser.add_argument("--no-music", action="store_true", help="Disable subtle background synth bed")
    parser.add_argument("--kit-only", action="store_true", help="Regenerate YOUTUBE_UPLOAD_KIT.md without re-rendering MP4s")
    args = parser.parse_args()

    _validate_cli_voice_and_rate(args.voice, args.rate)
    if not args.kit_only:
        _check_media_tools()

    slate = json.loads(args.data.read_text(encoding="utf-8"))
    raw_slug = "thanksgiving-2026" if "thanksgiving" in slate["slateId"] else slate["slateId"]
    slate_slug = _safe_slug(raw_slug)
    footer_label = "thescottyb.github.io/nfl-game-scout · Locked Pre-Kickoff"
    use_music = not args.no_music

    rendered: list[tuple[str, str, Path, float, dict[str, Any] | None]] = []

    if args.supercut:
        super_path = _safe_output_path(args.out_dir, f"00-{slate_slug}-all-3-freak-plays.mp4")
        if args.kit_only:
            dur = _ffprobe_duration(super_path) if super_path.is_file() and shutil.which("ffprobe") else 47.4
        else:
            dur = render_short_video(
                build_supercut_slides(slate),
                super_path,
                footer_label,
                args.voice,
                args.rate,
                use_music,
            )
            print(f"Rendered {super_path} ({dur:.2f}s)")
        rendered.append(("supercut", "All 3 Thanksgiving Freak Plays (Supercut)", super_path, dur, None))

    games_to_render = slate["games"]
    if args.game:
        safe_gid = _safe_slug(args.game)
        games_to_render = [g for g in slate["games"] if g["id"] == safe_gid]
        if not games_to_render:
            raise SystemExit(f"Game ID not found: {args.game}")
    elif not args.all and not args.supercut:
        games_to_render = [slate["games"][0]]

    if args.all or args.game or not args.supercut:
        for idx, game in enumerate(games_to_render, start=1):
            gid = _safe_slug(game["id"])
            filename = f"{idx:02d}-{gid}-{slate_slug}.mp4"
            out_path = _safe_output_path(args.out_dir, filename)
            if args.kit_only:
                dur = _ffprobe_duration(out_path) if out_path.is_file() and shutil.which("ffprobe") else 48.0
            else:
                dur = render_short_video(
                    build_game_slides(game),
                    out_path,
                    footer_label,
                    args.voice,
                    args.rate,
                    use_music,
                )
                print(f"Rendered {out_path} ({dur:.2f}s)")
            rendered.append((gid, f"{game['away']['name']} at {game['home']['name']}", out_path, dur, game))

    write_upload_kit(args.upload_kit, slate, rendered)
    print(f"Wrote YouTube upload kit: {args.upload_kit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
