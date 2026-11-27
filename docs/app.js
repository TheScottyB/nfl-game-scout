const SLATES_INDEX_URL = "./data/slates.json";
const REPO_URL = "https://github.com/TheScottyB/nfl-game-scout";
const SAFE_SLATE_FILE_RE = /^data\/[a-z0-9-]+\.json$/i;

/**
 * Allowlist only HTTPS URLs before assigning to dynamic anchor href attributes.
 */
export function sanitizeHttpsUrl(raw) {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const parsed = new URL(raw);
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

/**
 * Safely insert limited markup using the native Sanitizer API when available,
 * falling back to Trusted-Types-safe plain text assignment when unsupported.
 */
export function safeSetHTML(el, html) {
  if ("setHTML" in Element.prototype) {
    el.setHTML(html, {
      sanitizer: {
        elements: ["p", "b", "i", "strong", "em", "code"],
        attributes: ["class"],
      },
    });
  } else {
    el.textContent = String(html).replace(/<[^>]*>/g, "");
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function formatLocalDateTime(iso) {
  if (!iso) return "Pending";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(d);
}

/* ============================================================================
 * Dark Mode Toggle (following modern-web-guidance dark-mode-toggle rules)
 * ========================================================================== */
function initThemeToggle() {
  const meta = document.querySelector('meta[name="color-scheme"]');
  const btn = document.getElementById("theme-toggle-btn");
  if (!meta || !btn) return;

  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const getSystemScheme = () => (mq.matches ? "dark" : "light");
  const getRenderedScheme = () => {
    if (meta.content === "dark" || meta.content === "light") return meta.content;
    return getSystemScheme();
  };

  btn.addEventListener("click", () => {
    const rendered = getRenderedScheme();
    const target = rendered === "dark" ? "light" : "dark";
    const system = getSystemScheme();

    try {
      if (target !== system) {
        localStorage.setItem("color-scheme", target);
        meta.content = target;
      } else {
        localStorage.removeItem("color-scheme");
        meta.content = "light dark";
      }
    } catch {
      meta.content = target;
    }
  });

  window.addEventListener("storage", (event) => {
    if (event.key !== "color-scheme") return;
    meta.content = event.newValue === "light" || event.newValue === "dark" ? event.newValue : "light dark";
  });
}

/* ============================================================================
 * Tabs (following modern-web-guidance tabs + focusgroup fallback rules)
 * ========================================================================== */
function initTabs() {
  const tablist = document.querySelector('[role="tablist"]');
  if (!tablist) return;
  const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
  const panels = tabs.map((tab) =>
    document.querySelector(`[role="tabpanel"][aria-labelledby="${CSS.escape(tab.id)}"]`)
  );

  function selectTab(nextTab, focusTab = false) {
    for (let i = 0; i < tabs.length; i++) {
      const tab = tabs[i];
      const panel = panels[i];
      const active = tab === nextTab;
      tab.setAttribute("aria-selected", String(active));
      tab.toggleAttribute("focusgroupstart", active);
      if (panel) {
        if (active) {
          panel.hidden = false;
          panel.setAttribute("tabindex", "0");
        } else {
          panel.hidden = "until-found";
          panel.removeAttribute("tabindex");
        }
      }
    }
    nextTab.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (focusTab) nextTab.focus();
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => selectTab(tab));
    tab.addEventListener("focus", () => selectTab(tab));
  }

  if (!Object.hasOwn(HTMLElement.prototype, "focusGroup")) {
    tablist.addEventListener("keydown", (event) => {
      const current = document.activeElement;
      const idx = tabs.indexOf(current);
      if (idx === -1) return;
      let nextIdx = -1;
      if (event.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
      else if (event.key === "ArrowLeft") nextIdx = (idx - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") nextIdx = 0;
      else if (event.key === "End") nextIdx = tabs.length - 1;
      if (nextIdx !== -1) {
        event.preventDefault();
        selectTab(tabs[nextIdx], true);
      }
    });
  }

  document.addEventListener("beforematch", (event) => {
    const panel = event.target.closest?.('[role="tabpanel"]');
    if (!panel) return;
    const labelId = panel.getAttribute("aria-labelledby");
    const matchingTab = tabs.find((t) => t.id === labelId);
    if (matchingTab) selectTab(matchingTab);
  });
}

/* ============================================================================
 * Accordions ("Expand all / Collapse all" via Invoker commands + fallback)
 * ========================================================================== */
function initAccordionCommands() {
  function runToggleAll(button, accordion) {
    const expand = button.getAttribute("aria-expanded") !== "true";
    for (const details of accordion.querySelectorAll(":scope > details")) {
      details.open = expand;
    }
  }

  document.addEventListener(
    "command",
    (event) => {
      if (event.command !== "--toggle-all") return;
      runToggleAll(event.source, event.target);
    },
    { capture: true }
  );

  if (!Object.hasOwn(HTMLButtonElement.prototype, "commandForElement")) {
    document.addEventListener("click", (event) => {
      const btn = event.target.closest?.('[command="--toggle-all"]');
      if (!btn) return;
      const targetId = btn.getAttribute("commandfor");
      const target = targetId ? document.getElementById(targetId) : null;
      if (target) runToggleAll(btn, target);
    });
  }

  document.addEventListener(
    "toggle",
    (event) => {
      if (!event.target.matches?.(".accordion > details")) return;
      const accordion = event.target.parentElement;
      const btn = accordion?.querySelector(':scope [command="--toggle-all"]');
      if (!btn) return;
      const items = accordion.querySelectorAll(":scope > details");
      btn.setAttribute(
        "aria-expanded",
        String(Array.prototype.every.call(items, (d) => d.open))
      );
    },
    { capture: true }
  );
}

/* ============================================================================
 * Rendering Helpers
 * ========================================================================== */
function renderModelCard(game, pred) {
  const isPending = pred.status === "pending";
  const card = el("article", `model-card${isPending ? " is-pending" : ""}`);

  const head = el("div", "model-card-head");
  head.append(el("h3", "", pred.modelLabel));
  const badgeText =
    pred.status === "graded" && pred.grade
      ? `Graded: ${pred.grade.total}/${pred.grade.max}`
      : pred.status === "locked"
        ? "Timestamp Locked"
        : "Pending Run";
  head.append(el("span", `pill ${pred.status === "pending" ? "pill-amber" : "pill-accent"}`, badgeText));
  card.append(head);

  const callout = el("div", "score-callout");
  if (pred.projectedScore) {
    const scoreStr = `${game.away.abbr} ${pred.projectedScore.away} – ${game.home.abbr} ${pred.projectedScore.home}`;
    callout.append(el("span", "score-big", scoreStr));
    callout.append(el("span", "pill", `${pred.spreadLean} · ${pred.totalLean}`));
  } else {
    callout.append(el("span", "score-big", "Awaiting Prediction"));
    callout.append(el("span", "pill", pred.spreadLean));
  }
  card.append(callout);

  if (pred.surpriseCall) {
    const box = el("div", "surprise-spotlight");
    box.append(el("p", "surprise-tag", `Crazy Surprise Call (${pred.surpriseCall.probability})`));
    box.append(el("p", "surprise-title", pred.surpriseCall.title));
    box.append(el("p", "", pred.surpriseCall.detail));
    if (Array.isArray(pred.surpriseCall.ingredients) && pred.surpriseCall.ingredients.length) {
      const ing = el("p", "surprise-ingredients", `Ingredients: ${pred.surpriseCall.ingredients.join(" · ")}`);
      box.append(ing);
    }
    card.append(box);
  }

  const list = el("ul", "kv-list");
  const rows = [
    ["Primary Key", pred.primaryKey],
    ["Ref Call", pred.refCall],
    ["30d Sentiment", pred.sentimentEdge],
    ["Hidden Coach Edge", pred.hiddenMismatch],
  ];
  if (pred.bonusSleeper) {
    rows.push(["Bonus Sleeper", `${pred.bonusSleeper.title} (${pred.bonusSleeper.probability}) — ${pred.bonusSleeper.detail}`]);
  }
  if (pred.grade?.notes) {
    rows.push(["Post-Game Grade", pred.grade.notes]);
  }
  for (const [k, v] of rows) {
    const li = el("li");
    const strong = el("strong", "", `${k}: `);
    li.append(strong, document.createTextNode(v ?? "Pending"));
    list.append(li);
  }
  card.append(list);

  if (pred.modelMeta && typeof pred.modelMeta === "object") {
    const metaRow = el("div", "model-meta-row");
    if (pred.modelMeta.exactModelId) {
      metaRow.append(el("span", "meta-chip", `Model: ${pred.modelMeta.exactModelId}`));
    }
    if (pred.modelMeta.afterKickoff === true) {
      metaRow.append(el("span", "meta-chip", "Run after kickoff"));
    }
    if (pred.modelMeta.provider || pred.modelMeta.harness) {
      const via = [pred.modelMeta.provider, pred.modelMeta.harness].filter(Boolean).join(" / ");
      metaRow.append(el("span", "meta-chip", via));
    }
    if (typeof pred.modelMeta.promptSha256 === "string" && pred.modelMeta.promptSha256.length >= 8) {
      metaRow.append(el("span", "meta-chip", `Prompt #${pred.modelMeta.promptSha256.slice(0, 8)}`));
    }
    if (typeof pred.modelMeta.latencySec === "number") {
      metaRow.append(el("span", "meta-chip", `${pred.modelMeta.latencySec.toFixed(1)}s`));
    }
    const totalTok = pred.modelMeta.usage?.totalTokens ?? pred.modelMeta.usage?.outputTokens;
    if (typeof totalTok === "number") {
      metaRow.append(el("span", "meta-chip", `${totalTok} tok`));
    }
    card.append(metaRow);
  }

  if (pred.publishedAtUtc) {
    const receiptLine = el("p", "matchup-sub");
    receiptLine.append(
      document.createTextNode(`Locked UTC: ${pred.publishedAtUtc} (`),
      document.createTextNode(`${formatLocalDateTime(pred.publishedAtUtc)} local)`)
    );
    const safeReceipt = sanitizeHttpsUrl(pred.receiptUrl);
    if (safeReceipt) {
      const link = el("a", "", " · GitHub Receipt");
      link.href = safeReceipt;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      receiptLine.append(link);
    }
    card.append(receiptLine);
  }

  return card;
}

function makeDetails(title, metaText, buildBody) {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  // NEVER put headings inside <summary> (accessibility rule)
  summary.append(el("span", "summary-label", title));
  if (metaText) summary.append(el("span", "summary-meta", metaText));
  const body = el("div", "details-body");
  buildBody(body);
  details.append(summary, body);
  return details;
}

function renderOrgChartTable(game) {
  const capId = `org-cap-${game.id}`;
  const wrapper = el("div", "table-wrapper");
  wrapper.setAttribute("tabindex", "0");
  wrapper.setAttribute("role", "region");
  wrapper.setAttribute("aria-labelledby", capId);

  const table = el("table", "org-table");
  const caption = el("caption", "", `${game.away.abbr} at ${game.home.abbr} — Full 4-Tier Staff Comparison`);
  caption.id = capId;
  table.append(caption);

  const thead = document.createElement("thead");
  const hrow = document.createElement("tr");
  for (const col of ["Tier", `${game.away.abbr} Staff`, `${game.home.abbr} Staff`, "Edge", "Why It Matters"]) {
    hrow.append(el("th", "", col));
  }
  thead.append(hrow);
  table.append(thead);

  const tbody = document.createElement("tbody");
  for (const row of game.orgChart.tiers) {
    const tr = document.createElement("tr");
    const th = el("th", "", row.tier);
    th.scope = "row";
    tr.append(
      th,
      el("td", "", row.awayStaff),
      el("td", "", row.homeStaff),
      el("td", "", row.edge),
      el("td", "", row.impact)
    );
    tbody.append(tr);
  }
  table.append(tbody);
  wrapper.append(table);
  return wrapper;
}

function renderSentimentTable(game) {
  const capId = `sent-cap-${game.id}`;
  const wrapper = el("div", "table-wrapper");
  wrapper.setAttribute("tabindex", "0");
  wrapper.setAttribute("role", "region");
  wrapper.setAttribute("aria-labelledby", capId);

  const table = el("table", "sentiment-table");
  const caption = el("caption", "", `Last-30-Day Sentiment & Market Pulse`);
  caption.id = capId;
  table.append(caption);

  const thead = document.createElement("thead");
  const hrow = document.createElement("tr");
  for (const col of ["Dimension", game.away.name, game.home.name]) {
    hrow.append(el("th", "", col));
  }
  thead.append(hrow);
  table.append(thead);

  const s = game.sentiment30d;
  const rows = [
    ["30-Day Score", `${s.awayScore > 0 ? "+" : ""}${s.awayScore} / +5`, `${s.homeScore > 0 ? "+" : ""}${s.homeScore} / +5`],
    ["Trajectory", s.awayTrajectory, s.homeTrajectory],
    ["Locker Room Vibe", s.awayVibe, s.homeVibe],
    ["Beat Narrative", s.awayBeat, s.homeBeat],
  ];
  const tbody = document.createElement("tbody");
  for (const [label, a, b] of rows) {
    const tr = document.createElement("tr");
    const th = el("th", "", label);
    th.scope = "row";
    tr.append(th, el("td", "", a), el("td", "", b));
    tbody.append(tr);
  }
  table.append(tbody);
  wrapper.append(table);
  return wrapper;
}

function renderGameCard(game) {
  const card = el("article", "game-card");
  card.id = `game-${game.id}`;

  const top = el("div", "game-top");
  const left = el("div");
  left.append(el("p", "eyebrow", `${game.label} · ${game.broadcast}`));
  left.append(
    el(
      "h3",
      "matchup-title",
      `${game.away.name} (${game.away.record}) at ${game.home.name} (${game.home.record})`
    )
  );
  left.append(
    el(
      "p",
      "matchup-sub",
      `${game.venue.stadium} (${game.venue.city}) · Kickoff: ${formatLocalDateTime(game.kickoffUtc)} (${game.kickoffUtc})`
    )
  );

  const pills = el("div", "game-kicker-row");
  pills.append(
    el("span", "pill pill-accent", `Line: ${game.odds.spread} | O/U ${game.odds.total}`),
    el("span", "pill", `Ref: ${game.referee.headRef}`),
    el("span", "pill", `Avg Age: ${game.away.abbr} ${Number(game.away.avgAge).toFixed(1)} vs ${game.home.abbr} ${Number(game.home.avgAge).toFixed(1)}`),
    el("span", "pill pill-amber", game.weather.summary)
  );
  if (game.divisionContext?.isDivisional) {
    pills.append(el("span", "pill pill-accent", `Divisional: ${game.divisionContext.division || "Yes"}`));
  }
  left.append(pills);

  const right = el("div", "receipt-box");
  right.append(el("span", "", `ML: ${game.odds.moneyline}`));
  const primaryPred = game.predictions.find((p) => sanitizeHttpsUrl(p.receiptUrl));
  const primaryHref = sanitizeHttpsUrl(primaryPred?.receiptUrl);
  if (primaryHref) {
    const a = el("a", "", "View Git Commit Receipt");
    a.href = primaryHref;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    right.append(a);
  }
  const shortHref = sanitizeHttpsUrl(game.studioShortUrl);
  if (shortHref) {
    const sLink = el("a", "short-link", "▶ Watch Studio Short");
    sLink.href = shortHref;
    sLink.target = "_blank";
    sLink.rel = "noopener noreferrer";
    right.append(sLink);
  }
  top.append(left, right);
  card.append(top);

  if (game.result) {
    const res = el("div", "result-banner");
    res.append(
      el(
        "strong",
        "",
        `FINAL: ${game.away.abbr} ${game.result.finalScore.away} – ${game.home.abbr} ${game.result.finalScore.home} (${game.result.winnerAbbr} Win)`
      ),
      el("span", "", `${game.result.summary} Freak-play outcome: ${game.result.freakPlayOutcome}`)
    );
    card.append(res);
  }

  const predGrid = el("div", "predictions-grid");
  for (const pred of game.predictions) {
    predGrid.append(renderModelCard(game, pred));
  }
  card.append(predGrid);

  // Deep Scouting Modules Accordion
  const accId = `acc-${game.id}`;
  const accHeadingId = `acc-heading-${game.id}`;
  const accordion = el("section", "accordion");
  accordion.id = accId;
  accordion.setAttribute("aria-labelledby", accHeadingId);

  const toolbar = el("div", "accordion-toolbar");
  const accHeading = el("h4", "acc-heading", "Full 7-Module Scouting Breakdown");
  accHeading.id = accHeadingId;

  const toggleAllBtn = document.createElement("button");
  toggleAllBtn.type = "button";
  toggleAllBtn.className = "toggle-all-btn";
  toggleAllBtn.setAttribute("commandfor", accId);
  toggleAllBtn.setAttribute("command", "--toggle-all");
  toggleAllBtn.setAttribute("aria-expanded", "false");
  toggleAllBtn.append(
    el("span", "expand", "Expand all 5 modules"),
    el("span", "collapse", "Collapse all 5 modules")
  );
  toolbar.append(accHeading, toggleAllBtn);
  accordion.append(toolbar);

  accordion.append(
    makeDetails("1. Dual Win Scripts (How & Why Each Team Wins)", `${game.away.abbr} vs ${game.home.abbr}`, (body) => {
      if (game.divisionContext) {
        const divBox = el("div", "sub-box");
        divBox.append(el("h4", "", `Division Context: ${game.divisionContext.division || "Matchup"}`));
        if (game.divisionContext.awayDivRecord) {
          divBox.append(el("p", "", `${game.away.abbr}: ${game.divisionContext.awayDivRecord}`));
        }
        if (game.divisionContext.homeDivRecord) {
          divBox.append(el("p", "", `${game.home.abbr}: ${game.divisionContext.homeDivRecord}`));
        }
        if (game.divisionContext.note) {
          divBox.append(el("p", "", game.divisionContext.note));
        }
        body.append(divBox);
      }
      const grid = el("div", "two-col");
      for (const side of ["away", "home"]) {
        const team = game[side];
        const script = game.winScripts[side];
        const box = el("div", "sub-box");
        box.append(el("h4", "", `How & Why ${team.name} Win`));
        box.append(el("p", "", "Why (Structural Edge):"));
        const whyUl = document.createElement("ul");
        for (const item of script.why) whyUl.append(el("li", "", item));
        box.append(whyUl);
        box.append(el("p", "", "How (Winning Script):"));
        const howUl = document.createElement("ul");
        for (const item of script.how) howUl.append(el("li", "", item));
        box.append(howUl);
        grid.append(box);
      }
      body.append(grid);
    }),
    makeDetails(
      "2. Weather, Physics, Kicking Ceiling & Roster Age",
      `${game.weather.tempF}°F · Elev ${game.venue.elevationFt} ft`,
      (body) => {
        const grid = el("div", "two-col");
        const wBox = el("div", "sub-box");
        wBox.append(
          el("h4", "", "Venue & Ball-Flight Physics"),
          el("p", "", `Surface: ${game.venue.surface} · ${game.weather.summary}`),
          el("p", "", game.venue.quirk),
          el("p", "", game.weather.physicsNote)
        );
        const kBox = el("div", "sub-box");
        kBox.append(
          el("h4", "", "Kicker Ranges & Roster Ages"),
          el("p", "", `${game.away.kicker.name} (${game.away.abbr}): Career long ${game.away.kicker.careerLong} yds · Ceiling: ${game.away.kicker.ceiling}. ${game.away.kicker.note}`),
          el("p", "", `${game.home.kicker.name} (${game.home.abbr}): Career long ${game.home.kicker.careerLong} yds · Ceiling: ${game.home.kicker.ceiling}. ${game.home.kicker.note}`),
          el("p", "", `53-man Avg Age: ${game.away.abbr} ${Number(game.away.avgAge).toFixed(1)} vs ${game.home.abbr} ${Number(game.home.avgAge).toFixed(1)}`)
        );
        grid.append(wBox, kBox);
        const injBox = el("div", "sub-box");
        injBox.append(el("h4", "", "QBs & Key Injuries"));
        const injUl = document.createElement("ul");
        injUl.append(
          el("li", "", `${game.away.abbr} QB: ${game.away.qb}`),
          el("li", "", `${game.home.abbr} QB: ${game.home.qb}`),
          ...game.injuries.away.map((t) => el("li", "", `${game.away.abbr}: ${t}`)),
          ...game.injuries.home.map((t) => el("li", "", `${game.home.abbr}: ${t}`))
        );
        injBox.append(injUl);
        body.append(grid, injBox);
      }
    ),
    makeDetails("3. Officiating Crew Tendencies", `${game.referee.headRef} · ${game.referee.flagsPerGame}`, (body) => {
      const box = el("div", "sub-box");
      box.append(
        el("h4", "", `Head Referee: ${game.referee.headRef}`),
        el("p", "", `Flags/Game: ${game.referee.flagsPerGame} · Yards/Game: ${game.referee.yardsPerGame}`)
      );
      const ul = document.createElement("ul");
      for (const t of game.referee.tendencies) ul.append(el("li", "", t));
      box.append(ul);
      const impact = el("p", "impact-note", `Tonight's Impact: ${game.referee.tonightImpact}`);
      box.append(impact);
      body.append(box);
    }),
    makeDetails(
      "4. Last-30-Day Social & Locker-Room Sentiment",
      `${game.away.abbr} ${game.sentiment30d.awayScore > 0 ? "+" : ""}${game.sentiment30d.awayScore} vs ${game.home.abbr} ${game.sentiment30d.homeScore > 0 ? "+" : ""}${game.sentiment30d.homeScore}`,
      (body) => {
        body.append(renderSentimentTable(game));
        const note = el("div", "sub-box");
        note.append(
          el("p", "", `Betting Split: ${game.sentiment30d.marketSplit}`),
          el("p", "", `Contrarian Read: ${game.sentiment30d.contrarianTakeaway}`)
        );
        body.append(note);
      }
    ),
    makeDetails(
      "5. Full Org-Chart Matchup (GM → Assistant Position Coaches)",
      "4 Tiers + Coaching-Tree Tells",
      (body) => {
        body.append(renderOrgChartTable(game));
        const tells = el("div", "sub-box");
        tells.append(el("h4", "", "Cross-Staff Coaching Tells & #1 Hidden Mismatch"));
        const ul = document.createElement("ul");
        for (const tell of game.orgChart.crossStaffTells) ul.append(el("li", "", tell));
        tells.append(ul);
        const hm = el("p", "hidden-mismatch-note", `#1 Hidden Mismatch: ${game.orgChart.hiddenMismatch}`);
        tells.append(hm);
        body.append(tells);
      }
    )
  );

  card.append(accordion);
  return card;
}

function renderSlatePanel(mount, slate) {
  mount.replaceChildren();
  const header = el("div", "slate-header");
  header.append(
    el("h2", "", slate.title),
    el("p", "", `${slate.subtitle} Last updated UTC: ${slate.updatedAtUtc}`)
  );
  const supercutHref = sanitizeHttpsUrl(slate.studioShortUrl);
  if (supercutHref) {
    const pill = el("a", "short-pill", "▶ Watch Studio Supercut Short");
    pill.href = supercutHref;
    pill.target = "_blank";
    pill.rel = "noopener noreferrer";
    header.append(pill);
  }
  mount.append(header);

  for (const game of slate.games) {
    mount.append(renderGameCard(game));
  }

  if (Array.isArray(slate.missedGames) && slate.missedGames.length) {
    const auditCard = el("section", "missed-game-card");
    auditCard.append(el("h3", "", "Missed-Game Audit Receipt (Full-Window Schedule Check)"));
    for (const mg of slate.missedGames) {
      const item = el("div", "sub-box");
      item.append(
        el("h4", "", `${mg.matchup} — ${mg.label}`),
        el(
          "p",
          "",
          `FINAL: ${mg.finalScore.away}–${mg.finalScore.home} (${mg.winnerAbbr} Win) · ${mg.venue}`
        ),
        el("p", "", mg.recapSummary),
        el("p", "impact-note", `Why Missed & Pipeline Fix: ${mg.reasonMissed}`)
      );
      auditCard.append(item);
    }
    mount.append(auditCard);
  }
}

function renderStandingsTable(slates) {
  const byModel = new Map();
  for (const slate of slates) {
    for (const game of slate.games) {
      for (const pred of game.predictions) {
        const exactId = pred.modelMeta?.exactModelId || pred.modelId;
        const via = [pred.modelMeta?.provider, pred.modelMeta?.harness].filter(Boolean).join(" / ") || "manual";
        const current = byModel.get(exactId) ?? {
          label: pred.modelLabel,
          exactId,
          via,
          locked: 0,
          graded: 0,
          wins: 0,
          losses: 0,
          scoreSum: 0,
        };
        if (pred.status === "locked" || pred.status === "graded") current.locked += 1;
        if (pred.status === "graded" && pred.grade) {
          current.graded += 1;
          current.scoreSum += pred.grade.total;
          if (game.result?.winnerAbbr) {
            if (pred.winnerAbbr === game.result.winnerAbbr) current.wins += 1;
            else current.losses += 1;
          }
        }
        byModel.set(exactId, current);
      }
    }
  }

  const capId = "standings-cap";
  const wrapper = el("div", "table-wrapper");
  wrapper.setAttribute("tabindex", "0");
  wrapper.setAttribute("role", "region");
  wrapper.setAttribute("aria-labelledby", capId);

  const table = el("table", "standings-table");
  const cap = el("caption", "", "Cumulative Model Standings (Grouped by Recorded Exact Model ID)");
  cap.id = capId;
  table.append(cap);

  const thead = document.createElement("thead");
  const hrow = document.createElement("tr");
  for (const c of ["Model (Exact ID)", "Provider / Harness", "Locked", "Graded", "Straight-Up W-L", "Avg Score (/100)"]) {
    hrow.append(el("th", "", c));
  }
  thead.append(hrow);
  table.append(thead);

  const tbody = document.createElement("tbody");
  const rows = Array.from(byModel.values()).sort((a, b) => {
    const avgA = a.graded ? a.scoreSum / a.graded : -1;
    const avgB = b.graded ? b.scoreSum / b.graded : -1;
    return avgB - avgA || b.locked - a.locked;
  });
  for (const row of rows) {
    const tr = document.createElement("tr");
    const title = row.exactId && row.exactId !== row.label ? `${row.label} (${row.exactId})` : row.label;
    const th = el("th", "", title);
    th.scope = "row";
    const avgText = row.graded ? `${(row.scoreSum / row.graded).toFixed(1)} / 100` : "Awaiting final";
    tr.append(
      th,
      el("td", "", row.via),
      el("td", "", String(row.locked)),
      el("td", "", String(row.graded)),
      el("td", "", `${row.wins}-${row.losses}`),
      el("td", "", avgText)
    );
    tbody.append(tr);
  }
  table.append(tbody);
  wrapper.append(table);
  return wrapper;
}

function renderLeaderboardPanel(mount, slates) {
  mount.replaceChildren();
  const header = el("div", "slate-header");
  header.append(
    el("h2", "", "100-Point Multi-Model Arena Leaderboard"),
    el(
      "p",
      "",
      "Scoring rubric: Straight-Up Winner (20) + Score & Margin (20) + Spread & O/U (15) + Win Script & Ref Call (15) + Org Chart & Sentiment (15) + Crazy Surprise Call Bonus (15)."
    )
  );
  mount.append(header);
  mount.append(renderStandingsTable(slates));

  const capId = "leaderboard-cap";
  const wrapper = el("div", "table-wrapper");
  wrapper.setAttribute("tabindex", "0");
  wrapper.setAttribute("role", "region");
  wrapper.setAttribute("aria-labelledby", capId);

  const table = el("table", "leaderboard-table");
  const cap = el("caption", "", "Model Predictions & Graded Receipts Across Slates");
  cap.id = capId;
  table.append(cap);

  const thead = document.createElement("thead");
  const hrow = document.createElement("tr");
  for (const c of ["Model", "Game", "Pick / Score", "Spread / O/U", "Crazy Surprise Call", "Status / Grade"]) {
    hrow.append(el("th", "", c));
  }
  thead.append(hrow);
  table.append(thead);

  const tbody = document.createElement("tbody");
  for (const slate of slates) {
    for (const game of slate.games) {
      for (const pred of game.predictions) {
        const tr = document.createElement("tr");
        const exactId = pred.modelMeta?.exactModelId;
        const labelText = exactId ? `${pred.modelLabel} [${exactId}]` : pred.modelLabel;
        const th = el("th", "", labelText);
        th.scope = "row";
        const scoreText = pred.projectedScore
          ? `${pred.winnerAbbr} (${game.away.abbr} ${pred.projectedScore.away}-${game.home.abbr} ${pred.projectedScore.home})`
          : "Pending";
        const surpriseText = pred.surpriseCall
          ? `${pred.surpriseCall.title} (${pred.surpriseCall.probability})`
          : "Pending";
        const gradeText =
          pred.status === "graded" && pred.grade
            ? `${pred.grade.total} / ${pred.grade.max}`
            : pred.status === "locked"
              ? `Locked (${pred.publishedAtUtc})`
              : "Pending";
        tr.append(
          th,
          el("td", "", `${game.away.abbr} @ ${game.home.abbr}`),
          el("td", "", scoreText),
          el("td", "", `${pred.spreadLean} · ${pred.totalLean}`),
          el("td", "", surpriseText),
          el("td", "", gradeText)
        );
        tbody.append(tr);
      }
    }
  }
  table.append(tbody);
  wrapper.append(table);
  mount.append(wrapper);
}

function setupCopyPromptButton(activeSlate) {
  const btn = document.getElementById("copy-prompt-btn");
  const live = document.getElementById("status-live");
  if (!btn || !activeSlate) return;

  btn.addEventListener("click", async () => {
    const gamesList = activeSlate.games
      .map((g, i) => `${i + 1}. ${g.id}: ${g.away.name} (${g.away.record}) at ${g.home.name} (${g.home.record}) — ${g.label}`)
      .join("\n");
    const prompt = [
      `Use your nfl-game-scout skill to run a full live-verified pregame scouting dossier for ${activeSlate.title}:`,
      gamesList,
      "",
      `Verify last week's starting QBs & official depth charts/inactives, intra-division record (if divisional), spreads, weather/altitude physics, kicker longs, roster ages, assigned referees, 30-day sentiment (-5 to +5), full 4-tier GM-to-assistant-coach org charts, and one grounded Crazy Surprise Call per game.`,
      `Return both the human dossier + JSON array with exact modelMeta matching scripts/arena_cli.py ingest so we can score every model side-by-side!`,
    ].join("\n");

    try {
      await navigator.clipboard.writeText(prompt);
      if (live) live.textContent = `Copied ${activeSlate.title} multi-model benchmark prompt to clipboard!`;
    } catch {
      if (live) live.textContent = `Run locally: python3 scripts/arena_cli.py prompt --slate ${activeSlate.slateId}`;
    }
  });
}

async function boot() {
  initThemeToggle();
  initTabs();
  initAccordionCommands();

  const bfMount = document.getElementById("black-friday-mount");
  const tgMount = document.getElementById("thanksgiving-mount");
  const w11Mount = document.getElementById("week11-mount");
  const lbMount = document.getElementById("leaderboard-mount");

  try {
    const idxResp = await fetch(SLATES_INDEX_URL);
    if (!idxResp.ok) throw new Error(`HTTP ${idxResp.status}`);
    const idx = await idxResp.json();

    const loaded = await Promise.all(
      idx.slates.map(async (entry) => {
        if (!SAFE_SLATE_FILE_RE.test(String(entry.file ?? ""))) {
          throw new Error(`Disallowed slate path: ${entry.file}`);
        }
        const resp = await fetch(`./${entry.file}`);
        if (!resp.ok) throw new Error(`Failed loading ${entry.file}`);
        return resp.json();
      })
    );

    const bfSlate = loaded.find((s) => s.slateId === "2026-black-friday") ?? loaded[0];
    const tgSlate = loaded.find((s) => s.slateId === "2026-thanksgiving") ?? loaded[1];
    const w11Slate = loaded.find((s) => s.slateId === "2026-week11-archive") ?? loaded[2];

    if (bfMount && bfSlate) renderSlatePanel(bfMount, bfSlate);
    if (tgMount && tgSlate) renderSlatePanel(tgMount, tgSlate);
    if (w11Mount && w11Slate) renderSlatePanel(w11Mount, w11Slate);
    if (lbMount) renderLeaderboardPanel(lbMount, loaded);

    setupCopyPromptButton(bfSlate ?? tgSlate);
  } catch (err) {
    const msg = `Could not load arena slate JSONs (${err instanceof Error ? err.message : String(err)}). Visit ${REPO_URL}`;
    if (tgMount) safeSetHTML(tgMount, `<p>${msg}</p>`);
  }
}

if (typeof window !== "undefined") {
  boot();
}
