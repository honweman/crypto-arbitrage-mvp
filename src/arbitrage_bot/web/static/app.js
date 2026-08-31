const priceNumber = new Intl.NumberFormat("en-US", { maximumFractionDigits: 10 });
    const wholeNumberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
    const wholeNumber = {
      format(value) {
        const numeric = Number(value);
        return wholeNumberFormatter.format(Math.round(numeric) === 0 ? 0 : numeric);
      },
    };
    const fmt = priceNumber;
    const money = wholeNumber;
    const wholeQuantity = wholeNumber;
	    const PAGE_IDS = new Set(["status", "trading", "quant", "settings", "records"]);
	    const CORE_CONTROL_SECTION_IDS = new Set([
	      "user-market-maker-section",
	      "mm-section",
	      "slow-section",
	      "rebalance-section",
	      "spot-arbitrage-section",
	    ]);
	    let currentPage = pageFromLocation();
	    let lastState = null;
	    let lastReliablePortfolioPerformance = null;
	    let accountBalanceDetailPayload = null;
	    let accountBalanceDetailLoadedAt = 0;
	    let accountBalanceDetailLoading = false;
	    let accountBalanceDetailSignature = "";
	    let headerStatusIssue = null;
	    let refreshQueued = false;
	    const pageStateCache = {};
	    const PAGE_RENDER_INTERVAL_MS = { status: 750, trading: 2000, quant: 4000, settings: 3000, records: 2000 };
	    const PAGE_REFRESH_INTERVAL_MS = { status: 1000, trading: 3000, quant: 6000, settings: 5000, records: 3500 };
	    const REFRESH_INTERVAL_MS = PAGE_REFRESH_INTERVAL_MS.status;
	    const BALANCE_DETAIL_REFRESH_INTERVAL_MS = 3000;
	    const REFRESH_FAILURE_BACKOFF_MS = 15000;
	    const REFRESH_JITTER_MS = 300;
	    const LIVE_AUTO_BUY_SELL_CONFIRMATION = "ENABLE LIVE AUTO BUY SELL";
	    const LIVE_MARKET_MAKER_CONFIRMATION = "ENABLE LIVE MARKET MAKER";
	    const LIVE_REBALANCE_CONFIRMATION = "ENABLE LIVE REBALANCE";
	    const BALANCE_MIN_QUANTITY = 1;
	    const BALANCE_MIN_VALUE_USDT = 10;
	    const USD_STABLE_CURRENCIES = new Set([
	      "USD", "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USDP",
	      "DAI", "PYUSD", "USDE", "USDS", "USD1", "RLUSD", "GUSD",
	      "FRAX", "LUSD",
	    ]);
	    let refreshTimer = null;
	    let mutationRefreshTimer = null;
      let marketTickerPayload = null;
      let marketTickerDraft = [];
      let marketTickerLoading = false;
      let marketTickerLoadedAt = 0;
	    const PAGE_SECTION_IDS = {
	      status: [
	        "overview",
	        "readiness-actions",
	        "markets",
	        "rates",
	        "opportunities",
	        "holders",
	      ],
	      trading: [
	        "user-market-maker",
	        "strategy-settings-cards",
	        "mm-orders",
	        "slow-orders",
	        "rebalance-plan",
	        "markets-config",
	      ],
	      quant: [
	        "user-quant-strategies",
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
	        "user-market-maker-section",
	        "strategy-settings-section",
	        "mm-section",
	        "slow-section",
	        "rebalance-section",
	        "spot-arbitrage-section",
	      ],
	      quant: [
	        "quant-page-heading",
	        "user-quant-strategies-section",
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
    let userStrategyViewFilter = "";

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

    function applyRoleVisibility(auth) {
      const ownerMode = auth?.mode === "user" && auth?.role !== "admin";
      document.body.classList.toggle("owner-mode", ownerMode);
      document.querySelectorAll("[data-platform-only]").forEach((element) => {
        element.classList.toggle("owner-hidden", ownerMode);
        element.setAttribute("aria-hidden", ownerMode ? "true" : "false");
      });
      document.querySelectorAll("[data-owner-only]").forEach((element) => {
        element.classList.toggle("role-hidden", !ownerMode);
        element.setAttribute("aria-hidden", ownerMode ? "false" : "true");
      });
      const tradingBadge = document.querySelector("#trading-page-heading .page-mode-badge");
      const quantBadge = document.querySelector("#quant-page-heading .page-mode-badge");
      if (tradingBadge) {
        tradingBadge.textContent = uiText(ownerMode ? "My accounts · live confirmation required" : "Risk gated");
      }
      if (quantBadge) {
        quantBadge.textContent = uiText(ownerMode ? "My strategies · live + paper" : "Paper by default");
      }
      mountUserStrategyLab(currentPage);
    }

    function mountUserStrategyLab(page = currentPage) {
      const ownerMarketMaker = document.body.classList.contains("owner-mode")
        && page === "trading";
      const host = document.getElementById(
        ownerMarketMaker ? "user-market-maker" : "user-quant-strategies"
      );
      const lab = document.getElementById("user-strategy-lab");
      if (!host || !lab) return;
      userStrategyViewFilter = ownerMarketMaker ? "market_maker" : "";
      if (lab.parentElement !== host) host.appendChild(lab);
      const title = lab.querySelector(".workspace-strategy-header strong");
      if (title) title.textContent = uiText(ownerMarketMaker ? "My Market Maker" : "User Strategies");
      const createButton = document.getElementById("user-strategy-new");
      if (createButton) createButton.textContent = uiText(ownerMarketMaker ? "New MM" : "New Strategy");
      const typeSelect = document.getElementById("user-strategy-type");
      if (typeSelect) typeSelect.disabled = ownerMarketMaker;
      if (currentUserWorkspace) renderUserStrategies(currentUserWorkspace);
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
	      if (page === "status") {
	        const overview = document.getElementById("overview");
	        const onchain = document.getElementById("onchain-monitor-section");
	        if (overview && onchain) overview.insertAdjacentElement("afterend", onchain);
	        const balances = document.getElementById("account-balances-section");
	        if (overview && balances) overview.insertAdjacentElement("afterend", balances);
	      }
      for (const id of PAGE_DOM_ORDER[page] || []) {
        const section = document.getElementById(id);
        if (section) main.appendChild(section);
      }
    }

		    function setActivePage(page, options = {}) {
		      const activePage = PAGE_IDS.has(page) ? page : "status";
		      currentPage = activePage;
		      mountUserStrategyLab(activePage);
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
	      if (activePage === "status") loadAccountBalanceDetails({ force: true });
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
      if (el) el.textContent = friendlyAccountMessage(value);
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
      const ownerRisk = sectionId === "risk-section"
        && document.body.classList.contains("owner-mode");
      const resolvedSectionId = ownerRisk ? "user-workspace-section" : sectionId;
      const section = document.getElementById(resolvedSectionId);
      if (!section || isUiFeatureHidden(section)) return;
      const targetPage = PAGE_IDS.has(section.dataset.page) ? section.dataset.page : "settings";
      if (currentPage !== targetPage) setActivePage(targetPage, { refresh: false });
      closeOtherCoreControlSections(resolvedSectionId);
      section.classList.add("section-open");
      const title = section.querySelector(".section-title");
      if (title) title.setAttribute("aria-expanded", "true");
      refreshOpenedSection(section);
      const focus = ownerRisk
        ? document.getElementById("user-risk-profile-form")?.closest("details")
        : section;
      if (ownerRisk && focus) focus.open = true;
      (focus || section).scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function dangerConfirm(message, detail = "") {
      const fullMessage = detail ? `${uiText(message)}\n\n${detail}` : uiText(message);
      return window.confirm(fullMessage);
    }

    function formatAge(ts) {
      if (!ts) return "--";
      const age = Math.max(0, Date.now() / 1000 - ts);
      return age < 60 ? `${age.toFixed(0)}s ago` : `${(age / 60).toFixed(0)}m ago`;
    }

    function formatDurationSeconds(value) {
      const seconds = Math.max(0, Number(value || 0));
      if (seconds < 60) return `${Math.ceil(seconds)}s`;
      if (seconds < 3600) return `${Math.ceil(seconds / 60)}m`;
      return `${(seconds / 3600).toFixed(0)}h`;
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
      const textValue = uiText(friendlyAccountMessage(message)).trim();
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
      updateSlowLeverageHint();
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
      if (row.reason) parts.push(friendlyAccountMessage(row.reason));
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

    function coreAccountRows() {
      return [
        ...(lastState?.market_maker?.accounts || []),
        ...(lastState?.slow_execution?.accounts || []),
        ...(lastState?.cross_exchange_rebalance?.accounts || []),
        ...(lastState?.trading_console?.accounts || []),
      ].filter((row) => row && row.key);
    }

    function accountLabelForKey(key) {
      const accountKey = String(key || "");
      if (!accountKey) return "";
      const account = coreAccountRows().find((row) => row.key === accountKey);
      return account?.label || accountKey;
    }

    function displayExchange(exchange, explicitLabel = "") {
      const label = String(explicitLabel || "").trim();
      if (label && !label.startsWith("workspace:")) return label;
      return accountLabelForKey(exchange) || String(exchange || "");
    }

    function friendlyAccountMessage(message) {
      let textValue = String(message || "");
      const labels = new Map(
        coreAccountRows().map((row) => [String(row.key), String(row.label || row.key)]),
      );
      for (const [key, label] of [...labels.entries()].sort((left, right) => right[0].length - left[0].length)) {
        if (key && label !== key) textValue = textValue.split(key).join(label);
      }
      return textValue;
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
      else if (!accountsReady) detail = `${accountLabelForKey(blockedAccount)} ${uiText("account risk switch is off")}`;
      return {
        ready: globalReady && strategyReady && accountsReady,
        globalReady,
        strategyReady,
        accountsReady,
        detail,
      };
    }

    function slowDerivativeRiskReadiness(payload) {
      if (payload.instrument_type !== "perpetual" || payload.position_effect !== "open") {
        return {
          ready: true,
          detail: uiText("Reduce Only is not blocked by the opening leverage limit."),
          limit: Number(lastState?.operations?.risk?.max_derivative_leverage
            ?? lastState?.config?.risk?.max_derivative_leverage
            ?? 0),
        };
      }
      const risk = lastState?.operations?.risk || lastState?.config?.risk || {};
      const limit = Number(risk.max_derivative_leverage || 0);
      const requested = Number(payload.leverage || 0);
      if (!(limit > 0)) {
        return {
          ready: false,
          limit,
          detail: `${uiText("Perpetual opening is disabled. Set Risk Controls > Max Leverage to at least")} ${wholeNumber.format(requested || 1)}x`,
        };
      }
      if (!(requested > 0) || requested > limit + 1e-9) {
        return {
          ready: false,
          limit,
          detail: `${uiText("Requested leverage exceeds the global risk maximum")}: ${wholeNumber.format(requested)}x > ${wholeNumber.format(limit)}x`,
        };
      }
      return {
        ready: true,
        limit,
        detail: `${uiText("Global risk maximum leverage")}: ${wholeNumber.format(limit)}x`,
      };
    }

    function updateSlowLeverageHint() {
      const hint = document.getElementById("slow-leverage-hint");
      const input = document.getElementById("slow-leverage");
      if (!hint || !input) return;
      const check = slowDerivativeRiskReadiness(slowExecutionPayloadFromForm());
      hint.textContent = check.detail;
      hint.classList.toggle("risk-warning", !check.ready);
      if (check.limit > 0) input.max = String(check.limit);
      else input.removeAttribute("max");
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
        const blockers = Array.isArray(preflight.blockers)
          ? preflight.blockers.map((message) => friendlyAccountMessage(message))
          : [];
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
      const formatter = mode === "quote" ? money : wholeQuantity;
      return `${currency} ${formatter.format(Number(value || 0))}`;
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
      return `${prefix}${wholeNumber.format(value)}`;
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
          <td>${escapeHtml(displayExchange(row.exchange, row.exchange_label))}</td>
          <td>${row.symbol}</td>
          <td class="${row.status === "ok" ? "ok" : "missing"}">${row.status}</td>
          <td class="num">${row.bid == null ? "--" : fmt.format(row.bid)}</td>
          <td class="num">${row.ask == null ? "--" : fmt.format(row.ask)}</td>
          <td class="num">${row.bid_common == null ? "--" : fmt.format(row.bid_common)}</td>
          <td class="num">${row.ask_common == null ? "--" : fmt.format(row.ask_common)}</td>
          <td class="num">${row.bid_size == null ? "--" : wholeQuantity.format(row.bid_size)}</td>
          <td class="num">${row.ask_size == null ? "--" : wholeQuantity.format(row.ask_size)}</td>
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
      return wholeQuantity.format(Number(value));
    }

    function finiteOrderNumber(value) {
      if (value == null || typeof value === "function" || typeof value === "object") return null;
      const numeric = Number(value);
      return Number.isFinite(numeric) ? numeric : null;
    }

    function formatOrderNumber(value, currency = "") {
      const numeric = finiteOrderNumber(value);
      if (numeric == null) return "--";
      const formatted = wholeNumber.format(numeric);
      return currency ? `${formatted} ${currency}` : formatted;
    }

    function formatOrderPrice(value, currency = "") {
      const numeric = finiteOrderNumber(value);
      if (numeric == null) return "--";
      const formatted = priceNumber.format(numeric);
      return currency ? `${formatted} ${currency}` : formatted;
    }

    function formatOrderQuantity(value, currency = "") {
      const numeric = finiteOrderNumber(value);
      if (numeric == null) return "--";
      const formatted = wholeQuantity.format(numeric);
      return currency ? `${formatted} ${currency}` : formatted;
    }

    function orderOpenNotional(order) {
      const explicit = finiteOrderNumber(order?.open_notional);
      if (explicit != null) return explicit;
      const price = finiteOrderNumber(order?.price);
      const remaining = finiteOrderNumber(order?.remaining ?? order?.amount);
      return price == null || remaining == null ? null : price * remaining;
    }

    function formatBps(value) {
      if (value == null) return "--";
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return "--";
      return `${numeric.toFixed(0)} bps`;
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

    function balanceCommonValue(row) {
      const total = Number(row?.total || 0);
      if (!Number.isFinite(total)) return null;
      for (const field of ["value_common", "usd_value", "value_usd"]) {
        if (row?.[field] == null) continue;
        const explicit = Number(row?.[field]);
        if (Number.isFinite(explicit)) return explicit;
      }
      const currency = String(row?.currency || "").toUpperCase();
      if (!currency) return null;
      const rate = Number(lastState?.quote_rates?.[currency]);
      if (Number.isFinite(rate) && rate > 0) return total * rate;
      if (USD_STABLE_CURRENCIES.has(currency)) return total;
      const position = (lastState?.portfolio?.positions || []).find(
        (item) => String(item?.asset || "").toUpperCase() === currency
      );
      const mark = Number(position?.mark_price);
      if (Number.isFinite(mark) && mark > 0) return total * mark;
      const market = (lastState?.markets || []).find((item) => {
        const symbol = String(item?.symbol || "").split(":", 1)[0];
        return symbol.split("/", 1)[0].toUpperCase() === currency;
      });
      const bid = Number(market?.bid_common);
      const ask = Number(market?.ask_common);
      if (Number.isFinite(bid) && bid > 0 && Number.isFinite(ask) && ask > 0) {
        return total * ((bid + ask) / 2);
      }
      if (Number.isFinite(bid) && bid > 0) return total * bid;
      if (Number.isFinite(ask) && ask > 0) return total * ask;
      return null;
    }

    function meetsBalanceDisplayThreshold(amount, valueCommon) {
      const quantity = Number(amount);
      if (!Number.isFinite(quantity) || Math.abs(quantity) < BALANCE_MIN_QUANTITY) {
        return false;
      }
      if (valueCommon == null) return false;
      const value = Number(valueCommon);
      return Number.isFinite(value) && Math.abs(value) >= BALANCE_MIN_VALUE_USDT;
    }

    function isDisplayableBalance(row) {
      const currency = String(row?.currency || "").toUpperCase();
      return Boolean(currency)
        && meetsBalanceDisplayThreshold(row?.total, balanceCommonValue(row));
    }

    function visibleBalanceRows(rows) {
      const source = rows || [];
      const visible = source.filter(isDisplayableBalance);
      return { visible, hiddenCount: source.length - visible.length };
    }

    function hiddenBalanceText(hiddenCount) {
      return hiddenCount > 0
        ? `${hiddenCount} ${uiText("balance(s) below 1 unit, 10 USDT, or without price hidden")}`
        : "";
    }

    function accountBalancesForProfile(accountBalances) {
      if (!accountBalances) return accountBalances;
      const selectedId = document.getElementById("profile-account")?.value || "";
      if (!selectedId) return accountBalances;
      const accounts = (accountBalances.accounts || []).filter(
        (account) => accountBalanceFilterKey(account) === selectedId
      );
      const totalsByCurrency = new Map();
      for (const account of accounts) {
        for (const row of account.balance?.currencies || []) {
          const currency = String(row.currency || "").toUpperCase();
          if (!currency) continue;
          const total = totalsByCurrency.get(currency) || {
            currency,
            free: 0,
            used: 0,
            total: 0,
            open_order_reserved: 0,
          };
          total.free += Number(row.free || 0);
          total.used += Number(row.used || 0);
          total.total += Number(row.total || 0);
          total.open_order_reserved += Number(row.open_order_reserved || 0);
          totalsByCurrency.set(currency, total);
        }
      }
      return {
        ...accountBalances,
        accounts,
        totals: [...totalsByCurrency.values()],
        checked_account_count: accounts.filter((account) => account.balance?.checked).length,
        total_account_count: accounts.length,
      };
    }

    function accountBalanceFilterKey(account) {
      const workspaceId = String(account?.workspace_connection_id || "").trim();
      if (workspaceId) return `workspace:${workspaceId}`;
      const exchange = String(account?.exchange || "").trim();
      return exchange ? `platform:${exchange}` : "";
    }

    function selectedBalanceCurrency() {
      return String(document.getElementById("balance-currency-filter")?.value || "")
        .trim()
        .toUpperCase();
    }

    function syncBalanceCurrencyFilter(accountBalances) {
      const select = document.getElementById("balance-currency-filter");
      if (!select) return;
      const previous = String(select.value || "").toUpperCase();
      const currencies = [...new Set(
        (accountBalances?.accounts || []).flatMap((account) =>
          (account.balance?.currencies || [])
            .filter(isDisplayableBalance)
            .map((row) => String(row.currency || "").toUpperCase())
            .filter(Boolean)
        )
      )].sort();
      const signature = currencies.join("|");
      if (select.dataset.signature === signature) return;
      select.dataset.signature = signature;
      select.innerHTML = `<option value="">${escapeHtml(uiText("All currencies"))}</option>`;
      for (const currency of currencies) {
        const option = document.createElement("option");
        option.value = currency;
        option.textContent = currency;
        select.appendChild(option);
      }
      select.value = currencies.includes(previous) ? previous : "";
    }

    function accountBalanceValue(rows) {
      if (!(rows || []).length) return null;
      const values = (rows || []).map(balanceCommonValue);
      if (values.some((value) => value == null)) return null;
      return values.reduce((total, value) => total + Number(value || 0), 0);
    }

    function aggregateBalanceCurrencies(rows) {
      const grouped = new Map();
      for (const row of rows || []) {
        const currency = String(row?.currency || "").trim().toUpperCase();
        if (!currency) continue;
        const target = grouped.get(currency) || {
          currency,
          free: 0,
          used: 0,
          total: 0,
          open_order_reserved: 0,
          value_common: 0,
          value_common_complete: true,
          wallets: new Set(),
        };
        target.free += Number(row.free || 0);
        target.used += Number(row.used || 0);
        target.total += Number(row.total || 0);
        target.open_order_reserved += Number(row.open_order_reserved || 0);
        const valueCommon = balanceCommonValue(row);
        if (valueCommon == null) target.value_common_complete = false;
        else target.value_common += Number(valueCommon);
        if (row.wallet) target.wallets.add(String(row.wallet).toLowerCase());
        grouped.set(currency, target);
      }
      return new Map([...grouped].map(([currency, row]) => {
        const result = { ...row, wallets: [...row.wallets].sort() };
        if (!row.value_common_complete) delete result.value_common;
        delete result.value_common_complete;
        return [currency, result];
      }));
    }

    function balanceAccountHeader(account) {
      const status = String(account.status || "unknown");
      const marketType = String(account.market_type || "spot").toUpperCase();
      const issue = account.balance?.error
        || (account.errors || [])[0]
        || (account.warnings || [])[0]
        || (account.symbols || []).join(", ");
      return `
        <span class="balance-account-head" title="${escapeHtml(issue)}">
          <strong>${escapeHtml(displayExchange(account.exchange, account.label))}</strong>
          <small class="${balanceStatusClass(status)}">${escapeHtml(marketType)} · ${escapeHtml(status)} · ${escapeHtml(formatAge(account.checked_at || accountBalanceDetailPayload?.last_finished))}</small>
        </span>
      `;
    }

    function renderAccountBalanceSummary(accountBalances) {
      const { visible: visibleTotals, hiddenCount } = visibleBalanceRows(accountBalances?.totals || []);
      const totals = sortBalanceCurrencies(visibleTotals);
      const valueEl = document.getElementById("account-balances-total");
      const detailEl = document.getElementById("account-balances-detail");
      if (totals.length === 0) {
        valueEl.textContent = "--";
        detailEl.textContent = hiddenBalanceText(hiddenCount) || accountBalances?.status || "--";
        detailEl.title = detailEl.textContent;
        return;
      }

      valueEl.textContent = totals.length === 1
        ? `${formatBalanceAmount(totals[0].total)} ${totals[0].currency}`
        : `${totals.length} currencies`;
      const accountCount = Number(accountBalances?.total_account_count || 0);
      const balanceDetail = totals
        .slice(0, 5)
        .map((row) => `${row.currency} ${formatBalanceAmount(row.total)}`)
        .join(" · ");
      const detail = [
        accountCount > 0 ? `${uiText("All accounts")} (${accountCount})` : "",
        balanceDetail,
        hiddenBalanceText(hiddenCount),
      ].filter(Boolean).join(" · ");
      detailEl.textContent = detail;
      const totalsTitle = totals
        .map((row) => {
          const reserved = Number(row.open_order_reserved || 0);
          const reserveText = reserved > 0 ? ` · reserved ${formatBalanceAmount(reserved)}` : "";
          return `${row.currency} free ${formatBalanceAmount(row.free)} · used ${formatBalanceAmount(row.used)} · total ${formatBalanceAmount(row.total)}${reserveText}`;
        })
        .join(" | ");
      detailEl.title = [
        accountCount > 0 ? `${uiText("All accounts")} (${accountCount})` : "",
        totalsTitle,
        hiddenBalanceText(hiddenCount),
      ].filter(Boolean).join(" | ");
    }

    function renderAccountBalances(accountBalances) {
      const filtered = accountBalancesForProfile(accountBalances);
      syncBalanceCurrencyFilter(filtered);
      const currencyFilter = selectedBalanceCurrency();
      const filteredRows = (filtered?.accounts || []).flatMap(
        (account) => account.balance?.currencies || []
      );
      const hiddenCount = visibleBalanceRows(filteredRows).hiddenCount;
      text(
        "account-balances-meta",
        filtered
          ? [
            `${filtered.status || "unknown"} · checked ${filtered.checked_account_count || 0}/${filtered.total_account_count || 0} · ${formatAge(filtered.last_finished)}`,
            hiddenBalanceText(hiddenCount),
          ].filter(Boolean).join(" · ")
          : ""
      );

      const accounts = filtered?.accounts || [];
      const commonCurrency = String(lastState?.config?.common_quote_currency || "USD").toUpperCase();
      const signature = JSON.stringify([
        filtered?.last_finished || 0,
        currencyFilter,
        document.getElementById("profile-account")?.value || "",
        accounts.map((account) => [
          accountBalanceFilterKey(account),
          account.status,
          account.checked_at,
          (account.balance?.currencies || []).map((row) => [
            row.currency,
            row.free,
            row.used,
            row.total,
            row.open_order_reserved,
            row.value_common,
          ]),
        ]),
      ]);
      if (accountBalanceDetailSignature === signature) return;
      accountBalanceDetailSignature = signature;

      const head = document.getElementById("account-balances-head");
      const body = document.getElementById("account-balances");
      const foot = document.getElementById("account-balances-foot");
      const table = body.closest("table");
      const columnCount = accounts.length + 4;
      table.style.minWidth = `${Math.max(720, 430 + accounts.length * 190)}px`;
      head.innerHTML = `
        <tr>
          <th>${escapeHtml(uiText("Currency"))}</th>
          <th class="num">${escapeHtml(uiText("Total"))}</th>
          <th class="num">${escapeHtml(uiText("Price"))}</th>
          <th class="num">${escapeHtml(uiText("Value"))}</th>
          ${accounts.map((account) => `<th class="num balance-account-column">${balanceAccountHeader(account)}</th>`).join("")}
        </tr>
      `;
      body.innerHTML = "";
      foot.innerHTML = "";
      if (accounts.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="${columnCount}">${escapeHtml(uiText("No account balances yet."))}</td>`;
        body.appendChild(tr);
        return;
      }

      const accountCurrencyMaps = accounts.map((account) =>
        aggregateBalanceCurrencies(account.balance?.currencies || [])
      );
      const visibleMaps = accountCurrencyMaps.map((currencyMap) => new Map(
        [...currencyMap].filter(([, row]) => isDisplayableBalance(row))
      ));
      const currencyNames = [...new Set(
        visibleMaps.flatMap((currencyMap) => [...currencyMap.keys()])
      )];
      const currencies = sortBalanceCurrencies(
        currencyNames.map((currency) => ({ currency }))
      ).map((row) => row.currency).filter(
        (currency) => !currencyFilter || currency === currencyFilter
      );

      for (const currency of currencies) {
        const accountRows = visibleMaps.map((currencyMap) => currencyMap.get(currency) || null);
        const combined = aggregateBalanceCurrencies(accountRows.filter(Boolean)).get(currency);
        const valueCommon = combined ? balanceCommonValue(combined) : null;
        const total = Number(combined?.total || 0);
        const priceCommon = valueCommon != null && total ? Number(valueCommon) / total : null;
        const tr = document.createElement("tr");
        const accountCells = accountRows.map((row) => {
          if (!row) return `<td class="num balance-matrix-cell balance-matrix-empty">--</td>`;
          const reserved = Number(row.open_order_reserved || 0);
          const wallets = (row.wallets || []).join(", ") || "trading";
          const title = `${currency} · free ${formatBalanceAmount(row.free)} · used ${formatBalanceAmount(row.used)} · orders ${formatBalanceAmount(reserved)} · wallets ${wallets}`;
          return `
            <td class="num balance-matrix-cell" title="${escapeHtml(title)}">
              <strong>${formatBalanceAmount(row.total)}</strong>
              <small>${escapeHtml(uiText("Free"))} ${formatBalanceAmount(row.free)}${reserved > 0 ? ` · ${escapeHtml(uiText("In Orders"))} ${formatBalanceAmount(reserved)}` : ""}</small>
            </td>
          `;
        }).join("");
        tr.innerHTML = `
          <td class="balance-currency-cell"><strong>${escapeHtml(currency)}</strong></td>
          <td class="num"><strong>${formatBalanceAmount(total)}</strong></td>
          <td class="num">${priceCommon == null ? "--" : priceNumber.format(priceCommon)}</td>
          <td class="num">${valueCommon == null ? "--" : `${money.format(valueCommon)} ${escapeHtml(commonCurrency)}`}</td>
          ${accountCells}
        `;
        body.appendChild(tr);
      }
      if (!body.children.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="${columnCount}">${escapeHtml(uiText("No balances match this currency."))}</td>`;
        body.appendChild(tr);
        return;
      }

      const accountValues = visibleMaps.map((currencyMap) => accountBalanceValue([...currencyMap.values()]));
      const grandValue = accountValues.every((value) => value != null)
        ? accountValues.reduce((total, value) => total + Number(value || 0), 0)
        : null;
      foot.innerHTML = `
        <tr>
          <th>${escapeHtml(uiText("Account Value"))}</th>
          <td></td>
          <td></td>
          <td class="num"><strong>${grandValue == null ? "--" : `${money.format(grandValue)} ${escapeHtml(commonCurrency)}`}</strong></td>
          ${accountValues.map((value) => `<td class="num"><strong>${value == null ? "--" : money.format(value)}</strong><small>${escapeHtml(commonCurrency)}</small></td>`).join("")}
        </tr>
      `;
    }

    async function loadAccountBalanceDetails(options = {}) {
      if (currentPage !== "status" || accountBalanceDetailLoading) return;
      const force = Boolean(options.force);
      if (!force && accountBalanceDetailPayload
        && Date.now() - accountBalanceDetailLoadedAt < BALANCE_DETAIL_REFRESH_INTERVAL_MS) {
        return;
      }
      accountBalanceDetailLoading = true;
      const button = document.getElementById("account-balances-refresh");
      if (button) button.disabled = true;
      try {
        const response = await fetchWithTimeout(
          "/api/state?view=balances&sections=account-balances",
          { cache: "no-store" },
        );
        if (response.status === 401) {
          window.location.assign("/login");
          return;
        }
        if (!response.ok) throw new Error(`balance request failed (${response.status})`);
        const payload = await response.json();
        accountBalanceDetailPayload = payload.account_balances || null;
        accountBalanceDetailLoadedAt = Date.now();
        accountBalanceDetailSignature = "";
        if (payload.quote_rates && lastState) lastState.quote_rates = payload.quote_rates;
        renderProfileAccounts(accountBalanceDetailPayload);
        renderAccountBalanceSummary(accountBalancesForProfile(accountBalanceDetailPayload));
        renderAccountBalances(accountBalanceDetailPayload);
        applyMobileTableLabels();
      } catch (error) {
        if (!accountBalanceDetailPayload) {
          text("account-balances-meta", `${uiText("Data unavailable")} · ${error.message || error}`);
        }
      } finally {
        accountBalanceDetailLoading = false;
        if (button) button.disabled = false;
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
          <td>${escapeHtml(displayExchange(account.exchange, account.label))}</td>
            <td colspan="8">${escapeHtml(message)}</td>
            <td class="${balanceStatusClass(account.status)}">${escapeHtml(account.status || "--")}</td>
          `;
          body.appendChild(tr);
          continue;
        }
        for (const position of positions) {
          const funding = position.funding_rate == null ? "--" : `${(Number(position.funding_rate) * 100).toFixed(0)}%`;
          const buffer = position.liquidation_buffer_pct == null ? "--" : `${Number(position.liquidation_buffer_pct).toFixed(0)}%`;
          const reasons = (position.risk_reasons || []).join(" · ");
          const tr = document.createElement("tr");
          tr.innerHTML = `
          <td>${escapeHtml(displayExchange(account.exchange, account.label))}</td>
            <td>${escapeHtml(position.symbol || "--")}</td>
            <td class="${position.side === "long" ? "side-buy" : position.side === "short" ? "side-sell" : ""}">${escapeHtml(String(position.side || "--").toUpperCase())}</td>
            <td class="num">${position.notional_quote == null ? "--" : money.format(position.notional_quote)}</td>
            <td class="num">${position.leverage == null ? "--" : wholeNumber.format(position.leverage)}</td>
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
          .map((leg) => `${leg.side} ${leg.symbol} @ ${displayExchange(leg.exchange, leg.exchange_label)}`)
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
          <td>${escapeHtml(displayExchange(row.spot_exchange, row.spot_exchange_label) || "--")}<br><span class="subtle">${escapeHtml(row.spot_symbol || "--")}</span><br>${escapeHtml(displayExchange(row.derivative_exchange, row.derivative_exchange_label) || "--")}<br><span class="subtle">${escapeHtml(row.derivative_symbol || "--")}</span></td>
          <td class="num">${row.spot_mid == null ? "--" : fmt.format(row.spot_mid)}</td>
          <td class="num">${row.derivative_mid == null ? "--" : fmt.format(row.derivative_mid)}</td>
          <td class="num">${formatBps(row.basis_bps)}</td>
          <td class="num" title="${row.estimated_apr_pct == null ? "" : `APR ${Number(row.estimated_apr_pct).toFixed(0)}%`}">${formatBps(row.funding_rate_bps)}</td>
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
          `Mid ${formatMaybeNumber(signal.mid_price, priceNumber)}`,
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

    function formatMaybeNumber(value, formatter = wholeNumber) {
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
          <td>${escapeHtml(row.expiry || "--")}<br><span class="subtle">${row.days_to_expiry == null ? "--" : `${Number(row.days_to_expiry).toFixed(0)}d`} · K ${formatMaybeNumber(row.strike, priceNumber)}</span></td>
          <td>${escapeHtml(row.option_type || "--")}<br><span class="subtle">${escapeHtml(row.symbol || "--")}</span></td>
          <td class="num">${formatMaybeNumber(row.bid, priceNumber)} / ${formatMaybeNumber(row.ask, priceNumber)}</td>
          <td class="num">${formatMaybeNumber(row.mark_price, priceNumber)}<br><span class="subtle">${row.iv == null ? "IV --" : `IV ${formatMaybeNumber(row.iv)}`}</span></td>
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
      const cost = fee.cost == null ? "--" : money.format(fee.cost);
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
      const permissionAssets = auth?.permissions?.assets;
      const allowed = auth?.permission_model === "account_owner_v1"
        ? (permissionAssets || [])
        : (auth?.allowed_assets?.length ? auth.allowed_assets : available);
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
      applyRoleVisibility(auth);
    }

    function renderProfileAccounts(accountBalances) {
      const select = document.getElementById("profile-account");
      if (!select) return;
      const previous = select.value || "";
      const accounts = (accountBalances?.accounts || []).filter(
        (account) => accountBalanceFilterKey(account),
      );
      const signature = JSON.stringify(accounts.map((account) => [
        accountBalanceFilterKey(account),
        account.label,
        account.exchange,
        account.symbols,
      ]));
      if (select.dataset.signature === signature) return;
      select.dataset.signature = signature;
      select.innerHTML = "";
      const allOption = document.createElement("option");
      allOption.value = "";
      allOption.textContent = accounts.length
        ? `${uiText("All accounts")} (${accounts.length})`
        : uiText("No account balances yet.");
      select.appendChild(allOption);
      const seen = new Set();
      for (const account of accounts) {
        const key = accountBalanceFilterKey(account);
        if (seen.has(key)) continue;
        seen.add(key);
        const option = document.createElement("option");
        option.value = key;
        const ownerLabel = account.workspace_connection_id
          ? uiText("My API")
          : uiText("Platform account");
        option.textContent = `${displayExchange(account.exchange, account.label)} · ${ownerLabel}`;
        option.title = (account.symbols || [])
          .filter(Boolean)
          .join(" · ");
        select.appendChild(option);
      }
      select.value = seen.has(previous)
        ? previous
        : "";
      select.disabled = accounts.length === 0;
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
      const detail = `${displayExchange(order.exchange, order.label)} · ${order.symbol || "--"} · ${String(order.side || "--").toUpperCase()} · ${order.id || "--"}`;
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
        const base = baseCurrency(order.symbol);
        const quote = quoteCurrency(order.symbol);
        tr.innerHTML = `
          <td data-label="${uiText("Account")}">${escapeHtml(displayExchange(order.exchange, order.label))}</td>
          <td data-label="${uiText("Symbol")}">${escapeHtml(order.symbol || "--")}</td>
          <td data-label="${uiText("Side")}" class="${orderSideClass(order.side)}">${escapeHtml(order.side ? order.side.toUpperCase() : "--")}</td>
          <td data-label="${uiText("Status")}">${escapeHtml(order.status || "--")}</td>
          <td data-label="${uiText("Price")}" class="num">${formatOrderPrice(order.price, quote)}</td>
          <td data-label="${uiText("Order Qty")}" class="num">${formatOrderQuantity(order.amount, base)}</td>
          <td data-label="${uiText("Filled")}" class="num">${formatOrderQuantity(order.filled, base)}</td>
          <td data-label="${uiText("Remaining")}" class="num">${formatOrderQuantity(order.remaining, base)}</td>
          <td data-label="${uiText("Open Value")}" class="num">${formatOrderNumber(orderOpenNotional(order), quote)}</td>
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
        const base = baseCurrency(fill.symbol);
        const quote = quoteCurrency(fill.symbol);
        tr.innerHTML = `
          <td data-label="${uiText("Account")}">${escapeHtml(displayExchange(fill.exchange, fill.label))}</td>
          <td data-label="${uiText("Symbol")}">${escapeHtml(fill.symbol || "--")}</td>
          <td data-label="${uiText("Side")}" class="${orderSideClass(fill.side)}">${escapeHtml(fill.side ? fill.side.toUpperCase() : "--")}</td>
          <td data-label="${uiText("Source")}">${escapeHtml(fill.source_label || displaySource(fill.source))}</td>
          <td data-label="${uiText("Price")}" class="num">${formatOrderPrice(fill.price, quote)}</td>
          <td data-label="${uiText("Amount")}" class="num">${formatOrderQuantity(fill.amount, base)}</td>
          <td data-label="${uiText("Cost")}" class="num">${formatOrderNumber(fill.cost, quote)}</td>
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
          <td>${escapeHtml(displayExchange(issue.exchange, issue.exchange_label) || "--")}</td>
          <td>${escapeHtml(issue.symbol || "--")}</td>
          <td title="${escapeHtml(issue.order_id || "")}">${escapeHtml(shortId(issue.order_id))}</td>
          <td title="${escapeHtml(issue.source_id || "")}">${escapeHtml(friendlyAccountMessage(issue.message) || "--")}</td>
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
            ? `progress ${Number(row.progress_pct).toFixed(0)}%`
            : "";
        const avgFill = row.task_average_fill_price ?? row.average_fill_price;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td data-label="${uiText("Strategy")}">${escapeHtml(displayStrategy(row.strategy))}</td>
          <td data-label="${uiText("Instance")}" title="${escapeHtml(row.instance_id || "")}">${escapeHtml(shortId(row.instance_id || "default"))}</td>
          <td data-label="${uiText("Account / Symbol")}">${escapeHtml(row.account || "--")}<br><span class="subtle">${escapeHtml(row.symbol || "--")}</span></td>
          <td data-label="${uiText("Fills / Submitted")}" class="num">${Number(row.filled_order_count || 0)} / ${Number(row.submitted_order_count || 0)}</td>
          <td data-label="${uiText("Fill Rate")}" class="num">${row.fill_rate_pct == null ? "--" : `${Number(row.fill_rate_pct).toFixed(0)}%`}</td>
          <td data-label="${uiText("Average Fill")}" class="num">${avgFill == null ? "--" : fmt.format(avgFill)}</td>
          <td data-label="${uiText("Fees")}" class="num">${formatPnlValue(row.fees_common || 0)}</td>
          <td data-label="${uiText("P/L")}" class="num ${pnlClass(row.realized_pnl)}">${formatPnlValue(row.realized_pnl || 0)}${mmDetail ? `<br><span class="subtle">${escapeHtml(mmDetail)}</span>` : ""}</td>
          <td data-label="${uiText("Slippage")}" class="num">${row.average_slippage_bps == null ? "--" : `${Number(row.average_slippage_bps).toFixed(0)} bps`}</td>
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
        : `${uiText("Account")}: ${displayExchange(payload?.exchange) || "--"}`;
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
      const cancelAllowed = tradingConsole?.cancel_allowed !== false;
      body.hidden = !cancelAllowed;
      if (!cancelAllowed) return;
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
          <td data-label="${uiText("Account")}">${escapeHtml(displayExchange(strategy.exchange, strategy.exchange_label) || "--")}</td>
          <td data-label="${uiText("Symbol")}">${escapeHtml(strategy.symbol || "--")}</td>
          <td data-label="${uiText("Mode")}">${escapeHtml(strategy.mode || "--")}</td>
          <td data-label="${uiText("Action")}" class="strategy-action"></td>
        `;
        const action = tr.querySelector(".strategy-action");
        const button = document.createElement("button");
        button.className = strategy.paused ? "control-button" : "danger-button";
        button.type = "button";
        button.textContent = strategy.paused ? "Resume" : "Pause";
        if (strategy.owner_strategy_id) {
          const ownerStrategy = (currentUserWorkspace?.strategies || []).find(
            (row) => row.id === strategy.owner_strategy_id
          );
          button.disabled = !ownerStrategy;
          if (ownerStrategy) {
            button.addEventListener("click", () => toggleUserStrategy(ownerStrategy, button));
          }
        } else if (strategy.owner_auto_task_id) {
          button.addEventListener("click", () => controlAutoBuySellTask(
            strategy.owner_auto_task_id,
            strategy.paused ? "resume" : "pause",
            button
          ));
        } else {
          button.addEventListener("click", () => setStrategyPaused(strategy.id, !strategy.paused, button));
        }
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
      const cancelAllowed = tradingConsole?.cancel_allowed !== false;
      allButton.hidden = !cancelAllowed;
      allButton.disabled = !cancelAllowed || openOrders <= 0;
      allButton.onclick = cancelAllowed
        ? () => cancelBulkOrders({ scope: "all" }, allButton)
        : null;
      renderConsoleAccountActions(tradingConsole);
      renderConsoleStrategies(tradingConsole);
      renderOpenOrders(orderActivity, "console-open-orders", cancelAllowed);
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
          <td>${escapeHtml(displayExchange(strategy.exchange, strategy.exchange_label) || "--")}</td>
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
        option.textContent = displayExchange(exchange.key, exchange.label) || exchange.key;
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
          <td>${escapeHtml(displayExchange(market.exchange, market.exchange_label))}</td>
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

    function statusIssueRows(data = lastState) {
      if (!data || typeof data !== "object") return [];
      const issues = [];
      const seen = new Set();
      const add = ({ severity = "warning", title, reason, meta = [], action = "" }) => {
        const normalizedReason = friendlyAccountMessage(reason).trim();
        if (!normalizedReason) return;
        const normalizedTitle = String(title || "Warning").trim();
        const key = `${normalizedTitle}:${normalizedReason}`;
        if (seen.has(key)) return;
        seen.add(key);
        issues.push({
          severity,
          title: normalizedTitle,
          reason: normalizedReason,
          meta: meta.filter(Boolean),
          action: String(action || "").trim(),
        });
      };

      for (const warning of (data.warnings || [])) {
        add({ severity: "warning", title: "Warning", reason: warning });
      }
      if (data.error) {
        add({ severity: "error", title: "Error", reason: data.error });
      }

      for (const row of (data.strategy_lifecycle?.instances || [])) {
        if (row?.converged && row.convergence_state === "in_sync") continue;
        const severity = row.convergence_state === "error"
          ? "error"
          : row.convergence_state === "blocked"
            ? "blocked"
            : "attention";
        const meta = [
          row.account ? `${uiText("Account")}: ${displayExchange(row.account)}` : "",
          row.symbol ? `${uiText("Trading pair")}: ${row.symbol}` : "",
          row.mode ? `${uiText("Mode")}: ${row.mode}` : "",
          row.updated_at ? `${uiText("Updated")}: ${formatAge(row.updated_at)}` : "",
        ];
        const actions = (row.allowed_actions || [])
          .map((value) => uiText(String(value || "").replaceAll("_", " ")))
          .filter(Boolean)
          .join(" / ");
        add({
          severity,
          title: row.label || row.strategy_id || "Strategy",
          reason: row.reason || lifecycleDetail(row),
          meta,
          action: actions,
        });
      }

      const marketMaker = data.market_maker || {};
      const mmReason = marketMakerStatusReason(marketMaker);
      if (mmReason) {
        add({ severity: "error", title: "Market Maker", reason: mmReason });
      }

      if (data.auth?.role !== "admin") {
        for (const strategy of (data.user_workspace?.strategies || [])) {
          if (strategy?.strategy_type !== "market_maker" || strategy?.mode !== "live") continue;
          const runtime = strategy.live_runtime || {};
          const readiness = strategy.readiness || {};
          const blockers = Array.isArray(readiness.blockers) ? readiness.blockers : [];
          const status = String(runtime.status || "").toLowerCase();
          const hasProblem = [
            "blocked",
            "blocked_by_risk",
            "cancel_retry",
            "coordination_cancel_retry",
            "error",
            "execution_error",
            "open_order_sync_error",
            "reconciliation_required",
          ].includes(status) || (strategy.enabled && blockers.length > 0);
          if (!hasProblem) continue;
          const runtimeConfig = runtime.config || {};
          const account = (strategy.accounts || [])[0] || {};
          add({
            severity: status.includes("error") ? "error" : "blocked",
            title: strategy.name || "Market Maker",
            reason: runtime.status_reason || runtime.last_error || runtime.reason || blockers[0] || status || "risk check blocked",
            meta: [
              runtimeConfig.exchange || account.exchange
                ? `${uiText("Account")}: ${displayExchange(runtimeConfig.exchange || account.exchange, account.label)}`
                : "",
              runtimeConfig.symbol || account.symbol
                ? `${uiText("Trading pair")}: ${runtimeConfig.symbol || account.symbol}`
                : "",
            ],
          });
        }
      }

      const autoTasks = data.slow_execution?.tasks?.tasks || [];
      for (const task of autoTasks) {
        if (!["error", "blocked_by_risk", "recovering"].includes(task?.status || "")) continue;
        const config = task.config || {};
        add({
          severity: task.status === "error" ? "error" : "blocked",
          title: "Auto Buy/Sell",
          reason: task.last_error || task.status_reason || autoTaskLastOrderText(task, config),
          meta: [
            config.exchange ? `${uiText("Account")}: ${displayExchange(config.exchange)}` : "",
            config.symbol ? `${uiText("Trading pair")}: ${config.symbol}` : "",
          ],
        });
      }

      const reconciliation = data.order_activity?.reconciliation || {};
      for (const issue of (reconciliation.issues || [])) {
        add({
          severity: "error",
          title: "Orders",
          reason: issue.reason || issue.error || issue.detail || JSON.stringify(issue),
          meta: [
            issue.exchange ? `${uiText("Account")}: ${displayExchange(issue.exchange)}` : "",
            issue.symbol ? `${uiText("Trading pair")}: ${issue.symbol}` : "",
          ],
        });
      }
      if ((reconciliation.issue_count || 0) > 0 && !(reconciliation.issues || []).length) {
        add({
          severity: "error",
          title: "Orders",
          reason: reconciliation.reason || `${reconciliation.issue_count} ${uiText("order reconciliation issue(s)")}`,
        });
      }

      for (const nextAction of (data.readiness?.next_actions || [])) {
        if (nextAction.level !== "high" && nextAction.status !== "blocked") continue;
        add({
          severity: nextAction.status === "blocked" ? "blocked" : "warning",
          title: nextAction.action || "Risk",
          reason: nextAction.detail || nextAction.scope || "review required",
          meta: nextAction.scope ? [`${uiText("Scope")}: ${nextAction.scope}`] : [],
        });
      }
      return issues;
    }

    function statusIssuesWithConnectionState(data = lastState) {
      const issues = statusIssueRows(data);
      if (!headerStatusIssue) return issues;
      const duplicate = issues.some((issue) => issue.reason === headerStatusIssue.reason);
      return duplicate ? issues : [headerStatusIssue, ...issues];
    }

    function statusSeverityLabel(value) {
      const labels = {
        error: "Error",
        blocked: "Blocked",
        warning: "Warning",
        attention: "Attention",
      };
      return uiText(labels[value] || "Attention");
    }

    function openStatusDetails(preferredTitle = "") {
      const dialog = document.getElementById("status-detail-dialog");
      const summary = document.getElementById("status-detail-summary");
      const list = document.getElementById("status-detail-list");
      if (!dialog || !summary || !list) return;
      const issues = statusIssuesWithConnectionState();
      if (preferredTitle) {
        issues.sort((left, right) => (
          Number(right.title === preferredTitle) - Number(left.title === preferredTitle)
        ));
      }
      summary.textContent = issues.length
        ? `${issues.length} ${uiText("active issue(s)")}`
        : uiText("No active risk or error.");
      list.innerHTML = issues.length
        ? issues.map((issue) => `
          <article class="status-detail-item severity-${escapeHtml(issue.severity)}">
            <div class="status-detail-item-heading">
              <strong>${escapeHtml(uiText(issue.title))}</strong>
              <span>${escapeHtml(statusSeverityLabel(issue.severity))}</span>
            </div>
            <div class="status-detail-reason">
              <span>${escapeHtml(uiText("Reason"))}</span>
              <p>${escapeHtml(issue.reason)}</p>
            </div>
            ${issue.meta.length ? `<div class="status-detail-meta">${issue.meta.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</div>` : ""}
            ${issue.action ? `<div class="status-detail-action"><span>${escapeHtml(uiText("Available action"))}</span><strong>${escapeHtml(issue.action)}</strong></div>` : ""}
          </article>
        `).join("")
        : `<div class="status-detail-empty">${escapeHtml(uiText("No active risk or error."))}</div>`;
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }

    function renderStatusReasons(data) {
      const root = document.getElementById("status-reasons-section");
      if (!root) return;
      const issues = statusIssueRows(data);
      root.classList.toggle("has-items", issues.length > 0);
      root.innerHTML = issues
        .slice(0, 4)
        .map((issue) => `
          <button class="status-reason" type="button" data-status-title="${escapeHtml(issue.title)}" aria-haspopup="dialog">
            <strong>${escapeHtml(uiText(issue.title))}</strong>
            <span title="${escapeHtml(issue.reason)}">${escapeHtml(issue.reason)}</span>
          </button>
        `)
        .join("");
      root.querySelectorAll(".status-reason").forEach((button) => {
        button.addEventListener("click", () => openStatusDetails(button.dataset.statusTitle || ""));
      });
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
          ? `${displayExchange(mmPlan.exchange, mmPlan.exchange_label)} ${mmPlan.symbol} · mid ${fmt.format(mmPlan.mid_price)} · open ${mmRuntime.open_order_count ?? 0}${marketMakerStatusReason(marketMaker) ? ` · ${friendlyAccountMessage(marketMakerStatusReason(marketMaker))}` : ""}`
          : friendlyAccountMessage(marketMakerStatusReason(marketMaker) || marketMaker.error || mmRuntime.reason) || "--"
      );

      const auto = data.slow_execution || {};
      const autoTasks = auto.tasks?.tasks || [];
      const activeTasks = autoTasks.filter((task) => !["complete", "stopped", "below_min_order_quote"].includes(task.status));
      const autoStatus = auto.tasks
        ? `${activeTasks.length}/${autoTasks.length} active`
        : (auto.status || "disabled");
      const firstTask = activeTasks[0] || autoTasks[0];
      const autoDetail = firstTask
        ? `${String(firstTask.config?.side || "--").toUpperCase()} · ${firstTask.progress_pct == null ? "--" : firstTask.progress_pct.toFixed(0) + "%"} · ${firstTask.status || "--"}`
        : (auto.plan ? `${displayExchange(auto.plan.exchange, auto.plan.exchange_label)} ${auto.plan.symbol} · ${String(auto.plan.side || "").toUpperCase()}` : "--");
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
      const ownerMode = data.auth?.mode === "user" && data.auth?.role !== "admin";
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
            ? `${displayExchange(mmPlan.exchange, mmPlan.exchange_label) || "--"} ${mmPlan.symbol || "--"}`
            : `${marketMakerInstances(mm).length || 0} instance(s)`,
          detail: lifecycleCardDetail(mmLifecycle, mmPlan
            ? `mid ${fmt.format(mmPlan.mid_price)} · ${mmQuote} · open ${mmRuntime.open_order_count || 0}`
            : friendlyAccountMessage(marketMakerStatusReason(mm)) || "Open to edit ladder and risk"),
          target: ownerMode ? "user-market-maker-section" : "mm-section",
        },
        {
          title: "Auto Buy/Sell",
          status: autoLifecycle.worst?.actual_state || (activeTasks.length ? "running" : (auto.status || "disabled")),
          summary: activeTasks.length ? `${activeTasks.length}/${tasks.length} active task(s)` : (auto.status || "disabled"),
          detail: lifecycleCardDetail(autoLifecycle, firstTask
            ? `${displayExchange(firstTaskConfig.exchange, firstTaskConfig.exchange_label) || "--"} ${firstTaskConfig.symbol || "--"} · ${String(firstTaskConfig.side || "--").toUpperCase()} · ${firstTask.progress_pct == null ? "--" : firstTask.progress_pct.toFixed(0) + "%"} · ${autoProgressText}`
            : "Open to create or edit a task"),
          target: "slow-section",
        },
        {
          title: "Cross-Exchange Rebalance",
          status: rebalanceLifecycle.worst?.actual_state || rebalanceRuntime.status || rebalance.status || "disabled",
          summary: rebalancePlan
            ? `${displayExchange(rebalancePlan.buy_exchange, rebalancePlan.buy_exchange_label)} -> ${displayExchange(rebalancePlan.sell_exchange, rebalancePlan.sell_exchange_label)}`
            : (rebalance.status || "disabled"),
          detail: lifecycleCardDetail(rebalanceLifecycle, rebalancePlan
            ? `${rebalancePlan.base_asset} · ${Number(rebalanceRuntime.progress_pct || 0).toFixed(0)}% · cost ${Number(rebalancePlan.expected_cost_bps || 0).toFixed(0)} bps`
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
            ${escapeHtml(displayExchange(leg.exchange, leg.exchange_label))} ${escapeHtml(leg.symbol)}
            @ ${fmt.format(leg.average_price)}
          </span>
        `).join("");
        el.innerHTML = `
          <div><strong>$${money.format(item.profit_quote)}</strong><div class="subtle">profit</div></div>
          <div><strong>${item.profit_bps.toFixed(0)} bps</strong><div class="subtle">edge</div></div>
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
        `${riskState} · max/order $${money.format(risk.max_order_quote || 0)} · max/cycle $${money.format(risk.max_cycle_quote || 0)} · max/day $${money.format(risk.max_daily_loss_quote || 0)} · day P/L ${formatPnlValue(dailyPnl.total_realized_pnl || 0)} · open ${risk.max_open_orders || 0} · depth $${money.format(risk.min_order_book_depth_quote || 0)} · slip ${wholeNumber.format(risk.max_slippage_bps || 0)} bps · timeline ${timelineSummary.event_count || 0} · blocked ${timelineSummary.blocked_count || summary.blocked_event_count || 0} · alerts ${alerts.enabled ? "on" : "off"}`
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
          const reason = friendlyAccountMessage(event.reason || event.risk_triggers?.[0]) || "--";
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
            <td>${escapeHtml((event.accounts || []).map((account) => displayExchange(account)).join(", ") || "--")}</td>
            <td>${escapeHtml((event.symbols || []).join(", ") || "--")}</td>
            <td class="num">${latency == null ? "--" : `${Number(latency).toFixed(0)} ms`}</td>
            <td class="num">${slippage == null ? "--" : `${Number(slippage).toFixed(0)} bps`}</td>
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
        const reason = friendlyAccountMessage(event.reason) || "--";
        const eventId = event.event_id || "";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(eventId)}">${escapeHtml(eventId.slice(0, 8) || "--")}</td>
          <td>${formatAge(event.logged_at)}</td>
          <td>${escapeHtml(displayStrategy(event.strategy))}</td>
          <td>${escapeHtml(event.mode || "--")}</td>
          <td>${escapeHtml(event.status || "--")}</td>
          <td>${escapeHtml(displayExchange(event.exchange, event.exchange_label) || "--")}</td>
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
        const detail = friendlyAccountMessage(event.detail || event.error) || "--";
        const target = friendlyAccountMessage(
          event.target || event.strategy || displayExchange(event.exchange, event.exchange_label),
        ) || "--";
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
        : ["error", "open_order_sync_error", "execution_error", "reconciliation_required", "cancel_retry"].includes(runtimeStatus)
          ? runtimeStatus
          : approved ? "Ready" : "Blocked";
      const statusClass = runtimeStatus === "disabled"
        ? "risk-off"
        : ["error", "open_order_sync_error", "execution_error", "reconciliation_required", "cancel_retry", "blocked_by_risk"].includes(runtimeStatus)
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
        market.existing_spread_bps == null ? "--" : `${Number(market.existing_spread_bps).toFixed(0)} bps`,
        ""
      );
      text(
        "mm-safety-market-detail",
        `depth ${money.format(market.bid_depth_quote || 0)}/${money.format(market.ask_depth_quote || 0)} · gap ${Number.isFinite(maxLevelGapBps) ? maxLevelGapBps.toFixed(0) : "--"}/${limits.max_order_book_gap_bps == null ? "--" : wholeNumber.format(limits.max_order_book_gap_bps)} bps · age ${age == null ? "--" : age.toFixed(0) + "s"}`
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
        base == null ? "--" : wholeQuantity.format(base)
      );
      text(
        "mm-quality-inventory-detail",
        base == null
          ? "--"
          : `target ${wholeQuantity.format(target || 0)} · dev ${wholeQuantity.format(deviation || 0)} · buy ${buyMult == null ? "--" : Number(buyMult).toFixed(0)}x / sell ${sellMult == null ? "--" : Number(sellMult).toFixed(0)}x`
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
        quality.realized_spread_bps == null ? "--" : `${Number(quality.realized_spread_bps).toFixed(0)} bps`,
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
          <td data-label="${uiText("Amount")}" class="num">${wholeQuantity.format(order.amount)}</td>
          <td data-label="${uiText("Quote")}" class="num" title="${commonQuote}">${formatSymbolQuantity(order.quote_notional, marketMaker.plan.symbol, "quote")}</td>
          <td data-label="${uiText("Distance")}" class="num">${order.distance_bps.toFixed(0)} bps</td>
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
        <td data-label="${uiText("Exchange")}">${escapeHtml(displayExchange(plan.exchange, plan.exchange_label))}</td>
        <td data-label="${uiText("Symbol")}">${plan.symbol}</td>
        <td data-label="${uiText("Order Price")}" class="num">${fmt.format(order.price)}</td>
        <td data-label="${uiText("Slice Amount")}" class="num">${wholeQuantity.format(order.amount)}</td>
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
          <td class="num">${wholeQuantity.format(order.amount)}</td>
          <td class="num">${formatSymbolQuantity(order.quote_notional, spotGrid.plan.symbol, "quote")}</td>
          <td class="num">${Number(order.distance_bps || 0).toFixed(0)} bps</td>
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
          <td class="num">${wholeQuantity.format(row.amount_at_current_price || 0)}</td>
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
          <td class="num">${wholeQuantity.format(item.amount)}</td>
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

    function backtestPercent(value) {
      const number = Number(value);
      return Number.isFinite(number) ? `${number.toFixed(0)}%` : "--";
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
          label: `${account.label || displayExchange(account.exchange, account.exchange_label)} · ${displayExchange(account.exchange, account.exchange_label)} · ${account.symbol}`,
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
          <td class="num">${wholeQuantity.format(point.base)}</td>
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
          <td>${escapeHtml(displayExchange(run.account?.exchange, run.account?.exchange_label) || "--")}<br><span class="subtle">${escapeHtml(run.account?.symbol || "--")}</span></td>
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
      text("backtest-sharpe", result?.sharpe_ratio == null ? "--" : Number(result.sharpe_ratio).toFixed(0));
      text("backtest-fees", result ? money.format(result.fee_quote || 0) : "--");
      text("backtest-turnover", result ? backtestPercent(result.turnover_pct, 1) : "--");
      text("backtest-trades", result ? String(result.trade_count || 0) : "--");
      const progress = Math.max(0, Math.min(100, Number(selected?.progress_pct || 0)));
      document.getElementById("backtest-progress-fill").style.width = `${progress}%`;
      const market = selected?.account
        ? `${displayExchange(selected.account.exchange, selected.account.exchange_label) || "--"} ${selected.account.symbol || "--"}`
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
        label: `${account.label || account.id} · ${displayExchange(account.exchange, account.exchange_label) || "--"}`,
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
          <td>${escapeHtml(displayExchange(strategy.exchange, strategy.exchange_label) || "--")}<br><span class="subtle">${escapeHtml(strategy.symbol || strategy.asset || "--")}</span></td>
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
          <td>${escapeHtml(displayExchange(account.exchange, account.exchange_label) || "--")}<br><span class="subtle">${escapeHtml(account.market_type || "spot")}</span></td>
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

    function walletProviderType(provider, fallback = "injected") {
      if (provider?.isMetaMask) return "metamask";
      if (provider?.isImToken) return "imtoken";
      if (provider?.isCoinbaseWallet) return "coinbase_wallet";
      if (provider?.isRabby) return "rabby";
      if (provider?.isTrust || provider?.isTrustWallet) return "trust_wallet";
      return fallback;
    }

    function registerWalletProvider(provider, info = {}) {
      if (!provider || typeof provider.request !== "function") return;
      if ([...discoveredWalletProviders.values()].some((row) => row.provider === provider)) return;
      const providerType = walletProviderType(provider, info.name || "injected");
      const key = String(info.uuid || `${providerType}:${info.rdns || info.name || discoveredWalletProviders.size}`);
      discoveredWalletProviders.set(key, {
        key,
        provider,
        type: providerType,
        name: String(info.name || providerType.replaceAll("_", " ") || "Browser Wallet"),
      });
      renderWalletProviderOptions();
    }

    function renderWalletProviderOptions() {
      const rows = [...discoveredWalletProviders.values()].map((row) => ({
        value: row.key,
        label: row.name,
      }));
      const selected = document.getElementById("wallet-provider-select")?.value || rows[0]?.value || "";
      setSelectOptions(
        "wallet-provider-select",
        rows,
        selected,
        rows.length ? "Select wallet" : "Open in a wallet browser",
      );
      const connectButton = document.getElementById("wallet-connect");
      const workspaceUnavailable = ["user_account_required", "error"].includes(
        currentUserWorkspace?.status,
      );
      if (connectButton) connectButton.disabled = rows.length === 0 || workspaceUnavailable;
    }

    function walletNetworkLabel(chainId) {
      const labels = {
        1: "Ethereum",
        10: "Optimism",
        56: "BNB Chain",
        137: "Polygon",
        8453: "Base",
        42161: "Arbitrum",
      };
      return labels[Number(chainId)] || `EVM ${chainId}`;
    }

    function renderVenueConnections(workspace, wallets, venues) {
      const body = document.getElementById("wallet-venue-connections");
      if (!body) return;
      const walletById = new Map(wallets.map((wallet) => [wallet.id, wallet]));
      const venueById = new Map(venues.map((venue) => [venue.id, venue]));
      const links = workspace?.venue_connections || [];
      body.replaceChildren();
      if (!links.length) {
        const row = document.createElement("tr");
        row.innerHTML = `<td colspan="5">${escapeHtml(uiText("No venue connections yet."))}</td>`;
        body.appendChild(row);
        return;
      }
      for (const link of links) {
        const wallet = walletById.get(link.wallet_id);
        const venue = venueById.get(link.venue);
        const healthy = link.status === "healthy";
        const stale = Boolean(link.stale);
        const statusClass = stale ? "warn" : healthy ? "ok" : "bad";
        const statusLabel = stale
          ? uiText("Check overdue")
          : uiText(healthy ? "Read-only verified" : "Connection error");
        const detail = link.detail || {};
        const detailParts = [];
        const authorizedAccount = (workspace?.accounts || []).find((account) => (
          account.exchange === link.venue
          && account.wallet_id === link.wallet_id
          && account.authorization_verified_at
        ));
        if (detail.position_count != null) detailParts.push(`${uiText("Positions")} ${detail.position_count}`);
        if (detail.market_count != null) detailParts.push(`${uiText("Markets")} ${detail.market_count}`);
        const row = document.createElement("tr");
        row.innerHTML = `
          <td>${escapeHtml(venue?.label || link.venue || "--")}</td>
          <td title="${escapeHtml(link.wallet_address || "")}">${escapeHtml(wallet?.label || uiText("Public read-only"))}<br><span class="subtle">${escapeHtml(link.wallet_address ? `${link.wallet_address.slice(0, 8)}…${link.wallet_address.slice(-6)}` : "--")}</span></td>
          <td class="${statusClass}" title="${escapeHtml(link.error || "")}">${escapeHtml(statusLabel)}<br><span class="subtle">${escapeHtml(stale ? uiText("Automatic recheck pending") : authorizedAccount ? uiText("API Wallet authorized; account disabled") : healthy ? uiText("Automation disabled") : link.error || "--")}${detailParts.length ? ` · ${escapeHtml(detailParts.join(" · "))}` : ""}</span></td>
          <td>${escapeHtml(formatAge(link.checked_at))}<br><span class="subtle">${Number(link.latency_ms || 0).toFixed(0)}ms · ${escapeHtml(uiText("Auto-check enabled"))}</span></td>
          <td><div class="workspace-table-actions"></div></td>
        `;
        const refreshButton = document.createElement("button");
        refreshButton.type = "button";
        refreshButton.className = "ghost-button";
        refreshButton.textContent = uiText("Refresh");
        refreshButton.addEventListener("click", () => refreshVenueConnection(link, refreshButton));
        const revokeButton = document.createElement("button");
        revokeButton.type = "button";
        revokeButton.className = "danger-button";
        revokeButton.textContent = uiText("Revoke");
        revokeButton.addEventListener("click", () => revokeVenueConnection(link, revokeButton));
        row.querySelector(".workspace-table-actions").append(refreshButton, revokeButton);
        body.appendChild(row);
      }
    }

    function renderWalletConnections(workspace) {
      renderWalletProviderOptions();
      const wallets = workspace?.wallets || [];
      const venues = workspace?.dex_venue_catalog || [];
      renderVenueConnections(workspace, wallets, venues);
      setSelectOptions(
        "wallet-link-select",
        wallets.map((wallet) => ({
          value: wallet.id,
          label: `${wallet.label || "Wallet"} · ${wallet.address.slice(0, 6)}…${wallet.address.slice(-4)}`,
        })),
        document.getElementById("wallet-link-select")?.value || wallets[0]?.id || "",
        "No verified wallet",
      );
      setSelectOptions(
        "wallet-venue-select",
        venues.map((venue) => ({
          value: venue.id,
          label: `${venue.label} · ${(venue.market_types || []).join(" / ")}`,
          title: `Automation authorization: ${venue.automation_auth || "separate key"}`,
        })),
        document.getElementById("wallet-venue-select")?.value || venues[0]?.id || "",
        "Select venue",
      );
      const testButton = document.getElementById("wallet-venue-test");
      const selectedVenue = document.getElementById("wallet-venue-select")?.value || "";
      if (testButton) {
        testButton.disabled = ["user_account_required", "error"].includes(workspace?.status)
          || !selectedVenue
          || (!wallets.length && selectedVenue !== "dydx");
      }
      const refreshAllButton = document.getElementById("wallet-venue-refresh-all");
      if (refreshAllButton) {
        refreshAllButton.disabled = ["user_account_required", "error"].includes(workspace?.status)
          || !(workspace?.venue_connections || []).length;
      }

      const body = document.getElementById("wallet-connections");
      if (!body) return;
      body.replaceChildren();
      if (!wallets.length) {
        const row = document.createElement("tr");
        row.innerHTML = `<td colspan="4">${escapeHtml(uiText("No verified wallets yet."))}</td>`;
        body.appendChild(row);
        return;
      }
      for (const wallet of wallets) {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td title="${escapeHtml(wallet.address)}">${escapeHtml(wallet.label || "Wallet")}<br><span class="subtle">${escapeHtml(`${wallet.address.slice(0, 8)}…${wallet.address.slice(-6)}`)}</span></td>
          <td>${escapeHtml(walletNetworkLabel(wallet.chain_id))}<br><span class="subtle">${escapeHtml(wallet.wallet_type || "injected")}</span></td>
          <td class="ok">${escapeHtml(uiText("Read-only verified"))}<br><span class="subtle">${escapeHtml((wallet.permissions || []).join(" · "))}</span></td>
          <td><div class="workspace-table-actions"></div></td>
        `;
        const revokeButton = document.createElement("button");
        revokeButton.type = "button";
        revokeButton.className = "danger-button";
        revokeButton.textContent = uiText("Revoke");
        revokeButton.addEventListener("click", () => revokeWalletConnection(wallet, revokeButton));
        row.querySelector(".workspace-table-actions").appendChild(revokeButton);
        body.appendChild(row);
      }
    }

    async function connectAndVerifyWallet() {
      const providerKey = document.getElementById("wallet-provider-select")?.value || "";
      const providerRow = discoveredWalletProviders.get(providerKey);
      if (!providerRow) {
        text("wallet-connection-status", uiText("Open this page in a supported wallet browser."));
        return;
      }
      const button = document.getElementById("wallet-connect");
      button.disabled = true;
      text("wallet-connection-status", uiText("Waiting for wallet approval…"));
      try {
        const accounts = await providerRow.provider.request({ method: "eth_requestAccounts" });
        const address = String(accounts?.[0] || "");
        if (!address) throw new Error("wallet did not return an account");
        const chainValue = await providerRow.provider.request({ method: "eth_chainId" });
        const chainId = Number.parseInt(String(chainValue), String(chainValue).startsWith("0x") ? 16 : 10);
        const challengeResult = await postUserWorkspace({
          action: "wallet_challenge",
          address,
          chain_id: chainId,
          wallet_type: providerRow.type,
        });
        const challenge = challengeResult.wallet_challenge || {};
        let signature;
        try {
          signature = await providerRow.provider.request({
            method: "personal_sign",
            params: [challenge.message, address],
          });
        } catch (error) {
          if (Number(error?.code) !== -32602) throw error;
          signature = await providerRow.provider.request({
            method: "personal_sign",
            params: [address, challenge.message],
          });
        }
        await postUserWorkspace({
          action: "verify_wallet",
          challenge_id: challenge.challenge_id,
          signature,
          label: document.getElementById("wallet-label")?.value.trim() || providerRow.name,
        });
        text("wallet-connection-status", uiText("Wallet verified for read-only access."));
      } catch (error) {
        text("wallet-connection-status", `wallet connection failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function revokeWalletConnection(wallet, button) {
      if (!dangerConfirm("Revoke this wallet connection?")) return;
      button.disabled = true;
      try {
        await postUserWorkspace({ action: "delete_wallet", wallet_id: wallet.id });
        text("wallet-connection-status", uiText("Wallet connection revoked."));
      } catch (error) {
        text("wallet-connection-status", `wallet revoke failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function testWalletVenue() {
      const button = document.getElementById("wallet-venue-test");
      const venue = document.getElementById("wallet-venue-select")?.value || "";
      const walletId = document.getElementById("wallet-link-select")?.value || "";
      button.disabled = true;
      text("wallet-connection-status", `${uiText("Testing read-only access")} · ${venue}`);
      try {
        const result = await postUserWorkspace({
          action: "test_wallet_venue",
          venue,
          wallet_id: walletId,
        });
        const check = result.venue_check || {};
        if (check.status !== "healthy") throw new Error(check.error || "venue check failed");
        text(
          "wallet-connection-status",
          `${venue} · ${uiText("Read-only access healthy")} · ${Number(check.latency_ms || 0).toFixed(0)}ms`,
        );
      } catch (error) {
        text("wallet-connection-status", `${venue} check failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function revokeVenueConnection(link, button) {
      if (!dangerConfirm("Revoke this venue connection?")) return;
      button.disabled = true;
      try {
        await postUserWorkspace({
          action: "delete_venue_connection",
          connection_id: link.id,
        });
        text("wallet-connection-status", uiText("Venue connection revoked."));
      } catch (error) {
        text("wallet-connection-status", `venue revoke failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function refreshVenueConnection(link, button) {
      button.disabled = true;
      text("wallet-connection-status", `${uiText("Refreshing connection")} · ${link.venue}`);
      try {
        const result = await postUserWorkspace({
          action: "refresh_venue_connection",
          connection_id: link.id,
        });
        const refresh = result.venue_refresh || {};
        const healthy = Number(refresh.healthy_count || 0);
        text(
          "wallet-connection-status",
          healthy
            ? `${link.venue} · ${uiText("Read-only access healthy")}`
            : `${link.venue} · ${uiText("Connection error")}`,
        );
      } catch (error) {
        text("wallet-connection-status", `venue refresh failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function refreshAllVenueConnections() {
      const button = document.getElementById("wallet-venue-refresh-all");
      button.disabled = true;
      text("wallet-connection-status", uiText("Refreshing all connections…"));
      try {
        const result = await postUserWorkspace({
          action: "refresh_all_venue_connections",
        });
        const refresh = result.venue_refresh || {};
        text(
          "wallet-connection-status",
          `${uiText("Connections refreshed")} · ${Number(refresh.healthy_count || 0)} ${uiText("healthy")} · ${Number(refresh.error_count || 0)} ${uiText("errors")}`,
        );
      } catch (error) {
        text("wallet-connection-status", `venue refresh failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    function openCurrentPageInImToken() {
      const target = encodeURIComponent(window.location.href);
      window.location.href = `imtokenv2://navigate?screen=DappView&url=${target}`;
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
      if (["activate_project", "contact_administrator"].includes(actionCode)) {
        fillUserProjectForm(project);
        focusWorkspaceControl("user-project-asset");
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
        focusWorkspaceControl("user-exchange-id");
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
      focusWorkspaceControl("user-project-asset");
    }

    function renderUserSetupReadiness(workspace) {
      const container = document.getElementById("user-setup-readiness");
      if (!container) return;
      const signature = JSON.stringify({
        status: workspace?.status || "",
        error: workspace?.error || "",
        connections: (workspace?.connections || []).map((connection) => ({
          id: connection.id,
          label: connection.label,
          exchange: connection.exchange,
          status: connection.status,
          credentials: connection.credentials_configured,
          markets: connection.markets,
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
            ? "Account setup is temporarily unavailable"
            : "A registered user account is required"
        );
        const note = document.createElement("span");
        note.className = "subtle";
        note.textContent = workspace?.error || uiText("Log in with your username to manage exchange accounts.");
        detail.append(title, note);
        row.appendChild(detail);
        container.appendChild(row);
        return;
      }
      const connections = workspace?.connections || [];
      if (connections.length === 0) {
        const row = document.createElement("div");
        row.className = "workspace-readiness-row workspace-readiness-empty";
        const detail = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = uiText("Connect your first exchange account");
        const note = document.createElement("span");
        note.className = "subtle";
        note.textContent = uiText("Add one API connection, then choose its tradable currencies.");
        detail.append(title, note);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "control-button";
        button.textContent = uiText("Connect Exchange");
        button.addEventListener("click", () => {
          resetUserExchangeAccountForm();
          focusWorkspaceControl("user-exchange-id");
        });
        row.append(detail, button);
        container.appendChild(row);
        return;
      }

      for (const connection of connections) {
        const steps = [
          Boolean(connection.credentials_configured),
          Boolean(connection.withdrawal_disabled_confirmed && connection.trade_permission_confirmed),
          Boolean(connection.live_enabled),
        ];
        const completed = steps.filter(Boolean).length;
        const total = steps.length;
        const progress = completed * 100 / total;
        const ready = completed === total;
        const row = document.createElement("div");
        row.className = `workspace-readiness-row ${ready ? "workspace-ready" : "workspace-attention"}`;

        const identity = document.createElement("div");
        identity.className = "workspace-readiness-identity";
        const name = document.createElement("strong");
        name.textContent = connection.label || connection.id;
        const pair = document.createElement("span");
        pair.className = "subtle";
        pair.textContent = `${workspaceExchange(connection.exchange)?.label || connection.exchange} · ${(connection.market_types || [connection.market_type || "spot"]).join(" / ")}`;
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
        nextValue.textContent = uiText(
          !steps[0]
            ? "Save API credentials"
            : !steps[1]
              ? "Confirm API safety permissions"
              : !steps[2]
                ? "Test API connection"
                : "Account ready"
        );
        next.append(nextLabel, nextValue);

        const actionControl = document.createElement("button");
        actionControl.className = ready
          ? "ghost-button workspace-continue-button"
          : "control-button workspace-continue-button";
        actionControl.textContent = uiText(ready ? "Manage" : "Continue Setup");
        actionControl.type = "button";
        actionControl.addEventListener("click", () => {
          fillUserExchangeAccountForm(connection);
          focusWorkspaceControl(
            !steps[0]
              ? "user-exchange-api-key"
              : !steps[1]
                ? "user-exchange-no-withdraw"
                : "user-exchange-label"
          );
        });
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
      for (const project of projects) {
        const platformOnly = Boolean(project.platform_only);
        const tr = document.createElement("tr");
        tr.dataset.workspaceProjectId = project.id || "";
        const statusClass = project.status === "active" ? "ok" : project.status === "pending" ? "risk-blocked" : "subtle";
        const setup = project.readiness || {};
        const setupText = platformOnly
          ? uiText("Platform project summary")
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
        if (project.status !== "active") {
          const activateButton = document.createElement("button");
          activateButton.className = "control-button";
          activateButton.type = "button";
          activateButton.textContent = uiText("Activate");
          activateButton.addEventListener("click", () => activateUserProject(project, activateButton));
          actions.appendChild(activateButton);
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
        (account) => account.id === accountId || account.connection_id === accountId
      ) || null;
    }

    function workspaceConnection(connectionId) {
      return (currentUserWorkspace?.connections || []).find(
        (connection) => connection.id === connectionId
      ) || null;
    }

    function workspaceMarketCacheKey({ project, exchange, marketType, apiVariant }) {
      return [project?.asset || "", exchange || "", marketType || "", apiVariant || ""].join(":");
    }

    function workspaceDefaultSymbol(project, marketType = "spot") {
      if (!project?.asset || !project?.quote_currency) return project?.symbol || "";
      const spotSymbol = `${project.asset}/${project.quote_currency}`;
      return ["swap", "future"].includes(marketType)
        ? `${spotSymbol}:${project.quote_currency}`
        : spotSymbol;
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
      setFieldValue("user-exchange-proxy-url", "");
      setFieldValue("user-exchange-egress-mode", "default");
      setFieldValue("user-exchange-source-ip", "");
      setFieldValue("user-exchange-expected-ip", "");
      setFieldValue("user-exchange-assets", "");
      const symbolSelect = document.getElementById("user-exchange-symbol");
      if (symbolSelect) symbolSelect.replaceChildren();
      setCheckedValue("user-exchange-enabled", false);
      setCheckedValue("user-exchange-no-withdraw", false);
      setCheckedValue("user-exchange-trade-permission", false);
      const defaultExchange = currentUserWorkspace?.exchange_catalog?.[0];
      setFieldValue("user-exchange-project", "");
      setFieldValue("user-exchange-id", defaultExchange?.id || "");
      setFieldValue(
        "user-exchange-label",
        suggestedWorkspaceAccountLabel(defaultExchange?.id || "")
      );
      syncUserExchangeMarketTypes();
      syncUserExchangeEgressFields();
    }

    function syncUserExchangeEgressFields(connection = null) {
      const mode = document.getElementById("user-exchange-egress-mode")?.value || "default";
      const exchangeId = document.getElementById("user-exchange-id")?.value || "";
      const connectionId = document.getElementById("user-exchange-account-id")?.value || "";
      const sameExchangePeers = (currentUserWorkspace?.connections || []).filter(
        (row) => row.exchange === exchangeId && row.id !== connectionId,
      );
      const sourceField = document.getElementById("user-exchange-source-ip-field");
      const proxyField = document.getElementById("user-exchange-proxy-url-field");
      if (sourceField) sourceField.hidden = mode !== "source_ip";
      if (proxyField) proxyField.hidden = mode !== "proxy";
      const status = document.getElementById("user-exchange-egress-status");
      if (!status) return;
      const blockers = connection?.egress_blockers || [];
      if (connection?.egress_ready && connection?.egress_observed_ip) {
        status.textContent = `${uiText("Verified public IP")} ${connection.egress_observed_ip} · ${formatAge(connection.egress_checked_at)}`;
        status.className = "ok wide-field";
      } else if (blockers.length) {
        status.textContent = blockers.map((item) => friendlyAccountMessage(item)).join(" · ");
        status.className = "missing wide-field";
      } else if (sameExchangePeers.length && mode === "default") {
        status.textContent = uiText("Another account already uses this exchange. Select a dedicated source IP or proxy; the new account stays inactive until its public IP is verified.");
        status.className = "missing wide-field";
      } else if (mode === "default") {
        status.textContent = uiText("Single accounts may use the server default IP.");
        status.className = "subtle wide-field";
      } else {
        status.textContent = uiText("Save and test to verify this account's public IP.");
        status.className = "subtle wide-field";
      }
    }

    function suggestedWorkspaceAccountLabel(exchangeId) {
      const exchange = workspaceExchange(exchangeId);
      if (!exchangeId || !exchange) return "";
      if (exchangeId === "hyperliquid") return "Hyperliquid MetaMask";
      const used = new Set(
        (currentUserWorkspace?.connections || [])
          .filter((connection) => connection.exchange === exchangeId)
          .map((connection) => String(connection.label || "").trim().toLowerCase())
      );
      const base = `${exchange.label || exchangeId} Main`;
      if (!used.has(base.toLowerCase())) return base;
      let index = 2;
      while (used.has(`${exchange.label || exchangeId} ${index}`.toLowerCase())) index += 1;
      return `${exchange.label || exchangeId} ${index}`;
    }

    function userExchangeMarketKey(market) {
      return `${market?.market_type || market?.type || "spot"}|${market?.symbol || ""}`;
    }

    function renderUserExchangeMarketOptions(markets = [], selectedMarkets = []) {
      const select = document.getElementById("user-exchange-symbol");
      if (!select) return;
      const selectedKeys = new Set([
        ...Array.from(select.selectedOptions || []).map((option) => option.value),
        ...selectedMarkets.map(userExchangeMarketKey),
      ]);
      const rows = new Map();
      for (const market of markets) {
        if (!market?.symbol) continue;
        rows.set(userExchangeMarketKey(market), market);
      }
      select.replaceChildren();
      for (const [key, market] of [...rows.entries()].sort((left, right) => (
        left[0].localeCompare(right[0])
      ))) {
        const option = document.createElement("option");
        option.value = key;
        option.dataset.marketType = market.market_type || market.type || "spot";
        option.dataset.symbol = market.symbol || "";
        option.dataset.base = market.base || market.asset || "";
        option.dataset.quote = market.quote || market.quote_currency || "";
        const minimum = Number(market.cost_min || 0);
        option.textContent = `${String(option.dataset.marketType).toUpperCase()} · ${market.symbol}${minimum > 0 ? ` · min ${wholeNumber.format(minimum)} ${option.dataset.quote}` : ""}`;
        option.selected = selectedKeys.has(key);
        select.appendChild(option);
      }
    }

    function fillUserExchangeAccountForm(connection) {
      const connectionId = connection?.connection_id || connection?.id || "";
      selectedUserExchangeAccountId = connectionId;
      userExchangeAccountFormDirty = false;
      setFieldValue("user-exchange-account-id", connectionId);
      setFieldValue("user-exchange-project", "");
      setFieldValue("user-exchange-label", connection?.label || "");
      setFieldValue("user-exchange-id", connection?.exchange || "");
      setFieldValue("user-exchange-api-key", "");
      setFieldValue("user-exchange-secret", "");
      setFieldValue("user-exchange-passphrase", "");
      setFieldValue("user-exchange-proxy-url", "");
      setFieldValue("user-exchange-egress-mode", connection?.egress_mode || "default");
      setFieldValue("user-exchange-source-ip", connection?.egress_source_ip || "");
      setFieldValue("user-exchange-expected-ip", connection?.egress_expected_ip || "");
      const markets = connection?.markets || [];
      setFieldValue(
        "user-exchange-assets",
        [...new Set(markets.map((market) => market.asset).filter(Boolean))].join(", ")
      );
      renderUserExchangeMarketOptions(markets, markets);
      setCheckedValue("user-exchange-enabled", connection?.live_enabled);
      setCheckedValue(
        "user-exchange-no-withdraw",
        connection?.withdrawal_disabled_confirmed
      );
      setCheckedValue(
        "user-exchange-trade-permission",
        connection?.trade_permission_confirmed
      );
      syncUserExchangeMarketTypes(
        connection?.market_types?.[0] || connection?.market_type || "spot",
        connection?.api_variant || ""
      );
      syncUserExchangeEgressFields(connection);
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

      const needsPassphrase = (exchange?.required_credentials || []).includes("passphrase");
      text(
        "user-exchange-api-key-label",
        uiText(exchange?.credential_labels?.api_key || "API Key"),
      );
      text(
        "user-exchange-secret-label",
        uiText(exchange?.credential_labels?.secret || "API Secret / Private Key"),
      );
      const passphraseField = document.getElementById("user-exchange-passphrase-field");
      if (passphraseField) passphraseField.hidden = !needsPassphrase;
      const isHyperliquid = exchangeId === "hyperliquid";
      const apiKeyField = document.getElementById("user-exchange-api-key-field");
      const secretField = document.getElementById("user-exchange-secret-field");
      if (apiKeyField) apiKeyField.hidden = isHyperliquid;
      if (secretField) secretField.hidden = isHyperliquid;
      const authorizeButton = document.getElementById("user-hyperliquid-authorize");
      const authorizeHint = document.getElementById("user-hyperliquid-authorize-hint");
      if (authorizeButton) {
        authorizeButton.hidden = !isHyperliquid;
        authorizeButton.disabled = !(currentUserWorkspace?.wallets || []).length
          || userExchangeAccountFormBusy;
      }
      if (authorizeHint) authorizeHint.hidden = !isHyperliquid;
      const noWithdraw = document.getElementById("user-exchange-no-withdraw");
      const tradePermission = document.getElementById("user-exchange-trade-permission");
      if (noWithdraw) {
        if (isHyperliquid) noWithdraw.checked = true;
        noWithdraw.disabled = isHyperliquid;
      }
      if (tradePermission) {
        if (isHyperliquid) tradePermission.checked = true;
        tradePermission.disabled = isHyperliquid;
      }

      const selectedConnection = workspaceConnection(
        document.getElementById("user-exchange-account-id")?.value || ""
      );
      const sameConnection = Boolean(
        selectedConnection
        && selectedConnection.exchange === exchangeId
        && selectedConnection.api_variant === selectedVariant
      );
      const connectionReady = sameConnection && selectedConnection.status === "healthy";
      const enabled = document.getElementById("user-exchange-enabled");
      if (enabled) {
        enabled.disabled = true;
        if (!connectionReady) enabled.checked = false;
        enabled.title = "";
      }
      const testButton = document.getElementById("user-exchange-test");
      if (testButton) {
        const credentialsConfigured = Boolean(selectedConnection?.credentials_configured);
        testButton.disabled = !sameConnection || !credentialsConfigured;
        testButton.title = testButton.disabled
          ? uiText("Save the account and credentials before testing the connection.")
          : uiText("This test reads account data and never places or cancels orders.");
      }
    }

    async function authorizeHyperliquidWithMetaMask() {
      if (userExchangeAccountFormBusy) return;
      const walletId = document.getElementById("wallet-link-select")?.value || "";
      const wallet = (currentUserWorkspace?.wallets || []).find((row) => row.id === walletId);
      const providerKey = document.getElementById("wallet-provider-select")?.value || "";
      const providerRow = discoveredWalletProviders.get(providerKey);
      if (!wallet || !providerRow) {
        setUserWorkspaceNotice(uiText("Connect and verify MetaMask first."));
        return;
      }
      const button = document.getElementById("user-hyperliquid-authorize");
      let authorizationId = "";
      let signatureCreated = false;
      userExchangeAccountFormBusy = true;
      button.disabled = true;
      text("wallet-connection-status", uiText("Waiting for Hyperliquid authorization…"));
      try {
        const accounts = await providerRow.provider.request({ method: "eth_requestAccounts" });
        const address = String(accounts?.[0] || "");
        if (!address || address.toLowerCase() !== wallet.address.toLowerCase()) {
          throw new Error(uiText("Selected MetaMask account does not match the verified wallet."));
        }
        const chainValue = await providerRow.provider.request({ method: "eth_chainId" });
        const chainId = Number.parseInt(
          String(chainValue),
          String(chainValue).startsWith("0x") ? 16 : 10,
        );
        const account = {
          project_id: document.getElementById("user-exchange-project").value,
          label: document.getElementById("user-exchange-label").value.trim() || "Hyperliquid MetaMask",
          exchange: "hyperliquid",
          market_type: document.getElementById("user-exchange-market-type").value,
          api_variant: document.getElementById("user-exchange-api-variant").value,
          symbol: document.getElementById("user-exchange-symbol").value,
          enabled: false,
          withdrawal_disabled_confirmed: true,
          trade_permission_confirmed: true,
        };
        const accountId = document.getElementById("user-exchange-account-id").value.trim();
        if (accountId) account.id = accountId;
        const prepared = await postUserWorkspace({
          action: "prepare_hyperliquid_agent",
          wallet_id: wallet.id,
          chain_id: chainId,
          account,
        });
        const authorization = prepared.hyperliquid_authorization || {};
        authorizationId = authorization.authorization_id || "";
        if (!authorizationId || !authorization.typed_data) {
          throw new Error("server did not return a Hyperliquid authorization request");
        }
        const signature = await providerRow.provider.request({
          method: "eth_signTypedData_v4",
          params: [address, JSON.stringify(authorization.typed_data)],
        });
        signatureCreated = true;
        const completed = await postUserWorkspace({
          action: "complete_hyperliquid_agent",
          authorization_id: authorizationId,
          signature,
        });
        const saved = completed.account || {};
        resetUserExchangeAccountForm();
        setUserWorkspaceNotice(
          `${uiText("Hyperliquid API Wallet authorized")} · ${saved.agent_address?.slice(0, 8) || "--"}…${saved.agent_address?.slice(-6) || ""} · ${uiText("Account remains disabled until connection test passes.")}`,
          12000,
        );
        text("wallet-connection-status", uiText("Hyperliquid trading authorization verified."));
      } catch (error) {
        if (authorizationId && !signatureCreated) {
          await postUserWorkspace({
            action: "cancel_hyperliquid_agent",
            authorization_id: authorizationId,
          }).catch(() => {});
        }
        setUserWorkspaceNotice(`Hyperliquid authorization failed: ${error.message || error}`, 12000);
        text("wallet-connection-status", uiText("Hyperliquid authorization was not completed."));
      } finally {
        userExchangeAccountFormBusy = false;
        button.disabled = false;
        syncUserExchangeMarketTypes();
      }
    }

    function renderUserExchangeAccountForm(workspace) {
      if (userExchangeAccountFormDirty || userExchangeAccountFormBusy) return;
      const exchanges = (workspace?.exchange_catalog || []).map((exchange) => ({
        value: exchange.id,
        label: exchange.label || exchange.id,
        title: (exchange.market_types || []).join(", "),
      }));
      const selected = (workspace?.connections || []).find(
        (connection) => connection.id === selectedUserExchangeAccountId
      );
      const exchangeValue = selected?.exchange
        || document.getElementById("user-exchange-id")?.value
        || exchanges[0]?.value
        || "";
      setSelectOptions("user-exchange-id", exchanges, exchangeValue, "Select exchange");
      if (selected) {
        setFieldValue(
          "user-exchange-account-id",
          selected.id
        );
        setFieldValue("user-exchange-label", selected.label);
        setCheckedValue("user-exchange-enabled", selected.live_enabled);
        setCheckedValue(
          "user-exchange-no-withdraw",
          selected.withdrawal_disabled_confirmed
        );
        setFieldValue("user-exchange-egress-mode", selected.egress_mode || "default");
        setFieldValue("user-exchange-source-ip", selected.egress_source_ip || "");
        setFieldValue("user-exchange-expected-ip", selected.egress_expected_ip || "");
        setCheckedValue(
          "user-exchange-trade-permission",
          selected.trade_permission_confirmed
        );
        const markets = selected.markets || [];
        setFieldValue(
          "user-exchange-assets",
          [...new Set(markets.map((market) => market.asset).filter(Boolean))].join(", ")
        );
        renderUserExchangeMarketOptions(markets, markets);
      } else {
        selectedUserExchangeAccountId = "";
        setFieldValue("user-exchange-account-id", "");
        setCheckedValue("user-exchange-enabled", false);
        setCheckedValue("user-exchange-no-withdraw", false);
        setCheckedValue("user-exchange-trade-permission", false);
        setFieldValue("user-exchange-assets", "");
        setFieldValue("user-exchange-egress-mode", "default");
        setFieldValue("user-exchange-source-ip", "");
        setFieldValue("user-exchange-expected-ip", "");
        const symbolSelect = document.getElementById("user-exchange-symbol");
        if (symbolSelect) symbolSelect.replaceChildren();
      }
      setFieldValue("user-exchange-api-key", "");
      setFieldValue("user-exchange-secret", "");
      setFieldValue("user-exchange-passphrase", "");
      setFieldValue("user-exchange-proxy-url", "");
      syncUserExchangeMarketTypes(
        selected?.market_types?.[0] || selected?.market_type || "",
        selected?.api_variant || "",
        ""
      );
      syncUserExchangeEgressFields(selected || null);
    }

    function renderUserExchangeAccounts(workspace) {
      const body = document.getElementById("user-exchange-accounts");
      if (!body) return;
      body.innerHTML = "";
      const connections = workspace?.connections || [];
      if (connections.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6">${escapeHtml(uiText("No API connections yet."))}</td>`;
        body.appendChild(tr);
        return;
      }
      for (const connection of connections) {
        const marketLabels = (connection.markets || []).map((market) => (
          `${String(market.market_type || "spot").toUpperCase()} · ${market.symbol || "--"}`
        ));
        const visibleMarkets = marketLabels.slice(0, 3).join(" · ");
        const remainingMarkets = Math.max(0, marketLabels.length - 3);
        const marketsText = `${visibleMarkets}${remainingMarkets ? ` · +${remainingMarkets}` : ""}`;
        const marketPermissionText = connection.market_scope === "all_supported_markets"
          ? uiText("All supported markets")
          : (marketsText || "--");
        const credentialText = connection.credentials_configured
          ? "Encrypted / configured"
          : "Missing";
        const statusText = connection.status === "healthy"
          ? marketLabels.length
            ? `${connection.healthy_count}/${marketLabels.length} ${uiText("markets healthy")}`
            : uiText("API ready")
          : connection.status === "error"
            ? `${connection.error_count || 1} ${uiText("market checks failed")}`
            : "Needs connection test";
        const balanceCount = (connection.balances || []).length;
        const balanceStatus = connection.checked_at
          ? `${balanceCount} ${uiText("balances")} · ${formatAge(connection.checked_at)}`
          : uiText("Balance not checked");
        const connectionClass = connection.status === "healthy"
          ? "ok"
          : connection.status === "error"
            ? "missing"
            : "";
        const marketScope = (connection.market_types || [connection.market_type || "spot"])
          .map((marketType) => String(marketType).toUpperCase())
          .join(" / ");
        const variantText = connection.api_variant
          && !["default", "global"].includes(connection.api_variant)
          ? ` · ${connection.api_variant}`
          : "";
        const egressText = connection.egress_observed_ip
          ? `${uiText("Public IP")} ${connection.egress_observed_ip}`
          : connection.egress_expected_ip
            ? `${uiText("Expected IP")} ${connection.egress_expected_ip}`
            : uiText("Server Default IP");
        const tr = document.createElement("tr");
        tr.dataset.workspaceConnectionId = connection.id || "";
        tr.innerHTML = `
          <td title="${escapeHtml(connection.id || "")}">${escapeHtml(connection.label || connection.id)}<br><span class="subtle">ID ${escapeHtml(String(connection.id || "").slice(-8))}${connection.runtime_keys?.length ? ` · ${escapeHtml(connection.runtime_keys.join(" / "))}` : ""}</span></td>
          <td>${escapeHtml(workspaceExchange(connection.exchange)?.label || connection.exchange)}<br><span class="subtle">${escapeHtml(`${marketScope}${variantText}`)} · ${escapeHtml(egressText)}</span></td>
          <td title="${escapeHtml(marketLabels.join(" · "))}">${escapeHtml(marketPermissionText)}<br><span class="subtle">${marketLabels.length} ${escapeHtml(uiText("selected markets"))}</span></td>
          <td class="${connection.credentials_configured ? "ok" : "missing"}">${escapeHtml(credentialText)}</td>
          <td class="${connectionClass}">${escapeHtml(uiText(statusText))}<br><span class="subtle">${escapeHtml(balanceStatus)} · ${connection.enabled_count || 0} ${escapeHtml(uiText("enabled"))}</span></td>
          <td><div class="workspace-table-actions"></div></td>
        `;
        const actions = tr.querySelector(".workspace-table-actions");
        const editButton = document.createElement("button");
        editButton.className = "control-button";
        editButton.type = "button";
        editButton.textContent = uiText("Manage");
        editButton.addEventListener("click", () => fillUserExchangeAccountForm(connection));
        actions.appendChild(editButton);
        const testButton = document.createElement("button");
        testButton.className = "ghost-button workspace-account-test";
        testButton.type = "button";
        testButton.textContent = "Test";
        testButton.disabled = !connection.credentials_configured;
        testButton.title = testButton.disabled ? uiText("Save credentials first.") : "";
        testButton.addEventListener("click", () => testUserExchangeConnection(connection, testButton));
        actions.appendChild(testButton);
        const deleteButton = document.createElement("button");
        deleteButton.className = "danger-button";
        deleteButton.type = "button";
        deleteButton.textContent = "Delete";
        deleteButton.addEventListener("click", () => deleteUserExchangeConnection(connection, deleteButton));
        actions.appendChild(deleteButton);
        body.appendChild(tr);
      }
    }

    function setUserWorkspace(workspace) {
      currentUserWorkspace = workspace || null;
      if (lastState) lastState.user_workspace = workspace;
      if (pageStateCache.settings) pageStateCache.settings.user_workspace = workspace;
      if (pageStateCache.quant) pageStateCache.quant.user_workspace = workspace;
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
      setNumericField("user-risk-max-order", profile.max_order_quote || 0);
      setNumericField("user-risk-max-cycle", profile.max_cycle_quote || 0);
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
            max_order_quote: numericValue("user-risk-max-order"),
            max_cycle_quote: numericValue("user-risk-max-cycle"),
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
      const connections = workspace?.connections || [];
      const readyConnections = connections.filter(
        (connection) => connection.status === "healthy"
      ).length;
      const tradablePairCount = connections.reduce(
        (total, connection) => total + (connection.markets || []).length,
        0
      );
      const summaryParts = [
        `${readyConnections}/${connections.length} ${uiText("exchange accounts ready")}`,
        `${tradablePairCount} ${uiText("tradable pairs")}`,
        `${summary.ready_strategy_count || 0}/${summary.strategy_count || 0} ${uiText("live ready")}`,
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
      document.querySelectorAll("#user-risk-profile-form input, #user-risk-profile-form button, #user-project-form input, #user-project-form button, #wallet-connection-panel input, #wallet-connection-panel select, #wallet-connection-panel button, #user-exchange-account-form input, #user-exchange-account-form textarea, #user-exchange-account-form select, #user-exchange-account-form button, #user-strategy-form input, #user-strategy-form select, #user-strategy-form button, #user-strategy-new").forEach((control) => {
        control.disabled = formsDisabled;
      });
      renderUserSetupReadiness(workspace);
      renderUserRiskProfile(workspace);
      renderUserProjectForm(workspace);
      renderUserProjects(workspace);
      renderWalletConnections(workspace);
      renderUserExchangeAccountForm(workspace);
      renderUserExchangeAccounts(workspace);
      renderUserStrategies(workspace);
      if (!formsDisabled) syncUserExchangeMarketTypes();
    }

    function renderUserQuantStrategies(workspace) {
      currentUserWorkspace = workspace || currentUserWorkspace;
      const access = currentUserWorkspace?.strategy_access?.quant || {};
      text(
        "user-quant-access-meta",
        access.enabled === false
          ? uiText("Registered account required")
          : uiText("Owner scoped · live where supported, paper otherwise")
      );
      const formsDisabled = !currentUserWorkspace
        || currentUserWorkspace.status === "user_account_required"
        || currentUserWorkspace.status === "error";
      document.querySelectorAll("#user-strategy-form input, #user-strategy-form select, #user-strategy-form button, #user-strategy-new").forEach((control) => {
        control.disabled = formsDisabled;
      });
      renderUserStrategies(currentUserWorkspace);
    }

    function renderUserMarketMakerStrategies(workspace) {
      currentUserWorkspace = workspace || currentUserWorkspace;
      const access = currentUserWorkspace?.strategy_access?.core_trading || {};
      const capacity = currentUserWorkspace?.risk_capacity || {};
      const exposureLimit = Number(capacity.max_total_exposure_quote || 0);
      const strategyLimit = Number(capacity.max_active_strategies || 0);
      const capacityText = exposureLimit > 0
        ? ` · exposure ${money.format(capacity.reserved_exposure_quote || 0)}/${money.format(exposureLimit)}`
        : "";
      const strategyCapacityText = strategyLimit > 0
        ? ` · MM ${Number(capacity.active_strategies || 0)}/${strategyLimit}`
        : "";
      text(
        "user-mm-access-meta",
        access.enabled === false
          ? uiText("Registered account required")
          : `${uiText("My accounts · live risk gated")}${capacityText}${strategyCapacityText}`
      );
      mountUserStrategyLab("trading");
      const formsDisabled = !currentUserWorkspace
        || currentUserWorkspace.status === "user_account_required"
        || currentUserWorkspace.status === "error";
      document.querySelectorAll("#user-strategy-form input, #user-strategy-form select, #user-strategy-form button, #user-strategy-new").forEach((control) => {
        control.disabled = formsDisabled
          || (control.id === "user-strategy-type" && Boolean(userStrategyViewFilter));
      });
      renderUserStrategies(currentUserWorkspace);
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
      scheduleMutationRefresh();
      return result;
    }

    async function loadUserExchangeMarkets() {
      if (userMarketDiscoveryBusy) return;
      const button = document.getElementById("user-exchange-load-markets");
      const exchange = document.getElementById("user-exchange-id").value;
      const apiVariant = document.getElementById("user-exchange-api-variant").value;
      const connectionId = document.getElementById("user-exchange-account-id").value;
      const assets = String(document.getElementById("user-exchange-assets").value || "")
        .split(/[\s,]+/)
        .map((asset) => asset.trim().toUpperCase())
        .filter((asset, index, rows) => asset && rows.indexOf(asset) === index);
      if (!exchange || !assets.length) {
        setUserWorkspaceNotice(uiText("Select an exchange and enter at least one currency."));
        return;
      }
      userMarketDiscoveryBusy = true;
      button.disabled = true;
      try {
        const result = await postUserWorkspace({
          action: "discover_markets",
          connection_id: connectionId,
          exchange,
          api_variant: apiVariant,
          assets,
        });
        const cacheKey = `account:${connectionId || exchange}:${apiVariant}`;
        discoveredUserMarkets.set(cacheKey, result.markets || []);
        const connection = workspaceConnection(connectionId);
        renderUserExchangeMarketOptions(
          [...(connection?.markets || []), ...(result.markets || [])],
          connection?.markets || []
        );
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
          .map((row) => `${row.currency} ${wholeNumber.format(row.total ?? row.free ?? 0)}`)
          .join(" · ");
        setUserWorkspaceNotice(
          `${uiText("Connection healthy")} · ${displayExchange(account.exchange, account.exchange_label)} ${account.symbol} · ${Number(check.latency_ms || 0).toFixed(0)}ms · ${check.open_order_count || 0} ${uiText("open orders")}${balances ? ` · ${balances}` : ""}`
        );
      } catch (error) {
        setUserWorkspaceNotice(`connection test failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function testUserExchangeConnection(connection, button) {
      button.disabled = true;
      try {
        const result = await postUserWorkspace({
          action: "test_connection",
          connection_id: connection.id,
        });
        const check = result.connection_test || {};
        if (check.status !== "healthy") {
          const failed = (check.results || []).find((row) => row.status !== "healthy");
          throw new Error(failed?.error || "connection test failed");
        }
        const latency = Math.max(
          0,
          ...(check.results || []).map((row) => Number(row.latency_ms || 0))
        );
        const refreshedConnection = workspaceConnection(connection.id) || connection;
        const bindingCount = (refreshedConnection.markets || []).length;
        const readinessText = refreshedConnection.live_enabled
          ? uiText("Live enabled")
          : uiText("API ready");
        setUserWorkspaceNotice(
          `${uiText("Connection healthy")} · ${readinessText} · ${bindingCount} ${uiText("synced markets")} · ${latency.toFixed(0)}ms max`
        );
      } catch (error) {
        setUserWorkspaceNotice(`connection test failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    }

    async function testSelectedUserExchangeAccount(event) {
      const connection = workspaceConnection(
        document.getElementById("user-exchange-account-id")?.value || ""
      );
      if (!connection) {
        setUserWorkspaceNotice(uiText("Save an exchange account before testing it."));
        return;
      }
      await testUserExchangeConnection(connection, event.currentTarget);
    }

    async function applyUserProject(event) {
      event.preventDefault();
      if (userProjectFormBusy) return;
      userProjectFormBusy = true;
      const button = document.getElementById("user-project-save");
      button.disabled = true;
      const id = document.getElementById("user-project-id").value.trim();
      const asset = document.getElementById("user-project-asset").value.trim().toUpperCase();
      const quoteCurrency = document.getElementById("user-project-quote").value.trim().toUpperCase();
      const project = {
        name: document.getElementById("user-project-name").value.trim() || `${asset}/${quoteCurrency}`,
        asset,
        quote_currency: quoteCurrency,
      };
      if (id) project.id = id;
      try {
        const result = await postUserWorkspace({ action: "upsert_project", project });
        const savedProject = result.project || (result.workspace?.projects || []).find((row) => (
          (id && row.id === id)
          || (!id && row.asset === asset && row.quote_currency === quoteCurrency)
        ));
        resetUserProjectForm();
        resetUserExchangeAccountForm();
        if (savedProject) {
          setFieldValue("user-exchange-project", savedProject.id);
          syncUserExchangeMarketTypes("", "", workspaceDefaultSymbol(savedProject, "spot"));
          setUserWorkspaceNotice(
            `${uiText("Project saved")} · ${savedProject.symbol} · ${uiText("Select an exchange to continue.")}`
          );
          focusWorkspaceControl("user-exchange-id");
        }
        renderUserWorkspace(currentUserWorkspace);
      } catch (error) {
        setUserWorkspaceNotice(`project update failed: ${error.message || error}`);
      } finally {
        userProjectFormBusy = false;
        button.disabled = false;
      }
    }

    async function activateUserProject(project, button) {
      button.disabled = true;
      try {
        await postUserWorkspace({ action: "activate_project", project_id: project.id });
      } catch (error) {
        setUserWorkspaceNotice(`${uiText("Project activation failed")}: ${error.message || error}`);
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

    function collectUserExchangeAccount() {
      const exchangeId = document.getElementById("user-exchange-id").value;
      const exchange = workspaceExchange(exchangeId);
      const credentials = {};
      const apiKey = document.getElementById("user-exchange-api-key").value.trim();
      const secret = document.getElementById("user-exchange-secret").value.trim();
      const passphrase = document.getElementById("user-exchange-passphrase").value.trim();
      const proxyUrl = document.getElementById("user-exchange-proxy-url").value.trim();
      if (apiKey) credentials.api_key = apiKey;
      if (secret) credentials.secret = secret;
      if (passphrase) credentials.passphrase = passphrase;
      if (proxyUrl) credentials.proxy_url = proxyUrl;
      const account = {
        label: document.getElementById("user-exchange-label").value.trim(),
        exchange: exchangeId,
        market_types: exchange?.market_types || [],
        api_variant: document.getElementById("user-exchange-api-variant").value,
        egress_mode: document.getElementById("user-exchange-egress-mode").value,
        egress_source_ip: document.getElementById("user-exchange-source-ip").value.trim(),
        egress_expected_ip: document.getElementById("user-exchange-expected-ip").value.trim(),
        withdrawal_disabled_confirmed: document.getElementById("user-exchange-no-withdraw").checked,
        trade_permission_confirmed: document.getElementById("user-exchange-trade-permission").checked,
        replace_markets: true,
        markets: Array.from(
          document.getElementById("user-exchange-symbol")?.selectedOptions || []
        ).map((option) => ({
          market_type: option.dataset.marketType || "spot",
          symbol: option.dataset.symbol || "",
          base: option.dataset.base || "",
          quote: option.dataset.quote || "",
        })),
      };
      const id = document.getElementById("user-exchange-account-id").value.trim();
      if (id) account.connection_id = id;
      if (Object.keys(credentials).length) account.credentials = credentials;
      return account;
    }

    async function saveUserExchangeAccount({ testAfterSave = false } = {}) {
      if (userExchangeAccountFormBusy) return;
      userExchangeAccountFormBusy = true;
      const saveButton = document.getElementById("user-exchange-save");
      const saveTestButton = document.getElementById("user-exchange-save-test");
      saveButton.disabled = true;
      saveTestButton.disabled = true;
      const account = collectUserExchangeAccount();
      try {
        const result = await postUserWorkspace({ action: "sync_account", account });
        const savedConnection = workspaceConnection(result.connection_id);
        if (!savedConnection) throw new Error("saved API connection was not returned by the server");
        if (testAfterSave) {
          selectedUserExchangeAccountId = savedConnection.id;
          fillUserExchangeAccountForm(savedConnection);
          await testUserExchangeConnection(savedConnection, saveTestButton);
          const refreshed = workspaceConnection(savedConnection.id);
          if (refreshed) fillUserExchangeAccountForm(refreshed);
        } else {
          resetUserExchangeAccountForm();
          const warningText = (result.warnings || []).length
            ? ` · ${(result.warnings || []).join(" · ")}`
            : "";
          setUserWorkspaceNotice(
            `${uiText("Account saved")} · ${(result.accounts || []).length} ${uiText("synced markets")}${warningText}`
          );
        }
        renderUserWorkspace(currentUserWorkspace);
      } catch (error) {
        setUserWorkspaceNotice(`account update failed: ${error.message || error}`);
      } finally {
        document.getElementById("user-exchange-api-key").value = "";
        document.getElementById("user-exchange-secret").value = "";
        document.getElementById("user-exchange-passphrase").value = "";
        document.getElementById("user-exchange-proxy-url").value = "";
        userExchangeAccountFormBusy = false;
        saveButton.disabled = false;
        saveTestButton.disabled = false;
      }
    }

    async function applyUserExchangeAccount(event) {
      event.preventDefault();
      await saveUserExchangeAccount({ testAfterSave: false });
    }

    async function saveAndTestUserExchangeAccount() {
      await saveUserExchangeAccount({ testAfterSave: true });
    }

    async function deleteUserExchangeAccount(account, button) {
      const confirmation = account.exchange === "hyperliquid" && account.agent_address
        ? "Delete local Hyperliquid credentials? Revoke this API Wallet on Hyperliquid first; local deletion cannot revoke an on-chain authorization."
        : "Delete this exchange account and its encrypted API credentials?";
      if (!dangerConfirm(confirmation)) return;
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

    async function deleteUserExchangeConnection(connection, button) {
      const count = connection.account_ids?.length || 0;
      if (!dangerConfirm(
        `Delete this API connection and ${count} synced market binding(s)?`,
        "Encrypted credentials for this connection will also be deleted."
      )) return;
      button.disabled = true;
      try {
        await postUserWorkspace({
          action: "delete_connection",
          connection_id: connection.id,
        });
        if (selectedUserExchangeAccountId === connection.id) {
          resetUserExchangeAccountForm();
        }
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
        label: project.symbol || `${project.asset}/${project.quote_currency}`,
        title: project.status,
      }));
      const selected = selectedProjectId
        || projects.find((row) => workspaceProject(row.value)?.status === "active")?.value
        || projects[0]?.value
        || "";
      setSelectOptions("user-strategy-project", projects, selected, "Select trading pair");
      return selected;
    }

    function workspaceStrategyTypeOptions(selectedType = "") {
      const rows = (currentUserWorkspace?.strategy_catalog || [])
        .filter((definition) => !userStrategyViewFilter || definition.id === userStrategyViewFilter)
        .map((definition) => ({
          value: definition.id,
          label: `${uiText(definition.label || definition.id)} · ${uiText(definition.live_supported ? "Live" : "Paper")}`,
          title: `${definition.min_accounts}-${definition.max_accounts} accounts · ${definition.live_supported ? "live" : "paper"}`,
        }));
      const selected = userStrategyViewFilter || selectedType || rows[0]?.value || "market_maker";
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
        empty.textContent = uiText("No exchange accounts support this trading pair.");
        container.appendChild(empty);
      } else {
        for (const account of accounts) {
          const contractAccount = ["spot", "swap", "future"].includes(account.market_type);
          const accountTypeAllowed = ["contract_arbitrage", "prediction_arbitrage"].includes(strategyType)
            ? contractAccount
            : account.market_type === "spot";
          const label = document.createElement("label");
          label.className = "account-option";
          const input = document.createElement("input");
          input.type = "checkbox";
          input.value = account.id;
          input.checked = selected.has(account.id) && accountTypeAllowed;
          input.disabled = !accountTypeAllowed;
          input.title = !accountTypeAllowed
            ? uiText("Account market type is not supported by this strategy.")
            : account.enabled && account.connection_fresh
            ? uiText("Account ready")
            : uiText("Account must be enabled with a fresh connection test.");
          const name = document.createElement("span");
          const venueType = ["hyperliquid", "polymarket", "dydx", "aster"].includes(account.exchange)
            ? "DEX"
            : "CEX";
          name.textContent = `${account.label} · ${venueType} ${account.market_type} · ${displayExchange(account.exchange, account.exchange_label)} ${account.symbol}`;
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

    function predictionAssetIds(value) {
      return String(value || "")
        .split(",")
        .map((item) => item.trim())
        .filter((item, index, rows) => item && rows.indexOf(item) === index);
    }

    function timestampToLocalInput(value) {
      const timestamp = Number(value || 0);
      if (!Number.isFinite(timestamp) || timestamp <= 0) return "";
      const date = new Date(timestamp * 1000);
      const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
      return local.toISOString().slice(0, 19);
    }

    function localInputToTimestamp(fieldId) {
      const value = document.getElementById(fieldId)?.value || "";
      if (!value) return 0;
      const timestamp = new Date(value).getTime() / 1000;
      return Number.isFinite(timestamp) ? timestamp : 0;
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
        setFieldValue("user-strategy-mm-depth-shape", values.depth_shape);
        setFieldValue("user-strategy-mm-min-quote", values.min_order_quote);
        setFieldValue("user-strategy-mm-min-distance", values.min_distance_bps);
        setFieldValue("user-strategy-mm-reprice", values.reprice_threshold_bps);
        setFieldValue("user-strategy-mm-hysteresis", values.reprice_hysteresis_bps);
        setFieldValue("user-strategy-mm-full-reprice", values.full_reprice_threshold_bps);
        setCheckedValue("user-strategy-mm-adaptive", values.adaptive_reprice_enabled);
        setFieldValue("user-strategy-mm-adaptive-spread", values.adaptive_reprice_spread_fraction);
        setFieldValue("user-strategy-mm-max-cancels", values.max_cancels_per_cycle);
        setFieldValue("user-strategy-mm-max-gap", values.max_order_book_gap_bps);
        setCheckedValue("user-strategy-mm-inventory-enabled", values.inventory_control_enabled);
        setFieldValue("user-strategy-mm-inventory-target", values.inventory_target_base);
        setFieldValue("user-strategy-mm-inventory-band", values.inventory_band_base);
        setFieldValue("user-strategy-mm-inventory-max", values.inventory_max_deviation_base);
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
      } else if (strategyType === "contract_arbitrage") {
        setFieldValue("user-strategy-contract-basis", values.min_basis_bps);
        setFieldValue("user-strategy-contract-funding", values.min_funding_bps);
        setFieldValue("user-strategy-contract-quote", values.max_cycle_quote);
        setFieldValue("user-strategy-contract-leverage", values.max_leverage);
        setFieldValue("user-strategy-contract-scan", values.scan_interval_seconds);
        setCheckedValue("user-strategy-contract-require-dex", values.require_dex_leg);
      } else if (strategyType === "prediction_arbitrage") {
        setFieldValue("user-strategy-prediction-mechanism", values.mechanism);
        setFieldValue("user-strategy-prediction-event", values.event_group_id);
        setFieldValue("user-strategy-prediction-assets", (values.outcome_asset_ids || []).join(", "));
        setFieldValue("user-strategy-prediction-no-assets", (values.neg_risk_no_asset_ids || []).join(", "));
        setFieldValue("user-strategy-prediction-profit", values.min_profit_bps);
        setFieldValue("user-strategy-prediction-quote", values.max_cycle_quote);
        setFieldValue("user-strategy-prediction-scan", values.scan_interval_seconds);
        setFieldValue("user-strategy-prediction-conversion-cost", values.conversion_cost_bps);
        setCheckedValue("user-strategy-prediction-augmented", values.augmented_neg_risk);
        setFieldValue("user-strategy-prediction-direction", values.event_direction);
        setFieldValue("user-strategy-prediction-strike", values.strike_price);
        setFieldValue("user-strategy-prediction-expiry", timestampToLocalInput(values.resolution_timestamp));
        setFieldValue("user-strategy-prediction-volatility", values.annualized_volatility_pct);
        setFieldValue("user-strategy-prediction-hedge-ratio", values.hedge_ratio);
        setFieldValue("user-strategy-prediction-min-expiry", values.min_time_to_resolution_seconds);
        setCheckedValue("user-strategy-prediction-resolution-confirmed", values.resolution_source_confirmed);
        setCheckedValue("user-strategy-prediction-require-dex", values.require_dex_hedge);
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
      const predictionMechanism = document.getElementById("user-strategy-prediction-mechanism")?.value || "auto";
      document.querySelectorAll("[data-prediction-mechanisms]").forEach((field) => {
        const mechanisms = String(field.dataset.predictionMechanisms || "")
          .split(/\s+/)
          .filter(Boolean);
        field.hidden = strategyType !== "prediction_arbitrage"
          || !mechanisms.includes(predictionMechanism);
      });
      if (applyDefaults) {
        setUserStrategyParameterValues(strategyType);
        setUserStrategyRiskValues(strategyType);
      }
      renderUserStrategyAccountOptions(selectedUserStrategyAccountIds());
    }

    function openUserStrategyForm(strategy = null, preferredProjectId = "") {
      if (
        userStrategyViewFilter
        && strategy
        && strategy.strategy_type !== userStrategyViewFilter
      ) return;
      selectedUserStrategyId = strategy?.id || "";
      userStrategyFormDirty = false;
      const form = document.getElementById("user-strategy-form");
      form.hidden = false;
      setFieldValue("user-strategy-id", strategy?.id || "");
      const projectId = workspaceStrategyProjectOptions(
        strategy?.project_id || preferredProjectId
      );
      const strategyType = workspaceStrategyTypeOptions(
        userStrategyViewFilter || strategy?.strategy_type || ""
      );
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
          depth_shape: document.getElementById("user-strategy-mm-depth-shape").value,
          min_order_quote: numericValue("user-strategy-mm-min-quote"),
          min_distance_bps: numericValue("user-strategy-mm-min-distance"),
          reprice_threshold_bps: numericValue("user-strategy-mm-reprice"),
          reprice_hysteresis_bps: numericValue("user-strategy-mm-hysteresis"),
          full_reprice_threshold_bps: numericValue("user-strategy-mm-full-reprice"),
          adaptive_reprice_enabled: document.getElementById("user-strategy-mm-adaptive").checked,
          adaptive_reprice_spread_fraction: numericValue("user-strategy-mm-adaptive-spread"),
          max_cancels_per_cycle: numericValue("user-strategy-mm-max-cancels"),
          max_order_book_gap_bps: numericValue("user-strategy-mm-max-gap"),
          inventory_control_enabled: document.getElementById("user-strategy-mm-inventory-enabled").checked,
          inventory_target_base: numericValue("user-strategy-mm-inventory-target"),
          inventory_band_base: numericValue("user-strategy-mm-inventory-band"),
          inventory_max_deviation_base: numericValue("user-strategy-mm-inventory-max"),
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
      if (strategyType === "contract_arbitrage") {
        return {
          min_basis_bps: numericValue("user-strategy-contract-basis"),
          min_funding_bps: numericValue("user-strategy-contract-funding"),
          max_cycle_quote: numericValue("user-strategy-contract-quote"),
          max_leverage: numericValue("user-strategy-contract-leverage"),
          scan_interval_seconds: numericValue("user-strategy-contract-scan"),
          require_dex_leg: document.getElementById("user-strategy-contract-require-dex").checked,
        };
      }
      if (strategyType === "prediction_arbitrage") {
        return {
          mechanism: document.getElementById("user-strategy-prediction-mechanism").value,
          event_group_id: document.getElementById("user-strategy-prediction-event").value.trim(),
          outcome_asset_ids: predictionAssetIds(document.getElementById("user-strategy-prediction-assets").value),
          neg_risk_no_asset_ids: predictionAssetIds(document.getElementById("user-strategy-prediction-no-assets").value),
          min_profit_bps: numericValue("user-strategy-prediction-profit"),
          max_cycle_quote: numericValue("user-strategy-prediction-quote"),
          scan_interval_seconds: numericValue("user-strategy-prediction-scan"),
          conversion_cost_bps: numericValue("user-strategy-prediction-conversion-cost"),
          augmented_neg_risk: document.getElementById("user-strategy-prediction-augmented").checked,
          event_direction: document.getElementById("user-strategy-prediction-direction").value,
          strike_price: numericValue("user-strategy-prediction-strike"),
          resolution_timestamp: localInputToTimestamp("user-strategy-prediction-expiry"),
          annualized_volatility_pct: numericValue("user-strategy-prediction-volatility"),
          hedge_ratio: numericValue("user-strategy-prediction-hedge-ratio"),
          min_time_to_resolution_seconds: numericValue("user-strategy-prediction-min-expiry"),
          resolution_source_confirmed: document.getElementById("user-strategy-prediction-resolution-confirmed").checked,
          require_dex_hedge: document.getElementById("user-strategy-prediction-require-dex").checked,
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
      const definition = workspaceStrategyDefinition(strategyType);
      const liveSupported = Boolean(definition?.live_supported);
      const strategy = {
        project_id: document.getElementById("user-strategy-project").value,
        name: document.getElementById("user-strategy-name").value.trim(),
        strategy_type: strategyType,
        account_ids: selectedUserStrategyAccountIds(),
        enabled: document.getElementById("user-strategy-enabled").checked,
        mode: liveSupported ? "live" : "paper",
        live_enabled: liveSupported,
        parameters: userStrategyParametersFromForm(strategyType),
        risk: userStrategyRiskFromForm(),
      };
      const id = document.getElementById("user-strategy-id").value.trim();
      if (id) strategy.id = id;
      return strategy;
    }

    function existingMarketMakerForAccounts(strategy) {
      if (strategy.id || strategy.strategy_type !== "market_maker") return null;
      const selectedAccounts = new Set(strategy.account_ids || []);
      return (currentUserWorkspace?.strategies || []).find(
        (row) => row.strategy_type === "market_maker"
          && (row.account_ids || []).some((accountId) => selectedAccounts.has(accountId))
      ) || null;
    }

    function userStrategyCapacityBlockers(strategy) {
      if (!strategy?.enabled || strategy.mode !== "live") return [];
      const workspace = currentUserWorkspace || {};
      const profile = workspace.risk_profile || {};
      const otherEnabled = (workspace.strategies || []).filter(
        (row) => row.id !== strategy.id && row.enabled && row.mode === "live"
      );
      const projectedCount = otherEnabled.length + 1;
      const projectedExposure = Number(strategy.risk?.max_total_quote || 0)
        + otherEnabled.reduce((sum, row) => sum + Number(row.risk?.max_total_quote || 0), 0);
      const plannedOpenOrders = strategy.strategy_type === "market_maker"
        ? Math.min(
            Number(strategy.risk?.max_open_orders || 0),
            Number(strategy.parameters?.levels || 0) * 2,
          )
        : Number(strategy.risk?.max_open_orders || 0);
      const projectedOpenOrders = plannedOpenOrders + otherEnabled.reduce((sum, row) => {
        if (row.strategy_type === "market_maker") {
          return sum + Math.min(
            Number(row.risk?.max_open_orders || 0),
            Number(row.parameters?.levels || 0) * 2,
          );
        }
        return sum + Number(row.risk?.max_open_orders || 0);
      }, 0);
      const blockers = [];
      const maxStrategies = Number(profile.max_active_strategies || 0);
      const maxExposure = Number(profile.max_total_exposure_quote || 0);
      const maxOpenOrders = Number(profile.max_open_orders || 0);
      if (maxStrategies > 0 && projectedCount > maxStrategies) {
        blockers.push(`active strategies ${projectedCount} exceed your account limit ${maxStrategies}`);
      }
      if (maxExposure > 0 && projectedExposure > maxExposure) {
        blockers.push(`planned exposure ${money.format(projectedExposure)} exceeds your account limit ${money.format(maxExposure)}`);
      }
      if (maxOpenOrders > 0 && projectedOpenOrders > maxOpenOrders) {
        blockers.push(`planned open orders ${projectedOpenOrders} exceed your account limit ${maxOpenOrders}`);
      }
      return blockers;
    }

    function formatPaperPnl(value, currency) {
      const number = Number(value);
      if (!Number.isFinite(number)) return "--";
      return `${money.format(number)} ${currency || ""}`.trim();
    }

    function paperRuntimeStatusClass(status) {
      const value = String(status || "");
      if (["running", "orders_active", "placed", "unchanged", "complete"].includes(value)) return "ok";
      if (value.startsWith("blocked") || value === "error") return "risk-blocked";
      return "subtle";
    }

    function renderUserPaperEvents(workspace, visibleStrategies = null) {
      const details = document.getElementById("user-paper-activity");
      const body = document.getElementById("user-paper-events");
      if (!details || !body) return;
      const strategies = visibleStrategies || workspace?.strategies || [];
      details.hidden = strategies.length === 0;
      body.innerHTML = "";
      const paper = workspace?.paper || {};
      const summary = paper.summary || {};
      text(
        "user-paper-summary",
        `${summary.fill_count || 0} ${uiText("fills")} · ${summary.open_order_count || 0} ${uiText("open")}`
      );
      const strategyMap = new Map(strategies.map((strategy) => [strategy.id, strategy]));
      const visibleStrategyIds = new Set(strategies.map((strategy) => strategy.id));
      const events = (paper.events || []).filter(
        (event) => visibleStrategyIds.has(event.strategy_id)
      );
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
          <td title="${escapeHtml(friendlyAccountMessage(event.reason))}">${escapeHtml(friendlyAccountMessage(event.reason) || "--")}</td>
        `;
        body.appendChild(tr);
      }
    }

    function renderUserStrategies(workspace) {
      const body = document.getElementById("user-strategies");
      if (!body) return;
      body.innerHTML = "";
      const strategies = (workspace?.strategies || []).filter(
        (strategy) => !userStrategyViewFilter || strategy.strategy_type === userStrategyViewFilter
      );
      const readyCount = strategies.filter((strategy) => strategy.effective_enabled).length;
      const runningCount = strategies.filter((strategy) => strategy.enabled).length;
      text(
        "user-strategy-meta",
        `${runningCount} ${uiText("running")} · ${readyCount}/${strategies.length} ${uiText("ready")}`
      );
      renderUserPaperEvents(workspace, strategies.filter((strategy) => strategy.mode === "paper"));
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
        const isLive = strategy.mode === "live";
        const runtime = (isLive ? strategy.live_runtime : strategy.paper_runtime) || {};
        const runtimeTerminal = Boolean(runtime.terminal);
        const runtimeStatus = !strategy.enabled
          ? "paused"
          : runtimeTerminal && runtime.status
            ? runtime.status
            : strategy.status === "blocked"
            ? "blocked"
            : runtime.status || "not_started";
        const runtimeReason = friendlyAccountMessage(runtimeTerminal
          ? runtime.reason || blockers[0] || ""
          : blockers[0] || runtime.reason || "");
        const runtimeTitle = Array.from(
          new Set([runtimeReason, ...blockers].filter(Boolean))
        ).join("; ");
        const accounts = (strategy.accounts || [])
          .map((account) => `${displayExchange(account.exchange, account.exchange_label)} ${account.symbol}`)
          .join(" · ");
        const statusClass = paperRuntimeStatusClass(runtimeStatus);
        const progress = Number(runtime.progress_pct);
        const progressText = Number.isFinite(progress)
          ? `<br><span class="subtle">${escapeHtml(`${progress.toFixed(0)}%`)}</span>`
          : "";
        const activityText = `${Number(runtime.placed_count || 0)} ${uiText("placed")} · ${Number(runtime.open_order_count || 0)} ${uiText("open")}`;
        const predictionScan = runtime.prediction_scan || {};
        const predictionBest = predictionScan.best || {};
        const predictionEdge = Number(
          predictionBest.profit_bps ?? predictionBest.model_edge_bps
        );
        const predictionText = strategy.strategy_type === "prediction_arbitrage"
          ? [
              predictionBest.mechanism || "Polymarket",
              Number.isFinite(predictionEdge) ? `${wholeNumber.format(predictionEdge)} bps` : "",
              `${Number(predictionScan.candidate_count || 0)} ${uiText("candidates")}`,
            ].filter(Boolean).join(" · ")
          : "";
        const runtimeDetail = [runtimeReason, predictionText, activityText].filter(Boolean).join(" · ");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td title="${escapeHtml(strategy.id || "")}">${escapeHtml(strategy.name || strategy.id)}<br><span class="subtle">${escapeHtml(uiText(workspaceStrategyDefinition(strategy.strategy_type)?.label || strategy.strategy_type))} · ${escapeHtml(uiText(isLive ? "Live" : "Paper"))}</span></td>
          <td>${escapeHtml(project?.symbol || strategy.project_id || "--")}</td>
          <td>${escapeHtml(accounts || "--")}</td>
          <td class="num">${formatSymbolQuantity(strategy.risk?.max_total_quote || 0, project?.symbol || "", "quote")}${progressText}</td>
          <td class="${statusClass}" title="${escapeHtml(runtimeTitle)}">${escapeHtml(uiText(runtimeStatus))}<br><span class="subtle">${escapeHtml(runtimeDetail)}</span></td>
          <td class="num">${Number(runtime.open_order_count || 0)} ${escapeHtml(uiText("open"))}<br><span class="subtle">${Number(runtime.placed_count || 0)} ${escapeHtml(uiText("placed"))} · ${Number(runtime.canceled_count || 0)} ${escapeHtml(uiText("canceled"))}</span></td>
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
        if (!isLive) {
          const resetButton = document.createElement("button");
          resetButton.className = "ghost-button";
          resetButton.type = "button";
          resetButton.textContent = uiText("Reset");
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
        const strategy = userStrategyPayloadFromForm();
        const existingMarketMaker = existingMarketMakerForAccounts(strategy);
        if (existingMarketMaker) {
          if (!dangerConfirm(
            "Update the existing Market Maker for this account?",
            `${existingMarketMaker.name} will be reconfigured instead of creating a duplicate instance. Its current live state will be preserved unless Enable Immediately is selected.`
          )) return;
          strategy.id = existingMarketMaker.id;
          strategy.enabled = Boolean(existingMarketMaker.enabled || strategy.enabled);
          setFieldValue("user-strategy-id", existingMarketMaker.id);
          setCheckedValue("user-strategy-enabled", strategy.enabled);
        }
        const capacityBlockers = userStrategyCapacityBlockers(strategy);
        if (capacityBlockers.length) {
          throw new Error(`Your account risk capacity blocks this MM: ${capacityBlockers.join("; ")}. Pause another MM, reduce this MM budget, or raise your own risk limit.`);
        }
        const isLive = strategy.mode === "live";
        if (isLive && strategy.enabled && !existingMarketMaker && !dangerConfirm(
          "Start or update this live Market Maker?",
          `${strategy.name} · ${strategy.parameters.levels} levels/side · ${strategy.parameters.quote_per_level} quote/level · maximum ${strategy.risk.max_total_quote} quote`
        )) return;
        await postUserWorkspace({
          action: "upsert_strategy",
          strategy,
          confirm_live: isLive && strategy.enabled ? LIVE_MARKET_MAKER_CONFIRMATION : "",
        });
        setUserWorkspaceNotice(uiText(
          strategy.enabled
            ? `${isLive ? "Live" : "Paper"} strategy saved and starting.`
            : `${isLive ? "Live" : "Paper"} strategy saved in paused mode.`
        ));
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
        const isLive = strategy.mode === "live";
        if (isLive && !strategy.enabled && !dangerConfirm(
          "Resume this live Market Maker?",
          `${strategy.name} · ${strategy.accounts?.map((row) => `${row.label} ${row.symbol}`).join(" · ") || "selected account"}`
        )) return;
        await postUserWorkspace({
          action: "set_strategy_enabled",
          strategy_id: strategy.id,
          enabled: !strategy.enabled,
          confirm_live: isLive && !strategy.enabled ? LIVE_MARKET_MAKER_CONFIRMATION : "",
        });
        setUserWorkspaceNotice(
          uiText(
            strategy.enabled
              ? `${isLive ? "Live" : "Paper"} strategy paused${isLive ? "; tracked orders will be canceled" : ""}.`
              : `${isLive ? "Live" : "Paper"} strategy resumed.`
          )
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
        setUserWorkspaceNotice(uiText(`Strategy copy created in paused ${strategy.mode || "paper"} mode.`));
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
      const isLive = strategy.mode === "live";
      if (!dangerConfirm(
        `Delete this ${isLive ? "live" : "paper"} strategy?`,
        isLive
          ? "Any tracked MM orders will be canceled by the strategy runtime."
          : "Its paper state, fills, and events will also be deleted."
      )) return;
      button.disabled = true;
      try {
        await postUserWorkspace({
          action: "delete_strategy",
          strategy_id: strategy.id,
        });
        if (selectedUserStrategyId === strategy.id) closeUserStrategyForm();
        setUserWorkspaceNotice(uiText(`${isLive ? "Live" : "Paper"} strategy deleted.`));
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

    const AUTO_TERMINAL_STATUSES = new Set(["complete", "stopped", "below_min_order_quote"]);
    const AUTO_CONFIG_COMPARE_FIELDS = [
      ["exchange", "Account", "string"],
      ["symbol", "Symbol", "string"],
      ["instrument_type", "Instrument", "string"],
      ["position_effect", "Position Action", "string"],
      ["position_side", "Position Side", "string"],
      ["margin_mode", "Margin Mode", "string"],
      ["leverage", "Leverage", "number"],
      ["max_position_quote", "Max Position", "number"],
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
      ["coordinate_market_maker", "MM Coordination", "boolean"],
    ];

    function normalizeAutoConfigValue(value, type) {
      if (type === "boolean") return Boolean(value);
      if (type === "number") {
        const number = Number(value || 0);
        return Number.isFinite(number) ? Math.round(number * 1e12) / 1e12 : 0;
      }
      return String(value ?? "").trim();
    }

    function autoConfigValueText(value, type, key = "") {
      if (type === "boolean") return Boolean(value) ? "on" : "off";
      if (type === "number") {
        const formatter = ["start_price", "stop_price"].includes(key) ? priceNumber : wholeNumber;
        return formatter.format(Number(value || 0));
      }
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
        ? `${quoteCurrency(config.symbol)} ${wholeNumber.format(config.total_quote)}`
        : Number(config.total_base || 0) > 0
        ? `${baseCurrency(config.symbol)} ${wholeQuantity.format(config.total_base)}`
        : (config.unlimited_total ? "Unlimited" : "No target");
      const slice = config.slice_mode === "top_level"
        ? "Top level size"
        : Number(config.slice_base_min || 0) || Number(config.slice_base_max || 0)
        ? `${baseCurrency(config.symbol)} ${wholeNumber.format(config.slice_base_min || 0)}-${wholeNumber.format(config.slice_base_max || 0)}`
        : Number(config.slice_quote || 0) > 0
        ? `${quoteCurrency(config.symbol)} ${wholeNumber.format(config.slice_quote)}`
        : `${baseCurrency(config.symbol)} ${wholeQuantity.format(config.slice_base || 0)}`;
      const guard = config.block_conflicting_market_maker === false ? "MM guard off" : "MM guard on";
      const coordination = config.coordinate_market_maker ? "MM auto-coordinate" : "MM coordination off";
      return `${displayExchange(config.exchange, config.exchange_label) || "--"} ${config.symbol || "--"} · ${side} · ${config.price_mode || "--"} · target ${total} · size ${slice} · ${autoStartGateText(config)} · ${autoStopGateText(config)} · every ${wholeNumber.format(config.interval_seconds || 0)}s · ${guard} · ${coordination}`;
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
            task: autoConfigValueText(taskConfig[key], type, key),
            default: autoConfigValueText(defaultConfig[key], type, key),
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
          const label = `${shortId(task.id)} ${displayExchange(task.config?.exchange, task.config?.exchange_label) || "--"} ${task.config?.symbol || "--"}`;
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
      if (!order) {
        return autoTaskStartGateStatus(task, config)
          || riskReasons[0]
          || task.last_error
          || task.last_status
          || "--";
      }
      const side = String(order.side || config.side || "").toUpperCase();
      const amount = formatSymbolQuantity(order.amount, config.symbol, "base");
      const price = order.price == null ? "--" : fmt.format(order.price);
      return `${side} ${amount} @ ${price}`;
    }

    function autoTaskStartGateStatus(task, config) {
      const plan = task.last_plan || {};
      const waiting = plan.status === "waiting_for_start_price"
        || (task.status === "waiting_for_start_price" && !task.start_price_triggered);
      if (!waiting) return "";
      const trigger = Number(plan.trigger_price);
      const start = Number(config.start_price ?? plan.start_price);
      if (!(trigger > 0) || !(start > 0)) return uiText("Waiting for start price");
      const sell = String(config.side || plan.side || "").toLowerCase() === "sell";
      const priceLabel = uiText(sell ? "Best bid" : "Best ask");
      const comparison = sell ? "<" : ">";
      const gap = Math.abs(start - trigger);
      const gapBps = gap / start * 10_000;
      const quote = quoteCurrency(config.symbol);
      return `${uiText("Waiting for start price")}: ${priceLabel} ${fmt.format(trigger)} ${comparison} ${uiText("Start price")} ${fmt.format(start)} · ${uiText("Gap")} ${fmt.format(gap)} ${quote} (${gapBps.toFixed(0)} bps)`;
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
        `stop rearms: ${task.stop_price_rearm_count || 0}`,
        autoStartGateText(config),
        autoStopGateText(config),
      ];
      if (lastPlan.trigger_price != null) parts.push(`trigger: ${fmt.format(lastPlan.trigger_price)}`);
      const gateStatus = autoTaskStartGateStatus(task, config);
      if (gateStatus) parts.push(gateStatus);
      if (lastOrderId) parts.push(`last order: ${lastOrderId}`);
      for (const reason of task.last_risk?.reasons || []) {
        parts.push(`risk: ${reason}`);
      }
      const coordination = task.market_maker_coordination;
      if (coordination) {
        parts.push(
          coordination.ready
            ? "MM coordination: conflicting side withdrawn"
            : `MM coordination: ${(coordination.reasons || ["waiting for cancellation confirmation"])[0]}`,
        );
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
        const displayStatus = task.market_maker_coordination
          ? (task.market_maker_coordination.ready ? "mm_coordinated" : "coordinating_mm")
          : status;
        const terminal = AUTO_TERMINAL_STATUSES.has(status);
        const statusClass = status === "complete" ? "risk-ok" : status === "paused" || status === "stopped" ? "risk-off" : status === "recovering" || status === "coordinating_mm" ? "missing" : status === "blocked_by_risk" || status === "error" ? "risk-blocked" : "ok";
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
        const progressPct = unlimited ? "--" : `${(task.progress_pct || 0).toFixed(0)}%`;
        const configCell = autoTaskConfigCell(task, defaultConfig);
        const detailTitle = autoTaskDetailTitle(task);
        const lastText = autoTaskLastOrderText(task, config);
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td data-label="${uiText("Task")}" title="${escapeHtml(task.id || "")}">${escapeHtml(shortId(task.id))}</td>
          <td data-label="${uiText("Status")}" class="${statusClass}" title="${escapeHtml(detailTitle)}">${escapeHtml(displayStatus)}</td>
          <td data-label="${uiText("Config")}" class="${configCell.className}" title="${escapeHtml(configCell.title)}">${configCell.html}</td>
          <td data-label="${uiText("Account")}">${escapeHtml(displayExchange(config.exchange, config.exchange_label) || "--")}</td>
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
          const selfTradeGuard = task.last_risk?.self_trade_guard || {};
          const canEnableCoordination = status === "blocked_by_risk"
            && selfTradeGuard.blocked
            && !config.coordinate_market_maker
            && Number(task.filled_base || 0) === 0
            && Number(task.filled_quote || 0) === 0
            && Number(task.open_order_count || 0) === 0
            && Number(task.placed_count || 0) === 0;
          if (canEnableCoordination) {
            const coordinationButton = document.createElement("button");
            coordinationButton.className = "control-button";
            coordinationButton.type = "button";
            coordinationButton.textContent = uiText("Enable MM Coordination");
            coordinationButton.addEventListener("click", () => {
              const detail = [
                `${uiText("Account")}: ${displayExchange(config.exchange, config.exchange_label) || "--"}`,
                `${uiText("Trading pair")}: ${config.symbol || "--"}`,
                `${uiText("Side")}: ${String(config.side || "--").toUpperCase()}`,
                `${uiText("Total Quote")}: ${quoteCurrency(config.symbol)} ${money.format(config.total_quote || 0)}`,
                config.side === "buy"
                  ? uiText("The MM sell side will be withdrawn before live buying starts.")
                  : uiText("The MM buy side will be withdrawn before live selling starts."),
              ].join("\n");
              if (!dangerConfirm(
                uiText("Enable MM coordination and resume this live task?"),
                detail,
              )) return;
              controlAutoBuySellTask(
                task.id,
                "enable_mm_coordination",
                coordinationButton,
              );
            });
            action.appendChild(coordinationButton);
          }
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

    function formatPerformanceStart(timestamp) {
      if (!timestamp) return "--";
      return new Date(Number(timestamp) * 1000).toLocaleString([], {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    }

    function formatNetFlow(value, currency = "USD") {
      const numeric = Number(value || 0);
      const rounded = Math.round(numeric);
      const sign = rounded > 0 ? "+" : "";
      return `${currency} ${sign}${money.format(rounded)}`;
    }

    function performanceDetail(performance, windowName) {
      const window = performance?.[windowName];
      if (!window || window.pnl == null) {
        return uiText(performance?.reason || "Establishing P/L baseline");
      }
      const currency = performance.currency || "USD";
      const pieces = [];
      if (performance.status === "stale" && performance.reason) {
        pieces.push(`${uiText("Last reliable value")} · ${uiText(performance.reason)}`);
      }
      if (windowName === "since_inception") {
        pieces.push(`${uiText("From")} ${formatPerformanceStart(window.started_at)}`);
      }
      if (windowName === "rolling_24h") {
        pieces.push(`${uiText("From")} ${formatPerformanceStart(window.started_at)}`);
        if (window.complete_window === false) {
          pieces.push(uiText("Building 24h P/L window"));
        }
      }
      pieces.push(
        `${uiText("Net deposits/withdrawals excluded")} ${formatNetFlow(window.net_external_flow, currency)}`
      );
      const coverage = performance.cash_flow_coverage || {};
      if (coverage.status === "partial") {
        pieces.push(
          `${uiText("Cash-flow coverage")} ${coverage.covered_account_count || 0}/${coverage.account_count || 0}`
        );
      }
      return pieces.join(" · ");
    }

    function resolvedPortfolioPerformance(portfolio) {
      const performance = portfolio?.performance || {};
      const sincePnl = performance.since_inception?.pnl ?? portfolio?.total_pnl;
      const rollingPnl = performance.rolling_24h?.pnl
        ?? portfolio?.rolling_24h_pnl
        ?? performance.daily?.pnl
        ?? portfolio?.daily_total_pnl;
      if (sincePnl != null || rollingPnl != null) {
        const reliable = {
          ...performance,
          since_inception: {
            ...(performance.since_inception || {}),
            pnl: sincePnl,
          },
          rolling_24h: {
            ...(performance.rolling_24h || performance.daily || {}),
            pnl: rollingPnl,
          },
        };
        lastReliablePortfolioPerformance = reliable;
        return reliable;
      }
      if (!lastReliablePortfolioPerformance) return performance;
      return {
        ...lastReliablePortfolioPerformance,
        status: "stale",
        reason: performance.reason || "P/L refresh temporarily unavailable",
      };
    }

    function displayablePortfolioPositions(portfolio) {
      return (portfolio?.positions || []).filter((position) =>
        meetsBalanceDisplayThreshold(
          position?.position_base,
          position?.position_value,
        )
      );
    }

    function displayablePortfolioCash(portfolio) {
      const balances = portfolio?.cash_balances || {};
      const common = portfolio?.cash_balances_common || {};
      return Object.entries(balances)
        .map(([currency, amount]) => ({
          currency,
          amount: Number(amount || 0),
          valueCommon: Object.prototype.hasOwnProperty.call(common, currency)
            ? Number(common[currency])
            : null,
        }))
        .filter((row) => meetsBalanceDisplayThreshold(row.amount, row.valueCommon));
    }

    function formatCashDetail(portfolio) {
      const preferredOrder = { USDC: 0, USDT: 1, USD: 2, KRW: 3 };
      const visibleCash = displayablePortfolioCash(portfolio);
      const pieces = visibleCash
        .sort((left, right) => {
          const leftRank = preferredOrder[left.currency] ?? 99;
          const rightRank = preferredOrder[right.currency] ?? 99;
          return leftRank === rightRank
            ? left.currency.localeCompare(right.currency)
            : leftRank - rightRank;
        })
        .map((row) => `${row.currency} ${wholeQuantity.format(row.amount)}`);
      const visibleCurrencies = new Set(visibleCash.map((row) => row.currency));
      const missing = (portfolio?.cash_missing_rates || []).filter((currency) =>
        visibleCurrencies.has(currency)
      );
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

    function formatPositionDetail(portfolio, positions = displayablePortfolioPositions(portfolio)) {
      if (positions.length === 0) return "--";
      return positions
        .map((position) => {
          const accountCount = new Set(
            (position.account_breakdown || [])
              .map((row) => String(row.account || row.exchange || "").trim())
              .filter(Boolean)
          ).size;
          return [
            `${position.asset} ${wholeQuantity.format(position.position_base || 0)}`,
            formatPositionPrice(position, portfolio),
            formatPositionValue(position, portfolio),
            accountCount > 0 ? `${uiText("All accounts")} (${accountCount})` : "",
          ].filter(Boolean).join(" · ");
        })
        .join(" · ");
    }

    function formatPositionAccountTitle(portfolio, positions = displayablePortfolioPositions(portfolio)) {
      return positions
        .flatMap((position) => (position.account_breakdown || []).map((row) => {
          const wallet = String(row.wallet || "trading").toLowerCase() === "funding"
            ? uiText("Funding wallet")
            : uiText("Trading wallet");
          return `${position.asset} · ${row.account || displayExchange(row.exchange, row.exchange_label)} · ${wallet} ${wholeQuantity.format(row.amount || 0)}`;
        }))
        .join(" | ");
    }

    function formatMarkDetail(portfolio, positions = displayablePortfolioPositions(portfolio)) {
      return positions
        .map((position) => {
          const mark = position.mark_price == null ? "--" : `$${fmt.format(position.mark_price)}`;
          return `${position.asset} ${mark}`;
        })
        .join(" · ");
    }

    function formatTotalAssetsDetail(portfolio, includeMissingCurrencies = false) {
      if (portfolio?.total_asset_value == null) return "--";
      const currency = portfolio?.total_asset_currency
        || lastState?.config?.common_quote_currency
        || "USD";
      const pieces = [];
      const positionValue = portfolio?.position_value == null
        ? null
        : Number(portfolio.position_value);
      const cashValue = portfolio?.cash_value == null
        ? null
        : Number(portfolio.cash_value);
      if (Number.isFinite(positionValue)) {
        pieces.push(`${uiText("Positions")} ${currency} ${money.format(positionValue)}`);
      }
      if (Number.isFinite(cashValue)) {
        pieces.push(`${uiText("Cash Position")} ${currency} ${money.format(cashValue)}`);
      }
      const missing = portfolio?.total_asset_missing_rates || [];
      if (missing.length > 0) {
        pieces.push(
          includeMissingCurrencies
            ? `${uiText("Missing prices")}: ${missing.join("/")}`
            : `${uiText("Missing prices")} (${missing.length})`
        );
      }
      return pieces.length > 0 ? pieces.join(" · ") : "--";
    }

    function renderPortfolio(portfolio) {
      const performance = resolvedPortfolioPerformance(portfolio);
      if (!portfolio || portfolio.status === "disabled") {
        text("portfolio-total-assets", "--");
        text("portfolio-total-assets-detail", "--");
        text("portfolio-cash", "--");
        text("portfolio-cash-detail", "--");
        text("portfolio-mark", "--");
        text("portfolio-value", "--");
        const sincePnl = performance.since_inception?.pnl;
        const dailyPnl = performance.rolling_24h?.pnl;
        setPnl("portfolio-total-pnl", sincePnl);
        setPnl("portfolio-daily-pnl", dailyPnl);
        setPnl("portfolio-mm-pnl", null);
        setPnl("portfolio-arb-pnl", null);
        setPnl("portfolio-auto-pnl", null);
        setPnl("portfolio-other-pnl", null);
        setPnl("portfolio-price-pnl", null);
        const sinceDetail = performanceDetail(performance, "since_inception");
        const dailyDetail = performanceDetail(performance, "rolling_24h");
        document.getElementById("portfolio-total-pnl").title = sinceDetail;
        text("portfolio-total-pnl-detail", sinceDetail);
        text("portfolio-daily-pnl-detail", dailyDetail);
        document.getElementById("portfolio-daily-pnl").title = dailyDetail;
        return;
      }

      const positions = displayablePortfolioPositions(portfolio);
      const positionDetail = formatPositionDetail(portfolio, positions);
      const totalAssetValue = portfolio.total_asset_value == null
        ? null
        : Number(portfolio.total_asset_value);
      const totalAssetCurrency = portfolio.total_asset_currency
        || lastState?.config?.common_quote_currency
        || "USD";
      text(
        "portfolio-total-assets",
        Number.isFinite(totalAssetValue)
          ? `${totalAssetCurrency} ${money.format(totalAssetValue)}`
          : "--"
      );
      const totalAssetsDetail = formatTotalAssetsDetail(portfolio);
      text("portfolio-total-assets-detail", totalAssetsDetail);
      document.getElementById("portfolio-total-assets-detail").title = [
        formatTotalAssetsDetail(portfolio, true),
        formatPositionAccountTitle(portfolio, positions) || positionDetail,
      ].filter((value) => value && value !== "--").join(" | ");
      const visibleCash = displayablePortfolioCash(portfolio);
      const cashValues = visibleCash
        .map((row) => row.valueCommon)
        .filter((value) => value != null && Number.isFinite(value));
      const cashValue = visibleCash.length > 0 && cashValues.length > 0
        ? cashValues.reduce((sum, value) => sum + value, 0)
        : null;
      text("portfolio-cash", cashValue == null ? "--" : `$${money.format(cashValue)}`);
      const cashDetail = formatCashDetail(portfolio);
      text("portfolio-cash-detail", cashDetail);
      document.getElementById("portfolio-cash-detail").title = cashDetail;
      const markDetail = formatMarkDetail(portfolio, positions);
      text(
        "portfolio-mark",
        positions.length > 1
          ? "Mixed"
          : positions.length === 1 && positions[0].mark_price != null
            ? `$${fmt.format(positions[0].mark_price)}`
            : "--"
      );
      document.getElementById("portfolio-mark").title = markDetail || "";
      const positionValues = positions
        .map((position) => Number(position.position_value))
        .filter(Number.isFinite);
      const positionValue = positionValues.length > 0
        ? positionValues.reduce((sum, value) => sum + value, 0)
        : null;
      text("portfolio-value", positionValue == null ? "--" : `$${money.format(positionValue)}`);
      const sincePnl = performance.since_inception?.pnl ?? portfolio.total_pnl;
      const dailyPnl = performance.rolling_24h?.pnl
        ?? portfolio.rolling_24h_pnl
        ?? performance.daily?.pnl
        ?? portfolio.daily_total_pnl;
      setPnl("portfolio-total-pnl", sincePnl);
      setPnl("portfolio-daily-pnl", dailyPnl);
      const sinceDetail = performanceDetail(performance, "since_inception");
      const dailyDetail = performanceDetail(performance, "rolling_24h");
      text("portfolio-total-pnl-detail", sinceDetail);
      text("portfolio-daily-pnl-detail", dailyDetail);
      setPnl("portfolio-mm-pnl", portfolio.sources?.market_maker);
      setPnl("portfolio-arb-pnl", portfolio.sources?.arbitrage);
      setPnl("portfolio-auto-pnl", portfolio.sources?.auto_buy_sell);
      setPnl(
        "portfolio-other-pnl",
        (portfolio.sources?.manual || 0) + (portfolio.sources?.unattributed || 0)
      );
      setPnl("portfolio-price-pnl", portfolio.sources?.price_move);
      document.getElementById("portfolio-total-pnl").title = sinceDetail;
      document.getElementById("portfolio-daily-pnl").title = [
        dailyDetail,
        formatPnlSourceDetail(portfolio),
      ].filter(Boolean).join(" | ");
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
      const rounded = Math.round(Number(value));
      return `${rounded > 0 ? "+" : ""}${wholeQuantity.format(rounded)}`;
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
            <td class="num">${wholeQuantity.format(holder.amount)}</td>
            <td class="num">${holder.share_pct == null ? "--" : holder.share_pct.toFixed(0) + "%"}</td>
            <td class="num ${deltaClass(cumulativeDelta)}" title="Baseline ${holder.baseline_amount == null ? "--" : wholeQuantity.format(holder.baseline_amount)}">${formatTokenDelta(cumulativeDelta)}</td>
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
          <td class="num">${event.amount == null ? "--" : wholeQuantity.format(event.amount)}</td>
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
    const discoveredWalletProviders = new Map();
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
        title: strategy.symbol ? `${displayExchange(strategy.exchange, strategy.exchange_label) || "all"} · ${strategy.symbol}` : strategy.id,
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
        accounts: list.map((account) => [account.key, account.label, account.id, account.market_type, account.symbol, account.symbols, account.projects, account.markets, account.account_source, account.market_scope, account.workspace_connection_id]),
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
        const sourceLabel = account.account_source === "user_api"
          ? ` · ${uiText("My API")}`
          : "";
        option.textContent = `${account.label || account.key}${sourceLabel} (${account.market_type || "spot"})`;
        option.title = `${account.id || account.key} · ${(accountSymbols(account)).join(", ") || "no symbols"}`;
        accountSelect.appendChild(option);
      }
      if (selectedExchange && list.some((account) => account.key === selectedExchange)) {
        accountSelect.value = selectedExchange;
      }

      const projectSelect = document.createElement("select");
      projectSelect.dataset.projectSelector = inputName;
      projectSelect.className = "account-select";
      projectSelect.title = uiText("Currency");

      const exchangeSelect = document.createElement("select");
      exchangeSelect.dataset.exchangeSelector = inputName;
      exchangeSelect.className = "account-select";
      exchangeSelect.title = uiText("Exchange");

      const symbolSelect = document.createElement("select");
      symbolSelect.dataset.symbolSelector = inputName;
      symbolSelect.className = "account-select";
      symbolSelect.title = uiText("Trading pair");

      const usesManualSymbol = () => {
        const account = accountForKey(list, accountSelect.value);
        return inputName === "slow-account"
          && account?.market_scope === "all_supported_markets";
      };

      const syncSelectorVisibility = () => {
        const manual = usesManualSymbol();
        projectSelect.hidden = manual;
        exchangeSelect.hidden = manual;
        symbolSelect.hidden = manual;
      };

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
        placeholder.textContent = projects.length ? uiText("Select currency") : uiText("No currencies");
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
        syncSelectorVisibility();
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
      syncSelectorVisibility();
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
      return `${accountLabelForKey(config.exchange) || "account"} ${config.symbol || "symbol"} · ${status}`;
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
      const accountLabel = accountLabelForKey(config.exchange);
      if (accountLabel && accountLabel !== config.exchange) {
        return `${accountLabel} ${config.symbol || "symbol"}`;
      }
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
        const autoRecovery = runtime.auto_recovery || {};
        const nextRecoveryIn = autoRecovery.next_check_at == null
          ? null
          : Math.max(0, Number(autoRecovery.next_check_at) - Date.now() / 1000);
        const recoveryDetail = autoRecovery.status === "waiting"
          ? ` · ${uiText("Automatic recheck pending")} ${formatDurationSeconds(nextRecoveryIn)}`
          : autoRecovery.status === "checking"
            ? ` · ${uiText("recovering")}`
            : autoRecovery.status === "recovered"
              ? ` · ${uiText("Recovered")}`
              : "";
        const row = document.createElement("div");
        row.className = "instance-status-row";
        const detail = `${instance?.mode || runtime.mode || "dry_run"} · open ${runtime.open_order_count ?? 0} · placed ${runtime.placed_count ?? 0} · canceled ${runtime.canceled_count ?? 0}${recoveryDetail}`;
        const reason = friendlyAccountMessage(marketMakerStatusReason(instance)) || "--";
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
      const marketLimit = marketLimitFor(payload.exchange, payload.symbol);
      const costMin = marketLimitValue(marketLimit, "cost_min");
      const belowExchangeMinimum = costMin != null
        && payload.quote_per_level > 0
        && payload.quote_per_level < costMin;
      const quote = quoteCurrency(payload.symbol);
      return {
        ready: missing.length === 0 && !belowExchangeMinimum,
        detail: missing.length
          ? `${uiText("Missing")}: ${missing.join(", ")}`
          : belowExchangeMinimum
            ? `${uiText("quote per level")}: ${formatLimitValue(payload.quote_per_level, quote)} · ${uiText("Min notional")}: ${formatLimitValue(costMin, quote)}`
          : `${accountLabelForKey(payload.exchange)} · ${payload.symbol} · ${payload.levels} ${uiText("levels per side")}`,
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
        `${uiText("Account")}: ${accountLabelForKey(payload.exchange)}`,
        `${uiText("Trading pair")}: ${payload.symbol}`,
        `${uiText("Orders")}: ${plannedOrders} (${payload.levels} ${uiText("levels per side")})`,
        `${uiText("Quote/Level")}: ${quote} ${money.format(payload.quote_per_level)}`,
        `${uiText("Planned total")}: ${quote} ${money.format(plannedQuote)}`,
        `${uiText("Band %")}: ${wholeNumber.format(payload.price_band_pct)}`,
        `${uiText("Refresh Sec")}: ${wholeNumber.format(payload.poll_seconds)}`,
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
      if (!res.ok) {
        throw new Error(friendlyAccountMessage(result.error || "market maker update failed"));
      }
      syncSelectedMarketMakerId(
        result,
        payload.id || selectedMarketMakerInstanceId,
        payload.exchange,
        payload.symbol,
      );
      return result;
    }

    function applyMarketMakerMutationResult(result) {
      if (!lastState || !result || typeof result !== "object") return;
      const current = lastState.market_maker || {};
      const incoming = result.market_maker || {};
      const previousById = new Map(
        marketMakerInstances(current).map((instance) => [instance?.config?.id || "", instance]),
      );
      const configs = Array.isArray(result.instances) ? result.instances : [];
      const instances = configs.map((config) => {
        const previous = previousById.get(config.id) || {};
        const starting = Boolean(config.enabled && config.live_enabled);
        return {
          ...previous,
          config,
          status: starting ? "starting" : "disabled",
          mode: starting ? "live" : "paused",
          status_reason: "",
          error: null,
          runtime: {
            ...(previous.runtime || {}),
            status: starting ? "starting" : "disabled",
            mode: starting ? "live" : "paused",
            reason: null,
            last_error: null,
            last_risk: null,
            updated_at: Date.now() / 1000,
          },
        };
      });
      lastState = {
        ...lastState,
        market_maker: {
          ...current,
          ...incoming,
          config: result.config || incoming.config || current.config,
          instances,
        },
        trading_console: result.trading_console || lastState.trading_console,
      };
      pageStateCache[currentPage] = lastState;
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
      document.getElementById("mm-adaptive-reprice").checked = Boolean(config.adaptive_reprice_enabled);
      setNumericField("mm-adaptive-spread", config.adaptive_reprice_spread_fraction ?? 0.05);
      setNumericField("mm-poll", config.poll_seconds || 1);
      setNumericField("mm-max-order", config.max_order_quote || "");
      setNumericField("mm-max-cycle", config.max_cycle_quote || "");
      setNumericField("mm-max-open-orders", config.max_open_orders || "");
      setNumericField("mm-max-cancels", config.max_cancels_per_cycle || "");
      setNumericField("mm-max-slippage", config.max_slippage_bps || "");
      setNumericField("mm-max-gap", config.max_order_book_gap_bps || "");
      setNumericField("mm-max-book-age", config.max_order_book_age_seconds || "");
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
        adaptive_reprice_enabled: document.getElementById("mm-adaptive-reprice").checked,
        adaptive_reprice_spread_fraction: numericValue("mm-adaptive-spread"),
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
      applyMarketMakerMutationResult(result);
      scheduleMutationRefresh();
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
        const result = await postMarketMakerConfig({ copy_id: sourceId, new_id: newId });
        applyMarketMakerMutationResult(result);
        mmFormDirty = false;
        setStrategyFeedback(
          "mm-feedback",
          "Strategy copy created in stopped mode.",
          "ok",
        );
        scheduleMutationRefresh();
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
        payload.cleanup_recoverable_state = true;
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
        const result = await postMarketMakerConfig(payload);
        applyMarketMakerMutationResult(result);
        mmFormDirty = false;
        setStrategyFeedback(
          "mm-feedback",
          result.cleanup
            ? uiText("Market Maker settings saved after order-state cleanup.")
            : uiText("Market Maker settings saved."),
          "ok",
        );
        scheduleMutationRefresh();
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
        cleanup_recoverable_state: true,
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
        const result = await postMarketMakerConfig(payload);
        applyMarketMakerMutationResult(result);
        mmFormDirty = false;
        const absentCount = Number(result.cleanup?.recovery?.reconciled_absent_count || 0);
        const canceledCount = Number(result.cleanup?.canceled_count || 0);
        setStrategyFeedback(
          "mm-feedback",
          `${uiText("Live Market Maker started.")} ${uiText("Order-state cleanup completed.")} · ${absentCount} ${uiText("uncertain resolved")} · ${canceledCount} ${uiText("old orders canceled")}`,
          "ok",
        );
        scheduleMutationRefresh();
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
        cleanup_recoverable_state: true,
      };
      if (!dangerConfirm(
        "Stop this Market Maker and cancel its managed orders?",
        `${accountLabelForKey(payload.exchange)} · ${payload.symbol}`,
      )) return;
      mmFormBusy = true;
      setStrategyFeedback("mm-feedback");
      document.getElementById("mm-enabled").checked = false;
      document.getElementById("mm-live-enabled").checked = false;
      updateCoreFormStates();
      renderMarketMakerWorkflow(lastState?.market_maker);
      try {
        const result = await postMarketMakerConfig(payload);
        applyMarketMakerMutationResult(result);
        mmFormDirty = false;
        setStrategyFeedback(
          "mm-feedback",
          uiText("Market Maker stopped. Cleanup is running."),
          "ok",
        );
        scheduleMutationRefresh();
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
      const customSymbol = document.getElementById("slow-custom-symbol")?.value.trim() || "";
      return (
        customSymbol ||
        symbolSelectorValue("slow-account") ||
        ""
      );
    }

    function selectedSlowInstrumentType() {
      return document.getElementById("slow-instrument-type")?.value === "perpetual"
        ? "perpetual"
        : "spot";
    }

    function perpetualActionFields(action) {
      const value = String(action || "close_long");
      return {
        position_effect: value.startsWith("open_") ? "open" : "reduce_only",
        position_side: value.endsWith("_short") ? "short" : "long",
        side: ["open_long", "close_short"].includes(value) ? "buy" : "sell",
      };
    }

    function perpetualActionFromConfig(config = {}) {
      const effect = config.position_effect === "open" ? "open" : "close";
      const positionSide = config.position_side === "short" ? "short" : "long";
      return `${effect}_${positionSide}`;
    }

    function perpetualActionLabel(action) {
      return {
        open_long: uiText("Open / Increase Long"),
        close_long: uiText("Close Long (Reduce Only)"),
        open_short: uiText("Open / Increase Short"),
        close_short: uiText("Close Short (Reduce Only)"),
      }[action] || action;
    }

    function syncSlowInstrumentFields() {
      const instrumentType = selectedSlowInstrumentType();
      const fields = document.getElementById("slow-perpetual-fields");
      const side = document.getElementById("slow-side");
      const sideField = document.getElementById("slow-side-field");
      const unlimited = document.getElementById("slow-unlimited");
      const sliceMode = document.getElementById("slow-slice-mode");
      const contractAction = document.getElementById("slow-contract-action");
      if (fields) fields.hidden = instrumentType !== "perpetual";
      if (sideField) sideField.hidden = instrumentType === "perpetual";
      if (side) side.disabled = instrumentType === "perpetual";
      if (instrumentType === "perpetual") {
        if (unlimited) {
          unlimited.checked = false;
          unlimited.disabled = true;
        }
        if (sliceMode) {
          sliceMode.value = "configured";
          sliceMode.disabled = true;
        }
        const action = contractAction?.value || "close_long";
        if (side) side.value = perpetualActionFields(action).side;
      } else {
        if (unlimited) unlimited.disabled = false;
        if (sliceMode) sliceMode.disabled = false;
      }
      updateSlowLabels();
      updateSlowLeverageHint();
    }

    function renderSlowExecutionAccounts(accounts, selectedExchange, selectedSymbol) {
      const instrumentType = selectedSlowInstrumentType();
      const list = (Array.isArray(accounts) ? accounts : []).filter((account) => (
        instrumentType === "perpetual"
          ? account.market_type === "swap"
          : account.market_type !== "swap"
      ));
      const compatibleExchange = list.some((account) => account.key === selectedExchange)
        ? selectedExchange
        : (list[0]?.key || "");
      const compatibleSymbol = compatibleExchange === selectedExchange ? selectedSymbol : "";
      const body = document.getElementById("slow-accounts");
      if (body) body.classList.toggle("perpetual-account-options", instrumentType === "perpetual");
      const accountLabel = document.getElementById("slow-account-label");
      if (accountLabel) {
        accountLabel.textContent = instrumentType === "perpetual"
          ? uiText("Perpetual account")
          : uiText("Account / Currency / Exchange / Pair");
      }
      renderAccountSymbolSelectors("slow-accounts", "slow-account", list, compatibleExchange, compatibleSymbol, () => {
        markSlowFormDirty();
        const customSymbol = document.getElementById("slow-custom-symbol");
        const configuredSymbol = symbolSelectorValue("slow-account");
        if (customSymbol) {
          customSymbol.value = configuredSymbol || "";
        }
        syncSlowInstrumentFields();
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
      setSlowLabel("slow-max-position-label", `${uiText("Max Position")} (${quote})`);
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
      if (payload.instrument_type === "perpetual") {
        if (payload.unlimited_total) missing.push(uiText("finite total target"));
        if (payload.slice_mode !== "configured") missing.push(uiText("configured order size"));
        if (payload.position_effect === "open" && !(payload.max_position_quote > 0)) {
          missing.push(uiText("max position"));
        }
        if (!(payload.leverage > 0)) missing.push(uiText("leverage"));
      }
      return {
        ready: missing.length === 0,
        detail: missing.length
          ? `${uiText("Missing")}: ${missing.join(", ")}`
          : `${displayExchange(payload.exchange)} · ${payload.symbol} · ${String(payload.side || "").toUpperCase()}`,
      };
    }

    function renderSlowExecutionWorkflow(data = lastState?.slow_execution) {
      if (!data || !document.getElementById("slow-side")) return;
      const payload = slowExecutionPayloadFromForm();
      const parameters = slowExecutionFormReadiness(payload);
      const risk = coreLiveRiskReadiness("slow_execution", [payload.exchange]);
      const derivativeRisk = slowDerivativeRiskReadiness(payload);
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
      const riskReady = risk.ready && derivativeRisk.ready;
      const readyToStart = parameters.ready && riskReady;
      renderStrategyWorkflow("slow-workflow", [
        {
          title: "Parameters",
          state: parameters.ready ? "ready" : "blocked",
          label: parameters.ready ? (slowFormDirty ? "Unsaved" : "Ready") : "Required",
          detail: parameters.detail,
        },
        {
          title: "Risk Check",
          state: riskReady ? "ready" : "blocked",
          label: riskReady ? "Ready" : "Blocked",
          detail: risk.ready ? derivativeRisk.detail : risk.detail,
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
        createButton.disabled = slowFormBusy || !parameters.ready || !riskReady;
      }
      if (riskButton) riskButton.hidden = riskReady;
      updateSlowLeverageHint();
    }

    function slowExecutionConfirmationDetail(payload) {
      const base = baseCurrency(payload.symbol);
      const quote = quoteCurrency(payload.symbol);
      let total = uiText("Unlimited");
      if (!payload.unlimited_total) {
        total = payload.total_quote > 0
          ? `${quote} ${money.format(payload.total_quote)}`
          : `${base} ${wholeQuantity.format(payload.total_base)}`;
      }
      const size = payload.slice_mode === "top_level"
        ? uiText("Match top-of-book size")
        : `${base} ${wholeNumber.format(payload.slice_base_min)} - ${wholeNumber.format(payload.slice_base_max)}`;
      const side = String(payload.side || "").toLowerCase();
      const gatePrice = side === "buy" ? "Ask" : "Bid";
      const startOperator = side === "buy" ? "<=" : ">=";
      const stopOperator = side === "buy" ? ">=" : "<=";
      const details = [
        `${uiText("Account")}: ${displayExchange(payload.exchange)}`,
        `${uiText("Trading pair")}: ${payload.symbol}`,
        `${uiText("Instrument")}: ${payload.instrument_type}`,
        `${uiText("Side")}: ${side.toUpperCase()}`,
        `${uiText("Total target")}: ${total}`,
        `${uiText("Each order")}: ${size}`,
        `${uiText("Price Mode")}: ${payload.price_mode}`,
        `${uiText("Place Sec")}: ${wholeNumber.format(payload.interval_seconds)}`,
        `${uiText("Start Gate")}: ${payload.start_price > 0 ? `${gatePrice} ${startOperator} ${fmt.format(payload.start_price)} ${quote}` : uiText("Immediate")}`,
        `${uiText("Stop Gate")}: ${payload.stop_price > 0 ? `${gatePrice} ${stopOperator} ${fmt.format(payload.stop_price)} ${quote}` : uiText("None")}`,
        `${uiText("MM Coordination")}: ${payload.coordinate_market_maker ? uiText("On") : uiText("Off")}`,
      ];
      if (payload.instrument_type === "perpetual") {
        const contractAction = perpetualActionFromConfig(payload);
        details.splice(
          3,
          0,
          `${uiText("Contract Action")}: ${perpetualActionLabel(contractAction)}`,
          `${uiText("Margin Mode")}: ${payload.margin_mode}`,
          `${uiText("Leverage")}: ${wholeNumber.format(payload.leverage)}x`,
          `${uiText("Max Position")}: ${quote} ${wholeNumber.format(payload.max_position_quote)}`,
        );
      }
      return details.join("\n");
    }

    function renderSlowExecutionConfig(config, accounts) {
      if (!config || slowFormDirty || slowFormBusy) {
        updateCoreFormStates();
        updateSlowMarketLimitHint();
        renderSlowExecutionWorkflow(lastState?.slow_execution);
        return;
      }
      document.getElementById("slow-enabled").checked = Boolean(config.enabled);
      document.getElementById("slow-instrument-type").value = config.instrument_type === "perpetual"
        ? "perpetual"
        : "spot";
      renderSlowExecutionAccounts(config.accounts || accounts, config.exchange || "", config.symbol || "");
      document.getElementById("slow-side").value = config.side || "sell";
      document.getElementById("slow-custom-symbol").value = config.symbol || "";
      document.getElementById("slow-contract-action").value = perpetualActionFromConfig(config);
      document.getElementById("slow-margin-mode").value = config.margin_mode || "isolated";
      setNumericField("slow-leverage", config.leverage || 1);
      setNumericField("slow-max-position", config.max_position_quote || 0);
      syncSlowInstrumentFields();
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
      document.getElementById("slow-coordinate-mm").checked = Boolean(config.coordinate_market_maker);
      updateSlowMarketLimitHint();
      updateCoreFormStates();
      renderSlowExecutionWorkflow(lastState?.slow_execution);
    }

    function slowExecutionPayloadFromForm() {
      const instrumentType = selectedSlowInstrumentType();
      const contract = perpetualActionFields(
        document.getElementById("slow-contract-action").value,
      );
      return {
        enabled: document.getElementById("slow-enabled").checked,
        exchange: selectedSlowAccount(),
        symbol: selectedSlowSymbol(),
        side: instrumentType === "perpetual" ? contract.side : document.getElementById("slow-side").value,
        instrument_type: instrumentType,
        position_effect: contract.position_effect,
        position_side: contract.position_side,
        position_mode: "one_way",
        margin_mode: document.getElementById("slow-margin-mode").value,
        leverage: numericValue("slow-leverage"),
        max_position_quote: numericValue("slow-max-position"),
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
        block_conflicting_market_maker: true,
        coordinate_market_maker: document.getElementById("slow-coordinate-mm").checked,
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
        scheduleMutationRefresh();
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
      const stopReleaseButton = document.getElementById("rebalance-stop-release");
      const residual = runtime.residual_exposure || {};
      const hasReviewableResidual = runtime.halted
        && runtime.halt_reason === "hedge_required"
        && Number(residual.quantity_base || 0) > 0;
      if (acknowledgeButton) {
        acknowledgeButton.hidden = !hasReviewableResidual;
        acknowledgeButton.disabled = rebalanceFormBusy || !hasReviewableResidual;
      }
      if (stopReleaseButton) {
        stopReleaseButton.hidden = !hasReviewableResidual;
        stopReleaseButton.disabled = rebalanceFormBusy || !hasReviewableResidual;
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
        `${uiText("Max Cost bps")}: ${wholeNumber.format(payload.max_cost_bps)}`,
        `${uiText("Max Slippage bps")}: ${wholeNumber.format(payload.max_slippage_bps)}`,
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
        scheduleMutationRefresh();
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
        scheduleMutationRefresh();
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
        `${wholeQuantity.format(quantity)} ${asset}\nNo order will be placed. MM may resume, but rebalance remains blocked until reset and a new live confirmation.`,
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
        scheduleMutationRefresh();
      } catch (error) {
        setRebalanceFeedback(error.message || String(error), "error");
      } finally {
        rebalanceFormBusy = false;
        renderRebalanceReadiness(lastState?.cross_exchange_rebalance);
        updateCoreFormStates();
      }
    }

    async function stopRebalanceAndReleaseMm() {
      if (rebalanceFormBusy) return;
      const runtime = lastState?.cross_exchange_rebalance?.runtime || {};
      const residual = runtime.residual_exposure || {};
      const quantity = Number(residual.quantity_base || 0);
      const asset = residual.asset || baseCurrency(
        lastState?.cross_exchange_rebalance?.config?.buy_symbol || "",
      );
      if (!dangerConfirm(
        "Stop rebalance and release MM?",
        `${wholeQuantity.format(quantity)} ${asset}\nNo hedge order will be placed. The residual remains in the audit log.`,
      )) return;
      const button = document.getElementById("rebalance-stop-release");
      rebalanceFormBusy = true;
      button.disabled = true;
      try {
        const res = await fetch("/api/cross-exchange-rebalance", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action: "stop_and_release",
            confirm_stop: "STOP REBALANCE AND RELEASE MM",
          }),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.error || "stop and release failed");
        setRebalanceFeedback("Rebalance stopped and MM coordination released.", "ok");
        scheduleMutationRefresh();
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
      const reason = friendlyAccountMessage(runtime.halt_reason
        || (lastPayload.risk?.reasons || [])[0]
        || (lastPayload.errors || [])[0]
        || "");
      text(
        "rebalance-meta",
        `${mode} · ${status} · ${progressPct.toFixed(0)}%${plan ? ` · cost ${Number(plan.expected_cost_bps || 0).toFixed(0)} bps` : ""}${coordinationStatus ? ` · MM ${coordinationStatus}` : ""}${reason ? ` · ${reason}` : ""}`,
      );
      const progress = document.getElementById("rebalance-progress");
      const residual = runtime.residual_exposure || {};
      const acknowledgedResidual = Boolean(runtime.residual_exposure_acknowledged);
      progress.innerHTML = `
        <span class="config-chip ${runtime.halted ? "config-diff" : "config-match"}">${escapeHtml(status)}</span>
        <span>${uiText("Source spent")} ${escapeHtml(common)} ${money.format(completed)} / ${money.format(target)} · ${uiText("remaining")} ${money.format(remaining)}</span>
        <span>${uiText("Destination received")} ${escapeHtml(common)} ${money.format(destinationReceived)}</span>
        <span>${escapeHtml(baseCurrency(plan?.buy_symbol || data?.config?.buy_symbol || ""))} ${wholeQuantity.format(runtime.completed_base || 0)}</span>
        ${Number(residual.quantity_base || 0) > 0 ? `<span>${acknowledgedResidual ? "Acknowledged" : "Residual"}: ${escapeHtml(wholeQuantity.format(residual.quantity_base))} ${escapeHtml(residual.asset || "")}</span>` : ""}
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
          local: `${plan.buy_quote_currency} ${wholeNumber.format(plan.buy_cost_local)}`,
          common: `${common} ${money.format(plan.buy_cost_common)}`,
        },
        {
          role: "Cash Destination",
          exchange: plan.sell_exchange,
          symbol: plan.sell_symbol,
          side: "sell",
          price: plan.sell_average_price,
          base: plan.quantity_base,
          local: `${plan.sell_quote_currency} ${wholeNumber.format(plan.sell_proceeds_local)}`,
          common: `${common} ${money.format(plan.sell_proceeds_common)}`,
        },
      ];
      for (const row of rows) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${escapeHtml(uiText(row.role))}</td>
          <td>${escapeHtml(displayExchange(row.exchange, row.exchange_label) || "--")}</td>
          <td>${escapeHtml(row.symbol || "--")}</td>
          <td class="${row.side === "buy" ? "side-buy" : "side-sell"}">${row.side.toUpperCase()}</td>
          <td class="num">${fmt.format(row.price || 0)}</td>
          <td class="num">${wholeQuantity.format(row.base || 0)}</td>
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
      const derivativeRisk = slowDerivativeRiskReadiness(payload);
      if (!parameters.ready || !risk.ready || !derivativeRisk.ready) {
        setStrategyFeedback(
          "slow-feedback",
          parameters.ready
            ? (risk.ready ? derivativeRisk.detail : risk.detail)
            : parameters.detail,
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
        scheduleMutationRefresh();
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
          body: JSON.stringify({
            action,
            cancel_open_orders: action === "stop",
            confirm_live: action === "enable_mm_coordination"
              ? LIVE_AUTO_BUY_SELL_CONFIRMATION
              : "",
          }),
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
      return `${shortId(task.id)} · ${task.status || "--"} · ${displayExchange(task.exchange, task.exchange_label) || "--"} ${task.symbol || "--"} · ${side} · ${filled}`;
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

    function setHeaderStatus(statusValue, label, detail = "") {
      const status = document.getElementById("status");
      const normalized = statusValue || "starting";
      headerStatusIssue = detail
        ? {
            severity: normalized === "error" ? "error" : "attention",
            title: label || statusLabel(normalized),
            reason: friendlyAccountMessage(detail),
            meta: [],
            action: "",
          }
        : null;
      const issueCount = statusIssuesWithConnectionState().length;
      const clickable = ["degraded", "error", "auto_stopped"].includes(normalized)
        && issueCount > 0;
      status.textContent = label || statusLabel(normalized);
      status.className = `pill ${pillClassForStatus(normalized)} status-trigger${clickable ? " is-clickable" : ""}`;
      status.setAttribute("aria-disabled", clickable ? "false" : "true");
      status.title = clickable
        ? `${uiText("Click to view details")} · ${issueCount}`
        : "";
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

    function formatTickerPrice(value) {
      const price = Number(value);
      if (!Number.isFinite(price) || price <= 0) return "--";
      const maximumFractionDigits = price >= 1000
        ? 2
        : price >= 1
          ? 4
          : price >= 0.01
            ? 6
            : 10;
      return new Intl.NumberFormat("en-US", { maximumFractionDigits }).format(price);
    }

    function renderMarketTickers(payload = marketTickerPayload) {
      if (!payload) return;
      marketTickerPayload = payload;
      const list = document.getElementById("market-ticker-list");
      const updated = document.getElementById("market-ticker-updated");
      if (!list || !updated) return;
      updated.textContent = payload.updated_at
        ? `${uiText("24h")} · ${formatAge(payload.updated_at)}`
        : "--";
      list.innerHTML = "";
      const rows = Array.isArray(payload.items) ? payload.items : [];
      if (!rows.length) {
        const empty = document.createElement("div");
        empty.className = "market-ticker-empty subtle";
        empty.textContent = uiText("Data unavailable");
        list.appendChild(empty);
        return;
      }
      for (const row of rows) {
        const item = document.createElement("div");
        item.className = "market-ticker-item";
        const percentage = Number(row.change_24h_pct);
        const hasChange = row.change_24h_pct != null && Number.isFinite(percentage);
        const changeClass = hasChange
          ? percentage > 0
            ? "positive"
            : percentage < 0
              ? "negative"
              : ""
          : "";
        const changeText = hasChange
          ? `${percentage > 0 ? "+" : ""}${percentage.toFixed(0)}%`
          : "--";
        item.innerHTML = `
          <div class="market-ticker-symbol-row">
            <span class="market-ticker-symbol" title="${escapeHtml(row.symbol || "")}">${escapeHtml(row.symbol || "--")}</span>
            <span class="market-ticker-type">${escapeHtml(uiText(row.market_type === "swap" ? "Perpetual" : "Spot"))}</span>
          </div>
          <div class="market-ticker-price-row">
            <span class="market-ticker-price">${escapeHtml(formatTickerPrice(row.price))}</span>
            <span class="market-ticker-change ${changeClass}">${escapeHtml(changeText)}</span>
          </div>
          <div class="market-ticker-source" title="${escapeHtml(row.exchange_label || row.exchange || "")}">${escapeHtml(row.exchange_label || row.exchange || "--")}</div>
        `;
        list.appendChild(item);
      }
    }

    function renderMarketTickerEditor() {
      const accountSelect = document.getElementById("market-ticker-account");
      const draft = document.getElementById("market-ticker-draft");
      if (!accountSelect || !draft || !marketTickerPayload) return;
      const accounts = Array.isArray(marketTickerPayload.accounts)
        ? marketTickerPayload.accounts
        : [];
      const previousAccount = accountSelect.value;
      accountSelect.innerHTML = "";
      for (const account of accounts) {
        const option = document.createElement("option");
        option.value = account.key;
        option.textContent = `${account.label} · ${uiText(account.market_type === "swap" ? "Perpetual" : "Spot")}`;
        accountSelect.appendChild(option);
      }
      if (accounts.some((account) => account.key === previousAccount)) {
        accountSelect.value = previousAccount;
      }
      draft.innerHTML = "";
      for (const [index, row] of marketTickerDraft.entries()) {
        const account = accounts.find((candidate) => candidate.key === row.exchange);
        const item = document.createElement("div");
        item.className = "market-ticker-draft-item";
        const position = document.createElement("span");
        position.className = "market-ticker-draft-position";
        position.textContent = String(index + 1);
        const label = document.createElement("span");
        label.className = "market-ticker-draft-label";
        const symbol = document.createElement("strong");
        symbol.textContent = row.symbol;
        const source = document.createElement("span");
        source.textContent = account?.label || row.exchange;
        label.append(symbol, source);
        const actions = document.createElement("span");
        actions.className = "market-ticker-draft-actions";
        const moveUp = document.createElement("button");
        moveUp.type = "button";
        moveUp.className = "market-ticker-order-button";
        moveUp.title = uiText("Move up");
        moveUp.setAttribute("aria-label", uiText("Move up"));
        moveUp.textContent = "\u2191";
        moveUp.disabled = index === 0;
        moveUp.addEventListener("click", () => moveMarketTickerDraftItem(index, -1));
        const moveDown = document.createElement("button");
        moveDown.type = "button";
        moveDown.className = "market-ticker-order-button";
        moveDown.title = uiText("Move down");
        moveDown.setAttribute("aria-label", uiText("Move down"));
        moveDown.textContent = "\u2193";
        moveDown.disabled = index === marketTickerDraft.length - 1;
        moveDown.addEventListener("click", () => moveMarketTickerDraftItem(index, 1));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "market-ticker-remove";
        remove.title = uiText("Remove");
        remove.setAttribute("aria-label", uiText("Remove"));
        remove.textContent = "×";
        remove.addEventListener("click", () => {
          marketTickerDraft.splice(index, 1);
          renderMarketTickerEditor();
        });
        actions.append(moveUp, moveDown, remove);
        item.append(position, label, actions);
        draft.appendChild(item);
      }
    }

    function moveMarketTickerDraftItem(index, offset) {
      const target = index + offset;
      if (
        index < 0
        || index >= marketTickerDraft.length
        || target < 0
        || target >= marketTickerDraft.length
      ) return;
      [marketTickerDraft[index], marketTickerDraft[target]] = [
        marketTickerDraft[target],
        marketTickerDraft[index],
      ];
      renderMarketTickerEditor();
    }

    function openMarketTickerEditor() {
      if (!marketTickerPayload) return;
      marketTickerDraft = (marketTickerPayload.watchlist || []).map((row) => ({ ...row }));
      document.getElementById("market-ticker-editor").hidden = false;
      document.getElementById("market-ticker-feedback").textContent = "";
      renderMarketTickerEditor();
    }

    function closeMarketTickerEditor() {
      document.getElementById("market-ticker-editor").hidden = true;
      document.getElementById("market-ticker-feedback").textContent = "";
    }

    function addMarketTickerDraftItem() {
      const accountSelect = document.getElementById("market-ticker-account");
      const symbolInput = document.getElementById("market-ticker-symbol");
      const feedback = document.getElementById("market-ticker-feedback");
      let symbol = symbolInput.value.trim().toUpperCase().replaceAll(" ", "");
      const account = (marketTickerPayload?.accounts || []).find(
        (row) => row.key === accountSelect.value,
      );
      if (account?.market_type === "swap" && symbol.includes("/") && !symbol.includes(":")) {
        symbol = `${symbol}:${symbol.split("/", 2)[1]}`;
      }
      if (!account || !/^[A-Z0-9._-]+\/[A-Z0-9._-]+(?::[A-Z0-9._-]+)?$/.test(symbol)) {
        feedback.textContent = uiText("Use BASE/QUOTE or BASE/QUOTE:SETTLE format");
        return;
      }
      if (marketTickerDraft.some((row) => row.exchange === account.key && row.symbol === symbol)) {
        feedback.textContent = uiText("Market already added");
        return;
      }
      if (marketTickerDraft.length >= 20) {
        feedback.textContent = uiText("Maximum 20 markets");
        return;
      }
      marketTickerDraft.push({ exchange: account.key, symbol });
      symbolInput.value = "";
      feedback.textContent = "";
      renderMarketTickerEditor();
    }

    async function saveMarketTickerWatchlist() {
      const feedback = document.getElementById("market-ticker-feedback");
      const save = document.getElementById("market-ticker-save");
      if (!marketTickerDraft.length) {
        feedback.textContent = uiText("Add at least one market");
        return;
      }
      save.disabled = true;
      feedback.textContent = uiText("Saving");
      try {
        const response = await fetchWithTimeout("/api/market-tickers", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ items: marketTickerDraft }),
        }, 15000);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "watchlist save failed");
        marketTickerLoadedAt = Date.now();
        renderMarketTickers(payload);
        closeMarketTickerEditor();
      } catch (error) {
        feedback.textContent = error.message || String(error);
      } finally {
        save.disabled = false;
      }
    }

    async function loadMarketTickers(force = false) {
      if (currentPage !== "status" || marketTickerLoading) return;
      if (!force && marketTickerPayload && Date.now() - marketTickerLoadedAt < 8000) {
        renderMarketTickers(marketTickerPayload);
        return;
      }
      marketTickerLoading = true;
      try {
        const response = await fetchWithTimeout(
          "/api/market-tickers",
          { cache: "no-store" },
          15000,
        );
        if (!response.ok) throw new Error(`market ticker request failed (${response.status})`);
        const payload = await response.json();
        marketTickerLoadedAt = Date.now();
        renderMarketTickers(payload);
      } catch (error) {
        const updated = document.getElementById("market-ticker-updated");
        if (updated) updated.textContent = uiText("Data unavailable");
      } finally {
        marketTickerLoading = false;
      }
    }

    function renderCommonState(data) {
      const stateStatus = data.status || "starting";
      const visibleStatus = stateStatus === "running" && statusIssueRows(data).length
        ? "degraded"
        : stateStatus;
      setHeaderStatus(visibleStatus);
      renderAuthProfile(data.auth);
      if (Array.isArray(data.account_balances?.accounts)) {
        renderProfileAccounts(data.account_balances);
      }
      document.getElementById("program-toggle").checked = data.program?.running !== false;

      text("scan-count", data.scan?.count ?? 0);
      text("latency", data.scan?.elapsed_ms == null ? "--" : `${data.scan.elapsed_ms} ms`);
      text("opp-count", data.opportunities?.length ?? 0);
      text("notional", data.config ? `$${money.format(data.config.notional_quote)}` : "--");
      text("threshold", data.config ? `$${wholeNumber.format(data.config.min_profit_quote || 0)} / ${wholeNumber.format(data.config.min_profit_bps || 0)} bps` : "--");
      text("updated", formatAge(data.scan?.last_finished));
      text("onchain-status", data.onchain?.status || "off");
      text("common-quote", data.config?.common_quote_currency || "USD");
      text("warnings", (data.warnings || []).join(" · "));
      text("onchain-meta", data.onchain?.mint ? `${data.onchain.label || "Token"} · ${shortAddress(data.onchain.mint)} · ${formatAge(data.onchain.last_finished)}` : "");
      renderAccountBalanceSummary(accountBalancesForProfile(
        accountBalanceDetailPayload || data.account_balances
      ));

      const mmSelected = selectedMarketMakerInstance(data.market_maker) || data.market_maker;
      const mmInstances = marketMakerInstances(data.market_maker);
      const mmRuntime = mmSelected?.runtime || data.market_maker?.runtime || {};
      const mmPlan = mmSelected?.plan || data.market_maker?.plan;
      const mmRuntimeText = mmRuntime.status ? ` · ${mmRuntime.status} · open ${mmRuntime.open_order_count ?? 0} · placed ${mmRuntime.placed_count ?? 0} · canceled ${mmRuntime.canceled_count ?? 0}` : "";
      const mmMarketData = mmRuntime.market_data || mmSelected?.market_data || data.market_maker?.market_data || {};
      const mmWsText = mmMarketData.cache?.websocket_supported === false ? " · WS unsupported" : "";
      const mmMarketDataText = mmMarketData.source
        ? ` · ${String(mmMarketData.source).toUpperCase()}${mmMarketData.age_seconds == null ? "" : ` ${Number(mmMarketData.age_seconds).toFixed(0)}s`}${mmWsText}`
        : mmWsText;
      const mmQuote = mmSelected?.quote_conversion || data.market_maker?.quote_conversion;
      const mmQuoteText = mmQuote?.quote_currency ? ` · quote ${mmQuote.quote_currency}${mmQuote.quote_to_common_rate == null ? "" : `→${mmQuote.common_quote_currency} ${mmQuote.quote_to_common_rate}`}` : "";
      const mmFeatures = mmSelected?.exchange_features || data.market_maker?.exchange_features || {};
      const mmFeatureText = Object.keys(mmFeatures).length ? ` · post-only ${mmFeatures.post_only ? "yes" : "no"}` : "";
      const mmSpreadText = mmPlan?.existing_spread_bps == null
        ? "--"
        : Number(mmPlan.existing_spread_bps).toFixed(0);
      const mmInstanceText = mmInstances.length > 1 ? `${mmInstances.length} instances · ` : "";
      const mmReason = friendlyAccountMessage(
        marketMakerStatusReason(mmSelected) || marketMakerStatusReason(data.market_maker),
      );
      const mmReasonText = mmReason ? ` · ${mmReason}` : "";
      text("mm-meta", mmPlan ? `${mmInstanceText}${mmSelected?.mode || data.market_maker?.mode || "dry_run"} · ${displayExchange(mmPlan.exchange, mmPlan.exchange_label)} ${mmPlan.symbol} · mid ${fmt.format(mmPlan.mid_price)} · spread ${mmSpreadText} bps${mmMarketDataText}${mmQuoteText}${mmFeatureText}${mmRuntimeText}${mmReasonText}` : `${mmInstanceText}${mmSelected?.status || data.market_maker?.status || "disabled"}${mmMarketDataText}${mmQuoteText}${mmFeatureText}${mmRuntimeText}${mmReasonText}`);

      const slowPlan = data.slow_execution?.plan;
      const slowPriceText = slowPlan?.order ? `order ${fmt.format(slowPlan.order.price)}` : (data.slow_execution?.status || "no order");
      text("slow-meta", slowPlan ? `${data.slow_execution.mode || "dry_run"} · ${displayExchange(slowPlan.exchange, slowPlan.exchange_label)} ${slowPlan.symbol} · ${slowPlan.side.toUpperCase()} · ${slowPriceText}` : (data.slow_execution?.status || "disabled"));

      const gridPlan = data.spot_grid?.plan;
      const gridReason = friendlyAccountMessage((data.spot_grid?.safety?.reasons || [])[0] || data.spot_grid?.error);
      text(
        "grid-meta",
        gridPlan
          ? `${data.spot_grid.mode || "dry_run"} · ${displayExchange(gridPlan.exchange, gridPlan.exchange_label)} ${gridPlan.symbol} · mid ${fmt.format(gridPlan.mid_price)} · step ${Number(gridPlan.grid_step_bps || 0).toFixed(0)} bps · orders ${(gridPlan.orders || []).length}${gridReason ? ` · ${gridReason}` : ""}`
          : `${data.spot_grid?.status || "disabled"}${gridReason ? ` · ${gridReason}` : ""}`
      );

      const dcaPlan = data.dca?.plan;
      const dcaReason = friendlyAccountMessage((data.dca?.safety?.reasons || [])[0] || data.dca?.error);
      const dcaNext = dcaPlan?.next_order ? `next ${fmt.format(dcaPlan.next_order.price)}` : (dcaPlan?.reason || data.dca?.status || "disabled");
      text(
        "dca-meta",
        dcaPlan
          ? `${data.dca.mode || "dry_run"} · ${displayExchange(dcaPlan.exchange, dcaPlan.exchange_label)} ${dcaPlan.symbol} · ${String(dcaPlan.side || "").toUpperCase()} · ${dcaNext} · ${dcaPlan.max_orders || 0} orders${dcaReason ? ` · ${dcaReason}` : ""}`
          : `${data.dca?.status || "disabled"}${dcaReason ? ` · ${dcaReason}` : ""}`
      );

      const execPlan = data.execution_algo?.plan;
      const execReason = friendlyAccountMessage((data.execution_algo?.safety?.reasons || [])[0] || data.execution_algo?.error);
      text(
        "exec-meta",
        execPlan
          ? `${data.execution_algo.mode || "dry_run"} · ${displayExchange(execPlan.exchange, execPlan.exchange_label)} ${execPlan.symbol} · ${String(execPlan.algo || "").toUpperCase()} ${String(execPlan.side || "").toUpperCase()} · ${formatSymbolQuantity(execPlan.total_quote || 0, execPlan.symbol, "quote")} · slices ${(execPlan.schedule || []).length}${execReason ? ` · ${execReason}` : ""}`
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
        renderOpenSection("user-market-maker", () => renderUserMarketMakerStrategies(data.user_workspace));
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
	        renderOpenSection("user-quant-strategies", () => renderUserQuantStrategies(data.user_workspace));
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
      loadMarketTickers();
      loadAccountBalanceDetails();
      renderOpenSection("readiness-actions", () => renderReadiness(data.readiness, data.runtime_store));
      renderOpenSection("markets", () => renderMarkets(data.markets));
      renderOpenSection("account-balances", () => renderAccountBalances(
        accountBalanceDetailPayload || data.account_balances
      ));
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
          setHeaderStatus("degraded", "Retrying", `Connecting to server: ${message}`);
          text("warnings", `Connecting to server: ${message}`);
        } else if (refreshFailureCount < 2) {
          // A single missed poll on a healthy session is usually a transient
          // blip; retry silently instead of flashing the header pill.
        } else if (refreshFailureCount < 3) {
          setHeaderStatus("degraded", "Reconnecting", `Connection retry ${refreshFailureCount}/3: ${message}`);
          text("warnings", `Connection retry ${refreshFailureCount}/3: ${message}`);
        } else {
          setHeaderStatus("degraded", "Stale", `State is stale: ${message}`);
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

    function scheduleMutationRefresh(delayMs = 200) {
      if (mutationRefreshTimer) window.clearTimeout(mutationRefreshTimer);
      mutationRefreshTimer = window.setTimeout(() => {
        mutationRefreshTimer = null;
        refresh({ force: true });
      }, Math.max(0, delayMs));
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

	    mountUserStrategyLab();
	    applyFeatureVisibility();
    setupCompactSections();
    setActivePage(pageFromLocation(), { refresh: false });
    window.addEventListener("hashchange", () => {
      setActivePage(pageFromLocation());
    });

    window.addEventListener("eip6963:announceProvider", (event) => {
      registerWalletProvider(event.detail?.provider, event.detail?.info || {});
    });
    window.dispatchEvent(new Event("eip6963:requestProvider"));
    for (const provider of window.ethereum?.providers || [window.ethereum]) {
      registerWalletProvider(provider, {});
    }

    const statusTrigger = document.getElementById("status");
    statusTrigger.addEventListener("click", () => {
      if (statusTrigger.getAttribute("aria-disabled") !== "true") openStatusDetails();
    });
    const statusDetailDialog = document.getElementById("status-detail-dialog");
    statusDetailDialog.addEventListener("click", (event) => {
      if (event.target === statusDetailDialog) statusDetailDialog.close();
    });

    refresh({ force: true });
    document.getElementById("program-toggle").addEventListener("change", (event) => {
      setProgramRunning(event.target.checked);
    });
    document.getElementById("profile-asset").addEventListener("change", updateProfileAsset);
    document.getElementById("profile-account").addEventListener("change", (event) => {
      accountBalanceDetailSignature = "";
      renderAccountBalanceSummary(accountBalancesForProfile(
        accountBalanceDetailPayload || lastState?.account_balances
      ));
      renderAccountBalances(accountBalanceDetailPayload || lastState?.account_balances);
    });
    document.getElementById("balance-currency-filter").addEventListener("change", () => {
      accountBalanceDetailSignature = "";
      renderAccountBalances(accountBalanceDetailPayload || lastState?.account_balances);
      applyMobileTableLabels();
    });
    document.getElementById("account-balances-refresh").addEventListener("click", () => {
      loadAccountBalanceDetails({ force: true });
    });
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
	    document.getElementById("wallet-connect").addEventListener("click", connectAndVerifyWallet);
	    document.getElementById("wallet-open-imtoken").addEventListener("click", openCurrentPageInImToken);
	    document.getElementById("wallet-venue-test").addEventListener("click", testWalletVenue);
	    document.getElementById("wallet-venue-refresh-all").addEventListener("click", refreshAllVenueConnections);
	    document.getElementById("wallet-venue-select").addEventListener("change", () => {
	      renderWalletConnections(currentUserWorkspace);
	    });
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
        "user-exchange-egress-mode",
	      ].includes(event.target?.id)) {
	        if (event.target?.id === "user-exchange-id" && !selectedUserExchangeAccountId) {
	          const symbolSelect = document.getElementById("user-exchange-symbol");
	          if (symbolSelect) symbolSelect.replaceChildren();
                  const labelInput = document.getElementById("user-exchange-label");
                  if (labelInput) {
                    labelInput.value = suggestedWorkspaceAccountLabel(event.target.value);
                  }
	        }
	        syncUserExchangeMarketTypes();
	        syncUserExchangeEgressFields();
	      }
	    });
	    document.getElementById("user-exchange-account-form").addEventListener("submit", applyUserExchangeAccount);
	    document.getElementById("user-exchange-save-test").addEventListener("click", saveAndTestUserExchangeAccount);
	    document.getElementById("user-exchange-new").addEventListener("click", resetUserExchangeAccountForm);
	    document.getElementById("user-exchange-load-markets").addEventListener("click", loadUserExchangeMarkets);
	    document.getElementById("user-exchange-test").addEventListener("click", testSelectedUserExchangeAccount);
	    document.getElementById("user-hyperliquid-authorize").addEventListener("click", authorizeHyperliquidWithMetaMask);
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
      } else if (event.target?.id === "user-strategy-prediction-mechanism") {
        syncUserStrategyTypeFields();
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
    document.getElementById("slow-instrument-type").addEventListener("change", () => {
      renderSlowExecutionAccounts(lastState?.slow_execution?.accounts || [], "", "");
      syncSlowInstrumentFields();
      updateSlowLabels();
      renderSlowExecutionWorkflow(lastState?.slow_execution);
    });
    document.getElementById("slow-contract-action").addEventListener("change", syncSlowInstrumentFields);
    document.getElementById("slow-custom-symbol").addEventListener("input", updateSlowLabels);
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
    document.getElementById("rebalance-stop-release").addEventListener(
      "click",
      stopRebalanceAndReleaseMm,
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
      updateSlowLeverageHint();
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
      renderMarketTickers();
      if (!document.getElementById("market-ticker-editor").hidden) {
        renderMarketTickerEditor();
      }
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
    document.getElementById("market-ticker-edit").addEventListener("click", openMarketTickerEditor);
    document.getElementById("market-ticker-cancel").addEventListener("click", closeMarketTickerEditor);
    document.getElementById("market-ticker-add").addEventListener("click", addMarketTickerDraftItem);
    document.getElementById("market-ticker-save").addEventListener("click", saveMarketTickerWatchlist);
    document.getElementById("market-ticker-symbol").addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      addMarketTickerDraftItem();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearRefreshTimer();
        closeStateStream();
      } else {
        refresh({ force: true });
      }
    });
