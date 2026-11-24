---
name: nfl-game-scout
description: >-
  Run a comprehensive, current-season NFL matchup scouting dossier or live
  in-game check-in. Use whenever the user asks how/why either team wins an NFL
  game, wants a primetime/weekly NFL preview, asks about weather/altitude
  physics, roster age fatigue, officiating crew tendencies, 30-day social media
  sentiment, full GM-to-assistant-position-coach matchups, crazy freak-play
  surprise calls, or live box-score/play-by-play game updates. Designed to be
  model-agnostic for side-by-side multi-model comparison across Gemini, Claude,
  and other AI models.
---

# NFL Game Scout

Produce sharp, current-season, evidence-backed NFL scouting dossiers and live
in-game check-ins. Every claim about rosters, trades, injuries, coaching
staffs, betting lines, weather, and officiating crews **must be verified via
live web/browser lookup for the current season and date** — never rely on stale
training-data rosters or last season's coordinators.

## Hard Guardrails

1. **Zero Stale Roster Assumptions:** Always verify the current date, exact
   matchup, venue (watch for International Series / neutral-site games!),
   current-season QBs, mid-season trades, coordinator changes, and official
   inactives/injury reports before writing analysis.
2. **Respect Platform Boundaries:** Never bypass login walls, paywalls, or
   CAPTCHAs on X, Instagram, or sports sites. When social platforms gate posts
   behind sign-in, combine web-indexed social coverage, beat-writer quotes,
   public previews, and official team accounts, and clearly state what was
   publicly observable vs. gated.
3. **Separate Verified Facts from Speculation:** Label probabilities,
   model leans, and the "Crazy Surprise Call" clearly as projections grounded
   in verified ingredients.
4. **Stay Tight & Scannable:** Lead with high-signal insights, structured
   tables, and concrete numbers (EPA/PPG/yards/penalty deltas/ages). Avoid
   generic sports-cliché filler ("whoever wants it more", "win the turnover
   battle" without naming *why* or *who*).

---

## Mode Router

Determine which mode the user wants (or combine them when asked):

* **Mode A — Full Pregame Scouting Dossier** *(default for upcoming games)*:
  Execute Modules **1 through 7** below and finish with the **Model Arena
  Scorecard** (`references/model-arena-template.md`).
* **Mode B — Live In-Game Check-In** *(user mentions a quarter, live stats,
  or "what just happened")*: Execute **Live Check-In Protocol** below.
* **Mode C — Deep-Dive Single Module** *(user asks specifically about refs,
  altitude/age, kicking range, coaching staff, or 30-day sentiment)*: Run only
  the requested module at maximum depth.

---

## Mode A: Full Pregame Scouting Dossier (7 Modules)

Run research searches in parallel batches to keep latency low.

### Module 1: Matchup Snapshot & Dual Win Scripts ("How & Why")
Verify and report:
* Teams, current records, division standings, kickoff time, broadcast, **exact
  venue** (flag international/neutral-site stadiums), betting spread, moneyline,
  and over/under (plus public vs. sharp split if available).
* Official inactives (if within 90 minutes of kickoff) or latest injury report,
  highlighting returning stars and missing starters.
* **How & Why Team A Wins** — split into:
  * *Why (Structural Edge):* Verified current-season stat mismatches, scheme
    edges, health edges.
  * *How (Winning Script):* 3 concrete game-flow checkpoints that must occur.
* **How & Why Team B Wins** — same *Why (Structural Edge)* + *How (Winning
  Script)* format.

### Module 2: Weather, Venue & Physics Check
Verify kickoff-to-final-whistle forecast for the exact stadium:
* Temperature, wind speed & direction (note stadium swirl/open ends),
  precipitation probability, humidity, playing surface (grass vs. turf), and
  **elevation**.
* **Altitude / Environmental Physics:** For games above 5,000 ft (Denver,
  Mexico City, etc.), compute approximate kicking carry boost (+4–5 yds in
  Denver; +6–8 yds in Mexico City), ball flight effects, conditioning impact,
  and compare each team's travel/acclimation prep (early altitude camp vs.
  fly-in/fly-out / hypoxic chambers).
* Include each active kicker's verified career long and realistic stadium
  ceiling tonight.
* **Venue Promotions, Uniform Themes & Facility Quirks:** Check if tonight is a
  themed night (e.g., stadium-wide "Blackout" / "Whiteout" or throwback
  uniforms), whether the stadium has an infrastructure or physical reputation
  (sun-glare window, aging electrical grid, roof/clock/turf glitches), and
  whether any freak streak has followed either team recently.

### Module 3: Roster Age & Conditioning Profile
* Compare current-season **53-man average roster ages** for both teams.
* List key **30+ veterans** playing tonight, distinguishing **high-cardio /
  high-contact roles** (300+ lb linemen, every-down edge rushers, WRs/RBs
  returning from recent soft-tissue/Achilles/hamstring layoffs) from
  lower-cardio roles (pocket QBs).
* Note where late-game Q3/Q4 fatigue tilts the matchup.

### Module 4: Officiating Crew Scouting Report
* Identify the assigned **Head Referee** and full 7-official crew (check
  Football Zebras / NFL official assignments for the current week).
* Pull current-season & multi-year tendencies:
  * Accepted penalties/game & penalty yards/game vs. NFL average (~12.5/gm).
  * Signature call profile: **offensive holding**, **defensive holding**,
    **DPI**, **roughing the passer**, **false starts / neutral zone**, and
    late-game "swallow the whistle" tendency.
  * Over/Under and home/away lean.
