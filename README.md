# NFL Game Scout

A cross-platform **Agent Plugin & Skill** + **Multi-Model Arena Dashboard** + **YouTube Shorts Video Generator** for **Google Antigravity**, **Claude Desktop**, **Claude Code**, and any model supporting the open `SKILL.md` format.

* **Live Pre-Kickoff Receipts & Arena:** https://thescottyb.github.io/nfl-game-scout/
* **Public GitHub Repo:** https://github.com/TheScottyB/nfl-game-scout

> **Origin story:** Born during Week 11 SNF in Mexico City (Vikings vs. 49ers, Nov 22, 2026), where this workflow predicted a 35–10 game script *and* pegged Will Reichard's thin-air ceiling at 68–70 yards while calling out the rare fair-catch free kick rule — right before Reichard lined up a **71-yard fair-catch free kick** at `0:03` of Q2 that flew dead center and came up **1 yard short of the crossbar**.

---

## What It Does (7 Modules + Live Mode + Shorts + Pages)

1. **Matchup Snapshot & Dual Win Scripts ("How & Why"):** Live-verified records, spreads/totals, official inactives, and separate *Structural Edge (Why)* + *Winning Game Script (How)* blueprints for both teams.
2. **Weather, Venue & Altitude Physics:** Stadium forecast, wind/surface impact, elevation carry math (Denver/Mexico City kicking ranges), and team travel/acclimation prep.
3. **Roster Age & Fatigue Profile:** 53-man average roster ages plus high-mileage 30+ veterans at physically taxing positions (trenches, pass rush, injury returns).
4. **Officiating Crew Scouting Report:** Assigned head referee & full crew, accepted penalties/game vs. NFL average, holding/DPI/RTP/false-start tendencies, and late-game whistle profile.
5. **Last-30-Day Social Media & Locker-Room Sentiment:** 30-day sentiment score (`-5` to `+5`), trajectory label, public player/team social tone, beat-writer pulse, and public vs. sharp money divergence.
6. **Full Org-Chart Matchup (GM → Assistant Position Coaches):** 4-tier breakdown covering General Managers, Head Coach & Coordinators, Position Coaches (unit vs. unit), and Assistant Position Coaches / Quality Control + cross-staff coaching-tree tells.
7. **The "Crazy Surprise Story" (Freak-Play Prediction):** One headline rare-rule or freak-play prediction grounded in 3+ verified ingredients from the game, plus a realistic probability estimate.
8. **GitHub Pages Arena & Automated 9:16 YouTube Shorts:** Commit pre-kickoff predictions to `docs/data/`, publish verified receipts to GitHub Pages, and render 1080×1920 vertical Shorts with `scripts/render_shorts.py`.

---

## Install in Google Antigravity

Clone the repo and run `./install.sh` to symlink it directly into `~/.gemini/config/plugins/nfl-game-scout`:

```bash
git clone https://github.com/TheScottyB/nfl-game-scout.git
cd nfl-game-scout
./install.sh
```

The plugin activates automatically on your next turn.

---

## Install in Claude Desktop & Claude Code

### Option A — Claude Desktop App (Skill ZIP Upload)
1. Build the Claude Desktop skill zip (ensures the required `nfl-game-scout/SKILL.md` root folder):
   ```bash
   ./scripts/pack-claude-skill.sh
   ```
2. Open **Claude Desktop** → **Settings → Capabilities** and confirm **Code execution and file creation** is **ON** (plus **Web search** enabled for live NFL lookups).
3. Go to **Customize → Skills → `+` → Upload a skill**, select `dist/nfl-game-scout.zip`, and toggle **nfl-game-scout** **ON**.

### Option B — Claude Code / Desktop Plugin Marketplace
Install directly from GitHub or your local clone:

```bash
claude plugin marketplace add TheScottyB/nfl-game-scout
claude plugin install nfl-game-scout@nfl-game-scout
```

---

## Running Multi-Model Arena & Rendering YouTube Shorts

```bash
# 1. Print ready-to-paste prompt for Claude Desktop / GPT
python3 scripts/arena_cli.py prompt --slate 2026-thanksgiving

# 2. Ingest another model's JSON predictions into the Arena
python3 scripts/arena_cli.py ingest --slate 2026-thanksgiving --file claude_predictions.json

# 3. Validate all Arena slates
python3 scripts/arena_cli.py validate

# 4. Render Studio v2 1080x1920 YouTube Shorts (Gemini dual-host TTS + 9:16 art + 2-pass loudnorm + QA gate)
python3 scripts/render_studio_short.py --slate docs/data/2026-thanksgiving.json --cut all

# 5. Or render lightweight offline v1 Shorts (macOS Chrome + say + ffmpeg)
python3 scripts/render_shorts.py --data docs/data/2026-thanksgiving.json --out-dir shorts/output --all --supercut
```

---

## Repository Structure

```text
nfl-game-scout/
├── docs/                                           # GitHub Pages Arena Dashboard
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   ├── favicon.svg
│   └── data/
│       ├── slates.json
│       ├── 2026-thanksgiving.json                  # Active Thanksgiving 3-game slate + live Studio Short links
│       └── 2026-week11-archive.json                # SNF Mexico City (91/100) + MNF receipts
├── scripts/
│   ├── arena_cli.py                                # Validate slates, generate prompts, ingest models
│   ├── render_studio_short.py                      # Studio v2 Shorts (dual-host Gemini TTS, art, HUD, QA gate)
│   ├── render_shorts.py                            # Offline lightweight v1 Shorts renderer
│   └── pack-claude-skill.sh                        # Builds dist/nfl-game-scout.zip for Claude Desktop
├── shorts/
│   ├── YOUTUBE_UPLOAD_KIT.md                       # v1 upload metadata
│   └── YOUTUBE_STUDIO_UPLOAD_KIT.md                # Auto-generated Studio v2 titles, descriptions & QA stats
├── skills/nfl-game-scout/                          # Core cross-platform skill + 4 reference guides
└── tests/                                          # Stdlib unittest verification suite
```

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```
