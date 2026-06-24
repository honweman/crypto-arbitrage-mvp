const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 10 });
    const money = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
    const compact = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
	    const shortNumber = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 });
	    const PAGE_IDS = new Set(["status", "settings", "records"]);
	    let currentPage = pageFromLocation();
	    let lastState = null;
	    let refreshQueued = false;
	    const pageStateCache = {};
	    const PAGE_RENDER_INTERVAL_MS = { status: 1500, settings: 3000, records: 2000 };
	    const REFRESH_INTERVAL_MS = 2000;
	    const PAGE_SECTION_IDS = {
	      settings: [
	        "markets-config",
	        "carry-config",
	        "risk-form",
	        "strategy-instances",
	        "api-accounts",
	        "funding-arb-form",
	        "signal-bot-form",
	        "mm-orders",
	        "slow-orders",
	        "grid-orders",
	        "dca-orders",
	        "exec-schedule",
	        "backtest-points",
	      ],
	      records: [
	        "console-strategies",
	        "open-orders",
	        "strategy-timeline",
	        "audit-events",
	        "holder-changes",
	      ],
	    };
	    const lastVisibleRenderAt = { status: 0, settings: 0, records: 0 };

    function pageFromLocation() {
      const hashPage = window.location.hash.replace("#", "");
      if (hashPage === "monitor") return "status";
      if (hashPage === "control") return "settings";
      return PAGE_IDS.has(hashPage) ? hashPage : "status";
    }

	    function setActivePage(page, options = {}) {
	      const activePage = PAGE_IDS.has(page) ? page : "status";
	      currentPage = activePage;
	      document.querySelectorAll("[data-page]").forEach((el) => {
	        el.classList.toggle("active-page", el.dataset.page === activePage);
	      });
      document.querySelectorAll("[data-view-tab]").forEach((tab) => {
        const active = tab.dataset.viewTab === activePage;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-current", active ? "page" : "false");
      });
	      if (window.location.hash !== `#${activePage}`) {
	        history.replaceState(null, "", `#${activePage}`);
	      }
	      const cachedState = pageStateCache[activePage];
	      if (cachedState) {
	        renderCommonState(cachedState);
	        renderVisiblePage(cachedState, activePage, { force: true });
	      } else if (lastState) {
	        renderCommonState(lastState);
	      }
	      if (options.refresh !== false) refresh({ force: true });
	    }

    function setupCompactSections() {
      document.querySelectorAll(".compact-section > .section-title").forEach((title) => {
        const section = title.closest(".compact-section");
        if (!section) return;
        const sync = () => {
          title.setAttribute("aria-expanded", section.classList.contains("section-open") ? "true" : "false");
        };
        title.setAttribute("role", "button");
        title.setAttribute("tabindex", "0");
        title.addEventListener("click", (event) => {
          if (event.target.closest("a, button, input, label, select, textarea")) return;
          section.classList.toggle("section-open");
          sync();
          refreshOpenedSection(section);
        });
        title.addEventListener("keydown", (event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          section.classList.toggle("section-open");
          sync();
          refreshOpenedSection(section);
        });
        sync();
      });
    }

    function text(id, value) {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    }

    function isSectionOpenFor(id) {
      const el = document.getElementById(id);
      const section = el?.closest(".compact-section");
      return !section || section.classList.contains("section-open");
    }

    function renderOpenSection(id, renderFn) {
      if (!isSectionOpenFor(id)) return;
      renderFn();
    }

    function openSectionIdsForPage(page) {
      return (PAGE_SECTION_IDS[page] || []).filter((id) => isSectionOpenFor(id));
    }

    function refreshOpenedSection(section) {
      if (!section.classList.contains("section-open") || section.dataset.page !== currentPage) return;
      const cachedState = pageStateCache[currentPage] || lastState;
      if (cachedState) {
        window.requestAnimationFrame(() => {
          renderVisiblePage(cachedState, currentPage, { force: true });
        });
      }
      refresh({ force: true });
    }

    function formatAge(ts) {
      if (!ts) return "--";
      const age = Math.max(0, Date.now() / 1000 - ts);
      return age < 60 ? `${age.toFixed(0)}s ago` : `${(age / 60).toFixed(1)}m ago`;
    }

    function baseCurrency(symbol) {
      return String(symbol || "").split("/")[0] || "BASE";
    }

    function quoteCurrency(symbol) {
      return (String(symbol || "").split("/")[1] || "QUOTE").split(":")[0];
    }

    function formatSymbolQuantity(value, symbol, mode) {
      const currency = mode === "quote" ? quoteCurrency(symbol) : baseCurrency(symbol);
      return `${currency} ${formatBalanceAmount(value || 0)}`;
    }

    function renderMarkets(markets) {
      const body = document.getElementById("markets");
      body.innerHTML = "";
      for (const row of markets || []) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${row.exchange}</td>
          <td>${row.symbol}</td>
          <td class="${row.status === "ok" ? "ok" : "missing"}">${row.status}</td>
          <td class="num">${row.bid == null ? "--" : fmt.format(row.bid)}</td>
          <td class="num">${row.ask == null ? "--" : fmt.format(row.ask)}</td>
          <td class="num">${row.bid_common == null ? "--" : fmt.format(row.bid_common)}</td>
          <td class="num">${row.ask_common == null ? "--" : fmt.format(row.ask_common)}</td>
          <td class="num">${row.bid_size == null ? "--" : compact.format(row.bid_size)}</td>
          <td class="num">${row.ask_size == null ? "--" : compact.format(row.ask_size)}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderRates(rates) {
      const body = document.getElementById("rates");
      body.innerHTML = "";
      for (const [currency, rate] of Object.entries(rates || {}).sort()) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${currency}</td><td class="num">${fmt.format(rate)}</td>`;
        body.appendChild(tr);
      }
    }

    function formatBalanceAmount(value) {
      if (value == null) return "--";
      return Math.abs(value) >= 1_000_000 ? shortNumber.format(value) : fmt.format(value);
    }

    function formatBps(value) {
      if (value == null) return "--";
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return "--";
      return `${numeric.toFixed(2)} bps`;
    }

function balanceStatusClass(status) {
      if (status === "ok") return "ok";
      if (status === "candidate") return "ok";
      if (status === "blocked") return "risk-blocked";
      if (["idle", "starting", "checking"].includes(status)) return "subtle";
      return "missing";
    }

    function sortBalanceCurrencies(rows) {
      const preferredOrder = { ACS: 0, USDC: 1, USDT: 2, USD: 3, KRW: 4 };
      return [...(rows || [])].sort((left, right) => {
        const leftRank = preferredOrder[left.currency] ?? 99;
        const rightRank = preferredOrder[right.currency] ?? 99;
        return leftRank === rightRank
          ? String(left.currency).localeCompare(String(right.currency))
          : leftRank - rightRank;
      });
    }

    function renderAccountBalanceSummary(accountBalances) {
      const totals = sortBalanceCurrencies(accountBalances?.totals || []);
      const valueEl = document.getElementById("account-balances-total");
      const detailEl = document.getElementById("account-balances-detail");
      if (totals.length === 0) {
        valueEl.textContent = "--";
        detailEl.textContent = accountBalances?.status || "--";
        detailEl.title = detailEl.textContent;
        return;
      }

      valueEl.textContent = totals.length === 1
        ? `${formatBalanceAmount(totals[0].total)} ${totals[0].currency}`
        : `${totals.length} currencies`;
      const detail = totals
        .slice(0, 5)
        .map((row) => `${row.currency} ${formatBalanceAmount(row.total)}`)
        .join(" · ");
      detailEl.textContent = detail;
      detailEl.title = totals
        .map((row) => {
          const reserved = Number(row.open_order_reserved || 0);
          const reserveText = reserved > 0 ? ` · reserved ${formatBalanceAmount(reserved)}` : "";
          return `${row.currency} free ${formatBalanceAmount(row.free)} · used ${formatBalanceAmount(row.used)} · total ${formatBalanceAmount(row.total)}${reserveText}`;
        })
        .join(" | ");
    }

    function renderAccountBalances(accountBalances) {
      renderAccountBalanceSummary(accountBalances);
      text(
        "account-balances-meta",
        accountBalances
          ? `${accountBalances.status || "unknown"} · checked ${accountBalances.checked_account_count || 0}/${accountBalances.total_account_count || 0} · ${formatAge(accountBalances.last_finished)}`
          : ""
      );

      const body = document.getElementById("account-balances");
      body.innerHTML = "";
      const accounts = accountBalances?.accounts || [];
      if (accounts.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">No account balances yet.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const account of accounts) {
        const rows = sortBalanceCurrencies(account.balance?.currencies || []);
        if (rows.length === 0) {
          const message = account.balance?.error || account.balance?.skipped_reason || "No non-zero target balances.";
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(account.label || account.exchange)}</td>
            <td colspan="4">${escapeHtml(message)}</td>
            <td class="${balanceStatusClass(account.status)}">${escapeHtml(account.status || "--")}</td>
          `;
          body.appendChild(tr);
          continue;
        }

        for (const row of rows) {
          const tr = document.createElement("tr");
          const reserved = Number(row.open_order_reserved || 0);
          const usedTitle = reserved > 0 ? `Open-order reserve ${formatBalanceAmount(reserved)} ${row.currency}` : "";
          tr.innerHTML = `
            <td>${escapeHtml(account.label || account.exchange)}</td>
            <td>${escapeHtml(row.currency)}</td>
            <td class="num">${formatBalanceAmount(row.free)}</td>
            <td class="num" title="${escapeHtml(usedTitle)}">${formatBalanceAmount(row.used)}</td>
            <td class="num">${formatBalanceAmount(row.total)}</td>
            <td class="${balanceStatusClass(account.status)}">${escapeHtml(account.status || "--")}</td>
          `;
          body.appendChild(tr);
        }
      }
    }

    function renderDerivativesRisk(derivatives) {
      text(
        "derivatives-risk-meta",
        derivatives
          ? `${derivatives.status || "unknown"} · checked ${derivatives.checked_account_count || 0}/${derivatives.total_account_count || 0} · positions ${derivatives.position_count || 0} · ${formatAge(derivatives.last_finished)}`
          : ""
      );
      const body = document.getElementById("derivatives-risk");
      body.innerHTML = "";
      const accounts = derivatives?.accounts || [];
      if (accounts.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="10">No derivative accounts configured.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const account of accounts) {
        const positions = account.positions || [];
        if (positions.length === 0) {
          const message = account.error || account.skipped_reason || (account.risk_reasons || []).join(" · ") || "No open positions.";
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(account.label || account.exchange)}</td>
            <td colspan="8">${escapeHtml(message)}</td>
            <td class="${balanceStatusClass(account.status)}">${escapeHtml(account.status || "--")}</td>
          `;
          body.appendChild(tr);
          continue;
        }
        for (const position of positions) {
          const funding = position.funding_rate == null ? "--" : `${(Number(position.funding_rate) * 100).toFixed(4)}%`;
          const buffer = position.liquidation_buffer_pct == null ? "--" : `${Number(position.liquidation_buffer_pct).toFixed(2)}%`;
          const reasons = (position.risk_reasons || []).join(" · ");
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(account.label || account.exchange)}</td>
            <td>${escapeHtml(position.symbol || "--")}</td>
            <td class="${position.side === "long" ? "side-buy" : position.side === "short" ? "side-sell" : ""}">${escapeHtml(String(position.side || "--").toUpperCase())}</td>
            <td class="num">${formatBalanceAmount(position.notional_quote)}</td>
            <td class="num">${position.leverage == null ? "--" : fmt.format(position.leverage)}</td>
            <td class="num">${position.mark_price == null ? "--" : fmt.format(position.mark_price)}</td>
            <td class="num">${position.liquidation_price == null ? "--" : fmt.format(position.liquidation_price)}</td>
            <td class="num">${buffer}</td>
            <td class="num">${funding}</td>
            <td class="${position.status === "blocked" ? "risk-blocked" : "ok"}" title="${escapeHtml(reasons)}">${escapeHtml(position.status || "--")}</td>
          `;
          body.appendChild(tr);
        }
      }
    }

    function renderFundingBasis(fundingBasis) {
      text(
        "funding-basis-meta",
        fundingBasis
          ? `${fundingBasis.status || "unknown"} · candidates ${fundingBasis.candidate_count || 0} · checked ${fundingBasis.checked_count || 0}/${fundingBasis.configured_count || 0} · ${formatAge(fundingBasis.last_finished)}`
          : ""
      );
      const body = document.getElementById("funding-basis");
      body.innerHTML = "";
      const rows = fundingBasis?.rows || [];
      if (rows.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="8">No funding/basis pair configured.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const row of rows) {
        const paper = row.paper_execution || {};
        const protection = paper.protection || {};
        const legs = (paper.suggested_legs || [])
          .map((leg) => `${leg.side} ${leg.symbol} @ ${leg.exchange}`)
          .join(" / ");
        const protectionText = protection.status ? ` · protection ${protection.status}` : "";
        const protectionTitle = [
          ...(protection.reasons || []),
          ...(protection.warnings || []),
          ...((protection.playbooks || []).map((item) => `${item.event}: ${item.action}`)),
        ].filter(Boolean).join(" · ");
        const reason = [
          row.reason,
          ...(row.warnings || []),
          protectionTitle,
        ].filter(Boolean).join(" · ");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(row.pair_id || "")}">${escapeHtml(row.pair_id || "--")}</td>
          <td>${escapeHtml(row.spot_exchange || "--")}<br><span class="subtle">${escapeHtml(row.spot_symbol || "--")}</span><br>${escapeHtml(row.derivative_exchange || "--")}<br><span class="subtle">${escapeHtml(row.derivative_symbol || "--")}</span></td>
          <td class="num">${row.spot_mid == null ? "--" : fmt.format(row.spot_mid)}</td>
          <td class="num">${row.derivative_mid == null ? "--" : fmt.format(row.derivative_mid)}</td>
          <td class="num">${formatBps(row.basis_bps)}</td>
          <td class="num" title="${row.estimated_apr_pct == null ? "" : `APR ${Number(row.estimated_apr_pct).toFixed(2)}%`}">${formatBps(row.funding_rate_bps)}</td>
          <td>${escapeHtml(paper.state || "--")}<br><span class="subtle" title="${escapeHtml(protectionTitle)}">${escapeHtml((legs || paper.reason || "--") + protectionText)}</span></td>
          <td class="${balanceStatusClass(row.status)}" title="${escapeHtml(reason)}">${escapeHtml(row.status || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function contractSignalText(signal) {
      if (!signal || typeof signal !== "object") return ["--", ""];
      const primary = signal.primary || "--";
      if (primary === "funding") {
        const apr = signal.estimated_apr_pct == null
          ? "APR --"
          : `APR ${formatMaybeNumber(signal.estimated_apr_pct)}%`;
        return [
          `Funding ${formatBps(signal.funding_rate_bps)}`,
          `Basis ${formatBps(signal.basis_bps)} · ${apr}`,
        ];
      }
      if (primary === "basis") {
        return [
          `Basis ${formatBps(signal.basis_bps)}`,
          `Entry ${formatBps(signal.threshold_bps)} · Exit ${formatBps(signal.exit_bps)}`,
        ];
      }
      if (primary === "grid") {
        return [
          `Mid ${formatMaybeNumber(signal.mid_price)}`,
          signal.detail || "",
        ];
      }
      if (primary === "delta") {
        return [
          `Delta ${formatMaybeNumber(signal.net_mm_delta_base)}`,
          `Threshold ${formatMaybeNumber(signal.threshold_base)} · fills ${signal.trade_count || 0}`,
        ];
      }
      return [String(primary), signal.detail || ""];
    }

    function contractPlanText(plan) {
      if (!plan || typeof plan !== "object") return ["--", ""];
      const summary = plan.summary || "--";
      const orderCount = plan.order_count == null ? null : Number(plan.order_count);
      const detail = [
        plan.notional_quote == null ? "" : `Notional ${formatMaybeNumber(plan.notional_quote, money)}`,
        orderCount == null ? "" : `${orderCount} orders`,
        plan.leverage == null ? "" : `${formatMaybeNumber(plan.leverage)}x`,
        plan.post_only == null ? "" : (plan.post_only ? "post-only" : "taker"),
      ].filter(Boolean).join(" · ");
      return [summary, detail];
    }

    function renderContractStrategies(contractStrategies) {
      text(
        "contract-strategies-meta",
        contractStrategies
          ? `${contractStrategies.status || "unknown"} · candidates ${contractStrategies.candidate_count || 0} · blocked ${contractStrategies.blocked_count || 0} · ${formatAge(contractStrategies.last_finished)}`
          : ""
      );
      const summary = document.getElementById("contract-strategies-summary");
      if (summary) {
        const items = [
          ["Funding", contractStrategies?.summary?.funding_bot?.status || "--", `${contractStrategies?.summary?.funding_bot?.candidate_count || 0} candidates`],
          ["Basis", contractStrategies?.summary?.basis_bot?.status || "--", `${contractStrategies?.summary?.basis_bot?.candidate_count || 0} candidates`],
          ["Grid", contractStrategies?.summary?.futures_grid?.status || "--", `${contractStrategies?.summary?.futures_grid?.row_count || 0} plans`],
          ["Hedge", contractStrategies?.summary?.hedge_rebalancer?.status || "--", `${contractStrategies?.summary?.hedge_rebalancer?.candidate_count || 0} hedges`],
          ["Mode", contractStrategies?.mode || "paper", "auto-submit off"],
        ];
        summary.innerHTML = items.map(([label, value, detail]) => `
          <div class="metric compact">
            <div class="label">${escapeHtml(label)}</div>
            <div class="value">${escapeHtml(value)}</div>
            <div class="detail">${escapeHtml(detail)}</div>
          </div>
        `).join("");
      }
      const body = document.getElementById("contract-strategies");
      if (!body) return;
      body.innerHTML = "";
      const rows = contractStrategies?.rows || [];
      if (rows.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">No contract strategy rows.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const row of rows) {
        const [signalPrimary, signalDetail] = contractSignalText(row.signal);
        const [planPrimary, planDetail] = contractPlanText(row.plan);
        const risk = row.risk || {};
        const riskMessages = [
          ...(risk.reasons || []),
          ...(risk.warnings || []),
          ...(row.warnings || []),
        ].filter(Boolean).join(" · ");
        const reason = [
          row.reason,
          riskMessages,
        ].filter(Boolean).join(" · ");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(row.strategy || "--")}<br><span class="subtle">${escapeHtml(row.plan?.mode || "paper")}</span></td>
          <td title="${escapeHtml(row.market?.label || "")}">${escapeHtml(row.market?.label || "--")}</td>
          <td>${escapeHtml(signalPrimary)}<br><span class="subtle">${escapeHtml(signalDetail)}</span></td>
          <td>${escapeHtml(planPrimary)}<br><span class="subtle">${escapeHtml(planDetail)}</span></td>
          <td class="${risk.status === "blocked" ? "risk-blocked" : risk.status === "warning" ? "missing" : "ok"}" title="${escapeHtml(riskMessages)}">${escapeHtml(risk.status || "--")}</td>
          <td class="${balanceStatusClass(row.status)}" title="${escapeHtml(reason)}">${escapeHtml(row.status || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function formatMaybeNumber(value, formatter = fmt) {
      return value == null || !Number.isFinite(Number(value)) ? "--" : formatter.format(Number(value));
    }

    function renderOptionsRiskSummary(optionsArbitrage) {
      const container = document.getElementById("options-risk-summary");
      if (!container) return;
      const risk = optionsArbitrage?.risk || {};
      const controls = risk.controls || optionsArbitrage?.execution_controls || {};
      const expiryReminders = risk.expiry_reminders || [];
      const items = [
        ["Risk", risk.status || optionsArbitrage?.status || "--", `${risk.blocked_new_open_count || 0} blocked opens`],
        ["Delta", formatMaybeNumber(risk.total_delta), `Gamma ${formatMaybeNumber(risk.total_gamma)}`],
        ["Vega", formatMaybeNumber(risk.total_vega), `Theta ${formatMaybeNumber(risk.total_theta)}`],
        ["Expiry", `${expiryReminders.length || 0} alerts`, `${(risk.expiry_concentration || []).length || 0} expiries`],
        ["Payoff", formatMaybeNumber(risk.max_profit_quote, money), `Max loss ${formatMaybeNumber(risk.max_loss_quote, money)}`],
        ["Liquidity", `${formatMaybeNumber(controls.min_option_depth_quote)} min depth`, `${formatBps(controls.max_option_spread_bps)}`],
      ];
      container.innerHTML = items.map(([label, value, detail]) => `
        <div class="metric compact">
          <div class="label">${escapeHtml(label)}</div>
          <div class="value">${escapeHtml(value)}</div>
          <div class="detail">${escapeHtml(detail)}</div>
        </div>
      `).join("");
    }

    function renderOptionsChain(optionsArbitrage) {
      const body = document.getElementById("options-chain");
      if (!body) return;
      body.innerHTML = "";
      const rows = optionsArbitrage?.option_chain || [];
      if (rows.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="8">No option chain rows.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const row of rows) {
        const reason = (row.reasons || []).join(" · ");
        const greeks = [
          `D ${formatMaybeNumber(row.delta)}`,
          `G ${formatMaybeNumber(row.gamma)}`,
          `V ${formatMaybeNumber(row.vega)}`,
          `T ${formatMaybeNumber(row.theta)}`,
        ].join(" / ");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(row.expiry || "--")}<br><span class="subtle">${row.days_to_expiry == null ? "--" : `${Number(row.days_to_expiry).toFixed(1)}d`} · K ${formatMaybeNumber(row.strike)}</span></td>
          <td>${escapeHtml(row.option_type || "--")}<br><span class="subtle">${escapeHtml(row.symbol || "--")}</span></td>
          <td class="num">${formatMaybeNumber(row.bid)} / ${formatMaybeNumber(row.ask)}</td>
          <td class="num">${formatMaybeNumber(row.mark_price)}<br><span class="subtle">${row.iv == null ? "IV --" : `IV ${formatMaybeNumber(row.iv)}`}</span></td>
          <td class="num">${formatMaybeNumber(row.min_depth_quote, money)}<br><span class="subtle">${formatBps(row.spread_bps)}</span></td>
          <td class="num">${formatMaybeNumber(row.volume)} / ${formatMaybeNumber(row.open_interest)}</td>
          <td class="num" title="${escapeHtml(greeks)}">${escapeHtml(greeks)}</td>
          <td class="${balanceStatusClass(row.status)}" title="${escapeHtml(reason)}">${escapeHtml(row.status || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderOptionsArbitrage(optionsArbitrage) {
      renderOptionsRiskSummary(optionsArbitrage);
      renderOptionsChain(optionsArbitrage);
      text(
        "options-arbitrage-meta",
        optionsArbitrage
          ? `${optionsArbitrage.status || "unknown"} · candidates ${optionsArbitrage.candidate_count || 0} (${optionsArbitrage.parity_candidate_count || 0} parity / ${optionsArbitrage.enhanced_candidate_count || 0} enhanced) · checked ${optionsArbitrage.checked_count || 0}/${optionsArbitrage.configured_count || 0} · ${formatAge(optionsArbitrage.last_finished)}`
          : ""
      );
      const body = document.getElementById("options-arbitrage");
      body.innerHTML = "";
      const rows = optionsArbitrage?.rows || [];
      if (rows.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="8">No option combo configured.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const row of rows) {
        const paper = row.paper_execution || {};
        const protection = paper.protection || {};
        const ticket = paper.order_ticket || {};
        const opportunity = row.opportunity || {};
        const edge = opportunity.profit_bps == null ? "" : ` · edge ${formatBps(opportunity.profit_bps)}`;
        const protectionText = protection.status ? ` · protection ${protection.status}` : "";
        const ticketText = ticket.order_count ? ` · ticket ${ticket.order_count}` : "";
        const protectionTitle = [
          ...(protection.reasons || []),
          ...(protection.warnings || []),
          ...((protection.playbooks || []).map((item) => `${item.event}: ${item.action}`)),
        ].filter(Boolean).join(" · ");
        const legs = (paper.suggested_legs || [])
          .map((leg) => `${leg.side} ${leg.symbol}`)
          .join(" / ");
        const comboTitle = `${row.underlying || "--"} ${row.expiry || ""} K=${row.strike || "--"}`;
        const reason = [row.reason, ...(row.reasons || []), protectionTitle].filter(Boolean).join(" · ");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(comboTitle)}">${escapeHtml(row.underlying || "--")}<br><span class="subtle">K ${row.strike || "--"} ${escapeHtml(row.expiry || "")}</span></td>
          <td>${escapeHtml(row.spot_symbol || "--")}<br><span class="subtle">${escapeHtml(row.call_symbol || "--")} / ${escapeHtml(row.put_symbol || "--")}</span></td>
          <td class="num">${row.spot_mid == null ? "--" : fmt.format(row.spot_mid)}</td>
          <td class="num">${row.call_mid == null ? "--" : fmt.format(row.call_mid)}</td>
          <td class="num">${row.put_mid == null ? "--" : fmt.format(row.put_mid)}</td>
          <td class="num">${formatBps(row.parity_gap_bps)}</td>
          <td>${escapeHtml(paper.state || "--")}<br><span class="subtle" title="${escapeHtml(protectionTitle)}">${escapeHtml((legs || paper.reason || "--") + edge + protectionText + ticketText)}</span></td>
          <td class="${balanceStatusClass(row.status)}" title="${escapeHtml(reason)}">${escapeHtml(row.status || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function formatTimestamp(value) {
      if (value == null) return "--";
      const ts = Number(value);
      if (!Number.isFinite(ts)) return "--";
      return new Date(ts).toLocaleString();
    }

    function formatFee(fee) {
      if (!fee) return "--";
      const cost = fee.cost == null ? "--" : formatBalanceAmount(fee.cost);
      return fee.currency ? `${cost} ${fee.currency}` : cost;
    }

    function shortId(value) {
      if (!value) return "--";
      const textValue = String(value);
      return textValue.length > 12 ? `${textValue.slice(0, 8)}...` : textValue;
    }

    function orderSideClass(side) {
      return side === "buy" ? "side-buy" : side === "sell" ? "side-sell" : "";
    }

    function displaySource(value) {
      if (value === "market_maker") return "Market Maker";
      if (value === "arbitrage") return "Arbitrage";
      if (value === "auto_buy_sell" || value === "slow_execution") return "Auto Buy/Sell";
      if (value === "spot_grid") return "Spot Grid";
      if (value === "dca") return "DCA Bot";
      if (value === "execution_algo") return "TWAP/VWAP/POV";
      if (value === "backtest") return "Backtest/Paper";
      if (value === "spot_spread") return "Spot Arbitrage";
      if (value === "cash_and_carry") return "Cash & Carry";
      if (value === "funding_arbitrage") return "Funding Arbitrage";
      if (value === "options_arbitrage") return "Options Arbitrage";
      if (value === "signal_bot") return "Signal Bot";
      if (value === "manual") return "Manual";
      if (value === "unattributed") return "Unattributed";
      return value || "--";
    }

    function renderAuthProfile(auth) {
      const emailEl = document.getElementById("user-email");
      const select = document.getElementById("profile-asset");
      if (!emailEl || !select) return;
      const mode = auth?.mode || "legacy";
      emailEl.textContent = mode === "user" ? (auth.email || "User") : "Legacy";
      emailEl.title = emailEl.textContent;
      const available = auth?.available_assets || [];
      const allowed = auth?.allowed_assets?.length ? auth.allowed_assets : available;
      const assets = [...new Set((allowed || []).filter(Boolean))].sort();
      select.innerHTML = "";
      const allOption = document.createElement("option");
      allOption.value = "";
      allOption.textContent = assets.length > 1 ? "All assets" : "Asset";
      select.appendChild(allOption);
      for (const asset of assets) {
        const option = document.createElement("option");
        option.value = asset;
        option.textContent = asset;
        select.appendChild(option);
      }
      select.value = auth?.preferred_asset || "";
      select.disabled = mode !== "user" || assets.length === 0;
    }

    async function updateProfileAsset(event) {
      const preferredAsset = event.target.value || "";
      const res = await fetch("/api/profile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preferred_asset: preferredAsset }),
      });
      if (res.status === 401) {
        window.location.assign("/login");
        return;
      }
      if (!res.ok) {
        const payload = await res.json().catch(() => ({}));
        text("warnings", payload.error || `profile update failed (${res.status})`);
        return;
      }
      await refresh({ force: true });
    }

    function displayReconciliationType(value) {
      const labels = {
        tracked_order_missing: "Tracked Missing",
        tracked_order_filled_not_cleared: "Filled, Not Cleared",
        tracked_order_closed_not_cleared: "Closed, Not Cleared",
        untracked_open_order: "Untracked Open",
        unmanaged_strategy_order: "Unmanaged Strategy",
        unattributed_fill: "Unattributed Fill",
        order_activity_error: "Activity Error",
      };
      return labels[value] || value || "--";
    }

    function formatPnlValue(value) {
      return value == null ? "--" : `$${money.format(value)}`;
    }

    let cancelOrderBusy = new Set();
    let marketsConfigBusy = false;
    let carryConfigBusy = false;
    let currentSpotMarkets = [];
    let currentCashCarryPairs = [];

    async function cancelOrder(order, button) {
      const key = `${order.exchange}:${order.symbol}:${order.id}`;
      if (cancelOrderBusy.has(key)) return;
      cancelOrderBusy.add(key);
      button.disabled = true;
      button.textContent = "Canceling";
      try {
        const res = await fetch("/api/orders/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            exchange: order.exchange,
            symbol: order.symbol,
            order_id: order.id,
          }),
        });
        const payload = await res.json();
        if (!res.ok) throw new Error(payload.error || "cancel failed");
        if (payload.order_activity) {
          renderOpenSection("open-orders", () => renderOrderActivity(payload.order_activity));
        }
        await refresh();
      } catch (error) {
        text("orders-meta", `cancel failed: ${error.message || error}`);
        button.disabled = false;
        button.textContent = "Cancel";
      } finally {
        cancelOrderBusy.delete(key);
      }
    }

    function renderOpenOrders(orderActivity, bodyId = "open-orders", showActions = false) {
      const body = document.getElementById(bodyId);
      body.innerHTML = "";
      const orders = orderActivity?.open_orders || [];
      if (orders.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="${showActions ? 11 : 10}">No open orders.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const order of orders) {
        const tr = document.createElement("tr");
        const actionCell = showActions ? `<td class="order-action"></td>` : "";
        tr.innerHTML = `
          <td>${escapeHtml(order.label || order.exchange)}</td>
          <td>${escapeHtml(order.symbol || "--")}</td>
          <td class="${orderSideClass(order.side)}">${escapeHtml(order.side ? order.side.toUpperCase() : "--")}</td>
          <td>${escapeHtml(order.status || "--")}</td>
          <td class="num">${order.price == null ? "--" : fmt.format(order.price)}</td>
          <td class="num">${formatBalanceAmount(order.amount)}</td>
          <td class="num">${formatBalanceAmount(order.filled)}</td>
          <td class="num">${formatBalanceAmount(order.remaining)}</td>
          <td class="num">${formatBalanceAmount(order.cost)}</td>
          <td>${formatTimestamp(order.timestamp)}</td>
          ${actionCell}
        `;
        if (showActions) {
          const action = tr.querySelector(".order-action");
          const button = document.createElement("button");
          button.className = "danger-button";
          button.type = "button";
          button.textContent = "Cancel";
          button.disabled = !order.id;
          button.title = order.id || "";
          button.addEventListener("click", () => cancelOrder(order, button));
          action.appendChild(button);
        }
        body.appendChild(tr);
      }
    }

    function renderRecentFills(orderActivity, bodyId = "recent-fills") {
      const body = document.getElementById(bodyId);
      body.innerHTML = "";
      const fills = orderActivity?.recent_trades || [];
      if (fills.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="11">No recent fills.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const fill of fills) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(fill.label || fill.exchange)}</td>
          <td>${escapeHtml(fill.symbol || "--")}</td>
          <td class="${orderSideClass(fill.side)}">${escapeHtml(fill.side ? fill.side.toUpperCase() : "--")}</td>
          <td>${escapeHtml(fill.source_label || displaySource(fill.source))}</td>
          <td class="num">${fill.price == null ? "--" : fmt.format(fill.price)}</td>
          <td class="num">${formatBalanceAmount(fill.amount)}</td>
          <td class="num">${formatBalanceAmount(fill.cost)}</td>
          <td class="num ${pnlClass(fill.realized_pnl_common)}">${formatPnlValue(fill.realized_pnl_common)}</td>
          <td>${escapeHtml(formatFee(fill.fee))}</td>
          <td title="${escapeHtml(fill.order_id || "")}">${escapeHtml(shortId(fill.order_id))}</td>
          <td>${formatTimestamp(fill.timestamp)}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderOrderReconciliation(orderActivity) {
      const body = document.getElementById("order-reconciliation");
      if (!body) return;
      body.innerHTML = "";
      const reconciliation = orderActivity?.reconciliation || {};
      const issues = reconciliation.issues || [];
      if (issues.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7">Reconciliation OK.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const issue of issues) {
        const level = String(issue.level || "info").toLowerCase();
        const levelClass = level === "error" ? "risk-blocked" : level === "warning" ? "missing" : "subtle";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="${levelClass}">${escapeHtml(level.toUpperCase())}</td>
          <td>${escapeHtml(displayReconciliationType(issue.type))}</td>
          <td>${escapeHtml(displayStrategy(issue.strategy))}</td>
          <td>${escapeHtml(issue.exchange || "--")}</td>
          <td>${escapeHtml(issue.symbol || "--")}</td>
          <td title="${escapeHtml(issue.order_id || "")}">${escapeHtml(shortId(issue.order_id))}</td>
          <td title="${escapeHtml(issue.source_id || "")}">${escapeHtml(issue.message || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderOrderActivity(orderActivity) {
      const recentPnl = orderActivity?.pnl_summary?.total_realized_pnl;
      const dailyPnl = orderActivity?.daily_pnl?.enabled
        ? orderActivity?.daily_pnl?.total_realized_pnl
        : null;
      const storedFillCount = orderActivity?.pnl_store?.stored_fill_count;
      const reconciliation = orderActivity?.reconciliation || {};
      const criticalRecon = reconciliation.critical_issue_count || 0;
      const reconIssues = reconciliation.issue_count || 0;
      const reconNotices = reconciliation.notice_count || 0;
      const reconSuffix = reconciliation.auto_stop_suppressed
        ? ", suppressed"
        : "";
      const reconNoticeText = reconNotices > 0 ? `, notices ${reconNotices}` : "";
      const reconText = criticalRecon > 0
        ? `${reconciliation.status || "--"} (issues ${reconIssues}, critical ${criticalRecon}${reconNoticeText}${reconSuffix})`
        : reconIssues > 0
          ? `${reconciliation.status || "--"} (issues ${reconIssues}${reconNoticeText})`
          : reconNotices > 0
            ? `${reconciliation.status || "--"} (notices ${reconNotices})`
            : `${reconciliation.status || "--"} (0)`;
      const pnlText = dailyPnl == null
        ? `recent P/L ${formatPnlValue(recentPnl)}`
        : `daily P/L ${formatPnlValue(dailyPnl)} · recent ${formatPnlValue(recentPnl)} · stored ${storedFillCount || 0}`;
      text(
        "orders-meta",
        orderActivity
          ? `${orderActivity.status || "unknown"} · open ${orderActivity.open_order_count || 0} · fills ${orderActivity.recent_trade_count || 0} · recon ${reconText} · ${pnlText} · checked ${orderActivity.checked_account_count || 0}/${orderActivity.total_account_count || 0} · ${formatAge(orderActivity.last_finished)}`
          : ""
      );
      renderOpenOrders(orderActivity);
      renderRecentFills(orderActivity);
      renderOrderReconciliation(orderActivity);
    }

    let consoleActionBusy = false;

    async function cancelBulkOrders(payload, button) {
      if (consoleActionBusy) return;
      consoleActionBusy = true;
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = "Canceling";
      try {
        const res = await fetch("/api/orders/cancel-bulk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "cancel failed");
        if (result.order_activity) {
          renderOpenSection("open-orders", () => renderOrderActivity(result.order_activity));
        }
        await refresh();
      } catch (error) {
        text("console-meta", `cancel failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        button.textContent = originalText;
        consoleActionBusy = false;
      }
    }

    async function setStrategyPaused(strategyId, paused, button) {
      if (consoleActionBusy) return;
      consoleActionBusy = true;
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = paused ? "Pausing" : "Resuming";
      try {
        const res = await fetch("/api/strategies/control", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ strategy: strategyId, paused }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "strategy control failed");
        await refresh();
      } catch (error) {
        text("console-meta", `strategy failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        button.textContent = originalText;
        consoleActionBusy = false;
      }
    }

    function renderConsoleAccountActions(tradingConsole) {
      const body = document.getElementById("console-account-actions");
      body.innerHTML = "";
      const accounts = tradingConsole?.accounts || [];
      if (accounts.length === 0) {
        const empty = document.createElement("span");
        empty.className = "subtle";
        empty.textContent = "No accounts";
        body.appendChild(empty);
        return;
      }
      for (const account of accounts) {
        const button = document.createElement("button");
        button.className = "danger-button";
        button.type = "button";
        button.textContent = `Cancel ${account.label || account.key}`;
        button.disabled = (account.open_order_count || 0) <= 0;
        button.addEventListener("click", () => cancelBulkOrders({
          scope: "account",
          exchange: account.key,
        }, button));
        body.appendChild(button);
      }
    }

    function renderConsoleStrategies(tradingConsole) {
      const body = document.getElementById("console-strategies");
      body.innerHTML = "";
      const strategies = tradingConsole?.strategies || [];
      if (strategies.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7">No strategy status.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const strategy of strategies) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(strategy.label || strategy.id)}</td>
          <td class="${strategy.paused ? "risk-off" : strategy.configured ? "risk-ok" : "risk-off"}">${escapeHtml(strategy.paused ? "paused" : strategy.configured ? "enabled" : "disabled")}</td>
          <td class="${strategy.live ? "ok" : "missing"}">${strategy.live ? "YES" : "NO"}</td>
          <td>${escapeHtml(strategy.exchange || "--")}</td>
          <td>${escapeHtml(strategy.symbol || "--")}</td>
          <td>${escapeHtml(strategy.mode || "--")}</td>
          <td class="strategy-action"></td>
        `;
        const action = tr.querySelector(".strategy-action");
        const button = document.createElement("button");
        button.className = strategy.paused ? "control-button" : "danger-button";
        button.type = "button";
        button.textContent = strategy.paused ? "Resume" : "Pause";
        button.addEventListener("click", () => setStrategyPaused(strategy.id, !strategy.paused, button));
        action.appendChild(button);
        body.appendChild(tr);
      }
    }

    function renderTradingConsole(tradingConsole, orderActivity) {
      const openOrders = orderActivity?.open_order_count || 0;
      const recentFills = orderActivity?.recent_trade_count || 0;
      text(
        "console-meta",
        tradingConsole
          ? `${tradingConsole.live_trading ? "live allowed" : "live off"} · open ${openOrders} · fills ${recentFills} · ${formatAge(orderActivity?.last_finished)}`
          : ""
      );
      const allButton = document.getElementById("console-cancel-all");
      allButton.disabled = openOrders <= 0;
      allButton.onclick = () => cancelBulkOrders({ scope: "all" }, allButton);
      renderConsoleAccountActions(tradingConsole);
      renderConsoleStrategies(tradingConsole);
      renderOpenOrders(orderActivity, "console-open-orders", true);
      renderRecentFills(orderActivity, "console-recent-fills");
    }

    function readinessClass(status) {
      const value = String(status || "").toLowerCase();
      if (["ready", "live", "ok"].includes(value)) return "risk-ok";
      if (["blocked", "error"].includes(value)) return "risk-blocked";
      if (["warning", "guarded"].includes(value)) return "missing";
      if (["checking", "starting", "idle", "paused"].includes(value)) return "risk-off";
      return "risk-off";
    }

    function renderReadiness(readiness, runtimeStore) {
      const payload = readiness || {};
      const store = runtimeStore || {};
      const summary = payload.summary || {};
      const accounts = payload.accounts || [];
      const strategies = payload.strategies || [];
      const actions = payload.next_actions || [];
      const orderChecks = payload.order_checks || {};
      const balanceChecks = payload.balance_checks || {};
      const status = payload.status || "starting";

      text(
        "readiness-meta",
        `${status} · actions ${summary.action_count ?? actions.length} · blockers ${summary.blocked_count || 0} · warnings ${summary.warning_count || 0} · ${store.error ? "store error" : store.enabled ? "settings saved" : "settings memory-only"} · ${formatAge(payload.checked_at)}`
      );
      setValueState("readiness-status", status.toUpperCase(), readinessClass(status));
      text(
        "readiness-status-detail",
        payload.live_trading
          ? "global live enabled"
          : payload.risk_enabled === false
            ? "risk engine off"
            : "global live disabled"
      );
      setValueState(
        "readiness-accounts-summary",
        `${summary.ready_accounts || 0}/${summary.used_accounts || 0}`,
        summary.blocked_accounts > 0 ? "risk-blocked" : summary.warning_accounts > 0 ? "missing" : "risk-ok"
      );
      text(
        "readiness-accounts-detail",
        `${accounts.length} total · ${summary.idle_accounts || 0} idle`
      );
      setValueState(
        "readiness-strategies-summary",
        `${summary.live_strategies || 0}/${summary.configured_strategies || 0}`,
        summary.blocked_strategies > 0 ? "risk-blocked" : "risk-ok"
      );
      text(
        "readiness-strategies-detail",
        `${strategies.length} tracked · ${summary.paused_strategies || 0} paused`
      );
      setValueState(
        "readiness-orders-summary",
        orderChecks.reconciliation_status || orderChecks.status || "--",
        readinessClass(orderChecks.reconciliation_status || orderChecks.status)
      );
      text(
        "readiness-orders-detail",
        `orders ${orderChecks.status || "--"} · balances ${balanceChecks.status || "--"}`
      );

      const actionBody = document.getElementById("readiness-actions");
      actionBody.innerHTML = "";
      if (actions.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="5">No readiness actions.</td>`;
        actionBody.appendChild(tr);
      } else {
        for (const action of actions) {
          const level = String(action.priority || "info").toLowerCase();
          const levelClass = level === "high" ? "risk-blocked" : level === "medium" ? "missing" : "subtle";
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td class="${levelClass}">${escapeHtml(level.toUpperCase())}</td>
            <td>${escapeHtml(action.scope || "--")}</td>
            <td>${escapeHtml(action.action || "--")}</td>
            <td class="${readinessClass(action.status)}">${escapeHtml(action.status || "--")}</td>
            <td title="${escapeHtml(action.detail || "")}">${escapeHtml(action.detail || "--")}</td>
          `;
          actionBody.appendChild(tr);
        }
      }

      const accountBody = document.getElementById("readiness-accounts");
      accountBody.innerHTML = "";
      if (accounts.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="9">No accounts configured.</td>`;
        accountBody.appendChild(tr);
      } else {
        for (const account of accounts) {
          const tr = document.createElement("tr");
          const notes = (account.reasons || []).join(" · ") || "--";
          tr.innerHTML = `
            <td>${escapeHtml(account.label || account.key)}</td>
            <td>${escapeHtml(account.market_type || "--")}</td>
            <td title="${escapeHtml((account.symbols || []).join(", "))}">${escapeHtml(account.symbol_count ? String(account.symbol_count) : "--")}</td>
            <td class="${account.api_ready ? "risk-ok" : account.symbol_count ? "risk-blocked" : "risk-off"}">${escapeHtml(account.api_status || "--")}</td>
            <td class="${readinessClass(account.balance_status)}">${escapeHtml(account.balance_status || "--")}</td>
            <td class="${readinessClass(account.order_status)}">${escapeHtml(account.order_status || "--")}</td>
            <td class="${account.risk_enabled ? "risk-ok" : "risk-blocked"}">${account.risk_enabled ? "enabled" : "disabled"}</td>
            <td class="${readinessClass(account.status)}">${escapeHtml(account.status || "--")}</td>
            <td title="${escapeHtml(notes)}">${escapeHtml(notes)}</td>
          `;
          accountBody.appendChild(tr);
        }
      }

      const strategyBody = document.getElementById("readiness-strategies");
      strategyBody.innerHTML = "";
      if (strategies.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7">No strategies configured.</td>`;
        strategyBody.appendChild(tr);
        return;
      }
      for (const strategy of strategies) {
        const reasons = (strategy.reasons || []).join(" · ") || "--";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(strategy.label || displayStrategy(strategy.id))}</td>
          <td class="${strategy.configured ? "risk-ok" : "risk-off"}">${strategy.configured ? "yes" : "no"}</td>
          <td>${escapeHtml(strategy.exchange || "--")}</td>
          <td>${escapeHtml(strategy.symbol || "--")}</td>
          <td class="${strategy.live ? "risk-ok" : "risk-off"}">${strategy.live ? "YES" : "NO"}</td>
          <td class="${readinessClass(strategy.status)}">${escapeHtml(strategy.status || "--")}</td>
          <td title="${escapeHtml(reasons)}">${escapeHtml(reasons)}</td>
        `;
        strategyBody.appendChild(tr);
      }
    }

    function normalizeMarketRow(row) {
      const symbol = String(row.symbol || "").trim().toUpperCase();
      const quote = String(row.quote_currency || quoteCurrency(symbol)).trim().toUpperCase();
      return {
        asset: String(row.asset || baseCurrency(symbol)).trim().toUpperCase(),
        exchange: String(row.exchange || "").trim(),
        symbol,
        quote_currency: quote,
      };
    }

    function normalizeCashCarryPair(row) {
      return {
        spot_symbol: String(row.spot_symbol || "").trim().toUpperCase(),
        derivative_symbol: String(row.derivative_symbol || "").trim().toUpperCase(),
      };
    }

    function renderMarketExchangeSelect(exchanges) {
      const select = document.getElementById("market-exchange");
      const selected = select.value;
      const signature = JSON.stringify((exchanges || []).map((exchange) => [
        exchange.key,
        exchange.label,
        exchange.id,
        exchange.market_type,
      ]));
      if (select.dataset.signature === signature) return;
      select.dataset.signature = signature;
      select.innerHTML = "";
      for (const exchange of exchanges || []) {
        const option = document.createElement("option");
        option.value = exchange.key;
        option.textContent = exchange.label || exchange.key;
        select.appendChild(option);
      }
      if (selected && [...select.options].some((option) => option.value === selected)) {
        select.value = selected;
      }
    }

    function renderMarketsConfig(data) {
      if (marketsConfigBusy) return;
      const config = data.config || {};
      const exchanges = config.spot_exchanges || [];
      currentSpotMarkets = (config.spot_markets || []).map(normalizeMarketRow);
      renderMarketExchangeSelect(exchanges);
      text(
        "markets-config-meta",
        `${currentSpotMarkets.length} market${currentSpotMarkets.length === 1 ? "" : "s"} · ${exchanges.length} account${exchanges.length === 1 ? "" : "s"}`
      );

      const body = document.getElementById("markets-config");
      body.innerHTML = "";
      if (currentSpotMarkets.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="5">No markets configured.</td>`;
        body.appendChild(tr);
        return;
      }

      currentSpotMarkets.forEach((market, index) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(market.asset)}</td>
          <td>${escapeHtml(market.exchange)}</td>
          <td>${escapeHtml(market.symbol)}</td>
          <td>${escapeHtml(market.quote_currency)}</td>
          <td class="market-action"></td>
        `;
        const action = tr.querySelector(".market-action");
        const button = document.createElement("button");
        button.className = "danger-button";
        button.type = "button";
        button.textContent = "Remove";
        button.addEventListener("click", () => removeSpotMarket(index, button));
        action.appendChild(button);
        body.appendChild(tr);
      });
    }

    async function applySpotMarkets(markets) {
      if (marketsConfigBusy) return;
      marketsConfigBusy = true;
      try {
        const res = await fetch("/api/markets", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ spot_markets: markets }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "markets update failed");
        await refresh();
      } catch (error) {
        text("markets-config-meta", `update failed: ${error.message || error}`);
      } finally {
        marketsConfigBusy = false;
      }
    }

    async function addSpotMarket(event) {
      event.preventDefault();
      const exchange = document.getElementById("market-exchange").value;
      const symbol = document.getElementById("market-symbol").value.trim().toUpperCase();
      const asset = (
        document.getElementById("market-asset").value.trim().toUpperCase()
        || baseCurrency(symbol)
      );
      const nextMarket = normalizeMarketRow({ asset, exchange, symbol });
      await applySpotMarkets([...currentSpotMarkets, nextMarket]);
      document.getElementById("market-asset").value = "";
      document.getElementById("market-symbol").value = "";
    }

    async function removeSpotMarket(index, button) {
      button.disabled = true;
      await applySpotMarkets(
        currentSpotMarkets.filter((_, itemIndex) => itemIndex !== index)
      );
    }

    function renderCashCarryConfig(data) {
      if (carryConfigBusy) return;
      const config = data.config || {};
      const derivativeExchanges = config.derivative_exchanges || [];
      currentCashCarryPairs = (config.cash_and_carry_pairs || []).map(normalizeCashCarryPair);
      text(
        "carry-config-meta",
        `${currentCashCarryPairs.length} pair${currentCashCarryPairs.length === 1 ? "" : "s"} · ${derivativeExchanges.length} contract account${derivativeExchanges.length === 1 ? "" : "s"}`
      );

      const body = document.getElementById("carry-config");
      body.innerHTML = "";
      if (currentCashCarryPairs.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="3">No cash & carry pairs configured.</td>`;
        body.appendChild(tr);
        return;
      }

      currentCashCarryPairs.forEach((pair, index) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(pair.spot_symbol)}</td>
          <td>${escapeHtml(pair.derivative_symbol)}</td>
          <td class="carry-action"></td>
        `;
        const action = tr.querySelector(".carry-action");
        const button = document.createElement("button");
        button.className = "danger-button";
        button.type = "button";
        button.textContent = "Remove";
        button.addEventListener("click", () => removeCashCarryPair(index, button));
        action.appendChild(button);
        body.appendChild(tr);
      });
    }

    async function applyCashCarryPairs(pairs) {
      if (carryConfigBusy) return;
      carryConfigBusy = true;
      try {
        const res = await fetch("/api/cash-and-carry-pairs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cash_and_carry_pairs: pairs }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "cash & carry update failed");
        await refresh();
      } catch (error) {
        text("carry-config-meta", `update failed: ${error.message || error}`);
      } finally {
        carryConfigBusy = false;
      }
    }

    async function addCashCarryPair(event) {
      event.preventDefault();
      const pair = normalizeCashCarryPair({
        spot_symbol: document.getElementById("carry-spot-symbol").value,
        derivative_symbol: document.getElementById("carry-derivative-symbol").value,
      });
      await applyCashCarryPairs([...currentCashCarryPairs, pair]);
      document.getElementById("carry-spot-symbol").value = "";
      document.getElementById("carry-derivative-symbol").value = "";
    }

    async function removeCashCarryPair(index, button) {
      button.disabled = true;
      await applyCashCarryPairs(
        currentCashCarryPairs.filter((_, itemIndex) => itemIndex !== index)
      );
    }

    function renderStrategySummaries(data) {
      const warnings = data.warnings || [];
      const program = data.program || {};
      const scan = data.scan || {};
      const marketMaker = data.market_maker || {};
      const mmRuntime = marketMaker.runtime || {};
      const mmPlan = marketMaker.plan || mmRuntime.last_plan || null;
      const mmStatus = mmRuntime.status || marketMaker.status || "disabled";
      const mmMode = mmRuntime.mode || marketMaker.mode || "dry_run";
      text("monitor-mm-summary", `${mmMode} · ${mmStatus}`);
      text(
        "monitor-mm-detail",
        mmPlan
          ? `${mmPlan.exchange} ${mmPlan.symbol} · mid ${fmt.format(mmPlan.mid_price)} · open ${mmRuntime.open_order_count || 0}`
          : marketMaker.error || mmRuntime.reason || "--"
      );

      const auto = data.slow_execution || {};
      const autoTasks = auto.tasks?.tasks || [];
      const activeTasks = autoTasks.filter((task) => !["complete", "stopped_by_price", "below_min_order_quote"].includes(task.status));
      const autoStatus = auto.tasks
        ? `${activeTasks.length}/${autoTasks.length} active`
        : (auto.status || "disabled");
      const firstTask = activeTasks[0] || autoTasks[0];
      const autoDetail = firstTask
        ? `${String(firstTask.config?.side || "--").toUpperCase()} · ${firstTask.progress_pct == null ? "--" : firstTask.progress_pct.toFixed(1) + "%"} · ${firstTask.status || "--"}`
        : (auto.plan ? `${auto.plan.exchange} ${auto.plan.symbol} · ${String(auto.plan.side || "").toUpperCase()}` : "--");
      text("monitor-auto-summary", autoStatus);
      text("monitor-auto-detail", autoDetail);

      const risk = data.operations?.risk || {};
      const riskSummary = risk.allow_live_trading ? "Live allowed" : "Live blocked";
      const riskDetail = `order $${money.format(risk.max_order_quote || 0)} · exposure $${money.format(risk.max_exposure_quote || 0)} · open ${risk.max_open_orders || 0}`;
      text("monitor-risk-summary", riskSummary);
      text("monitor-risk-detail", riskDetail);

      const activity = data.order_activity || {};
      const openOrders = activity.open_order_count || 0;
      const fills = activity.recent_trade_count || 0;
      const recon = activity.reconciliation || {};
      const dailyPnl = activity.daily_pnl?.enabled
        ? activity.daily_pnl?.total_realized_pnl
        : activity.pnl_summary?.total_realized_pnl;
      text("monitor-orders-summary", `Open ${openOrders} · Fills ${fills}`);
      text("monitor-orders-detail", `P/L ${formatPnlValue(dailyPnl)} · ${formatAge(activity.last_finished)}`);
      const spot = data.spot_arbitrage || {};
      text("overview-meta", warnings.length ? `${warnings.length} warning(s)` : `updated ${formatAge(scan.last_finished)}`);
      text(
        "overview-program",
        `${program.running === false ? "Paused" : "Running"} · ${data.status || "--"}`
      );
      text(
        "overview-mm",
        `${mmMode} · ${mmStatus} · open ${mmRuntime.open_order_count || 0}`
      );
      text(
        "overview-arb",
        `${spot.mode || "dry_run"} · ${spot.status || "disabled"}`
      );
      text(
        "overview-orders",
        `open ${openOrders} · fills ${fills} · issues ${recon.issue_count || 0}`
      );
      text("overview-auto", autoDetail === "--" ? autoStatus : `${autoStatus} · ${autoDetail}`);
      text(
        "overview-risk",
        `${riskSummary} · max $${money.format(risk.max_order_quote || 0)}`
      );
    }

    function renderOpportunities(items) {
      const root = document.getElementById("opportunities");
      root.innerHTML = "";
      if (!items || items.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No active opportunities at the current threshold.";
        root.appendChild(empty);
        return;
      }

      for (const item of items) {
        const el = document.createElement("div");
        el.className = "opportunity";
        const legs = (item.legs || []).map((leg) => `
          <span class="leg">
            <span class="${leg.side === "buy" ? "side-buy" : "side-sell"}">${leg.side.toUpperCase()}</span>
            ${leg.exchange} ${leg.symbol}
            @ ${fmt.format(leg.average_price)}
          </span>
        `).join("");
        el.innerHTML = `
          <div><strong>$${money.format(item.profit_quote)}</strong><div class="subtle">profit</div></div>
          <div><strong>${item.profit_bps.toFixed(2)} bps</strong><div class="subtle">edge</div></div>
          <div class="legs">${legs}</div>
        `;
        root.appendChild(el);
      }
    }

    function renderRiskEvents(ops) {
      const risk = ops?.risk || {};
      const alerts = ops?.alerts || {};
      const tradeLog = ops?.trade_log || {};
      const timeline = ops?.strategy_timeline || {};
      const dailyPnl = ops?.daily_pnl || {};
      const summary = tradeLog.summary || {};
      const timelineSummary = timeline.summary || {};
      const riskState = risk.enabled === false ? "off" : risk.trading_enabled === false ? "trading off" : risk.allow_live_trading ? "live allowed" : "dry-run guarded";
      text(
        "risk-meta",
        `${riskState} · max/order $${money.format(risk.max_order_quote || 0)} · max/cycle $${money.format(risk.max_cycle_quote || 0)} · max/day $${money.format(risk.max_daily_loss_quote || 0)} · day P/L ${formatPnlValue(dailyPnl.total_realized_pnl || 0)} · open ${risk.max_open_orders || 0} · depth $${money.format(risk.min_order_book_depth_quote || 0)} · slip ${risk.max_slippage_bps || 0} bps · timeline ${timelineSummary.event_count || 0} · blocked ${timelineSummary.blocked_count || summary.blocked_event_count || 0} · alerts ${alerts.enabled ? "on" : "off"}`
      );

      const timelineBody = document.getElementById("strategy-timeline");
      timelineBody.innerHTML = "";
      const timelineEvents = timeline.recent_entries || [];
      if (timelineEvents.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="9">No strategy timeline events yet.</td>`;
        timelineBody.appendChild(tr);
      } else {
        for (const event of timelineEvents.slice(0, 30)) {
          const metrics = event.metrics || {};
          const reason = event.reason || event.risk_triggers?.[0] || "--";
          const latency = metrics.opportunity_to_submit_ms ?? metrics.opportunity_to_decision_ms ?? metrics.opportunity_age_ms;
          const slippage = metrics.max_slippage_bps;
          const statusClass = event.action === "blocked" || event.action === "execution_error" || event.action === "hedge_required"
            ? "risk-blocked"
            : event.action === "no_order" || event.action === "paused"
              ? "risk-off"
              : "risk-ok";
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${formatAge(event.logged_at)}</td>
            <td>${escapeHtml(displayStrategy(event.strategy || event.event_type || "--"))}</td>
            <td class="${statusClass}">${escapeHtml(event.action || "--")}</td>
            <td>${escapeHtml(event.status || "--")}</td>
            <td>${escapeHtml((event.accounts || []).join(", ") || "--")}</td>
            <td>${escapeHtml((event.symbols || []).join(", ") || "--")}</td>
            <td class="num">${latency == null ? "--" : `${Number(latency).toFixed(0)} ms`}</td>
            <td class="num">${slippage == null ? "--" : `${Number(slippage).toFixed(1)} bps`}</td>
            <td title="${escapeHtml(reason)}">${escapeHtml(reason)}</td>
          `;
          timelineBody.appendChild(tr);
        }
      }

      const body = document.getElementById("events");
      body.innerHTML = "";
      const events = tradeLog.recent_entries || [];
      if (events.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="14">No trade events yet.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const event of events.slice(0, 20)) {
        const riskClass = event.risk_level === "blocked" ? "risk-blocked" : event.risk_level === "off" ? "risk-off" : "risk-ok";
        const reason = event.reason || "--";
        const eventId = event.event_id || "";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(eventId)}">${escapeHtml(eventId.slice(0, 8) || "--")}</td>
          <td>${formatAge(event.logged_at)}</td>
          <td>${escapeHtml(displayStrategy(event.strategy))}</td>
          <td>${escapeHtml(event.mode || "--")}</td>
          <td>${escapeHtml(event.status || "--")}</td>
          <td>${escapeHtml(event.exchange || "--")}</td>
          <td>${escapeHtml(event.symbol || "--")}</td>
          <td class="${event.side === "buy" ? "side-buy" : event.side === "sell" ? "side-sell" : ""}">${escapeHtml(event.side ? event.side.toUpperCase() : "--")}</td>
          <td class="num">${event.order_count ?? "--"}</td>
          <td class="num">${event.placed_count ?? "--"}</td>
          <td class="num">${event.canceled_count ?? "--"}</td>
          <td class="num">${event.total_quote_notional == null ? "--" : "$" + money.format(event.total_quote_notional)}</td>
          <td class="${riskClass}">${escapeHtml(event.risk_level || "--")}</td>
          <td title="${escapeHtml(reason)}">${escapeHtml(reason)}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderAuditTrail(ops) {
      const audit = ops?.web_audit || {};
      text(
        "audit-meta",
        `${audit.enabled === false ? "off" : "on"} · ${audit.recent_events?.length || 0} recent · ${audit.error || audit.path || ""}`
      );
      const auditBody = document.getElementById("audit-events");
      auditBody.innerHTML = "";
      const auditEvents = audit.recent_events || [];
      if (auditEvents.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">No audit events yet.</td>`;
        auditBody.appendChild(tr);
        return;
      }
      for (const event of auditEvents.slice(0, 30)) {
        const statusClass = event.status === "ok" ? "risk-ok" : "risk-blocked";
        const detail = event.detail || event.error || "--";
        const target = event.target || event.strategy || event.exchange || "--";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${formatAge(event.logged_at)}</td>
          <td>${escapeHtml(event.action || "--")}</td>
          <td class="${statusClass}">${escapeHtml(event.status || "--")}</td>
          <td>${escapeHtml(event.actor_ip || "--")}</td>
          <td>${escapeHtml(target)}</td>
          <td title="${escapeHtml(detail)}">${escapeHtml(detail)}</td>
        `;
        auditBody.appendChild(tr);
      }
    }

    function renderOperations(ops) {
      renderRiskEvents(ops);
      renderAuditTrail(ops);
    }

    function setValueState(id, value, stateClass) {
      const el = document.getElementById(id);
      el.textContent = value;
      el.className = `value ${stateClass || ""}`.trim();
    }

    function firstRiskMessage(riskLike) {
      const reasons = Array.isArray(riskLike?.reasons) ? riskLike.reasons : [];
      if (reasons.length > 0) return reasons[0];
      const warnings = Array.isArray(riskLike?.warnings) ? riskLike.warnings : [];
      if (warnings.length > 0) return warnings[0];
      return "--";
    }

    function renderMarketMakerSafety(marketMaker) {
      const plan = marketMaker?.plan || {};
      const planOrders = Array.isArray(plan.orders) ? plan.orders : [];
      const safety = marketMaker?.safety || {};
      const runtimeRisk = marketMaker?.runtime?.last_risk || null;
      const risk = runtimeRisk || safety.risk || safety;
      const limits = safety.limits || {};
      const quoteRate = marketMaker?.quote_conversion?.quote_to_common_rate;
      const quoteRateValue = quoteRate == null ? 1 : Number(quoteRate);
      const planTotal = planOrders.reduce(
        (sum, order) => sum + Number(order.quote_notional || 0) * quoteRateValue,
        0
      );
      const totalQuote = safety.total_quote_notional ?? risk.total_quote_notional ?? planTotal;
      const largestOrder = safety.max_order_quote_notional ?? Math.max(0, ...planOrders.map((order) => Number(order.quote_notional || 0) * quoteRateValue));
      const orderCount = safety.order_count ?? risk.order_count ?? planOrders.length;
      const approved = risk.approved === true || safety.approved === true;
      const statusText = marketMaker?.status === "disabled"
        ? "Disabled"
        : approved ? "Ready" : "Blocked";
      const statusClass = marketMaker?.status === "disabled"
        ? "risk-off"
        : approved ? "risk-ok" : "risk-blocked";

      setValueState("mm-safety-status", statusText, statusClass);
      text("mm-safety-reason", firstRiskMessage(risk));
      setValueState(
        "mm-safety-orders",
        `${orderCount}/${limits.max_orders_per_cycle || "--"}`,
        limits.max_orders_per_cycle > 0 && orderCount > limits.max_orders_per_cycle ? "risk-blocked" : ""
      );
      text(
        "mm-safety-orders-detail",
        `buy ${safety.buy_order_count ?? "--"} · sell ${safety.sell_order_count ?? "--"} · open cap ${limits.max_open_orders || "--"}`
      );
      setValueState(
        "mm-safety-budget",
        `$${money.format(totalQuote || 0)}`,
        limits.max_cycle_quote > 0 && totalQuote > limits.max_cycle_quote ? "risk-blocked" : ""
      );
      text(
        "mm-safety-budget-detail",
        `largest $${money.format(largestOrder || 0)} / $${money.format(limits.max_order_quote || 0)} · cycle $${money.format(limits.max_cycle_quote || 0)}`
      );

      const market = safety.market || {};
      const age = market.order_book_received_at
        ? Math.max(0, Date.now() / 1000 - market.order_book_received_at)
        : market.order_book_timestamp_ms
          ? Math.max(0, Date.now() / 1000 - market.order_book_timestamp_ms / 1000)
          : null;
      setValueState(
        "mm-safety-market",
        market.existing_spread_bps == null ? "--" : `${Number(market.existing_spread_bps).toFixed(1)} bps`,
        ""
      );
      text(
        "mm-safety-market-detail",
        `depth ${money.format(market.bid_depth_quote || 0)}/${money.format(market.ask_depth_quote || 0)} · gap ${(market.max_level_gap_bps || 0).toFixed ? market.max_level_gap_bps.toFixed(1) : market.max_level_gap_bps || 0} bps · age ${age == null ? "--" : age.toFixed(1) + "s"}`
      );
      renderMarketMakerQuality(marketMaker);
    }

    function renderMarketMakerQuality(marketMaker) {
      const quality = marketMaker?.quality || {};
      const inventory = quality.inventory || {};
      const base = inventory.base;
      const deviation = inventory.deviation_base;
      const target = inventory.target_base;
      const buyMult = inventory.buy_multiplier;
      const sellMult = inventory.sell_multiplier;
      const daily = quality.daily || {};
      const usingDaily = quality.window === "daily_pnl";
      text(
        "mm-quality-inventory",
        base == null ? "--" : compact.format(base)
      );
      text(
        "mm-quality-inventory-detail",
        base == null
          ? "--"
          : `target ${compact.format(target || 0)} · dev ${compact.format(deviation || 0)} · buy ${buyMult == null ? "--" : Number(buyMult).toFixed(2)}x / sell ${sellMult == null ? "--" : Number(sellMult).toFixed(2)}x`
      );

      const buy = quality.buy || {};
      const sell = quality.sell || {};
      text(
        "mm-quality-fills",
        `${quality.trade_count || 0} ${usingDaily ? "today" : "recent"}`
      );
      text(
        "mm-quality-fills-detail",
        usingDaily
          ? `today notional $${money.format(daily.total_notional || 0)} · updated ${formatAge(daily.updated_at)}`
          : `buy ${buy.trade_count || 0} @ ${buy.average_price == null ? "--" : fmt.format(buy.average_price)} · sell ${sell.trade_count || 0} @ ${sell.average_price == null ? "--" : fmt.format(sell.average_price)}`
      );
      setValueState(
        "mm-quality-spread",
        quality.realized_spread_bps == null ? "--" : `${Number(quality.realized_spread_bps).toFixed(1)} bps`,
        quality.realized_spread_bps == null ? "" : quality.realized_spread_bps >= 0 ? "risk-ok" : "risk-blocked"
      );
      text(
        "mm-quality-spread-detail",
        `P/L ${formatPnlValue(quality.realized_pnl)} · fees ${formatPnlValue(-(quality.total_fees || 0))} · notional $${money.format(quality.total_notional || 0)}`
      );
    }

    function renderMarketMaker(marketMaker) {
      const body = document.getElementById("mm-orders");
      body.innerHTML = "";
      if (!marketMaker || !marketMaker.plan || !marketMaker.plan.orders || marketMaker.plan.orders.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">No market maker plan.</td>`;
        body.appendChild(tr);
        return;
      }

      const common = marketMaker.quote_conversion?.common_quote_currency || "USD";
      const rate = marketMaker.quote_conversion?.quote_to_common_rate;
      for (const order of marketMaker.plan.orders) {
        const commonQuote = rate == null ? "--" : `${common} ${money.format(order.quote_notional * rate)}`;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="${order.side === "buy" ? "side-buy" : "side-sell"}">${order.side.toUpperCase()}</td>
          <td class="num">${order.level}</td>
          <td class="num">${fmt.format(order.price)}</td>
          <td class="num">${compact.format(order.amount)}</td>
          <td class="num" title="${commonQuote}">${formatSymbolQuantity(order.quote_notional, marketMaker.plan.symbol, "quote")}</td>
          <td class="num">${order.distance_bps.toFixed(2)} bps</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderSlowExecution(slowExecution) {
      const body = document.getElementById("slow-orders");
      body.innerHTML = "";
      if (!slowExecution || !slowExecution.plan || !slowExecution.plan.order) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="12">${slowExecution?.status || "disabled"}</td>`;
        body.appendChild(tr);
        return;
      }

      const plan = slowExecution.plan;
      const order = plan.order;
      const progressMode = plan.progress_mode || ((plan.total_quote || 0) > 0 ? "quote" : "base");
      const unlimited = progressMode === "unlimited" || plan.unlimited_total;
      const submittedText = unlimited
        ? `${formatSymbolQuantity(order.submitted_base_before, plan.symbol, "base")} / Unlimited`
        : progressMode === "quote"
        ? `${formatSymbolQuantity(order.submitted_quote_before, plan.symbol, "quote")} / ${formatSymbolQuantity(plan.total_quote, plan.symbol, "quote")}`
        : `${formatSymbolQuantity(order.submitted_base_before, plan.symbol, "base")} / ${formatSymbolQuantity(plan.total_base, plan.symbol, "base")}`;
      const remainingText = unlimited
        ? "Unlimited"
        : progressMode === "quote"
        ? formatSymbolQuantity(plan.remaining_quote, plan.symbol, "quote")
        : formatSymbolQuantity(plan.remaining_base, plan.symbol, "base");
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="${order.side === "buy" ? "side-buy" : "side-sell"}">${order.side.toUpperCase()}</td>
        <td>${plan.exchange}</td>
        <td>${plan.symbol}</td>
        <td class="num">${fmt.format(order.price)}</td>
        <td class="num">${compact.format(order.amount)}</td>
        <td class="num">${money.format(order.quote_notional)}</td>
        <td class="num">${submittedText}</td>
        <td class="num">${remainingText}</td>
        <td class="num">${plan.interval_seconds}s</td>
        <td class="num">${plan.order_ttl_seconds || 0}s</td>
        <td class="num">${plan.start_price ? fmt.format(plan.start_price) : "--"}</td>
        <td class="num">${plan.stop_price ? fmt.format(plan.stop_price) : "--"}</td>
      `;
      body.appendChild(tr);
    }

    function renderSpotGrid(spotGrid) {
      const body = document.getElementById("grid-orders");
      body.innerHTML = "";
      const orders = spotGrid?.plan?.orders || [];
      if (orders.length === 0) {
        const tr = document.createElement("tr");
        const status = spotGrid?.error || spotGrid?.plan?.reason || spotGrid?.status || "disabled";
        tr.innerHTML = `<td colspan="6">${escapeHtml(status)}</td>`;
        body.appendChild(tr);
        return;
      }

      for (const order of orders) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="${order.side === "buy" ? "side-buy" : "side-sell"}">${String(order.side || "").toUpperCase()}</td>
          <td class="num">${order.level}</td>
          <td class="num">${fmt.format(order.price)}</td>
          <td class="num">${compact.format(order.amount)}</td>
          <td class="num">${formatSymbolQuantity(order.quote_notional, spotGrid.plan.symbol, "quote")}</td>
          <td class="num">${Number(order.distance_bps || 0).toFixed(2)} bps</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderDca(dca) {
      const body = document.getElementById("dca-orders");
      body.innerHTML = "";
      const plan = dca?.plan;
      const schedule = plan?.order_schedule || [];
      if (!plan || schedule.length === 0) {
        const tr = document.createElement("tr");
        const status = dca?.error || dca?.status || "disabled";
        tr.innerHTML = `<td colspan="6">${escapeHtml(status)}</td>`;
        body.appendChild(tr);
        return;
      }

      const nextOrder = plan.next_order;
      const displayPrice = nextOrder?.price || (plan.side === "buy" ? plan.best_ask : plan.best_bid);
      for (const row of schedule) {
        const isNext = nextOrder && Number(row.order_index) === Number(nextOrder.order_index);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="num">${row.order_index}</td>
          <td class="${plan.side === "buy" ? "side-buy" : "side-sell"}">${String(plan.side || "").toUpperCase()}</td>
          <td class="num">${fmt.format(displayPrice)}</td>
          <td class="num">${compact.format(row.amount_at_current_price || 0)}</td>
          <td class="num">${formatSymbolQuantity(row.quote_notional, plan.symbol, "quote")}</td>
          <td>${isNext ? "Next" : escapeHtml(plan.status || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderExecutionAlgo(executionAlgo) {
      const body = document.getElementById("exec-schedule");
      body.innerHTML = "";
      const plan = executionAlgo?.plan;
      const schedule = plan?.schedule || [];
      if (!plan || schedule.length === 0) {
        const tr = document.createElement("tr");
        const status = executionAlgo?.error || executionAlgo?.status || "disabled";
        tr.innerHTML = `<td colspan="7">${escapeHtml(status)}</td>`;
        body.appendChild(tr);
        return;
      }

      for (const item of schedule.slice(0, 40)) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="num">${item.slice_index}</td>
          <td class="${item.side === "buy" ? "side-buy" : "side-sell"}">${String(item.side || "").toUpperCase()}</td>
          <td class="num">${Number(item.scheduled_at_seconds || 0).toFixed(0)}s</td>
          <td class="num">${fmt.format(item.price)}</td>
          <td class="num">${compact.format(item.amount)}</td>
          <td class="num">${formatSymbolQuantity(item.quote_notional, plan.symbol, "quote")}</td>
          <td>${item.status === "next" ? "Next" : escapeHtml(item.status || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderBacktest(backtest) {
      const result = backtest?.result;
      text("backtest-return", result ? `${Number(result.return_pct || 0).toFixed(2)}%` : "--");
      text("backtest-drawdown", result ? `${Number(result.max_drawdown_pct || 0).toFixed(2)}%` : "--");
      text("backtest-fees", result ? money.format(result.fee_quote || 0) : "--");
      text("backtest-fill-rate", result ? `${(Number(result.fill_rate || 0) * 100).toFixed(1)}%` : "--");
      const body = document.getElementById("backtest-points");
      body.innerHTML = "";
      const points = result?.points || [];
      if (!result || points.length === 0) {
        const tr = document.createElement("tr");
        const status = backtest?.error || backtest?.status || "disabled";
        tr.innerHTML = `<td colspan="6">${escapeHtml(status)}</td>`;
        body.appendChild(tr);
        return;
      }
      for (const point of points.slice(-80)) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="num">${point.step}</td>
          <td class="num">${fmt.format(point.price)}</td>
          <td class="num">${money.format(point.equity)}</td>
          <td class="num">${Number(point.drawdown_pct || 0).toFixed(2)}%</td>
          <td class="num">${compact.format(point.base)}</td>
          <td class="num">${money.format(point.cash)}</td>
        `;
        body.appendChild(tr);
      }
    }

    function setFieldValue(id, value) {
      const el = document.getElementById(id);
      if (el) el.value = value == null ? "" : String(value);
    }

    function setCheckedValue(id, value) {
      const el = document.getElementById(id);
      if (el) el.checked = Boolean(value);
    }

    function parseJsonField(id) {
      const value = document.getElementById(id).value.trim();
      if (!value) return {};
      const parsed = JSON.parse(value);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
        throw new Error(`${id} must be a JSON object`);
      }
      return parsed;
    }

    function setJsonField(id, value) {
      document.getElementById(id).value = JSON.stringify(value || {}, null, 2);
    }

    function splitCsv(value) {
      return String(value || "")
        .split(/[,\s]+/)
        .map((item) => item.trim().toUpperCase())
        .filter(Boolean);
    }

    function strategyUniverseAccounts(kind = "all") {
      const universe = lastState?.config?.strategy_universe || {};
      return universe?.[kind]?.accounts || [];
    }

    function appendOption(select, value, label, title = "") {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      if (title) option.title = title;
      select.appendChild(option);
    }

    function setSelectOptions(selectId, rows, selectedValue, placeholder) {
      const select = document.getElementById(selectId);
      if (!select) return;
      const normalizedRows = rows.filter((row) => row && row.value);
      const signature = JSON.stringify({
        rows: normalizedRows,
        selectedValue,
        placeholder,
      });
      if (select.dataset.signature === signature) return;
      select.dataset.signature = signature;
      select.innerHTML = "";
      appendOption(select, "", placeholder);
      for (const row of normalizedRows) {
        appendOption(select, row.value, row.label || row.value, row.title || "");
      }
      if (selectedValue && !normalizedRows.some((row) => row.value === selectedValue)) {
        appendOption(select, selectedValue, selectedValue, "Current saved value");
      }
      select.value = selectedValue || "";
    }

    function renderStrategyInstanceAccountOptions(selectedAccountId) {
      const accounts = lastState?.strategy_center?.user_api_accounts || [];
      const rows = accounts.map((account) => ({
        value: account.id,
        label: `${account.label || account.id} · ${account.exchange || "--"}`,
        title: `${account.owner_email || "--"} · ${(account.asset_scope || []).join(", ") || "all assets"}`,
      }));
      setSelectOptions(
        "strategy-instance-account",
        rows,
        selectedAccountId || "",
        "No API account"
      );
    }

    function renderStrategyInstanceMarketOptions(selectedExchange, selectedSymbol) {
      const accounts = strategyUniverseAccounts("all");
      const exchangeRows = accounts.map((account) => ({
        value: account.key,
        label: `${account.label || account.key} (${account.market_type || "spot"})`,
        title: `${account.id || account.key} · ${accountSymbols(account).join(", ") || "no symbols"}`,
      }));
      setSelectOptions(
        "strategy-instance-exchange",
        exchangeRows,
        selectedExchange || "",
        "Select exchange"
      );

      const account = accountForKey(accounts, selectedExchange);
      const symbolRows = accountSymbols(account).map((symbol) => ({
        value: symbol,
        label: symbol,
      }));
      const targetSymbol = selectedSymbol || symbolRows[0]?.value || "";
      setSelectOptions(
        "strategy-instance-symbol",
        symbolRows,
        targetSymbol,
        "Select symbol"
      );
    }

    function syncStrategyInstanceSymbols() {
      const exchange = document.getElementById("strategy-instance-exchange").value;
      renderStrategyInstanceMarketOptions(exchange, "");
      const symbol = document.getElementById("strategy-instance-symbol").value;
      const asset = document.getElementById("strategy-instance-asset");
      if (symbol && !asset.value.trim()) asset.value = baseCurrency(symbol);
    }

    function renderStrategyForm(strategy) {
      if (strategyCenterFormDirty || strategyCenterFormBusy) return;
      const authEmail = lastState?.auth?.email || "";
      renderStrategyInstanceAccountOptions(strategy?.account_id || "");
      renderStrategyInstanceMarketOptions(strategy?.exchange || "", strategy?.symbol || "");
      setFieldValue("strategy-instance-id", strategy?.id || "");
      setFieldValue("strategy-instance-name", strategy?.name || "");
      setFieldValue("strategy-instance-type", strategy?.strategy_type || "market_maker");
      setFieldValue("strategy-instance-owner", strategy?.owner_email || authEmail);
      setFieldValue("strategy-instance-account", strategy?.account_id || "");
      setFieldValue("strategy-instance-exchange", strategy?.exchange || "");
      setFieldValue("strategy-instance-symbol", strategy?.symbol || "");
      setFieldValue("strategy-instance-asset", strategy?.asset || "");
      setCheckedValue("strategy-instance-enabled", strategy?.enabled);
      setCheckedValue("strategy-instance-live", strategy?.live_enabled);
      setJsonField("strategy-instance-params", strategy?.parameters || {});
      setJsonField("strategy-instance-risk", strategy?.risk_overrides || {});
    }

    function fillStrategyForm(strategy) {
      strategyCenterFormDirty = false;
      strategyCenterFormBusy = false;
      renderStrategyForm(strategy);
    }

    function renderStrategyInstances(center) {
      const body = document.getElementById("strategy-instances");
      body.innerHTML = "";
      const strategies = center?.strategy_instances || [];
      if (strategies.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="9">No strategy instances yet.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const strategy of strategies) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(strategy.id || "")}">${escapeHtml(strategy.name || shortId(strategy.id))}</td>
          <td>${escapeHtml(displayStrategy(strategy.strategy_type))}</td>
          <td>${escapeHtml(strategy.owner_email || "--")}</td>
          <td>${escapeHtml(strategy.account_id || "--")}</td>
          <td>${escapeHtml(strategy.exchange || "--")}<br><span class="subtle">${escapeHtml(strategy.symbol || strategy.asset || "--")}</span></td>
          <td class="${strategy.enabled ? "ok" : "subtle"}">${strategy.live_enabled ? "live ready" : (strategy.status || (strategy.enabled ? "enabled" : "draft"))}</td>
          <td class="num">${money.format(strategy.pnl_quote || 0)}</td>
          <td class="num">${strategy.open_order_count || 0}</td>
          <td class="strategy-action"></td>
        `;
        const actionCell = tr.querySelector(".strategy-action");
        const editButton = document.createElement("button");
        editButton.className = "control-button";
        editButton.type = "button";
        editButton.textContent = "Edit";
        editButton.addEventListener("click", () => fillStrategyForm(strategy));
        actionCell.appendChild(editButton);
        const deleteButton = document.createElement("button");
        deleteButton.className = "danger-button";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";
        deleteButton.addEventListener("click", () => deleteStrategyInstance(strategy.id, deleteButton));
        actionCell.appendChild(deleteButton);
        body.appendChild(tr);
      }
    }

    function strategyPayloadFromForm() {
      const id = document.getElementById("strategy-instance-id").value.trim();
      const payload = {
        name: document.getElementById("strategy-instance-name").value.trim(),
        strategy_type: document.getElementById("strategy-instance-type").value,
        owner_email: document.getElementById("strategy-instance-owner").value.trim(),
        account_id: document.getElementById("strategy-instance-account").value.trim(),
        exchange: document.getElementById("strategy-instance-exchange").value.trim(),
        symbol: document.getElementById("strategy-instance-symbol").value.trim(),
        asset: document.getElementById("strategy-instance-asset").value.trim().toUpperCase(),
        enabled: document.getElementById("strategy-instance-enabled").checked,
        live_enabled: document.getElementById("strategy-instance-live").checked,
        parameters: parseJsonField("strategy-instance-params"),
        risk_overrides: parseJsonField("strategy-instance-risk"),
      };
      if (id) payload.id = id;
      return payload;
    }

    async function applyStrategyCenterConfig(event) {
      event.preventDefault();
      if (strategyCenterFormBusy) return;
      strategyCenterFormBusy = true;
      const button = document.getElementById("strategy-center-apply");
      button.disabled = true;
      try {
        const res = await fetch("/api/strategy-center", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "upsert_strategy", strategy: strategyPayloadFromForm() }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "strategy update failed");
        strategyCenterFormDirty = false;
        if (lastState) lastState.strategy_center = result.strategy_center;
        renderStrategyCenter(result.strategy_center);
      } catch (error) {
        text("strategy-center-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        strategyCenterFormBusy = false;
      }
    }

    async function deleteStrategyInstance(strategyId, button) {
      if (!strategyId) return;
      button.disabled = true;
      try {
        const res = await fetch("/api/strategy-center", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "delete_strategy", strategy_id: strategyId }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "delete failed");
        if (lastState) lastState.strategy_center = result.strategy_center;
        renderStrategyCenter(result.strategy_center);
      } catch (error) {
        text("strategy-center-meta", `delete failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    function renderApiAccountForm(account) {
      if (apiAccountFormDirty || apiAccountFormBusy) return;
      const authEmail = lastState?.auth?.email || "";
      setFieldValue("api-account-id", account?.id || "");
      setFieldValue("api-account-owner", account?.owner_email || authEmail);
      setFieldValue("api-account-label", account?.label || "");
      setFieldValue("api-account-exchange", account?.exchange || "");
      setFieldValue("api-account-market-type", account?.market_type || "spot");
      setFieldValue("api-account-assets", (account?.asset_scope || []).join(","));
      setFieldValue("api-account-key-env", account?.api_key_env || "");
      setFieldValue("api-account-secret-env", account?.secret_env || "");
      setFieldValue("api-account-password-env", account?.password_env || "");
      setFieldValue("api-account-proxy-env", account?.proxy_env || "");
      setCheckedValue("api-account-enabled", account?.enabled);
      setFieldValue("api-account-ip", account?.ip_label || "");
    }

    function fillApiAccountForm(account) {
      apiAccountFormDirty = false;
      apiAccountFormBusy = false;
      renderApiAccountForm(account);
    }

    function renderApiAccounts(center) {
      const body = document.getElementById("api-accounts");
      body.innerHTML = "";
      const accounts = center?.user_api_accounts || [];
      text("api-accounts-meta", `${accounts.length} account refs · env names only`);
      if (accounts.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7">No user API account references yet.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const account of accounts) {
        const auth = account.auth || {};
        const missing = auth.missing_env || [];
        const envStatus = missing.length ? `missing ${missing.length}` : (auth.configured ? "set" : "not set");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(account.id || "")}">${escapeHtml(account.label || shortId(account.id))}</td>
          <td>${escapeHtml(account.owner_email || "--")}</td>
          <td>${escapeHtml(account.exchange || "--")}<br><span class="subtle">${escapeHtml(account.market_type || "spot")}</span></td>
          <td>${escapeHtml((account.asset_scope || []).join(", ") || "all")}</td>
          <td class="${missing.length ? "missing" : "ok"}">${escapeHtml(envStatus)}</td>
          <td>${escapeHtml(account.ip_label || "--")}</td>
          <td class="strategy-action"></td>
        `;
        const actionCell = tr.querySelector(".strategy-action");
        const editButton = document.createElement("button");
        editButton.className = "control-button";
        editButton.type = "button";
        editButton.textContent = "Edit";
        editButton.addEventListener("click", () => fillApiAccountForm(account));
        actionCell.appendChild(editButton);
        const deleteButton = document.createElement("button");
        deleteButton.className = "danger-button";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";
        deleteButton.addEventListener("click", () => deleteApiAccount(account.id, deleteButton));
        actionCell.appendChild(deleteButton);
        body.appendChild(tr);
      }
    }

    function apiAccountPayloadFromForm() {
      const id = document.getElementById("api-account-id").value.trim();
      const payload = {
        owner_email: document.getElementById("api-account-owner").value.trim(),
        label: document.getElementById("api-account-label").value.trim(),
        exchange: document.getElementById("api-account-exchange").value.trim(),
        market_type: document.getElementById("api-account-market-type").value,
        asset_scope: splitCsv(document.getElementById("api-account-assets").value),
        api_key_env: document.getElementById("api-account-key-env").value.trim(),
        secret_env: document.getElementById("api-account-secret-env").value.trim(),
        password_env: document.getElementById("api-account-password-env").value.trim(),
        proxy_env: document.getElementById("api-account-proxy-env").value.trim(),
        enabled: document.getElementById("api-account-enabled").checked,
        ip_label: document.getElementById("api-account-ip").value.trim(),
      };
      if (id) payload.id = id;
      return payload;
    }

    async function applyApiAccountConfig(event) {
      event.preventDefault();
      if (apiAccountFormBusy) return;
      apiAccountFormBusy = true;
      const button = document.getElementById("api-account-apply");
      button.disabled = true;
      try {
        const res = await fetch("/api/strategy-center", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "upsert_account", account: apiAccountPayloadFromForm() }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "api account update failed");
        apiAccountFormDirty = false;
        if (lastState) lastState.strategy_center = result.strategy_center;
        renderApiAccountsPanel(result.strategy_center);
        renderOpenSection("strategy-instances", () => renderStrategyCenter(result.strategy_center));
      } catch (error) {
        text("api-accounts-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        apiAccountFormBusy = false;
      }
    }

    async function deleteApiAccount(accountId, button) {
      if (!accountId) return;
      button.disabled = true;
      try {
        const res = await fetch("/api/strategy-center", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "delete_account", account_id: accountId }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "delete failed");
        if (lastState) lastState.strategy_center = result.strategy_center;
        renderApiAccountsPanel(result.strategy_center);
        renderOpenSection("strategy-instances", () => renderStrategyCenter(result.strategy_center));
      } catch (error) {
        text("api-accounts-meta", `delete failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    function renderFundingArbConfig(config) {
      if (fundingArbFormDirty || fundingArbFormBusy) return;
      const row = config || {};
      setCheckedValue("funding-enabled", row.enabled);
      setFieldValue("funding-pair-id", row.pair_id || "");
      setFieldValue("funding-spot-exchange", row.spot_exchange || "");
      setFieldValue("funding-spot-symbol", row.spot_symbol || "");
      setFieldValue("funding-derivative-exchange", row.derivative_exchange || "");
      setFieldValue("funding-derivative-symbol", row.derivative_symbol || "");
      setNumericField("funding-predicted-bps", row.predicted_funding_rate_bps || 0);
      setNumericField("funding-min-funding-bps", row.min_funding_bps || 0);
      setNumericField("funding-min-entry-bps", row.min_entry_basis_bps || 0);
      setNumericField("funding-take-profit-bps", row.take_profit_bps || 0);
      setNumericField("funding-stop-loss-bps", row.stop_loss_bps || 0);
      setNumericField("funding-margin-pct", row.max_margin_usage_pct || 0);
      setNumericField("funding-liq-buffer-pct", row.min_liquidation_buffer_pct || 0);
    }

    function fundingArbPayloadFromForm() {
      return {
        enabled: document.getElementById("funding-enabled").checked,
        pair_id: document.getElementById("funding-pair-id").value.trim(),
        spot_exchange: document.getElementById("funding-spot-exchange").value.trim(),
        spot_symbol: document.getElementById("funding-spot-symbol").value.trim(),
        derivative_exchange: document.getElementById("funding-derivative-exchange").value.trim(),
        derivative_symbol: document.getElementById("funding-derivative-symbol").value.trim(),
        predicted_funding_rate_bps: numericValue("funding-predicted-bps"),
        min_funding_bps: numericValue("funding-min-funding-bps"),
        min_entry_basis_bps: numericValue("funding-min-entry-bps"),
        take_profit_bps: numericValue("funding-take-profit-bps"),
        stop_loss_bps: numericValue("funding-stop-loss-bps"),
        max_margin_usage_pct: numericValue("funding-margin-pct"),
        min_liquidation_buffer_pct: numericValue("funding-liq-buffer-pct"),
      };
    }

    async function applyFundingArbConfig(event) {
      event.preventDefault();
      if (fundingArbFormBusy) return;
      fundingArbFormBusy = true;
      const button = document.getElementById("funding-arb-apply");
      button.disabled = true;
      try {
        const res = await fetch("/api/strategy-center", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "update_funding", funding_arbitrage: fundingArbPayloadFromForm() }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "funding update failed");
        fundingArbFormDirty = false;
        if (lastState) lastState.strategy_center = result.strategy_center;
        renderFundingArbitragePanel(result.strategy_center);
      } catch (error) {
        text("funding-arb-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        fundingArbFormBusy = false;
      }
    }

    function renderSignalBotConfig(config) {
      if (signalBotFormDirty || signalBotFormBusy) return;
      const row = config || {};
      setCheckedValue("signal-bot-enabled", row.enabled);
      setCheckedValue("signal-bot-custom", row.allow_custom_webhook !== false);
      setFieldValue("signal-bot-secret-env", row.webhook_secret_env || "SIGNAL_BOT_WEBHOOK_SECRET");
      setFieldValue("signal-bot-default-strategy", row.default_strategy_id || "");
      setNumericField("signal-bot-age", row.max_signal_age_seconds || 60);
      setNumericField("signal-bot-dedupe", row.dedupe_seconds || 300);
      text("signal-webhook-url", `${window.location.origin}/api/signal/tradingview`);
    }

    function signalBotPayloadFromForm() {
      return {
        enabled: document.getElementById("signal-bot-enabled").checked,
        allow_custom_webhook: document.getElementById("signal-bot-custom").checked,
        webhook_secret_env: document.getElementById("signal-bot-secret-env").value.trim(),
        default_strategy_id: document.getElementById("signal-bot-default-strategy").value.trim(),
        max_signal_age_seconds: numericValue("signal-bot-age"),
        dedupe_seconds: numericValue("signal-bot-dedupe"),
        allowed_sources: ["tradingview", "custom"],
      };
    }

    async function applySignalBotConfig(event) {
      event.preventDefault();
      if (signalBotFormBusy) return;
      signalBotFormBusy = true;
      const button = document.getElementById("signal-bot-apply");
      button.disabled = true;
      try {
        const res = await fetch("/api/strategy-center", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "update_signal_bot", signal_bot: signalBotPayloadFromForm() }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "signal bot update failed");
        signalBotFormDirty = false;
        if (lastState) lastState.strategy_center = result.strategy_center;
        renderSignalBotPanel(result.strategy_center);
      } catch (error) {
        text("signal-bot-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        signalBotFormBusy = false;
      }
    }

    function renderSignalEvents(center) {
      const body = document.getElementById("signal-events");
      body.innerHTML = "";
      const signals = center?.signals || [];
      if (signals.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7">No signals received yet.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const signal of signals.slice(0, 40)) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${formatTimestamp((signal.received_at || 0) * 1000)}</td>
          <td>${escapeHtml(signal.source || "--")}</td>
          <td title="${escapeHtml(signal.strategy_id || "")}">${escapeHtml(shortId(signal.strategy_id))}</td>
          <td>${escapeHtml(signal.symbol || "--")}</td>
          <td>${escapeHtml(signal.action || signal.side || "--")}</td>
          <td class="${signal.status === "accepted" ? "ok" : signal.status === "blocked" ? "missing" : "subtle"}">${escapeHtml(signal.status || "--")}</td>
          <td>${escapeHtml(signal.reason || signal.message || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderStrategyCenter(center) {
      const summary = center?.summary || {};
      text(
        "strategy-center-meta",
        `${center?.status || "disabled"} · ${summary.strategy_count || 0} strategies · ${summary.api_account_count || 0} accounts · ${summary.recent_signal_count || 0} signals`
      );
      renderStrategyForm((center?.strategy_instances || [])[0] || null);
      renderStrategyInstances(center);
    }

    function renderApiAccountsPanel(center) {
      renderApiAccountForm((center?.user_api_accounts || [])[0] || null);
      renderApiAccounts(center);
    }

    function renderFundingArbitragePanel(center) {
      const funding = center?.funding_arbitrage || {};
      text(
        "funding-arb-meta",
        `${funding.enabled ? "enabled" : "disabled"} · ${funding.spot_symbol || "--"} / ${funding.derivative_symbol || "--"}`
      );
      renderFundingArbConfig(center?.funding_arbitrage);
    }

    function renderSignalBotPanel(center) {
      const signalBot = center?.signal_bot || {};
      text(
        "signal-bot-meta",
        `${signalBot.enabled ? "enabled" : "disabled"} · secret ${signalBot.webhook_secret_set ? "set" : "missing"}`
      );
      renderSignalBotConfig(center?.signal_bot);
      renderSignalEvents(center);
    }

    function formatDue(ts) {
      if (!ts) return "--";
      const seconds = ts - Date.now() / 1000;
      return seconds <= 0 ? "due" : `${seconds.toFixed(0)}s`;
    }

    const AUTO_TERMINAL_STATUSES = new Set(["complete", "stopped", "stopped_by_price", "below_min_order_quote"]);
    const AUTO_CONFIG_COMPARE_FIELDS = [
      ["exchange", "Account", "string"],
      ["symbol", "Symbol", "string"],
      ["side", "Side", "string"],
      ["price_mode", "Price", "string"],
      ["price_offset_bps", "Offset", "number"],
      ["unlimited_total", "Unlimited", "boolean"],
      ["total_base", "Total Base", "number"],
      ["total_quote", "Total Quote", "number"],
      ["slice_mode", "Size Mode", "string"],
      ["slice_base", "Base/Order", "number"],
      ["slice_quote", "Quote/Order", "number"],
      ["slice_base_min", "Min Base", "number"],
      ["slice_base_max", "Max Base", "number"],
      ["randomize_slice", "Random", "boolean"],
      ["interval_seconds", "Place Sec", "number"],
      ["order_ttl_seconds", "Cancel Sec", "number"],
      ["start_price", "Start", "number"],
      ["stop_price", "Stop", "number"],
    ];

    function normalizeAutoConfigValue(value, type) {
      if (type === "boolean") return Boolean(value);
      if (type === "number") {
        const number = Number(value || 0);
        return Number.isFinite(number) ? Math.round(number * 1e12) / 1e12 : 0;
      }
      return String(value ?? "").trim();
    }

    function autoConfigValueText(value, type) {
      if (type === "boolean") return Boolean(value) ? "on" : "off";
      if (type === "number") return fmt.format(Number(value || 0));
      return String(value ?? "--") || "--";
    }

    function autoStartGateText(config) {
      const start = Number(config?.start_price || 0);
      if (start <= 0) return "start off";
      return config?.side === "sell"
        ? `start bid >= ${fmt.format(start)}`
        : `start ask <= ${fmt.format(start)}`;
    }

    function autoStopGateText(config) {
      const stop = Number(config?.stop_price || 0);
      if (stop <= 0) return "stop off";
      return config?.side === "sell"
        ? `stop bid <= ${fmt.format(stop)}`
        : `stop ask <= ${fmt.format(stop)}`;
    }

    function autoConfigSummary(config) {
      if (!config) return "No default config";
      const side = String(config.side || "--").toUpperCase();
      const total = Number(config.total_quote || 0) > 0
        ? `${quoteCurrency(config.symbol)} ${fmt.format(config.total_quote)}`
        : Number(config.total_base || 0) > 0
        ? `${baseCurrency(config.symbol)} ${fmt.format(config.total_base)}`
        : (config.unlimited_total ? "Unlimited" : "No target");
      const slice = config.slice_mode === "top_level"
        ? "Top level size"
        : Number(config.slice_base_min || 0) || Number(config.slice_base_max || 0)
        ? `${baseCurrency(config.symbol)} ${fmt.format(config.slice_base_min || 0)}-${fmt.format(config.slice_base_max || 0)}`
        : Number(config.slice_quote || 0) > 0
        ? `${quoteCurrency(config.symbol)} ${fmt.format(config.slice_quote)}`
        : `${baseCurrency(config.symbol)} ${fmt.format(config.slice_base || 0)}`;
      return `${config.exchange || "--"} ${config.symbol || "--"} · ${side} · ${config.price_mode || "--"} · target ${total} · size ${slice} · ${autoStartGateText(config)} · ${autoStopGateText(config)} · every ${fmt.format(config.interval_seconds || 0)}s`;
    }

    function compareAutoTaskConfig(taskConfig, defaultConfig) {
      if (!taskConfig || !defaultConfig) return [];
      return AUTO_CONFIG_COMPARE_FIELDS
        .map(([key, label, type]) => {
          const taskValue = normalizeAutoConfigValue(taskConfig[key], type);
          const defaultValue = normalizeAutoConfigValue(defaultConfig[key], type);
          if (taskValue === defaultValue) return null;
          return {
            key,
            label,
            task: autoConfigValueText(taskConfig[key], type),
            default: autoConfigValueText(defaultConfig[key], type),
          };
        })
        .filter(Boolean);
    }

    function renderSlowConfigStatus(taskPayload, defaultConfig) {
      const box = document.getElementById("slow-config-status");
      if (!box) return;
      const tasks = taskPayload?.tasks || [];
      const runningTasks = tasks.filter((task) => !AUTO_TERMINAL_STATUSES.has(task.status || ""));
      const compared = runningTasks.map((task) => ({
        task,
        diffs: compareAutoTaskConfig(task.config || {}, defaultConfig || {}),
      }));
      const diffTasks = compared.filter((item) => item.diffs.length > 0);
      const defaultText = autoConfigSummary(defaultConfig);
      if (runningTasks.length === 0) {
        box.innerHTML = `
          <div><span class="config-chip config-neutral">Default</span>${escapeHtml(defaultText)}</div>
          <div class="subtle">No active Auto Buy/Sell task. New tasks will use the default configuration above.</div>
        `;
        return;
      }
      const statusClass = diffTasks.length ? "config-diff" : "config-same";
      const statusText = diffTasks.length
        ? `${diffTasks.length}/${runningTasks.length} active task(s) differ from defaults`
        : `${runningTasks.length} active task(s) match defaults`;
      const details = compared
        .slice(0, 4)
        .map(({ task, diffs }) => {
          const label = `${shortId(task.id)} ${task.config?.exchange || "--"} ${task.config?.symbol || "--"}`;
          if (!diffs.length) return `<li><strong>${escapeHtml(label)}</strong>: matches default</li>`;
          const diffText = diffs.slice(0, 5).map((diff) => `${diff.label}: ${diff.task} vs ${diff.default}`).join("; ");
          const more = diffs.length > 5 ? `; +${diffs.length - 5} more` : "";
          return `<li><strong>${escapeHtml(label)}</strong>: ${escapeHtml(diffText + more)}</li>`;
        })
        .join("");
      box.innerHTML = `
        <div><span class="config-chip config-neutral">Default</span>${escapeHtml(defaultText)}</div>
        <div><span class="config-chip ${statusClass}">${diffTasks.length ? "Different" : "Same"}</span>${escapeHtml(statusText)}</div>
        <ul>${details}</ul>
      `;
    }

    function autoTaskConfigCell(task, defaultConfig) {
      const diffs = compareAutoTaskConfig(task.config || {}, defaultConfig || {});
      if (!diffs.length) {
        return {
          className: "risk-ok",
          html: "Same as default",
          title: "Current running task config matches the default form config",
        };
      }
      const title = diffs
        .map((diff) => `${diff.label}: task ${diff.task}, default ${diff.default}`)
        .join("\n");
      const rows = diffs
        .map((diff) => `
          <div class="config-diff-row">
            <span>${escapeHtml(diff.label)}</span>
            <span title="Running task">${escapeHtml(diff.task)}</span>
            <span title="Default form">${escapeHtml(diff.default)}</span>
          </div>
        `)
        .join("");
      return {
        className: "missing",
        html: `
          <details class="config-diff-details">
            <summary>${diffs.length} diff${diffs.length === 1 ? "" : "s"}</summary>
            <div class="config-diff-grid">
              <div class="config-diff-head">Field</div>
              <div class="config-diff-head">Task</div>
              <div class="config-diff-head">Default</div>
              ${rows}
            </div>
          </details>
        `,
        title,
      };
    }

    function autoTaskLastOrderText(task, config) {
      const order = task.last_plan?.order || null;
      if (!order) return task.last_status || "--";
      const side = String(order.side || config.side || "").toUpperCase();
      const amount = formatSymbolQuantity(order.amount, config.symbol, "base");
      const price = order.price == null ? "--" : fmt.format(order.price);
      return `${side} ${amount} @ ${price}`;
    }

    function autoTaskDetailTitle(task) {
      const config = task.config || {};
      const execution = task.last_execution || {};
      const orderIds = execution.placed_order_ids || [];
      const lastOrderId = orderIds.length ? orderIds[orderIds.length - 1] : "";
      const lastPlan = task.last_plan || {};
      const parts = [
        `last status: ${task.last_status || task.status || "--"}`,
        `placed: ${task.placed_count || 0}`,
        `canceled: ${task.canceled_count || 0}`,
        `start: ${task.start_price_triggered ? "triggered" : "waiting"}`,
        autoStartGateText(config),
        autoStopGateText(config),
      ];
      if (lastPlan.trigger_price != null) parts.push(`trigger: ${fmt.format(lastPlan.trigger_price)}`);
      if (lastOrderId) parts.push(`last order: ${lastOrderId}`);
      if (task.last_error) parts.push(`error: ${task.last_error}`);
      return parts.join(" · ");
    }

    function renderSlowExecutionTasks(taskPayload, defaultConfig) {
      const body = document.getElementById("slow-tasks");
      body.innerHTML = "";
      const tasks = taskPayload?.tasks || [];
      renderSlowConfigStatus(taskPayload, defaultConfig);
      if (tasks.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="12">No Auto Buy/Sell tasks.</td>`;
        body.appendChild(tr);
        return;
      }

      for (const task of tasks) {
        const config = task.config || {};
        const status = task.status || "--";
        const terminal = AUTO_TERMINAL_STATUSES.has(status);
        const statusClass = status === "complete" ? "risk-ok" : status === "paused" || status === "stopped" ? "risk-off" : status === "blocked_by_risk" || status === "error" ? "risk-blocked" : "ok";
        const progressLabel = task.progress_label || (config.side === "buy" ? "Bought" : "Sold");
        const progressMode = task.progress_mode || ((config.total_quote || 0) > 0 ? "quote" : "base");
        const unlimited = progressMode === "unlimited" || config.unlimited_total;
        const filledValue = progressMode === "quote" ? task.filled_quote : task.filled_base;
        const totalValue = progressMode === "quote" ? config.total_quote : config.total_base;
        const remainingValue = progressMode === "quote" ? task.remaining_quote : task.remaining_base;
        const filledText = unlimited
          ? `${progressLabel} ${formatSymbolQuantity(task.filled_base, config.symbol, "base")} · ${formatSymbolQuantity(task.filled_quote, config.symbol, "quote")} / Unlimited`
          : `${progressLabel} ${formatSymbolQuantity(filledValue, config.symbol, progressMode)} / ${formatSymbolQuantity(totalValue, config.symbol, progressMode)}`;
        const remainingText = unlimited ? "Unlimited" : formatSymbolQuantity(remainingValue, config.symbol, progressMode);
        const progressPct = unlimited ? "--" : `${(task.progress_pct || 0).toFixed(2)}%`;
        const configCell = autoTaskConfigCell(task, defaultConfig);
        const detailTitle = autoTaskDetailTitle(task);
        const lastText = autoTaskLastOrderText(task, config);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(task.id || "")}">${escapeHtml(shortId(task.id))}</td>
          <td class="${statusClass}" title="${escapeHtml(detailTitle)}">${escapeHtml(status)}</td>
          <td class="${configCell.className}" title="${escapeHtml(configCell.title)}">${configCell.html}</td>
          <td>${escapeHtml(config.exchange || "--")}</td>
          <td class="${config.side === "buy" ? "side-buy" : "side-sell"}">${escapeHtml(String(config.side || "--").toUpperCase())}</td>
          <td class="num">${filledText}</td>
          <td class="num">${remainingText}</td>
          <td class="num">${progressPct}</td>
          <td class="num" title="${escapeHtml(detailTitle)}">${task.open_order_count || 0}</td>
          <td title="${escapeHtml(lastText)}"><div>${formatAge(task.last_cycle_at)}</div><div class="subtle">${escapeHtml(lastText)}</div></td>
          <td>${formatDue(task.next_run_at)}</td>
          <td class="strategy-action"></td>
        `;
        const action = tr.querySelector(".strategy-action");
        if (!terminal) {
          const button = document.createElement("button");
          button.className = status === "paused" ? "control-button" : "danger-button";
          button.type = "button";
          button.textContent = status === "paused" ? "Resume" : "Pause";
          button.addEventListener("click", () => controlAutoBuySellTask(
            task.id,
            status === "paused" ? "resume" : "pause",
            button
          ));
          action.appendChild(button);
          const stopButton = document.createElement("button");
          stopButton.className = "danger-button";
          stopButton.type = "button";
          stopButton.textContent = "Stop";
          stopButton.addEventListener("click", () => controlAutoBuySellTask(
            task.id,
            "stop",
            stopButton
          ));
          action.appendChild(stopButton);
        } else {
          action.textContent = "--";
        }
        body.appendChild(tr);
      }
    }

    function pnlClass(value) {
      if (value == null || Math.abs(value) < 1e-12) return "pnl-flat";
      return value > 0 ? "pnl-positive" : "pnl-negative";
    }

    function setPnl(id, value) {
      const el = document.getElementById(id);
      el.textContent = value == null ? "--" : `$${money.format(value)}`;
      el.className = `value ${pnlClass(value)}`;
    }

    function formatPnlSourceDetail(portfolio) {
      const labels = {
        market_maker: "MM",
        arbitrage: "Arb",
        auto_buy_sell: "Auto",
        manual: "Manual",
        unattributed: "Unattributed",
        price_move: "Price",
      };
      return Object.entries(portfolio?.sources || {})
        .filter(([, value]) => value != null && Math.abs(value) >= 1e-12)
        .map(([key, value]) => `${labels[key] || key}: ${formatPnlValue(value)}`)
        .join(" | ");
    }

    function formatCashDetail(portfolio) {
      const balances = portfolio?.cash_balances || {};
      const preferredOrder = { USDC: 0, USDT: 1, USD: 2, KRW: 3 };
      const pieces = Object.entries(balances)
        .sort(([left], [right]) => {
          const leftRank = preferredOrder[left] ?? 99;
          const rightRank = preferredOrder[right] ?? 99;
          return leftRank === rightRank ? left.localeCompare(right) : leftRank - rightRank;
        })
        .map(([currency, amount]) => `${currency} ${compact.format(amount || 0)}`);
      const missing = portfolio?.cash_missing_rates || [];
      if (missing.length > 0) {
        pieces.push(`missing ${missing.join("/")}`);
      }
      return pieces.length === 0 ? "--" : pieces.join(" · ");
    }

    function formatPositionPrice(position, portfolio) {
      const price = position?.mark_price ?? portfolio?.mark_price;
      return price == null ? "price --" : `price $${fmt.format(price)}`;
    }

    function formatPositionValue(position, portfolio) {
      const value = position?.position_value ?? portfolio?.position_value;
      return value == null ? "value --" : `value $${money.format(value)}`;
    }

    function formatPositionDetail(portfolio) {
      const positions = portfolio?.positions || [];
      if (positions.length === 0) {
        return portfolio?.asset
          ? `${portfolio.asset} ${formatPositionPrice(null, portfolio)} · ${formatPositionValue(null, portfolio)}`
          : "--";
      }
      return positions
        .map((position) => `${position.asset} ${compact.format(position.position_base || 0)} · ${formatPositionPrice(position, portfolio)} · ${formatPositionValue(position, portfolio)}`)
        .join(" · ");
    }

    function formatMarkDetail(portfolio) {
      return (portfolio?.positions || [])
        .map((position) => {
          const mark = position.mark_price == null ? "--" : `$${fmt.format(position.mark_price)}`;
          return `${position.asset} ${mark}`;
        })
        .join(" · ");
    }

    function renderPortfolio(portfolio) {
      if (!portfolio || portfolio.status === "disabled") {
        text("portfolio-position", "--");
        text("portfolio-position-detail", "--");
        text("portfolio-cash", "--");
        text("portfolio-cash-detail", "--");
        text("portfolio-mark", "--");
        text("portfolio-value", "--");
        setPnl("portfolio-total-pnl", null);
        setPnl("portfolio-mm-pnl", null);
        setPnl("portfolio-arb-pnl", null);
        setPnl("portfolio-auto-pnl", null);
        setPnl("portfolio-other-pnl", null);
        setPnl("portfolio-price-pnl", null);
        document.getElementById("portfolio-total-pnl").title = "";
        return;
      }

      const positions = portfolio.positions || [];
      const positionDetail = formatPositionDetail(portfolio);
      if (positions.length > 1) {
        text("portfolio-position", `${positions.length} assets`);
        text("portfolio-position-detail", positionDetail);
      } else {
        text("portfolio-position", `${compact.format(portfolio.position_base || 0)} ${portfolio.asset || ""}`);
        text("portfolio-position-detail", positionDetail);
      }
      document.getElementById("portfolio-position-detail").title = positionDetail;
      const cashValue = portfolio.cash_value == null ? null : portfolio.cash_value;
      text("portfolio-cash", cashValue == null ? "--" : `$${money.format(cashValue)}`);
      const cashDetail = formatCashDetail(portfolio);
      text("portfolio-cash-detail", cashDetail);
      document.getElementById("portfolio-cash-detail").title = cashDetail;
      const markDetail = formatMarkDetail(portfolio);
      text(
        "portfolio-mark",
        positions.length > 1
          ? "Mixed"
          : portfolio.mark_price == null ? "--" : `$${fmt.format(portfolio.mark_price)}`
      );
      document.getElementById("portfolio-mark").title = markDetail || "";
      text("portfolio-value", portfolio.position_value == null ? "--" : `$${money.format(portfolio.position_value)}`);
      setPnl("portfolio-total-pnl", portfolio.total_pnl);
      setPnl("portfolio-mm-pnl", portfolio.sources?.market_maker);
      setPnl("portfolio-arb-pnl", portfolio.sources?.arbitrage);
      setPnl("portfolio-auto-pnl", portfolio.sources?.auto_buy_sell);
      setPnl(
        "portfolio-other-pnl",
        (portfolio.sources?.manual || 0) + (portfolio.sources?.unattributed || 0)
      );
      setPnl("portfolio-price-pnl", portfolio.sources?.price_move);
      document.getElementById("portfolio-total-pnl").title = formatPnlSourceDetail(portfolio);
    }

    function shortAddress(address) {
      if (!address || address.length < 12) return address || "--";
      return `${address.slice(0, 6)}...${address.slice(-6)}`;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    function displayStrategy(value) {
      if (value === "slow_execution") return "Auto Buy/Sell";
      if (value === "market_maker") return "Market Maker";
      if (value === "spot_grid") return "Spot Grid";
      if (value === "dca") return "DCA Bot";
      if (value === "execution_algo") return "TWAP/VWAP/POV";
      if (value === "backtest") return "Backtest/Paper";
      if (value === "spot_spread") return "Spot Arbitrage";
      if (value === "cash_and_carry") return "Cash & Carry";
      if (value === "funding_arbitrage") return "Funding Arbitrage";
      if (value === "options_arbitrage") return "Options Arbitrage";
      if (value === "signal_bot") return "Signal Bot";
      return value || "--";
    }

    function formatTokenDelta(value) {
      if (value == null) return "--";
      return `${value >= 0 ? "+" : ""}${compact.format(value)}`;
    }

    function deltaClass(value) {
      return value == null ? "" : value >= 0 ? "ok" : "missing";
    }

    function displayHolderEventType(value) {
      if (value === "entered_top_holders") return "Entered Top";
      if (value === "balance_change") return "Balance";
      return value || "--";
    }

    function renderHolders(onchain) {
      const body = document.getElementById("holders");
      body.innerHTML = "";
      if (!onchain || !onchain.holders || onchain.holders.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="9">No holder data yet.</td>`;
        body.appendChild(tr);
      } else {
        for (const holder of onchain.holders) {
          const cumulativeDelta = holder.cumulative_delta_amount ?? holder.delta_amount;
          const lastDelta = holder.last_delta_amount;
          const label = holder.label || "Unknown";
          const labelClass = holder.is_labeled ? "known" : "unknown";
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${holder.rank}</td>
            <td><span class="holder-label ${labelClass}" title="${escapeHtml(label)}">${escapeHtml(label)}</span></td>
            <td title="${holder.owner}">${shortAddress(holder.owner)}</td>
            <td class="num">${compact.format(holder.amount)}</td>
            <td class="num">${holder.share_pct == null ? "--" : holder.share_pct.toFixed(4) + "%"}</td>
            <td class="num ${deltaClass(cumulativeDelta)}" title="Baseline ${holder.baseline_amount == null ? "--" : compact.format(holder.baseline_amount)}">${formatTokenDelta(cumulativeDelta)}</td>
            <td class="num ${deltaClass(lastDelta)}" title="${holder.last_change_at ? formatAge(holder.last_change_at) : "No change"}">${formatTokenDelta(lastDelta)}</td>
            <td class="num">${holder.change_count || 0}</td>
            <td class="num">${holder.token_account_count}</td>
          `;
          body.appendChild(tr);
        }
      }

      const history = onchain?.history || {};
      const baselineText = history.baseline_at ? `since ${formatAge(history.baseline_at)}` : "baseline pending";
      text(
        "onchain-history-meta",
        `${history.event_count || 0} total changes · ${baselineText} · ${history.path || ""}`
      );

      const changesBody = document.getElementById("holder-changes");
      changesBody.innerHTML = "";
      const events = history.recent_events || [];
      if (events.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="8">No wallet changes recorded since the baseline.</td>`;
        changesBody.appendChild(tr);
        return;
      }

      for (const event of events) {
        const label = event.label || "Unknown";
        const labelClass = event.is_labeled ? "known" : "unknown";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${formatAge(event.observed_at)}</td>
          <td>${escapeHtml(displayHolderEventType(event.event_type))}</td>
          <td><span class="holder-label ${labelClass}" title="${escapeHtml(label)}">${escapeHtml(label)}</span></td>
          <td title="${event.owner}">${shortAddress(event.owner)}</td>
          <td class="num">${event.previous_rank && event.previous_rank !== event.rank ? `${event.previous_rank}→${event.rank || "--"}` : event.rank || "--"}</td>
          <td class="num ${deltaClass(event.delta_amount)}">${formatTokenDelta(event.delta_amount)}</td>
          <td class="num">${event.amount == null ? "--" : compact.format(event.amount)}</td>
          <td class="num ${deltaClass(event.cumulative_delta_amount)}">${formatTokenDelta(event.cumulative_delta_amount)}</td>
        `;
        changesBody.appendChild(tr);
      }
    }

    let programToggleBusy = false;
    let riskFormDirty = false;
    let riskFormBusy = false;
    let mmFormDirty = false;
    let mmFormBusy = false;
    let slowFormDirty = false;
    let slowFormBusy = false;
    let gridFormDirty = false;
    let gridFormBusy = false;
    let dcaFormDirty = false;
    let dcaFormBusy = false;
    let execFormDirty = false;
    let execFormBusy = false;
    let backtestFormDirty = false;
    let backtestFormBusy = false;
    let strategyCenterFormDirty = false;
    let strategyCenterFormBusy = false;
    let apiAccountFormDirty = false;
    let apiAccountFormBusy = false;
    let fundingArbFormDirty = false;
    let fundingArbFormBusy = false;
    let signalBotFormDirty = false;
    let signalBotFormBusy = false;

    async function setProgramRunning(running) {
      if (programToggleBusy) return;
      programToggleBusy = true;
      const toggle = document.getElementById("program-toggle");
      toggle.disabled = true;
      try {
        const res = await fetch("/api/control", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ running }),
        });
        if (!res.ok) throw new Error("control failed");
        await refresh();
      } catch (error) {
        toggle.checked = !running;
      } finally {
        toggle.disabled = false;
        programToggleBusy = false;
      }
    }

    function numericValue(id) {
      const value = document.getElementById(id).value;
      if (value === "") return 0;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }

    function setNumericField(id, value) {
      document.getElementById(id).value = value == null ? "" : String(value);
    }

    function renderRiskToggleOptions(containerId, inputName, items, enabledMap, emptyText) {
      const body = document.getElementById(containerId);
      const list = Array.isArray(items) ? items : [];
      const signature = JSON.stringify({
        items: list.map((item) => [item.key || item.id, item.label, item.title]),
        enabledMap,
      });
      if (body.dataset.signature === signature) return;
      body.dataset.signature = signature;
      body.innerHTML = "";
      if (list.length === 0) {
        const empty = document.createElement("span");
        empty.className = "subtle";
        empty.textContent = emptyText;
        body.appendChild(empty);
        return;
      }

      for (const item of list) {
        const key = item.key || item.id;
        const label = document.createElement("label");
        label.className = "account-option";
        label.title = item.title || key;
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.name = inputName;
        checkbox.value = key;
        checkbox.checked = enabledMap?.[key] !== false;
        const textNode = document.createElement("span");
        textNode.textContent = item.label || key;
        label.appendChild(checkbox);
        label.appendChild(textNode);
        body.appendChild(label);
      }
    }

    function checkboxMap(inputName) {
      const values = {};
      document.querySelectorAll(`input[name="${inputName}"]`).forEach((input) => {
        values[input.value] = input.checked;
      });
      return values;
    }

    function renderRiskControls(ops, tradingConsole) {
      if (riskFormDirty || riskFormBusy) return;
      const risk = ops?.risk || {};
      document.getElementById("risk-allow-live").checked = Boolean(risk.allow_live_trading);
      setNumericField("risk-max-order", risk.max_order_quote || 0);
      setNumericField("risk-max-cycle", risk.max_cycle_quote || 0);
      setNumericField("risk-max-exposure", risk.max_exposure_quote || 0);
      setNumericField("risk-max-daily-loss", risk.max_daily_loss_quote || 0);
      setNumericField("risk-max-orders-cycle", risk.max_orders_per_cycle || 0);
      setNumericField("risk-max-open-orders", risk.max_open_orders || 0);
      setNumericField("risk-max-cancels", risk.max_cancels_per_cycle || 0);
      setNumericField("risk-cancel-cooldown", risk.min_seconds_between_cancels || 0);
      setNumericField("risk-min-book-depth", risk.min_order_book_depth_quote || 0);
      setNumericField("risk-max-slippage", risk.max_slippage_bps || 0);
      setNumericField("risk-max-book-age", risk.max_order_book_age_seconds || 0);
      setNumericField("risk-max-book-gap", risk.max_order_book_gap_bps || 0);
      setNumericField("risk-max-price-jump", risk.max_price_jump_bps || 0);
      setNumericField("risk-max-derivative-leverage", risk.max_derivative_leverage || 0);
      setNumericField("risk-min-liquidation-buffer", risk.min_liquidation_buffer_pct || 0);
      setNumericField("risk-max-margin-usage", risk.max_margin_usage_pct || 0);

      const accounts = (tradingConsole?.accounts || []).map((account) => ({
        key: account.key,
        label: account.label || account.key,
        title: `${account.id || account.key} · ${account.market_type || "spot"}`,
      }));
      const strategies = (tradingConsole?.strategies || []).map((strategy) => ({
        key: strategy.id,
        label: strategy.label || displayStrategy(strategy.id),
        title: strategy.symbol ? `${strategy.exchange || "all"} · ${strategy.symbol}` : strategy.id,
      }));
      renderRiskToggleOptions(
        "risk-accounts",
        "risk-account",
        accounts,
        risk.account_enabled || {},
        "No accounts"
      );
      renderRiskToggleOptions(
        "risk-strategies",
        "risk-strategy",
        strategies,
        risk.strategy_enabled || {},
        "No strategies"
      );

      const liveState = risk.allow_live_trading ? "live allowed" : "live blocked";
      text(
        "risk-control-meta",
        `${liveState} · max/order $${money.format(risk.max_order_quote || 0)} · cycle $${money.format(risk.max_cycle_quote || 0)} · orders ${risk.max_orders_per_cycle || 0}/cycle · open ${risk.max_open_orders || 0}`
      );
    }

    async function applyRiskConfig(event) {
      event.preventDefault();
      if (riskFormBusy) return;
      riskFormBusy = true;
      const button = document.getElementById("risk-apply");
      button.disabled = true;
      const payload = {
        allow_live_trading: document.getElementById("risk-allow-live").checked,
        account_enabled: checkboxMap("risk-account"),
        strategy_enabled: checkboxMap("risk-strategy"),
        max_order_quote: numericValue("risk-max-order"),
        max_cycle_quote: numericValue("risk-max-cycle"),
        max_exposure_quote: numericValue("risk-max-exposure"),
        max_daily_loss_quote: numericValue("risk-max-daily-loss"),
        max_orders_per_cycle: numericValue("risk-max-orders-cycle"),
        max_open_orders: numericValue("risk-max-open-orders"),
        max_cancels_per_cycle: numericValue("risk-max-cancels"),
        min_seconds_between_cancels: numericValue("risk-cancel-cooldown"),
        min_order_book_depth_quote: numericValue("risk-min-book-depth"),
        max_slippage_bps: numericValue("risk-max-slippage"),
        max_order_book_age_seconds: numericValue("risk-max-book-age"),
        max_order_book_gap_bps: numericValue("risk-max-book-gap"),
        max_price_jump_bps: numericValue("risk-max-price-jump"),
        max_derivative_leverage: numericValue("risk-max-derivative-leverage"),
        min_liquidation_buffer_pct: numericValue("risk-min-liquidation-buffer"),
        max_margin_usage_pct: numericValue("risk-max-margin-usage"),
      };
      try {
        const res = await fetch("/api/risk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error("risk update failed");
        riskFormDirty = false;
        await refresh();
      } finally {
        button.disabled = false;
        riskFormBusy = false;
      }
    }

    function accountSymbols(account) {
      const symbols = Array.isArray(account?.symbols) ? account.symbols : [];
      const rows = [...symbols];
      if (account?.symbol && !rows.includes(account.symbol)) rows.unshift(account.symbol);
      return rows.filter(Boolean);
    }

    function accountSelectorValue(inputName) {
      return document.querySelector(`[data-account-selector="${inputName}"]`)?.value || "";
    }

    function symbolSelectorValue(inputName) {
      return document.querySelector(`[data-symbol-selector="${inputName}"]`)?.value || "";
    }

    function accountForKey(accounts, key) {
      const list = Array.isArray(accounts) ? accounts : [];
      return list.find((account) => account.key === key) || null;
    }

    function renderAccountSymbolSelectors(containerId, inputName, accounts, selectedExchange, selectedSymbol, onDirty) {
      const body = document.getElementById(containerId);
      const list = Array.isArray(accounts) ? accounts : [];
      const signature = JSON.stringify({
        accounts: list.map((account) => [account.key, account.label, account.id, account.market_type, account.symbol, account.symbols]),
        selectedExchange,
        selectedSymbol,
      });
      if (body.dataset.signature === signature) return;
      body.dataset.signature = signature;
      body.innerHTML = "";
      if (list.length === 0) {
        const empty = document.createElement("span");
        empty.className = "subtle";
        empty.textContent = "No accounts";
        body.appendChild(empty);
        return;
      }

      const wrapper = document.createElement("div");
      wrapper.className = "account-selector";
      const accountSelect = document.createElement("select");
      accountSelect.dataset.accountSelector = inputName;
      accountSelect.className = "account-select";
      accountSelect.title = "Exchange account";
      const accountPlaceholder = document.createElement("option");
      accountPlaceholder.value = "";
      accountPlaceholder.textContent = "Select account";
      accountSelect.appendChild(accountPlaceholder);
      for (const account of list) {
        const option = document.createElement("option");
        option.value = account.key;
        option.textContent = `${account.label || account.key} (${account.market_type || "spot"})`;
        option.title = `${account.id || account.key} · ${(accountSymbols(account)).join(", ") || "no symbols"}`;
        accountSelect.appendChild(option);
      }
      if (selectedExchange && list.some((account) => account.key === selectedExchange)) {
        accountSelect.value = selectedExchange;
      }

      const symbolSelect = document.createElement("select");
      symbolSelect.dataset.symbolSelector = inputName;
      symbolSelect.className = "account-select";
      symbolSelect.title = "Trading pair";

      const fillSymbols = (preferredSymbol = "") => {
        const account = accountForKey(list, accountSelect.value);
        const symbols = accountSymbols(account);
        symbolSelect.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = symbols.length ? "Select symbol" : "No symbols";
        symbolSelect.appendChild(placeholder);
        for (const symbol of symbols) {
          const option = document.createElement("option");
          option.value = symbol;
          option.textContent = symbol;
          symbolSelect.appendChild(option);
        }
        if (preferredSymbol && symbols.includes(preferredSymbol)) {
          symbolSelect.value = preferredSymbol;
        } else if (symbols.length) {
          symbolSelect.value = symbols[0];
        }
      };

      accountSelect.addEventListener("change", () => {
        fillSymbols("");
        onDirty();
      });
      symbolSelect.addEventListener("change", onDirty);
      fillSymbols(selectedSymbol || "");
      wrapper.appendChild(accountSelect);
      wrapper.appendChild(symbolSelect);
      body.appendChild(wrapper);
    }

    function selectedMarketMakerAccount() {
      return accountSelectorValue("mm-account");
    }

    function selectedMarketMakerSymbol() {
      return symbolSelectorValue("mm-account");
    }

    function renderMarketMakerAccounts(accounts, selectedExchange, selectedSymbol) {
      renderAccountSymbolSelectors("mm-accounts", "mm-account", accounts, selectedExchange, selectedSymbol, () => {
        mmFormDirty = true;
      });
    }

    function renderMarketMakerConfig(config, accounts) {
      if (!config || mmFormDirty || mmFormBusy) return;
      document.getElementById("mm-enabled").checked = Boolean(config.enabled);
      document.getElementById("mm-live-enabled").checked = Boolean(config.live_enabled);
      renderMarketMakerAccounts(config.accounts || accounts, config.exchange || "", config.symbol || "");
      setNumericField("mm-levels", config.levels || 1);
      setNumericField("mm-band", config.price_band_pct || 0);
      setNumericField("mm-quote", config.quote_per_level || 0);
      document.getElementById("mm-depth-shape").value = config.depth_shape || "linear";
      setNumericField("mm-min-quote", config.min_order_quote || 0);
      setNumericField("mm-min-distance", config.min_distance_bps || 0);
      setNumericField("mm-reprice", config.reprice_threshold_bps || 0);
      setNumericField("mm-poll", config.poll_seconds || 1);
      document.getElementById("mm-inventory-enabled").checked = Boolean(config.inventory_control_enabled);
      setNumericField("mm-inventory-target", config.inventory_target_base || 0);
      setNumericField("mm-inventory-band", config.inventory_band_base || 0);
      setNumericField("mm-inventory-max", config.inventory_max_deviation_base || 0);
      document.getElementById("mm-post-only").checked = Boolean(config.post_only);
    }

    function marketMakerPayloadFromForm() {
      return {
        enabled: document.getElementById("mm-enabled").checked,
        live_enabled: document.getElementById("mm-live-enabled").checked,
        exchange: selectedMarketMakerAccount(),
        symbol: selectedMarketMakerSymbol(),
        levels: numericValue("mm-levels"),
        price_band_pct: numericValue("mm-band"),
        quote_per_level: numericValue("mm-quote"),
        depth_shape: document.getElementById("mm-depth-shape").value,
        min_order_quote: numericValue("mm-min-quote"),
        min_distance_bps: numericValue("mm-min-distance"),
        reprice_threshold_bps: numericValue("mm-reprice"),
        poll_seconds: numericValue("mm-poll"),
        inventory_control_enabled: document.getElementById("mm-inventory-enabled").checked,
        inventory_target_base: numericValue("mm-inventory-target"),
        inventory_band_base: numericValue("mm-inventory-band"),
        inventory_max_deviation_base: numericValue("mm-inventory-max"),
        post_only: document.getElementById("mm-post-only").checked,
      };
    }

    async function applyMarketMakerConfig(event) {
      event.preventDefault();
      if (mmFormBusy) return;
      mmFormBusy = true;
      const button = document.getElementById("mm-apply");
      button.disabled = true;
      try {
        const res = await fetch("/api/market-maker", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(marketMakerPayloadFromForm()),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "market maker update failed");
        mmFormDirty = false;
        await refresh();
      } catch (error) {
        text("mm-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        mmFormBusy = false;
      }
    }

    function selectedSlowAccount() {
      return accountSelectorValue("slow-account");
    }

    function selectedSlowSymbol() {
      return symbolSelectorValue("slow-account");
    }

    function renderSlowExecutionAccounts(accounts, selectedExchange, selectedSymbol) {
      renderAccountSymbolSelectors("slow-accounts", "slow-account", accounts, selectedExchange, selectedSymbol, () => {
        slowFormDirty = true;
      });
    }

    function updateSlowGateLabels() {
      const side = document.getElementById("slow-side")?.value || "sell";
      const startLabel = document.getElementById("slow-start-price-label");
      const stopLabel = document.getElementById("slow-stop-price-label");
      const startHelp = document.getElementById("slow-start-price-help");
      const stopHelp = document.getElementById("slow-stop-price-help");
      if (side === "buy") {
        if (startLabel) startLabel.textContent = "Start Gate (Ask <=)";
        if (stopLabel) stopLabel.textContent = "Stop Gate (Ask <=)";
        if (startHelp) startHelp.textContent = "Buy starts when the best ask reaches this price or lower.";
        if (stopHelp) stopHelp.textContent = "Buy stops when the best ask reaches this price or lower. This is checked before Start.";
        return;
      }
      if (startLabel) startLabel.textContent = "Start Gate (Bid >=)";
      if (stopLabel) stopLabel.textContent = "Stop Gate (Bid <=)";
      if (startHelp) startHelp.textContent = "Sell starts when the best bid reaches this price or higher.";
      if (stopHelp) stopHelp.textContent = "Sell stops when the best bid reaches this price or lower.";
    }

    function renderSlowExecutionConfig(config, accounts) {
      if (!config || slowFormDirty || slowFormBusy) return;
      document.getElementById("slow-enabled").checked = Boolean(config.enabled);
      renderSlowExecutionAccounts(config.accounts || accounts, config.exchange || "", config.symbol || "");
      document.getElementById("slow-side").value = config.side || "sell";
      updateSlowGateLabels();
      document.getElementById("slow-price-mode").value = config.price_mode || "taker";
      setNumericField("slow-offset-bps", config.price_offset_bps || 0);
      document.getElementById("slow-unlimited").checked = Boolean(config.unlimited_total);
      setNumericField("slow-total-base", config.total_base || 0);
      setNumericField("slow-total-quote", config.total_quote || 0);
      document.getElementById("slow-slice-mode").value = config.slice_mode || "configured";
      setNumericField("slow-slice-min", config.slice_base_min || config.slice_base || 0);
      setNumericField("slow-slice-max", config.slice_base_max || config.slice_base || 0);
      document.getElementById("slow-randomize").checked = Boolean(config.randomize_slice);
      setNumericField("slow-interval", config.interval_seconds || 60);
      setNumericField("slow-ttl", config.order_ttl_seconds || 0);
      setNumericField("slow-start-price", config.start_price || 0);
      setNumericField("slow-stop-price", config.stop_price || 0);
    }

    function slowExecutionPayloadFromForm() {
      return {
        enabled: document.getElementById("slow-enabled").checked,
        exchange: selectedSlowAccount(),
        symbol: selectedSlowSymbol(),
        side: document.getElementById("slow-side").value,
        price_mode: document.getElementById("slow-price-mode").value,
        price_offset_bps: numericValue("slow-offset-bps"),
        unlimited_total: document.getElementById("slow-unlimited").checked,
        total_base: numericValue("slow-total-base"),
        total_quote: numericValue("slow-total-quote"),
        slice_mode: document.getElementById("slow-slice-mode").value,
        slice_base_min: numericValue("slow-slice-min"),
        slice_base_max: numericValue("slow-slice-max"),
        randomize_slice: document.getElementById("slow-randomize").checked,
        interval_seconds: numericValue("slow-interval"),
        order_ttl_seconds: numericValue("slow-ttl"),
        start_price: numericValue("slow-start-price"),
        stop_price: numericValue("slow-stop-price"),
      };
    }

    async function applySlowExecutionConfig(event) {
      event.preventDefault();
      if (slowFormBusy) return;
      slowFormBusy = true;
      const button = document.getElementById("slow-apply");
      button.disabled = true;
      const payload = slowExecutionPayloadFromForm();
      try {
        const res = await fetch("/api/auto-buy-sell", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "auto buy/sell update failed");
        slowFormDirty = false;
        await refresh();
      } catch (error) {
        text("slow-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        slowFormBusy = false;
      }
    }

    function selectedStrategyAccount(inputName) {
      return accountSelectorValue(inputName);
    }

    function selectedStrategySymbol(inputName) {
      return symbolSelectorValue(inputName);
    }

    function renderStrategyAccounts(containerId, inputName, accounts, selectedExchange, selectedSymbol, onDirty) {
      renderAccountSymbolSelectors(containerId, inputName, accounts, selectedExchange, selectedSymbol, onDirty);
    }

    function renderSpotGridConfig(config, accounts) {
      if (!config || gridFormDirty || gridFormBusy) return;
      document.getElementById("grid-enabled").checked = Boolean(config.enabled);
      document.getElementById("grid-live-enabled").checked = Boolean(config.live_enabled);
      renderStrategyAccounts("grid-accounts", "grid-account", accounts, config.exchange || "", config.symbol || "", () => {
        gridFormDirty = true;
      });
      setNumericField("grid-lower", config.lower_price || 0);
      setNumericField("grid-upper", config.upper_price || 0);
      setNumericField("grid-count", config.grid_count || 1);
      document.getElementById("grid-spacing").value = config.spacing || "arithmetic";
      setNumericField("grid-quote", config.quote_per_grid || 0);
      setNumericField("grid-take-profit", config.take_profit_price || 0);
      setNumericField("grid-stop-loss", config.stop_loss_price || 0);
      document.getElementById("grid-auto-rebuild").checked = Boolean(config.auto_rebuild);
      setNumericField("grid-max-position", config.max_position_base || 0);
      setNumericField("grid-max-open-orders", config.max_open_orders || 1);
      setNumericField("grid-min-step", config.min_grid_step_bps || 0);
      setNumericField("grid-cancel-retries", config.cancel_retry_attempts || 0);
      document.getElementById("grid-post-only").checked = Boolean(config.post_only);
    }

    function spotGridPayloadFromForm() {
      return {
        enabled: document.getElementById("grid-enabled").checked,
        live_enabled: document.getElementById("grid-live-enabled").checked,
        exchange: selectedStrategyAccount("grid-account"),
        symbol: selectedStrategySymbol("grid-account"),
        lower_price: numericValue("grid-lower"),
        upper_price: numericValue("grid-upper"),
        grid_count: numericValue("grid-count"),
        spacing: document.getElementById("grid-spacing").value,
        quote_per_grid: numericValue("grid-quote"),
        take_profit_price: numericValue("grid-take-profit"),
        stop_loss_price: numericValue("grid-stop-loss"),
        auto_rebuild: document.getElementById("grid-auto-rebuild").checked,
        max_position_base: numericValue("grid-max-position"),
        max_open_orders: numericValue("grid-max-open-orders"),
        min_grid_step_bps: numericValue("grid-min-step"),
        cancel_retry_attempts: numericValue("grid-cancel-retries"),
        post_only: document.getElementById("grid-post-only").checked,
      };
    }

    async function applySpotGridConfig(event) {
      event.preventDefault();
      if (gridFormBusy) return;
      gridFormBusy = true;
      const button = document.getElementById("grid-apply");
      button.disabled = true;
      try {
        const res = await fetch("/api/spot-grid", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(spotGridPayloadFromForm()),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "spot grid update failed");
        gridFormDirty = false;
        await refresh();
      } catch (error) {
        text("grid-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        gridFormBusy = false;
      }
    }

    function renderDcaConfig(config, accounts) {
      if (!config || dcaFormDirty || dcaFormBusy) return;
      document.getElementById("dca-enabled").checked = Boolean(config.enabled);
      document.getElementById("dca-live-enabled").checked = Boolean(config.live_enabled);
      renderStrategyAccounts("dca-accounts", "dca-account", accounts, config.exchange || "", config.symbol || "", () => {
        dcaFormDirty = true;
      });
      document.getElementById("dca-side").value = config.side || "buy";
      setNumericField("dca-trigger", config.trigger_price || 0);
      setNumericField("dca-interval", config.interval_seconds || 3600);
      setNumericField("dca-quote", config.quote_per_order || 0);
      setNumericField("dca-multiplier", config.size_multiplier || 1);
      setNumericField("dca-max-orders", config.max_orders || 1);
      setNumericField("dca-average-entry", config.average_entry_price || 0);
      setNumericField("dca-take-profit", config.take_profit_price || 0);
      setNumericField("dca-max-position", config.max_position_base || 0);
      setNumericField("dca-max-loss", config.max_loss_quote || 0);
      document.getElementById("dca-price-mode").value = config.price_mode || "taker";
      setNumericField("dca-offset-bps", config.price_offset_bps || 0);
    }

    function dcaPayloadFromForm() {
      return {
        enabled: document.getElementById("dca-enabled").checked,
        live_enabled: document.getElementById("dca-live-enabled").checked,
        exchange: selectedStrategyAccount("dca-account"),
        symbol: selectedStrategySymbol("dca-account"),
        side: document.getElementById("dca-side").value,
        trigger_price: numericValue("dca-trigger"),
        interval_seconds: numericValue("dca-interval"),
        quote_per_order: numericValue("dca-quote"),
        size_multiplier: numericValue("dca-multiplier"),
        max_orders: numericValue("dca-max-orders"),
        average_entry_price: numericValue("dca-average-entry"),
        take_profit_price: numericValue("dca-take-profit"),
        max_position_base: numericValue("dca-max-position"),
        max_loss_quote: numericValue("dca-max-loss"),
        price_mode: document.getElementById("dca-price-mode").value,
        price_offset_bps: numericValue("dca-offset-bps"),
      };
    }

    async function applyDcaConfig(event) {
      event.preventDefault();
      if (dcaFormBusy) return;
      dcaFormBusy = true;
      const button = document.getElementById("dca-apply");
      button.disabled = true;
      try {
        const res = await fetch("/api/dca", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(dcaPayloadFromForm()),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "dca update failed");
        dcaFormDirty = false;
        await refresh();
      } catch (error) {
        text("dca-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        dcaFormBusy = false;
      }
    }

    function renderExecutionAlgoConfig(config, accounts) {
      if (!config || execFormDirty || execFormBusy) return;
      document.getElementById("exec-enabled").checked = Boolean(config.enabled);
      document.getElementById("exec-live-enabled").checked = Boolean(config.live_enabled);
      renderStrategyAccounts("exec-accounts", "exec-account", accounts, config.exchange || "", config.symbol || "", () => {
        execFormDirty = true;
      });
      document.getElementById("exec-side").value = config.side || "buy";
      document.getElementById("exec-algo").value = config.algo || "twap";
      setNumericField("exec-total-quote", config.total_quote || 0);
      setNumericField("exec-total-base", config.total_base || 0);
      setNumericField("exec-duration", config.duration_seconds || 3600);
      setNumericField("exec-slices", config.slice_count || 1);
      setNumericField("exec-interval", config.interval_seconds || 300);
      setNumericField("exec-participation", config.participation_rate || 0);
      setNumericField("exec-min-slice", config.min_slice_quote || 0);
      setNumericField("exec-max-slice", config.max_slice_quote || 0);
      setNumericField("exec-start-price", config.start_price || 0);
      setNumericField("exec-stop-price", config.stop_price || 0);
      setNumericField("exec-max-slippage", config.max_slippage_bps || 0);
      document.getElementById("exec-price-mode").value = config.price_mode || "taker";
      setNumericField("exec-offset-bps", config.price_offset_bps || 0);
    }

    function executionAlgoPayloadFromForm() {
      return {
        enabled: document.getElementById("exec-enabled").checked,
        live_enabled: document.getElementById("exec-live-enabled").checked,
        exchange: selectedStrategyAccount("exec-account"),
        symbol: selectedStrategySymbol("exec-account"),
        side: document.getElementById("exec-side").value,
        algo: document.getElementById("exec-algo").value,
        total_quote: numericValue("exec-total-quote"),
        total_base: numericValue("exec-total-base"),
        duration_seconds: numericValue("exec-duration"),
        slice_count: numericValue("exec-slices"),
        interval_seconds: numericValue("exec-interval"),
        participation_rate: numericValue("exec-participation"),
        min_slice_quote: numericValue("exec-min-slice"),
        max_slice_quote: numericValue("exec-max-slice"),
        start_price: numericValue("exec-start-price"),
        stop_price: numericValue("exec-stop-price"),
        max_slippage_bps: numericValue("exec-max-slippage"),
        price_mode: document.getElementById("exec-price-mode").value,
        price_offset_bps: numericValue("exec-offset-bps"),
      };
    }

    async function applyExecutionAlgoConfig(event) {
      event.preventDefault();
      if (execFormBusy) return;
      execFormBusy = true;
      const button = document.getElementById("exec-apply");
      button.disabled = true;
      try {
        const res = await fetch("/api/execution-algo", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(executionAlgoPayloadFromForm()),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "execution algo update failed");
        execFormDirty = false;
        await refresh();
      } catch (error) {
        text("exec-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        execFormBusy = false;
      }
    }

    function renderBacktestConfig(config, accounts) {
      if (!config || backtestFormDirty || backtestFormBusy) return;
      document.getElementById("backtest-enabled").checked = Boolean(config.enabled);
      renderStrategyAccounts("backtest-accounts", "backtest-account", accounts, config.exchange || "", config.symbol || "", () => {
        backtestFormDirty = true;
      });
      document.getElementById("backtest-strategy").value = config.strategy || "spot_grid";
      setNumericField("backtest-cash", config.initial_cash || 0);
      setNumericField("backtest-base", config.initial_base || 0);
      setNumericField("backtest-fee", config.fee_bps || 0);
      setNumericField("backtest-slippage", config.slippage_bps || 0);
      setNumericField("backtest-price-start", config.price_start || 0);
      setNumericField("backtest-price-end", config.price_end || 0);
      setNumericField("backtest-steps", config.step_count || 2);
      setNumericField("backtest-volatility", config.volatility_bps || 0);
      setNumericField("backtest-trend", config.trend_bps || 0);
      setNumericField("backtest-max-points", config.max_recent_points || 80);
    }

    function backtestPayloadFromForm() {
      return {
        enabled: document.getElementById("backtest-enabled").checked,
        exchange: selectedStrategyAccount("backtest-account"),
        symbol: selectedStrategySymbol("backtest-account"),
        strategy: document.getElementById("backtest-strategy").value,
        initial_cash: numericValue("backtest-cash"),
        initial_base: numericValue("backtest-base"),
        fee_bps: numericValue("backtest-fee"),
        slippage_bps: numericValue("backtest-slippage"),
        price_start: numericValue("backtest-price-start"),
        price_end: numericValue("backtest-price-end"),
        step_count: numericValue("backtest-steps"),
        volatility_bps: numericValue("backtest-volatility"),
        trend_bps: numericValue("backtest-trend"),
        max_recent_points: numericValue("backtest-max-points"),
      };
    }

    async function applyBacktestConfig(event) {
      event.preventDefault();
      if (backtestFormBusy) return;
      backtestFormBusy = true;
      const button = document.getElementById("backtest-apply");
      button.disabled = true;
      try {
        const res = await fetch("/api/backtest", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(backtestPayloadFromForm()),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "backtest update failed");
        backtestFormDirty = false;
        await refresh();
      } catch (error) {
        text("backtest-meta", `update failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        backtestFormBusy = false;
      }
    }

    async function createAutoBuySellTask() {
      if (slowFormBusy) return;
      slowFormBusy = true;
      const button = document.getElementById("slow-create-task");
      button.disabled = true;
      try {
        const res = await fetch("/api/auto-buy-sell/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(slowExecutionPayloadFromForm()),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "create task failed");
        slowFormDirty = false;
        await refresh();
      } catch (error) {
        text("slow-meta", `create failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        slowFormBusy = false;
      }
    }

    async function controlAutoBuySellTask(taskId, action, button) {
      button.disabled = true;
      try {
        const res = await fetch(`/api/auto-buy-sell/tasks/${encodeURIComponent(taskId)}/control`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action, cancel_open_orders: action === "stop" }),
        });
        if (!res.ok) throw new Error("task control failed");
        await refresh();
      } finally {
        button.disabled = false;
      }
    }

    function cleanupTaskLine(task) {
      const filled = task.progress_mode === "quote"
        ? formatSymbolQuantity(task.filled_quote, task.symbol, "quote")
        : formatSymbolQuantity(task.filled_base, task.symbol, "base");
      const side = String(task.side || "--").toUpperCase();
      return `${shortId(task.id)} · ${task.status || "--"} · ${task.exchange || "--"} ${task.symbol || "--"} · ${side} · ${filled}`;
    }

    function renderCleanupPreview(tasks, mode = "preview") {
      const box = document.getElementById("slow-cleanup-preview");
      if (!box) return;
      if (!tasks.length) {
        box.innerHTML = `<span class="config-chip config-neutral">Cleanup</span>No completed Auto Buy/Sell tasks to delete.`;
        return;
      }
      const lines = tasks
        .slice(0, 8)
        .map((task) => `<li>${escapeHtml(cleanupTaskLine(task))}</li>`)
        .join("");
      const more = tasks.length > 8 ? `<li>+${tasks.length - 8} more</li>` : "";
      const label = mode === "deleted" ? "Deleted" : "Cleanup preview";
      const verb = mode === "deleted" ? "Deleted" : "Will delete";
      box.innerHTML = `
        <span class="config-chip ${mode === "deleted" ? "config-same" : "config-diff"}">${label}</span>
        ${verb} ${tasks.length} completed task record(s):
        <ul>${lines}${more}</ul>
      `;
    }

    async function clearTerminalAutoBuySellTasks() {
      const button = document.getElementById("slow-clear-terminal");
      button.disabled = true;
      try {
        const previewRes = await fetch("/api/auto-buy-sell/tasks/cleanup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ terminal_only: true, preview: true }),
        });
        const preview = await previewRes.json();
        if (!previewRes.ok) throw new Error(preview.error || "task cleanup preview failed");
        const tasks = preview.removed_tasks || [];
        renderCleanupPreview(tasks);
        if (tasks.length === 0) return;
        const message = [
          `Cleanup will delete ${tasks.length} completed Auto Buy/Sell task record(s):`,
          "",
          ...tasks.slice(0, 12).map((task) => `- ${cleanupTaskLine(task)}`),
          tasks.length > 12 ? `- +${tasks.length - 12} more` : "",
          "",
          "Open orders are not canceled by cleanup. Continue?",
        ].filter(Boolean).join("\n");
        if (!window.confirm(message)) {
          text("slow-meta", "cleanup canceled");
          return;
        }
        const res = await fetch("/api/auto-buy-sell/tasks/cleanup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ terminal_only: true }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "task cleanup failed");
        renderCleanupPreview(result.removed_tasks || [], "deleted");
        await refresh();
      } finally {
        button.disabled = false;
      }
    }

    let refreshHadSuccess = false;
    let refreshFailureCount = 0;
    let refreshInFlight = false;
    const STATE_FETCH_TIMEOUT_MS = 10000;

    function statusLabel(status) {
      const value = String(status || "starting").toLowerCase();
      const labels = {
        running: "Running",
        degraded: "Attention",
        error: "Error",
        starting: "Checking",
        checking: "Checking",
        paused: "Paused",
        auto_stopped: "Stopped",
      };
      return labels[value] || value;
    }

    function pillClassForStatus(status) {
      if (status === "auto_stopped") return "degraded";
      if (status === "checking") return "starting";
      if (["running", "degraded", "error", "starting", "paused"].includes(status)) {
        return status;
      }
      return "degraded";
    }

    function setHeaderStatus(statusValue, label) {
      const status = document.getElementById("status");
      const normalized = statusValue || "starting";
      status.textContent = label || statusLabel(normalized);
      status.className = `pill ${pillClassForStatus(normalized)}`;
    }

    async function fetchWithTimeout(url, options = {}, timeoutMs = STATE_FETCH_TIMEOUT_MS) {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
      try {
        return await fetch(url, { ...options, signal: controller.signal });
      } finally {
        window.clearTimeout(timeout);
      }
    }

    function renderCommonState(data) {
      setHeaderStatus(data.status || "starting");
      renderAuthProfile(data.auth);
      document.getElementById("program-toggle").checked = data.program?.running !== false;

      text("scan-count", data.scan?.count ?? 0);
      text("latency", data.scan?.elapsed_ms == null ? "--" : `${data.scan.elapsed_ms} ms`);
      text("opp-count", data.opportunities?.length ?? 0);
      text("notional", data.config ? `$${money.format(data.config.notional_quote)}` : "--");
      text("threshold", data.config ? `$${data.config.min_profit_quote} / ${data.config.min_profit_bps} bps` : "--");
      text("updated", formatAge(data.scan?.last_finished));
      text("onchain-status", data.onchain?.status || "off");
      text("common-quote", data.config?.common_quote_currency || "USD");
      text("warnings", (data.warnings || []).join(" · "));
      text("onchain-meta", data.onchain?.mint ? `${data.onchain.label || "Token"} · ${shortAddress(data.onchain.mint)} · ${formatAge(data.onchain.last_finished)}` : "");
      renderAccountBalanceSummary(data.account_balances);

      const mmRuntime = data.market_maker?.runtime || {};
      const mmPlan = data.market_maker?.plan;
      const mmRuntimeText = mmRuntime.status ? ` · ${mmRuntime.status} · open ${mmRuntime.open_order_count || 0} · placed ${mmRuntime.placed_count || 0} · canceled ${mmRuntime.canceled_count || 0}` : "";
      const mmMarketData = mmRuntime.market_data || data.market_maker?.market_data || {};
      const mmWsText = mmMarketData.cache?.websocket_supported === false ? " · WS unsupported" : "";
      const mmMarketDataText = mmMarketData.source
        ? ` · ${String(mmMarketData.source).toUpperCase()}${mmMarketData.age_seconds == null ? "" : ` ${Number(mmMarketData.age_seconds).toFixed(2)}s`}${mmWsText}`
        : mmWsText;
      const mmQuote = data.market_maker?.quote_conversion;
      const mmQuoteText = mmQuote?.quote_currency ? ` · quote ${mmQuote.quote_currency}${mmQuote.quote_to_common_rate == null ? "" : `→${mmQuote.common_quote_currency} ${mmQuote.quote_to_common_rate}`}` : "";
      const mmFeatures = data.market_maker?.exchange_features || {};
      const mmFeatureText = Object.keys(mmFeatures).length ? ` · post-only ${mmFeatures.post_only ? "yes" : "no"}` : "";
      const mmSpreadText = mmPlan?.existing_spread_bps == null
        ? "--"
        : Number(mmPlan.existing_spread_bps).toFixed(2);
      text("mm-meta", mmPlan ? `${data.market_maker.mode || "dry_run"} · ${mmPlan.exchange} ${mmPlan.symbol} · mid ${fmt.format(mmPlan.mid_price)} · spread ${mmSpreadText} bps${mmMarketDataText}${mmQuoteText}${mmFeatureText}${mmRuntimeText}` : `${data.market_maker?.status || "disabled"}${mmMarketDataText}${mmQuoteText}${mmFeatureText}${mmRuntimeText}`);

      const slowPlan = data.slow_execution?.plan;
      const slowPriceText = slowPlan?.order ? `order ${fmt.format(slowPlan.order.price)}` : (data.slow_execution?.status || "no order");
      text("slow-meta", slowPlan ? `${data.slow_execution.mode || "dry_run"} · ${slowPlan.exchange} ${slowPlan.symbol} · ${slowPlan.side.toUpperCase()} · ${slowPriceText}` : (data.slow_execution?.status || "disabled"));

      const gridPlan = data.spot_grid?.plan;
      const gridReason = (data.spot_grid?.safety?.reasons || [])[0] || data.spot_grid?.error || "";
      text(
        "grid-meta",
        gridPlan
          ? `${data.spot_grid.mode || "dry_run"} · ${gridPlan.exchange} ${gridPlan.symbol} · mid ${fmt.format(gridPlan.mid_price)} · step ${Number(gridPlan.grid_step_bps || 0).toFixed(2)} bps · orders ${(gridPlan.orders || []).length}${gridReason ? ` · ${gridReason}` : ""}`
          : `${data.spot_grid?.status || "disabled"}${gridReason ? ` · ${gridReason}` : ""}`
      );

      const dcaPlan = data.dca?.plan;
      const dcaReason = (data.dca?.safety?.reasons || [])[0] || data.dca?.error || "";
      const dcaNext = dcaPlan?.next_order ? `next ${fmt.format(dcaPlan.next_order.price)}` : (dcaPlan?.reason || data.dca?.status || "disabled");
      text(
        "dca-meta",
        dcaPlan
          ? `${data.dca.mode || "dry_run"} · ${dcaPlan.exchange} ${dcaPlan.symbol} · ${String(dcaPlan.side || "").toUpperCase()} · ${dcaNext} · ${dcaPlan.max_orders || 0} orders${dcaReason ? ` · ${dcaReason}` : ""}`
          : `${data.dca?.status || "disabled"}${dcaReason ? ` · ${dcaReason}` : ""}`
      );

      const execPlan = data.execution_algo?.plan;
      const execReason = (data.execution_algo?.safety?.reasons || [])[0] || data.execution_algo?.error || "";
      text(
        "exec-meta",
        execPlan
          ? `${data.execution_algo.mode || "dry_run"} · ${execPlan.exchange} ${execPlan.symbol} · ${String(execPlan.algo || "").toUpperCase()} ${String(execPlan.side || "").toUpperCase()} · ${formatSymbolQuantity(execPlan.total_quote || 0, execPlan.symbol, "quote")} · slices ${(execPlan.schedule || []).length}${execReason ? ` · ${execReason}` : ""}`
          : `${data.execution_algo?.status || "disabled"}${execReason ? ` · ${execReason}` : ""}`
      );

      const backtestResult = data.backtest?.result;
      text(
        "backtest-meta",
        backtestResult
          ? `${displayStrategy(backtestResult.strategy)} · return ${Number(backtestResult.return_pct || 0).toFixed(2)}% · max DD ${Number(backtestResult.max_drawdown_pct || 0).toFixed(2)}% · trades ${backtestResult.trade_count || 0}`
          : `${data.backtest?.status || "disabled"}${data.backtest?.error ? ` · ${data.backtest.error}` : ""}`
      );

      renderPortfolio(data.portfolio);
      renderStrategySummaries(data);
    }

    function renderVisiblePage(data, page = currentPage, options = {}) {
      const activePage = PAGE_IDS.has(page) ? page : "status";
      const now = Date.now();
      const minIntervalMs = PAGE_RENDER_INTERVAL_MS[activePage] || 1000;
      if (!options.force && lastVisibleRenderAt[activePage] && now - lastVisibleRenderAt[activePage] < minIntervalMs) {
        return;
      }
      lastVisibleRenderAt[activePage] = now;
      if (activePage === "settings") {
        renderOpenSection("markets-config", () => renderMarketsConfig(data));
        renderOpenSection("carry-config", () => renderCashCarryConfig(data));
        renderOpenSection("risk-form", () => renderRiskControls(data.operations, data.trading_console));
        renderOpenSection("strategy-instances", () => renderStrategyCenter(data.strategy_center));
        renderOpenSection("api-accounts", () => renderApiAccountsPanel(data.strategy_center));
        renderOpenSection("funding-arb-form", () => renderFundingArbitragePanel(data.strategy_center));
        renderOpenSection("signal-bot-form", () => renderSignalBotPanel(data.strategy_center));
        renderOpenSection("mm-orders", () => {
          renderMarketMakerConfig(data.market_maker?.config, data.market_maker?.accounts);
          renderMarketMakerSafety(data.market_maker);
          renderMarketMaker(data.market_maker);
        });
        renderOpenSection("slow-orders", () => {
          renderSlowExecutionConfig(data.slow_execution?.config, data.slow_execution?.accounts);
          renderSlowExecution(data.slow_execution);
          renderSlowExecutionTasks(data.slow_execution?.tasks, data.slow_execution?.config);
        });
        renderOpenSection("grid-orders", () => {
          renderSpotGridConfig(data.spot_grid?.config, data.spot_grid?.accounts);
          renderSpotGrid(data.spot_grid);
        });
        renderOpenSection("dca-orders", () => {
          renderDcaConfig(data.dca?.config, data.dca?.accounts);
          renderDca(data.dca);
        });
        renderOpenSection("exec-schedule", () => {
          renderExecutionAlgoConfig(data.execution_algo?.config, data.execution_algo?.accounts);
          renderExecutionAlgo(data.execution_algo);
        });
        renderOpenSection("backtest-points", () => {
          renderBacktestConfig(data.backtest?.config, data.backtest?.accounts);
          renderBacktest(data.backtest);
        });
        return;
      }
      if (activePage === "records") {
        renderOpenSection("console-strategies", () => renderTradingConsole(data.trading_console, data.order_activity));
        renderOpenSection("open-orders", () => renderOrderActivity(data.order_activity));
        renderOpenSection("strategy-timeline", () => renderRiskEvents(data.operations));
        renderOpenSection("audit-events", () => renderAuditTrail(data.operations));
        renderOpenSection("holder-changes", () => renderHolders(data.onchain));
        return;
      }
      renderOpenSection("readiness-actions", () => renderReadiness(data.readiness, data.runtime_store));
      renderOpenSection("markets", () => renderMarkets(data.markets));
      renderOpenSection("account-balances", () => renderAccountBalances(data.account_balances));
      renderOpenSection("derivatives-risk", () => renderDerivativesRisk(data.derivatives));
      renderOpenSection("funding-basis", () => renderFundingBasis(data.funding_basis));
      renderOpenSection("contract-strategies", () => renderContractStrategies(data.contract_strategies));
      renderOpenSection("options-arbitrage", () => renderOptionsArbitrage(data.options_arbitrage));
      renderOpenSection("rates", () => renderRates(data.quote_rates));
      renderOpenSection("opportunities", () => renderOpportunities(data.opportunities));
      renderOpenSection("holders", () => renderHolders(data.onchain));
    }

    async function refresh(options = {}) {
      if (refreshInFlight) {
        if (options.force) refreshQueued = true;
        return;
      }
      refreshInFlight = true;
      const requestedPage = PAGE_IDS.has(currentPage) ? currentPage : "status";
      try {
        const params = new URLSearchParams({ view: requestedPage });
        const sectionIds = openSectionIdsForPage(requestedPage);
        if (sectionIds.length > 0) params.set("sections", sectionIds.join(","));
        const stateUrl = `/api/state?${params.toString()}`;
        const res = await fetchWithTimeout(stateUrl, { cache: "no-store" });
        if (res.status === 401) {
          window.location.assign("/login");
          return;
        }
        if (!res.ok) throw new Error(`state request failed (${res.status})`);
        const data = await res.json();
        if (!data || typeof data !== "object" || Array.isArray(data)) {
          throw new Error("state response is invalid");
        }
        refreshHadSuccess = true;
        refreshFailureCount = 0;

        lastState = data;
        pageStateCache[requestedPage] = data;
        renderCommonState(data);
        if (requestedPage === currentPage) {
          renderVisiblePage(data, requestedPage, { force: Boolean(options.force) });
        }
      } catch (error) {
        refreshFailureCount += 1;
        const message = error?.name === "AbortError"
          ? "state request timed out"
          : (error?.message || String(error || "state request failed"));
        if (!refreshHadSuccess) {
          setHeaderStatus("degraded", "Retrying");
          text("warnings", `Connecting to server: ${message}`);
        } else if (refreshFailureCount < 3) {
          setHeaderStatus("degraded", "Reconnecting");
          text("warnings", `Connection retry ${refreshFailureCount}/3: ${message}`);
        } else {
          setHeaderStatus("degraded", "Stale");
          text("warnings", `State is stale: ${message}`);
        }
      } finally {
        refreshInFlight = false;
        if (refreshQueued) {
          refreshQueued = false;
          refresh({ force: true });
        }
      }
    }

    setupCompactSections();
    setActivePage(pageFromLocation(), { refresh: false });
    window.addEventListener("hashchange", () => {
      setActivePage(pageFromLocation());
    });

    refresh({ force: true });
    document.getElementById("program-toggle").addEventListener("change", (event) => {
      setProgramRunning(event.target.checked);
    });
    document.getElementById("profile-asset").addEventListener("change", updateProfileAsset);
    document.getElementById("risk-form").addEventListener("input", () => {
      riskFormDirty = true;
    });
    document.getElementById("markets-form").addEventListener("submit", addSpotMarket);
    document.getElementById("carry-form").addEventListener("submit", addCashCarryPair);
    document.getElementById("risk-form").addEventListener("submit", applyRiskConfig);
    document.getElementById("mm-form").addEventListener("input", () => {
      mmFormDirty = true;
    });
    document.getElementById("mm-form").addEventListener("change", () => {
      mmFormDirty = true;
    });
    document.getElementById("mm-form").addEventListener("submit", applyMarketMakerConfig);
    document.getElementById("slow-form").addEventListener("input", () => {
      slowFormDirty = true;
    });
    document.getElementById("slow-form").addEventListener("change", () => {
      slowFormDirty = true;
    });
    document.getElementById("slow-side").addEventListener("change", updateSlowGateLabels);
    document.getElementById("slow-form").addEventListener("submit", applySlowExecutionConfig);
    document.getElementById("grid-form").addEventListener("input", () => {
      gridFormDirty = true;
    });
    document.getElementById("grid-form").addEventListener("change", () => {
      gridFormDirty = true;
    });
    document.getElementById("grid-form").addEventListener("submit", applySpotGridConfig);
    document.getElementById("dca-form").addEventListener("input", () => {
      dcaFormDirty = true;
    });
    document.getElementById("dca-form").addEventListener("change", () => {
      dcaFormDirty = true;
    });
    document.getElementById("dca-form").addEventListener("submit", applyDcaConfig);
    document.getElementById("exec-form").addEventListener("input", () => {
      execFormDirty = true;
    });
    document.getElementById("exec-form").addEventListener("change", () => {
      execFormDirty = true;
    });
    document.getElementById("exec-form").addEventListener("submit", applyExecutionAlgoConfig);
    document.getElementById("backtest-form").addEventListener("input", () => {
      backtestFormDirty = true;
    });
    document.getElementById("backtest-form").addEventListener("change", () => {
      backtestFormDirty = true;
    });
    document.getElementById("backtest-form").addEventListener("submit", applyBacktestConfig);
    document.getElementById("strategy-center-form").addEventListener("input", () => {
      strategyCenterFormDirty = true;
    });
    document.getElementById("strategy-center-form").addEventListener("change", () => {
      strategyCenterFormDirty = true;
    });
    document.getElementById("strategy-instance-exchange").addEventListener("change", syncStrategyInstanceSymbols);
    document.getElementById("strategy-instance-symbol").addEventListener("change", () => {
      const asset = document.getElementById("strategy-instance-asset");
      const symbol = document.getElementById("strategy-instance-symbol").value;
      if (symbol && !asset.value.trim()) asset.value = baseCurrency(symbol);
    });
    document.getElementById("strategy-center-form").addEventListener("submit", applyStrategyCenterConfig);
    document.getElementById("api-account-form").addEventListener("input", () => {
      apiAccountFormDirty = true;
    });
    document.getElementById("api-account-form").addEventListener("submit", applyApiAccountConfig);
    document.getElementById("funding-arb-form").addEventListener("input", () => {
      fundingArbFormDirty = true;
    });
    document.getElementById("funding-arb-form").addEventListener("submit", applyFundingArbConfig);
    document.getElementById("signal-bot-form").addEventListener("input", () => {
      signalBotFormDirty = true;
    });
    document.getElementById("signal-bot-form").addEventListener("submit", applySignalBotConfig);
    document.getElementById("slow-create-task").addEventListener("click", createAutoBuySellTask);
    document.getElementById("slow-clear-terminal").addEventListener("click", clearTerminalAutoBuySellTasks);
    setInterval(() => {
      if (!document.hidden) refresh();
    }, REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh({ force: true });
    });