* Tie tendencies directly to tonight's personnel (e.g., flag-happy RTP ref vs.
  blitz-heavy DC; whistle-swallowing holding ref helping a team missing its
  starting LT).

### Module 5: Last-30-Day Social Media & Locker-Room Sentiment Analysis
Read [`references/sentiment-30d.md`](./references/sentiment-30d.md) before
running this module.
* Audit the **last 30 days** across 3 layers for each team:
  1. **Key Players' Public Social Activity & Media Tone:** Public X/Instagram
     previews, official team social feeds, postgame quotes, trade/return buzz,
     cryptic or frustrated posts, injury-return optimism.
  2. **Beat-Writer & Locker-Room Pulse:** Local beat reporting on morale,
     trust in play-calling, hot-seat smoke, chemistry (e.g., new QB-WR
     connection), or fatigue/travel complaints.
  3. **Fanbase & Market Sentiment Trajectory:** 30-day direction (`Surging`,
     `Steady`, `Cautious`, `Volatile`, `Panicking`) + public vs. sharp betting
     divergence.
* Assign each team a **30-Day Sentiment Score (`-5` to `+5`)** and summarize the
  **Sentiment Edge & Contrarian Signal** (e.g., overhyped public darling vs.
  quietly galvanized locker room).

### Module 6: Full Org-Chart Matchup (GM Down to Assistant Position Coaches)
Read [`references/org-chart-matchup.md`](./references/org-chart-matchup.md)
before running this module.
Verify current-season staff directories for both franchises and compare across
all **4 organizational tiers**:
1. **Tier 1 — Front Office / GM:** Roster construction, mid-season trade
   deadline aggression, depth resilience for tonight's injuries.
2. **Tier 2 — Head Coach & Coordinators (HC / OC / DC / STC):** Play-calling
   identity, 4th-down & 2-point aggression, scheme chess match, clock/timeout
   management, head-to-head history.
3. **Tier 3 — Position Coaches:** Direct unit-vs-unit coaching battles:
   * Passing: `QB Coach + WR/TE Coaches` vs. `DB / Secondary Coach + Pass-Game Coord.`
   * Trenches: `OL Coach` vs. `DL Coach / Pass-Rush Specialist`
   * Ground/Box: `RB Coach / Run-Game Coord.` vs. `LB Coach`
4. **Tier 4 — Assistant Position Coaches, Specialists & Quality Control:**
   Assistant OL/DL/DB/WR/LB coaches, special teams assistants, and **cross-staff
   coaching-tree intelligence** (any assistant who previously coached on the
   opposing staff or shares the exact scheme family and knows cadences,
   protections, or blitz tells).
* Deliver a **Tier-by-Tier Advantage Table** (`Team A`, `Team B`, or `Even`)
  and name the **#1 Hidden Coaching Mismatch** below the coordinator level.

### Module 7: The "Crazy Surprise Story" (Freak-Play Prediction)
Read [`references/freak-play-catalog.md`](./references/freak-play-catalog.md)
before running this module.
* Synthesize **3+ real game-specific ingredients** from Modules 1–6 (venue
  altitude/wind, retiring or booming specialist, aggressive 4th-down coach,
  historical quirk between these two teams, referee blind spot, depleted
  position group, rare NFL rulebook edge case, plus **narrative irony &
  poetic symmetry** such as "Blackout"/"Whiteout" stadium themes, active
  team curses/streaks, venue quirks, revenge/reunion storylines, and farewell
  milestones).
* **Narrative-First Selection Rule:** When multiple plausible candidates are
  backed by 3+ verified ingredients, **always prioritize the one that makes the
  best, most poetic, most viral narrative** (the *"you cannot script this"*
  story — like a literal stadium power outage on a team's promotional
  "Blackout" night two weeks after a lighting failure in Madrid).
* Pitch **one headline Crazy Surprise Call** + **one bonus sleeper**, each
  with:
  * **The Narrative Hook:** Why *this* storyline is the most poetic/viral script
    on the board.
  * The exact scenario / rule mechanic (e.g., fair-catch free kick, fake-punt
    pass by a retiring punter, defensive "pick-two" 2-pt return, 68+ yd record
    FG attempt, 1-point safety, zero-punt shootout, backup-QB gadget package,
    venue power/clock/sun-glare chaos).
  * Why *this exact game* makes it plausibly live.
  * Realistic probability estimate (e.g., `~4%`, `~10%`, `<1%`).

---

## Mode B: Live In-Game Check-In Protocol

When checking in during a live game (Q1, Halftime, Q3, Q4):
1. Pull the live **ESPN / NFL.com Box Score, Team Stats, and Play-by-Play**
   (use browser inspection for live JS-rendered scoreboards when available;
   fall back to web search if browser tools are unavailable).
2. Report:
   * Exact score, quarter, game clock, possession, and scoring summary.
   * Compact **Team Stats table** (total yds, net pass, rush, 1st downs, 3rd
     down, TOP, turnovers, sacks, penalties).
   * Key individual stat lines + any **live in-game injuries**.
   * Current stadium weather update (if conditions changed).
3. **Win-Script & Referee Audit:** Explicitly check which pregame win script
   is materializing, whether the ref crew is living up to its flag profile,
   and whether altitude/age fatigue or special-teams madness is showing up.

---

## Multi-Model Arena Output Block

Always conclude a Mode A Full Pregame Dossier with the standardized
**Model Arena Scorecard** from
[`references/model-arena-template.md`](./references/model-arena-template.md) so
the user can run the same matchup across different models (Gemini, Claude,
GPT, etc.) and grade accuracy + creativity after the final whistle.
