(() => {
  const FKEYS = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"];
  const TAB_SWITCH_MS = 120; // must match --duration-fast in style.css

  const el = {
    asOf: document.getElementById("asOf"),
    refreshBtn: document.getElementById("refreshBtn"),
    warningBar: document.getElementById("warningBar"),
    tabs: document.getElementById("tabs"),
    content: document.getElementById("content"),
  };

  const tplCard = document.getElementById("tpl-opportunity-card");
  const tplRoute = document.getElementById("tpl-route");

  let state = null; // last successful API payload
  let activeCategory = null;
  const scanResults = {}; // category -> last POST /api/scan/{category} result
  const scanningCategories = new Set();

  async function loadAndRender({ isRefresh = false } = {}) {
    if (isRefresh) el.refreshBtn.classList.add("is-loading");
    try {
      const res = await fetch("/api/opportunities");
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      const data = await res.json();
      state = data;
      if (!activeCategory || !data.categories[activeCategory]) {
        activeCategory = pickDefaultCategory(data);
      }
      renderAsOf(data.generated_at);
      renderWarnings(data.warnings);
      renderTabs(data);
      renderCategory(activeCategory, { animate: !isRefresh });
    } catch (err) {
      renderFatalError(err);
    } finally {
      el.refreshBtn.classList.remove("is-loading");
    }
  }

  function pickDefaultCategory(data) {
    const withPass = data.category_order.find((cat) => data.categories[cat].pass_count > 0);
    return withPass || data.category_order[0];
  }

  function renderAsOf(isoTime) {
    const d = new Date(isoTime);
    el.asOf.textContent = `AS OF ${d.toLocaleTimeString([], { hour12: false })}`;
    el.asOf.classList.remove("is-stale");
  }

  function renderWarnings(warnings) {
    if (!warnings || warnings.length === 0) {
      el.warningBar.hidden = true;
      el.warningBar.textContent = "";
      return;
    }
    el.warningBar.hidden = false;
    el.warningBar.textContent = "⚠ " + warnings.join("   •   ");
  }

  function renderTabs(data) {
    el.tabs.innerHTML = "";
    data.category_order.forEach((cat, i) => {
      const c = data.categories[cat];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tab";
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", String(cat === activeCategory));
      btn.dataset.category = cat;

      const fkey = document.createElement("span");
      fkey.className = "fkey";
      fkey.textContent = `[${FKEYS[i] || "F" + (i + 1)}]`;

      const label = document.createElement("span");
      label.className = "label";
      label.textContent = c.label.toUpperCase();

      const dot = document.createElement("span");
      dot.className = "dot" + dotVariantFor(c, scanResults[cat]);

      btn.append(fkey, label, dot);
      btn.addEventListener("click", () => {
        if (cat === activeCategory) return;
        activeCategory = cat;
        [...el.tabs.children].forEach((t) =>
          t.setAttribute("aria-selected", String(t.dataset.category === cat))
        );
        renderCategory(cat, { animate: true });
      });
      el.tabs.appendChild(btn);
    });
  }

  // Crosswalk signal takes priority (it's already loaded/instant), but a
  // category with zero crosswalk coverage still deserves a dot once its
  // own scan has actually found something -- otherwise every category
  // without crosswalk pairs stays permanently dotless even after scanning
  // turns up real candidates.
  function dotVariantFor(c, scan) {
    const scanHasProfitable = !!scan && !scan.error && scan.profitable_markets.length > 0;
    if (c.pass_count > 0 || scanHasProfitable) return " dot--pass";
    if (c.fee_adjusted_count > 0) return " dot--fee-adjusted";
    if (c.no_edge_count > 0) return " dot--no-edge";
    const scanHasCandidates =
      !!scan && !scan.error && (scan.near_miss_markets.length > 0 || scan.candidate_count > 0);
    if (scanHasCandidates) return " dot--scanned";
    return "";
  }

  function renderCategory(cat, { animate }) {
    const build = () => {
      const c = state.categories[cat];
      el.content.innerHTML = "";
      let cardIndex = 0;

      el.content.appendChild(buildScanControl(cat));

      const scan = scanResults[cat];

      // At most one status/empty message renders before real cards -- once
      // scan results exist (any outcome), its own section below is the one
      // status message for this category, so every category tab has the
      // same shape: SCAN button, then either real opportunity cards or one
      // plain status line. Never a long per-market breakdown list.
      if (c.market_count === 0 && !scan) {
        el.content.appendChild(
          buildEmptyState(
            "NOTHING LOADED YET",
            `No crosswalk pairs configured for ${c.label} yet. Click SCAN below to search this ` +
              "category for live arb opportunities."
          )
        );
      } else if (c.market_count > 0) {
        if (c.markets.length > 0) {
          el.content.appendChild(
            sectionLabel(
              `Compared — ${c.pass_count} arb-found, ${c.fee_adjusted_count} fee-erased, ` +
                `${c.no_edge_count} not profitable`
            )
          );
          c.markets.forEach((m) => {
            const card = buildOpportunityCard(m);
            card.style.setProperty("--i", cardIndex++);
            el.content.appendChild(card);
          });
        } else if (!scan) {
          el.content.appendChild(
            buildEmptyState("NOTHING TO SHOW", "Nothing compared this scan -- try RESCAN.")
          );
        }
      }

      if (scan) {
        if (scan.error) {
          el.content.appendChild(
            buildEmptyState("SCAN FAILED", `Could not complete the discovery scan (${scan.error}). Try again.`)
          );
        } else if (scan.profitable_markets.length > 0) {
          const profitableLabel =
            scan.profitable_total > scan.profitable_markets.length
              ? `Top ${scan.profitable_markets.length} of ${scan.profitable_total} profitable candidates`
              : `${scan.profitable_total} profitable candidate${scan.profitable_total === 1 ? "" : "s"}`;
          el.content.appendChild(
            sectionLabel(
              `${profitableLabel} this scan (of ${scan.candidate_count} title match${scan.candidate_count === 1 ? "" : "es"})`
            )
          );
          scan.profitable_markets.forEach((m) => {
            const card = buildOpportunityCard(m);
            card.style.setProperty("--i", cardIndex++);
            el.content.appendChild(card);
          });
        } else if (scan.near_miss_markets.length > 0) {
          el.content.appendChild(
            sectionLabel(
              `No profitable candidates — closest ${scan.near_miss_markets.length} of ${scan.near_miss_total} ` +
                `near-miss${scan.near_miss_total === 1 ? "" : "es"} (of ${scan.candidate_count} title matches)`
            )
          );
          scan.near_miss_markets.forEach((m) => {
            const card = buildOpportunityCard(m);
            card.style.setProperty("--i", cardIndex++);
            el.content.appendChild(card);
          });
        } else if (scan.candidate_count > 0) {
          el.content.appendChild(
            buildEmptyState(
              "NO COMPARABLE CANDIDATES",
              `Scanned ${scan.total_listed.toLocaleString()} listed markets and found ` +
                `${scan.candidate_count} title match${scan.candidate_count === 1 ? "" : "es"}, but none priced ` +
                "out as a real comparison -- either a live price was missing on one side, or the two venues' " +
                "listed close dates disagree enough that this tool won't treat them as the same bet."
            )
          );
        } else {
          el.content.appendChild(
            buildEmptyState(
              "NO TITLE MATCHES FOUND",
              `Scanned ${scan.total_listed.toLocaleString()} listed markets across both venues this scan. Kalshi ` +
                "and Polymarket essentially never phrase a question identically outside a few categories " +
                "(elections, culture, sports) that happen to share template phrasing like \"Will [name] win " +
                "[event]?\" -- this category's listings just don't overlap in wording today. That's a structural " +
                "fact about how the two venues write questions, not a broken scan -- rescanning won't change it, " +
                "only the venues adding more similarly-worded markets would."
            )
          );
        }
      }
    };

    if (!animate) {
      build();
      return;
    }
    el.content.classList.add("is-switching");
    window.setTimeout(() => {
      el.content.classList.remove("is-switching");
      build();
    }, TAB_SWITCH_MS);
  }

  function buildScanControl(cat) {
    const wrap = document.createElement("div");
    wrap.className = "scan-control";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "scan-btn";
    const scanning = scanningCategories.has(cat);
    btn.disabled = scanning;
    if (scanning) btn.classList.add("is-loading");
    btn.textContent = scanning ? "⟳ SCANNING…" : "⚲ SCAN FOR NEW MARKETS";
    btn.addEventListener("click", () => runCategoryScan(cat));

    const status = document.createElement("span");
    status.className = "scan-control__status";
    const scan = scanResults[cat];
    if (scanning) {
      status.textContent = "Listing both venues' full catalogs and pricing new matches — usually 15–25s.";
    } else if (scan && !scan.error) {
      const t = new Date(scan.scanned_at);
      status.textContent =
        `Last scanned ${t.toLocaleTimeString([], { hour12: false })} — ` +
        `${scan.total_listed.toLocaleString()} markets listed, ${scan.candidate_count} title matches.`;
    } else {
      status.textContent = "Scans only this category — other tabs are never touched.";
    }

    wrap.append(btn, status);
    return wrap;
  }

  async function runCategoryScan(cat) {
    scanningCategories.add(cat);
    if (cat === activeCategory) renderCategory(cat, { animate: false });
    try {
      const res = await fetch(`/api/scan/${encodeURIComponent(cat)}`, { method: "POST" });
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      scanResults[cat] = await res.json();
    } catch (err) {
      scanResults[cat] = {
        error: err.message,
        profitable_markets: [],
        profitable_total: 0,
        near_miss_markets: [],
        near_miss_total: 0,
        candidate_count: 0,
        total_listed: 0,
        scanned_at: new Date().toISOString(),
      };
    } finally {
      scanningCategories.delete(cat);
      // Re-render tabs too, not just content -- a scan on a background
      // (non-active) category still needs its dot to reflect what it
      // found once it finishes, not just the category the user is
      // currently looking at.
      renderTabs(state);
      if (cat === activeCategory) renderCategory(cat, { animate: true });
    }
  }

  function sectionLabel(text) {
    const label = document.createElement("div");
    label.className = "section-label";
    label.textContent = text;
    return label;
  }

  function buildEmptyState(title, body) {
    const wrap = document.createElement("div");
    wrap.className = "empty-state";
    const h = document.createElement("div");
    h.className = "empty-state__title";
    h.textContent = title;
    const p = document.createElement("div");
    p.className = "empty-state__body";
    p.textContent = body;
    wrap.append(h, p);
    return wrap;
  }

  function fmtMoney(v) {
    if (v === null || v === undefined) return "Unknown";
    const sign = v < 0 ? "-" : "";
    return `${sign}$${Math.abs(v).toFixed(2)}`;
  }

  function fmtPct(v) {
    if (v === null || v === undefined) return "--";
    return `${v.toFixed(2)}%`;
  }

  const STATUS_BADGE = {
    PASS: { text: "ARB FOUND", cls: "status-badge--pass" },
    FEE_ADJUSTED_NO_EDGE: { text: "NOT PROFITABLE AFTER FEES", cls: "status-badge--fee-adjusted" },
    NO_EDGE: { text: "NOT PROFITABLE", cls: "status-badge--no-edge" },
  };

  // Handles both market shapes this site has: crosswalk markets (
  // market_name) and scan-found markets (kalshi_title/polymarket_title,
  // shown as a title plus an alt-title line when the venues word it
  // differently). Both render with the exact same badge/card treatment --
  // there's no separate "unverified" styling. A scan candidate is a real,
  // computed number resting on an automated title match rather than a
  // human reading both venues' rules; see the sitewide disclaimer banner
  // for the standing caveat, instead of repeating one on every card.
  function buildOpportunityCard(market) {
    const node = tplCard.content.firstElementChild.cloneNode(true);
    const badge = node.querySelector(".status-badge");
    const shape = STATUS_BADGE[market.best_status] || STATUS_BADGE.NO_EDGE;
    badge.textContent = shape.text;
    badge.classList.add(shape.cls);
    node.querySelector(".opp-card__name").textContent = market.market_name || market.kalshi_title;

    const extras = [];
    const altTitle =
      market.polymarket_title && market.polymarket_title !== market.kalshi_title ? market.polymarket_title : null;
    if (altTitle) {
      const alt = document.createElement("p");
      alt.className = "opp-card__alt-title";
      alt.textContent = `Polymarket: "${altTitle}"`;
      extras.push(alt);
    }
    if (market.best_status === "FEE_ADJUSTED_NO_EDGE") {
      const caveat = document.createElement("p");
      caveat.className = "fee-adjusted__caveat";
      caveat.textContent =
        "Cleared the flat 0.30% fee-buffer estimate, but real per-venue trading fees erase the " +
        "entire edge before any size is tradeable — see the sizing breakdown below. Not a real " +
        "arbitrage right now.";
      extras.push(caveat);
    }
    if (extras.length > 0) {
      node.querySelector(".opp-card__head").after(...extras);
    }

    const routesWrap = node.querySelector(".opp-card__routes");
    market.routes.forEach((route) => routesWrap.appendChild(buildRoute(route)));
    return node;
  }

  function buildRoute(route) {
    const node = tplRoute.content.firstElementChild.cloneNode(true);
    node.querySelector(".route__label").textContent = route.route_label;

    const legsWrap = node.querySelector(".legs");
    route.legs.forEach((leg) => {
      const legEl = document.createElement("div");
      legEl.className = "leg";

      const action = document.createElement("span");
      action.className = "leg__action";
      const sideSpan = document.createElement("span");
      sideSpan.className = leg.side === "YES" ? "side-yes" : "side-no";
      sideSpan.textContent = leg.side;
      action.append(`${leg.action} `, sideSpan);

      const venue = document.createElement("div");
      venue.className = "leg__venue";
      venue.textContent = `on ${leg.venue}`;

      const price = document.createElement("div");
      price.className = "leg__price";
      price.textContent = `$${leg.price.toFixed(4)}`;

      legEl.append(action, venue, price);
      legsWrap.appendChild(legEl);
    });

    const hero = node.querySelector(".hero-row");
    hero.appendChild(buildStat("Total Cost", fmtMoney(route.total_cost), null, true));
    hero.appendChild(buildStat("Guaranteed", fmtMoney(route.guaranteed_payout), null, true));
    // Tone follows route.status (real, fee-adjusted profitability), not the
    // raw net_edge sign -- a FEE_ADJUSTED_NO_EDGE route has a positive flat-
    // buffer net_edge by construction (that's why arb_engine passed it),
    // but is not actually profitable once real per-venue fees are counted,
    // so it must not render green here.
    const isRealPositive = route.status === "PASS";
    hero.appendChild(
      buildStat(
        "Net Edge",
        `${fmtPct(route.net_edge_pct)} · ${fmtMoney(route.profit_per_100)} / $100`,
        isRealPositive ? "positive" : "negative"
      )
    );
    // The headline size figure: sizing.max_units, not the old per-venue
    // "depth" field -- that field silently ignored Kalshi's real liquidity
    // (Kalshi's public API never reports it) and was contradicting this
    // correct, both-legs-and-real-fees-aware number on the same card.
    const hasSize = route.sizing && route.sizing.max_units > 0;
    hero.appendChild(
      buildStat(
        "Max Shares",
        hasSize ? `${Math.floor(route.sizing.max_units).toLocaleString()}` : "0",
        hasSize ? null : "negative"
      )
    );

    node.querySelector(".sizing").appendChild(buildSizing(route.sizing));
    return node;
  }

  function buildStat(label, value, tone, secondary) {
    const wrap = document.createElement("div");
    wrap.className = "stat" + (secondary ? " stat--secondary" : "");
    const l = document.createElement("div");
    l.className = "stat__label";
    l.textContent = label;
    const v = document.createElement("div");
    v.className = "stat__value" + (tone ? ` ${tone}` : "");
    v.textContent = value;
    wrap.append(l, v);
    return wrap;
  }

  function buildSizing(sizing) {
    const wrap = document.createElement("div");

    if (!sizing || !sizing.max_units) {
      const reason = sizing ? sizing.limiting_factor : "sizing unavailable";
      const p = document.createElement("div");
      p.className = "sizing__headline";
      p.textContent = `Max shares before slippage/fees erode edge: 0 (${reason})`;
      wrap.appendChild(p);
      return wrap;
    }

    const headline = document.createElement("div");
    headline.className = "sizing__headline";
    headline.textContent =
      `Max shares before slippage/fees erode edge: ${Math.floor(sizing.max_units).toLocaleString()}` +
      ` — limited by: ${sizing.limiting_factor}`;
    wrap.appendChild(headline);

    if (sizing.avg_cost_per_unit != null) {
      const avg = document.createElement("div");
      avg.textContent = `Avg cost per share incl. fees: $${sizing.avg_cost_per_unit.toFixed(4)}`;
      wrap.appendChild(avg);
    }

    sizing.legs_fees.forEach((lf) => {
      const row = document.createElement("div");
      row.className = "sizing__fee-row";

      const left = document.createElement("span");
      left.textContent = `${lf.venue} ${lf.side} fee`;

      const right = document.createElement("span");
      const rateTxt = `${(lf.fee_rate * 100).toFixed(2)}%`;
      right.append(`${fmtMoney(lf.fee_amount)} (${rateTxt} — `);
      const tag = document.createElement("span");
      if (lf.fee_rate_is_estimate) {
        tag.className = "estimate-tag";
        tag.textContent = "est., not venue-confirmed";
      } else {
        tag.textContent = "from market data";
      }
      right.append(tag, ")");

      row.append(left, right);
      wrap.appendChild(row);
    });

    const profit = document.createElement("div");
    profit.className = "sizing__profit";
    profit.textContent = `Est. profit after real fees: ${fmtMoney(sizing.estimated_profit_after_fees)}`;
    wrap.appendChild(profit);

    return wrap;
  }

  function renderFatalError(err) {
    el.content.innerHTML = "";
    el.content.appendChild(
      buildEmptyState(
        "SCAN FAILED",
        `Could not reach the scanner backend (${err.message}). Confirm the server is still ` +
          "running, then hit RESCAN."
      )
    );
    el.asOf.textContent = "-- last scan failed --";
    el.asOf.classList.add("is-stale");
  }

  el.refreshBtn.addEventListener("click", () => loadAndRender({ isRefresh: true }));

  loadAndRender();
})();
