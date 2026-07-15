const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 10 });
    const money = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
    const compact = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
	    const shortNumber = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 });
	    const PAGE_IDS = new Set(["status", "trading", "quant", "settings", "records"]);
	    const CORE_CONTROL_SECTION_IDS = new Set([
	      "mm-section",
	      "slow-section",
	      "rebalance-section",
	      "spot-arbitrage-section",
	    ]);
	    let currentPage = pageFromLocation();
	    let lastState = null;
	    let refreshQueued = false;
	    const pageStateCache = {};
	    const PAGE_RENDER_INTERVAL_MS = { status: 1500, trading: 2000, quant: 4000, settings: 3000, records: 2000 };
	    const PAGE_REFRESH_INTERVAL_MS = { status: 2000, trading: 3000, quant: 6000, settings: 5000, records: 3500 };
	    const REFRESH_INTERVAL_MS = PAGE_REFRESH_INTERVAL_MS.status;
	    const REFRESH_FAILURE_BACKOFF_MS = 15000;
	    const REFRESH_JITTER_MS = 300;
	    const LIVE_AUTO_BUY_SELL_CONFIRMATION = "ENABLE LIVE AUTO BUY SELL";
	    const LIVE_MARKET_MAKER_CONFIRMATION = "ENABLE LIVE MARKET MAKER";
	    const LIVE_REBALANCE_CONFIRMATION = "ENABLE LIVE REBALANCE";
	    let refreshTimer = null;
	    const PAGE_SECTION_IDS = {
	      status: [
	        "overview",
	        "readiness-actions",
	        "markets",
	        "account-balances",
	        "rates",
	        "opportunities",
	        "holders",
	      ],
	      trading: [
	        "strategy-settings-cards",
	        "mm-orders",
	        "slow-orders",
	        "rebalance-plan",
	        "markets-config",
	      ],
	      quant: [
	        "backtest-points",
	        "grid-orders",
	        "dca-orders",
	        "exec-schedule",
	        "carry-config",
	        "funding-arb-form",
	        "signal-bot-form",
	        "derivatives-risk",
	        "funding-basis",
	        "contract-strategies",
	        "options-arbitrage",
	      ],
	      settings: [
	        "user-workspace-section",
	        "risk-form",
	        "config-versions",
	        "strategy-instances",
	        "api-accounts",
	      ],
	      records: [
	        "console-strategies",
	        "open-orders",
	        "strategy-timeline",
	        "audit-events",
	        "holder-changes",
	      ],
	    };
	    const HIDDEN_UI_FEATURES = new Set([
	      "api_accounts",
	      "audit_trail",
	      "onchain_history",
	      "onchain_monitor",
	      "orders_detail",
	      "quote_rates",
	      "readiness",
	      "scan_status",
	      "strategy_center",
	      "strategy_timeline",
	    ]);
	    const PAGE_DOM_ORDER = {
	      trading: [
	        "trading-page-heading",
	        "strategy-settings-section",
	        "mm-section",
	        "slow-section",
	        "rebalance-section",
	        "spot-arbitrage-section",
	      ],
	      quant: [
	        "quant-page-heading",
	        "backtest-section",
	        "spot-grid-section",
	        "dca-section",
	        "execution-section",
	        "cash-carry-section",
	        "funding-arbitrage-section",
	        "signal-bot-section",
	        "derivatives-section",
	        "funding-basis-section",
	        "contract-strategies-section",
	        "options-arbitrage-section",
	      ],
	    };
	    const lastVisibleRenderAt = { status: 0, trading: 0, quant: 0, settings: 0, records: 0 };
    let configVersionPayload = null;
    let configVersionLoadAt = 0;
    let configVersionLoading = false;

    function uiFeatureNamesFor(el) {
      return String(el?.dataset?.uiFeature || "")
        .split(/\s+/)
        .map((value) => value.trim())
        .filter(Boolean);
    }

    function isUiFeatureHidden(el) {
      if (!el) return false;
      const target = el.closest?.("[data-ui-feature]") || el;
      if (target.dataset?.uiHiddenDefault === "true") return true;
      return uiFeatureNamesFor(target).some((feature) => HIDDEN_UI_FEATURES.has(feature));
    }

    function applyFeatureVisibility() {
      document.querySelectorAll("[data-ui-feature]").forEach((el) => {
        const hidden = isUiFeatureHidden(el);
        el.classList.toggle("ui-feature-hidden", hidden);
        el.setAttribute("aria-hidden", hidden ? "true" : "false");
      });
    }

    function pageFromLocation() {
      const hashPage = window.location.hash.replace("#", "");
      if (hashPage === "monitor") return "status";
      if (hashPage === "control") return "trading";
      return PAGE_IDS.has(hashPage) ? hashPage : "status";
    }

    function applyPageSectionOrder(page) {
      const main = document.querySelector("main");
      if (!main) return;
      for (const id of PAGE_DOM_ORDER[page] || []) {
        const section = document.getElementById(id);
        if (section) main.appendChild(section);
      }
    }

		    function setActivePage(page, options = {}) {
		      const activePage = PAGE_IDS.has(page) ? page : "status";
		      currentPage = activePage;
		      if (activePage !== "quant") scheduleUserBacktestPoll(false);
	      clearRefreshTimer();
	      applyFeatureVisibility();
	      applyPageSectionOrder(activePage);
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
	      ensureStateStream();
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
          if (isUiFeatureHidden(section)) return;
          if (!section.classList.contains("section-open")) closeOtherCoreControlSections(section.id);
          section.classList.toggle("section-open");
          sync();
          refreshOpenedSection(section);
        });
        title.addEventListener("keydown", (event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          if (isUiFeatureHidden(section)) return;
          event.preventDefault();
          if (!section.classList.contains("section-open")) closeOtherCoreControlSections(section.id);
          section.classList.toggle("section-open");
          sync();
          refreshOpenedSection(section);
        });
        sync();
      });
    }

    function closeOtherCoreControlSections(activeSectionId) {
      if (!CORE_CONTROL_SECTION_IDS.has(activeSectionId)) return;
      for (const sectionId of CORE_CONTROL_SECTION_IDS) {
        if (sectionId === activeSectionId) continue;
        const section = document.getElementById(sectionId);
        if (!section) continue;
        section.classList.remove("section-open");
        section.querySelector(".section-title")?.setAttribute("aria-expanded", "false");
      }
    }

    function text(id, value) {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    }

    function isSectionOpenFor(id) {
      const el = document.getElementById(id);
      const section = el?.closest(".compact-section");
      if (section && isUiFeatureHidden(section)) return false;
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
      if (isUiFeatureHidden(section)) return;
      if (!section.classList.contains("section-open") || section.dataset.page !== currentPage) return;
      const cachedState = pageStateCache[currentPage] || lastState;
      if (cachedState) {
        window.requestAnimationFrame(() => {
          renderVisiblePage(cachedState, currentPage, { force: true });
        });
      }
      refresh({ force: true });
      ensureStateStream();
    }

    function openSettingsSection(sectionId) {
      const section = document.getElementById(sectionId);
      if (!section || isUiFeatureHidden(section)) return;
      const targetPage = PAGE_IDS.has(section.dataset.page) ? section.dataset.page : "settings";
      if (currentPage !== targetPage) setActivePage(targetPage, { refresh: false });
      closeOtherCoreControlSections(sectionId);
      section.classList.add("section-open");
      const title = section.querySelector(".section-title");
      if (title) title.setAttribute("aria-expanded", "true");
      refreshOpenedSection(section);
      section.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function dangerConfirm(message, detail = "") {
      const fullMessage = detail ? `${uiText(message)}\n\n${detail}` : uiText(message);
      return window.confirm(fullMessage);
    }

    function formatAge(ts) {
      if (!ts) return "--";
      const age = Math.max(0, Date.now() / 1000 - ts);
      return age < 60 ? `${age.toFixed(0)}s ago` : `${(age / 60).toFixed(1)}m ago`;
    }

    function formatDurationSeconds(value) {
      const seconds = Math.max(0, Number(value || 0));
      if (seconds < 60) return `${Math.ceil(seconds)}s`;
      if (seconds < 3600) return `${Math.ceil(seconds / 60)}m`;
      return `${(seconds / 3600).toFixed(seconds < 36000 ? 1 : 0)}h`;
    }

    function baseCurrency(symbol) {
      return String(symbol || "").split("/")[0] || "BASE";
    }

    function quoteCurrency(symbol) {
      return (String(symbol || "").split("/")[1] || "QUOTE").split(":")[0];
    }

    function uiText(source) {
      return window.CryptoArbI18n?.t?.(source) || source;
    }

    // Lightweight toast notifications for action feedback. Errors stay
    // longer than confirmations; hovering pauses auto-dismiss.
    const TOAST_OK_MS = 3500;
    const TOAST_ERROR_MS = 8000;

    function toastContainer() {
      let container = document.getElementById("toast-container");
      if (!container) {
        container = document.createElement("div");
        container.id = "toast-container";
        container.setAttribute("role", "status");
        container.setAttribute("aria-live", "polite");
        document.body.appendChild(container);
      }
      return container;
    }

    function showToast(message, level = "ok") {
      const textValue = uiText(String(message || "")).trim();
      if (!textValue) return;
      const container = toastContainer();
      // Collapse duplicates: refresh the timer instead of stacking copies.
      for (const existing of container.children) {
        if (existing.__toastText === textValue) {
          existing.remove();
          break;
        }
      }
      const toast = document.createElement("div");
      toast.className = `toast toast-${level === "error" ? "error" : "ok"}`;
      toast.textContent = textValue;
      toast.__toastText = textValue;
      let timer = null;
      const dismiss = () => {
        window.clearTimeout(timer);
        toast.classList.add("toast-leaving");
        window.setTimeout(() => toast.remove(), 200);
      };
      const arm = () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(
          dismiss,
          level === "error" ? TOAST_ERROR_MS : TOAST_OK_MS,
        );
      };
      toast.addEventListener("mouseenter", () => window.clearTimeout(timer));
      toast.addEventListener("mouseleave", arm);
      toast.addEventListener("click", dismiss);
      container.appendChild(toast);
      while (container.children.length > 4) container.firstChild.remove();
      arm();
    }

    // Safety net: form handlers that throw without a catch used to fail
    // silently. Surface those errors instead of losing them.
    window.addEventListener("unhandledrejection", (event) => {
      const reason = event.reason;
      const message = reason?.message || String(reason || "");
      if (!message || message === "[object Object]") return;
      showToast(message, "error");
    });

    function applyMobileTableLabels(root = document) {
      const scope = root?.querySelectorAll ? root : document;
      scope.querySelectorAll("table.mobile-card-table").forEach((table) => {
        const labels = Array.from(table.querySelectorAll("thead th")).map((th) => th.textContent.trim());
        table.querySelectorAll("tbody tr").forEach((row) => {
          Array.from(row.children).forEach((cell, index) => {
            if (cell.tagName !== "TD" || cell.hasAttribute("colspan")) return;
            cell.dataset.label = labels[index] || cell.dataset.label || "";
          });
        });
      });
    }

    function ensureDirtyBadge(section) {
      const title = section?.querySelector(".section-title h2");
      if (!title) return null;
      let badge = title.querySelector(".dirty-badge");
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "dirty-badge";
        badge.textContent = uiText("Unsaved");
        title.appendChild(badge);
      }
      return badge;
    }

    function setCoreFormState(sectionId, buttonId, dirty, busy, defaultText = "Apply") {
      const section = document.getElementById(sectionId);
      if (section) {
        section.classList.toggle("has-unsaved", Boolean(dirty));
        ensureDirtyBadge(section);
      }
      const button = document.getElementById(buttonId);
      if (!button) return;
      const label = uiText(defaultText);
      button.disabled = Boolean(busy);
      button.classList.toggle("is-saving", Boolean(busy));
      button.textContent = busy ? uiText("Saving") : dirty ? `${label} *` : label;
    }

    function updateCoreFormStates() {
      setCoreFormState("risk-section", "risk-apply", riskFormDirty, riskFormBusy);
      setCoreFormState(
        "slow-section",
        "slow-apply",
        slowFormDirty,
        slowFormBusy,
        "Save Defaults",
      );
      setCoreFormState(
        "mm-section",
        "mm-apply",
        mmFormDirty,
        mmFormBusy,
        "Save Settings",
      );
      setCoreFormState(
        "rebalance-section",
        "rebalance-apply",
        rebalanceFormDirty,
        rebalanceFormBusy,
        "Save Settings",
      );
    }

    function markRiskFormDirty() {
      riskFormDirty = true;
      updateCoreFormStates();
    }

    function markSlowFormDirty() {
      slowFormDirty = true;
      updateCoreFormStates();
      renderSlowExecutionWorkflow(lastState?.slow_execution);
    }

    function markMarketMakerFormDirty() {
      mmFormDirty = true;
      updateCoreFormStates();
      renderMarketMakerWorkflow(lastState?.market_maker);
    }

    function renderStrategyWorkflow(rootId, steps) {
      const root = document.getElementById(rootId);
      if (!root) return;
      root.innerHTML = steps.map((step, index) => {
        const state = ["ready", "live", "blocked"].includes(step.state)
          ? step.state
          : "idle";
        return `
          <div class="strategy-workflow-step is-${state}">
            <span class="strategy-workflow-index">${index + 1}</span>
            <div class="strategy-workflow-copy">
              <div class="strategy-workflow-title">
                <span>${escapeHtml(uiText(step.title))}</span>
                <span class="strategy-workflow-state">${escapeHtml(uiText(step.label || state))}</span>
              </div>
              <div class="strategy-workflow-detail" title="${escapeHtml(uiText(step.detail || ""))}">${escapeHtml(uiText(step.detail || "--"))}</div>
            </div>
          </div>
        `;
      }).join("");
    }

    function strategyLifecycleRows(strategyId, data = lastState) {
      const rows = data?.strategy_lifecycle?.instances;
      if (!Array.isArray(rows)) return [];
      return rows.filter((row) => row?.strategy_id === strategyId);
    }

    function strategyLifecycleRow(strategyId, options = {}) {
      const rows = strategyLifecycleRows(strategyId, options.data || lastState);
      if (options.instanceId) {
        const exact = rows.find((row) => row.instance_id === options.instanceId);
        if (exact) return exact;
      }
      if (options.account || options.symbol) {
        const route = rows.find((row) => (
          (!options.account || row.account === options.account)
          && (!options.symbol || row.symbol === options.symbol)
        ));
        if (route) return route;
      }
      return rows[0] || null;
    }

    function lifecyclePriority(row) {
      const convergence = row?.convergence_state || "";
      if (convergence === "error") return 0;
      if (convergence === "blocked") return 1;
      if (convergence === "transitioning") return 2;
      const actualPriority = {
        starting: 3,
        pausing: 4,
        stopping: 4,
        waiting: 5,
        running: 6,
        paused: 7,
        complete: 8,
        stopped: 9,
      };
      return actualPriority[row?.actual_state] ?? 10;
    }

    function strategyLifecycleSummary(strategyId, data = lastState) {
      const rows = strategyLifecycleRows(strategyId, data);
      const worst = [...rows].sort((left, right) => lifecyclePriority(left) - lifecyclePriority(right))[0] || null;
      return {
        rows,
        worst,
        converged: rows.filter((row) => row.converged).length,
      };
    }

    function lifecycleStateLabel(value) {
      const labels = {
        starting: "Starting",
        running: "Running",
        waiting: "Waiting",
        pausing: "Pausing",
        paused: "Paused",
        stopping: "Stopping",
        stopped: "Stopped",
        blocked: "Blocked",
        error: "Error",
        complete: "Complete",
        in_sync: "In sync",
        transitioning: "Transitioning",
      };
      return uiText(labels[value] || String(value || "--"));
    }

    function lifecycleDetail(row, { compact = false } = {}) {
      if (!row) return "";
      const desired = lifecycleStateLabel(row.desired_state);
      const actual = lifecycleStateLabel(row.actual_state);
      const convergence = lifecycleStateLabel(row.convergence_state);
      const parts = compact
        ? [`${desired} → ${actual}`, convergence]
        : [
            `${uiText("Desired")}: ${desired}`,
            `${uiText("Actual")}: ${actual}`,
            convergence,
          ];
      if (row.raw_status && row.raw_status !== row.actual_state) {
        const rawLabels = { no_task: "No active task" };
        parts.push(uiText(rawLabels[row.raw_status] || row.raw_status));
      }
      if (row.reason) parts.push(row.reason);
      return parts.filter(Boolean).join(" · ");
    }

    function lifecycleWorkflowStep(row, fallback) {
      if (!row) return fallback;
      const blocked = ["blocked", "error"].includes(row.convergence_state);
      const active = ["running", "waiting"].includes(row.actual_state);
      return {
        title: fallback.title,
        state: blocked ? "blocked" : active && row.mode === "live" ? "live" : row.converged ? "ready" : "idle",
        label: lifecycleStateLabel(row.actual_state),
        detail: lifecycleDetail(row),
      };
    }

    function coreLiveRiskReadiness(strategyId, exchanges = []) {
      const risk = lastState?.operations?.risk || lastState?.config?.risk || {};
      const accountKeys = [...new Set(exchanges.filter(Boolean))];
      const globalReady = risk.enabled !== false
        && risk.trading_enabled !== false
        && risk.allow_live_trading === true;
      const strategyReady = risk.strategy_enabled?.[strategyId] !== false;
      const blockedAccount = accountKeys.find(
        (exchange) => risk.account_enabled?.[exchange] === false,
      ) || "";
      const accountsReady = !blockedAccount;
      let detail = "Risk checks passed";
      if (!globalReady) detail = "Global live gate is off";
      else if (!strategyReady) detail = "Strategy risk switch is off";
      else if (!accountsReady) detail = `${blockedAccount} ${uiText("account risk switch is off")}`;
      return {
        ready: globalReady && strategyReady && accountsReady,
        globalReady,
        strategyReady,
        accountsReady,
        detail,
      };
    }

    async function runStrategyPreflight(strategyId, candidate) {
      const response = await fetch("/api/strategies/preflight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ strategy_id: strategyId, candidate }),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || "strategy preflight failed");
      }
      const preflight = result.preflight || {};
      if (!preflight.ready || !preflight.token) {
        const blockers = Array.isArray(preflight.blockers) ? preflight.blockers : [];
        throw new Error(
          blockers.length
            ? `${uiText("Preflight blocked")}: ${blockers.slice(0, 3).join("; ")}`
            : uiText("Strategy preflight did not approve this start."),
        );
      }
      return preflight;
    }

    function setStrategyFeedback(id, message = "", level = "") {
      const feedback = document.getElementById(id);
      if (!feedback) return;
      feedback.textContent = message ? uiText(message) : "";
      feedback.classList.toggle("is-error", level === "error");
      feedback.classList.toggle("is-ok", level === "ok");
    }

    function formatSymbolQuantity(value, symbol, mode) {
      const currency = mode === "quote" ? quoteCurrency(symbol) : baseCurrency(symbol);
      return `${currency} ${formatBalanceAmount(value || 0)}`;
    }

    function marketLimitKey(exchange, symbol) {
      return `${String(exchange || "").trim()}::${String(symbol || "").trim()}`;
    }

    function marketLimitFor(exchange, symbol) {
      const key = marketLimitKey(exchange, symbol);
      return (currentMarketLimits || []).find((row) => marketLimitKey(row.exchange, row.symbol) === key) || null;
    }

    function marketLimitValue(row, field) {
      const value = row?.limits?.[field];
      const numeric = Number(value);
      return Number.isFinite(numeric) ? numeric : null;
    }

    function marketPrecisionValue(row, field) {
      const value = row?.precision?.[field];
      const numeric = Number(value);
      return Number.isFinite(numeric) ? numeric : null;
    }

    function formatLimitValue(value, currency = "") {
      if (value == null) return "--";
      const prefix = currency ? `${currency} ` : "";
      return `${prefix}${formatBalanceAmount(value)}`;
    }

    function marketLimitSummary(row, symbol) {
      if (!row) return uiText("Exchange minimum unavailable");
      if (row.status && row.status !== "ok") {
        return row.error || `${uiText("Exchange minimum unavailable")} (${row.status})`;
      }
      const quote = quoteCurrency(symbol || row.symbol);
      const base = baseCurrency(symbol || row.symbol);
      const costMin = marketLimitValue(row, "cost_min");
      const amountMin = marketLimitValue(row, "amount_min");
      const priceTick = marketPrecisionValue(row, "price");
      const parts = [];
      parts.push(`${uiText("Min notional")}: ${formatLimitValue(costMin, quote)}`);
      if (amountMin != null) parts.push(`${uiText("Min base")}: ${formatLimitValue(amountMin, base)}`);
      if (priceTick != null) parts.push(`${uiText("Price tick")}: ${fmt.format(priceTick)}`);
      return parts.join(" · ");
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
      const securityLink = document.getElementById("security-link");
      if (!emailEl || !select) return;
      const mode = auth?.mode || "legacy";
      emailEl.textContent = mode === "user" ? (auth.username || auth.email || "User") : "Legacy";
      if (mode === "user" && auth.email) emailEl.title = auth.email;
      else emailEl.title = emailEl.textContent;
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
      if (securityLink) {
        securityLink.hidden = mode !== "user";
        securityLink.title = auth?.totp_enabled
          ? "Authenticator enabled"
          : "Authenticator not enabled";
      }
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
    let currentMarketLimits = [];
    let currentCashCarryPairs = [];

    async function cancelOrder(order, button) {
      const key = `${order.exchange}:${order.symbol}:${order.id}`;
      if (cancelOrderBusy.has(key)) return;
      const detail = `${order.label || order.exchange} · ${order.symbol || "--"} · ${String(order.side || "--").toUpperCase()} · ${order.id || "--"}`;
      if (!dangerConfirm("Confirm cancel this order?", detail)) return;
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
          <td data-label="${uiText("Account")}">${escapeHtml(order.label || order.exchange)}</td>
          <td data-label="${uiText("Symbol")}">${escapeHtml(order.symbol || "--")}</td>
          <td data-label="${uiText("Side")}" class="${orderSideClass(order.side)}">${escapeHtml(order.side ? order.side.toUpperCase() : "--")}</td>
          <td data-label="${uiText("Status")}">${escapeHtml(order.status || "--")}</td>
          <td data-label="${uiText("Price")}" class="num">${order.price == null ? "--" : fmt.format(order.price)}</td>
          <td data-label="${uiText("Amount")}" class="num">${formatBalanceAmount(order.amount)}</td>
          <td data-label="${uiText("Filled")}" class="num">${formatBalanceAmount(order.filled)}</td>
          <td data-label="${uiText("Remaining")}" class="num">${formatBalanceAmount(order.remaining)}</td>
          <td data-label="${uiText("Cost")}" class="num">${formatBalanceAmount(order.cost)}</td>
          <td data-label="${uiText("Updated")}">${formatTimestamp(order.timestamp)}</td>
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
          action.dataset.label = uiText("Action");
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
          <td data-label="${uiText("Account")}">${escapeHtml(fill.label || fill.exchange)}</td>
          <td data-label="${uiText("Symbol")}">${escapeHtml(fill.symbol || "--")}</td>
          <td data-label="${uiText("Side")}" class="${orderSideClass(fill.side)}">${escapeHtml(fill.side ? fill.side.toUpperCase() : "--")}</td>
          <td data-label="${uiText("Source")}">${escapeHtml(fill.source_label || displaySource(fill.source))}</td>
          <td data-label="${uiText("Price")}" class="num">${fill.price == null ? "--" : fmt.format(fill.price)}</td>
          <td data-label="${uiText("Amount")}" class="num">${formatBalanceAmount(fill.amount)}</td>
          <td data-label="${uiText("Cost")}" class="num">${formatBalanceAmount(fill.cost)}</td>
          <td data-label="${uiText("P/L")}" class="num ${pnlClass(fill.realized_pnl_common)}">${formatPnlValue(fill.realized_pnl_common)}</td>
          <td data-label="${uiText("Fee")}">${escapeHtml(formatFee(fill.fee))}</td>
          <td data-label="${uiText("Order")}" title="${escapeHtml(fill.order_id || "")}">${escapeHtml(shortId(fill.order_id))}</td>
          <td data-label="${uiText("Time")}">${formatTimestamp(fill.timestamp)}</td>
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

    function renderStrategyPerformance(orderActivity) {
      const body = document.getElementById("strategy-performance");
      if (!body) return;
      body.innerHTML = "";
      const performance = orderActivity?.strategy_performance || {};
      const rows = performance.rows || [];
      const summary = performance.summary || {};
      text(
        "strategy-performance-meta",
        `${performance.window || "daily"} · fills ${summary.fill_count || 0} · submitted ${summary.submitted_order_count || 0} · fees ${formatPnlValue(summary.fees_common || 0)} · P/L ${formatPnlValue(summary.realized_pnl || 0)}`
      );
      if (!rows.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="11">No strategy performance yet.</td>`;
        body.appendChild(tr);
        return;
      }
      for (const row of rows) {
        const mmDetail = row.strategy === "market_maker"
          ? `spread ${formatPnlValue(row.spread_capture_estimate || 0)} · inventory ${formatPnlValue(row.inventory_pnl_residual || 0)}`
          : row.strategy === "slow_execution" && row.progress_pct != null
            ? `progress ${Number(row.progress_pct).toFixed(1)}%`
            : "";
        const avgFill = row.task_average_fill_price ?? row.average_fill_price;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td data-label="${uiText("Strategy")}">${escapeHtml(displayStrategy(row.strategy))}</td>
          <td data-label="${uiText("Instance")}" title="${escapeHtml(row.instance_id || "")}">${escapeHtml(shortId(row.instance_id || "default"))}</td>
          <td data-label="${uiText("Account / Symbol")}">${escapeHtml(row.account || "--")}<br><span class="subtle">${escapeHtml(row.symbol || "--")}</span></td>
          <td data-label="${uiText("Fills / Submitted")}" class="num">${Number(row.filled_order_count || 0)} / ${Number(row.submitted_order_count || 0)}</td>
          <td data-label="${uiText("Fill Rate")}" class="num">${row.fill_rate_pct == null ? "--" : `${Number(row.fill_rate_pct).toFixed(1)}%`}</td>
          <td data-label="${uiText("Average Fill")}" class="num">${avgFill == null ? "--" : fmt.format(avgFill)}</td>
          <td data-label="${uiText("Fees")}" class="num">${formatPnlValue(row.fees_common || 0)}</td>
          <td data-label="${uiText("P/L")}" class="num ${pnlClass(row.realized_pnl)}">${formatPnlValue(row.realized_pnl || 0)}${mmDetail ? `<br><span class="subtle">${escapeHtml(mmDetail)}</span>` : ""}</td>
          <td data-label="${uiText("Slippage")}" class="num">${row.average_slippage_bps == null ? "--" : `${Number(row.average_slippage_bps).toFixed(2)} bps`}</td>
          <td data-label="${uiText("Latency")}" class="num">${row.average_submit_latency_ms == null ? "--" : `${Number(row.average_submit_latency_ms).toFixed(0)} ms`}</td>
          <td data-label="${uiText("Paper vs Live")}" class="num ${pnlClass(row.paper_vs_live_delta)}">${formatPnlValue(row.paper_vs_live_delta)}</td>
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
      const reconSuffix = reconciliation.automatic_retry_active
        ? `, ${uiText("Retrying")}`
        : reconciliation.auto_stop_suppressed
          ? ", suppressed"
          : "";
      const reconNoticeText = reconNotices > 0 ? `, notices ${reconNotices}` : "";
      const reconText = criticalRecon > 0
        ? `${reconciliation.status || "--"} (issues ${reconIssues}, critical ${criticalRecon}${reconNoticeText}${reconSuffix})`
        : reconIssues > 0
          ? `${reconciliation.status || "--"} (issues ${reconIssues}${reconNoticeText}${reconSuffix})`
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
      renderStrategyPerformance(orderActivity);
    }

    let consoleActionBusy = false;

    async function cancelBulkOrders(payload, button) {
      if (consoleActionBusy) return;
      const detail = payload?.scope === "all"
        ? uiText("All accounts")
        : `${uiText("Account")}: ${payload?.exchange || "--"}`;
      if (!dangerConfirm("Confirm cancel open orders?", detail)) return;
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
        empty.textContent = uiText("No accounts");
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
          <td data-label="${uiText("Strategy")}">${escapeHtml(strategy.label || strategy.id)}</td>
          <td data-label="${uiText("Status")}" class="${strategy.paused ? "risk-off" : strategy.configured ? "risk-ok" : "risk-off"}">${escapeHtml(strategy.paused ? "paused" : strategy.configured ? "enabled" : "disabled")}</td>
          <td data-label="${uiText("Live")}" class="${strategy.live ? "ok" : "missing"}">${strategy.live ? "YES" : "NO"}</td>
          <td data-label="${uiText("Account")}">${escapeHtml(strategy.exchange || "--")}</td>
          <td data-label="${uiText("Symbol")}">${escapeHtml(strategy.symbol || "--")}</td>
          <td data-label="${uiText("Mode")}">${escapeHtml(strategy.mode || "--")}</td>
          <td data-label="${uiText("Action")}" class="strategy-action"></td>
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

    function renderSpotArbitrageWorkflow(data) {
      const markets = currentSpotMarkets || [];
      const assetVenues = new Map();
      for (const market of markets) {
        const venues = assetVenues.get(market.asset) || new Set();
        if (market.exchange) venues.add(market.exchange);
        assetVenues.set(market.asset, venues);
      }
      const readyAssets = [...assetVenues.entries()]
        .filter(([, venues]) => venues.size >= 2)
        .map(([asset]) => asset);
      const parametersReady = readyAssets.length > 0;
      const risk = coreLiveRiskReadiness(
        "spot_spread",
        markets.map((market) => market.exchange),
      );
      const spot = data.spot_arbitrage || {};
      const live = spot.mode === "live";
      const lifecycle = strategyLifecycleRow("spot_spread", { data });
      renderStrategyWorkflow("spot-workflow", [
        {
          title: "Markets",
          state: parametersReady ? "ready" : "blocked",
          label: parametersReady ? "Ready" : "Required",
          detail: parametersReady
            ? `${readyAssets.join(", ")} · ${markets.length} ${uiText("market(s)")}`
            : "Add the same asset on at least two accounts",
        },
        {
          title: "Risk Check",
          state: risk.ready ? "ready" : "blocked",
          label: risk.ready ? "Ready" : "Blocked",
          detail: risk.detail,
        },
        lifecycleWorkflowStep(lifecycle, {
          title: "Run State",
          state: live ? "live" : "idle",
          label: live ? "Live" : "Dry Run",
          detail: spot.status || "waiting for market data",
        }),
      ]);
      const riskButton = document.getElementById("spot-open-risk");
      if (riskButton) riskButton.hidden = risk.ready;
    }

    function renderMarketsConfig(data) {
      if (marketsConfigBusy) return;
      const config = data.config || {};
      const exchanges = config.spot_exchanges || [];
      if (Array.isArray(data.market_limits)) currentMarketLimits = data.market_limits;
      currentSpotMarkets = (config.spot_markets || []).map(normalizeMarketRow);
      renderMarketExchangeSelect(exchanges);
      text(
        "markets-config-meta",
        `${currentSpotMarkets.length} market${currentSpotMarkets.length === 1 ? "" : "s"} · ${exchanges.length} account${exchanges.length === 1 ? "" : "s"}`
      );
      renderSpotArbitrageWorkflow(data);

      const body = document.getElementById("markets-config");
      body.innerHTML = "";
      if (currentSpotMarkets.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="8">No markets configured.</td>`;
        body.appendChild(tr);
        return;
      }

      currentSpotMarkets.forEach((market, index) => {
        const limit = marketLimitFor(market.exchange, market.symbol);
        const costMin = marketLimitValue(limit, "cost_min");
        const amountMin = marketLimitValue(limit, "amount_min");
        const priceTick = marketPrecisionValue(limit, "price");
        const title = marketLimitSummary(limit, market.symbol);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(market.asset)}</td>
          <td>${escapeHtml(market.exchange)}</td>
          <td>${escapeHtml(market.symbol)}</td>
          <td>${escapeHtml(market.quote_currency)}</td>
          <td class="num" title="${escapeHtml(title)}">${escapeHtml(formatLimitValue(costMin, market.quote_currency))}</td>
          <td class="num" title="${escapeHtml(title)}">${escapeHtml(formatLimitValue(amountMin, baseCurrency(market.symbol)))}</td>
          <td class="num" title="${escapeHtml(title)}">${priceTick == null ? "--" : fmt.format(priceTick)}</td>
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

    function renderStatusReasons(data) {
      const root = document.getElementById("status-reasons-section");
      if (!root) return;
      const items = [];
      for (const warning of (data.warnings || []).slice(0, 3)) {
        items.push(["Warning", warning]);
      }
      const mmReason = marketMakerStatusReason(data.market_maker || {});
      const mmRuntime = data.market_maker?.runtime || {};
      if (mmRuntime.problem_instance_count || mmReason) {
        items.push(["MM", mmReason || `${mmRuntime.problem_instance_count} instance(s) need attention`]);
      }
      const autoTasks = data.slow_execution?.tasks?.tasks || [];
      const autoProblem = autoTasks.find((task) => ["error", "blocked_by_risk", "waiting_for_start_price"].includes(task.status || ""));
      if (autoProblem) {
        items.push(["Auto", `${autoProblem.status || "--"} · ${autoTaskLastOrderText(autoProblem, autoProblem.config || {})}`]);
      }
      const reconciliation = data.order_activity?.reconciliation || {};
      if ((reconciliation.issue_count || 0) > 0) {
        items.push(["Orders", `${reconciliation.status || "--"} · ${reconciliation.issue_count} issue(s)`]);
      }
      const nextAction = (data.readiness?.next_actions || []).find((action) => action.level === "high" || action.status === "blocked");
      if (nextAction) {
        items.push(["Risk", nextAction.detail || nextAction.action || nextAction.scope || "review required"]);
      }

      const unique = [];
      const seen = new Set();
      for (const [label, detail] of items) {
        const normalized = String(detail || "").trim();
        if (!normalized || seen.has(`${label}:${normalized}`)) continue;
        seen.add(`${label}:${normalized}`);
        unique.push([label, normalized]);
      }
      root.classList.toggle("has-items", unique.length > 0);
      root.innerHTML = unique
        .slice(0, 4)
        .map(([label, detail]) => `
          <div class="status-reason">
            <strong>${escapeHtml(uiText(label))}</strong>
            <span title="${escapeHtml(detail)}">${escapeHtml(detail)}</span>
          </div>
        `)
        .join("");
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
      const mmProblems = Number(mmRuntime.problem_instance_count ?? marketMaker.problem_instance_count ?? 0);
      text("monitor-mm-summary", `${mmMode} · ${mmStatus}${mmProblems ? ` · ${mmProblems} attention` : ""}`);
      text(
        "monitor-mm-detail",
        mmPlan
          ? `${mmPlan.exchange} ${mmPlan.symbol} · mid ${fmt.format(mmPlan.mid_price)} · open ${mmRuntime.open_order_count ?? 0}${marketMakerStatusReason(marketMaker) ? ` · ${marketMakerStatusReason(marketMaker)}` : ""}`
          : marketMakerStatusReason(marketMaker) || marketMaker.error || mmRuntime.reason || "--"
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

      const risk = data.operations?.risk || data.config?.risk || {};
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
      renderStatusReasons(data);
    }

    function strategySettingsStatusClass(status) {
      const value = String(status || "").toLowerCase();
      if (["live", "running", "waiting", "complete", "unchanged", "placed", "ready", "ok", "enabled"].includes(value)) return "ok";
      if (["blocked", "blocked_by_risk", "error", "sync_error", "open_order_sync_error"].includes(value)) return "blocked";
      return "";
    }

    function renderStrategySettingsCard({ title, status, summary, detail, target }) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "strategy-settings-card";
      card.innerHTML = `
        <div class="strategy-settings-card-title">
          <span>${escapeHtml(uiText(title))}</span>
          <span class="strategy-settings-card-status ${strategySettingsStatusClass(status)}">${escapeHtml(uiText(status || "--"))}</span>
        </div>
        <div class="strategy-settings-card-summary">${escapeHtml(summary || "--")}</div>
        <div class="strategy-settings-card-detail">${escapeHtml(detail || "--")}</div>
      `;
      card.addEventListener("click", () => openSettingsSection(target));
      return card;
    }

    function renderStrategySettingCards(data) {
      const body = document.getElementById("strategy-settings-cards");
      if (!body) return;
      body.innerHTML = "";
      const risk = data.operations?.risk || data.config?.risk || {};
      const mm = data.market_maker || {};
      const mmRuntime = mm.runtime || {};
      const mmPlan = mm.plan || mmRuntime.last_plan || null;
      const mmStatus = mmRuntime.mode === "live" || mm.mode === "live"
        ? (mmRuntime.status || mm.status || "live")
        : (mm.status || "dry_run");
      const auto = data.slow_execution || {};
      const tasks = auto.tasks?.tasks || [];
      const activeTasks = tasks.filter((task) => !AUTO_TERMINAL_STATUSES.has(task.status || ""));
      const firstTask = activeTasks[0] || tasks[0];
      const firstTaskConfig = firstTask?.config || {};
      const autoProgressMode = firstTask?.progress_mode || ((firstTaskConfig.total_quote || 0) > 0 ? "quote" : "base");
      const autoProgressText = firstTask
        ? autoProgressMode === "quote"
          ? `${formatSymbolQuantity(firstTask.filled_quote, firstTaskConfig.symbol, "quote")} filled`
          : `${formatSymbolQuantity(firstTask.filled_base, firstTaskConfig.symbol, "base")} filled`
        : "";
      const mmSymbol = mmPlan?.symbol || mm.config?.symbol || "";
      const mmQuote = mmPlan
        ? `${quoteCurrency(mmSymbol)} ${money.format(mm.config?.quote_per_level || mmPlan.orders?.[0]?.quote_notional || 0)}/level`
        : "";
      const spot = data.spot_arbitrage || {};
      const spotOpportunities = Array.isArray(data.opportunities) ? data.opportunities.length : 0;
      const rebalance = data.cross_exchange_rebalance || {};
      const rebalanceRuntime = rebalance.runtime || {};
      const rebalancePlan = rebalance.plan || rebalanceRuntime.last_payload?.plan || null;
      const mmLifecycle = strategyLifecycleSummary("market_maker", data);
      const autoLifecycle = strategyLifecycleSummary("slow_execution", data);
      const rebalanceLifecycle = strategyLifecycleSummary("cross_exchange_rebalance", data);
      const spotLifecycle = strategyLifecycleSummary("spot_spread", data);
      const lifecycleCardDetail = (summary, fallback) => {
        if (!summary.worst) return fallback;
        const sync = `${summary.converged}/${summary.rows.length} ${uiText("In sync")}`;
        return `${sync} · ${lifecycleDetail(summary.worst, { compact: true })}`;
      };
      const cards = [
        {
          title: "Market Maker",
          status: mmLifecycle.worst?.actual_state || mmStatus,
          summary: mmPlan
            ? `${mmPlan.exchange || "--"} ${mmPlan.symbol || "--"}`
            : `${marketMakerInstances(mm).length || 0} instance(s)`,
          detail: lifecycleCardDetail(mmLifecycle, mmPlan
            ? `mid ${fmt.format(mmPlan.mid_price)} · ${mmQuote} · open ${mmRuntime.open_order_count || 0}`
            : marketMakerStatusReason(mm) || "Open to edit ladder and risk"),
          target: "mm-section",
        },
        {
          title: "Auto Buy/Sell",
          status: autoLifecycle.worst?.actual_state || (activeTasks.length ? "running" : (auto.status || "disabled")),
          summary: activeTasks.length ? `${activeTasks.length}/${tasks.length} active task(s)` : (auto.status || "disabled"),
          detail: lifecycleCardDetail(autoLifecycle, firstTask
            ? `${firstTaskConfig.exchange || "--"} ${firstTaskConfig.symbol || "--"} · ${String(firstTaskConfig.side || "--").toUpperCase()} · ${firstTask.progress_pct == null ? "--" : firstTask.progress_pct.toFixed(1) + "%"} · ${autoProgressText}`
            : "Open to create or edit a task"),
          target: "slow-section",
        },
        {
          title: "Cross-Exchange Rebalance",
          status: rebalanceLifecycle.worst?.actual_state || rebalanceRuntime.status || rebalance.status || "disabled",
          summary: rebalancePlan
            ? `${rebalancePlan.buy_exchange} -> ${rebalancePlan.sell_exchange}`
            : (rebalance.status || "disabled"),
          detail: lifecycleCardDetail(rebalanceLifecycle, rebalancePlan
            ? `${rebalancePlan.base_asset} · ${Number(rebalanceRuntime.progress_pct || 0).toFixed(1)}% · cost ${Number(rebalancePlan.expected_cost_bps || 0).toFixed(2)} bps`
            : "No plan"),
          target: "rebalance-section",
        },
        {
          title: "Spot Arbitrage",
          status: spotLifecycle.worst?.actual_state || spot.status || "disabled",
          summary: `${spot.mode || "dry_run"} · ${spot.status || "disabled"}`,
          detail: lifecycleCardDetail(spotLifecycle, `${spotOpportunities} ${uiText("active opportunity(s)")}`),
          target: "spot-arbitrage-section",
        },
        {
          title: "Risk Controls",
          status: risk.allow_live_trading ? "live" : "blocked",
          summary: risk.allow_live_trading ? "Live trading allowed" : "Live trading blocked",
          detail: `max/order USD ${money.format(risk.max_order_quote || 0)} · exposure USD ${money.format(risk.max_exposure_quote || 0)} · max open ${risk.max_open_orders || 0}`,
          target: "risk-section",
        },
      ];
      for (const card of cards) body.appendChild(renderStrategySettingsCard(card));
      text("strategy-settings-meta", `${cards.length} ${uiText("core controls")}`);
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
      marketMaker = selectedMarketMakerInstance(marketMaker) || marketMaker;
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
      const runtimeStatus = marketMaker?.runtime?.status || marketMaker?.status || "";
      const statusReason = marketMakerStatusReason(marketMaker);
      const statusText = runtimeStatus === "disabled"
        ? "Disabled"
        : ["error", "open_order_sync_error", "execution_error", "cancel_retry"].includes(runtimeStatus)
          ? runtimeStatus
          : approved ? "Ready" : "Blocked";
      const statusClass = runtimeStatus === "disabled"
        ? "risk-off"
        : ["error", "open_order_sync_error", "execution_error", "cancel_retry", "blocked_by_risk"].includes(runtimeStatus)
          ? "risk-blocked"
          : approved ? "risk-ok" : "risk-blocked";

      setValueState("mm-safety-status", statusText, statusClass);
      text("mm-safety-reason", statusReason || firstRiskMessage(risk));
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
      const maxLevelGapBps = Number(market.max_level_gap_bps || 0);
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
        `depth ${money.format(market.bid_depth_quote || 0)}/${money.format(market.ask_depth_quote || 0)} · gap ${Number.isFinite(maxLevelGapBps) ? maxLevelGapBps.toFixed(1) : "--"}/${limits.max_order_book_gap_bps || "--"} bps · age ${age == null ? "--" : age.toFixed(1) + "s"}`
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
      marketMaker = selectedMarketMakerInstance(marketMaker) || marketMaker;
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
          <td data-label="${uiText("Side")}" class="${order.side === "buy" ? "side-buy" : "side-sell"}">${order.side.toUpperCase()}</td>
          <td data-label="${uiText("Level")}" class="num">${order.level}</td>
          <td data-label="${uiText("Price")}" class="num">${fmt.format(order.price)}</td>
          <td data-label="${uiText("Amount")}" class="num">${compact.format(order.amount)}</td>
          <td data-label="${uiText("Quote")}" class="num" title="${commonQuote}">${formatSymbolQuantity(order.quote_notional, marketMaker.plan.symbol, "quote")}</td>
          <td data-label="${uiText("Distance")}" class="num">${order.distance_bps.toFixed(2)} bps</td>
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
        <td data-label="${uiText("Side")}" class="${order.side === "buy" ? "side-buy" : "side-sell"}">${order.side.toUpperCase()}</td>
        <td data-label="${uiText("Exchange")}">${plan.exchange}</td>
        <td data-label="${uiText("Symbol")}">${plan.symbol}</td>
        <td data-label="${uiText("Order Price")}" class="num">${fmt.format(order.price)}</td>
        <td data-label="${uiText("Slice Amount")}" class="num">${compact.format(order.amount)}</td>
        <td data-label="${uiText("Quote")}" class="num">${money.format(order.quote_notional)}</td>
        <td data-label="${uiText("Submitted")}" class="num">${submittedText}</td>
        <td data-label="${uiText("Remaining")}" class="num">${remainingText}</td>
        <td data-label="${uiText("Interval")}" class="num">${plan.interval_seconds}s</td>
        <td data-label="${uiText("Cancel")}" class="num">${plan.order_ttl_seconds || 0}s</td>
        <td data-label="${uiText("Start Gate")}">${escapeHtml(autoStartGateText(plan))}</td>
        <td data-label="${uiText("Stop Gate")}">${escapeHtml(autoStopGateText(plan))}</td>
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

    const USER_BACKTEST_ACTIVE_STATUSES = new Set(["queued", "fetching", "running"]);
    const USER_BACKTEST_STRATEGIES = new Set(["spot_grid", "dca"]);

    function backtestStatusClass(status) {
      if (status === "complete") return "risk-ok";
      if (status === "error" || status === "interrupted") return "risk-blocked";
      return USER_BACKTEST_ACTIVE_STATUSES.has(status) ? "ok" : "risk-off";
    }

    function backtestPercent(value, digits = 2) {
      const number = Number(value);
      return Number.isFinite(number) ? `${number.toFixed(digits)}%` : "--";
    }

    function backtestEpoch(value) {
      const seconds = Number(value);
      return Number.isFinite(seconds) ? formatTimestamp(seconds * 1000) : "--";
    }

    function replaceBacktestOptions(select, rows, preferred, placeholder) {
      select.innerHTML = "";
      if (!rows.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = uiText(placeholder);
        select.appendChild(option);
        select.disabled = true;
        return "";
      }
      select.disabled = false;
      for (const row of rows) {
        const option = document.createElement("option");
        option.value = row.value;
        option.textContent = row.label;
        select.appendChild(option);
      }
      const selected = rows.some((row) => row.value === preferred)
        ? preferred
        : rows[0].value;
      select.value = selected;
      return selected;
    }

    function syncBacktestAccountOptions(preferredAccount = "", applyFeeDefault = false) {
      const workspace = currentUserWorkspace || {};
      const projectId = document.getElementById("backtest-project").value;
      const strategyId = document.getElementById("backtest-strategy").value;
      const strategy = (workspace.strategies || []).find((row) => row.id === strategyId);
      const assigned = new Set(strategy?.account_ids || []);
      const accounts = (workspace.accounts || [])
        .filter((account) => (
          account.project_id === projectId
          && account.market_type === "spot"
          && Boolean(account.symbol)
          && assigned.has(account.id)
        ))
        .map((account) => ({
          value: account.id,
          label: `${account.label || account.exchange} · ${account.exchange} · ${account.symbol}`,
        }));
      const accountId = replaceBacktestOptions(
        document.getElementById("backtest-account"),
        accounts,
        preferredAccount,
        "No assigned spot account",
      );
      if (applyFeeDefault && strategy?.risk?.paper_fee_bps != null) {
        setNumericField("backtest-fee", strategy.risk.paper_fee_bps);
      }
      const account = (workspace.accounts || []).find((row) => row.id === accountId);
      const symbol = account?.symbol || "BASE/QUOTE";
      text("backtest-cash-label", `${uiText("Initial Cash")} (${quoteCurrency(symbol)})`);
      text("backtest-base-label", `${uiText("Initial Base")} (${baseCurrency(symbol)})`);
      document.getElementById("backtest-run").disabled = !accountId || backtestFormBusy;
    }

    function syncBacktestStrategyOptions(preferredStrategy = "", preferredAccount = "") {
      const workspace = currentUserWorkspace || {};
      const projectId = document.getElementById("backtest-project").value;
      const strategies = (workspace.strategies || [])
        .filter((strategy) => (
          strategy.project_id === projectId
          && USER_BACKTEST_STRATEGIES.has(strategy.strategy_type)
        ))
        .map((strategy) => ({
          value: strategy.id,
          label: `${strategy.name || strategy.id} · ${uiText(workspaceStrategyDefinition(strategy.strategy_type)?.label || strategy.strategy_type)}`,
        }));
      replaceBacktestOptions(
        document.getElementById("backtest-strategy"),
        strategies,
        preferredStrategy,
        "No Spot Grid or DCA strategy",
      );
      syncBacktestAccountOptions(preferredAccount, false);
    }

    function renderBacktestSelectors(workspace) {
      currentUserWorkspace = workspace || currentUserWorkspace;
      const projectSelect = document.getElementById("backtest-project");
      if (!projectSelect) return;
      const previousProject = projectSelect.value;
      const previousStrategy = document.getElementById("backtest-strategy").value;
      const previousAccount = document.getElementById("backtest-account").value;
      const projects = (currentUserWorkspace?.projects || [])
        .filter((project) => project.status === "active")
        .map((project) => ({
          value: project.id,
          label: `${project.name || project.id} · ${project.symbol || "--"}`,
        }));
      replaceBacktestOptions(
        projectSelect,
        projects,
        previousProject,
        "No active projects",
      );
      syncBacktestStrategyOptions(previousStrategy, previousAccount);
    }

    function drawBacktestChart(points) {
      const canvas = document.getElementById("backtest-chart");
      if (!canvas) return;
      const cssWidth = Math.max(280, Math.floor(canvas.clientWidth || 800));
      const cssHeight = Math.max(140, Math.floor(canvas.clientHeight || 180));
      const ratio = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.floor(cssWidth * ratio);
      canvas.height = Math.floor(cssHeight * ratio);
      const context = canvas.getContext("2d");
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, cssWidth, cssHeight);
      const styles = getComputedStyle(document.documentElement);
      const lineColor = styles.getPropertyValue("--line").trim();
      const textColor = styles.getPropertyValue("--muted").trim();
      context.strokeStyle = lineColor;
      context.lineWidth = 1;
      for (let index = 1; index < 4; index += 1) {
        const y = Math.round((cssHeight - 24) * index / 4) + 8;
        context.beginPath();
        context.moveTo(8, y);
        context.lineTo(cssWidth - 8, y);
        context.stroke();
      }
      if (!points || points.length < 2) {
        context.fillStyle = textColor;
        context.font = "12px system-ui";
        context.textAlign = "center";
        context.fillText(uiText("No completed backtest selected."), cssWidth / 2, cssHeight / 2);
        return;
      }
      const plot = (key, color) => {
        const values = points.map((point) => Number(point[key])).filter(Number.isFinite);
        const minimum = Math.min(...values);
        const maximum = Math.max(...values);
        const span = Math.max(Math.abs(maximum - minimum), Math.abs(maximum) * 1e-9, 1e-12);
        context.beginPath();
        context.strokeStyle = color;
        context.lineWidth = 1.7;
        points.forEach((point, index) => {
          const x = 8 + index / (points.length - 1) * (cssWidth - 16);
          const y = 8 + (maximum - Number(point[key])) / span * (cssHeight - 24);
          if (index === 0) context.moveTo(x, y);
          else context.lineTo(x, y);
        });
        context.stroke();
      };
      plot("equity", styles.getPropertyValue("--green").trim());
      plot("price", styles.getPropertyValue("--blue").trim());
    }

    function renderBacktestPoints(run) {
      const result = run?.result;
      const points = result?.points || [];
      const body = document.getElementById("backtest-points");
      body.innerHTML = "";
      text("backtest-point-count", points.length ? `${Math.min(60, points.length)} / ${points.length}` : "");
      if (!points.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">${escapeHtml(run?.error || uiText("No completed backtest selected."))}</td>`;
        body.appendChild(tr);
        drawBacktestChart([]);
        return;
      }
      for (const point of points.slice(-60)) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${formatTimestamp(point.timestamp_ms)}</td>
          <td class="num">${fmt.format(point.price)}</td>
          <td class="num">${money.format(point.equity)}</td>
          <td class="num">${backtestPercent(point.drawdown_pct)}</td>
          <td class="num">${compact.format(point.base)}</td>
          <td class="num">${money.format(point.cash)}</td>
        `;
        body.appendChild(tr);
      }
      drawBacktestChart(points);
    }

    function renderBacktestRuns(payload) {
      const body = document.getElementById("backtest-runs");
      body.innerHTML = "";
      const runs = payload?.runs || [];
      if (!runs.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="8">${escapeHtml(uiText("No historical backtests yet."))}</td>`;
        body.appendChild(tr);
        return;
      }
      for (const run of runs) {
        const metrics = run.metrics || {};
        const request = run.request || {};
        const tr = document.createElement("tr");
        if (run.id === selectedBacktestRunId) tr.className = "backtest-run-selected";
        tr.innerHTML = `
          <td title="${escapeHtml(run.id || "")}">${escapeHtml(shortId(run.id))}<br><span class="subtle">${escapeHtml(backtestEpoch(run.created_at))}</span></td>
          <td>${escapeHtml(run.strategy?.name || "--")}</td>
          <td>${escapeHtml(run.account?.exchange || "--")}<br><span class="subtle">${escapeHtml(run.account?.symbol || "--")}</span></td>
          <td>${escapeHtml(request.timeframe || "--")} · ${Number(request.history_bars || 0)} ${escapeHtml(uiText("bars"))}</td>
          <td class="num ${pnlClass(Number(metrics.return_pct || 0))}">${backtestPercent(metrics.return_pct)}</td>
          <td class="num">${backtestPercent(metrics.max_drawdown_pct)}</td>
          <td class="${backtestStatusClass(run.status)}">${escapeHtml(uiText(run.status || "--"))}</td>
          <td class="workspace-table-actions"></td>
        `;
        const action = tr.querySelector(".workspace-table-actions");
        const viewButton = document.createElement("button");
        viewButton.type = "button";
        viewButton.className = "ghost-button";
        viewButton.textContent = uiText("View");
        viewButton.addEventListener("click", () => loadUserBacktests({ runId: run.id, force: true }));
        action.appendChild(viewButton);
        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = "danger-button";
        deleteButton.textContent = uiText("Delete");
        deleteButton.disabled = USER_BACKTEST_ACTIVE_STATUSES.has(run.status);
        deleteButton.addEventListener("click", () => deleteUserBacktest(run.id, deleteButton));
        action.appendChild(deleteButton);
        body.appendChild(tr);
      }
    }

    function scheduleUserBacktestPoll(active) {
      if (userBacktestPollTimer) clearTimeout(userBacktestPollTimer);
      userBacktestPollTimer = null;
      if (!active || currentPage !== "quant" || !isSectionOpenFor("backtest-points")) return;
      userBacktestPollTimer = setTimeout(() => {
        loadUserBacktests({ runId: selectedBacktestRunId, force: true });
      }, 2000);
    }

    function renderUserBacktests(payload) {
      currentUserBacktests = payload || null;
      const selected = payload?.selected || null;
      if (selected?.id) selectedBacktestRunId = selected.id;
      const result = selected?.result || null;
      text("backtest-meta", `${Number(payload?.active_count || 0)} ${uiText("running backtests")} · ${(payload?.runs || []).length} ${uiText("saved runs")}`);
      text("backtest-return", result ? backtestPercent(result.return_pct) : "--");
      text("backtest-benchmark", result ? backtestPercent(result.benchmark_return_pct) : "--");
      text("backtest-excess", result ? backtestPercent(result.excess_return_pct) : "--");
      text("backtest-drawdown", result ? backtestPercent(result.max_drawdown_pct) : "--");
      text("backtest-sharpe", result?.sharpe_ratio == null ? "--" : Number(result.sharpe_ratio).toFixed(2));
      text("backtest-fees", result ? money.format(result.fee_quote || 0) : "--");
      text("backtest-turnover", result ? backtestPercent(result.turnover_pct, 1) : "--");
      text("backtest-trades", result ? String(result.trade_count || 0) : "--");
      const progress = Math.max(0, Math.min(100, Number(selected?.progress_pct || 0)));
      document.getElementById("backtest-progress-fill").style.width = `${progress}%`;
      const market = selected?.account
        ? `${selected.account.exchange || "--"} ${selected.account.symbol || "--"}`
        : "";
      const marketData = result?.market_data || {};
      const barSummary = Number(marketData.received_bars || 0) > 0
        ? ` · ${Number(marketData.received_bars)} ${uiText("bars")}`
        : "";
      const gapSummary = Number(marketData.gap_filled_bars || 0) > 0
        ? ` · ${Number(marketData.gap_filled_bars)} ${uiText("no-trade bars filled")}`
        : "";
      const progressText = selected
        ? `${uiText(selected.status || "--")} · ${market}${barSummary}${gapSummary}${selected.error ? ` · ${selected.error}` : ""}`
        : uiText("No backtest selected.");
      text("backtest-progress-text", progressText);
      const warnings = document.getElementById("backtest-warnings");
      warnings.innerHTML = (result?.warnings || [])
        .map((warning) => `<span>${escapeHtml(warning)}</span>`)
        .join("");
      renderBacktestRuns(payload);
      renderBacktestPoints(selected);
      applyMobileTableLabels(document.getElementById("backtest-section"));
      scheduleUserBacktestPoll(Number(payload?.active_count || 0) > 0);
    }

    async function loadUserBacktests({ runId = "", force = false } = {}) {
      if (userBacktestLoadBusy) return;
      if (!force && Date.now() - userBacktestLastLoadedAt < 3000) return;
      if (currentPage !== "quant" || !isSectionOpenFor("backtest-points")) return;
      userBacktestLoadBusy = true;
      try {
        const query = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
        const response = await fetch(`/api/user-backtests${query}`);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "backtest load failed");
        userBacktestLastLoadedAt = Date.now();
        renderUserBacktests(payload);
      } catch (error) {
        text("backtest-meta", `${uiText("Load failed")}: ${error.message || error}`);
        scheduleUserBacktestPoll(false);
      } finally {
        userBacktestLoadBusy = false;
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

    function workspaceProjectLabel(project) {
      const owner = lastState?.auth?.role === "admin" ? ` · ${project.owner_email}` : "";
      return `${project.name || project.symbol || project.id} · ${project.symbol || "--"}${owner}`;
    }

    function workspaceExchange(exchangeId) {
      return (currentUserWorkspace?.exchange_catalog || []).find(
        (row) => row.id === exchangeId
      ) || null;
    }

    function focusWorkspaceControl(id) {
      const control = document.getElementById(id);
      if (!control) return;
      control.scrollIntoView({ behavior: "smooth", block: "center" });
      window.setTimeout(() => control.focus(), 250);
    }

    function continueUserProjectSetup(project) {
      const action = project?.readiness?.next_action || {};
      const actionCode = action.code || "";
      if (["wait_for_project_approval", "contact_administrator"].includes(actionCode)) {
        fillUserProjectForm(project);
        focusWorkspaceControl("user-project-name");
        return;
      }

      const account = (currentUserWorkspace?.accounts || []).find(
        (row) => row.id === action.account_id
      );
      if (account) {
        fillUserExchangeAccountForm(account);
        const focusByAction = {
          confirm_withdrawal_disabled: "user-exchange-no-withdraw",
          save_credentials: "user-exchange-api-key",
          select_symbol: "user-exchange-load-markets",
          fix_connection: "user-exchange-api-key",
          test_connection: "user-exchange-test",
          enable_account: "user-exchange-enabled",
        };
        focusWorkspaceControl(focusByAction[actionCode] || "user-exchange-label");
        return;
      }

      if (actionCode === "add_exchange_account") {
        resetUserExchangeAccountForm();
        setFieldValue("user-exchange-project", project.id);
        syncUserExchangeMarketTypes("", "", project.symbol || "");
        focusWorkspaceControl("user-exchange-label");
        return;
      }

      const strategy = (currentUserWorkspace?.strategies || []).find(
        (row) => row.id === action.strategy_id
      );
      if (strategy) {
        openUserStrategyForm(strategy);
        focusWorkspaceControl(
          actionCode === "enable_strategy"
            ? "user-strategy-enabled"
            : "user-strategy-name"
        );
        return;
      }
      if (actionCode === "create_strategy") {
        openUserStrategyForm(null, project.id);
        focusWorkspaceControl("user-strategy-name");
        return;
      }

      fillUserProjectForm(project);
      focusWorkspaceControl("user-project-name");
    }

    function renderUserSetupReadiness(workspace) {
      const container = document.getElementById("user-setup-readiness");
      if (!container) return;
      const signature = JSON.stringify({
        status: workspace?.status || "",
        error: workspace?.error || "",
        projects: (workspace?.projects || []).map((project) => ({
          id: project.id,
          name: project.name,
          symbol: project.symbol,
          status: project.status,
          readiness: project.readiness,
        })),
      });
      if (signature === userSetupReadinessSignature) return;
      userSetupReadinessSignature = signature;
      container.replaceChildren();
      if (["user_account_required", "error"].includes(workspace?.status)) {
        const row = document.createElement("div");
        row.className = "workspace-readiness-row workspace-readiness-empty";
        const detail = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = uiText(
          workspace?.status === "error"
            ? "Project setup is temporarily unavailable"
            : "A registered user account is required"
        );
        const note = document.createElement("span");
        note.className = "subtle";
        note.textContent = workspace?.error || uiText("Log in with your username to manage projects and exchange accounts.");
        detail.append(title, note);
        row.appendChild(detail);
        container.appendChild(row);
        return;
      }
      const projects = workspace?.projects || [];
      if (projects.length === 0) {
        const row = document.createElement("div");
        row.className = "workspace-readiness-row workspace-readiness-empty";
        const detail = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = uiText("Create your first trading project");
        const note = document.createElement("span");
        note.className = "subtle";
        note.textContent = uiText("Choose an asset and quote currency to begin.");
        detail.append(title, note);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "control-button";
        button.textContent = uiText("Create Project");
        button.addEventListener("click", () => {
          resetUserProjectForm();
          focusWorkspaceControl("user-project-name");
        });
        row.append(detail, button);
        container.appendChild(row);
        return;
      }

      for (const project of projects) {
        const readiness = project.readiness || {};
        const completed = Number(readiness.completed_steps || 0);
        const total = Number(readiness.total_steps || 0);
        const progress = Math.max(0, Math.min(100, Number(readiness.progress_pct || 0)));
        const nextAction = readiness.next_action || {};
        const row = document.createElement("div");
        row.className = `workspace-readiness-row ${readiness.ready ? "workspace-ready" : "workspace-attention"}`;

        const identity = document.createElement("div");
        identity.className = "workspace-readiness-identity";
        const name = document.createElement("strong");
        name.textContent = project.name || project.symbol || project.id;
        const pair = document.createElement("span");
        pair.className = "subtle";
        pair.textContent = `${project.symbol || "--"} · ${uiText(project.status || "--")}`;
        identity.append(name, pair);

        const progressBlock = document.createElement("div");
        progressBlock.className = "workspace-readiness-progress-block";
        const progressLabel = document.createElement("span");
        progressLabel.textContent = `${completed}/${total} ${uiText("setup steps")}`;
        const progressTrack = document.createElement("div");
        progressTrack.className = "workspace-readiness-progress";
        progressTrack.setAttribute("role", "progressbar");
        progressTrack.setAttribute("aria-valuemin", "0");
        progressTrack.setAttribute("aria-valuemax", "100");
        progressTrack.setAttribute("aria-valuenow", String(progress));
        const progressValue = document.createElement("span");
        progressValue.style.width = `${progress}%`;
        progressTrack.appendChild(progressValue);
        progressBlock.append(progressLabel, progressTrack);

        const next = document.createElement("div");
        next.className = "workspace-readiness-next";
        const nextLabel = document.createElement("span");
        nextLabel.className = "subtle";
        nextLabel.textContent = uiText("Next step");
        const nextValue = document.createElement("strong");
        nextValue.textContent = uiText(nextAction.label || "Review project setup");
        next.append(nextLabel, nextValue);
        next.title = (readiness.steps || [])
          .filter((step) => !step.complete)
          .map((step) => uiText(step.label || step.id))
          .join(" · ");

        const actionControl = document.createElement(readiness.ready ? "span" : "button");
        actionControl.className = readiness.ready
          ? "workspace-ready-label"
          : "ghost-button workspace-continue-button";
        actionControl.textContent = uiText(readiness.ready ? "Ready" : "Continue Setup");
        if (!readiness.ready) {
          actionControl.type = "button";
          actionControl.addEventListener("click", () => continueUserProjectSetup(project));
        }
        row.append(identity, progressBlock, next, actionControl);
        container.appendChild(row);
      }
    }

    function resetUserProjectForm() {
      selectedUserProjectId = "";
      userProjectFormDirty = false;
      setFieldValue("user-project-id", "");
      setFieldValue("user-project-name", "");
      setFieldValue("user-project-asset", "");
      setFieldValue("user-project-quote", "");
    }

    function fillUserProjectForm(project) {
      selectedUserProjectId = project?.id || "";
      userProjectFormDirty = false;
      setFieldValue("user-project-id", project?.id || "");
      setFieldValue("user-project-name", project?.name || "");
      setFieldValue("user-project-asset", project?.asset || "");
      setFieldValue("user-project-quote", project?.quote_currency || "");
      document.getElementById("user-project-name")?.focus();
    }

    function renderUserProjectForm(workspace) {
      if (userProjectFormDirty || userProjectFormBusy) return;
      const selected = (workspace?.projects || []).find(
        (project) => project.id === selectedUserProjectId
      );
      if (selected) {
        setFieldValue("user-project-id", selected.id);
        setFieldValue("user-project-name", selected.name);
        setFieldValue("user-project-asset", selected.asset);
        setFieldValue("user-project-quote", selected.quote_currency);
      } else {
        selectedUserProjectId = "";
        setFieldValue("user-project-id", "");
        setFieldValue("user-project-name", "");
        setFieldValue("user-project-asset", "");
        setFieldValue("user-project-quote", "");
      }
    }

    function renderUserProjects(workspace) {
      const body = document.getElementById("user-projects");
      if (!body) return;
      body.innerHTML = "";
      const ownProjects = workspace?.projects || [];
      const projects = [
        ...ownProjects,
        ...(workspace?.platform_projects || []).filter(
          (project) => !ownProjects.some((own) => own.id === project.id)
        ),
      ];
      if (projects.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="5">${escapeHtml(uiText("No projects yet. Create one before adding an exchange account."))}</td>`;
        body.appendChild(tr);
        return;
      }
      const isAdmin = lastState?.auth?.role === "admin";
      for (const project of projects) {
        const platformOnly = Boolean(project.platform_only);
        const tr = document.createElement("tr");
        tr.dataset.workspaceProjectId = project.id || "";
        const statusClass = project.status === "active" ? "ok" : project.status === "pending" ? "risk-blocked" : "subtle";
        const setup = project.readiness || {};
        const setupText = platformOnly
          ? uiText("Platform approval only")
          : `${setup.completed_steps || 0}/${setup.total_steps || 0} ${uiText("setup steps")}`;
        tr.innerHTML = `
          <td title="${escapeHtml(project.id || "")}">${escapeHtml(project.name || project.id)}</td>
          <td>${escapeHtml(project.owner_email || "--")}</td>
          <td>${escapeHtml(project.symbol || `${project.asset}/${project.quote_currency}`)}</td>
          <td class="${statusClass}">${escapeHtml(uiText(project.status || "--"))}<br><span class="subtle">${escapeHtml(setupText)}</span></td>
          <td><div class="workspace-table-actions"></div></td>
        `;
        const actions = tr.querySelector(".workspace-table-actions");
        if (!platformOnly) {
          const editButton = document.createElement("button");
          editButton.className = "control-button";
          editButton.type = "button";
          editButton.textContent = "Edit";
          editButton.addEventListener("click", () => fillUserProjectForm(project));
          actions.appendChild(editButton);
        }
        if (isAdmin && project.status !== "active") {
          const approveButton = document.createElement("button");
          approveButton.className = "control-button";
          approveButton.type = "button";
          approveButton.textContent = "Approve";
          approveButton.addEventListener("click", () => approveUserProject(project, approveButton));
          actions.appendChild(approveButton);
        }
        if (project.status === "active") {
          const disableButton = document.createElement("button");
          disableButton.className = "ghost-button danger";
          disableButton.type = "button";
          disableButton.textContent = "Disable";
          disableButton.addEventListener("click", () => disableUserProject(project, disableButton));
          actions.appendChild(disableButton);
        }
        if (!platformOnly) {
          const deleteButton = document.createElement("button");
          deleteButton.className = "danger-button";
          deleteButton.type = "button";
          deleteButton.textContent = "Delete";
          deleteButton.addEventListener("click", () => deleteUserProject(project, deleteButton));
          actions.appendChild(deleteButton);
        }
        body.appendChild(tr);
      }
    }

    function workspaceProject(projectId) {
      return (currentUserWorkspace?.projects || []).find(
        (project) => project.id === projectId
      ) || null;
    }

    function workspaceSelectedAccount() {
      const accountId = document.getElementById("user-exchange-account-id")?.value || "";
      return (currentUserWorkspace?.accounts || []).find(
        (account) => account.id === accountId
      ) || null;
    }

    function workspaceMarketCacheKey({ project, exchange, marketType, apiVariant }) {
      return [project?.asset || "", exchange || "", marketType || "", apiVariant || ""].join(":");
    }

    function workspaceConnectionFresh(account) {
      if (typeof account?.connection_fresh === "boolean") {
        return account.connection_fresh;
      }
      const checkedAt = Number(account?.connection_checked_at || 0);
      const ageSeconds = Date.now() / 1000 - checkedAt;
      return Boolean(
        account?.connection_status === "healthy"
        && checkedAt > 0
        && ageSeconds >= 0
        && ageSeconds <= 86400
      );
    }

    function resetUserExchangeAccountForm() {
      selectedUserExchangeAccountId = "";
      userExchangeAccountFormDirty = false;
      setFieldValue("user-exchange-account-id", "");
      setFieldValue("user-exchange-label", "");
      setFieldValue("user-exchange-api-key", "");
      setFieldValue("user-exchange-secret", "");
      setFieldValue("user-exchange-passphrase", "");
      setCheckedValue("user-exchange-enabled", false);
      setCheckedValue("user-exchange-no-withdraw", false);
      setCheckedValue("user-exchange-trade-permission", false);
      const projects = currentUserWorkspace?.projects || [];
      const defaultProject = projects.find((project) => project.status === "active") || projects[0];
      const defaultExchange = currentUserWorkspace?.exchange_catalog?.[0];
      setFieldValue("user-exchange-project", defaultProject?.id || "");
      setFieldValue("user-exchange-id", defaultExchange?.id || "");
      syncUserExchangeMarketTypes("", "", defaultProject?.symbol || "");
    }

    function fillUserExchangeAccountForm(account) {
      selectedUserExchangeAccountId = account?.id || "";
      userExchangeAccountFormDirty = false;
      setFieldValue("user-exchange-account-id", account?.id || "");
      setFieldValue("user-exchange-project", account?.project_id || "");
      setFieldValue("user-exchange-label", account?.label || "");
      setFieldValue("user-exchange-id", account?.exchange || "");
      setFieldValue("user-exchange-api-key", "");
      setFieldValue("user-exchange-secret", "");
      setFieldValue("user-exchange-passphrase", "");
      setCheckedValue("user-exchange-enabled", account?.enabled);
      setCheckedValue(
        "user-exchange-no-withdraw",
        account?.withdrawal_disabled_confirmed
      );
      setCheckedValue(
        "user-exchange-trade-permission",
        account?.trade_permission_confirmed
      );
      syncUserExchangeMarketTypes(
        account?.market_type || "spot",
        account?.api_variant || "",
        account?.symbol || ""
      );
      document.getElementById("user-exchange-label")?.focus();
    }

    function syncUserExchangeMarketTypes(
      preferredMarketType = "",
      preferredVariant = "",
      preferredSymbol = ""
    ) {
      const exchangeId = document.getElementById("user-exchange-id")?.value || "";
      const exchange = workspaceExchange(exchangeId);
      const currentMarketType = preferredMarketType
        || document.getElementById("user-exchange-market-type")?.value
        || "spot";
      const marketRows = (exchange?.market_types || []).map((marketType) => ({
        value: marketType,
        label: marketType === "swap" ? "Perpetual Swap" : marketType === "future" ? "Futures" : "Spot",
      }));
      const selectedMarketType = marketRows.some((row) => row.value === currentMarketType)
        ? currentMarketType
        : marketRows[0]?.value || "";
      setSelectOptions(
        "user-exchange-market-type",
        marketRows,
        selectedMarketType,
        "Select market"
      );

      const variants = (exchange?.variants || []).map((variant) => ({
        value: variant.id,
        label: variant.label || variant.id,
      }));
      const currentVariant = preferredVariant
        || document.getElementById("user-exchange-api-variant")?.value
        || exchange?.default_variant
        || variants[0]?.value
        || "default";
      const selectedVariant = variants.some((row) => row.value === currentVariant)
        ? currentVariant
        : exchange?.default_variant || variants[0]?.value || "default";
      setSelectOptions(
        "user-exchange-api-variant",
        variants,
        selectedVariant,
        "Select API region"
      );
      const variantField = document.getElementById("user-exchange-variant-field");
      if (variantField) variantField.hidden = variants.length <= 1;

      const projectId = document.getElementById("user-exchange-project")?.value || "";
      const project = workspaceProject(projectId);
      const currentSymbol = preferredSymbol
        || document.getElementById("user-exchange-symbol")?.value
        || project?.symbol
        || "";
      const cacheKey = workspaceMarketCacheKey({
        project,
        exchange: exchangeId,
        marketType: selectedMarketType,
        apiVariant: selectedVariant,
      });
      const discovered = discoveredUserMarkets.get(cacheKey) || [];
      const symbolRows = discovered.map((market) => ({
        value: market.symbol,
        label: market.cost_min
          ? `${market.symbol} · min ${fmt.format(market.cost_min)} ${market.quote}`
          : market.symbol,
        title: `${market.type || selectedMarketType} · ${market.active === false ? "inactive" : "active"}`,
      }));
      if (project?.symbol && !symbolRows.some((row) => row.value === project.symbol)) {
        symbolRows.unshift({ value: project.symbol, label: project.symbol });
      }
      setSelectOptions(
        "user-exchange-symbol",
        symbolRows,
        currentSymbol,
        "Load or select pair"
      );

      const needsPassphrase = (exchange?.required_credentials || []).includes("passphrase");
      const passphraseField = document.getElementById("user-exchange-passphrase-field");
      if (passphraseField) passphraseField.hidden = !needsPassphrase;

      const selectedAccount = workspaceSelectedAccount();
      const sameConnection = Boolean(
        selectedAccount
        && selectedAccount.project_id === projectId
        && selectedAccount.exchange === exchangeId
        && selectedAccount.market_type === selectedMarketType
        && selectedAccount.api_variant === selectedVariant
        && selectedAccount.symbol === currentSymbol
      );
      const connectionReady = sameConnection && workspaceConnectionFresh(selectedAccount);
      const projectReady = project?.status === "active";
      const enabled = document.getElementById("user-exchange-enabled");
      if (enabled) {
        enabled.disabled = !projectReady || (!connectionReady && !selectedAccount?.enabled);
        if (!connectionReady) enabled.checked = false;
        enabled.title = !projectReady
          ? uiText("The project must be approved before this account can be enabled.")
          : !connectionReady
            ? uiText("Run a successful connection test before enabling this account.")
            : "";
      }
      const testButton = document.getElementById("user-exchange-test");
      if (testButton) {
        const credentialsConfigured = Boolean(selectedAccount?.credentials?.configured);
        testButton.disabled = !sameConnection || !credentialsConfigured || !currentSymbol;
        testButton.title = testButton.disabled
          ? uiText("Save the account and credentials before testing the connection.")
          : uiText("This test reads account data and never places or cancels orders.");
      }
    }

    function renderUserExchangeAccountForm(workspace) {
      if (userExchangeAccountFormDirty || userExchangeAccountFormBusy) return;
      const projectRows = workspace?.projects || [];
      const projects = projectRows.map((project) => ({
        value: project.id,
        label: workspaceProjectLabel(project),
        title: `${project.status} · ${project.owner_email}`,
      }));
      const exchanges = (workspace?.exchange_catalog || []).map((exchange) => ({
        value: exchange.id,
        label: exchange.label || exchange.id,
        title: (exchange.market_types || []).join(", "),
      }));
      const selected = (workspace?.accounts || []).find(
        (account) => account.id === selectedUserExchangeAccountId
      );
      const projectValue = selected?.project_id
        || document.getElementById("user-exchange-project")?.value
        || projects.find((row) => projectRows.find((project) => project.id === row.value)?.status === "active")?.value
        || projects[0]?.value
        || "";
      const exchangeValue = selected?.exchange
        || document.getElementById("user-exchange-id")?.value
        || exchanges[0]?.value
        || "";
      setSelectOptions("user-exchange-project", projects, projectValue, "Select project");
      setSelectOptions("user-exchange-id", exchanges, exchangeValue, "Select exchange");
      if (selected) {
        setFieldValue("user-exchange-account-id", selected.id);
        setFieldValue("user-exchange-label", selected.label);
        setCheckedValue("user-exchange-enabled", selected.enabled);
        setCheckedValue(
          "user-exchange-no-withdraw",
          selected.withdrawal_disabled_confirmed
        );
        setCheckedValue(
          "user-exchange-trade-permission",
          selected.trade_permission_confirmed
        );
      } else {
        selectedUserExchangeAccountId = "";
        setFieldValue("user-exchange-account-id", "");
        setCheckedValue("user-exchange-enabled", false);
        setCheckedValue("user-exchange-no-withdraw", false);
        setCheckedValue("user-exchange-trade-permission", false);
      }
      setFieldValue("user-exchange-api-key", "");
      setFieldValue("user-exchange-secret", "");
      setFieldValue("user-exchange-passphrase", "");
      syncUserExchangeMarketTypes(
        selected?.market_type || "",
        selected?.api_variant || "",
        selected?.symbol || ""
      );
    }

    function renderUserExchangeAccounts(workspace) {
      const body = document.getElementById("user-exchange-accounts");
      if (!body) return;
      body.innerHTML = "";
      const accounts = workspace?.accounts || [];
      const projectMap = new Map(
        (workspace?.projects || []).map((project) => [project.id, project])
      );
      if (accounts.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">${escapeHtml(uiText("No exchange accounts yet."))}</td>`;
        body.appendChild(tr);
        return;
      }
      for (const account of accounts) {
        const project = projectMap.get(account.project_id);
        const credentials = account.credentials || {};
        const readiness = account.readiness || {};
        const rotationText = credentials.rotation_required
          ? " · rotation due"
          : credentials.rotation_remaining_seconds != null
            ? ` · rotate in ${formatDurationSeconds(credentials.rotation_remaining_seconds)}`
            : "";
        const credentialText = credentials.configured
          ? `Encrypted / configured${rotationText}`
          : "Missing";
        const connectionFresh = workspaceConnectionFresh(account);
        const accountStatus = account.enabled
          ? "Enabled"
          : project?.status !== "active"
            ? "Waiting for project"
            : account.connection_status === "error"
              ? "Connection error"
              : connectionFresh
                ? "Ready to enable"
                : "Needs connection test";
        const connectionText = account.connection_checked_at
          ? `${account.connection_status || "unverified"} · ${formatAge(account.connection_checked_at)}`
          : account.connection_status || "unverified";
        const connectionRemaining = readiness.connection_remaining_seconds;
        const readinessText = `${readiness.completed_steps || 0}/${readiness.total_steps || 0} ${uiText("setup steps")}`;
        const expiryText = connectionRemaining == null
          ? ""
          : ` · ${uiText("test valid for")} ${formatDurationSeconds(connectionRemaining)}`;
        const connectionClass = account.connection_status === "healthy"
          ? "ok"
          : account.connection_status === "error"
            ? "missing"
            : "subtle";
        const variantText = account.api_variant && !["default", "global"].includes(account.api_variant)
          ? ` · ${account.api_variant}`
          : "";
        const tr = document.createElement("tr");
        tr.dataset.workspaceAccountId = account.id || "";
        tr.innerHTML = `
          <td title="${escapeHtml(account.id || "")}">${escapeHtml(account.label || account.id)}<br><span class="subtle">${escapeHtml(account.owner_email || "--")}</span></td>
          <td>${escapeHtml(project?.name || account.project_id || "--")}<br><span class="subtle">${escapeHtml(account.symbol || project?.symbol || "--")}</span></td>
          <td>${escapeHtml(workspaceExchange(account.exchange)?.label || account.exchange)}<br><span class="subtle">${escapeHtml(`${account.market_type || "spot"}${variantText}`)}</span></td>
          <td class="${credentials.configured ? "ok" : "missing"}">${escapeHtml(credentialText)}</td>
          <td class="${connectionClass}" title="${escapeHtml((readiness.blockers || []).join(" · ") || account.connection_error || "")}">${escapeHtml(uiText(accountStatus))}<br><span class="subtle">${escapeHtml(connectionText + expiryText)}</span><br><span class="subtle">${escapeHtml(readinessText)}</span></td>
          <td><div class="workspace-table-actions"></div></td>
        `;
        const actions = tr.querySelector(".workspace-table-actions");
        const editButton = document.createElement("button");
        editButton.className = "control-button";
        editButton.type = "button";
        editButton.textContent = "Edit";
        editButton.addEventListener("click", () => fillUserExchangeAccountForm(account));
        actions.appendChild(editButton);
        const testButton = document.createElement("button");
        testButton.className = "ghost-button workspace-account-test";
        testButton.type = "button";
        testButton.textContent = "Test";
        testButton.disabled = !credentials.configured || !account.symbol;
        testButton.title = testButton.disabled ? uiText("Save credentials and a trading pair first.") : "";
        testButton.addEventListener("click", () => testUserExchangeAccount(account, testButton));
        actions.appendChild(testButton);
        const deleteButton = document.createElement("button");
        deleteButton.className = "danger-button";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";
        deleteButton.addEventListener("click", () => deleteUserExchangeAccount(account, deleteButton));
        actions.appendChild(deleteButton);
        body.appendChild(tr);
      }
    }

    function setUserWorkspace(workspace) {
      currentUserWorkspace = workspace || null;
      if (lastState) lastState.user_workspace = workspace;
      if (pageStateCache.settings) pageStateCache.settings.user_workspace = workspace;
      renderUserWorkspace(workspace);
    }

    function setUserWorkspaceNotice(value, durationMs = 12000) {
      userWorkspaceNoticeText = String(value || "");
      userWorkspaceNoticeUntil = Date.now() + Math.max(1000, durationMs);
      text("user-workspace-notice", userWorkspaceNoticeText);
    }

    function renderUserRiskProfile(workspace) {
      if (userRiskProfileDirty || userRiskProfileBusy) return;
      const profile = workspace?.risk_profile || {};
      setCheckedValue("user-risk-trading-enabled", profile.trading_enabled !== false);
      setNumericField("user-risk-max-exposure", profile.max_total_exposure_quote || 0);
      setNumericField("user-risk-max-loss", profile.max_daily_loss_quote || 0);
      setNumericField("user-risk-max-orders", profile.max_open_orders || 0);
      setNumericField("user-risk-max-strategies", profile.max_active_strategies || 0);
    }

    async function applyUserRiskProfile(event) {
      event.preventDefault();
      if (userRiskProfileBusy) return;
      userRiskProfileBusy = true;
      const button = document.getElementById("user-risk-profile-save");
      button.disabled = true;
      try {
        await postUserWorkspace({
          action: "update_risk_profile",
          risk_profile: {
            trading_enabled: document.getElementById("user-risk-trading-enabled").checked,
            max_total_exposure_quote: numericValue("user-risk-max-exposure"),
            max_daily_loss_quote: numericValue("user-risk-max-loss"),
            max_open_orders: numericValue("user-risk-max-orders"),
            max_active_strategies: numericValue("user-risk-max-strategies"),
          },
        });
        userRiskProfileDirty = false;
        renderUserRiskProfile(currentUserWorkspace);
      } catch (error) {
        setUserWorkspaceNotice(`risk profile update failed: ${error.message || error}`);
      } finally {
        userRiskProfileBusy = false;
        button.disabled = false;
      }
    }

    function renderUserWorkspace(workspace) {
      currentUserWorkspace = workspace || null;
      const summary = workspace?.summary || {};
      const summaryParts = [
        `${summary.ready_project_count || 0}/${summary.project_count || 0} ${uiText("projects ready")}`,
        `${summary.ready_account_count || 0}/${summary.account_count || 0} ${uiText("accounts ready")}`,
        `${summary.ready_strategy_count || 0}/${summary.strategy_count || 0} ${uiText("paper ready")}`,
      ];
      if (!workspace?.vault_available) {
        summaryParts.push(uiText("credential vault unavailable"));
      }
      const statusText = workspace?.status === "user_account_required"
        ? uiText("registered account required")
        : workspace?.error || summaryParts.join(" · ");
      if (userWorkspaceNoticeUntil <= Date.now()) {
        userWorkspaceNoticeText = "";
        userWorkspaceNoticeUntil = 0;
      }
      text("user-workspace-meta", statusText);
      text("user-workspace-notice", userWorkspaceNoticeText);
      const formsDisabled = workspace?.status === "user_account_required" || workspace?.status === "error";
      document.querySelectorAll("#user-risk-profile-form input, #user-risk-profile-form button, #user-project-form input, #user-project-form button, #user-exchange-account-form input, #user-exchange-account-form textarea, #user-exchange-account-form select, #user-exchange-account-form button, #user-strategy-form input, #user-strategy-form select, #user-strategy-form button, #user-strategy-new").forEach((control) => {
        control.disabled = formsDisabled;
      });
      renderUserSetupReadiness(workspace);
      renderUserRiskProfile(workspace);
      renderUserProjectForm(workspace);
      renderUserProjects(workspace);
      renderUserExchangeAccountForm(workspace);
      renderUserExchangeAccounts(workspace);
      renderUserStrategies(workspace);
      if (!formsDisabled) syncUserExchangeMarketTypes();
    }

    async function postUserWorkspace(payload) {
      const res = await fetch("/api/user-workspace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(result.error || `workspace update failed (${res.status})`);
      setUserWorkspace(result.workspace);
      return result;
    }

    async function loadUserExchangeMarkets() {
      if (userMarketDiscoveryBusy) return;
      const button = document.getElementById("user-exchange-load-markets");
      const projectId = document.getElementById("user-exchange-project").value;
      const exchange = document.getElementById("user-exchange-id").value;
      const marketType = document.getElementById("user-exchange-market-type").value;
      const apiVariant = document.getElementById("user-exchange-api-variant").value;
      const project = workspaceProject(projectId);
      if (!project || !exchange || !marketType) {
        setUserWorkspaceNotice(uiText("Select a project and exchange first."));
        return;
      }
      userMarketDiscoveryBusy = true;
      button.disabled = true;
      try {
        const result = await postUserWorkspace({
          action: "discover_markets",
          project_id: projectId,
          exchange,
          market_type: marketType,
          api_variant: apiVariant,
        });
        const cacheKey = workspaceMarketCacheKey({
          project,
          exchange,
          marketType,
          apiVariant,
        });
        discoveredUserMarkets.set(cacheKey, result.markets || []);
        syncUserExchangeMarketTypes(marketType, apiVariant);
        setUserWorkspaceNotice(
          `${(result.markets || []).length} ${uiText("trading pairs loaded")}${result.cached ? ` · ${uiText("cached")}` : ""}`
        );
      } catch (error) {
        setUserWorkspaceNotice(`market discovery failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
        userMarketDiscoveryBusy = false;
      }
    }

    async function testUserExchangeAccount(account, button) {
      button.disabled = true;
      try {
        const result = await postUserWorkspace({
          action: "test_account",
          account_id: account.id,
        });
        const check = result.connection_test || {};
        if (check.status !== "healthy") {
          throw new Error(check.error || "connection test failed");
        }
        const balances = (check.balances || [])
          .map((row) => `${row.currency} ${fmt.format(row.total ?? row.free ?? 0)}`)
          .join(" · ");
        setUserWorkspaceNotice(
          `${uiText("Connection healthy")} · ${account.exchange} ${account.symbol} · ${Number(check.latency_ms || 0).toFixed(0)}ms · ${check.open_order_count || 0} ${uiText("open orders")}${balances ? ` · ${balances}` : ""}`
        );
      } catch (error) {
        setUserWorkspaceNotice(`connection test failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function testSelectedUserExchangeAccount(event) {
      const account = workspaceSelectedAccount();
      if (!account) {
        setUserWorkspaceNotice(uiText("Save an exchange account before testing it."));
        return;
      }
      await testUserExchangeAccount(account, event.currentTarget);
    }

    async function applyUserProject(event) {
      event.preventDefault();
      if (userProjectFormBusy) return;
      userProjectFormBusy = true;
      const button = document.getElementById("user-project-save");
      button.disabled = true;
      const id = document.getElementById("user-project-id").value.trim();
      const project = {
        name: document.getElementById("user-project-name").value.trim(),
        asset: document.getElementById("user-project-asset").value.trim().toUpperCase(),
        quote_currency: document.getElementById("user-project-quote").value.trim().toUpperCase(),
      };
      if (id) project.id = id;
      try {
        await postUserWorkspace({ action: "upsert_project", project });
        resetUserProjectForm();
        renderUserWorkspace(currentUserWorkspace);
      } catch (error) {
        setUserWorkspaceNotice(`project update failed: ${error.message || error}`);
      } finally {
        userProjectFormBusy = false;
        button.disabled = false;
      }
    }

    async function approveUserProject(project, button) {
      button.disabled = true;
      try {
        await postUserWorkspace({ action: "approve_project", project_id: project.id });
      } catch (error) {
        setUserWorkspaceNotice(`approval failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function disableUserProject(project, button) {
      if (!dangerConfirm("Disable this project and all of its exchange accounts?")) return;
      button.disabled = true;
      try {
        await postUserWorkspace({ action: "disable_project", project_id: project.id });
      } catch (error) {
        setUserWorkspaceNotice(`disable failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function deleteUserProject(project, button) {
      if (!dangerConfirm("Delete this project?", "Delete its exchange accounts first.")) return;
      button.disabled = true;
      try {
        await postUserWorkspace({ action: "delete_project", project_id: project.id });
        if (selectedUserProjectId === project.id) resetUserProjectForm();
      } catch (error) {
        setUserWorkspaceNotice(`delete failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function applyUserExchangeAccount(event) {
      event.preventDefault();
      if (userExchangeAccountFormBusy) return;
      userExchangeAccountFormBusy = true;
      const button = document.getElementById("user-exchange-save");
      button.disabled = true;
      const id = document.getElementById("user-exchange-account-id").value.trim();
      const credentials = {};
      const apiKey = document.getElementById("user-exchange-api-key").value.trim();
      const secret = document.getElementById("user-exchange-secret").value.trim();
      const passphrase = document.getElementById("user-exchange-passphrase").value.trim();
      if (apiKey) credentials.api_key = apiKey;
      if (secret) credentials.secret = secret;
      if (passphrase) credentials.passphrase = passphrase;
      const account = {
        project_id: document.getElementById("user-exchange-project").value,
        label: document.getElementById("user-exchange-label").value.trim(),
        exchange: document.getElementById("user-exchange-id").value,
        market_type: document.getElementById("user-exchange-market-type").value,
        api_variant: document.getElementById("user-exchange-api-variant").value,
        symbol: document.getElementById("user-exchange-symbol").value,
        enabled: document.getElementById("user-exchange-enabled").checked,
        withdrawal_disabled_confirmed: document.getElementById("user-exchange-no-withdraw").checked,
        trade_permission_confirmed: document.getElementById("user-exchange-trade-permission").checked,
      };
      if (id) account.id = id;
      if (Object.keys(credentials).length) account.credentials = credentials;
      try {
        await postUserWorkspace({ action: "upsert_account", account });
        resetUserExchangeAccountForm();
        renderUserWorkspace(currentUserWorkspace);
      } catch (error) {
        setUserWorkspaceNotice(`account update failed: ${error.message || error}`);
      } finally {
        document.getElementById("user-exchange-api-key").value = "";
        document.getElementById("user-exchange-secret").value = "";
        document.getElementById("user-exchange-passphrase").value = "";
        userExchangeAccountFormBusy = false;
        button.disabled = false;
      }
    }

    async function deleteUserExchangeAccount(account, button) {
      if (!dangerConfirm("Delete this exchange account and its encrypted API credentials?")) return;
      button.disabled = true;
      try {
        await postUserWorkspace({ action: "delete_account", account_id: account.id });
        if (selectedUserExchangeAccountId === account.id) resetUserExchangeAccountForm();
      } catch (error) {
        setUserWorkspaceNotice(`delete failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    function workspaceStrategyDefinition(strategyType) {
      return (currentUserWorkspace?.strategy_catalog || []).find(
        (row) => row.id === strategyType
      ) || null;
    }

    function workspaceStrategyProjectOptions(selectedProjectId = "") {
      const projects = (currentUserWorkspace?.projects || []).map((project) => ({
        value: project.id,
        label: workspaceProjectLabel(project),
        title: `${project.status} · ${project.owner_email}`,
      }));
      const selected = selectedProjectId
        || projects.find((row) => workspaceProject(row.value)?.status === "active")?.value
        || projects[0]?.value
        || "";
      setSelectOptions("user-strategy-project", projects, selected, "Select project");
      return selected;
    }

    function workspaceStrategyTypeOptions(selectedType = "") {
      const rows = (currentUserWorkspace?.strategy_catalog || []).map((definition) => ({
        value: definition.id,
        label: uiText(definition.label || definition.id),
        title: `${definition.min_accounts}-${definition.max_accounts} accounts · paper`,
      }));
      const selected = selectedType || rows[0]?.value || "market_maker";
      setSelectOptions("user-strategy-type", rows, selected, "Select strategy");
      return selected;
    }

    function selectedUserStrategyAccountIds() {
      return Array.from(
        document.querySelectorAll("#user-strategy-accounts input[type='checkbox']:checked")
      ).map((input) => input.value);
    }

    function renderUserStrategyAccountOptions(selectedAccountIds = []) {
      const container = document.getElementById("user-strategy-accounts");
      const projectId = document.getElementById("user-strategy-project")?.value || "";
      const strategyType = document.getElementById("user-strategy-type")?.value || "";
      const definition = workspaceStrategyDefinition(strategyType);
      const selected = new Set(selectedAccountIds);
      const accounts = (currentUserWorkspace?.accounts || []).filter(
        (account) => account.project_id === projectId
      );
      container.innerHTML = "";
      if (accounts.length === 0) {
        const empty = document.createElement("span");
        empty.className = "subtle";
        empty.textContent = uiText("No exchange accounts for this project.");
        container.appendChild(empty);
      } else {
        for (const account of accounts) {
          const label = document.createElement("label");
          label.className = "account-option";
          const input = document.createElement("input");
          input.type = "checkbox";
          input.value = account.id;
          input.checked = selected.has(account.id);
          input.disabled = false;
          input.title = account.enabled && account.connection_fresh
            ? uiText("Account ready")
            : uiText("Account must be enabled with a fresh connection test.");
          const name = document.createElement("span");
          name.textContent = `${account.label} · ${account.exchange} ${account.symbol}`;
          label.append(input, name);
          container.appendChild(label);
        }
      }
      const minAccounts = Number(definition?.min_accounts || 0);
      const maxAccounts = Number(definition?.max_accounts || 0);
      text(
        "user-strategy-account-hint",
        minAccounts === maxAccounts
          ? `${uiText("Required accounts")}: ${minAccounts}`
          : `${uiText("Required accounts")}: ${minAccounts}-${maxAccounts}`
      );
    }

    function setUserStrategyParameterValues(strategyType, parameters = {}) {
      const definition = workspaceStrategyDefinition(strategyType);
      const values = { ...(definition?.default_parameters || {}), ...parameters };
      if (strategyType === "market_maker") {
        setFieldValue("user-strategy-mm-levels", values.levels);
        setFieldValue("user-strategy-mm-band", values.price_band_pct);
        setFieldValue("user-strategy-mm-quote", values.quote_per_level);
        setFieldValue("user-strategy-mm-refresh", values.refresh_seconds);
        setCheckedValue("user-strategy-mm-post-only", values.post_only);
      } else if (["auto_buy_sell", "dca"].includes(strategyType)) {
        setFieldValue("user-strategy-side", values.side);
        setFieldValue("user-strategy-total-quote", values.total_quote);
        setFieldValue("user-strategy-order-quote", values.quote_per_order);
        setFieldValue("user-strategy-interval", values.interval_seconds);
        if (strategyType === "auto_buy_sell") {
          setFieldValue("user-strategy-start-price", values.start_price);
          setFieldValue("user-strategy-stop-price", values.stop_price);
        } else {
          setFieldValue("user-strategy-trigger-price", values.trigger_price);
          setFieldValue("user-strategy-take-profit", values.take_profit_pct);
        }
      } else if (strategyType === "spot_grid") {
        setFieldValue("user-strategy-grid-lower", values.lower_price);
        setFieldValue("user-strategy-grid-upper", values.upper_price);
        setFieldValue("user-strategy-grid-count", values.grid_count);
        setFieldValue("user-strategy-grid-quote", values.quote_per_grid);
        setFieldValue("user-strategy-grid-spacing", values.spacing);
        setFieldValue("user-strategy-grid-refresh", values.refresh_seconds);
      } else if (strategyType === "spot_spread") {
        setFieldValue("user-strategy-profit-bps", values.min_profit_bps);
        setFieldValue("user-strategy-cycle-quote", values.max_cycle_quote);
        setFieldValue("user-strategy-scan-seconds", values.scan_interval_seconds);
      }
    }

    function setUserStrategyRiskValues(strategyType, risk = {}) {
      const defaults = workspaceStrategyDefinition(strategyType)?.default_risk || {};
      const values = { ...defaults, ...risk };
      setFieldValue("user-strategy-risk-order", values.max_order_quote);
      setFieldValue("user-strategy-risk-total", values.max_total_quote);
      setFieldValue("user-strategy-risk-loss", values.max_daily_loss_quote);
      setFieldValue("user-strategy-risk-orders", values.max_open_orders);
      setFieldValue("user-strategy-risk-slippage", values.max_slippage_bps);
      setFieldValue("user-strategy-risk-book-age", values.max_order_book_age_seconds);
      setFieldValue("user-strategy-risk-fee", values.paper_fee_bps);
    }

    function syncUserStrategyTypeFields({ applyDefaults = false } = {}) {
      const strategyType = document.getElementById("user-strategy-type")?.value || "";
      document.querySelectorAll("[data-user-strategy-types]").forEach((field) => {
        const supported = String(field.dataset.userStrategyTypes || "")
          .split(/\s+/)
          .filter(Boolean);
        field.hidden = !supported.includes(strategyType);
      });
      if (applyDefaults) {
        setUserStrategyParameterValues(strategyType);
        setUserStrategyRiskValues(strategyType);
      }
      renderUserStrategyAccountOptions(selectedUserStrategyAccountIds());
    }

    function openUserStrategyForm(strategy = null, preferredProjectId = "") {
      selectedUserStrategyId = strategy?.id || "";
      userStrategyFormDirty = false;
      const form = document.getElementById("user-strategy-form");
      form.hidden = false;
      setFieldValue("user-strategy-id", strategy?.id || "");
      const projectId = workspaceStrategyProjectOptions(
        strategy?.project_id || preferredProjectId
      );
      const strategyType = workspaceStrategyTypeOptions(strategy?.strategy_type || "");
      const definition = workspaceStrategyDefinition(strategyType);
      const project = workspaceProject(projectId);
      setFieldValue(
        "user-strategy-name",
        strategy?.name || `${project?.asset || ""} ${uiText(definition?.label || "Strategy")}`.trim()
      );
      setCheckedValue("user-strategy-enabled", strategy?.enabled || false);
      setUserStrategyParameterValues(strategyType, strategy?.parameters || {});
      setUserStrategyRiskValues(strategyType, strategy?.risk || {});
      syncUserStrategyTypeFields();
      renderUserStrategyAccountOptions(strategy?.account_ids || []);
      document.getElementById("user-strategy-name")?.focus();
    }

    function closeUserStrategyForm() {
      selectedUserStrategyId = "";
      userStrategyFormDirty = false;
      userStrategyFormBusy = false;
      document.getElementById("user-strategy-form").hidden = true;
      setFieldValue("user-strategy-id", "");
    }

    function userStrategyParametersFromForm(strategyType) {
      if (strategyType === "market_maker") {
        return {
          levels: numericValue("user-strategy-mm-levels"),
          price_band_pct: numericValue("user-strategy-mm-band"),
          quote_per_level: numericValue("user-strategy-mm-quote"),
          refresh_seconds: numericValue("user-strategy-mm-refresh"),
          post_only: document.getElementById("user-strategy-mm-post-only").checked,
        };
      }
      if (strategyType === "auto_buy_sell") {
        return {
          side: document.getElementById("user-strategy-side").value,
          total_quote: numericValue("user-strategy-total-quote"),
          quote_per_order: numericValue("user-strategy-order-quote"),
          interval_seconds: numericValue("user-strategy-interval"),
          start_price: numericValue("user-strategy-start-price"),
          stop_price: numericValue("user-strategy-stop-price"),
        };
      }
      if (strategyType === "dca") {
        return {
          side: document.getElementById("user-strategy-side").value,
          total_quote: numericValue("user-strategy-total-quote"),
          quote_per_order: numericValue("user-strategy-order-quote"),
          interval_seconds: numericValue("user-strategy-interval"),
          trigger_price: numericValue("user-strategy-trigger-price"),
          take_profit_pct: numericValue("user-strategy-take-profit"),
        };
      }
      if (strategyType === "spot_grid") {
        return {
          lower_price: numericValue("user-strategy-grid-lower"),
          upper_price: numericValue("user-strategy-grid-upper"),
          grid_count: numericValue("user-strategy-grid-count"),
          quote_per_grid: numericValue("user-strategy-grid-quote"),
          spacing: document.getElementById("user-strategy-grid-spacing").value,
          refresh_seconds: numericValue("user-strategy-grid-refresh"),
        };
      }
      return {
        min_profit_bps: numericValue("user-strategy-profit-bps"),
        max_cycle_quote: numericValue("user-strategy-cycle-quote"),
        scan_interval_seconds: numericValue("user-strategy-scan-seconds"),
      };
    }

    function userStrategyRiskFromForm() {
      return {
        max_order_quote: numericValue("user-strategy-risk-order"),
        max_total_quote: numericValue("user-strategy-risk-total"),
        max_daily_loss_quote: numericValue("user-strategy-risk-loss"),
        max_open_orders: numericValue("user-strategy-risk-orders"),
        max_slippage_bps: numericValue("user-strategy-risk-slippage"),
        max_order_book_age_seconds: numericValue("user-strategy-risk-book-age"),
        paper_fee_bps: numericValue("user-strategy-risk-fee"),
      };
    }

    function userStrategyPayloadFromForm() {
      const strategyType = document.getElementById("user-strategy-type").value;
      const strategy = {
        project_id: document.getElementById("user-strategy-project").value,
        name: document.getElementById("user-strategy-name").value.trim(),
        strategy_type: strategyType,
        account_ids: selectedUserStrategyAccountIds(),
        enabled: document.getElementById("user-strategy-enabled").checked,
        mode: "paper",
        parameters: userStrategyParametersFromForm(strategyType),
        risk: userStrategyRiskFromForm(),
      };
      const id = document.getElementById("user-strategy-id").value.trim();
      if (id) strategy.id = id;
      return strategy;
    }

    function formatPaperPnl(value, currency) {
      const number = Number(value);
      if (!Number.isFinite(number)) return "--";
      return `${money.format(number)} ${currency || ""}`.trim();
    }

    function paperRuntimeStatusClass(status) {
      const value = String(status || "");
      if (["running", "orders_active", "complete"].includes(value)) return "ok";
      if (value.startsWith("blocked") || value === "error") return "risk-blocked";
      return "subtle";
    }

    function renderUserPaperEvents(workspace) {
      const details = document.getElementById("user-paper-activity");
      const body = document.getElementById("user-paper-events");
      if (!details || !body) return;
      const strategies = workspace?.strategies || [];
      details.hidden = strategies.length === 0;
      body.innerHTML = "";
      const paper = workspace?.paper || {};
      const summary = paper.summary || {};
      text(
        "user-paper-summary",
        `${summary.fill_count || 0} ${uiText("fills")} · ${summary.open_order_count || 0} ${uiText("open")}`
      );
      const strategyMap = new Map(strategies.map((strategy) => [strategy.id, strategy]));
      const events = paper.events || [];
      if (events.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="4">${escapeHtml(uiText("No paper activity yet."))}</td>`;
        body.appendChild(tr);
        return;
      }
      for (const event of events) {
        const strategy = strategyMap.get(event.strategy_id);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${formatTimestamp(Number(event.created_at || 0) * 1000)}</td>
          <td>${escapeHtml(strategy?.name || event.strategy_id || "--")}</td>
          <td class="${paperRuntimeStatusClass(event.status)}">${escapeHtml(uiText(event.event_type || event.status || "--"))}</td>
          <td title="${escapeHtml(event.reason || "")}">${escapeHtml(event.reason || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderUserStrategies(workspace) {
      const body = document.getElementById("user-strategies");
      if (!body) return;
      body.innerHTML = "";
      const strategies = workspace?.strategies || [];
      const summary = workspace?.summary || {};
      text(
        "user-strategy-meta",
        `${summary.paper_running_count || 0} ${uiText("running")} · ${summary.ready_strategy_count || 0}/${summary.strategy_count || 0} ${uiText("ready")}`
      );
      renderUserPaperEvents(workspace);
      if (strategies.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7">${escapeHtml(uiText("No user strategies yet."))}</td>`;
        body.appendChild(tr);
        return;
      }
      const projectMap = new Map(
        (workspace?.projects || []).map((project) => [project.id, project])
      );
      for (const strategy of strategies) {
        const project = projectMap.get(strategy.project_id);
        const readiness = strategy.readiness || {};
        const blockers = readiness.blockers || [];
        const runtime = strategy.paper_runtime || {};
        const runtimeTerminal = Boolean(runtime.terminal);
        const runtimeStatus = !strategy.enabled
          ? "paused"
          : runtimeTerminal && runtime.status
            ? runtime.status
            : strategy.status === "blocked"
            ? "blocked"
            : runtime.status || "not_started";
        const runtimeReason = runtimeTerminal
          ? runtime.reason || blockers[0] || ""
          : blockers[0] || runtime.reason || "";
        const runtimeTitle = Array.from(
          new Set([runtimeReason, ...blockers].filter(Boolean))
        ).join("; ");
        const currency = runtime.common_quote_currency
          || workspace?.paper?.summary?.common_quote_currency
          || project?.quote_currency
          || "";
        const accounts = (strategy.accounts || [])
          .map((account) => `${account.exchange} ${account.symbol}`)
          .join(" · ");
        const statusClass = paperRuntimeStatusClass(runtimeStatus);
        const progress = Number(runtime.progress_pct);
        const progressText = Number.isFinite(progress)
          ? `<br><span class="subtle">${escapeHtml(`${progress.toFixed(1)}%`)}</span>`
          : "";
        const activityText = `${Number(runtime.fill_count || 0)} ${uiText("fills")} · ${Number(runtime.open_order_count || 0)} ${uiText("open")}`;
        const runtimeDetail = [runtimeReason, activityText].filter(Boolean).join(" · ");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(strategy.id || "")}">${escapeHtml(strategy.name || strategy.id)}<br><span class="subtle">${escapeHtml(uiText(workspaceStrategyDefinition(strategy.strategy_type)?.label || strategy.strategy_type))}</span></td>
          <td>${escapeHtml(project?.name || strategy.project_id || "--")}</td>
          <td>${escapeHtml(accounts || "--")}</td>
          <td class="num">${formatSymbolQuantity(strategy.risk?.max_total_quote || 0, project?.symbol || "", "quote")}${progressText}</td>
          <td class="${statusClass}" title="${escapeHtml(runtimeTitle)}">${escapeHtml(uiText(runtimeStatus))}<br><span class="subtle">${escapeHtml(runtimeDetail)}</span></td>
          <td class="num ${pnlClass(runtime.total_pnl_common)}">${formatPaperPnl(runtime.total_pnl_common || 0, currency)}<br><span class="subtle">${escapeHtml(uiText("Today"))}: ${formatPaperPnl(runtime.daily_pnl_common || 0, currency)}</span></td>
          <td><div class="workspace-table-actions"></div></td>
        `;
        const actions = tr.querySelector(".workspace-table-actions");
        const editButton = document.createElement("button");
        editButton.className = "control-button";
        editButton.type = "button";
        editButton.textContent = uiText("Edit");
        editButton.addEventListener("click", () => openUserStrategyForm(strategy));
        actions.appendChild(editButton);
        const copyButton = document.createElement("button");
        copyButton.className = "ghost-button";
        copyButton.type = "button";
        copyButton.textContent = uiText("Copy");
        copyButton.addEventListener("click", () => copyUserStrategy(strategy, copyButton));
        actions.appendChild(copyButton);
        const toggleButton = document.createElement("button");
        toggleButton.className = "ghost-button";
        toggleButton.type = "button";
        toggleButton.textContent = uiText(strategy.enabled ? "Pause" : "Resume");
        toggleButton.addEventListener("click", () => toggleUserStrategy(strategy, toggleButton));
        actions.appendChild(toggleButton);
        const paperCounts = strategy.paper_counts || {};
        if (Number(paperCounts.state_count || 0) + Number(paperCounts.fill_count || 0) > 0) {
          const resetButton = document.createElement("button");
          resetButton.className = "ghost-button";
          resetButton.type = "button";
          resetButton.textContent = uiText("Reset Paper");
          resetButton.addEventListener("click", () => resetUserStrategyPaper(strategy, resetButton));
          actions.appendChild(resetButton);
        }
        const deleteButton = document.createElement("button");
        deleteButton.className = "danger-button";
        deleteButton.type = "button";
        deleteButton.textContent = uiText("Delete");
        deleteButton.addEventListener("click", () => deleteUserStrategy(strategy, deleteButton));
        actions.appendChild(deleteButton);
        body.appendChild(tr);
      }
      applyMobileTableLabels();
    }

    async function applyUserStrategy(event) {
      event.preventDefault();
      if (userStrategyFormBusy) return;
      userStrategyFormBusy = true;
      const button = document.getElementById("user-strategy-save");
      button.disabled = true;
      try {
        await postUserWorkspace({
          action: "upsert_strategy",
          strategy: userStrategyPayloadFromForm(),
        });
        setUserWorkspaceNotice(uiText("Paper strategy saved."));
        closeUserStrategyForm();
      } catch (error) {
        setUserWorkspaceNotice(`strategy update failed: ${error.message || error}`);
      } finally {
        userStrategyFormBusy = false;
        button.disabled = false;
      }
    }

    async function toggleUserStrategy(strategy, button) {
      button.disabled = true;
      try {
        await postUserWorkspace({
          action: "set_strategy_enabled",
          strategy_id: strategy.id,
          enabled: !strategy.enabled,
        });
        setUserWorkspaceNotice(
          uiText(strategy.enabled ? "Paper strategy paused." : "Paper strategy resumed.")
        );
      } catch (error) {
        setUserWorkspaceNotice(`strategy control failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function copyUserStrategy(strategy, button) {
      button.disabled = true;
      try {
        await postUserWorkspace({
          action: "clone_strategy",
          strategy_id: strategy.id,
        });
        setUserWorkspaceNotice(uiText("Strategy copy created in paused paper mode."));
      } catch (error) {
        setUserWorkspaceNotice(`strategy copy failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function resetUserStrategyPaper(strategy, button) {
      const counts = strategy.paper_counts || {};
      const detail = `${Number(counts.state_count || 0)} ${uiText("state")} · ${Number(counts.fill_count || 0)} ${uiText("fills")} · ${Number(counts.event_count || 0)} ${uiText("events")}`;
      if (!dangerConfirm("Reset this paper simulation?", detail)) return;
      button.disabled = true;
      try {
        await postUserWorkspace({
          action: "reset_strategy_paper",
          strategy_id: strategy.id,
        });
        setUserWorkspaceNotice(uiText("Paper simulation reset."));
      } catch (error) {
        setUserWorkspaceNotice(`paper reset failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function deleteUserStrategy(strategy, button) {
      if (!dangerConfirm("Delete this paper strategy?")) return;
      button.disabled = true;
      try {
        await postUserWorkspace({
          action: "delete_strategy",
          strategy_id: strategy.id,
        });
        if (selectedUserStrategyId === strategy.id) closeUserStrategyForm();
        setUserWorkspaceNotice(uiText("Paper strategy deleted."));
      } catch (error) {
        setUserWorkspaceNotice(`strategy delete failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
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
      ["block_conflicting_market_maker", "MM Guard", "boolean"],
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
      const quote = quoteCurrency(config?.symbol);
      return config?.side === "sell"
        ? `AutoSell start: Bid >= ${fmt.format(start)} ${quote}`
        : `AutoBuy start: Ask <= ${fmt.format(start)} ${quote}`;
    }

    function autoStopGateText(config) {
      const stop = Number(config?.stop_price || 0);
      if (stop <= 0) return "stop off";
      const quote = quoteCurrency(config?.symbol);
      return config?.side === "sell"
        ? `AutoSell stop: Bid <= ${fmt.format(stop)} ${quote}`
        : `AutoBuy stop: Ask >= ${fmt.format(stop)} ${quote}`;
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
      const guard = config.block_conflicting_market_maker === false ? "MM guard off" : "MM guard on";
      return `${config.exchange || "--"} ${config.symbol || "--"} · ${side} · ${config.price_mode || "--"} · target ${total} · size ${slice} · ${autoStartGateText(config)} · ${autoStopGateText(config)} · every ${fmt.format(config.interval_seconds || 0)}s · ${guard}`;
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
      const riskReasons = task.last_risk?.reasons || [];
      if (!order) return riskReasons[0] || task.last_status || "--";
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
      for (const reason of task.last_risk?.reasons || []) {
        parts.push(`risk: ${reason}`);
      }
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
          <td data-label="${uiText("Task")}" title="${escapeHtml(task.id || "")}">${escapeHtml(shortId(task.id))}</td>
          <td data-label="${uiText("Status")}" class="${statusClass}" title="${escapeHtml(detailTitle)}">${escapeHtml(status)}</td>
          <td data-label="${uiText("Config")}" class="${configCell.className}" title="${escapeHtml(configCell.title)}">${configCell.html}</td>
          <td data-label="${uiText("Account")}">${escapeHtml(config.exchange || "--")}</td>
          <td data-label="${uiText("Side")}" class="${config.side === "buy" ? "side-buy" : "side-sell"}">${escapeHtml(String(config.side || "--").toUpperCase())}</td>
          <td data-label="${uiText("Filled")}" class="num">${filledText}</td>
          <td data-label="${uiText("Remaining")}" class="num">${remainingText}</td>
          <td data-label="${uiText("Progress")}" class="num">${progressPct}</td>
          <td data-label="${uiText("Open")}" class="num" title="${escapeHtml(detailTitle)}">${task.open_order_count || 0}</td>
          <td data-label="${uiText("Last")}" title="${escapeHtml(lastText)}"><div>${formatAge(task.last_cycle_at)}</div><div class="subtle">${escapeHtml(lastText)}</div></td>
          <td data-label="${uiText("Next")}">${formatDue(task.next_run_at)}</td>
          <td data-label="${uiText("Action")}" class="strategy-action"></td>
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
    let selectedMarketMakerInstanceId = "";
    let slowFormDirty = false;
    let slowFormBusy = false;
    let rebalanceFormDirty = false;
    let rebalanceFormBusy = false;
    let rebalanceLiveConfirmed = false;
    let rebalanceFeedbackMessage = "";
    let rebalanceFeedbackLevel = "";
    let gridFormDirty = false;
    let gridFormBusy = false;
    let dcaFormDirty = false;
    let dcaFormBusy = false;
    let execFormDirty = false;
    let execFormBusy = false;
    let backtestFormDirty = false;
    let backtestFormBusy = false;
    let currentUserBacktests = null;
    let selectedBacktestRunId = "";
    let userBacktestLoadBusy = false;
    let userBacktestLastLoadedAt = 0;
    let userBacktestPollTimer = null;
    let strategyCenterFormDirty = false;
    let strategyCenterFormBusy = false;
    let apiAccountFormDirty = false;
    let apiAccountFormBusy = false;
    let fundingArbFormDirty = false;
    let fundingArbFormBusy = false;
    let signalBotFormDirty = false;
    let signalBotFormBusy = false;
    let userRiskProfileDirty = false;
    let userRiskProfileBusy = false;
    let userProjectFormDirty = false;
    let userProjectFormBusy = false;
    let selectedUserProjectId = "";
    let userExchangeAccountFormDirty = false;
    let userExchangeAccountFormBusy = false;
    let selectedUserExchangeAccountId = "";
    let userStrategyFormDirty = false;
    let userStrategyFormBusy = false;
    let selectedUserStrategyId = "";
    let currentUserWorkspace = null;
    let userSetupReadinessSignature = "";
    let userMarketDiscoveryBusy = false;
    const discoveredUserMarkets = new Map();
    let userWorkspaceNoticeText = "";
    let userWorkspaceNoticeUntil = 0;

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
      if (riskFormDirty || riskFormBusy) {
        updateCoreFormStates();
        return;
      }
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
      document.getElementById("risk-auto-hedge-live").checked = Boolean(risk.auto_hedge_live_enabled);
      setNumericField("risk-auto-hedge-max-quote", risk.max_auto_hedge_quote || 0);
      setNumericField("risk-auto-hedge-slippage", risk.auto_hedge_slippage_bps ?? 50);
      setNumericField("risk-auto-hedge-attempts", risk.auto_hedge_max_attempts || 1);
      setNumericField("risk-auto-hedge-ttl", risk.auto_hedge_order_ttl_seconds ?? 2);
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
      updateCoreFormStates();
    }

    async function applyRiskConfig(event) {
      event.preventDefault();
      if (riskFormBusy) return;
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
        auto_hedge_live_enabled: document.getElementById("risk-auto-hedge-live").checked,
        max_auto_hedge_quote: numericValue("risk-auto-hedge-max-quote"),
        auto_hedge_slippage_bps: numericValue("risk-auto-hedge-slippage"),
        auto_hedge_max_attempts: numericValue("risk-auto-hedge-attempts"),
        auto_hedge_order_ttl_seconds: numericValue("risk-auto-hedge-ttl"),
        max_derivative_leverage: numericValue("risk-max-derivative-leverage"),
        min_liquidation_buffer_pct: numericValue("risk-min-liquidation-buffer"),
        max_margin_usage_pct: numericValue("risk-max-margin-usage"),
      };
      const currentRisk = lastState?.operations?.risk || lastState?.config?.risk || {};
      const enablingLive = payload.allow_live_trading && !currentRisk.allow_live_trading;
      const enablingAutoHedge = payload.auto_hedge_live_enabled && !currentRisk.auto_hedge_live_enabled;
      if (enablingLive || enablingAutoHedge) {
        const enabledControls = [
          enablingLive ? uiText("Global live trading") : "",
          enablingAutoHedge ? uiText("Automatic emergency hedge") : "",
        ].filter(Boolean).join(" · ");
        if (!dangerConfirm(
          "Enable live risk controls?",
          `${enabledControls}\n${uiText("Live orders can use real account balances.")}`,
        )) return;
        payload.confirm_live_risk = true;
      }
      riskFormBusy = true;
      updateCoreFormStates();
      try {
        const res = await fetch("/api/risk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "risk update failed");
        riskFormDirty = false;
        showToast("Risk controls saved.");
        await refresh();
      } catch (error) {
        showToast(error?.message || String(error), "error");
      } finally {
        riskFormBusy = false;
        updateCoreFormStates();
      }
    }

    function configDiffText(diff) {
      const rows = Array.isArray(diff) ? diff : [];
      if (!rows.length) return "Initial snapshot";
      return rows.slice(0, 4).map((row) => {
        const before = row.before == null ? "--" : JSON.stringify(row.before);
        const after = row.after == null ? "--" : JSON.stringify(row.after);
        return `${row.path}: ${before} -> ${after}`;
      }).join(" · ");
    }

    function renderConfigVersions(payload) {
      const body = document.getElementById("config-versions");
      if (!body) return;
      configVersionPayload = payload;
      const versions = Array.isArray(payload?.versions) ? payload.versions : [];
      text(
        "config-version-meta",
        payload?.enabled
          ? `${versions.length} ${uiText("versions")} · ${String(payload.current_hash || "").slice(0, 10)}`
          : uiText("Configuration history is unavailable"),
      );
      body.innerHTML = "";
      if (!versions.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7">${escapeHtml(uiText("No configuration versions yet."))}</td>`;
        body.appendChild(tr);
        return;
      }
      for (const version of versions) {
        const isCurrent = version.hash === payload.current_hash;
        const diffText = configDiffText(version.diff);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>#${Number(version.id || 0)}${version.known_good ? `<br><span class="risk-ok">${escapeHtml(uiText("Verified"))}</span>` : ""}</td>
          <td>${formatTimestamp(Number(version.created_at || 0) * 1000)}</td>
          <td>${escapeHtml(version.actor_email || "system")}</td>
          <td>${escapeHtml(version.action || "--")}</td>
          <td class="num">${Number(version.change_count || 0)}</td>
          <td title="${escapeHtml(diffText)}">${escapeHtml(diffText)}</td>
          <td><button class="ghost-button" type="button" ${isCurrent ? "disabled" : ""}>${escapeHtml(uiText(isCurrent ? "Current" : "Rollback"))}</button></td>
        `;
        const button = tr.querySelector("button");
        if (!isCurrent) button.addEventListener("click", () => rollbackConfigVersion(version, button));
        body.appendChild(tr);
      }
      applyMobileTableLabels();
    }

    async function loadConfigVersions(force = false) {
      if (configVersionLoading) return;
      if (!force && Date.now() - configVersionLoadAt < 5000 && configVersionPayload) {
        renderConfigVersions(configVersionPayload);
        return;
      }
      configVersionLoading = true;
      try {
        const res = await fetch("/api/config-versions?limit=30", { cache: "no-store" });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "configuration history request failed");
        configVersionLoadAt = Date.now();
        renderConfigVersions(result);
      } catch (error) {
        text("config-version-meta", error.message || String(error));
      } finally {
        configVersionLoading = false;
      }
    }

    async function rollbackConfigVersion(version, button) {
      const detail = [
        `#${version.id} · ${version.action || "--"}`,
        configDiffText(version.diff),
        uiText("Running strategies may adopt the restored settings on their next cycle."),
      ].join("\n");
      if (!dangerConfirm("Rollback to this configuration version?", detail)) return;
      button.disabled = true;
      try {
        const res = await fetch("/api/config-versions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action: "rollback",
            version_id: version.id,
            current_hash: configVersionPayload?.current_hash || "",
            confirm: true,
          }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "configuration rollback failed");
        configVersionLoadAt = 0;
        await refresh({ force: true });
        await loadConfigVersions(true);
      } catch (error) {
        text("config-version-meta", error.message || String(error));
      } finally {
        button.disabled = false;
      }
    }

    function accountSymbols(account) {
      const symbols = Array.isArray(account?.symbols) ? account.symbols : [];
      const rows = [...symbols];
      if (account?.symbol && !rows.includes(account.symbol)) rows.unshift(account.symbol);
      return rows.filter(Boolean);
    }

    function accountMarkets(account) {
      const markets = Array.isArray(account?.markets) ? account.markets : [];
      if (markets.length) {
        return markets
          .map((market) => ({
            accountKey: account.key,
            accountLabel: account.label || account.key,
            asset: String(market.asset || market.project || baseCurrency(market.symbol)).toUpperCase(),
            exchange: market.exchange || account.key,
            exchangeId: market.exchange_id || account.id || account.key,
            exchangeLabel: market.exchange_label || account.label || account.key,
            marketType: market.market_type || account.market_type || "spot",
            symbol: market.symbol || "",
            quoteCurrency: market.quote_currency || quoteCurrency(market.symbol),
            marketLimit: marketLimitFor(market.exchange || account.key, market.symbol || ""),
          }))
          .filter((market) => market.symbol);
      }
      return accountSymbols(account).map((symbol) => ({
        accountKey: account?.key || "",
        accountLabel: account?.label || account?.key || "",
        asset: baseCurrency(symbol).toUpperCase(),
        exchange: account?.key || "",
        exchangeId: account?.id || account?.key || "",
        exchangeLabel: account?.label || account?.key || "",
        marketType: account?.market_type || "spot",
        symbol,
        quoteCurrency: quoteCurrency(symbol),
        marketLimit: marketLimitFor(account?.key || "", symbol),
      }));
    }

    function allAccountMarkets(accounts) {
      return (Array.isArray(accounts) ? accounts : []).flatMap((account) => accountMarkets(account));
    }

    function uniqueBy(items, keyFn) {
      const seen = new Set();
      const rows = [];
      for (const item of items) {
        const key = keyFn(item);
        if (!key || seen.has(key)) continue;
        seen.add(key);
        rows.push(item);
      }
      return rows;
    }

    function selectedProjectForSymbol(accounts, selectedSymbol) {
      if (!selectedSymbol) return "";
      const market = allAccountMarkets(accounts).find((row) => row.symbol === selectedSymbol);
      return market?.asset || baseCurrency(selectedSymbol).toUpperCase();
    }

    function accountSelectorValue(inputName) {
      return document.querySelector(`[data-account-selector="${inputName}"]`)?.value || "";
    }

    function projectSelectorValue(inputName) {
      return document.querySelector(`[data-project-selector="${inputName}"]`)?.value || "";
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
        accounts: list.map((account) => [account.key, account.label, account.id, account.market_type, account.symbol, account.symbols, account.projects, account.markets]),
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
      accountSelect.title = uiText("Exchange account");
      const accountPlaceholder = document.createElement("option");
      accountPlaceholder.value = "";
      accountPlaceholder.textContent = uiText("Select account");
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

      const projectSelect = document.createElement("select");
      projectSelect.dataset.projectSelector = inputName;
      projectSelect.className = "account-select";
      projectSelect.title = uiText("Project");

      const exchangeSelect = document.createElement("select");
      exchangeSelect.dataset.exchangeSelector = inputName;
      exchangeSelect.className = "account-select";
      exchangeSelect.title = uiText("Exchange");

      const symbolSelect = document.createElement("select");
      symbolSelect.dataset.symbolSelector = inputName;
      symbolSelect.className = "account-select";
      symbolSelect.title = uiText("Trading pair");

      const fillProjects = (preferredProject = "") => {
        const account = accountForKey(list, accountSelect.value);
        const sourceMarkets = account ? accountMarkets(account) : allAccountMarkets(list);
        const projects = uniqueBy(sourceMarkets, (market) => market.asset)
          .map((market) => market.asset)
          .sort();
        if (preferredProject && !projects.includes(preferredProject)) {
          projects.unshift(preferredProject);
        }
        projectSelect.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = projects.length ? uiText("Select project") : uiText("No projects");
        projectSelect.appendChild(placeholder);
        for (const project of projects) {
          const option = document.createElement("option");
          option.value = project;
          option.textContent = project;
          projectSelect.appendChild(option);
        }
        if (preferredProject && projects.includes(preferredProject)) {
          projectSelect.value = preferredProject;
        } else if (projects.length) {
          projectSelect.value = projects[0];
        }
      };

      const fillExchanges = (preferredExchange = "") => {
        const project = projectSelect.value;
        const markets = allAccountMarkets(list).filter((market) => !project || market.asset === project);
        const exchangeRows = uniqueBy(markets, (market) => market.accountKey);
        if (preferredExchange && !exchangeRows.some((market) => market.accountKey === preferredExchange)) {
          const account = accountForKey(list, preferredExchange);
          const preferredMarkets = accountMarkets(account).filter(
            (market) => !project || market.asset === project,
          );
          if (account && preferredMarkets.length) {
            exchangeRows.unshift(preferredMarkets[0] || {
              accountKey: account.key,
              exchangeId: account.id || account.key,
              exchangeLabel: account.label || account.key,
              marketType: account.market_type || "spot",
            });
          }
        }
        exchangeSelect.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = exchangeRows.length ? uiText("Select exchange") : uiText("No exchanges");
        exchangeSelect.appendChild(placeholder);
        for (const market of exchangeRows) {
          const option = document.createElement("option");
          option.value = market.accountKey;
          option.textContent = `${market.exchangeLabel || market.exchangeId} (${market.marketType || "spot"})`;
          option.title = market.accountKey;
          exchangeSelect.appendChild(option);
        }
        if (preferredExchange && exchangeRows.some((market) => market.accountKey === preferredExchange)) {
          exchangeSelect.value = preferredExchange;
        } else if (exchangeRows.length) {
          exchangeSelect.value = exchangeRows[0].accountKey;
          accountSelect.value = exchangeSelect.value;
        }
      };

      const fillSymbols = (preferredSymbol = "") => {
        const project = projectSelect.value;
        const account = accountForKey(list, accountSelect.value);
        let markets = accountMarkets(account);
        if (project) markets = markets.filter((market) => market.asset === project);
        let symbols = uniqueBy(markets, (market) => market.symbol).map((market) => market.symbol);
        if (account && preferredSymbol && !symbols.includes(preferredSymbol)) symbols.unshift(preferredSymbol);
        symbolSelect.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = symbols.length ? uiText("Select pair") : uiText("No pairs");
        symbolSelect.appendChild(placeholder);
        for (const symbol of symbols) {
          const option = document.createElement("option");
          option.value = symbol;
          option.textContent = symbol;
          option.title = marketLimitSummary(marketLimitFor(account?.key || "", symbol), symbol);
          symbolSelect.appendChild(option);
        }
        if (preferredSymbol && symbols.includes(preferredSymbol)) {
          symbolSelect.value = preferredSymbol;
        } else if (symbols.length) {
          symbolSelect.value = symbols[0];
        }
      };

      accountSelect.addEventListener("change", () => {
        fillProjects(projectSelectorValue(inputName));
        fillExchanges(accountSelect.value);
        fillSymbols("");
        onDirty();
        updateSlowMarketLimitHint();
      });
      projectSelect.addEventListener("change", () => {
        fillExchanges(accountSelect.value);
        if (exchangeSelect.value) accountSelect.value = exchangeSelect.value;
        fillSymbols("");
        onDirty();
        updateSlowMarketLimitHint();
      });
      exchangeSelect.addEventListener("change", () => {
        if (exchangeSelect.value) accountSelect.value = exchangeSelect.value;
        fillProjects(projectSelect.value);
        fillSymbols("");
        onDirty();
        updateSlowMarketLimitHint();
      });
      symbolSelect.addEventListener("change", () => {
        onDirty();
        updateSlowMarketLimitHint();
      });
      fillProjects(selectedProjectForSymbol(list, selectedSymbol));
      fillExchanges(selectedExchange || accountSelect.value);
      fillSymbols(selectedSymbol || "");
      wrapper.appendChild(accountSelect);
      wrapper.appendChild(projectSelect);
      wrapper.appendChild(exchangeSelect);
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
        markMarketMakerFormDirty();
      });
    }

    function marketMakerInstances(marketMaker) {
      const instances = Array.isArray(marketMaker?.instances) ? marketMaker.instances : [];
      if (instances.length) return instances;
      return marketMaker?.config ? [{ config: marketMaker.config, status: marketMaker.status, mode: marketMaker.mode, runtime: marketMaker.runtime }] : [];
    }

    function marketMakerInstanceLabel(instance) {
      const config = instance?.config || {};
      const status = instance?.runtime?.status || instance?.status || "disabled";
      return `${config.exchange || "account"} ${config.symbol || "symbol"} · ${status}`;
    }

    function firstListText(items) {
      if (!Array.isArray(items)) return "";
      const item = items.find((value) => String(value || "").trim());
      return item == null ? "" : String(item);
    }

    function marketMakerStatusReason(instance) {
      const runtime = instance?.runtime || {};
      const risk = runtime.last_risk || instance?.safety?.risk || instance?.safety || {};
      const execution = runtime.last_execution || {};
      return (
        instance?.status_reason ||
        instance?.error ||
        runtime.last_error ||
        runtime.open_order_sync_error ||
        runtime.reason ||
        (instance?.config?.id_mismatch
          ? `ID mismatch: ${instance.config.id} should be ${instance.config.expected_id}`
          : "") ||
        firstListText(risk.reasons) ||
        firstListText(risk.warnings) ||
        execution.reason ||
        firstListText(execution.reasons) ||
        firstListText(execution.warnings) ||
        ""
      );
    }

    function marketMakerStatusClass(status) {
      if (["placed", "unchanged", "planned"].includes(status)) return "risk-ok";
      if (["disabled", "paused", "starting"].includes(status)) return "risk-off";
      return "risk-blocked";
    }

    function marketMakerInstanceName(instance) {
      const config = instance?.config || {};
      return instance?.display_name || `${config.exchange || "account"} ${config.symbol || "symbol"}`;
    }

    function selectedMarketMakerInstance(marketMaker) {
      const instances = marketMakerInstances(marketMaker);
      if (!instances.length) return null;
      const selected = instances.find((instance) => (instance.config?.id || "") === selectedMarketMakerInstanceId);
      return selected || instances[0];
    }

    function renderMarketMakerInstanceSelect(marketMaker) {
      const select = document.getElementById("mm-instance");
      if (!select) return;
      const instances = marketMakerInstances(marketMaker);
      const ids = instances.map((instance) => instance.config?.id || "").filter(Boolean);
      if (!selectedMarketMakerInstanceId || !ids.includes(selectedMarketMakerInstanceId)) {
        selectedMarketMakerInstanceId = ids[0] || "";
      }
      select.innerHTML = "";
      for (const instance of instances) {
        const config = instance.config || {};
        const option = document.createElement("option");
        option.value = config.id || "";
        option.textContent = marketMakerInstanceLabel(instance);
        select.appendChild(option);
      }
      select.value = selectedMarketMakerInstanceId;
      const copyButton = document.getElementById("mm-copy");
      if (copyButton) copyButton.disabled = !instances.length || mmFormBusy;
      document.getElementById("mm-delete").disabled = instances.length <= 1 || mmFormBusy;
    }

    function renderMarketMakerInstanceStatus(marketMaker) {
      const body = document.getElementById("mm-instance-status");
      if (!body) return;
      const instances = marketMakerInstances(marketMaker);
      body.innerHTML = "";
      if (!instances.length) {
        const row = document.createElement("div");
        row.className = "instance-status-row";
        row.textContent = "No market maker instances";
        body.appendChild(row);
        return;
      }
      for (const instance of instances) {
        const status = instance?.runtime?.status || instance?.status || "disabled";
        const runtime = instance?.runtime || {};
        const row = document.createElement("div");
        row.className = "instance-status-row";
        const detail = `${instance?.mode || runtime.mode || "dry_run"} · open ${runtime.open_order_count ?? 0} · placed ${runtime.placed_count ?? 0} · canceled ${runtime.canceled_count ?? 0}`;
        const reason = marketMakerStatusReason(instance) || "--";
        row.innerHTML = `
          <div class="instance-status-name" title="${escapeHtml(marketMakerInstanceName(instance))}">${escapeHtml(marketMakerInstanceName(instance))}</div>
          <div class="instance-status-pill ${marketMakerStatusClass(status)}">${escapeHtml(status)}</div>
          <div class="instance-status-detail">${escapeHtml(detail)}</div>
          <div class="instance-status-reason" title="${escapeHtml(reason)}">${escapeHtml(reason)}</div>
        `;
        body.appendChild(row);
      }
    }

    function marketMakerFormReadiness(payload = marketMakerPayloadFromForm()) {
      const missing = [];
      if (!payload.exchange) missing.push(uiText("account"));
      if (!payload.symbol) missing.push(uiText("pair"));
      if (!(payload.levels >= 1)) missing.push(uiText("levels"));
      if (!(payload.price_band_pct > 0)) missing.push(uiText("price band"));
      if (!(payload.quote_per_level > 0)) missing.push(uiText("quote per level"));
      if (!(payload.poll_seconds >= 1)) missing.push(uiText("refresh interval"));
      return {
        ready: missing.length === 0,
        detail: missing.length
          ? `${uiText("Missing")}: ${missing.join(", ")}`
          : `${payload.exchange} · ${payload.symbol} · ${payload.levels} ${uiText("levels per side")}`,
      };
    }

    function marketMakerLiveState(marketMaker = lastState?.market_maker) {
      const selected = selectedMarketMakerInstance(marketMaker || {});
      const config = selected?.config || marketMaker?.config || {};
      const runtime = selected?.runtime || marketMaker?.runtime || {};
      return {
        configuredLive: Boolean(config.enabled && config.live_enabled),
        status: runtime.status || selected?.status || marketMaker?.status || "stopped",
        mode: runtime.mode || selected?.mode || marketMaker?.mode || "dry_run",
      };
    }

    function renderMarketMakerWorkflow(marketMaker = lastState?.market_maker) {
      if (!marketMaker || !document.getElementById("mm-levels")) return;
      const payload = marketMakerPayloadFromForm();
      const parameters = marketMakerFormReadiness(payload);
      const risk = coreLiveRiskReadiness("market_maker", [payload.exchange]);
      const live = marketMakerLiveState(marketMaker);
      const selected = selectedMarketMakerInstance(marketMaker);
      const lifecycle = strategyLifecycleRow("market_maker", {
        instanceId: selected?.config?.id || selected?.id || selectedMarketMakerInstanceId,
        account: payload.exchange,
        symbol: payload.symbol,
      });
      renderStrategyWorkflow("mm-workflow", [
        {
          title: "Parameters",
          state: parameters.ready ? "ready" : "blocked",
          label: parameters.ready ? (mmFormDirty ? "Unsaved" : "Ready") : "Required",
          detail: parameters.detail,
        },
        {
          title: "Risk Check",
          state: risk.ready ? "ready" : "blocked",
          label: risk.ready ? "Ready" : "Blocked",
          detail: risk.detail,
        },
        lifecycleWorkflowStep(lifecycle, {
          title: "Run State",
          state: live.configuredLive ? "live" : "idle",
          label: live.configuredLive ? "Live" : "Stopped",
          detail: `${live.mode} · ${live.status}`,
        }),
      ]);
      const startButton = document.getElementById("mm-start");
      const stopButton = document.getElementById("mm-stop");
      const riskButton = document.getElementById("mm-open-risk");
      if (startButton) {
        startButton.hidden = live.configuredLive;
        startButton.disabled = mmFormBusy || !parameters.ready || !risk.ready;
      }
      if (stopButton) {
        stopButton.hidden = !live.configuredLive;
        stopButton.disabled = mmFormBusy;
      }
      if (riskButton) riskButton.hidden = risk.ready;
    }

    function marketMakerConfirmationDetail(payload) {
      const quote = quoteCurrency(payload.symbol);
      const plannedOrders = Math.max(0, Number(payload.levels || 0)) * 2;
      const plannedQuote = plannedOrders * Math.max(0, Number(payload.quote_per_level || 0));
      return [
        `${uiText("Account")}: ${payload.exchange}`,
        `${uiText("Trading pair")}: ${payload.symbol}`,
        `${uiText("Orders")}: ${plannedOrders} (${payload.levels} ${uiText("levels per side")})`,
        `${uiText("Quote/Level")}: ${quote} ${money.format(payload.quote_per_level)}`,
        `${uiText("Planned total")}: ${quote} ${money.format(plannedQuote)}`,
        `${uiText("Band %")}: ${fmt.format(payload.price_band_pct)}`,
        `${uiText("Refresh Sec")}: ${fmt.format(payload.poll_seconds)}`,
        `${uiText("Post Only")}: ${payload.post_only ? uiText("Yes") : uiText("No")}`,
      ].join("\n");
    }

    function syncSelectedMarketMakerId(result, preferredId, exchange = "", symbol = "") {
      const instances = Array.isArray(result?.instances) ? result.instances : [];
      const exact = instances.find((instance) => instance.id === preferredId);
      const route = instances.find(
        (instance) => instance.exchange === exchange && instance.symbol === symbol,
      );
      const resolvedId = exact?.id || route?.id || result?.config?.id || preferredId || "";
      if (!resolvedId) return;
      selectedMarketMakerInstanceId = resolvedId;
      if (resolvedId !== preferredId) text("mm-meta", `saved as ${resolvedId}`);
    }

    async function postMarketMakerConfig(payload) {
      const res = await fetch("/api/market-maker", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await res.json();
      if (!res.ok) throw new Error(result.error || "market maker update failed");
      syncSelectedMarketMakerId(
        result,
        payload.id || selectedMarketMakerInstanceId,
        payload.exchange,
        payload.symbol,
      );
      return result;
    }

    function renderMarketMakerConfig(marketMaker) {
      if (!marketMaker || mmFormBusy) return;
      renderMarketMakerInstanceSelect(marketMaker);
      renderMarketMakerInstanceStatus(marketMaker);
      if (mmFormDirty) {
        updateCoreFormStates();
        renderMarketMakerWorkflow(marketMaker);
        return;
      }
      const selected = selectedMarketMakerInstance(marketMaker);
      const config = selected?.config || marketMaker.config;
      if (!config) return;
      document.getElementById("mm-enabled").checked = Boolean(config.enabled);
      document.getElementById("mm-live-enabled").checked = Boolean(config.live_enabled);
      renderMarketMakerAccounts(marketMaker.accounts, config.exchange || "", config.symbol || "");
      setNumericField("mm-levels", config.levels || 1);
      setNumericField("mm-band", config.price_band_pct || 0);
      setNumericField("mm-quote", config.quote_per_level || 0);
      document.getElementById("mm-depth-shape").value = config.depth_shape || "linear";
      setNumericField("mm-min-quote", config.min_order_quote || 0);
      setNumericField("mm-min-distance", config.min_distance_bps || 0);
      setNumericField("mm-reprice", config.reprice_threshold_bps || 0);
      setNumericField("mm-reprice-hysteresis", config.reprice_hysteresis_bps ?? 3);
      setNumericField("mm-full-reprice", config.full_reprice_threshold_bps ?? 25);
      setNumericField("mm-poll", config.poll_seconds || 1);
      setNumericField("mm-max-order", config.max_order_quote || 0);
      setNumericField("mm-max-cycle", config.max_cycle_quote || 0);
      setNumericField("mm-max-open-orders", config.max_open_orders || 0);
      setNumericField("mm-max-cancels", config.max_cancels_per_cycle || 0);
      setNumericField("mm-max-slippage", config.max_slippage_bps || 0);
      setNumericField("mm-max-gap", config.max_order_book_gap_bps || 0);
      setNumericField("mm-max-book-age", config.max_order_book_age_seconds || 0);
      document.getElementById("mm-inventory-enabled").checked = Boolean(config.inventory_control_enabled);
      setNumericField("mm-inventory-target", config.inventory_target_base || 0);
      setNumericField("mm-inventory-band", config.inventory_band_base || 0);
      setNumericField("mm-inventory-max", config.inventory_max_deviation_base || 0);
      document.getElementById("mm-post-only").checked = Boolean(config.post_only);
      updateCoreFormStates();
      renderMarketMakerWorkflow(marketMaker);
    }

    function marketMakerPayloadFromForm() {
      return {
        id: selectedMarketMakerInstanceId,
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
        reprice_hysteresis_bps: numericValue("mm-reprice-hysteresis"),
        full_reprice_threshold_bps: numericValue("mm-full-reprice"),
        poll_seconds: numericValue("mm-poll"),
        max_order_quote: numericValue("mm-max-order"),
        max_cycle_quote: numericValue("mm-max-cycle"),
        max_open_orders: numericValue("mm-max-open-orders"),
        max_cancels_per_cycle: numericValue("mm-max-cancels"),
        max_slippage_bps: numericValue("mm-max-slippage"),
        max_order_book_gap_bps: numericValue("mm-max-gap"),
        max_order_book_age_seconds: numericValue("mm-max-book-age"),
        inventory_control_enabled: document.getElementById("mm-inventory-enabled").checked,
        inventory_target_base: numericValue("mm-inventory-target"),
        inventory_band_base: numericValue("mm-inventory-band"),
        inventory_max_deviation_base: numericValue("mm-inventory-max"),
        post_only: document.getElementById("mm-post-only").checked,
      };
    }

    function newMarketMakerId(exchange, symbol) {
      const seed = `${exchange || "mm"}-${symbol || "symbol"}`.toLowerCase();
      const normalized = seed.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "mm";
      return `${normalized}-${Date.now().toString(36)}`;
    }

    async function saveMarketMakerInstances(instances) {
      const res = await fetch("/api/market-maker", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instances }),
      });
      const result = await res.json();
      if (!res.ok) throw new Error(result.error || "market maker update failed");
      mmFormDirty = false;
      const selectedDraft = instances.find(
        (instance) => instance.id === selectedMarketMakerInstanceId,
      ) || {};
      syncSelectedMarketMakerId(
        result,
        selectedMarketMakerInstanceId,
        selectedDraft.exchange,
        selectedDraft.symbol,
      );
      await refresh({ force: true });
    }

    async function addMarketMakerInstance() {
      if (mmFormBusy) return;
      mmFormBusy = true;
      updateCoreFormStates();
      try {
        const marketMaker = lastState?.market_maker || {};
        const currentInstances = marketMakerInstances(marketMaker).map((instance) => ({ ...(instance.config || {}) }));
        const draft = marketMakerPayloadFromForm();
        draft.id = newMarketMakerId(draft.exchange, draft.symbol);
        draft.enabled = false;
        draft.live_enabled = false;
        currentInstances.push(draft);
        selectedMarketMakerInstanceId = draft.id;
        await saveMarketMakerInstances(currentInstances);
      } catch (error) {
        text("mm-meta", `add failed: ${error.message || error}`);
      } finally {
        mmFormBusy = false;
        updateCoreFormStates();
      }
    }

    async function copyMarketMakerInstance() {
      if (mmFormBusy || !selectedMarketMakerInstanceId) return;
      const sourceId = selectedMarketMakerInstanceId;
      const suffix = Date.now().toString(36);
      const newId = `${sourceId.slice(0, 52)}-copy-${suffix}`;
      mmFormBusy = true;
      updateCoreFormStates();
      try {
        selectedMarketMakerInstanceId = newId;
        await postMarketMakerConfig({ copy_id: sourceId, new_id: newId });
        mmFormDirty = false;
        setStrategyFeedback(
          "mm-feedback",
          "Strategy copy created in stopped mode.",
          "ok",
        );
        await refresh({ force: true });
      } catch (error) {
        selectedMarketMakerInstanceId = sourceId;
        setStrategyFeedback("mm-feedback", error.message || String(error), "error");
      } finally {
        mmFormBusy = false;
        updateCoreFormStates();
      }
    }

    async function deleteMarketMakerInstance() {
      if (mmFormBusy || !selectedMarketMakerInstanceId) return;
      const marketMaker = lastState?.market_maker || {};
      const currentInstances = marketMakerInstances(marketMaker).map((instance) => ({ ...(instance.config || {}) }));
      if (currentInstances.length <= 1) return;
      mmFormBusy = true;
      updateCoreFormStates();
      try {
        const remaining = currentInstances.filter((instance) => instance.id !== selectedMarketMakerInstanceId);
        selectedMarketMakerInstanceId = remaining[0]?.id || "";
        await saveMarketMakerInstances(remaining);
      } catch (error) {
        text("mm-meta", `delete failed: ${error.message || error}`);
      } finally {
        mmFormBusy = false;
        updateCoreFormStates();
      }
    }

    async function applyMarketMakerConfig(event) {
      event.preventDefault();
      if (mmFormBusy) return;
      const payload = marketMakerPayloadFromForm();
      const parameters = marketMakerFormReadiness(payload);
      if (!parameters.ready) {
        setStrategyFeedback("mm-feedback", parameters.detail, "error");
        renderMarketMakerWorkflow(lastState?.market_maker);
        return;
      }
      if (payload.enabled && payload.live_enabled) {
        const risk = coreLiveRiskReadiness("market_maker", [payload.exchange]);
        if (!risk.ready) {
          setStrategyFeedback("mm-feedback", risk.detail, "error");
          renderMarketMakerWorkflow(lastState?.market_maker);
          return;
        }
        if (!dangerConfirm(
          "Apply these changes to the running live Market Maker?",
          marketMakerConfirmationDetail(payload),
        )) return;
        payload.confirm_live = LIVE_MARKET_MAKER_CONFIRMATION;
        try {
          setStrategyFeedback("mm-feedback", "Running live preflight...");
          const preflight = await runStrategyPreflight("market_maker", payload);
          payload.preflight_token = preflight.token;
        } catch (error) {
          setStrategyFeedback("mm-feedback", error.message || String(error), "error");
          return;
        }
      }
      mmFormBusy = true;
      setStrategyFeedback("mm-feedback");
      updateCoreFormStates();
      renderMarketMakerWorkflow(lastState?.market_maker);
      try {
        await postMarketMakerConfig(payload);
        mmFormDirty = false;
        setStrategyFeedback("mm-feedback", "Market Maker settings saved.", "ok");
        await refresh({ force: true });
      } catch (error) {
        setStrategyFeedback("mm-feedback", error.message || String(error), "error");
      } finally {
        mmFormBusy = false;
        updateCoreFormStates();
        renderMarketMakerWorkflow(lastState?.market_maker);
      }
    }

    async function startMarketMaker() {
      if (mmFormBusy) return;
      const payload = {
        ...marketMakerPayloadFromForm(),
        enabled: true,
        live_enabled: true,
        confirm_live: LIVE_MARKET_MAKER_CONFIRMATION,
      };
      const parameters = marketMakerFormReadiness(payload);
      const risk = coreLiveRiskReadiness("market_maker", [payload.exchange]);
      if (!parameters.ready || !risk.ready) {
        setStrategyFeedback(
          "mm-feedback",
          parameters.ready ? risk.detail : parameters.detail,
          "error",
        );
        renderMarketMakerWorkflow(lastState?.market_maker);
        return;
      }
      let preflight;
      try {
        setStrategyFeedback("mm-feedback", "Running live preflight...");
        preflight = await runStrategyPreflight("market_maker", payload);
      } catch (error) {
        setStrategyFeedback("mm-feedback", error.message || String(error), "error");
        return;
      }
      if (!dangerConfirm(
        "Start live Market Maker with these settings?",
        `${marketMakerConfirmationDetail(payload)}\n${uiText("Preflight")}: ${preflight.checks?.length || 0} ${uiText("checks passed")}`,
      )) return;
      payload.preflight_token = preflight.token;
      mmFormBusy = true;
      setStrategyFeedback("mm-feedback");
      document.getElementById("mm-enabled").checked = true;
      document.getElementById("mm-live-enabled").checked = true;
      updateCoreFormStates();
      renderMarketMakerWorkflow(lastState?.market_maker);
      try {
        await postMarketMakerConfig(payload);
        mmFormDirty = false;
        setStrategyFeedback("mm-feedback", "Live Market Maker started.", "ok");
        await refresh({ force: true });
      } catch (error) {
        document.getElementById("mm-enabled").checked = false;
        document.getElementById("mm-live-enabled").checked = false;
        setStrategyFeedback("mm-feedback", error.message || String(error), "error");
      } finally {
        mmFormBusy = false;
        updateCoreFormStates();
        renderMarketMakerWorkflow(lastState?.market_maker);
      }
    }

    async function stopMarketMaker() {
      if (mmFormBusy) return;
      const payload = {
        ...marketMakerPayloadFromForm(),
        enabled: false,
        live_enabled: false,
      };
      if (!dangerConfirm(
        "Stop this Market Maker and cancel its managed orders?",
        `${payload.exchange} · ${payload.symbol}`,
      )) return;
      mmFormBusy = true;
      setStrategyFeedback("mm-feedback");
      document.getElementById("mm-enabled").checked = false;
      document.getElementById("mm-live-enabled").checked = false;
      updateCoreFormStates();
      renderMarketMakerWorkflow(lastState?.market_maker);
      try {
        await postMarketMakerConfig(payload);
        mmFormDirty = false;
        setStrategyFeedback("mm-feedback", "Market Maker stop requested.", "ok");
        await refresh({ force: true });
      } catch (error) {
        document.getElementById("mm-enabled").checked = true;
        document.getElementById("mm-live-enabled").checked = true;
        setStrategyFeedback("mm-feedback", error.message || String(error), "error");
      } finally {
        mmFormBusy = false;
        updateCoreFormStates();
        renderMarketMakerWorkflow(lastState?.market_maker);
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
        markSlowFormDirty();
        updateSlowLabels();
      });
    }

    function slowUnitContext() {
      const symbol = selectedSlowSymbol();
      const base = baseCurrency(symbol);
      const quote = quoteCurrency(symbol);
      const pair = symbol || `${base}/${quote}`;
      return { base, quote, pair };
    }

    function setSlowLabel(id, text) {
      const label = document.getElementById(id);
      if (label) label.textContent = text;
    }

    function updateSlowUnitLabels() {
      const { base, quote } = slowUnitContext();
      setSlowLabel("slow-total-base-label", `${uiText("Total Base")} (${base})`);
      setSlowLabel("slow-total-quote-label", `${uiText("Total Quote")} (${quote})`);
      setSlowLabel("slow-slice-min-label", `${uiText("Min Base/Order")} (${base})`);
      setSlowLabel("slow-slice-max-label", `${uiText("Max Base/Order")} (${base})`);
    }

    function updateSlowGateLabels() {
      const side = document.getElementById("slow-side")?.value || "sell";
      const startLabel = document.getElementById("slow-start-price-label");
      const stopLabel = document.getElementById("slow-stop-price-label");
      const startHelp = document.getElementById("slow-start-price-help");
      const stopHelp = document.getElementById("slow-stop-price-help");
      const { pair, quote } = slowUnitContext();
      const unitText = `${uiText("Unit")}: ${quote}.`;
      if (side === "buy") {
        if (startLabel) {
          startLabel.textContent = `${uiText("Start Gate")} (${uiText("AutoBuy start when Ask <= price")} · ${pair} · ${quote})`;
        }
        if (stopLabel) {
          stopLabel.textContent = `${uiText("Stop Gate")} (${uiText("AutoBuy stop when Ask >= price")} · ${pair} · ${quote})`;
        }
        if (startHelp) {
          startHelp.textContent = `${uiText("AutoBuy starts when best ask is at or below this price.")} ${unitText}`;
        }
        if (stopHelp) {
          stopHelp.textContent = `${uiText("AutoBuy stops before each execution when best ask is at or above this price.")} ${unitText}`;
        }
        return;
      }
      if (startLabel) {
        startLabel.textContent = `${uiText("Start Gate")} (${uiText("AutoSell start when Bid >= price")} · ${pair} · ${quote})`;
      }
      if (stopLabel) {
        stopLabel.textContent = `${uiText("Stop Gate")} (${uiText("AutoSell stop when Bid <= price")} · ${pair} · ${quote})`;
      }
      if (startHelp) {
        startHelp.textContent = `${uiText("AutoSell starts when best bid is at or above this price.")} ${unitText}`;
      }
      if (stopHelp) {
        stopHelp.textContent = `${uiText("AutoSell stops before each execution when best bid is at or below this price.")} ${unitText}`;
      }
    }

    function updateSlowLabels() {
      updateSlowUnitLabels();
      updateSlowGateLabels();
      updateSlowMarketLimitHint();
    }

    function slowPlanReferencePrice() {
      const plan = lastState?.slow_execution?.plan;
      if (!plan) return null;
      const exchange = selectedSlowAccount();
      const symbol = selectedSlowSymbol();
      if (exchange && plan.exchange && exchange !== plan.exchange) return null;
      if (symbol && plan.symbol && symbol !== plan.symbol) return null;
      const value = Number(plan.trigger_price || plan.order?.price || plan.mid_price);
      return Number.isFinite(value) && value > 0 ? value : null;
    }

    function updateSlowMarketLimitHint() {
      const box = document.getElementById("slow-market-limits");
      if (!box) return;
      const exchange = selectedSlowAccount();
      const symbol = selectedSlowSymbol();
      if (!exchange || !symbol) {
        box.textContent = uiText("Select an account and pair to view exchange minimums.");
        box.className = "field wide-field market-limit-hint";
        return;
      }
      const limit = marketLimitFor(exchange, symbol);
      const costMin = marketLimitValue(limit, "cost_min");
      const referencePrice = slowPlanReferencePrice();
      const base = baseCurrency(symbol);
      const quote = quoteCurrency(symbol);
      const configuredMin = numericValue("slow-slice-min");
      const suggestedBase = costMin != null && referencePrice ? costMin / referencePrice : null;
      const belowExchangeMin = costMin != null && referencePrice && configuredMin > 0 && configuredMin * referencePrice < costMin;
      const summary = marketLimitSummary(limit, symbol);
      const configuredText = configuredMin > 0 && referencePrice
        ? `${uiText("Configured min/order")}: ${formatLimitValue(configuredMin, base)} ≈ ${formatLimitValue(configuredMin * referencePrice, quote)}`
        : `${uiText("Configured min/order")}: --`;
      const suggestedText = suggestedBase != null
        ? `${uiText("Suggested minimum base")}: ${formatLimitValue(suggestedBase, base)}`
        : `${uiText("Suggested minimum base")}: --`;
      box.textContent = `${summary} · ${configuredText} · ${suggestedText}`;
      box.className = `field wide-field market-limit-hint ${belowExchangeMin ? "limit-warning" : ""}`;
    }

    function slowExecutionFormReadiness(payload = slowExecutionPayloadFromForm()) {
      const missing = [];
      if (!payload.exchange) missing.push(uiText("account"));
      if (!payload.symbol) missing.push(uiText("pair"));
      if (!payload.unlimited_total && !(payload.total_base > 0 || payload.total_quote > 0)) {
        missing.push(uiText("total target"));
      }
      if (payload.slice_mode === "configured") {
        if (!(payload.slice_base_min > 0 && payload.slice_base_max >= payload.slice_base_min)) {
          missing.push(uiText("order size range"));
        }
      }
      if (!(payload.interval_seconds > 0)) missing.push(uiText("interval"));
      return {
        ready: missing.length === 0,
        detail: missing.length
          ? `${uiText("Missing")}: ${missing.join(", ")}`
          : `${payload.exchange} · ${payload.symbol} · ${String(payload.side || "").toUpperCase()}`,
      };
    }

    function renderSlowExecutionWorkflow(data = lastState?.slow_execution) {
      if (!data || !document.getElementById("slow-side")) return;
      const payload = slowExecutionPayloadFromForm();
      const parameters = slowExecutionFormReadiness(payload);
      const risk = coreLiveRiskReadiness("slow_execution", [payload.exchange]);
      const tasks = data.tasks?.tasks || [];
      const activeTasks = tasks.filter((task) => !AUTO_TERMINAL_STATUSES.has(task.status || ""));
      const routeTasks = activeTasks.filter((task) => {
        const config = task.config || task;
        return config.exchange === payload.exchange && config.symbol === payload.symbol;
      });
      const first = routeTasks[0] || activeTasks[0];
      const lifecycle = strategyLifecycleRow("slow_execution", {
        instanceId: first?.id || "default",
        account: payload.exchange,
        symbol: payload.symbol,
      });
      const readyToStart = parameters.ready && risk.ready;
      renderStrategyWorkflow("slow-workflow", [
        {
          title: "Parameters",
          state: parameters.ready ? "ready" : "blocked",
          label: parameters.ready ? (slowFormDirty ? "Unsaved" : "Ready") : "Required",
          detail: parameters.detail,
        },
        {
          title: "Risk Check",
          state: risk.ready ? "ready" : "blocked",
          label: risk.ready ? "Ready" : "Blocked",
          detail: risk.detail,
        },
        lifecycleWorkflowStep(lifecycle, {
          title: "Task State",
          state: activeTasks.length ? "live" : readyToStart ? "ready" : "blocked",
          label: activeTasks.length ? "Running" : readyToStart ? "Ready to start" : "Not ready",
          detail: first
            ? `${activeTasks.length} ${uiText("active task(s)")} · ${first.status || "running"}`
            : "No active task",
        }),
      ]);
      const createButton = document.getElementById("slow-create-task");
      const riskButton = document.getElementById("slow-open-risk");
      if (createButton) {
        createButton.disabled = slowFormBusy || !parameters.ready || !risk.ready;
      }
      if (riskButton) riskButton.hidden = risk.ready;
    }

    function slowExecutionConfirmationDetail(payload) {
      const base = baseCurrency(payload.symbol);
      const quote = quoteCurrency(payload.symbol);
      let total = uiText("Unlimited");
      if (!payload.unlimited_total) {
        total = payload.total_quote > 0
          ? `${quote} ${money.format(payload.total_quote)}`
          : `${base} ${fmt.format(payload.total_base)}`;
      }
      const size = payload.slice_mode === "top_level"
        ? uiText("Match top-of-book size")
        : `${base} ${fmt.format(payload.slice_base_min)} - ${fmt.format(payload.slice_base_max)}`;
      const side = String(payload.side || "").toLowerCase();
      const gatePrice = side === "buy" ? "Ask" : "Bid";
      const startOperator = side === "buy" ? "<=" : ">=";
      const stopOperator = side === "buy" ? ">=" : "<=";
      return [
        `${uiText("Account")}: ${payload.exchange}`,
        `${uiText("Trading pair")}: ${payload.symbol}`,
        `${uiText("Side")}: ${side.toUpperCase()}`,
        `${uiText("Total target")}: ${total}`,
        `${uiText("Each order")}: ${size}`,
        `${uiText("Price Mode")}: ${payload.price_mode}`,
        `${uiText("Place Sec")}: ${fmt.format(payload.interval_seconds)}`,
        `${uiText("Start Gate")}: ${payload.start_price > 0 ? `${gatePrice} ${startOperator} ${fmt.format(payload.start_price)} ${quote}` : uiText("Immediate")}`,
        `${uiText("Stop Gate")}: ${payload.stop_price > 0 ? `${gatePrice} ${stopOperator} ${fmt.format(payload.stop_price)} ${quote}` : uiText("None")}`,
      ].join("\n");
    }

    function renderSlowExecutionConfig(config, accounts) {
      if (!config || slowFormDirty || slowFormBusy) {
        updateCoreFormStates();
        updateSlowMarketLimitHint();
        renderSlowExecutionWorkflow(lastState?.slow_execution);
        return;
      }
      document.getElementById("slow-enabled").checked = Boolean(config.enabled);
      renderSlowExecutionAccounts(config.accounts || accounts, config.exchange || "", config.symbol || "");
      document.getElementById("slow-side").value = config.side || "sell";
      updateSlowLabels();
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
      updateSlowMarketLimitHint();
      updateCoreFormStates();
      renderSlowExecutionWorkflow(lastState?.slow_execution);
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
      const payload = slowExecutionPayloadFromForm();
      const parameters = slowExecutionFormReadiness(payload);
      if (!parameters.ready) {
        setStrategyFeedback("slow-feedback", parameters.detail, "error");
        renderSlowExecutionWorkflow(lastState?.slow_execution);
        return;
      }
      slowFormBusy = true;
      setStrategyFeedback("slow-feedback");
      updateCoreFormStates();
      renderSlowExecutionWorkflow(lastState?.slow_execution);
      try {
        const res = await fetch("/api/auto-buy-sell", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "auto buy/sell update failed");
        slowFormDirty = false;
        setStrategyFeedback("slow-feedback", "Auto Buy/Sell defaults saved.", "ok");
        await refresh({ force: true });
      } catch (error) {
        setStrategyFeedback("slow-feedback", error.message || String(error), "error");
      } finally {
        slowFormBusy = false;
        updateCoreFormStates();
        renderSlowExecutionWorkflow(lastState?.slow_execution);
      }
    }

    function setRebalanceFeedback(message = "", level = "") {
      rebalanceFeedbackMessage = message;
      rebalanceFeedbackLevel = level;
      const feedback = document.getElementById("rebalance-feedback");
      if (!feedback) return;
      feedback.textContent = message ? uiText(message) : "";
      feedback.classList.toggle("is-error", level === "error");
      feedback.classList.toggle("is-ok", level === "ok");
    }

    function invalidateLiveRebalanceConfirmation() {
      rebalanceLiveConfirmed = false;
    }

    function rebalanceRiskReadiness(data) {
      const config = data?.config || {};
      const buyExchange = selectedStrategyAccount("rebalance-buy") || config.buy_exchange || "";
      const sellExchange = selectedStrategyAccount("rebalance-sell") || config.sell_exchange || "";
      return coreLiveRiskReadiness(
        "cross_exchange_rebalance",
        [buyExchange, sellExchange],
      );
    }

    function rebalanceFormReadiness(payload = crossExchangeRebalancePayloadFromForm()) {
      const missing = [];
      if (!payload.buy_exchange || !payload.buy_symbol) missing.push(uiText("cash source"));
      if (!payload.sell_exchange || !payload.sell_symbol) missing.push(uiText("cash destination"));
      if (!(payload.total_quote_common > 0)) missing.push(uiText("total target"));
      if (!(payload.quote_per_cycle_common > 0)) missing.push(uiText("per-cycle amount"));
      if (!(payload.interval_seconds > 0)) missing.push(uiText("interval"));
      return {
        ready: missing.length === 0,
        detail: missing.length
          ? `${uiText("Missing")}: ${missing.join(", ")}`
          : `${payload.buy_exchange} ${payload.buy_symbol} -> ${payload.sell_exchange} ${payload.sell_symbol}`,
      };
    }

    function rebalanceProgressRequiresReset(data, payload = null) {
      const config = data?.config || {};
      const runtime = data?.runtime || {};
      const selected = payload || {
        buy_exchange: selectedStrategyAccount("rebalance-buy") || config.buy_exchange || "",
        buy_symbol: selectedStrategySymbol("rebalance-buy") || config.buy_symbol || "",
        sell_exchange: selectedStrategyAccount("rebalance-sell") || config.sell_exchange || "",
        sell_symbol: selectedStrategySymbol("rebalance-sell") || config.sell_symbol || "",
        total_quote_common: numericValue("rebalance-total") || config.total_quote_common || 0,
      };
      const sameRoute = selected.buy_exchange === config.buy_exchange
        && selected.buy_symbol === config.buy_symbol
        && selected.sell_exchange === config.sell_exchange
        && selected.sell_symbol === config.sell_symbol;
      const target = Math.max(0, Number(selected.total_quote_common || 0));
      const completed = Math.max(0, Number(runtime.completed_quote_common || 0));
      return sameRoute && target > 0 && completed >= target - Math.max(target * 1e-12, 1e-9);
    }

    function renderRebalanceReadiness(data = lastState?.cross_exchange_rebalance) {
      const readiness = document.getElementById("rebalance-readiness");
      if (!readiness || !data) return;
      const resetRequired = rebalanceProgressRequiresReset(data);
      const risk = rebalanceRiskReadiness(data);
      const payload = crossExchangeRebalancePayloadFromForm();
      const parameters = rebalanceFormReadiness(payload);
      const config = data.config || {};
      const runtime = data.runtime || {};
      const configuredLive = Boolean(config.enabled && config.live_enabled);
      const lifecycle = strategyLifecycleRow("cross_exchange_rebalance");
      renderStrategyWorkflow("rebalance-readiness", [
        {
          title: "Parameters",
          state: parameters.ready && !resetRequired ? "ready" : "blocked",
          label: resetRequired
            ? "Reset required"
            : parameters.ready
              ? (rebalanceFormDirty ? "Unsaved" : "Ready")
              : "Required",
          detail: resetRequired ? "Previous target is complete" : parameters.detail,
        },
        {
          title: "Risk Check",
          state: risk.ready ? "ready" : "blocked",
          label: risk.ready ? "Ready" : "Blocked",
          detail: risk.detail,
        },
        lifecycleWorkflowStep(lifecycle, {
          title: "Run State",
          state: configuredLive ? "live" : "idle",
          label: configuredLive ? "Live" : "Stopped",
          detail: `${runtime.mode || data.mode || "dry_run"} · ${runtime.status || data.status || "disabled"}`,
        }),
      ]);
      const riskButton = document.getElementById("rebalance-open-risk");
      if (riskButton) riskButton.hidden = risk.ready;
      const startButton = document.getElementById("rebalance-live-confirm");
      if (startButton) {
        startButton.hidden = configuredLive;
        startButton.disabled = rebalanceFormBusy || resetRequired || !parameters.ready || !risk.ready;
        startButton.classList.remove("is-confirmed");
        startButton.setAttribute("aria-pressed", "false");
        startButton.textContent = uiText("Review & Start Live");
      }
      const stopButton = document.getElementById("rebalance-stop");
      if (stopButton) {
        stopButton.hidden = !configuredLive;
        stopButton.disabled = rebalanceFormBusy;
      }
      const resetButton = document.getElementById("rebalance-reset");
      const acknowledgeButton = document.getElementById("rebalance-acknowledge-exposure");
      if (acknowledgeButton) {
        const residual = runtime.residual_exposure || {};
        const canAcknowledge = runtime.halted
          && runtime.halt_reason === "hedge_required"
          && Number(residual.quantity_base || 0) > 0;
        acknowledgeButton.hidden = !canAcknowledge;
        acknowledgeButton.disabled = rebalanceFormBusy || !canAcknowledge;
      }
      const hasProgress = Number(runtime.completed_quote_common || 0) > 0
        || Number(runtime.completed_destination_quote_common || 0) > 0
        || Number(runtime.completed_base || 0) > 0;
      if (resetButton) {
        resetButton.disabled = rebalanceFormBusy || configuredLive || !hasProgress;
      }
    }

    function liveRebalanceValidationError(data, payload, requireConfirmation = true) {
      if (!payload.live_enabled) return "";
      if (data?.runtime?.residual_exposure_acknowledged) {
        return "Residual exposure was acknowledged. Stop Live Ready, reset progress, then complete a new live confirmation before restarting.";
      }
      if (rebalanceProgressRequiresReset(data, payload)) {
        return "Previous task is complete. Turn off Live Ready and reset progress before starting a new task.";
      }
      const risk = rebalanceRiskReadiness(data);
      if (!risk.globalReady) {
        return "Global live trading is blocked in Risk Controls.";
      }
      if (!risk.strategyReady) {
        return "Enable Cross-Exchange Rebalance in Risk Controls before starting live.";
      }
      if (!risk.accountsReady) {
        return "The source or destination account is disabled in Risk Controls.";
      }
      if (requireConfirmation && !rebalanceLiveConfirmed) {
        return "Review and confirm the live settings before starting.";
      }
      return "";
    }

    function liveRebalanceConfirmationDetail(payload) {
      const common = lastState?.config?.common_quote_currency || "USD";
      return [
        `${uiText("Cash Source")}: ${payload.buy_exchange} · ${payload.buy_symbol}`,
        `${uiText("Cash Destination")}: ${payload.sell_exchange} · ${payload.sell_symbol}`,
        `${uiText("Source Spend USD").replace("USD", common)}: ${money.format(payload.total_quote_common)}`,
        `${uiText("Per Cycle Source USD").replace("USD", common)}: ${money.format(payload.quote_per_cycle_common)}`,
        `${uiText("Max Cost bps")}: ${fmt.format(payload.max_cost_bps)}`,
        `${uiText("Max Slippage bps")}: ${fmt.format(payload.max_slippage_bps)}`,
      ].join("\n");
    }

    async function confirmLiveRebalance() {
      if (rebalanceFormBusy) return;
      const payload = {
        ...crossExchangeRebalancePayloadFromForm(),
        enabled: true,
        live_enabled: true,
      };
      const parameters = rebalanceFormReadiness(payload);
      if (!parameters.ready) {
        setRebalanceFeedback(parameters.detail, "error");
        renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
        return;
      }
      const validationError = liveRebalanceValidationError(
        lastState?.cross_exchange_rebalance,
        payload,
        false,
      );
      if (validationError) {
        setRebalanceFeedback(validationError, "error");
        renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
        return;
      }
      let preflight;
      try {
        setRebalanceFeedback("Running live preflight...");
        preflight = await runStrategyPreflight("cross_exchange_rebalance", payload);
      } catch (error) {
        setRebalanceFeedback(error.message || String(error), "error");
        return;
      }
      if (!dangerConfirm(
        "Confirm live rebalance with these settings?",
        `${liveRebalanceConfirmationDetail(payload)}\n${uiText("Preflight")}: ${preflight.checks?.length || 0} ${uiText("checks passed")}`,
      )) return;
      rebalanceLiveConfirmed = true;
      payload.confirm_live = LIVE_REBALANCE_CONFIRMATION;
      payload.preflight_token = preflight.token;
      document.getElementById("rebalance-enabled").checked = true;
      document.getElementById("rebalance-live-enabled").checked = true;
      await submitCrossExchangeRebalance(
        payload,
        "Live rebalance started.",
      );
    }

    function updateRebalanceUnitLabels(data = lastState?.cross_exchange_rebalance) {
      const config = data?.config || {};
      const plan = data?.plan || data?.runtime?.last_payload?.plan || {};
      const common = plan.common_quote_currency || lastState?.config?.common_quote_currency || "USD";
      const buySymbol = selectedStrategySymbol("rebalance-buy") || config.buy_symbol || "";
      const sellSymbol = selectedStrategySymbol("rebalance-sell") || config.sell_symbol || "";
      text(
        "rebalance-total-label",
        uiText("Source Spend USD").replace("USD", common),
      );
      text(
        "rebalance-cycle-label",
        uiText("Per Cycle Source USD").replace("USD", common),
      );
      text(
        "rebalance-buy-reserve-label",
        `${uiText("Source Cash Reserve")} ${quoteCurrency(buySymbol) || "Quote"}`,
      );
      text(
        "rebalance-sell-reserve-label",
        `${uiText("Destination Token Reserve")} ${baseCurrency(sellSymbol) || "Base"}`,
      );
    }

    function renderCrossExchangeRebalanceConfig(data) {
      const config = data?.config;
      if (!config || rebalanceFormDirty || rebalanceFormBusy) {
        updateCoreFormStates();
        updateRebalanceUnitLabels(data);
        return;
      }
      document.getElementById("rebalance-enabled").checked = Boolean(config.enabled);
      document.getElementById("rebalance-live-enabled").checked = Boolean(config.live_enabled);
      renderStrategyAccounts(
        "rebalance-buy-accounts",
        "rebalance-buy",
        data.accounts,
        config.buy_exchange || "",
        config.buy_symbol || "",
        () => {
          rebalanceFormDirty = true;
          updateRebalanceUnitLabels(data);
        },
      );
      renderStrategyAccounts(
        "rebalance-sell-accounts",
        "rebalance-sell",
        data.accounts,
        config.sell_exchange || "",
        config.sell_symbol || "",
        () => {
          rebalanceFormDirty = true;
          updateRebalanceUnitLabels(data);
        },
      );
      setNumericField("rebalance-total", config.total_quote_common || 0);
      setNumericField("rebalance-cycle", config.quote_per_cycle_common || 0);
      setNumericField("rebalance-interval", config.interval_seconds || 30);
      setNumericField("rebalance-ttl", config.order_ttl_seconds ?? 2);
      setNumericField("rebalance-max-cost", config.max_cost_bps ?? 50);
      setNumericField("rebalance-max-slippage", config.max_slippage_bps ?? 50);
      setNumericField("rebalance-buy-reserve", config.buy_quote_reserve || 0);
      setNumericField("rebalance-sell-reserve", config.sell_base_reserve || 0);
      document.getElementById("rebalance-coordinate-mm").checked = config.coordinate_market_maker !== false;
      setNumericField("rebalance-coordination-timeout", config.coordination_timeout_seconds ?? 30);
      document.getElementById("rebalance-block-orders").checked = config.block_conflicting_open_orders !== false;
      document.getElementById("rebalance-halt-error").checked = config.halt_on_error !== false;
      updateRebalanceUnitLabels(data);
      renderRebalanceReadiness(data);
      updateCoreFormStates();
    }

    function crossExchangeRebalancePayloadFromForm() {
      return {
        action: "update",
        enabled: document.getElementById("rebalance-enabled").checked,
        live_enabled: document.getElementById("rebalance-live-enabled").checked,
        buy_exchange: selectedStrategyAccount("rebalance-buy"),
        buy_symbol: selectedStrategySymbol("rebalance-buy"),
        sell_exchange: selectedStrategyAccount("rebalance-sell"),
        sell_symbol: selectedStrategySymbol("rebalance-sell"),
        total_quote_common: numericValue("rebalance-total"),
        quote_per_cycle_common: numericValue("rebalance-cycle"),
        interval_seconds: numericValue("rebalance-interval"),
        order_ttl_seconds: numericValue("rebalance-ttl"),
        max_cost_bps: numericValue("rebalance-max-cost"),
        max_slippage_bps: numericValue("rebalance-max-slippage"),
        buy_quote_reserve: numericValue("rebalance-buy-reserve"),
        sell_base_reserve: numericValue("rebalance-sell-reserve"),
        coordinate_market_maker: document.getElementById("rebalance-coordinate-mm").checked,
        coordination_timeout_seconds: numericValue("rebalance-coordination-timeout"),
        block_conflicting_open_orders: document.getElementById("rebalance-block-orders").checked,
        halt_on_error: document.getElementById("rebalance-halt-error").checked,
        confirm_live: rebalanceLiveConfirmed ? LIVE_REBALANCE_CONFIRMATION : "",
      };
    }

    async function submitCrossExchangeRebalance(payload, successMessage) {
      if (rebalanceFormBusy) return false;
      const previousConfig = lastState?.cross_exchange_rebalance?.config || {};
      rebalanceFormBusy = true;
      setRebalanceFeedback();
      updateCoreFormStates();
      renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
      try {
        const res = await fetch("/api/cross-exchange-rebalance", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "rebalance update failed");
        rebalanceFormDirty = false;
        invalidateLiveRebalanceConfirmation();
        setRebalanceFeedback(successMessage, "ok");
        await refresh({ force: true });
        return true;
      } catch (error) {
        document.getElementById("rebalance-enabled").checked = Boolean(previousConfig.enabled);
        document.getElementById("rebalance-live-enabled").checked = Boolean(previousConfig.live_enabled);
        invalidateLiveRebalanceConfirmation();
        setRebalanceFeedback(error.message || String(error), "error");
        return false;
      } finally {
        rebalanceFormBusy = false;
        renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
        updateCoreFormStates();
      }
    }

    async function applyCrossExchangeRebalanceConfig(event) {
      event.preventDefault();
      if (rebalanceFormBusy) return;
      const payload = crossExchangeRebalancePayloadFromForm();
      const parameters = rebalanceFormReadiness(payload);
      if (!parameters.ready) {
        setRebalanceFeedback(parameters.detail, "error");
        renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
        return;
      }
      if (payload.live_enabled) {
        const validationError = liveRebalanceValidationError(
          lastState?.cross_exchange_rebalance,
          payload,
          false,
        );
        if (validationError) {
          setRebalanceFeedback(validationError, "error");
          renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
          return;
        }
        let preflight;
        try {
          setRebalanceFeedback("Running live preflight...");
          preflight = await runStrategyPreflight("cross_exchange_rebalance", payload);
        } catch (error) {
          setRebalanceFeedback(error.message || String(error), "error");
          return;
        }
        if (!dangerConfirm(
          "Apply these changes to the running live rebalance?",
          liveRebalanceConfirmationDetail(payload),
        )) return;
        rebalanceLiveConfirmed = true;
        payload.confirm_live = LIVE_REBALANCE_CONFIRMATION;
        payload.preflight_token = preflight.token;
      }
      await submitCrossExchangeRebalance(payload, "Rebalance settings saved.");
    }

    async function stopCrossExchangeRebalance() {
      if (rebalanceFormBusy) return;
      const payload = {
        ...crossExchangeRebalancePayloadFromForm(),
        enabled: false,
        live_enabled: false,
        confirm_live: "",
      };
      if (!dangerConfirm(
        "Stop the live rebalance after the current operation?",
        `${payload.buy_exchange} ${payload.buy_symbol} -> ${payload.sell_exchange} ${payload.sell_symbol}`,
      )) return;
      document.getElementById("rebalance-enabled").checked = false;
      document.getElementById("rebalance-live-enabled").checked = false;
      await submitCrossExchangeRebalance(payload, "Rebalance stop requested.");
    }

    async function resetCrossExchangeRebalanceProgress() {
      if (lastState?.cross_exchange_rebalance?.config?.live_enabled) {
        setRebalanceFeedback("Stop the live rebalance before resetting progress.", "error");
        return;
      }
      if (!dangerConfirm("Reset cross-exchange rebalance progress?")) return;
      const button = document.getElementById("rebalance-reset");
      button.disabled = true;
      try {
        const res = await fetch("/api/cross-exchange-rebalance", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "reset", confirm_reset: "RESET REBALANCE" }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "rebalance reset failed");
        setRebalanceFeedback("Rebalance progress reset. Review settings before enabling live.", "ok");
        await refresh({ force: true });
      } catch (error) {
        setRebalanceFeedback(error.message || String(error), "error");
      } finally {
        button.disabled = false;
        renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
      }
    }

    async function acknowledgeRebalanceExposure() {
      if (rebalanceFormBusy) return;
      const runtime = lastState?.cross_exchange_rebalance?.runtime || {};
      const residual = runtime.residual_exposure || {};
      const asset = residual.asset || baseCurrency(
        lastState?.cross_exchange_rebalance?.config?.buy_symbol || "",
      );
      const quantity = Number(residual.quantity_base || 0);
      if (!(quantity > 0)) {
        setRebalanceFeedback("Residual exposure amount is unavailable.", "error");
        return;
      }
      if (!dangerConfirm(
        "Acknowledge residual exposure?",
        `${fmt.format(quantity)} ${asset}\nNo order will be placed. MM may resume, but rebalance remains blocked until reset and a new live confirmation.`,
      )) return;
      const button = document.getElementById("rebalance-acknowledge-exposure");
      rebalanceFormBusy = true;
      button.disabled = true;
      try {
        const res = await fetch("/api/cross-exchange-rebalance", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action: "acknowledge_exposure",
            confirm_acknowledgement: "ACKNOWLEDGE RESIDUAL EXPOSURE",
          }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "exposure acknowledgement failed");
        setRebalanceFeedback("Residual exposure acknowledged. Rebalance remains blocked.", "ok");
        await refresh({ force: true });
      } catch (error) {
        setRebalanceFeedback(error.message || String(error), "error");
      } finally {
        rebalanceFormBusy = false;
        renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
        updateCoreFormStates();
      }
    }

    function renderCrossExchangeRebalance(data) {
      const runtime = data?.runtime || {};
      const lastPayload = runtime.last_payload || {};
      const plan = data?.plan || lastPayload.plan || null;
      const status = runtime.status || data?.status || "disabled";
      const mode = runtime.mode || data?.mode || "dry_run";
      const target = Number(data?.config?.total_quote_common || plan?.target_quote_common || 0);
      const completed = Number(runtime.completed_quote_common || 0);
      const destinationReceived = Number(runtime.completed_destination_quote_common || 0);
      const remaining = Math.max(0, Number(runtime.remaining_quote_common ?? target - completed));
      const progressPct = Number(runtime.progress_pct ?? (target > 0 ? completed / target * 100 : 0));
      const common = plan?.common_quote_currency || lastState?.config?.common_quote_currency || "USD";
      const coordination = lastPayload.coordination || {};
      const coordinationStatus = coordination.status || "";
      const reason = runtime.halt_reason
        || (lastPayload.risk?.reasons || [])[0]
        || (lastPayload.errors || [])[0]
        || "";
      text(
        "rebalance-meta",
        `${mode} · ${status} · ${progressPct.toFixed(1)}%${plan ? ` · cost ${Number(plan.expected_cost_bps || 0).toFixed(2)} bps` : ""}${coordinationStatus ? ` · MM ${coordinationStatus}` : ""}${reason ? ` · ${reason}` : ""}`,
      );
      const progress = document.getElementById("rebalance-progress");
      const residual = runtime.residual_exposure || {};
      const acknowledgedResidual = Boolean(runtime.residual_exposure_acknowledged);
      progress.innerHTML = `
        <span class="config-chip ${runtime.halted ? "config-diff" : "config-match"}">${escapeHtml(status)}</span>
        <span>${uiText("Source spent")} ${escapeHtml(common)} ${money.format(completed)} / ${money.format(target)} · ${uiText("remaining")} ${money.format(remaining)}</span>
        <span>${uiText("Destination received")} ${escapeHtml(common)} ${money.format(destinationReceived)}</span>
        <span>${escapeHtml(baseCurrency(plan?.buy_symbol || data?.config?.buy_symbol || ""))} ${fmt.format(runtime.completed_base || 0)}</span>
        ${Number(residual.quantity_base || 0) > 0 ? `<span>${acknowledgedResidual ? "Acknowledged" : "Residual"}: ${escapeHtml(fmt.format(residual.quantity_base))} ${escapeHtml(residual.asset || "")}</span>` : ""}
        ${coordinationStatus ? `<span>${escapeHtml(uiText("MM coordination"))}: ${escapeHtml(coordinationStatus)}</span>` : ""}
      `;

      const body = document.getElementById("rebalance-plan");
      body.innerHTML = "";
      if (!plan) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="8">${escapeHtml(uiText("No rebalance plan."))}</td>`;
        body.appendChild(tr);
        return;
      }
      const rows = [
        {
          role: "Cash Source",
          exchange: plan.buy_exchange,
          symbol: plan.buy_symbol,
          side: "buy",
          price: plan.buy_average_price,
          base: plan.quantity_base,
          local: `${plan.buy_quote_currency} ${fmt.format(plan.buy_cost_local)}`,
          common: `${common} ${money.format(plan.buy_cost_common)}`,
        },
        {
          role: "Cash Destination",
          exchange: plan.sell_exchange,
          symbol: plan.sell_symbol,
          side: "sell",
          price: plan.sell_average_price,
          base: plan.quantity_base,
          local: `${plan.sell_quote_currency} ${fmt.format(plan.sell_proceeds_local)}`,
          common: `${common} ${money.format(plan.sell_proceeds_common)}`,
        },
      ];
      for (const row of rows) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(uiText(row.role))}</td>
          <td>${escapeHtml(row.exchange || "--")}</td>
          <td>${escapeHtml(row.symbol || "--")}</td>
          <td class="${row.side === "buy" ? "side-buy" : "side-sell"}">${row.side.toUpperCase()}</td>
          <td class="num">${fmt.format(row.price || 0)}</td>
          <td class="num">${fmt.format(row.base || 0)}</td>
          <td class="num">${escapeHtml(row.local)}</td>
          <td class="num">${escapeHtml(row.common)}</td>
        `;
        body.appendChild(tr);
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

    function backtestPayloadFromForm() {
      return {
        action: "create",
        project_id: document.getElementById("backtest-project").value,
        strategy_id: document.getElementById("backtest-strategy").value,
        account_id: document.getElementById("backtest-account").value,
        timeframe: document.getElementById("backtest-timeframe").value,
        history_bars: numericValue("backtest-history-bars"),
        initial_cash: numericValue("backtest-cash"),
        initial_base: numericValue("backtest-base"),
        fee_bps: numericValue("backtest-fee"),
        slippage_bps: numericValue("backtest-slippage"),
        latency_bars: numericValue("backtest-latency-bars"),
      };
    }

    async function applyUserBacktest(event) {
      event.preventDefault();
      if (backtestFormBusy) return;
      backtestFormBusy = true;
      const button = document.getElementById("backtest-run");
      button.disabled = true;
      button.textContent = uiText("Starting");
      try {
        const payload = backtestPayloadFromForm();
        if (!payload.project_id || !payload.strategy_id || !payload.account_id) {
          throw new Error(uiText("Select a project, strategy, and assigned account."));
        }
        const res = await fetch("/api/user-backtests", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "backtest start failed");
        backtestFormDirty = false;
        selectedBacktestRunId = result.run?.id || "";
        userBacktestLastLoadedAt = Date.now();
        renderUserBacktests(result.backtests);
      } catch (error) {
        text("backtest-meta", `${uiText("Start failed")}: ${error.message || error}`);
      } finally {
        backtestFormBusy = false;
        button.textContent = uiText("Run Backtest");
        syncBacktestAccountOptions(document.getElementById("backtest-account").value, false);
      }
    }

    async function deleteUserBacktest(runId, button) {
      if (!dangerConfirm("Delete this backtest result?")) return;
      button.disabled = true;
      try {
        const response = await fetch("/api/user-backtests", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "delete", run_id: runId }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "backtest delete failed");
        if (selectedBacktestRunId === runId) selectedBacktestRunId = "";
        renderUserBacktests(payload.backtests);
      } catch (error) {
        text("backtest-meta", `${uiText("Delete failed")}: ${error.message || error}`);
        button.disabled = false;
      }
    }

    async function createAutoBuySellTask() {
      if (slowFormBusy) return;
      const payload = {
        ...slowExecutionPayloadFromForm(),
        enabled: true,
        confirm_live: LIVE_AUTO_BUY_SELL_CONFIRMATION,
      };
      const parameters = slowExecutionFormReadiness(payload);
      const risk = coreLiveRiskReadiness("slow_execution", [payload.exchange]);
      if (!parameters.ready || !risk.ready) {
        setStrategyFeedback(
          "slow-feedback",
          parameters.ready ? risk.detail : parameters.detail,
          "error",
        );
        renderSlowExecutionWorkflow(lastState?.slow_execution);
        return;
      }
      let preflight;
      try {
        setStrategyFeedback("slow-feedback", "Running live preflight...");
        preflight = await runStrategyPreflight("slow_execution", payload);
      } catch (error) {
        setStrategyFeedback("slow-feedback", error.message || String(error), "error");
        return;
      }
      if (!dangerConfirm(
        "Create and start this live Auto Buy/Sell task?",
        `${slowExecutionConfirmationDetail(payload)}\n${uiText("Preflight")}: ${preflight.checks?.length || 0} ${uiText("checks passed")}`,
      )) return;
      payload.preflight_token = preflight.token;
      slowFormBusy = true;
      const button = document.getElementById("slow-create-task");
      button.disabled = true;
      setStrategyFeedback("slow-feedback");
      updateCoreFormStates();
      renderSlowExecutionWorkflow(lastState?.slow_execution);
      try {
        const res = await fetch("/api/auto-buy-sell/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "create task failed");
        slowFormDirty = false;
        setStrategyFeedback("slow-feedback", "Auto Buy/Sell task started.", "ok");
        await refresh({ force: true });
      } catch (error) {
        setStrategyFeedback("slow-feedback", error.message || String(error), "error");
      } finally {
        button.disabled = false;
        slowFormBusy = false;
        updateCoreFormStates();
        renderSlowExecutionWorkflow(lastState?.slow_execution);
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

    function clearRefreshTimer() {
      if (refreshTimer) {
        window.clearTimeout(refreshTimer);
        refreshTimer = null;
      }
    }

    function nextRefreshDelayMs() {
      const base = PAGE_REFRESH_INTERVAL_MS[currentPage] || REFRESH_INTERVAL_MS;
      const multiplier = refreshFailureCount > 0
        ? Math.min(6, refreshFailureCount + 1)
        : 1;
      const jitter = Math.random() * REFRESH_JITTER_MS;
      return Math.min(REFRESH_FAILURE_BACKOFF_MS, base * multiplier + jitter);
    }

    function scheduleNextRefresh(delayMs) {
      clearRefreshTimer();
      if (document.hidden) return;
      const delay = typeof delayMs === "number" ? delayMs : nextRefreshDelayMs();
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        refresh();
      }, delay);
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

      const mmSelected = selectedMarketMakerInstance(data.market_maker) || data.market_maker;
      const mmInstances = marketMakerInstances(data.market_maker);
      const mmRuntime = mmSelected?.runtime || data.market_maker?.runtime || {};
      const mmPlan = mmSelected?.plan || data.market_maker?.plan;
      const mmRuntimeText = mmRuntime.status ? ` · ${mmRuntime.status} · open ${mmRuntime.open_order_count ?? 0} · placed ${mmRuntime.placed_count ?? 0} · canceled ${mmRuntime.canceled_count ?? 0}` : "";
      const mmMarketData = mmRuntime.market_data || mmSelected?.market_data || data.market_maker?.market_data || {};
      const mmWsText = mmMarketData.cache?.websocket_supported === false ? " · WS unsupported" : "";
      const mmMarketDataText = mmMarketData.source
        ? ` · ${String(mmMarketData.source).toUpperCase()}${mmMarketData.age_seconds == null ? "" : ` ${Number(mmMarketData.age_seconds).toFixed(2)}s`}${mmWsText}`
        : mmWsText;
      const mmQuote = mmSelected?.quote_conversion || data.market_maker?.quote_conversion;
      const mmQuoteText = mmQuote?.quote_currency ? ` · quote ${mmQuote.quote_currency}${mmQuote.quote_to_common_rate == null ? "" : `→${mmQuote.common_quote_currency} ${mmQuote.quote_to_common_rate}`}` : "";
      const mmFeatures = mmSelected?.exchange_features || data.market_maker?.exchange_features || {};
      const mmFeatureText = Object.keys(mmFeatures).length ? ` · post-only ${mmFeatures.post_only ? "yes" : "no"}` : "";
      const mmSpreadText = mmPlan?.existing_spread_bps == null
        ? "--"
        : Number(mmPlan.existing_spread_bps).toFixed(2);
      const mmInstanceText = mmInstances.length > 1 ? `${mmInstances.length} instances · ` : "";
      const mmReason = marketMakerStatusReason(mmSelected) || marketMakerStatusReason(data.market_maker);
      const mmReasonText = mmReason ? ` · ${mmReason}` : "";
      text("mm-meta", mmPlan ? `${mmInstanceText}${mmSelected?.mode || data.market_maker?.mode || "dry_run"} · ${mmPlan.exchange} ${mmPlan.symbol} · mid ${fmt.format(mmPlan.mid_price)} · spread ${mmSpreadText} bps${mmMarketDataText}${mmQuoteText}${mmFeatureText}${mmRuntimeText}${mmReasonText}` : `${mmInstanceText}${mmSelected?.status || data.market_maker?.status || "disabled"}${mmMarketDataText}${mmQuoteText}${mmFeatureText}${mmRuntimeText}${mmReasonText}`);

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

      renderPortfolio(data.portfolio);
      renderStrategySummaries(data);
    }

    function finishVisiblePageRender() {
      applyMobileTableLabels();
      updateCoreFormStates();
    }

    function renderVisiblePage(data, page = currentPage, options = {}) {
      const activePage = PAGE_IDS.has(page) ? page : "status";
      const now = Date.now();
      const minIntervalMs = PAGE_RENDER_INTERVAL_MS[activePage] || 1000;
      if (!options.force && lastVisibleRenderAt[activePage] && now - lastVisibleRenderAt[activePage] < minIntervalMs) {
        return;
      }
      lastVisibleRenderAt[activePage] = now;
      if (activePage === "trading") {
        if (Array.isArray(data.market_limits)) currentMarketLimits = data.market_limits;
        renderOpenSection("strategy-settings-cards", () => renderStrategySettingCards(data));
        renderOpenSection("mm-orders", () => {
          renderMarketMakerConfig(data.market_maker);
          renderMarketMakerSafety(data.market_maker);
          renderMarketMaker(data.market_maker);
        });
        renderOpenSection("slow-orders", () => {
          renderSlowExecutionConfig(data.slow_execution?.config, data.slow_execution?.accounts);
          renderSlowExecution(data.slow_execution);
          renderSlowExecutionTasks(data.slow_execution?.tasks, data.slow_execution?.config);
        });
        renderOpenSection("rebalance-plan", () => {
          renderCrossExchangeRebalanceConfig(data.cross_exchange_rebalance);
          renderCrossExchangeRebalance(data.cross_exchange_rebalance);
        });
        renderOpenSection("markets-config", () => renderMarketsConfig(data));
        finishVisiblePageRender();
        return;
      }
      if (activePage === "quant") {
        renderOpenSection("carry-config", () => renderCashCarryConfig(data));
        renderOpenSection("funding-arb-form", () => renderFundingArbitragePanel(data.strategy_center));
        renderOpenSection("signal-bot-form", () => renderSignalBotPanel(data.strategy_center));
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
          renderBacktestSelectors(data.user_workspace);
          if (currentUserBacktests) renderUserBacktests(currentUserBacktests);
          else renderUserBacktests({ active_count: 0, runs: [], selected: null });
          loadUserBacktests();
        });
        renderOpenSection("derivatives-risk", () => renderDerivativesRisk(data.derivatives));
        renderOpenSection("funding-basis", () => renderFundingBasis(data.funding_basis));
        renderOpenSection("contract-strategies", () => renderContractStrategies(data.contract_strategies));
        renderOpenSection("options-arbitrage", () => renderOptionsArbitrage(data.options_arbitrage));
        finishVisiblePageRender();
        return;
      }
      if (activePage === "settings") {
		        renderOpenSection("user-workspace-section", () => renderUserWorkspace(data.user_workspace));
        renderOpenSection("risk-form", () => renderRiskControls(data.operations || { risk: data.config?.risk }, data.trading_console));
        renderOpenSection("config-version-section", () => loadConfigVersions());
        renderOpenSection("strategy-instances", () => renderStrategyCenter(data.strategy_center));
        renderOpenSection("api-accounts", () => renderApiAccountsPanel(data.strategy_center));
        finishVisiblePageRender();
        return;
      }
      if (activePage === "records") {
        renderOpenSection("console-strategies", () => renderTradingConsole(data.trading_console, data.order_activity));
        renderOpenSection("open-orders", () => renderOrderActivity(data.order_activity));
        renderOpenSection("strategy-timeline", () => renderRiskEvents(data.operations));
        renderOpenSection("audit-events", () => renderAuditTrail(data.operations));
        renderOpenSection("holder-changes", () => renderHolders(data.onchain));
        finishVisiblePageRender();
        return;
      }
      renderOpenSection("readiness-actions", () => renderReadiness(data.readiness, data.runtime_store));
      renderOpenSection("markets", () => renderMarkets(data.markets));
      renderOpenSection("account-balances", () => renderAccountBalances(data.account_balances));
      renderOpenSection("rates", () => renderRates(data.quote_rates));
      renderOpenSection("opportunities", () => renderOpportunities(data.opportunities));
      renderOpenSection("holders", () => renderHolders(data.onchain));
      finishVisiblePageRender();
    }

    async function refresh(options = {}) {
      if (refreshInFlight) {
        if (options.force) refreshQueued = true;
        return;
      }
      refreshInFlight = true;
      let redirecting = false;
      const requestedPage = PAGE_IDS.has(currentPage) ? currentPage : "status";
      try {
        const params = new URLSearchParams({ view: requestedPage });
        const sectionIds = openSectionIdsForPage(requestedPage);
        params.set("sections", sectionIds.join(","));
        const stateUrl = `/api/state?${params.toString()}`;
        const res = await fetchWithTimeout(stateUrl, { cache: "no-store" });
        if (res.status === 401) {
          redirecting = true;
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
        ensureStateStream();
      } catch (error) {
        refreshFailureCount += 1;
        const message = error?.name === "AbortError"
          ? "state request timed out"
          : (error?.message || String(error || "state request failed"));
        if (!refreshHadSuccess) {
          setHeaderStatus("degraded", "Retrying");
          text("warnings", `Connecting to server: ${message}`);
        } else if (refreshFailureCount < 2) {
          // A single missed poll on a healthy session is usually a transient
          // blip; retry silently instead of flashing the header pill.
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
        } else if (!redirecting) {
          scheduleNextRefresh();
        }
      }
    }

    // ---- Server-Sent Events state stream (with polling fallback) ----
    // The stream pushes the same payload as /api/state on a fixed interval.
    // Polling stays armed as a watchdog: every stream message pushes the next
    // poll out to 3x the page interval, so if the stream stalls or errors the
    // regular polling cadence resumes automatically.
    let stateStream = null;
    let stateStreamKey = "";
    let stateStreamDisabledUntil = 0;
    const STATE_STREAM_RETRY_COOLDOWN_MS = 60000;

    function stateStreamActive() {
      return stateStream !== null && stateStream.readyState !== 2;
    }

    function closeStateStream() {
      if (stateStream) {
        stateStream.close();
        stateStream = null;
        stateStreamKey = "";
      }
    }

    function ensureStateStream() {
      if (!window.EventSource) return;
      if (Date.now() < stateStreamDisabledUntil) return;
      if (document.hidden) {
        closeStateStream();
        return;
      }
      const requestedPage = PAGE_IDS.has(currentPage) ? currentPage : "status";
      const params = new URLSearchParams({ view: requestedPage });
      const sectionIds = openSectionIdsForPage(requestedPage);
      params.set("sections", sectionIds.join(","));
      const baseIntervalMs = PAGE_REFRESH_INTERVAL_MS[requestedPage] || REFRESH_INTERVAL_MS;
      params.set("interval", String(baseIntervalMs / 1000));
      const key = params.toString();
      if (stateStreamActive() && stateStreamKey === key) return;
      closeStateStream();
      const source = new EventSource(`/api/state/stream?${key}`);
      stateStream = source;
      stateStreamKey = key;
      source.onmessage = (event) => {
        if (source !== stateStream) return;
        let data = null;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (!data || typeof data !== "object" || Array.isArray(data)) return;
        refreshHadSuccess = true;
        refreshFailureCount = 0;
        lastState = data;
        pageStateCache[requestedPage] = data;
        renderCommonState(data);
        if (requestedPage === currentPage) {
          renderVisiblePage(data, requestedPage, { force: false });
        }
        scheduleNextRefresh(baseIntervalMs * 3);
      };
      source.onerror = () => {
        if (source !== stateStream) return;
        if (source.readyState === 2) {
          // Hard failure (auth, proxy, unsupported): stop trying for a
          // while and let polling carry the updates.
          closeStateStream();
          stateStreamDisabledUntil = Date.now() + STATE_STREAM_RETRY_COOLDOWN_MS;
        }
        scheduleNextRefresh();
      };
    }

    applyFeatureVisibility();
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
	    document.getElementById("risk-form").addEventListener("input", markRiskFormDirty);
	    document.getElementById("user-risk-profile-form").addEventListener("input", () => {
	      userRiskProfileDirty = true;
	    });
	    document.getElementById("user-risk-profile-form").addEventListener("submit", applyUserRiskProfile);
	    document.getElementById("user-project-form").addEventListener("input", () => {
	      userProjectFormDirty = true;
	    });
	    document.getElementById("user-project-form").addEventListener("submit", applyUserProject);
	    document.getElementById("user-project-new").addEventListener("click", resetUserProjectForm);
	    document.getElementById("user-exchange-account-form").addEventListener("input", () => {
	      userExchangeAccountFormDirty = true;
	    });
	    document.getElementById("user-exchange-account-form").addEventListener("change", (event) => {
	      userExchangeAccountFormDirty = true;
	      if (event.target?.id === "user-exchange-project") {
	        const project = workspaceProject(event.target.value);
	        syncUserExchangeMarketTypes("", "", project?.symbol || "");
	      } else if ([
	        "user-exchange-id",
	        "user-exchange-market-type",
	        "user-exchange-api-variant",
	      ].includes(event.target?.id)) {
	        syncUserExchangeMarketTypes();
	      }
	    });
	    document.getElementById("user-exchange-account-form").addEventListener("submit", applyUserExchangeAccount);
	    document.getElementById("user-exchange-new").addEventListener("click", resetUserExchangeAccountForm);
	    document.getElementById("user-exchange-load-markets").addEventListener("click", loadUserExchangeMarkets);
	    document.getElementById("user-exchange-test").addEventListener("click", testSelectedUserExchangeAccount);
    document.getElementById("user-strategy-new").addEventListener("click", () => openUserStrategyForm());
    document.getElementById("user-strategy-cancel").addEventListener("click", closeUserStrategyForm);
    document.getElementById("user-strategy-form").addEventListener("input", () => {
      userStrategyFormDirty = true;
    });
    document.getElementById("user-strategy-form").addEventListener("change", (event) => {
      userStrategyFormDirty = true;
      if (event.target?.id === "user-strategy-project") {
        renderUserStrategyAccountOptions([]);
      } else if (event.target?.id === "user-strategy-type") {
        syncUserStrategyTypeFields({ applyDefaults: true });
      } else if (
        event.target?.matches("#user-strategy-accounts input[type='checkbox']")
        && event.target.checked
      ) {
        const definition = workspaceStrategyDefinition(
          document.getElementById("user-strategy-type").value
        );
        if (Number(definition?.max_accounts || 0) === 1) {
          const accountInputs = document.querySelectorAll(
            "#user-strategy-accounts input[type='checkbox']"
          );
          accountInputs.forEach((input) => {
            if (input !== event.target) input.checked = false;
          });
        }
      }
    });
    document.getElementById("user-strategy-form").addEventListener("submit", applyUserStrategy);
	    document.getElementById("markets-form").addEventListener("submit", addSpotMarket);
    document.getElementById("carry-form").addEventListener("submit", addCashCarryPair);
    document.getElementById("risk-form").addEventListener("submit", applyRiskConfig);
    document.getElementById("mm-form").addEventListener("input", (event) => {
      if (event.target?.id === "mm-instance") return;
      markMarketMakerFormDirty();
    });
    document.getElementById("mm-form").addEventListener("change", (event) => {
      if (event.target?.id === "mm-instance") return;
      markMarketMakerFormDirty();
    });
    document.getElementById("mm-instance").addEventListener("change", (event) => {
      selectedMarketMakerInstanceId = event.target.value || "";
      mmFormDirty = false;
      renderMarketMakerConfig(lastState?.market_maker);
    });
    document.getElementById("mm-add").addEventListener("click", addMarketMakerInstance);
    document.getElementById("mm-copy").addEventListener("click", copyMarketMakerInstance);
    document.getElementById("mm-delete").addEventListener("click", deleteMarketMakerInstance);
    document.getElementById("mm-form").addEventListener("submit", applyMarketMakerConfig);
    document.getElementById("mm-start").addEventListener("click", startMarketMaker);
    document.getElementById("mm-stop").addEventListener("click", stopMarketMaker);
    document.getElementById("mm-open-risk").addEventListener(
      "click",
      () => openSettingsSection("risk-section"),
    );
    document.getElementById("slow-form").addEventListener("input", markSlowFormDirty);
    document.getElementById("slow-form").addEventListener("change", markSlowFormDirty);
    document.getElementById("slow-side").addEventListener("change", updateSlowLabels);
    document.getElementById("slow-form").addEventListener("submit", applySlowExecutionConfig);
    document.getElementById("rebalance-form").addEventListener("input", () => {
      rebalanceFormDirty = true;
      invalidateLiveRebalanceConfirmation();
      setRebalanceFeedback();
      renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
      updateCoreFormStates();
    });
    document.getElementById("rebalance-form").addEventListener("change", () => {
      rebalanceFormDirty = true;
      invalidateLiveRebalanceConfirmation();
      setRebalanceFeedback();
      updateRebalanceUnitLabels();
      renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
      updateCoreFormStates();
    });
    document.getElementById("rebalance-form").addEventListener(
      "submit",
      applyCrossExchangeRebalanceConfig,
    );
    document.getElementById("rebalance-reset").addEventListener(
      "click",
      resetCrossExchangeRebalanceProgress,
    );
    document.getElementById("rebalance-acknowledge-exposure").addEventListener(
      "click",
      acknowledgeRebalanceExposure,
    );
    document.getElementById("rebalance-live-confirm").addEventListener(
      "click",
      confirmLiveRebalance,
    );
    document.getElementById("rebalance-stop").addEventListener(
      "click",
      stopCrossExchangeRebalance,
    );
    document.getElementById("rebalance-open-risk").addEventListener(
      "click",
      () => openSettingsSection("risk-section"),
    );
    window.addEventListener("crypto-arb-language-change", () => {
      updateSlowLabels();
      updateRebalanceUnitLabels();
      setRebalanceFeedback(rebalanceFeedbackMessage, rebalanceFeedbackLevel);
      renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
      renderMarketMakerWorkflow(lastState?.market_maker);
      renderSlowExecutionWorkflow(lastState?.slow_execution);
      if (lastState) renderSpotArbitrageWorkflow(lastState);
      if (!rebalanceFormDirty && lastState?.cross_exchange_rebalance) {
        for (const id of ["rebalance-buy-accounts", "rebalance-sell-accounts"]) {
          const selector = document.getElementById(id);
          if (selector) selector.dataset.signature = "";
        }
        renderCrossExchangeRebalanceConfig(lastState.cross_exchange_rebalance);
        renderCrossExchangeRebalance(lastState.cross_exchange_rebalance);
      }
      if (lastState) renderStrategySettingCards(lastState);
      renderUserStrategies(currentUserWorkspace);
      renderBacktestSelectors(currentUserWorkspace);
      if (currentUserBacktests) renderUserBacktests(currentUserBacktests);
      const userStrategyForm = document.getElementById("user-strategy-form");
      if (userStrategyForm && !userStrategyForm.hidden) {
        const strategyType = document.getElementById("user-strategy-type").value;
        const selectedAccounts = selectedUserStrategyAccountIds();
        workspaceStrategyTypeOptions(strategyType);
        renderUserStrategyAccountOptions(selectedAccounts);
      }
      applyMobileTableLabels();
      updateCoreFormStates();
    });
    window.addEventListener("crypto-arb-theme-change", () => {
      if (currentUserBacktests) renderUserBacktests(currentUserBacktests);
    });
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
    document.getElementById("backtest-project").addEventListener("change", () => {
      syncBacktestStrategyOptions("", "");
    });
    document.getElementById("backtest-strategy").addEventListener("change", () => {
      syncBacktestAccountOptions("", true);
    });
    document.getElementById("backtest-account").addEventListener("change", () => {
      syncBacktestAccountOptions(document.getElementById("backtest-account").value, false);
    });
    document.getElementById("backtest-form").addEventListener("submit", applyUserBacktest);
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
    document.getElementById("slow-open-risk").addEventListener(
      "click",
      () => openSettingsSection("risk-section"),
    );
    document.getElementById("slow-clear-terminal").addEventListener("click", clearTerminalAutoBuySellTasks);
    document.getElementById("spot-open-risk").addEventListener(
      "click",
      () => openSettingsSection("risk-section"),
    );
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearRefreshTimer();
        closeStateStream();
      } else {
        refresh({ force: true });
      }
    });
