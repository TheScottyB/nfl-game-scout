# NFL Game Scout

A cross-platform **Agent Plugin & Skill** for **Google Antigravity**, **Claude Desktop**, **Claude Code**, and any model/harness supporting the open `SKILL.md` format.

Built to run deep, current-season NFL game scouting dossiers — and designed specifically so you can run the **exact same scouting protocol across different AI models** (Gemini, Claude, GPT, etc.) and score their predictions side-by-side after the final whistle.

> **Origin story:** Born during Week 11 SNF in Mexico City (Vikings vs. 49ers, Nov 22, 2026), where this workflow predicted a 35–10 game script *and* pegged Will Reichard's thin-air ceiling at 68–70 yards while calling out the rare fair-catch free kick rule — right before Reichard lined up a **71-yard fair-catch free kick** at `0:03` of Q2 that flew dead center and came up **1 yard short of the crossbar**.

---

## What It Does (7 Modules + Live Mode)

1. **Matchup Snapshot & Dual Win Scripts ("How & Why"):** Live-verified records, spreads/totals, official inactives, and separate *Structural Edge (Why)* + *Winning Game Script (How)* blueprints for both teams.
2. **Weather, Venue & Altitude Physics:** Stadium forecast, wind/surface impact, elevation carry math (Denver/Mexico City kicking ranges), and team travel/acclimation prep.
3. **Roster Age & Fatigue Profile:** 53-man average roster ages plus high-mileage 30+ veterans at physically taxing positions (trenches, pass rush, injury returns).
4. **Officiating Crew Scouting Report:** Assigned head referee & full crew, accepted penalties/game vs. NFL average, holding/DPI/RTP/false-start tendencies, and late-game whistle profile.
5. **Last-30-Day Social Media & Locker-Room Sentiment:** 30-day sentiment score (`-5` to `+5`), trajectory label, public player/team social tone, beat-writer pulse, and public vs. sharp money divergence.
6. **Full Org-Chart Matchup (GM → Assistant Position Coaches):** 4-tier breakdown covering General Managers, Head Coach & Coordinators, Position Coaches (unit vs. unit), and Assistant Position Coaches / Quality Control + cross-staff coaching-tree tells.
7. **The "Crazy Surprise Story" (Freak-Play Prediction):** One headline rare-rule or freak-play prediction (fair-catch free kick, fake-punt pass, pick-two, record FG, zero-punt shootout, etc.) grounded in 3+ verified ingredients from the game, plus a realistic probability estimate.
8. **Live In-Game Check-In Mode & Multi-Model Arena Scorecard:** Mid-game Q1/Halftime/Q3/Q4 box-score refreshes and a standardized 100-point grading rubric for comparing models.

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
3. Go to **Customize → Skills → `+` → Upload a skill** and select `dist/nfl-game-scout.zip`.
4. Toggle **nfl-game-scout** **ON**.

### Option B — Claude Code / Claude Desktop Plugin Marketplace
Install directly from GitHub or your local clone:

```bash
claude plugin marketplace add TheScottyB/nfl-game-scout
claude plugin install nfl-game-scout@nfl-game-scout
```

---

## Running a Multi-Model Arena Comparison

Give the exact same prompt to two or more models with the `nfl-game-scout` skill loaded:

> *"Use nfl-game-scout to run a full pregame scouting dossier on tonight's NFL game, including the 30-day social media sentiment, full GM-to-assistant-position-coach matchup, ref crew, crazy surprise call, and Model Arena Scorecard."*

Every model outputs the same compact **Model Arena Scorecard** at the bottom so you can track and grade them out of 100 points after the final whistle (see [`skills/nfl-game-scout/references/model-arena-template.md`](./skills/nfl-game-scout/references/model-arena-template.md)).

---

## Repository Structure

```text
nfl-game-scout/
├── plugin.json                                     # Antigravity plugin manifest
├── .claude-plugin/
│   ├── plugin.json                                 # Claude Code / Desktop plugin manifest
│   └── marketplace.json                            # Claude plugin marketplace entry
├── assets/
│   └── logo.svg                                    # High-contrast plugin icon
├── skills/
│   └── nfl-game-scout/
│       ├── SKILL.md                                # Core cross-platform skill instructions
│       └── references/
│           ├── org-chart-matchup.md                # 4-tier GM → Asst. Position Coach guide
│           ├── sentiment-30d.md                    # 30-day social & locker-room sentiment rubric
│           ├── freak-play-catalog.md               # Grounded crazy-surprise-play catalog
│           └── model-arena-template.md             # Standardized scorecard & 100-pt grading rubric
├── scripts/
│   └── pack-claude-skill.sh                        # Builds dist/nfl-game-scout.zip for Claude Desktop
├── install.sh                                      # Symlinks plugin into Antigravity
└── tests/
    └── test_plugin_structure.py                    # Validates manifests, SKILL.md, refs, and ZIP layout
```

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```
