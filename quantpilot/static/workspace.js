"use strict";
const $ = (id) => document.getElementById(id),
  esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const fmt = (v, d = 2) =>
  v === null || v === undefined || !Number.isFinite(Number(v))
    ? "—"
    : Number(v).toLocaleString("en-US", {
        maximumFractionDigits: d,
        minimumFractionDigits: d,
      });
const price = (v) => fmt(v, Number(v) < 10 ? 4 : 2),
  time = (v) =>
    v ? new Date(v * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
const exchangeName = { binance: "BINANCE", okx: "OKX", gate: "GATE" },
  kindName = { paper: "本地模拟", demo: "交易所测试", live: "真实账户" },
  directions = { long: "做多", short: "做空", wait: "等待", flat: "震荡" };
let state = null,
  csrf = "",
  selected = "",
  page = "market",
  rows = [],
  chartFrame = "1h",
  chartCount = 80,
  chartGeneration = 0,
  loading = false,
  chartBusy = false,
  timer = null,
  chartTimer = null;
const titles = {
  market: ["市场与机会", "让数据说清楚，什么时候值得行动。"],
  accounts: ["账户与仓位", "各自的资金，各自的授权，清楚的风险边界。"],
  history: ["交易记录", "追踪每次判断与执行，让结果有迹可循。"],
  settings: ["数据与 AI", "连接可靠的数据，把判断依据放在眼前。"],
};
async function api(path, body, method) {
  const opts = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.method = method || "POST";
    opts.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data;
  try {
    data = await r.json();
  } catch {
    throw Error("服务器响应异常，请稍后重试");
  }
  if (!r.ok) {
    if (r.status === 401 && path != "/api/login") {
      stop();
      $("app").hidden = true;
      $("login").hidden = false;
    }
    throw Error(
      typeof data.detail === "string"
        ? {
            authentication_required: "请重新登录",
            invalid_credentials: "用户名或密码不正确",
            login_rate_limited: "尝试过多，请稍后重试",
          }[data.detail] || data.detail
        : "请求未通过校验",
    );
  }
  return data;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  setTimeout(() => {
    $("toast").hidden = true;
  }, 4500);
}
function error(message) {
  $("errorBanner").textContent = message;
  $("errorBanner").hidden = !message;
}
async function action(fn) {
  try {
    await fn();
  } catch (e) {
    toast(e.message);
  }
}
function modal(title, html) {
  $("dialogTitle").textContent = title;
  $("dialogBody").innerHTML = html;
  $("dialog").showModal();
}
$("closeDialog").onclick = () => $("dialog").close();
$("dialog").addEventListener("click", (e) => {
  if (e.target === $("dialog")) {
    const r = $("dialog").getBoundingClientRect();
    if (
      e.clientX < r.left ||
      e.clientX > r.right ||
      e.clientY < r.top ||
      e.clientY > r.bottom
    )
      $("dialog").close();
  }
});
function accountPayload(a) {
  return Object.fromEntries(
    [
      "id",
      "name",
      "kind",
      "exchange",
      "capital",
      "leverage",
      "auto",
      "allowed",
      "sizing",
    ].map((k) => [k, a[k]]),
  );
}
function go(next) {
  page = next;
  document
    .querySelectorAll("[data-panel]")
    .forEach((x) => (x.hidden = x.dataset.panel !== page));
  document
    .querySelectorAll(".nav")
    .forEach((x) => x.classList.toggle("active", x.dataset.page === page));
  $("breadcrumb").textContent = titles[page][0];
  $("pageTitle").textContent = titles[page][0];
  $("pageDescription").textContent = titles[page][1];
  if (page === "history") loadAudit();
  if (page === "settings") fillSettings();
}
document
  .querySelectorAll("[data-page]")
  .forEach((x) => (x.onclick = () => go(x.dataset.page)));
document
  .querySelectorAll("[data-go]")
  .forEach((x) => (x.onclick = () => go(x.dataset.go)));
$("loginForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.currentTarget,
    button = f.querySelector("button");
  button.disabled = true;
  $("loginError").textContent = "";
  try {
    const result = await api("/api/login", {
      username: f.username.value,
      password: f.password.value,
    });
    csrf = result.csrf;
    f.password.value = "";
    await start();
  } catch (e) {
    $("loginError").textContent = e.message;
  } finally {
    button.disabled = false;
  }
};
$("logout").onclick = () =>
  action(async () => {
    await api("/api/logout", {});
    stop();
    csrf = "";
    state = null;
    $("app").hidden = true;
    $("login").hidden = false;
  });
function stop() {
  clearInterval(timer);
  clearInterval(chartTimer);
  timer = chartTimer = null;
}
async function start() {
  $("login").hidden = true;
  $("app").hidden = false;
  await refresh();
  stop();
  timer = setInterval(refresh, 5000);
  chartTimer = setInterval(() => {
    if (page === "market") loadChart();
  }, 5000);
  await loadChart();
}
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    state = await api("/api/v2/state");
    if (!state.watchlist.some((i) => i.id === selected)) {
      selected = state.watchlist[0]?.id || "";
      rows = [];
    }
    render();
    error("");
    $("connection").innerHTML =
      '<i class="dot"></i> 已连接 · ' +
      new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (e) {
    error(e.message);
    $("connection").textContent = "连接中断 · 暂停展示有效机会";
    if (state)
      Object.values(state.opportunities).forEach((o) => (o.valid = false));
  } finally {
    loading = false;
  }
}
function render() {
  if (!state) return;
  $("demoBanner").hidden = !state.demo;
  $("haltBanner").hidden = !state.preferences.halted;
  $("profile").value = state.preferences.profile;
  $("halt").textContent = state.preferences.halted
    ? "▷ 解除全局暂停"
    : "Ⅱ 暂停开仓";
  renderTiles();
  renderOpportunity();
  renderAccounts();
  renderOrders();
  $("aiStatus").textContent = state.ai_status;
}
function renderTiles() {
  $("watchTiles").innerHTML = state.watchlist
    .map((i) => {
      const q = state.quotes[i.id],
        o = state.opportunities[i.id],
        fresh = q && !q.error && state.now - q.timestamp <= 15;
      return `<button class="watch-tile ${i.id === selected ? "selected" : ""}" data-instrument="${esc(i.id)}" aria-pressed="${i.id === selected}"><span class="tile-head">${esc(i.label)}<small>${exchangeName[i.exchange]}</small></span><span class="tile-price">${fresh ? price(q.mid) : "—"}</span><span class="tile-foot"><span>${!i.enabled ? "已停用" : q?.error ? "连接不可用" : !fresh ? "等待新鲜行情" : o?.valid ? "● 有效机会" : "观察中"}</span><b class="${o?.valid ? "positive" : ""}">${o ? o.score + " 分" : "—"}</b></span></button>`;
    })
    .join("");
  document.querySelectorAll("[data-instrument]").forEach(
    (b) =>
      (b.onclick = () => {
        selected = b.dataset.instrument;
        rows = [];
        chartGeneration++;
        renderTiles();
        renderOpportunity();
        drawChart();
        loadChart();
      }),
  );
}
function renderOpportunity() {
  const i = state.watchlist.find((i) => i.id === selected);
  if (!i) return;
  const q = state.quotes[selected],
    o = state.opportunities[selected];
  $("chartTitle").textContent = i.label + " / USDT";
  $("chartVenue").textContent = exchangeName[i.exchange] + " · 永续合约";
  $("chartPrice").textContent = q && !q.error ? price(q.mid) : "—";
  $("chartMeta").textContent =
    q && !q.error && Date.now() / 1000 - q.timestamp > 15
      ? "报价已过期，暂停开仓"
      : q?.error
        ? "行情连接暂不可用"
        : q
          ? `资金费率 ${q.funding_rate === null ? "—" : fmt(q.funding_rate * 100, 4) + "%"} · ${state.demo ? "合成演示" : time(q.timestamp)}`
          : "等待交易所报价";
  $("score").textContent = o?.score ?? "—";
  $("scoreBar").style.width = (o?.score || 0) + "%";
  $("signalBadge").className = "badge " + (o?.valid ? "good" : "");
  $("signalBadge").textContent = o?.valid
    ? "有效 · " + directions[o.direction]
    : "等待确认";
  $("scoreLabel").textContent = o?.valid ? "满足入场条件" : "80 分起生效";
  $("signalSummary").textContent = o
    ? `${directions[o.direction] || "等待"}方向 · ${o.valid ? "评分及风控检查通过，账户仍会单独检查资金和授权。" : "当前仅供观察，不会新增仓位。"}`
    : "等待多周期数据；新上市合约可能缺少足够周线。";
  const p = o?.plan;
  $("tradePlan").innerHTML = p
    ? `<div class="plan-row"><span>开仓区间</span><strong>${price(p.entry_low)} — ${price(p.entry_high)}</strong></div><div class="plan-row stop"><span>保护止损</span><strong>${price(p.stop)}</strong></div>${p.targets.map((t, n) => `<div class="plan-row target"><span>止盈 ${n + 1} · 平仓 ${fmt(t.ratio * 100, 0)}%</span><strong>${price(t.price)}</strong></div>`).join("")}<div class="plan-row"><span>收益 / 风险 · 含预估费用</span><strong>${fmt(p.rr)} : 1</strong></div><div class="plan-row"><span>加仓观察 / 减仓观察</span><strong>${price(p.add_at)} / ${price(p.reduce_at)}</strong></div>`
    : '<div class="empty">行情准备好后显示交易计划</div>';
  $("signalReasons").innerHTML = (
    o ? [...o.blockers, ...o.reasons] : [q?.error || "正在收集四个周期的行情"]
  )
    .map((x) => `<div>${esc(x)}</div>`)
    .join("");
  if (o?.ai)
    $("signalReasons").innerHTML += `<div>AI：${esc(o.ai.reason)}</div>`;
  $("expiry").textContent = o
    ? `${Math.max(0, Math.floor(o.expires_at - Date.now() / 1000))} 秒后过期 · 持续复核`
    : "等待评估";
  $("coverage").textContent = "数据完整度 " + (o?.coverage ?? 0) + "%";
  $("scoreDetails").innerHTML = o?.breakdown?.length
    ? o.breakdown
        .map(
          (b) =>
            `<div class="factor-row"><div><span>${esc(b.name)}</span><b>${b.available ? b.points + " / " + b.max : "缺失"}</b></div><div class="factor-track"><span style="width:${(b.points / b.max) * 100}%"></span></div></div>`,
        )
        .join("")
    : '<div class="empty">等待可验证的数据</div>';
  $("trendRow").innerHTML = ["1w", "1d", "4h", "1h"]
    .map((f, n) => {
      const s = o?.indicators?.[f];
      return `<div class="trend-cell">${["周线", "日线", "4 小时", "1 小时"][n]}<strong class="${s?.trend === "long" ? "positive" : s?.trend === "short" ? "negative" : ""}">${s ? directions[s.trend] : "数据不足"}</strong><small>RSI ${s ? fmt(s.rsi, 1) : "—"} · MACD ${s ? fmt(s.macd_hist, 2) : "—"}</small></div>`;
    })
    .join("");
  const names = {
    etf: "ETF 实际披露资金流",
    options: "期权 / 交割 / 最大痛点",
    liquidation: "清算量与热力图",
    macro: "宏观 / 财报 / 突发新闻",
  };
  const expanded = new Set(
    [...$("evidenceList").querySelectorAll("details[open]")].map(
      (x) => x.dataset.evidence,
    ),
  );
  $("evidenceList").innerHTML = (
    o?.evidence ||
    Object.keys(names).map((c) => ({
      category: c,
      status: "missing",
      note: "等待数据源",
    }))
  )
    .map(
      (e) =>
        `<div class="evidence-row"><div><b>${names[e.category]}</b><span class="badge ${e.status === "fresh" ? "good" : e.status === "stale" ? "bad" : ""}">${{ fresh: "数据有效", stale: "已过期", missing: "暂缺数据" }[e.status] || "未验证"}</span></div><p>${esc(e.note || "")} ${e.value !== undefined ? "· " + fmt(e.value, 0) : ""}</p>${e.source ? `<p>${esc(e.source)} ${e.published_at ? "· " + time(e.published_at) : ""}</p>` : ""}${
          e.details
            ? `<details data-evidence="${esc(e.category + "|" + e.source)}" ${expanded.has(e.category + "|" + e.source) ? "open" : ""}><summary>查看数据与事件</summary>${e.details.max_pain ? `<p>最近到期 ${time(e.details.expiry)}<br>最大痛点估算 ${price(e.details.max_pain)}<br>看涨 OI ${fmt(e.details.call_oi, 0)} / 看跌 OI ${fmt(e.details.put_oi, 0)}</p>` : ""}${(e.details.events || []).map((x) => `<p>${esc(x.title)} · ${time(x.event_at)}</p>`).join("")}${(
                e.details.news || []
              )
                .slice(0, 4)
                .map((x) => `<p>${esc(x.title)} · ${time(x.published_at)}</p>`)
                .join(
                  "",
                )}${e.details.long_liquidation_usd !== undefined ? `<p>多单清算 ${fmt(e.details.long_liquidation_usd, 0)} / 空单清算 ${fmt(e.details.short_liquidation_usd, 0)} USD</p>` : ""}${renderHeatmap(e.details.heatmap)}</details>`
            : ""
        }${e.url?.startsWith("https://") ? `<a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">查看原始来源 ↗</a>` : ""}</div>`,
    )
    .join("");
}
function renderHeatmap(h) {
  if (
    !h ||
    !Array.isArray(h.cells) ||
    !h.prices?.length ||
    !h.timestamps?.length
  )
    return "";
  const cells = h.cells.filter(
    (c) =>
      c.length >= 3 &&
      c.every(Number.isFinite) &&
      c[0] >= 0 &&
      c[0] < h.timestamps.length &&
      c[1] >= 0 &&
      c[1] < h.prices.length &&
      c[2] > 0,
  );
  if (!cells.length) return "<p>暂无可展示的热力图数据</p>";
  const max = Math.max(...cells.map((c) => c[2])),
    w = 600 / h.timestamps.length,
    ht = 160 / h.prices.length;
  const boxes = cells
    .map(
      (c) =>
        `<rect x="${c[0] * w}" y="${160 - (c[1] + 1) * ht}" width="${Math.max(w, 1)}" height="${Math.max(ht, 1)}" fill="hsl(${165 - (125 * Math.log1p(c[2])) / Math.log1p(max)} 65% 55%)"><title>${esc(time(h.timestamps[c[0]]))} · ${esc(price(h.prices[c[1]]))} · ${esc(fmt(c[2], 0))}</title></rect>`,
    )
    .join("");
  return `<div class="liquidation-map"><p>清算热力图 · 模型估算 / 最近最多 2,000 个色块</p><svg viewBox="0 0 600 160" role="img" aria-label="清算风险热力图，横轴时间，纵轴价格"><rect width="600" height="160" fill="#143e38"/>${boxes}</svg><p>${esc(time(h.timestamps[0]))} → ${esc(time(h.timestamps.at(-1)))} · 价格 ${price(h.prices[0])}—${price(h.prices.at(-1))}</p></div>`;
}
async function loadChart() {
  if (!selected || chartBusy || !state) return;
  const generation = chartGeneration,
    id = selected,
    frame = $("timeframe").value;
  chartBusy = true;
  try {
    const data = await api(
      `/api/v2/chart/${encodeURIComponent(id)}?frame=${encodeURIComponent(frame)}`,
    );
    if (
      generation !== chartGeneration ||
      id !== selected ||
      frame !== $("timeframe").value
    )
      return;
    rows = data.candles;
    chartFrame = frame;
    $("chartEmpty").hidden = true;
    $("chartFresh").textContent =
      (data.demo ? "演示 K 线" : "交易所 K 线") +
      " · " +
      new Date(data.fetched_at * 1000).toLocaleTimeString("zh-CN");
    drawChart();
  } catch (e) {
    if (id === selected) {
      rows = [];
      drawChart();
      $("chartEmpty").textContent = e.message;
      $("chartEmpty").hidden = false;
      $("chartFresh").textContent = "数据暂不可用";
    }
  } finally {
    chartBusy = false;
  }
}
$("timeframe").onchange = () => {
  chartGeneration++;
  rows = [];
  drawChart();
  loadChart();
};
$("chartReset").onclick = () => {
  chartCount = 80;
  drawChart();
};
$("candles").ondblclick = $("chartReset").onclick;
$("candles").addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    chartCount = Math.max(
      20,
      Math.min(180, chartCount + (e.deltaY > 0 ? 10 : -10)),
    );
    drawChart();
  },
  { passive: false },
);
function drawChart() {
  const svg = $("candles");
  if (!rows.length) {
    svg.innerHTML = "";
    $("chartEmpty").hidden = false;
    return;
  }
  $("chartEmpty").hidden = true;
  const data = rows.slice(-chartCount),
    lo = Math.min(...data.map((r) => r[3])),
    hi = Math.max(...data.map((r) => r[2])),
    pad = (hi - lo) * 0.12 || 1,
    min = lo - pad,
    max = hi + pad,
    dx = 865 / data.length,
    y = (p) => 20 + ((max - p) / (max - min)) * 260,
    vol = Math.max(...data.map((r) => r[5]), 1);
  let out = "";
  for (let n = 0; n < 5; n++) {
    let yp = 20 + n * 65;
    out += `<line x1="0" y1="${yp}" x2="875" y2="${yp}" stroke="#eef2ee"/><text x="887" y="${yp + 4}" fill="#99a99d" font-size="10">${price(max - ((max - min) * n) / 4)}</text>`;
  }
  data.forEach((r, n) => {
    const x = n * dx + dx / 2,
      c = r[4] >= r[1] ? "#389e80" : "#cf8274";
    out += `<line x1="${x}" y1="${y(r[2])}" x2="${x}" y2="${y(r[3])}" stroke="${c}" stroke-width="1"/><rect x="${x - dx * 0.29}" y="${Math.min(y(r[1]), y(r[4]))}" width="${dx * 0.58}" height="${Math.max(1, Math.abs(y(r[1]) - y(r[4])))}" fill="${c}" rx=".5"/><rect x="${x - dx * 0.29}" y="${345 - (r[5] / vol) * 45}" width="${dx * 0.58}" height="${(r[5] / vol) * 45}" fill="${c}" opacity=".22"/>`;
    if (n % Math.max(1, Math.floor(data.length / 5)) === 0)
      out += `<text x="${Math.max(1, x - 20)}" y="369" fill="#9aab9d" font-size="10">${esc(new Date(r[0]).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" }))}</text>`;
  });
  const last = data.at(-1)[4];
  out += `<line x1="0" y1="${y(last)}" x2="875" y2="${y(last)}" stroke="#65ab89" stroke-dasharray="3 4" opacity=".7"/><line id="crosshair" x1="0" y1="15" x2="0" y2="347" stroke="#abbbae" stroke-dasharray="3 3" opacity="0"/>`;
  svg.innerHTML = out;
}
$("candles").onmousemove = (e) => {
  if (!rows.length) return;
  const rect = $("candles").getBoundingClientRect(),
    vx = ((e.clientX - rect.left) / rect.width) * 960,
    data = rows.slice(-chartCount),
    n = Math.max(
      0,
      Math.min(data.length - 1, Math.floor((vx / 865) * data.length)),
    ),
    r = data[n];
  $("chartTooltip").hidden = false;
  $("chartTooltip").textContent =
    `${new Date(r[0]).toLocaleString("zh-CN", { hour12: false })} · 开 ${price(r[1])} 高 ${price(r[2])} 低 ${price(r[3])} 收 ${price(r[4])} 量 ${fmt(r[5], 0)}`;
  const line = $("crosshair");
  if (line) {
    line.setAttribute("x1", (n * 865) / data.length + 432.5 / data.length);
    line.setAttribute("x2", (n * 865) / data.length + 432.5 / data.length);
    line.setAttribute("opacity", "1");
  }
};
$("candles").onmouseleave = () => {
  $("chartTooltip").hidden = true;
  $("crosshair")?.setAttribute("opacity", "0");
};
$("profile").onchange = () =>
  action(async () => {
    await api(
      "/api/v2/preferences",
      { ...state.preferences, profile: $("profile").value },
      "PUT",
    );
    toast("风险偏好已更新，正在重新评估");
    await refresh();
  });
