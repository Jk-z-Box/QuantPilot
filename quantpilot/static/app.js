"use strict";
const $ = (id) => document.getElementById(id);
let csrf = "",
  currentState = null,
  marketData = null,
  strategyLoaded = false,
  refreshing = false,
  marketGeneration = 0;
let pendingOrder = null,
  aiSettingsLoaded = false;
try {
  pendingOrder = JSON.parse(
    sessionStorage.getItem("qp_pending_order") || "null",
  );
} catch {
  /* No persistent order. */
}
const fmt = (value, digits = 2) =>
  value === null || value === undefined
    ? "—"
    : Number(value).toLocaleString("en-US", {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      });
const date = (value) =>
  value
    ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
    : "—";
const errors = {
  authentication_required: "请先登录",
  invalid_credentials: "用户名或密码错误",
  login_rate_limited: "尝试次数过多，请 15 分钟后再试",
  exchange_unavailable: "交易所暂时不可访问，请稍后重试或切换交易所",
  inactive_or_non_spot_market: "该交易所不支持此现货交易对",
  stale_quote: "行情已过期，订单已拒绝",
  unverified_quote_timestamp: "交易所报价没有可验证时间戳，禁止成交",
  spread_too_wide: "买卖价差超过限制",
  insufficient_cash: "模拟资金不足",
  insufficient_position: "可卖出仓位不足",
  max_order_exceeded: "单笔金额超过风控上限",
  max_exposure_exceeded: "总敞口超过上限",
  max_position_exceeded: "单一仓位超过上限",
  daily_loss_limit: "已触及每日亏损限制",
  trade_loss_limit: "止损点对应的预计损失超过单仓风险预算",
  kill_switch_active: "开仓已暂停",
  amount_precision: "数量不符合该交易所的精度要求",
  below_min_cost: "订单金额低于交易所最小要求",
  below_min_amount: "数量低于交易所最小要求",
  ioc_limit_not_marketable: "限价无法立即成交，订单已取消",
  invalid_protection_prices: "买单需要满足：止损价 < 成交价 < 止盈价",
  order_expired_or_invalid_ttl: "订单已过期，请重新提交",
  cannot_loosen_stop: "加仓不能放宽已有止损价",
  invalid_request: "输入格式不正确，请检查数值与必填项",
  openai_disabled_or_model_missing: "AI 复核尚未启用或未设置模型",
  openai_key_missing: "尚未保存 OpenAI Key",
  advice_rate_limited: "AI 复核每分钟最多一次",
  live_trading_locked: "真实交易已锁定",
  account_read_failed: "账户查询失败，请检查只读密钥权限与网络",
  credentials_missing: "尚未保存此交易所的凭据",
};
const explain = (code) => errors[code] || code;
function notice(message) {
  $("global-error").textContent = message;
  $("global-error").hidden = !message;
}
async function api(path, method = "GET", body) {
  const headers = {};
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = csrf;
  }
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: method === "GET" ? undefined : JSON.stringify(body ?? {}),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务返回异常，请检查连接");
  }
  if (!response.ok) {
    if (response.status === 401) showLogin();
    const error = new Error(explain(data.detail || "请求失败"));
    error.status = response.status;
    throw error;
  }
  return data;
}
function showLogin() {
  csrf = "";
  $("login-screen").hidden = false;
  $("console").hidden = true;
}
function showConsole(username) {
  $("login-screen").hidden = true;
  $("console").hidden = false;
  $("avatar").textContent = username.slice(0, 1).toUpperCase();
}
function options(id, values, selected) {
  const target = $(id);
  target.replaceChildren();
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    target.append(option);
  }
  target.value = selected || values[0];
}
function cells(target, rows, columns, empty) {
  target.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr"),
      td = document.createElement("td");
    td.colSpan = columns;
    td.className = "empty";
    td.textContent = empty;
    tr.append(td);
    target.append(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const value of row) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    target.append(tr);
  }
}
function renderState(state) {
  const first = !currentState;
  currentState = state;
  if (first) {
    for (const id of ["exchange", "strategy-exchange"])
      options(id, state.exchanges, state.strategy.exchange);
    for (const id of ["symbol", "strategy-symbol"])
      options(id, state.symbols, state.strategy.symbol);
    options("provider", [...state.exchanges, "openai"]);
    $("initial-cash").textContent = fmt(state.initial_cash);
  }
  const account = state.account;
  $("equity").textContent = fmt(account.equity);
  $("cash").textContent = fmt(account.cash);
  $("day-pnl").textContent = fmt(account.day_pnl);
  $("day-pnl").className =
    Number(account.day_pnl) >= 0 ? "positive" : "negative";
  $("exposure").textContent = fmt(account.exposure);
  $("exposure-limit").textContent = fmt(state.limits.max_exposure);
  $("fees").textContent = fmt(account.fees);
  $("kill").textContent = state.kill_switch ? "恢复开仓" : "暂停开仓";
  $("kill-notice").hidden = !state.kill_switch;
  $("demo-notice").hidden = state.market_mode !== "demo";
  $("ai-advice").disabled = !state.openai_enabled;
  if (!aiSettingsLoaded) {
    $("ai-enabled").checked = state.openai_enabled;
    $("ai-model").value = state.openai_model;
    aiSettingsLoaded = true;
  }
  $("system-status").textContent = state.runner_error
    ? explain(state.runner_error)
    : account.valuation_valid
      ? "控制台已连接 · 实盘锁定"
      : "持仓估值不可用 · 暂停开仓";
  cells(
    $("positions"),
    Object.entries(account.positions).map(([key, p]) => [
      key,
      fmt(p.quantity, 8),
      fmt(p.cost),
      fmt(p.stop_price),
      fmt(p.take_price),
    ]),
    5,
    "暂无持仓。先观察市场，再验证你的第一笔模拟交易。",
  );
  if (!strategyLoaded) {
    for (const [id, key] of [
      ["strategy-exchange", "exchange"],
      ["strategy-symbol", "symbol"],
      ["strategy-timeframe", "timeframe"],
      ["strategy-budget", "budget"],
      ["strategy-stop", "stop_pct"],
      ["strategy-take", "take_pct"],
    ])
      $(id).value = state.strategy[key];
    $("strategy-enabled").checked = state.strategy.enabled;
    strategyLoaded = true;
  }
  const labels = {
    max_order: "单笔金额上限 / USDT",
    max_exposure: "总敞口上限 / USDT",
    max_position: "单一仓位上限 / USDT",
    daily_loss_limit: "每日亏损限额 / USDT",
    max_trade_loss: "单仓止损风险预算 / USDT",
    fee_bps: "模拟手续费 / bps",
    slippage_bps: "模拟滑点 / bps",
    max_quote_age: "行情最大年龄 / 秒",
  };
  $("risk-limits").replaceChildren();
  for (const [key, value] of Object.entries(state.limits)) {
    const row = document.createElement("div"),
      dt = document.createElement("dt"),
      dd = document.createElement("dd");
    dt.textContent = labels[key];
    dd.textContent = value;
    row.append(dt, dd);
    $("risk-limits").append(row);
  }
  $("connection-list").replaceChildren();
  for (const name of [...state.exchanges, "openai"]) {
    const row = document.createElement("div"),
      label = document.createElement("span"),
      controls = document.createElement("div"),
      status = document.createElement("small");
    row.className = "connection";
    label.textContent = name;
    status.textContent = state.credentials.includes(name)
      ? "凭据已保存"
      : "未设置";
    controls.append(status);
    if (state.credentials.includes(name)) {
      if (name !== "openai") {
        const read = document.createElement("button");
        read.className = "secondary";
        read.textContent = "查询余额";
        read.addEventListener("click", () =>
          task(async () => {
            $("balance-result").textContent = JSON.stringify(
              await api("/api/balance/" + name),
              null,
              2,
            );
          }),
        );
        controls.append(read);
      }
      const remove = document.createElement("button");
      remove.className = "danger";
      remove.textContent = "移除";
      remove.addEventListener("click", () =>
        task(async () => {
          await api("/api/credentials/" + name, "DELETE");
          await refresh();
        }),
      );
      controls.append(remove);
    }
    row.append(label, controls);
    $("connection-list").append(row);
  }
  $("updated").textContent =
    "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
}
function svgNode(name, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}
function chart(rows) {
  const svg = $("chart");
  svg.replaceChildren();
  const values = rows.map((r) => r[4]),
    min = Math.min(...values),
    max = Math.max(...values),
    range = max - min || max * 0.001,
    x = (i) => 12 + (i / (rows.length - 1)) * 730,
    y = (v) => 225 - ((v - min) / range) * 190;
  for (let i = 0; i < 4; i++) {
    const value = min + (range * i) / 3,
      yy = y(value);
    svg.append(
      svgNode("line", {
        x1: 10,
        x2: 750,
        y1: yy,
        y2: yy,
        stroke: "#e9eeeb",
        "stroke-dasharray": "4 5",
      }),
    );
    svg.append(
      svgNode(
        "text",
        { x: 762, y: yy + 4, fill: "#9ba8a3", "font-size": 10 },
        fmt(value, max < 10 ? 4 : 0),
      ),
    );
  }
  const line = values
    .map((v, i) => (i ? "L" : "M") + x(i) + "," + y(v))
    .join(" ");
  svg.append(
    svgNode("path", { d: line + " L742,242 L12,242 Z", fill: "#edf6f1" }),
  );
  svg.append(
    svgNode("path", {
      d: line,
      fill: "none",
      stroke: "#238b73",
      "stroke-width": 2.5,
      "stroke-linejoin": "round",
    }),
  );
  svg.append(
    svgNode("circle", {
      cx: x(rows.length - 1),
      cy: y(values.at(-1)),
      r: 4,
      fill: "#238b73",
      stroke: "white",
      "stroke-width": 2,
    }),
  );
  $("chart-start").textContent = date(rows[0][0] / 1000);
  $("chart-end").textContent = date(rows.at(-1)[0] / 1000);
}
async function refreshMarket() {
  const generation = ++marketGeneration;
  $("market-error").textContent = "";
  try {
    const query = new URLSearchParams({
      exchange: $("exchange").value,
      symbol: $("symbol").value,
      timeframe: $("timeframe").value,
    });
    const data = await api("/api/market?" + query);
    if (generation !== marketGeneration) return;
    marketData = data;
    $("price").textContent =
      fmt(data.quote.mid, Number(data.quote.mid) < 10 ? 4 : 2) + " USDT";
    $("quote-meta").textContent =
      (data.quote.source === "synthetic_demo"
        ? "合成演示 · "
        : "交易所盘口 · ") +
      (data.quote.timestamp_verified
        ? date(data.quote.timestamp)
        : "无交易所时间戳，仅展示");
    chart(data.candles);
    const signal = data.signal;
    $("signal").textContent = {
      buy: "观察买入机会",
      sell: "考虑退出多头",
      hold: "保持观望",
    }[signal.action];
    $("signal-reason").textContent = signal.reason;
    $("ema").textContent = fmt(signal.ema20) + " / " + fmt(signal.ema50);
    $("rsi").textContent = fmt(signal.rsi14, 1);
    $("atr").textContent = fmt(signal.atr14);
    $("volume").textContent = fmt(signal.volume_ratio) + "×";
    $("validity").textContent =
      "信号有效至 " + date(signal.valid_until) + "；下单时重新校验。";
    if (!$("order-limit").value) fillOrderPrices();
  } catch (error) {
    if (generation !== marketGeneration) return;
    marketData = null;
    $("market-error").textContent = error.message;
    $("price").textContent = "—";
    $("quote-meta").textContent = "行情不可用";
    $("signal").textContent = "数据不可用";
    $("chart").replaceChildren();
  }
}
function fillOrderPrices() {
  if (!marketData) return;
  const price = Number(marketData.quote.mid);
  $("order-limit").value = (
    price * ($("order-side").value === "buy" ? 1.005 : 0.995)
  ).toFixed(8);
  $("order-stop").value = (price * 0.98).toFixed(8);
  $("order-take").value = (price * 1.04).toFixed(8);
}
async function logs() {
  const [orders, audit] = await Promise.all([
    api("/api/orders"),
    api("/api/audit"),
  ]);
  cells(
    $("orders"),
    orders.map((o) => [
      date(o.timestamp),
      o.symbol || "—",
      o.side === "buy" ? "买入" : o.side === "sell" ? "卖出" : "—",
      o.quantity ? fmt(o.quantity, 8) : "—",
      o.price ? fmt(o.price) : "—",
      o.status === "filled" ? "已模拟成交" : explain(o.code),
    ]),
    6,
    "还没有订单记录。",
  );
  $("audit").replaceChildren();
  for (const event of audit) {
    const row = document.createElement("div"),
      at = document.createElement("time"),
      name = document.createElement("strong"),
      detail = document.createElement("code");
    row.className = "audit-row";
    at.textContent = date(event.created);
    name.textContent = event.event;
    detail.textContent = JSON.stringify(event.detail);
    row.append(at, name, detail);
    $("audit").append(row);
  }
}
async function refresh() {
  if (refreshing || !csrf) return;
  refreshing = true;
  try {
    renderState(await api("/api/state"));
    await Promise.all([refreshMarket(), logs()]);
  } finally {
    refreshing = false;
  }
}
async function task(fn) {
  try {
    notice("");
    await fn();
  } catch (error) {
    notice(error.message);
  }
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    const data = await api("/api/login", "POST", {
      username: $("username").value,
      password: $("password").value,
    });
    $("password").value = "";
    csrf = data.csrf;
    currentState = null;
    strategyLoaded = false;
    showConsole(data.username);
    await refresh();
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("logout").addEventListener("click", () =>
  task(async () => {
    await api("/api/logout", "POST");
    showLogin();
  }),
);
$("refresh").addEventListener("click", () => task(refresh));
$("kill").addEventListener("click", () =>
  task(async () => {
    await api("/api/kill", "POST", { enabled: !currentState.kill_switch });
    strategyLoaded = false;
    await refresh();
  }),
);
for (const id of ["exchange", "symbol", "timeframe"])
  $(id).addEventListener("change", () =>
    task(async () => {
      marketData = null;
      $("order-limit").value = "";
      await refreshMarket();
    }),
  );
$("order-side").addEventListener("change", fillOrderPrices);
$("order-form").addEventListener("submit", (event) => {
  event.preventDefault();
  task(async () => {
    const button = event.submitter;
    button.disabled = true;
    try {
      if (!pendingOrder) {
        pendingOrder = {
          id: crypto.randomUUID(),
          exchange: $("exchange").value,
          symbol: $("symbol").value,
          side: $("order-side").value,
          quantity: $("order-quantity").value,
          limit_price: $("order-limit").value,
          expires_at: Date.now() / 1000 + 30,
          mode: "paper",
        };
        if (pendingOrder.side === "buy") {
          pendingOrder.stop_price = $("order-stop").value;
          pendingOrder.take_price = $("order-take").value;
        }
        sessionStorage.setItem(
          "qp_pending_order",
          JSON.stringify(pendingOrder),
        );
      }
      const result = await api("/api/orders", "POST", pendingOrder);
      pendingOrder = null;
      sessionStorage.removeItem("qp_pending_order");
      $("order-result").textContent =
        result.status === "filled"
          ? "模拟成交：" + result.quantity + " @ " + fmt(result.price)
          : "拒绝：" + explain(result.code);
      await refresh();
    } catch (error) {
      if (error.status === 422) {
        pendingOrder = null;
        sessionStorage.removeItem("qp_pending_order");
        $("order-result").textContent =
          "请求格式不正确，未下单，请修正后提交。";
      } else {
        $("order-result").textContent =
          "结果未确认。再次提交将复用原订单编号，避免重复成交。";
      }
      throw error;
    } finally {
      button.disabled = false;
    }
  });
});
$("strategy-form").addEventListener("submit", (event) => {
  event.preventDefault();
  task(async () => {
    await api("/api/strategy", "POST", {
      enabled: $("strategy-enabled").checked,
      exchange: $("strategy-exchange").value,
      symbol: $("strategy-symbol").value,
      timeframe: $("strategy-timeframe").value,
      budget: $("strategy-budget").value,
      stop_pct: $("strategy-stop").value,
      take_pct: $("strategy-take").value,
    });
    $("strategy-result").textContent = "策略设置已保存";
    strategyLoaded = false;
    await refresh();
  });
});
$("credentials-form").addEventListener("submit", (event) => {
  event.preventDefault();
  task(async () => {
    await api("/api/credentials/" + $("provider").value, "POST", {
      api_key: $("api-key").value,
      secret: $("api-secret").value,
      passphrase: $("passphrase").value,
    });
    for (const id of ["api-key", "api-secret", "passphrase"]) $(id).value = "";
    $("credentials-result").textContent = "已加密保存";
    await refresh();
  });
});
$("ai-advice").addEventListener("click", () =>
  task(async () => {
    const button = $("ai-advice");
    button.disabled = true;
    $("advice").textContent = "正在复核…";
    try {
      const data = await api("/api/advice", "POST");
      $("advice").textContent =
        data.summary + "\n" + data.risks.map((r) => "• " + r).join("\n");
    } finally {
      button.disabled = !currentState.openai_enabled;
    }
  }),
);
const pages = {
  overview: ["总览", "交易总览", "看清市场，保持纪律。"],
  strategy: [
    "策略引擎",
    "让策略有序运行",
    "明确规则，限定风险，再交给自动化。",
  ],
  orders: [
    "订单与审计",
    "每次操作，都有记录",
    "跟踪模拟成交、拒单原因和管理操作。",
  ],
  connections: [
    "连接与设置",
    "连接你的交易工具",
    "密钥在服务器加密保存，账户接口只读。",
  ],
};
for (const button of document.querySelectorAll("[data-page]"))
  button.addEventListener("click", () => {
    const page = button.dataset.page;
    document
      .querySelectorAll("[data-panel]")
      .forEach((el) => (el.hidden = el.dataset.panel !== page));
    document
      .querySelectorAll("[data-page]")
      .forEach((el) => el.classList.toggle("active", el === button));
    $("page-name").textContent = pages[page][0];
    $("page-title").textContent = pages[page][1];
    $("page-subtitle").textContent = pages[page][2];
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
setInterval(() => {
  if (csrf) task(refresh);
}, 15000);
task(async () => {
  try {
    const data = await api("/api/session");
    csrf = data.csrf;
    showConsole(data.username);
    await refresh();
  } catch {
    showLogin();
  }
});

$("ai-settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  task(async () => {
    await api("/api/ai/settings", "POST", {
      enabled: $("ai-enabled").checked,
      model: $("ai-model").value,
    });
    $("ai-settings-result").textContent =
      "AI 设置已保存；仅在点击复核按钮后请求 API。";
    aiSettingsLoaded = false;
    await refresh();
  });
});
