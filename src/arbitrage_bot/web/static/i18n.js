(() => {
  const STORAGE_KEY = "cryptoArbLanguage";
  const SUPPORTED = new Set(["en", "zh"]);
  const ZH = {
    "Crypto Trading Dashboard": "加密交易控制台",
    "Multi-asset arbitrage · market making · auto buy/sell": "多币种套利 · 做市 · 自动买卖",
    "Language": "语言",
    "English": "English",
    "Legacy": "旧版",
    "Logout": "退出",
    "Program": "程序",
    "Status": "状态",
    "Settings": "设置",
    "Records": "记录",
    "Starting": "启动中",
    "Retrying": "重试中",
    "Reconnecting": "重连中",
    "Stale": "数据过期",
    "Position": "仓位",
    "Cash Position": "现金仓位",
    "Balances": "余额",
    "Mark Price": "标记价格",
    "Position Value": "仓位价值",
    "Total P/L": "总盈亏",
    "MM P/L": "做市盈亏",
    "Arb P/L": "套利盈亏",
    "Auto P/L": "自动买卖盈亏",
    "Other P/L": "其他盈亏",
    "Price Move": "价格涨跌",
    "Overview": "总览",
    "MM": "做市",
    "Arbitrage": "套利",
    "Orders": "订单",
    "Auto": "自动",
    "Risk": "风控",
    "Market Maker": "做市",
    "Auto Buy/Sell": "自动买卖",
    "Readiness": "就绪检查",
    "Live Gate": "实盘开关",
    "Accounts": "账户",
    "Strategies": "策略",
    "Priority": "优先级",
    "Scope": "范围",
    "Action": "操作",
    "Detail": "详情",
    "Account": "账户",
    "Type": "类型",
    "Symbols": "交易对",
    "API": "API",
    "Balance": "余额",
    "Notes": "备注",
    "Strategy": "策略",
    "Configured": "已配置",
    "Symbol": "交易对",
    "Live": "实盘",
    "Reason": "原因",
    "Scans": "扫描",
    "Latency": "延迟",
    "Opportunity": "机会",
    "Notional": "名义金额",
    "Threshold": "阈值",
    "Updated": "更新",
    "On-chain": "链上",
    "Live Trading Console": "实盘交易控制台",
    "Cancel All": "全部撤单",
    "Mode": "模式",
    "Side": "方向",
    "Price": "价格",
    "Amount": "数量",
    "Filled": "已成交",
    "Remaining": "剩余",
    "Cost": "成本",
    "Source": "来源",
    "P/L": "盈亏",
    "Fee": "手续费",
    "Order": "订单",
    "Time": "时间",
    "Markets": "市场",
    "Asset": "币种",
    "Add Market": "添加市场",
    "Quote": "计价",
    "Cash & Carry Pairs": "期现配对",
    "Cash & Carry": "期现套利",
    "Spot Symbol": "现货交易对",
    "Contract Symbol": "合约交易对",
    "Add Pair": "添加配对",
    "Risk Controls": "风控设置",
    "Allow Live": "允许实盘",
    "Order Budget": "订单预算",
    "Max Order": "最大单笔",
    "Max Cycle": "最大单轮",
    "Max Exposure": "最大敞口",
    "Daily Loss": "日亏损上限",
    "MM Guardrails": "做市风控",
    "Max Orders/Cycle": "每轮最大订单",
    "Max Open": "最大挂单",
    "Max Cancels": "最大撤单",
    "Cancel Sec": "撤单秒数",
    "Market Data": "市场数据",
    "Min Book Depth": "最小盘口深度",
    "Max Slippage Bps": "最大滑点 bps",
    "Max Slippage bps": "最大滑点 bps",
    "Book Age Sec": "盘口有效秒数",
    "Max Gap Bps": "最大盘口断层 bps",
    "Max Jump Bps": "最大跳价 bps",
    "Derivatives": "衍生品",
    "Max Leverage": "最大杠杆",
    "Min Liq Buffer %": "最小强平缓冲 %",
    "Max Margin Used %": "最大保证金占用 %",
    "Apply": "应用",
    "Strategy Center": "策略中心",
    "ID": "ID",
    "Name": "名称",
    "Owner": "用户",
    "Owner Email": "用户邮箱",
    "API Account": "API 账户",
    "Exchange": "交易所",
    "Parameters JSON": "参数 JSON",
    "Risk JSON": "风控 JSON",
    "Save Strategy": "保存策略",
    "User API Accounts": "用户 API 账户",
    "Label": "标签",
    "Market": "市场",
    "Assets": "币种",
    "Env": "环境变量",
    "API Key Env": "API Key 环境变量",
    "Secret Env": "Secret 环境变量",
    "Password Env": "Password 环境变量",
    "Proxy Env": "代理环境变量",
    "IP": "IP",
    "IP Label": "IP 标签",
    "Save Account": "保存账户",
    "Funding Arbitrage": "资金费率套利",
    "Pair ID": "配对 ID",
    "Spot Exchange": "现货交易所",
    "Perp Exchange": "永续交易所",
    "Perp Symbol": "永续交易对",
    "Pred Funding bps": "预测资金费 bps",
    "Min Funding bps": "最小资金费 bps",
    "Min Basis bps": "最小基差 bps",
    "Take Profit bps": "止盈 bps",
    "Stop Loss bps": "止损 bps",
    "Max Margin %": "最大保证金 %",
    "Liq Buffer %": "强平缓冲 %",
    "Save Funding": "保存资金费策略",
    "Signal Bot": "信号机器人",
    "Custom Webhook": "自定义 Webhook",
    "Default Strategy": "默认策略",
    "Max Age Sec": "最大有效秒数",
    "Dedupe Sec": "去重秒数",
    "Save Signal Bot": "保存信号机器人",
    "Webhook": "Webhook",
    "Derivatives Risk": "衍生品风险",
    "Lev": "杠杆",
    "Mark": "标记",
    "Liq": "强平",
    "Liq Buffer": "强平缓冲",
    "Funding": "资金费",
    "Funding / Basis": "资金费 / 基差",
    "Pair": "配对",
    "Spot / Perp": "现货 / 永续",
    "Spot": "现货",
    "Perp": "永续",
    "Basis": "基差",
    "Paper": "模拟",
    "Contract Strategies": "合约策略",
    "Signal": "信号",
    "Paper Plan": "模拟计划",
    "Options Arbitrage": "期权套利",
    "Expiry / Strike": "到期 / 行权价",
    "Bid / Ask": "买价 / 卖价",
    "Mark / IV": "标记 / IV",
    "Depth / Spread": "深度 / 价差",
    "Vol / OI": "成交量 / 持仓量",
    "Greeks": "希腊值",
    "Combo": "组合",
    "Legs": "腿",
    "Call": "看涨",
    "Put": "看跌",
    "Parity Gap": "平价差",
    "Audit Trail": "审计记录",
    "Actor": "操作者",
    "Target": "对象",
    "Account Balances": "账户余额",
    "Currency": "币种",
    "Free": "可用",
    "Used": "占用",
    "Total": "总计",
    "Orders & Fills": "订单与成交",
    "Level": "级别",
    "Risk & Events": "风控与事件",
    "Slip": "滑点",
    "Bid": "买价",
    "Ask": "卖价",
    "Bid USD": "买价 USD",
    "Ask USD": "卖价 USD",
    "Bid Size": "买盘数量",
    "Ask Size": "卖盘数量",
    "Live Opportunities": "实时机会",
    "Pricing": "定价",
    "Price Mode": "价格模式",
    "Taker Top": "吃单最优价",
    "Maker Top": "挂单最优价",
    "Offset bps": "偏移 bps",
    "Target": "目标",
    "Unlimited": "不限总量",
    "Total Base": "总基础币",
    "Total Quote": "总计价金额",
    "Order Size": "订单大小",
    "Size Mode": "数量模式",
    "Configured": "已配置",
    "Top Level": "盘口一档",
    "Min Base/Order": "每单最小基础币",
    "Max Base/Order": "每单最大基础币",
    "Random": "随机",
    "Timing": "时间",
    "Unit": "单位",
    "Place Sec": "下单间隔秒",
    "Start Gate": "启动价格",
    "Stop Gate": "停止价格",
    "AutoBuy starts when best ask is at or below this price.": "AutoBuy 会在卖一价小于等于该价格时启动。",
    "AutoBuy stops before each execution when best ask is at or above this price.": "AutoBuy 每次执行前会检查，卖一价大于等于该价格时停止。",
    "AutoSell starts when best bid is at or above this price.": "AutoSell 会在买一价大于等于该价格时启动。",
    "AutoSell stops before each execution when best bid is at or below this price.": "AutoSell 每次执行前会检查，买一价小于等于该价格时停止。",
    "Create Task": "创建任务",
    "Clear Done": "清理完成任务",
    "Task": "任务",
    "Config": "配置",
    "Progress": "进度",
    "Open": "挂单",
    "Last": "上次",
    "Next": "下次",
    "Order Price": "订单价格",
    "Slice Amount": "单次数量",
    "Submitted": "已提交",
    "Interval": "间隔",
    "Cancel": "撤单",
    "Spot Grid": "现货网格",
    "Live Ready": "允许实盘",
    "Range": "区间",
    "Lower": "下限",
    "Upper": "上限",
    "Grids": "网格数",
    "Spacing": "间距",
    "Arithmetic": "等差",
    "Geometric": "等比",
    "Quote/Grid": "每格金额",
    "Stops": "止盈止损",
    "Take Profit": "止盈",
    "Stop Loss": "止损",
    "Auto Rebuild": "自动重建",
    "Max Position": "最大持仓",
    "Max Orders": "最大订单数",
    "Min Step bps": "最小间距 bps",
    "Cancel Retries": "撤单重试",
    "Post Only": "只挂单",
    "Distance": "距离",
    "DCA Bot": "DCA 机器人",
    "Trigger": "触发",
    "Trigger Price": "触发价",
    "Interval Sec": "间隔秒",
    "Quote/Order": "每单金额",
    "Multiplier": "倍数",
    "Exit": "退出",
    "Avg Cost": "平均成本",
    "Max Loss": "最大亏损",
    "TWAP / VWAP / POV": "TWAP / VWAP / POV",
    "Algo": "算法",
    "Duration Sec": "持续秒数",
    "Slices": "切片数",
    "POV Rate": "POV 比例",
    "Limits": "限制",
    "Min Slice Quote": "最小切片金额",
    "Max Slice Quote": "最大切片金额",
    "Start Price": "启动价",
    "Stop Price": "停止价",
    "When": "时间",
    "Backtest / Paper": "回测 / 模拟",
    "Portfolio": "组合",
    "Initial Cash": "初始现金",
    "Initial Base": "初始基础币",
    "Fee bps": "手续费 bps",
    "Slippage bps": "滑点 bps",
    "Synthetic Path": "模拟路径",
    "End Price": "结束价",
    "Steps": "步数",
    "Volatility bps": "波动 bps",
    "Trend bps": "趋势 bps",
    "Max Points": "最大点数",
    "Return": "收益",
    "Max DD": "最大回撤",
    "Fees": "手续费",
    "Fill Rate": "成交率",
    "Step": "步",
    "Equity": "权益",
    "Drawdown": "回撤",
    "Base": "基础币",
    "Cash": "现金",
    "Live MM": "实盘做市",
    "Ladder": "深度梯子",
    "Levels": "层数",
    "Band %": "区间 %",
    "Quote/Level": "每层金额",
    "Depth Shape": "深度形状",
    "Linear": "线性",
    "Flat": "平坦",
    "Min Quote": "最小金额",
    "Min Distance": "最小距离",
    "Execution": "执行",
    "Reprice Bps": "重定价 bps",
    "Refresh Sec": "刷新秒数",
    "Inventory": "库存",
    "Inventory Control": "库存控制",
    "Target Base": "目标基础币",
    "No-skew Band": "不偏移区间",
    "Max Deviation": "最大偏离",
    "Budget": "预算",
    "Fills": "成交",
    "Spread P/L": "价差盈亏",
    "Quote Rates": "汇率",
    "USD Rate": "USD 汇率",
    "Solana Top Holders": "Solana 前排持仓",
    "Rank": "排名",
    "Owner Wallet": "钱包地址",
    "Supply Share": "供应占比",
    "Since Online": "上线以来",
    "Last Change": "最近变化",
    "Changes": "变化",
    "Token Accts": "Token 账户",
    "Holder Change Log": "持仓变化日志",
    "Delta": "变化量",
    "Buy": "买入",
    "Sell": "卖出",
    "Enabled": "启用",
    "Future": "交割合约",
    "Swap": "永续",
    "TWAP/VWAP/POV": "TWAP/VWAP/POV",
    "Spot Arbitrage": "现货套利",
    "Manual": "手动",
    "Unattributed": "未归因",
    "All assets": "全部币种",
    "Pause": "暂停",
    "Resume": "恢复",
    "Stop": "停止",
    "Delete": "删除",
    "Remove": "删除",
    "Edit": "编辑",
    "Save": "保存",
    "Canceling": "撤单中",
    "No account balances yet.": "暂无账户余额。",
    "No non-zero target balances.": "暂无非零目标余额。",
    "No derivative accounts configured.": "暂无衍生品账户配置。",
    "No open positions.": "暂无持仓。",
    "No funding/basis pair configured.": "暂无资金费/基差配对。",
    "No contract strategy rows.": "暂无合约策略记录。",
    "No option chain rows.": "暂无期权链记录。",
    "No option combo configured.": "暂无期权组合配置。",
    "No Auto Buy/Sell tasks.": "暂无自动买卖任务。",
    "No active Auto Buy/Sell task. New tasks will use the default configuration above.": "暂无运行中的自动买卖任务。新任务会使用上方默认配置。",
    "Same as default": "与默认一致",
    "Current running task config matches the default form config": "当前运行任务配置与默认表单配置一致",
    "Default": "默认",
    "Different": "不同",
    "Same": "相同",
    "Field": "字段",
    "Running task": "运行任务",
    "Default form": "默认表单",
    "No open orders.": "暂无挂单。",
    "No recent fills.": "暂无最近成交。",
    "No readiness actions.": "暂无就绪检查动作。",
    "No audit events.": "暂无审计事件。",
    "No holder changes.": "暂无持仓变化。",
    "No opportunities.": "暂无机会。",
    "Pause or resume scans": "暂停或恢复扫描",
    "Current asset view": "当前币种视图",
    "ok": "正常",
    "warning": "警告",
    "error": "错误",
    "blocked": "阻断",
    "running": "运行中",
    "paused": "已暂停",
    "disabled": "已关闭",
    "dry_run": "模拟",
    "live": "实盘",
    "paper": "模拟",
    "placed": "已下单",
    "complete": "完成",
    "stopped": "已停止",
    "waiting_for_fill": "等待成交",
    "waiting_for_interval": "等待间隔",
    "waiting_for_start_price": "等待启动价",
    "blocked_by_risk": "风控阻断",
    "program_paused": "程序暂停",
    "strategy_paused": "策略暂停",
    "configured": "固定配置",
    "top_level": "盘口一档",
    "taker": "吃单",
    "maker": "挂单",
    "buy": "买入",
    "sell": "卖出",
  };

  const DICTS = { en: {}, zh: ZH };

  function initialLanguage() {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (SUPPORTED.has(stored)) return stored;
    const browser = String(navigator.language || "").toLowerCase();
    return browser.startsWith("zh") ? "zh" : "en";
  }

  let currentLanguage = initialLanguage();

  function translate(source, lang = currentLanguage) {
    const text = String(source ?? "");
    if (lang === "en") return text;
    return DICTS[lang]?.[text] || text;
  }

  function setTextNodeValue(node, translated, original) {
    const value = node.nodeValue || "";
    const leading = value.match(/^\s*/)?.[0] || "";
    const trailing = value.match(/\s*$/)?.[0] || "";
    const next = `${leading}${translated}${trailing}`;
    if (node.nodeValue !== next) node.nodeValue = next;
    node.__i18nSource = original;
    node.__i18nTranslated = translated;
  }

  function translateTextNode(node) {
    const value = node.nodeValue || "";
    const trimmed = value.trim();
    if (!trimmed) return;
    const source =
      node.__i18nSource && trimmed === node.__i18nTranslated
        ? node.__i18nSource
        : trimmed;
    const translated = translate(source);
    setTextNodeValue(node, translated, source);
  }

  function shouldSkipElement(element) {
    if (!element || element.nodeType !== Node.ELEMENT_NODE) return false;
    if (element.closest("[data-no-i18n]")) return true;
    return ["SCRIPT", "STYLE", "TEXTAREA", "CODE", "PRE"].includes(element.tagName);
  }

  function translateAttributes(root) {
    const elements = root.querySelectorAll
      ? root.querySelectorAll("[title], [aria-label], [placeholder]")
      : [];
    for (const element of elements) {
      if (shouldSkipElement(element)) continue;
      for (const attr of ["title", "aria-label", "placeholder"]) {
        if (!element.hasAttribute(attr)) continue;
        const value = element.getAttribute(attr) || "";
        element.__i18nAttrSources = element.__i18nAttrSources || {};
        element.__i18nAttrTranslated = element.__i18nAttrTranslated || {};
        const source =
          element.__i18nAttrSources[attr] &&
          value === element.__i18nAttrTranslated[attr]
            ? element.__i18nAttrSources[attr]
            : value;
        element.__i18nAttrSources[attr] = source;
        const translated = translate(source);
        element.__i18nAttrTranslated[attr] = translated;
        if (translated !== value) element.setAttribute(attr, translated);
      }
    }
  }

  function translateTree(root = document.body) {
    if (!root || shouldSkipElement(root)) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent || shouldSkipElement(parent)) {
          return NodeFilter.FILTER_REJECT;
        }
        return node.nodeValue && node.nodeValue.trim()
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) translateTextNode(node);
    translateAttributes(root);
  }

  function syncDocument() {
    document.documentElement.lang = currentLanguage === "zh" ? "zh-CN" : "en";
    document.title = translate("Crypto Trading Dashboard");
    const selector = document.getElementById("language-select");
    if (selector) selector.value = currentLanguage;
  }

  function applyLanguage(lang = currentLanguage) {
    currentLanguage = SUPPORTED.has(lang) ? lang : "en";
    localStorage.setItem(STORAGE_KEY, currentLanguage);
    syncDocument();
    translateTree(document.body);
    window.dispatchEvent(new CustomEvent("crypto-arb-language-change", {
      detail: { language: currentLanguage },
    }));
  }

  function setupLanguageSelector() {
    const selector = document.getElementById("language-select");
    if (!selector) return;
    selector.value = currentLanguage;
    selector.addEventListener("change", () => {
      applyLanguage(selector.value);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupLanguageSelector();
    applyLanguage(currentLanguage);
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === "childList") {
          for (const node of mutation.addedNodes) {
            if (node.nodeType === Node.TEXT_NODE) {
              translateTextNode(node);
            } else if (node.nodeType === Node.ELEMENT_NODE) {
              translateTree(node);
            }
          }
        } else if (mutation.type === "characterData") {
          translateTextNode(mutation.target);
        } else if (mutation.type === "attributes") {
          translateAttributes(mutation.target);
        }
      }
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["title", "aria-label", "placeholder"],
    });
  });

  window.CryptoArbI18n = {
    applyLanguage,
    get language() {
      return currentLanguage;
    },
    t: translate,
  };
})();