$("halt").onclick = () =>
  action(async () => {
    await api(
      "/api/v2/preferences",
      { ...state.preferences, halted: !state.preferences.halted },
      "PUT",
    );
    toast("已更新。账户自动交易需要单独启用");
    await refresh();
  });
$("reviewAI").onclick = () =>
  action(async () => {
    const b = $("reviewAI");
    b.disabled = true;
    try {
      await api("/api/v2/ai/review", {});
      toast("AI 复核完成");
      await refresh();
    } finally {
      b.disabled = false;
    }
  });
function renderAccounts() {
  const accounts = state.accounts;
  $("accountCards").innerHTML = accounts
    .map((a) => {
      const s = a.summary || {};
      return `<article class="card account-card"><div class="card-head"><div><p class="eyebrow">${exchangeName[a.exchange]} · ${kindName[a.kind]}</p><h2>${esc(a.name)}</h2></div><span class="badge ${a.auto ? "good" : a.kind === "live" ? "live" : ""}">${a.auto ? "自动交易运行中" : "已暂停"}</span></div><div class="account-numbers"><div><small>${a.kind === "paper" ? "模拟权益" : "账户权益"} / USDT</small><strong>${fmt(s.equity)}</strong></div><div><small>可用资金</small><strong>${fmt(s.available)}</strong></div><div><small>浮动盈亏</small><strong class="${Number(s.unrealized) >= 0 ? "positive" : "negative"}">${fmt(s.unrealized)}</strong></div></div><p class="account-state">${a.kind === "paper" ? "可评估全部监测品种；模拟 " + exchangeName[a.exchange] + " 账户" : "授权交易对：" + (a.allowed.map((id) => state.watchlist.find((i) => i.id === id)?.label || id).join("、") || "未选择")}<br>${esc(a.execution.reason)}${a.last_error ? '<br><span class="negative">' + esc(a.last_error) + "</span>" : ""}</p><div class="account-actions"><button class="${a.auto ? "secondary" : "primary"}" data-toggle-account="${a.id}">${a.auto ? "暂停自动交易" : "启用自动交易"}</button><button class="secondary" data-edit-account="${a.id}">账户设置</button>${a.kind === "paper" ? `<button class="text-button" data-reset-account="${a.id}">重置模拟资金</button>` : `<button class="secondary" data-account-key="${a.id}">API 密钥</button><button class="text-button" data-check-account="${a.id}">读取与核对</button>`}</div></article>`;
    })
    .join("");
  $("positions").innerHTML =
    accounts
      .flatMap((a) =>
        (a.summary?.positions || []).map((p) => {
          const item = state.watchlist.find((i) => i.id === p.instrument_id);
          return `<tr><td>${esc(a.name)}<small>${esc(item?.symbol || p.symbol)}</small></td><td>${directions[p.side] || esc(p.side)}<small>${fmt(p.qty ?? p.contracts, 6)}</small></td><td>${price(p.entry ?? p.entryPrice)}</td><td class="${Number(p.unrealized ?? p.unrealizedPnl) >= 0 ? "positive" : "negative"}">${fmt(p.unrealized ?? p.unrealizedPnl)}${p.price_verified === false ? "<small>行情待验证</small>" : ""}</td><td>${price(p.stop)}<small>强平 ${price(p.liquidation ?? p.liquidationPrice)}</small></td><td>${a.kind === "paper" ? `<button class="text-button" data-close-position="${a.id}" data-position="${esc(p.instrument_id)}">减仓 / 平仓</button>` : "交易所原生保护"}</td></tr>`;
        }),
      )
      .join("") ||
    '<tr><td colspan="6" class="empty">还没有仓位。启用模拟账户后，系统会等待有效机会。</td></tr>';
  bindAccounts();
}
function bindAccounts() {
  document.querySelectorAll("[data-toggle-account]").forEach(
    (b) =>
      (b.onclick = () =>
        action(async () => {
          const a = state.accounts.find(
            (x) => x.id === b.dataset.toggleAccount,
          );
          if (!a.auto && a.kind !== "paper") {
            modal(
              "确认启用交易所自动交易",
              `<p class="muted">账户：${esc(a.name)} · ${kindName[a.kind]}。仅执行已授权的交易对，仍需服务端验收通过。</p><div class="dialog-footer"><button id="confirmAuto" class="primary">确认启用此账户</button></div>`,
            );
            $("confirmAuto").onclick = () =>
              action(async () => {
                await api("/api/v2/accounts", {
                  ...accountPayload(a),
                  auto: true,
                });
                $("dialog").close();
                await refresh();
              });
          } else {
            await api("/api/v2/accounts", {
              ...accountPayload(a),
              auto: !a.auto,
            });
            await refresh();
          }
        })),
  );
  document
    .querySelectorAll("[data-edit-account]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          editAccount(
            state.accounts.find((x) => x.id === b.dataset.editAccount),
          )),
    );
  document
    .querySelectorAll("[data-account-key]")
    .forEach((b) => (b.onclick = () => accountKey(b.dataset.accountKey)));
  document.querySelectorAll("[data-check-account]").forEach(
    (b) =>
      (b.onclick = () =>
        action(async () => {
          b.disabled = true;
          try {
            await api(`/api/v2/accounts/${b.dataset.checkAccount}/check`, {});
            toast("账户读取与订单核对完成");
            await refresh();
          } finally {
            b.disabled = false;
          }
        })),
  );
  document
    .querySelectorAll("[data-reset-account]")
    .forEach((b) => (b.onclick = () => resetAccount(b.dataset.resetAccount)));
  document.querySelectorAll("[data-close-position]").forEach(
    (b) =>
      (b.onclick = () => {
        modal(
          "减仓 / 平仓",
          `<p class="muted">仅操作此模拟仓位，按最新可验证报价模拟成交。</p><form id="closePosition"><label>平仓比例<select name="ratio"><option value="0.5">50% · 减半</option><option value="1">100% · 全部平仓</option></select></label><button class="primary">确认执行</button></form>`,
        );
        $("closePosition").onsubmit = (e) => {
          e.preventDefault();
          action(async () => {
            await api(`/api/v2/accounts/${b.dataset.closePosition}/close`, {
              instrument_id: b.dataset.position,
              ratio: Number(e.currentTarget.ratio.value),
            });
            $("dialog").close();
            await refresh();
          });
        };
      }),
  );
}
$("addAccount").onclick = () => editAccount();
function editAccount(a) {
  modal(
    a ? "账户与仓位设置" : "添加一个账户",
    `<form id="accountForm"><div class="form-grid"><label>账户名称<input name="name" maxlength="50" required value="${esc(a?.name || "")}"></label><label>账户类型<select name="kind" ${a ? "disabled" : ""}><option value="paper">本地模拟 · 不需要密钥</option><option value="demo">交易所测试 · 需要测试密钥</option><option value="live">真实账户 · 默认锁定执行</option></select></label><label>交易所 / 模拟成此交易所<select name="exchange" ${a ? "disabled" : ""}>${Object.keys(
      exchangeName,
    )
      .map((x) => `<option value="${x}">${exchangeName[x]}</option>`)
      .join(
        "",
      )}</select></label><label>初始模拟资金 / USDT<input name="capital" type="number" min="100" max="100000000" value="${a?.capital || 10000}" ${a ? "disabled" : ""}></label><label>最大杠杆<select name="leverage"><option value="1">1 倍</option><option value="2">2 倍</option><option value="3">3 倍</option></select></label></div><div class="notice-box">仓位金额指名义价值，不是保证金。固定模式严格按设定值开仓，风险额度不足时拒绝；最大模式由系统评估额度，含后续加仓。加仓比例基于初始仓位。留空由系统评估；填 0 禁止加仓。</div><h3>交易对授权与仓位</h3><div id="sizingRows"></div><div class="dialog-footer"><button class="primary">保存账户设置</button></div></form>`,
  );
  const f = $("accountForm");
  f.kind.value = a?.kind || "paper";
  f.exchange.value = a?.exchange || "binance";
  f.leverage.value = a?.leverage || 1;
  function populate() {
    const items = state.watchlist.filter(
      (i) => f.kind.value === "paper" || i.exchange === f.exchange.value,
    );
    $("sizingRows").innerHTML =
      items
        .map((i) => {
          const s = a?.sizing.find((s) => s.instrument_id === i.id);
          return `<div class="sizing-row" data-size-id="${i.id}"><label class="check"><input type="checkbox" class="allowed" ${f.kind.value === "paper" || a?.allowed.includes(i.id) ? "checked" : ""}>${esc(i.label)} · ${exchangeName[i.exchange]} <span class="muted small">${f.kind.value === "paper" ? "自定义此品种仓位" : "授权此品种自动交易"}</span></label><div class="sizing-grid"><label>仓位模式<select class="size-mode"><option value="maximum" ${s?.mode === "maximum" ? "selected" : ""}>最大名义仓位</option><option value="fixed" ${s?.mode === "fixed" ? "selected" : ""}>固定开仓金额</option></select></label><label>金额 / USDT<input class="size-amount" type="number" min="5" max="1000000" step="any" value="${s?.notional || 100}"></label><label>最多加仓次数<input class="size-adds" type="number" min="0" max="5" value="${s?.add_count ?? ""}" placeholder="自动评估"></label><label>每次比例，逗号分隔<input class="size-ratios" value="${esc(s?.add_ratios.join(",") || "")}" placeholder="例如 0.25,0.20"></label></div></div>`;
        })
        .join("") ||
      '<p class="muted">此交易所尚无监测品种，请先到监测列表添加。</p>';
    document.querySelectorAll(".sizing-row").forEach((r) => {
      const update = () =>
        r
          .querySelectorAll(".sizing-grid input,.sizing-grid select")
          .forEach((x) => (x.disabled = !r.querySelector(".allowed").checked));
      r.querySelector(".allowed").onchange = update;
      update();
    });
  }
  f.kind.onchange = populate;
  f.exchange.onchange = populate;
  populate();
  f.onsubmit = (e) => {
    e.preventDefault();
    action(async () => {
      const allowed = [],
        sizing = [];
      document.querySelectorAll(".sizing-row").forEach((r) => {
        if (!r.querySelector(".allowed").checked) return;
        allowed.push(r.dataset.sizeId);
        const count = r.querySelector(".size-adds").value,
          ratios = r.querySelector(".size-ratios").value.trim();
        sizing.push({
          instrument_id: r.dataset.sizeId,
          mode: r.querySelector(".size-mode").value,
          notional: Number(r.querySelector(".size-amount").value),
          add_count: count === "" ? null : Number(count),
          add_ratios: ratios ? ratios.split(/[,，]/).map(Number) : [],
        });
      });
      await api("/api/v2/accounts", {
        id: a?.id || "",
        name: f.elements.namedItem("name").value,
        kind: f.kind.value,
        exchange: f.exchange.value,
        capital: Number(f.capital.value),
        leverage: Number(f.leverage.value),
        auto: a?.auto || false,
        allowed: f.kind.value === "paper" ? [] : allowed,
        sizing,
      });
      $("dialog").close();
      toast("账户设置已保存");
      await refresh();
    });
  };
}
function accountKey(id) {
  modal(
    "保存账户 API 密钥",
    `<p class="muted">密钥只在服务器加密保存，不回显。交易所测试账户必须使用测试环境密钥。</p><form id="accountKeyForm"><label>API Key<input name="api_key" type="password" autocomplete="off" required></label><label>API Secret<input name="secret" type="password" autocomplete="off" required></label><label>Passphrase（OKX 必填）<input name="passphrase" type="password" autocomplete="off"></label><button class="primary">加密保存</button></form>`,
  );
  $("accountKeyForm").onsubmit = (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    action(async () => {
      await api(`/api/v2/accounts/${id}/credentials`, {
        api_key: f.api_key.value,
        secret: f.secret.value,
        passphrase: f.passphrase.value,
      });
      f.reset();
      $("dialog").close();
      toast("密钥已保存；点击读取与核对检查连接");
      await refresh();
    });
  };
}
function resetAccount(id) {
  modal(
    "重置模拟账户",
    `<div class="banner warning">将清空此模拟账户的持仓和资金结果，自动交易会暂停，历史操作记录保留。</div><form id="resetForm"><label>新的虚拟资金 / USDT<input name="capital" type="number" min="100" max="100000000" value="10000" required></label><label>输入 RESET 确认<input name="confirmation" autocomplete="off" pattern="RESET" required placeholder="RESET"></label><button class="primary">重置此模拟账户</button></form>`,
  );
  $("resetForm").onsubmit = (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    action(async () => {
      await api(`/api/v2/accounts/${id}/reset`, {
        capital: Number(f.capital.value),
        confirmation: f.confirmation.value,
      });
      $("dialog").close();
      await refresh();
    });
  };
}
function renderOrders() {
  const names = Object.fromEntries(state.accounts.map((a) => [a.id, a.name]));
  $("orderRows").innerHTML =
    state.orders
      .map(
        (o) =>
          `<tr><td>${time(o.created)}</td><td>${esc(names[o.account_id] || o.account_id)}</td><td>${{ open: "开仓", add: "加仓", close: "平仓" }[o.action] || esc(o.action)}</td><td>${esc(state.watchlist.find((i) => i.id === o.result.instrument_id)?.label || o.result.symbol || "—")}</td><td>${fmt(o.result.qty ?? o.result.filled, 6)}<small>${price(o.result.price)}</small></td><td><span class="badge ${o.state === "filled" || o.state === "protected" ? "good" : "bad"}">${{ filled: "已成交", protected: "保护单已提交", unfilled: "未成交", sending: "发送中", unknown: "待人工核对" }[o.state] || esc(o.state)}</span></td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="6" class="empty">还没有交易。系统仅在机会有效且账户已启用时执行。</td></tr>';
}
async function loadAudit() {
  try {
    const rows = await api("/api/audit");
    $("auditRows").innerHTML =
      rows
        .slice(0, 30)
        .map(
          (r) =>
            `<div class="audit-row"><time>${time(r.created)}</time><span>${esc({ v2_account_saved: "账户设置已保存", v2_preferences_saved: "风险偏好或运行状态已更新", v2_account_reset: "模拟账户已重置", v2_watchlist_saved: "监测列表已更新", v2_startup: "服务启动，所有账户自动交易暂停", v2_execution_paused: "交易所执行已暂停", v2_open: "模拟开仓", v2_close: "模拟平仓", v2_add: "模拟加仓", v2_ai_review: "AI 复核完成", login: "登录成功", logout: "退出登录" }[r.event] || r.event)}</span></div>`,
        )
        .join("") || '<p class="empty">暂无操作记录</p>';
  } catch (e) {
    toast(e.message);
  }
}
$("refreshAudit").onclick = loadAudit;
$("editWatch").onclick = () => {
  let draft = structuredClone(state.watchlist);
  modal(
    "管理监测列表",
    `<p class="muted">输入交易所的统一合约名称，例如 BTC/USDT:USDT。可以编辑名称、交易所或添加其他品种。</p><div id="watchEditor"></div><button id="appendWatch" class="text-button">＋ 添加一个交易对</button><details style="margin-top:15px"><summary>查找交易所支持的永续合约</summary><div class="form-grid"><label>交易所<select id="searchExchange">${Object.keys(
      exchangeName,
    )
      .map((x) => `<option value="${x}">${exchangeName[x]}</option>`)
      .join(
        "",
      )}</select></label><label>关键字<input id="searchQuery" placeholder="BTC / AVGO / SOXL"></label></div><button id="searchMarkets" class="secondary">查询合约目录</button><div id="marketSearchResults" class="small muted"></div></details><div class="dialog-footer"><button id="saveWatch" class="primary">保存监测列表</button></div>`,
  );
  function read() {
    document.querySelectorAll("[data-watch-index]").forEach((r) => {
      let d = draft[Number(r.dataset.watchIndex)];
      d.label = r.querySelector(".watch-label").value;
      d.exchange = r.querySelector(".watch-exchange").value;
      d.symbol = r.querySelector(".watch-symbol").value.trim().toUpperCase();
      d.group = r.querySelector(".watch-group").value;
    });
  }
  function draw() {
    $("watchEditor").innerHTML = draft
      .map(
        (i, n) =>
          `<div class="watch-editor-row" data-watch-index="${n}"><input class="watch-label" aria-label="显示名称" value="${esc(i.label)}" placeholder="名称"><select class="watch-exchange" aria-label="交易所">${Object.keys(
            exchangeName,
          )
            .map(
              (x) =>
                `<option value="${x}" ${i.exchange === x ? "selected" : ""}>${exchangeName[x]}</option>`,
            )
            .join(
              "",
            )}</select><input class="watch-symbol" aria-label="合约名称" value="${esc(i.symbol)}" placeholder="BTC/USDT:USDT"><select class="watch-group" aria-label="相关资产分组"><option value="crypto" ${i.group === "crypto" ? "selected" : ""}>加密资产</option><option value="equity" ${i.group === "equity" ? "selected" : ""}>股票 / ETF</option></select><button class="icon-button remove-watch" data-index="${n}" aria-label="删除交易对">×</button></div>`,
      )
      .join("");
    document.querySelectorAll(".remove-watch").forEach(
      (b) =>
        (b.onclick = () => {
          read();
          draft.splice(Number(b.dataset.index), 1);
          draw();
        }),
    );
  }
  draw();
  $("appendWatch").onclick = () => {
    read();
    draft.push({
      id: "",
      label: "",
      exchange: "binance",
      symbol: "",
      enabled: true,
      group: "crypto",
    });
    draw();
  };
  $("saveWatch").onclick = () =>
    action(async () => {
      read();
      await api("/api/v2/watchlist", { items: draft }, "PUT");
      $("dialog").close();
      chartGeneration++;
      toast("监测列表已更新");
      await refresh();
      rows = [];
      loadChart();
    });
  $("searchMarkets").onclick = () =>
    action(async () => {
      const result = await api(
        `/api/v2/markets?exchange=${$("searchExchange").value}&q=${encodeURIComponent($("searchQuery").value)}`,
      );
      $("marketSearchResults").textContent =
        result.map((x) => x.symbol).join(" · ") || "没有匹配的已上市合约";
    });
};
function fillSettings() {
  if (!state) return;
  const f = $("aiForm");
  f.model.value = state.preferences.ai_model;
  f.interval.value = state.preferences.ai_interval;
  f.enabled.checked = state.preferences.ai_enabled;
  f.intervene.checked = state.preferences.ai_intervene;
  $("openaiSaved").textContent = state.ai_key_saved ? "已保存" : "未配置";
  for (const p of ["coinglass", "finnhub"])
    $(p + "Saved").textContent = state.providers[p] ? "已保存" : "未配置";
  $("evidenceForm").instrument.innerHTML = state.watchlist
    .map(
      (i) =>
        `<option value="${i.id}">${esc(i.label)} · ${exchangeName[i.exchange]}</option>`,
    )
    .join("");
}
$("aiForm").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  action(async () => {
    await api(
      "/api/v2/preferences",
      {
        ...state.preferences,
        ai_model: f.model.value.trim(),
        ai_interval: Number(f.interval.value),
        ai_enabled: f.enabled.checked,
        ai_intervene: f.intervene.checked,
      },
      "PUT",
    );
    toast("AI 设置已保存");
    await refresh();
  });
};
document.querySelectorAll(".key-form").forEach(
  (f) =>
    (f.onsubmit = (e) => {
      e.preventDefault();
      action(async () => {
        await api("/api/v2/keys/" + f.dataset.provider, {
          api_key: f.key.value,
        });
        f.reset();
        toast("密钥已加密保存");
        await refresh();
        fillSettings();
      });
    }),
);
$("evidenceForm").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  action(async () => {
    await api("/api/v2/evidence", {
      instrument_id: f.instrument.value,
      category: f.category.value,
      source: f.source.value,
      url: f.url.value,
      published_at: new Date(f.published.value).getTime() / 1000,
      expires_at: new Date(f.expires.value).getTime() / 1000,
      bias: f.bias.value,
      event_at: f.event.value ? new Date(f.event.value).getTime() / 1000 : null,
      value: Number(f.value.value),
      note: f.note.value,
    });
    toast("披露信息已保存，等待重新评估");
  });
};
$("help").onclick = () =>
  modal(
    "从这里开始",
    `<div class="notice-box"><b>1 · 先看市场</b><p>点击顶部品种卡片切换 K 线；周线、日线、4 小时、1 小时共同参与机会评估。未收盘 K 线只展示，不参与评分。</p></div><div class="notice-box"><b>2 · 理解机会</b><p>达到 80 分且没有阻断项，机会才有效。评分不等于胜率。两档止盈分别显示价格和比例；数据缺失、来源过期和风险事件都会影响有效性。</p></div><div class="notice-box"><b>3 · 用模拟账户验证</b><p>到账户页设置资金和仓位，再启用自动交易。系统会等待有效信号。重启后账户自动交易暂停，需要重新启用。</p></div><div class="notice-box"><b>4 · 连接真实账户</b><p>先添加账户、选择允许交易的品种、保存密钥并读取核对。真实下单需要服务端开关和当前账户的验收记录，界面会显示尚未满足的条件。</p></div>`,
  );
action(async () => {
  try {
    const session = await api("/api/session");
    csrf = session.csrf;
    await start();
  } catch {
    $("login").hidden = false;
    $("app").hidden = true;
  }
});
