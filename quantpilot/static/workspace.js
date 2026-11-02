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
let scoreDirty = false;
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
  logic: ["量化逻辑", "规则透明，修改可追溯，复盘有依据。"],
  admin: ["设置", "管理账号、版本与工作区偏好。"],
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
            invalid_request: "填写内容不符合要求，请检查必填项、格式和数值范围",
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
let busyActions = 0;
async function action(fn) {
  const button = document.activeElement?.closest("button");
  if (button?.disabled) return;
  const formButton = document.activeElement
    ?.closest("form")
    ?.querySelector('button:not([type="button"])');
  const controls = [...new Set([button, formButton].filter(Boolean))];
  const previous = controls.map((b) => b.disabled);
  controls.forEach((b) => (b.disabled = true));
  busyActions++;
  if ($("dialogError")) $("dialogError").hidden = true;
  try {
    await fn();
  } catch (e) {
    toast(e.message);
    if ($("dialog").open && $("dialogError")) {
      $("dialogError").textContent = e.message;
      $("dialogError").hidden = false;
      $("dialogError").scrollIntoView({ block: "nearest" });
    }
  } finally {
    busyActions--;
    controls.forEach((b, i) => (b.disabled = previous[i]));
    if (state && !busyActions) renderAccounts(true);
  }
}
function modal(title, html) {
  $("dialogTitle").textContent = title;
  $("dialogBody").innerHTML =
    '<p id="dialogError" class="banner error" role="alert" hidden></p>' + html;
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
      "auto",
      "ai_action_enabled",
      "position_review_enabled",
      "position_review_interval",
      "profit_trailing_enabled",
      "ai_take_profit_enabled",
      "allowed",
      "sizing",
    ].map((k) => [k, a[k]]),
  );
}
function go(next) {
  page = next;
  window.scrollTo({ top: 0, behavior: "instant" });
  document
    .querySelectorAll("[data-panel]")
    .forEach((x) => (x.hidden = x.dataset.panel !== page));
  document
    .querySelectorAll(".nav")
    .forEach((x) => x.classList.toggle("active", x.dataset.page === page));
  $("breadcrumb").textContent = titles[page][0];
  $("pageTitle").textContent = titles[page][0];
  $("pageDescription").textContent = titles[page][1];
  document.querySelector(".heading-actions").hidden = page !== "market";
  $("scoreHelp").hidden = page !== "market";
  if (page === "logic" || page === "admin") action(() => loadManagement(true));
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
  await loadDisplayPreference();
  timer = setInterval(refresh, window.refreshSeconds * 1000);
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
    if (page === "logic" || page === "admin") await loadManagement(false);
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
  if (!scoreDirty) $("minScore").value = state.preferences.min_score;
  $("scoreThreshold").style.left =
    `calc(${state.preferences.min_score}% - 1px)`;
  $("scoreThreshold").title = `有效门槛 ${state.preferences.min_score} 分`;
  renderAIDataStatus();
  if (typeof renderNotifications === "function") renderNotifications();
  $("halt").textContent = state.preferences.halted
    ? "▷ 解除全局暂停"
    : "Ⅱ 暂停开仓";
  renderTiles();
  renderOpportunity();
  renderAccounts();
  renderOrders();
  $("aiStatus").textContent = state.ai_status;
  $("aiStatus").className = "badge" + (state.ai_error ? " bad" : "");
  const failure = state.ai_error;
  $("aiErrorBox").hidden = !failure;
  $("aiErrorTitle").textContent = failure?.title || "";
  $("aiErrorMessage").textContent = failure?.message || "";
  errorAction("aiErrorAction", failure);
  renderChatGPT();
  renderCodex();
  $("aiErrorCode").textContent = failure
    ? [
        failure.http_status ? `HTTP ${failure.http_status}` : "",
        failure.provider_code,
        failure.checked_at ? time(failure.checked_at) : "",
      ]
        .filter(Boolean)
        .join(" · ")
    : "";
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
  $("scoreLabel").textContent = o?.valid
    ? `已达 ${state.preferences.min_score} 分门槛`
    : `${state.preferences.min_score} 分起生效`;
  $("signalSummary").textContent = o
    ? `${directions[o.direction] || "等待"}方向 · ${o.valid ? "评分及风控检查通过，账户仍会单独检查资金和授权。" : "当前仅供观察，不会新增仓位。"}`
    : "等待多周期数据；新上市合约可能缺少足够周线。";
  const p = o?.plan;
  $("tradePlan").innerHTML = p
    ? `<div class="plan-row"><span>开仓区间</span><strong>${price(p.entry_low)} — ${price(p.entry_high)}</strong></div><div class="plan-row stop"><span>保护止损</span><strong>${price(p.stop)}</strong></div>${p.targets.map((t, n) => `<div class="plan-row target"><span>止盈 ${n + 1} · ${t.enabled === false ? "已关闭" : "平仓 " + fmt(t.ratio * 100, 0) + "%"}</span><strong>${price(t.price)}</strong></div>`).join("")}<div class="plan-row"><span>收益 / 风险 · 含预估费用</span><strong>${fmt(p.rr)} : 1</strong></div><div class="plan-row"><span>加仓观察 / 减仓观察</span><strong>${price(p.add_at)} / ${price(p.reduce_at)}</strong></div>`
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
            `<div class="factor-row"><div><span>${esc(b.name)}</span><b>${b.available ? b.points + " / " + b.max : "缺失"}</b></div><div class="factor-track"><span data-width="${(b.points / b.max) * 100}"></span></div></div>`,
        )
        .join("")
    : '<div class="empty">等待可验证的数据</div>';
  $("scoreDetails")
    .querySelectorAll("[data-width]")
    .forEach((el) => {
      el.style.width = el.dataset.width + "%";
    });
  $("trendRow").innerHTML = ["1w", "1d", "4h", "1h"]
    .map((f, n) => {
      const s = o?.indicators?.[f],
        h = o?.history?.[f];
      return `<div class="trend-cell">${["周线", "日线", "4 小时", "1 小时"][n]}<strong class="${s?.trend === "long" ? "positive" : s?.trend === "short" ? "negative" : ""}">${s ? directions[s.trend] : "数据不足"}</strong><small>RSI ${o?.parameters?.rsi_enabled === false ? "已关闭" : s ? fmt(s.rsi, 1) : "—"} · MACD ${o?.parameters?.macd_enabled === false ? "已关闭" : s ? fmt(s.macd_hist, 2) : "—"}</small>${h ? `<small class="history-note">${esc(h.source || h.note)}${h.last_bar_at ? " · 最后完成 " + time(h.last_bar_at) : ""}</small>` : ""}</div>`;
    })
    .join("");
  const names = {
    etf: "ETF 实际披露资金流",
    options: "期权 / 交割 / 最大痛点",
    liquidation: "清算量与热力图",
    macro: "宏观 / 财报 / 突发新闻",
    derivatives: "跨交易所合约背景",
  };
  const expanded = new Set(
    [...$("evidenceList").querySelectorAll("details[open]")].map(
      (x) => x.dataset.evidence,
    ),
  );
  let evidenceRows =
    o?.evidence ||
    Object.keys(names).map((c) => ({
      category: c,
      status: "missing",
      note: "等待数据源",
    }));
  const item = state.watchlist.find((i) => i.id === selected);
  const extra = (state.ai_data_evidence || []).filter(
    (e) =>
      e.instrument_id === selected &&
      e.exchange === item?.exchange &&
      e.symbol === item?.symbol &&
      !evidenceRows.some((x) => x.category === e.category && x.url === e.url),
  );
  const filled = new Set(
    extra.filter((e) => e.status === "fresh").map((e) => e.category),
  );
  evidenceRows = evidenceRows
    .filter((e) => !(e.status === "missing" && filled.has(e.category)))
    .concat(extra);
  $("evidenceList").innerHTML = evidenceRows
    .map(
      (e) =>
        `<div class="evidence-row"><div><b>${names[e.category] || esc(e.category)}</b><span class="badge ${e.status === "fresh" ? "good" : e.status === "stale" ? "bad" : ""}">${{ fresh: "数据有效", stale: "已过期", missing: "暂缺数据" }[e.status] || "未验证"}</span></div><p>${esc(e.note || "")} ${e.value !== undefined ? "· " + fmt(e.value, 0) : ""}</p>${e.source ? `<p>${esc(e.source)} ${e.published_at ? "· " + time(e.published_at) : ""}</p>` : ""}${
          e.details
            ? `<details data-evidence="${esc(e.category + "|" + e.source)}" ${expanded.has(e.category + "|" + e.source) ? "open" : ""}><summary>查看数据与事件</summary>${e.details.funding_rate !== undefined ? `<p>资金费率 ${e.details.funding_rate == null ? "缺失" : fmt(e.details.funding_rate * 100, 4) + "%"} · OI ${fmt(e.details.open_interest, 2)}</p>` : ""}${e.details.scope ? `<p>${esc(e.details.scope)}</p>` : ""}${e.details.max_pain ? `<p>最近到期 ${time(e.details.expiry)}<br>最大痛点估算 ${price(e.details.max_pain)}<br>看涨 OI ${fmt(e.details.call_oi, 0)} / 看跌 OI ${fmt(e.details.put_oi, 0)}</p>` : ""}${(e.details.events || []).map((x) => `<p>${esc(x.title)} · ${time(x.event_at)}</p>`).join("")}${(
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
      (data.reference
        ? "参考 K 线 · " + data.reference.source + "（非当前合约历史）"
        : data.demo
          ? "演示 K 线"
          : "交易所 K 线") +
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
let chartGeometry = { width: 1, plot: 1, data: [] };
function drawChart() {
  const svg = $("candles"),
    container = $("chartContainer");
  const width = Math.max(240, container.clientWidth),
    height = Math.max(240, container.clientHeight);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  if (!rows.length) {
    svg.innerHTML = "";
    $("chartEmpty").hidden = false;
    return;
  }
  $("chartEmpty").hidden = true;
  const plot = width - (width < 500 ? 64 : 80),
    top = 14,
    bottom = height - 80;
  const data = rows.slice(-Math.min(chartCount, Math.floor(plot / 4))),
    lo = Math.min(...data.map((r) => r[3])),
    hi = Math.max(...data.map((r) => r[2])),
    pad = (hi - lo) * 0.12 || 1,
    min = lo - pad,
    max = hi + pad,
    dx = plot / data.length,
    y = (p) => top + ((max - p) / (max - min)) * (bottom - top),
    vol = Math.max(...data.map((r) => r[5]), 1),
    volBottom = height - 26;
  chartGeometry = { width, plot, data };
  let out = "";
  for (let n = 0; n < 5; n++) {
    const yp = top + (n * (bottom - top)) / 4;
    out += `<line x1="0" y1="${yp}" x2="${plot}" y2="${yp}" stroke="#e8efeb"/><text x="${plot + 7}" y="${yp + 4}" fill="#708779" font-size="11">${price(max - ((max - min) * n) / 4)}</text>`;
  }
  data.forEach((r, n) => {
    const x = n * dx + dx / 2,
      c = r[4] >= r[1] ? "#238c70" : "#c2695c";
    out += `<line x1="${x}" y1="${y(r[2])}" x2="${x}" y2="${y(r[3])}" stroke="${c}"/><rect x="${x - dx * 0.29}" y="${Math.min(y(r[1]), y(r[4]))}" width="${dx * 0.58}" height="${Math.max(1, Math.abs(y(r[1]) - y(r[4])))}" fill="${c}"/><rect x="${x - dx * 0.29}" y="${volBottom - (r[5] / vol) * 35}" width="${dx * 0.58}" height="${(r[5] / vol) * 35}" fill="${c}" opacity=".3"/>`;
    if (n % Math.max(1, Math.floor(data.length / (width < 500 ? 3 : 5))) === 0)
      out += `<text x="${Math.max(1, x - 16)}" y="${height - 7}" fill="#708779" font-size="10">${esc(new Date(r[0]).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" }))}</text>`;
  });
  out += `<line x1="0" y1="${y(data.at(-1)[4])}" x2="${plot}" y2="${y(data.at(-1)[4])}" stroke="#65ab89" stroke-dasharray="3 4"/><line id="crosshair" x1="0" y1="${top}" x2="0" y2="${volBottom}" stroke="#859d8c" stroke-dasharray="3 3" opacity="0"/>`;
  svg.innerHTML = out;
}
$("candles").onpointermove = (e) => {
  const { width, plot, data } = chartGeometry;
  if (!data.length) return;
  const rect = $("candles").getBoundingClientRect(),
    vx = ((e.clientX - rect.left) / rect.width) * width,
    n = Math.max(
      0,
      Math.min(data.length - 1, Math.floor((vx / plot) * data.length)),
    ),
    r = data[n];
  $("chartTooltip").hidden = false;
  $("chartTooltip").textContent =
    `${new Date(r[0]).toLocaleString("zh-CN", { hour12: false })} · 开 ${price(r[1])} 高 ${price(r[2])} 低 ${price(r[3])} 收 ${price(r[4])} 量 ${fmt(r[5], 0)}`;
  const line = $("crosshair");
  if (line) {
    line.setAttribute("x1", ((n + 0.5) * plot) / data.length);
    line.setAttribute("x2", ((n + 0.5) * plot) / data.length);
    line.setAttribute("opacity", "1");
  }
};
$("candles").onpointerleave = () => {
  $("chartTooltip").hidden = true;
  $("crosshair")?.setAttribute("opacity", "0");
};
new ResizeObserver(() => requestAnimationFrame(drawChart)).observe(
  $("chartContainer"),
);
$("minScore").oninput = () => {
  scoreDirty = true;
};
$("scoreForm").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const value = Number($("minScore").value);
    if (!Number.isInteger(value) || value < 1 || value > 100)
      throw Error("有效分数需为 1–100 的整数");
    await api(
      "/api/v2/preferences",
      { ...state.preferences, min_score: value },
      "PUT",
    );
    scoreDirty = false;
    await refresh();
    toast(`有效分数已改为 ${value}，正在重新评估`);
  });
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
function renderAccounts(force = false) {
  if (
    !force &&
    (busyActions ||
      $("accountCards").contains(document.activeElement) ||
      $("positions").contains(document.activeElement))
  )
    return;
  const accounts = state.accounts;
  $("accountCards").innerHTML =
    accounts
      .map((a) => {
        const s = a.summary || {};
        return `<article class="card account-card"><div class="card-head"><div><p class="eyebrow">${exchangeName[a.exchange]} · ${kindName[a.kind]}</p><h2>${esc(a.name)}</h2></div><span class="badge ${a.auto ? "good" : a.kind === "live" ? "live" : ""}">${a.auto ? "自动交易运行中" : "已暂停"}</span></div><div class="account-numbers"><div><small>${a.kind === "paper" ? "模拟权益" : "账户权益"} / USDT</small><strong>${fmt(s.equity)}</strong></div><div><small>可用资金</small><strong>${fmt(s.available)}</strong></div><div><small>浮动盈亏</small><strong class="${Number(s.unrealized) >= 0 ? "positive" : "negative"}">${fmt(s.unrealized)}</strong></div></div><p class="account-state">${a.kind === "paper" ? "已启用品种：" + (a.sizing.map((x) => state.watchlist.find((i) => i.id === x.instrument_id)?.label || x.instrument_id).join("、") || "未选择") : "授权交易对：" + (a.allowed.map((id) => state.watchlist.find((i) => i.id === id)?.label || id).join("、") || "未选择")}<br>${(a.sizing.some((x) => x.mode !== "system" && !x.quantity) || !a.sizing.length ? '<span class="negative">请填写币数量或选择由系统评估，再启用自动交易。</span><br>' : "") + esc(a.execution.reason)}${a.last_error ? '<br><span class="negative">' + esc(a.last_error) + "</span>" : ""}</p><p class="fine">交易前 AI 审核：${a.ai_action_enabled ? "已启用 · 行情共享 / 账户风险独立" : "关闭"}</p><p class="fine">持仓 AI 评估：${a.position_review_enabled ? "已启用 · 每 " + a.position_review_interval + " 秒 · " + esc(a.position_review?.message || "等待评估") : "关闭"}</p><div class="account-actions"><button class="${a.auto ? "secondary" : "primary"}" data-toggle-account="${a.id}">${a.auto ? "暂停自动交易" : "启用自动交易"}</button><button class="secondary" data-edit-account="${a.id}">账户设置</button>${a.kind === "paper" ? `<button class="text-button" data-reset-account="${a.id}">重置模拟资金</button>` : `<button class="secondary" data-account-key="${a.id}">API 密钥</button><button class="text-button" data-check-account="${a.id}">读取与核对</button>`}<button class="text-button negative" data-delete-account="${a.id}">删除账户</button></div></article>`;
      })
      .join("") ||
    '<div class="card empty">尚无账户。点击“添加账户”创建模拟或交易所账户。</div>';
  $("positions").innerHTML =
    accounts
      .flatMap((a) =>
        (a.summary?.positions || []).map((p) => {
          const item = state.watchlist.find((i) => i.id === p.instrument_id);
          const points = p.planned_points || {};
          const planned = (v) => (v == null ? "未计划" : price(v));
          const targetText =
            (points.targets || [])
              .map(
                (t, i) =>
                  `止盈 ${t.stage || i + 1}：${price(t.price)} · ${fmt(t.ratio * 100, 0)}%`,
              )
              .join("<br>") || "止盈：未计划";
          const liquidation =
            p.liquidation_verified === false
              ? "未计划（行情待验证）"
              : p.liquidation_status === "no_positive_price"
                ? "未计划（当前无正价格强平线）"
                : planned(p.liquidation ?? p.liquidationPrice);
          return `<tr><td data-label="账户 / 市场">${esc(a.name)}<small>${esc(item?.symbol || p.symbol)}</small></td><td data-label="方向 / 数量">${directions[p.side] || esc(p.side)} · ${(p.margin_mode || p.marginMode || "isolated") === "cross" ? "全仓" : "逐仓"} ${fmt(p.leverage, 0)}×<small>${fmt(p.qty ?? (p.contracts != null && p.contractSize != null ? p.contracts * p.contractSize : null), 8)}</small></td><td data-label="开仓均价">${price(p.entry ?? p.entryPrice)}</td><td data-label="浮动盈亏" class="${Number(p.unrealized ?? p.unrealizedPnl) >= 0 ? "positive" : "negative"}">${fmt(p.unrealized ?? p.unrealizedPnl)}${p.price_verified === false ? "<small>行情待验证</small>" : ""}</td><td data-label="当前计划点位" class="position-points">${targetText}<br>止损：${planned(points.stop)}<br>加仓：${planned(points.add_at)}<br>减仓：${planned(points.reduce_at)}<br>${a.kind === "paper" ? "预估强平" : "交易所强平"}：${liquidation}<small>${esc(points.review_status || "未计划")} · 点位不代表已成交</small></td><td data-label="操作">${a.kind === "paper" ? `<button class="text-button" data-close-position="${a.id}" data-position="${esc(p.instrument_id)}">减仓 / 平仓</button>` : "交易所原生保护"}</td></tr>`;
        }),
      )
      .join("") ||
    '<tr><td colspan="6" class="empty">还没有仓位。启用模拟账户后，系统会等待有效机会。</td></tr>';
  bindAccounts();
  renderPositionChanges();
}
function changeValue(key, value) {
  if (value == null) return "未计划";
  if (key === "targets")
    return value
      .map((t, i) =>
        t.enabled
          ? `止盈 ${i + 1}：${price(t.price)} / ${fmt(t.ratio * 100, 0)}%`
          : `止盈 ${i + 1}：停用`,
      )
      .join("；");
  return price(value);
}
function renderPositionChanges() {
  const rows = state.position_changes || [];
  $("positionChanges").innerHTML = rows.length
    ? rows
        .map((r) => {
          const account = state.accounts.find((a) => a.id === r.account_id);
          const item = state.watchlist.find((i) => i.id === r.instrument_id);
          return `<article class="position-change"><div class="card-head"><strong>${esc(account?.name || state.archived_accounts?.[r.account_id] || "已删除账户")} · ${esc(item?.symbol || r.instrument_id)}</strong><time>${time(r.created)}</time></div><p class="fine">${r.stage === "protection" ? "保护点位已更新" : "加减仓计划已更新"} · 不代表已成交</p>${Object.entries(
            r.changes,
          )
            .map(
              ([k, v]) =>
                `<div class="position-change-row"><span>${esc(
                  {
                    stop: "止损",
                    targets: "止盈安排",
                    target_basis_qty: "止盈比例基准数量",
                    add_at: "加仓计划",
                    reduce_at: "减仓计划",
                  }[k] || k,
                )}</span><div><del>${esc(changeValue(k, v.before))}</del><strong>→ ${esc(changeValue(k, v.after))}</strong></div></div>`,
            )
            .join(
              "",
            )}<p class="position-change-reason">${esc(r.reason)}</p></article>`;
        })
        .join("")
    : '<p class="empty">暂无持仓 AI 调整。仅记录已生效的点位或计划变化，原样保留不重复提醒。</p>';
}
function bindAccounts() {
  document.querySelectorAll("[data-delete-account]").forEach(
    (b) =>
      (b.onclick = () => {
        const a = state.accounts.find((x) => x.id === b.dataset.deleteAccount);
        modal(
          "删除账户",
          `<p>删除 <b>${esc(a.name)}</b> 及其保存的 API 密钥？</p><div class="notice-box">历史交易记录会保留。须先暂停自动交易并处理平台管理的仓位及待核对订单。此操作不会替你平掉交易所外部仓位。</div><div class="dialog-footer"><button id="cancelDelete" class="secondary">取消</button><button id="confirmDeleteAccount" class="danger-outline">确认删除账户</button></div>`,
        );
        $("cancelDelete").onclick = () => $("dialog").close();
        $("confirmDeleteAccount").onclick = () =>
          action(async () => {
            await api(`/api/v2/accounts/${a.id}`, {}, "DELETE");
            $("dialog").close();
            await refresh();
            toast("账户及密钥已删除，历史记录已保留");
          });
      }),
  );
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
            const result = await api(
              `/api/v2/accounts/${b.dataset.closePosition}/close`,
              {
                instrument_id: b.dataset.position,
                ratio: Number(e.currentTarget.ratio.value),
              },
            );
            if (result.status !== "filled" && result.status !== "duplicate") {
              toast(result.reason || "等待 AI 审核，请稍后再次确认");
              return;
            }
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
    `<form id="accountForm"><div class="form-grid"><label>账户名称<input name="name" maxlength="50" required value="${esc(a?.name || "")}" placeholder="例如：OKX 趋势账户"></label><label>账户类型<select name="kind" ${a ? "disabled" : ""}><option value="paper">本地模拟 · 虚拟资金</option><option value="demo">交易所测试 · 测试 API</option><option value="live">真实账户 · 交易所 API</option></select></label><label>交易所 / 模拟交易所<select name="exchange" ${a ? "disabled" : ""}>${Object.keys(
      exchangeName,
    )
      .map((x) => `<option value="${x}">${exchangeName[x]}</option>`)
      .join(
        "",
      )}</select></label><label id="paperCapital">初始模拟资金 / USDT<input name="capital" type="number" min="100" max="100000000" step="any" value="${a?.capital || 10000}" ${a ? "disabled" : ""}></label></div><p id="accountKindHint" class="notice-box"></p><fieldset class="position-review-settings"><legend>每次交易前 AI 审核（账户独立）</legend><label class="check"><input type="checkbox" name="actionReview" ${a?.ai_action_enabled ? "checked" : ""}>启用此账户的交易前 AI 审核</label><p class="fine">只审核本账户已启用的交易对。同一交易所、同一交易对的行情与点位结论共用，各账户资金、强平风险独立审核。Telegram 会分别报告各账户是否获准及成交状态。</p><p class="fine">当前共用评估周期 ${state.preferences.evaluation_interval} 秒，价格重审阈值 ${state.preferences.ai_reaudit_threshold_pct}%。在「数据与 AI」中统一调整。AI 超时或不可用时暂停开仓、加仓；既有止盈止损保护继续。</p></fieldset><fieldset class="position-review-settings"><legend>现有仓位 AI 定期评估</legend><label class="check"><input type="checkbox" name="positionReview" ${a?.position_review_enabled ? "checked" : ""}>启用此账户的持仓 AI 评估</label><label>持仓评估间隔（秒）<input type="number" name="positionInterval" min="60" max="86400" step="1" value="${a?.position_review_interval || 300}" required></label><p class="fine">独立于交易前审核，对本平台管理的现有仓位评估止盈、止损和加减仓点位。加仓仍须账户自动交易和该交易对已启用；未落定时不新增加仓计划，既有保护继续。会消耗当前 AI 连接额度。</p><div class="position-review-options"><label class="check"><input type="checkbox" name="profitTrailing" ${a?.profit_trailing_enabled ? "checked" : ""}>盈利移动止损 · 由持仓 AI 评估决定</label><p class="fine">盈利空间足以覆盖估算手续费和滑点时，AI 可设置并移动保护浮盈的止损。止损只向降低回撤的方向移动；不保证获得最大利润。</p><label class="check"><input type="checkbox" name="aiTakeProfit" ${a?.ai_take_profit_enabled ? "checked" : ""}>允许 AI 决定是否分批止盈</label><p class="fine">需此持仓的第一、第二档止盈均已启用。AI 可选择第一档一次止盈，或两档止盈并调整比例；已执行止盈批次不重新分配。两项功能均依赖持仓 AI 评估。</p></div></fieldset><h3>逐交易对设置</h3><p class="muted small">可填写标的数量（例如 0.01 BTC），或选择由系统评估。杠杆与逐仓 / 全仓独立设置，风险偏好仍控制亏损及总敞口。未配置的品种不会自动开仓。</p><div id="sizingRows"></div><div class="dialog-footer"><button type="button" id="cancelAccount" class="secondary">取消</button><button class="primary">保存账户设置</button></div></form>`,
  );
  const f = $("accountForm"),
    drafts = new Map();
  f.kind.value = a?.kind || "paper";
  f.exchange.value = a?.exchange || "binance";
  function syncPositionReview() {
    f.profitTrailing.disabled = f.aiTakeProfit.disabled =
      !f.positionReview.checked;
    if (!f.positionReview.checked)
      f.profitTrailing.checked = f.aiTakeProfit.checked = false;
  }
  f.positionReview.onchange = syncPositionReview;
  syncPositionReview();
  $("cancelAccount").onclick = () => $("dialog").close();
  function readDrafts() {
    document.querySelectorAll(".sizing-row").forEach((r) =>
      drafts.set(r.dataset.sizeId, {
        checked: r.querySelector(".allowed").checked,
        mode: r.querySelector(".size-mode").value,
        system_level: r.querySelector(".size-level").value,
        quantity: r.querySelector(".size-quantity").value,
        leverage: r.querySelector(".size-leverage").value,
        margin_mode: r.querySelector(".size-margin-mode").value,
        count: r.querySelector(".size-adds").value,
        ratios: r.querySelector(".size-ratios").value,
        open: r.querySelector("details").open,
      }),
    );
  }
  function populate() {
    const paper = f.kind.value === "paper";
    $("paperCapital").hidden = !paper;
    f.capital.disabled = !paper || !!a;
    f.capital.required = paper;
    $("accountKindHint").textContent = paper
      ? "模拟资金仅在本地记账。可配置所有监测品种；合约杠杆规格按所选模拟交易所核验。"
      : "真实 / 测试账户不需要填写模拟资金。保存后点击账户卡片的「API 密钥」连接交易所，再读取余额；自动下单默认关闭。";
    const items = state.watchlist.filter(
      (i) => paper || i.exchange === f.exchange.value,
    );
    $("sizingRows").innerHTML =
      items
        .map((i) => {
          const setting = a?.sizing.find((x) => x.instrument_id === i.id);
          const d = drafts.get(i.id) || {
            checked: !!setting || !!a?.allowed.includes(i.id),
            mode: setting?.mode || "maximum",
            system_level: setting?.system_level || "balanced",
            quantity: setting?.quantity ?? "",
            leverage: setting?.leverage || 1,
            margin_mode: setting?.margin_mode || "isolated",
            count: setting?.add_count ?? "",
            ratios:
              setting?.add_ratios
                .map((x) => Number((x * 100).toFixed(4)))
                .join(", ") || "",
            open: !!setting,
          };
          const unit = i.symbol.split("/")[0];
          const held = (a?.summary?.positions || []).find(
            (p) => p.instrument_id === i.id || p.symbol === i.symbol,
          );
          if (held)
            d.margin_mode = held.margin_mode || held.marginMode || "isolated";
          return `<div class="sizing-row" data-size-id="${i.id}"><label class="check"><input type="checkbox" class="allowed" ${d.checked ? "checked" : ""}><b>${esc(i.label)}</b><span class="muted small">${esc(i.symbol)} · ${paper ? "启用此交易对" : "授权自动交易"}</span></label><details ${d.open ? "open" : ""}><summary>数量、保证金、杠杆与加仓</summary><div class="sizing-grid"><label>仓位模式<select class="size-mode"><option value="maximum" ${d.mode === "maximum" ? "selected" : ""}>最大总持仓数量</option><option value="fixed" ${d.mode === "fixed" ? "selected" : ""}>固定首次开仓数量</option><option value="system" ${d.mode === "system" ? "selected" : ""}>由系统评估</option></select></label><label class="system-level-label">资金使用偏好<select class="size-level"><option value="conservative" ${d.system_level === "conservative" ? "selected" : ""}>保守 · 预留更多资金</option><option value="balanced" ${d.system_level === "balanced" ? "selected" : ""}>平衡 · 均衡利用资金</option><option value="aggressive" ${d.system_level === "aggressive" ? "selected" : ""}>激进 · 提高资金利用</option></select></label><label class="quantity-label">标的数量 / ${esc(unit)}<input class="size-quantity" type="number" min="0.00000001" max="1000000000" step="any" required value="${esc(d.quantity)}" placeholder="填写 ${esc(unit)} 数量"></label><label>保证金模式<select class="size-margin-mode" ${held ? 'data-held="true"' : ""}><option value="isolated" ${d.margin_mode === "isolated" ? "selected" : ""}>逐仓 · 保证金独立</option><option value="cross" ${d.margin_mode === "cross" ? "selected" : ""}>全仓 · 共享账户保证金</option></select><small class="margin-mode-note">${held ? "当前有持仓，平仓后才能切换模式。" : "仅此交易对生效；已有仓位不自动转换。"}</small></label><label>此交易对杠杆 / 倍<input class="size-leverage" type="number" min="1" max="1000" step="1" required value="${esc(d.leverage)}"></label><div class="limit-control"><button type="button" class="secondary load-limits">读取交易所上限</button><small class="limit-note">尚未核验。高于 1 倍必须通过服务器校验。</small></div><label>最多加仓次数<input class="size-adds" type="number" min="0" max="5" step="1" value="${esc(d.count)}" placeholder="留空：系统评估；0：不加仓"></label><label>每次加仓比例 / %<input class="size-ratios" value="${esc(d.ratios)}" placeholder="例如 25, 20" inputmode="decimal"></label></div><p class="sizing-help"><b>逐仓</b>：只使用分配给该仓位的保证金。<b>全仓</b>：同一账户的全仓仓位共享 USDT 保证金，其他仓位盈亏会影响强平线；逐仓占用资金不参与共享。模拟采用统一费率估算，不等于所选交易所的实际强平价。<br>固定：每次初始开仓按填写数量，风控额度不足则不下单。最大：初始仓位由系统评估，后续加仓也不超过此总数量。<br><b>由系统评估</b>：无需填写币数量。保守 / 平衡 / 激进分别以可用资金的 20% / 40% / 70% 作为本次保证金及开仓费预算上限；止损风险预算分别为当前风险偏好额度的 50% / 75% / 100%。还需通过强平距离、相关敞口及总风险限制，实际用量可能更低。计划加仓时预留额度。<br><b>25, 20</b> 表示两次分别加「初始开仓数量」的 25% 和 20%。例如初始 0.01 BTC，对应 0.0025、0.002 BTC。不是剩余仓位的比例。次数和比例都留空时系统评估；只填次数时每次上限为初始数量的 25%；填 0 禁止加仓。</p></details></div>`;
        })
        .join("") ||
      '<p class="empty">此交易所尚无监测品种，请先在监测列表添加。</p>';
    document.querySelectorAll(".sizing-row").forEach((r) => {
      const update = () => {
        const yes = r.querySelector(".allowed").checked;
        r.querySelectorAll(
          ".sizing-grid input,.sizing-grid select,.load-limits",
        ).forEach((x) => (x.disabled = !yes));
        const system = r.querySelector(".size-mode").value === "system";
        r.querySelector(".quantity-label").hidden = system;
        r.querySelector(".size-quantity").disabled = !yes || system;
        r.querySelector(".size-quantity").required = yes && !system;
        r.querySelector(".system-level-label").hidden = !system;
        r.querySelector(".size-level").disabled = !yes || !system;
        if (r.querySelector(".size-margin-mode").dataset.held)
          r.querySelector(".size-margin-mode").disabled = true;
        if (yes) r.querySelector("details").open = true;
      };
      r.querySelector(".allowed").onchange = update;
      r.querySelector(".size-mode").onchange = update;
      update();
      r.querySelector(".load-limits").onclick = () =>
        action(async () => {
          const note = r.querySelector(".limit-note");
          note.textContent = "正在读取合约规格…";
          const limits = await api(
            `/api/v2/limits/${r.dataset.sizeId}?exchange=${f.exchange.value}&account_id=${encodeURIComponent(a?.id || "")}`,
          );
          r.querySelector(".size-leverage").max = limits.max_leverage;
          note.textContent = `${limits.verified ? "最高 " + limits.max_leverage + " 倍。" : "暂只允许 1 倍。"}${limits.min_quantity ? "最小数量 " + limits.min_quantity + "。" : ""}${limits.quantity_step ? "数量步长 " + limits.quantity_step + "。" : ""}${limits.note}`;
        });
    });
  }
  f.kind.onchange = f.exchange.onchange = () => {
    readDrafts();
    populate();
  };
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
          input = r.querySelector(".size-ratios").value.trim();
        const percentages = input
          ? input.split(/[,，]/).map((x) => x.trim())
          : [];
        if (
          percentages.some(
            (x) =>
              !x ||
              !Number.isFinite(Number(x)) ||
              Number(x) <= 0 ||
              Number(x) > 100,
          )
        )
          throw Error("加仓比例请填写 0 到 100 之间的百分数，例如 25, 20");
        if (percentages.length && Number(count) !== percentages.length)
          throw Error("加仓次数必须与比例个数一致，例如次数 2、比例 25, 20");
        sizing.push({
          instrument_id: r.dataset.sizeId,
          mode: r.querySelector(".size-mode").value,
          quantity:
            r.querySelector(".size-mode").value === "system"
              ? null
              : Number(r.querySelector(".size-quantity").value),
          system_level: r.querySelector(".size-level").value,
          leverage: Number(r.querySelector(".size-leverage").value),
          margin_mode: r.querySelector(".size-margin-mode").value,
          add_count: count === "" ? null : Number(count),
          add_ratios: percentages.map((x) => Number(x) / 100),
        });
      });
      await api("/api/v2/accounts", {
        id: a?.id || "",
        name: f.elements.namedItem("name").value.trim(),
        kind: f.kind.value,
        exchange: f.exchange.value,
        capital: f.kind.value === "paper" ? Number(f.capital.value) : null,
        auto: a?.auto || false,
        ai_action_enabled: f.actionReview.checked,
        position_review_enabled: f.positionReview.checked,
        position_review_interval: Number(f.positionInterval.value),
        profit_trailing_enabled: f.profitTrailing.checked,
        ai_take_profit_enabled: f.aiTakeProfit.checked,
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
  $("accountKeyForm").passphrase.required =
    state.accounts.find((a) => a.id === id)?.exchange === "okx";
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
  const scope = state.history_scope || { generation: 0, started_at: 0 };
  $("historyScope").textContent = scope.generation
    ? `第 ${scope.generation + 1} 轮 · ${time(scope.started_at)} 起统计；跨轮旧仓位不计入新复盘，未决订单继续显示。`
    : "当前为初始统计轮次；清除后使用新开并完整平仓的交易重新复盘。";
  const names = {
    ...Object.fromEntries(
      Object.entries(state.archived_accounts || {}).map(([id, name]) => [
        id,
        name + "（已删除）",
      ]),
    ),
    ...Object.fromEntries(state.accounts.map((a) => [a.id, a.name])),
  };
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
        .filter((r) => r.created >= (state.history_scope?.started_at || 0))
        .slice(0, 30)
        .map(
          (r) =>
            `<div class="audit-row"><time>${time(r.created)}</time><span>${esc({ history_reset: "交易记录与复盘样本已重置", position_plan_changed: "持仓 AI 点位已调整", position_review: "持仓 AI 评估完成", v2_account_saved: "账户设置已保存", v2_preferences_saved: "风险偏好或运行状态已更新", v2_account_reset: "模拟账户已重置", v2_watchlist_saved: "监测列表已更新", v2_startup: "服务启动，所有账户自动交易暂停", v2_execution_paused: "交易所执行已暂停", v2_open: "模拟开仓", v2_close: "模拟平仓", v2_add: "模拟加仓", v2_ai_review: "AI 复核完成", login: "登录成功", logout: "退出登录" }[r.event] || r.event)}</span></div>`,
        )
        .join("") || '<p class="empty">暂无操作记录</p>';
  } catch (e) {
    toast(e.message);
  }
}
$("resetHistory").onclick = () => {
  const generation = state.history_scope?.generation || 0;
  modal(
    "清除交易记录并重置复盘样本",
    `<p>将全部账户已有交易移出当前记录列表，并重新积累 AI 定期复盘评分样本。旧评分不再用于本轮评估。</p><div class="notice-box">当前仓位、资金、止盈止损和策略参数保留。重置前已持有的仓位，即使之后平仓也不计入本轮评分；未决订单仍须核对。底层订单与审计记录保留，防止重复下单。</div><form id="historyResetForm"><label>输入 RESET HISTORY 确认<input name="confirmation" autocomplete="off" required pattern="RESET HISTORY" placeholder="RESET HISTORY"></label><div class="dialog-footer"><button type="button" id="cancelHistoryReset" class="secondary">取消</button><button class="danger-outline">清除并开始新一轮</button></div></form>`,
  );
  $("cancelHistoryReset").onclick = () => $("dialog").close();
  $("historyResetForm").onsubmit = (e) => {
    e.preventDefault();
    action(async () => {
      await api("/api/v2/history/reset", {
        confirmation: e.currentTarget.confirmation.value,
        expected_generation: generation,
      });
      $("dialog").close();
      await refresh();
      await loadAudit();
      toast("交易记录已清除，复盘样本从新一轮开始积累");
    });
  };
};
$("refreshAudit").onclick = loadAudit;
$("editWatch").onclick = () => {
  let draft = structuredClone(state.watchlist);
  modal(
    "管理监测列表",
    `<p class="muted">输入交易所的统一合约名称，例如 BTC/USDT:USDT。可以编辑名称、交易所或添加其他品种。</p><div id="watchEditor"></div><button id="appendWatch" class="text-button">＋ 添加一个交易对</button><details class="market-search"><summary>查找交易所支持的永续合约</summary><div class="form-grid"><label>交易所<select id="searchExchange">${Object.keys(
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
      d.history_enabled = r.querySelector(".watch-history").checked;
      d.history_interval = Number(
        r.querySelector(".watch-history-interval").value,
      );
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
            )}</select><input class="watch-symbol" aria-label="合约名称" value="${esc(i.symbol)}" placeholder="BTC/USDT:USDT"><select class="watch-group" aria-label="相关资产分组"><option value="crypto" ${i.group === "crypto" ? "selected" : ""}>加密资产</option><option value="equity" ${i.group === "equity" ? "selected" : ""}>股票 / ETF</option></select><button class="icon-button remove-watch" data-index="${n}" aria-label="删除交易对">×</button><div class="history-settings"><label class="check"><input class="watch-history" type="checkbox" ${i.history_enabled ? "checked" : ""}>周期不足时补全参考历史</label><label>抓取间隔 / 秒<input class="watch-history-interval" type="number" min="300" max="86400" step="300" value="${i.history_interval || 3600}" aria-label="抓取间隔秒数"><small>填写秒数，300 = 5 分钟，3600 = 1 小时。按品种限频；先尝试其他交易所，AVGO/SOXL 可用标的股票参考。AI 联网补全已开启时，再检索公开历史表格。</small></label></div></div>`,
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
let chatgptPairBaseline = null;
function connectionReady(provider) {
  return provider === "codex"
    ? state.codex?.ready
    : provider === "chatgpt"
      ? state.chatgpt?.ready
      : state.ai_key_saved;
}
function errorAction(id, error) {
  const link = $(id),
    allowed = [
      "https://chatgpt.com/settings/usage",
      "https://platform.openai.com/settings/organization/billing/overview",
    ];
  link.hidden = !allowed.includes(error?.action_url);
  if (!link.hidden) {
    link.href = error.action_url;
    link.textContent = error.action_label + " ↗";
  }
}
function toggleProvider() {
  const official = $("aiProvider").value === "chatgpt";
  $("chatgptPanel").hidden = !official;
  $("codexPanel").hidden = $("aiProvider").value !== "codex";
  $("openaiKeyForm").hidden = $("aiProvider").value !== "api_key";
  $("aiProviderHint").textContent =
    $("aiProvider").value === "codex"
      ? "设备授权在服务器完成，使用官方 Codex 调用通道。无需复制 API Key 或本机凭据。"
      : official
        ? "使用获准共享的 ChatGPT 计划额度。Plus 用量与其他应用共享；不会自动切换到 API Key。"
        : "使用 OpenAI API 余额，账单与 ChatGPT Plus 订阅分开。";
  renderChatGPT();
  renderCodex();
}
let deviceAttempt = null;
let activatingCodex = false;
function clearDevice() {
  deviceAttempt = null;
  $("codexCode").value = "";
  $("codexDevice").hidden = true;
}
function renderCodex() {
  const c = state?.codex;
  if (!c) return;
  $("codexBadge").textContent = c.ready
    ? "已登录" + (c.plan ? " · " + c.plan : "")
    : c.pending
      ? "等待授权"
      : c.available
        ? "未连接"
        : "服务未就绪";
  $("codexBadge").className = "badge" + (c.ready ? " good" : "");
  $("codexLogin").hidden = c.ready || !!deviceAttempt;
  $("codexLogin").disabled = !!deviceAttempt || !c.available;
  $("codexLogout").hidden = !c.ready;
  $("codexUse").hidden = !c.ready || state.preferences.ai_provider === "codex";
  if (!deviceAttempt)
    $("codexMessage").textContent =
      c.error?.message ||
      (c.ready
        ? "连接已就绪，请获取模型并保存设置。"
        : c.pending
          ? "服务器正在等待授权，点击 Continue with ChatGPT 可重新显示当前授权码。"
          : c.available
            ? "点击上方按钮开始登录。"
            : "服务器 AI 服务暂不可用，请使用 qp ai-status 检查。");
  if (deviceAttempt && c.ready && !activatingCodex) {
    if ($("aiProvider").value !== "codex") {
      clearDevice();
      toast("服务器 ChatGPT 已登录，可选择服务器登录方式并使用此连接");
      return;
    }
    activatingCodex = true;
    api("/api/v2/codex/activate", {})
      .then(async () => {
        clearDevice();
        modelLoaded = false;
        await refresh();
        fillSettings();
        toast("ChatGPT 已连接。请选择模型并保存 AI 设置；自动交易已暂停");
      })
      .catch((e) => {
        clearDevice();
        toast(e.message);
      })
      .finally(() => {
        activatingCodex = false;
      });
  } else if (
    deviceAttempt &&
    (!c.available || c.error || Date.now() / 1000 >= deviceAttempt.expires_at)
  ) {
    clearDevice();
    $("codexMessage").textContent =
      c.error?.message || "授权码已失效，请重新生成。";
  }
}
$("codexLogin").onclick = () =>
  action(async () => {
    const result = await api("/api/v2/codex/login", {});
    deviceAttempt = result;
    $("codexCode").value = result.user_code;
    $("codexDevice").hidden = false;
    $("codexMessage").textContent =
      "请在 10 分钟内完成授权，仅在官方页面输入此代码。";
    await refresh();
  });
$("codexCopy").onclick = () =>
  action(async () => {
    try {
      await navigator.clipboard.writeText($("codexCode").value);
      toast("授权码已复制");
    } catch {
      $("codexCode").select();
      toast("请手动复制选中的授权码");
    }
  });
$("codexCancel").onclick = () =>
  action(async () => {
    await api("/api/v2/codex/cancel", {});
    clearDevice();
    await refresh();
  });
$("codexUse").onclick = () =>
  action(async () => {
    await api("/api/v2/codex/activate", {});
    modelLoaded = false;
    await refresh();
    fillSettings();
    toast("连接已切换，请选择模型；自动交易已暂停");
  });
$("codexLogout").onclick = () => {
  if (
    !confirm(
      "断开服务器的 ChatGPT 登录？相关 AI 自动任务和账户自动交易将暂停，已有仓位保护继续运行。",
    )
  )
    return;
  action(async () => {
    await api("/api/v2/codex/logout", {});
    clearDevice();
    modelLoaded = false;
    await refresh();
    fillSettings();
    toast("服务器 ChatGPT 连接已断开");
  });
};
function renderChatGPT() {
  if (!state?.chatgpt) return;
  const c = state.chatgpt,
    select = $("chatgptProfile"),
    previous = select.value;
  const signature =
    JSON.stringify(c.profiles.map((p) => [p.id, p.label, p.connected])) +
    c.active;
  if (select.dataset.signature !== signature) {
    select.innerHTML =
      '<option value="">添加新账户</option>' +
      c.profiles
        .map(
          (p) =>
            `<option value="${esc(p.id)}">${esc(p.label)}${p.id === c.active ? " · 当前账户" : ""}${p.connected ? "" : " · 已断开"}</option>`,
        )
        .join("");
    select.value = c.profiles.some((p) => p.id === previous)
      ? previous
      : c.active || "";
    select.dataset.signature = signature;
  }
  const selectedProfile = c.profiles.find((p) => p.id === select.value),
    active = c.profiles.find((p) => p.id === c.active);
  $("chatgptUse").disabled =
    !selectedProfile?.connected || select.value === c.active;
  $("chatgptDisconnect").disabled = !selectedProfile?.connected;
  $("chatgptBadge").textContent = c.ready
    ? "已连接 · 使用 ChatGPT 计划"
    : active?.paused === "plan_limit"
      ? "共享额度已用完"
      : active?.connected
        ? "需检查计划授权"
        : "未连接";
  $("chatgptBadge").className = "badge" + (c.ready ? " good" : "");
  $("chatgptResume").hidden = !active?.connected || !active?.paused;
  if (
    chatgptPairBaseline !== null &&
    c.revision !== chatgptPairBaseline &&
    $("dialog").open &&
    $("chatgptPairCode")
  ) {
    chatgptPairBaseline = null;
    $("chatgptPairCode").value = "";
    $("dialog").close();
    fillSettings();
    toast("官方登录已连接，请获取模型并保存 AI 设置；自动交易已暂停");
  }
}
$("chatgptProfile").onchange = renderChatGPT;
$("aiProvider").onchange = () => {
  modelLoaded = false;
  const f = $("aiForm");
  f.model.value = "";
  f.modelList.innerHTML = '<option value="">请重新获取当前连接的模型</option>';
  f.enabled.checked = false;
  f.intervene.checked = false;
  $("modelMessage").textContent =
    "绑定方式已更改，选择模型后保存生效。切换连接会暂停账户自动交易。";
  toggleProvider();
};
$("chatgptLogin").onclick = () =>
  action(async () => {
    const result = await api("/api/v2/chatgpt/pair", {
      profile_id: $("chatgptProfile").value,
    });
    chatgptPairBaseline = state.chatgpt?.revision || "initial";
    modal(
      "OpenAI 官方登录授权",
      `<p>在运行浏览器的电脑上启动 QuantPilot 登录助手，随后只在 OpenAI 官方页面登录并确认授权。</p><ol class="login-steps"><li>在本机终端运行：<code id="chatgptCommand"></code><small>需先安装 QuantPilot；已部署在远程服务器上的程序不能代替本机助手。<a href="https://github.com/Jk-z-Box/QuantPilot/blob/feat/mission-control-v2/docs/CHATGPT_LOGIN.md" target="_blank" rel="noopener noreferrer">安装与使用说明 ↗</a></small></li><li>助手提示时粘贴下方一次性连接码：<input id="chatgptPairCode" type="password" readonly autocomplete="off" aria-label="一次性连接码"><button type="button" id="copyPairCode" class="secondary">复制连接码</button><small>10 分钟内有效，仅限本次连接。不要发送给其他人。</small></li><li>在 OpenAI 页面选择你的 ChatGPT 账户，确认允许 QuantPilot 使用计划。成功后返回此页面选择模型。</li></ol><p class="fine">登录成功会暂停账户自动交易及 AI 自动任务，供你核对连接和模型。官方授权不支持时会明确报错。</p><div class="dialog-footer"><button id="closePair" class="secondary">关闭</button></div>`,
    );
    $("chatgptCommand").textContent =
      "qp chatgpt-login --server " + location.origin;
    $("chatgptPairCode").value = result.pair_code;
    $("copyPairCode").onclick = () =>
      action(async () => {
        await navigator.clipboard.writeText($("chatgptPairCode").value);
        toast("连接码已复制，请粘贴到本机登录助手");
      });
    $("closePair").onclick = () => {
      $("chatgptPairCode").value = "";
      $("dialog").close();
    };
  });
$("chatgptUse").onclick = () =>
  action(async () => {
    await api("/api/v2/chatgpt/select", {
      profile_id: $("chatgptProfile").value,
    });
    await refresh();
    modelLoaded = false;
    fillSettings();
    toast("账户已切换，请重新选择模型；自动交易已暂停");
  });
$("chatgptResume").onclick = () =>
  action(async () => {
    await api("/api/v2/chatgpt/resume", {});
    await refresh();
    toast("已允许重试，请重新获取模型或发起复核");
  });
$("chatgptDisconnect").onclick = () => {
  const id = $("chatgptProfile").value;
  modal(
    "断开官方授权",
    '<p>断开后会清除服务器上的登录凭据，并尝试撤销 OpenAI 会话。如果这是当前 AI 连接，AI 功能及账户自动交易将暂停。</p><div class="dialog-footer"><button id="cancelDisconnect" class="secondary">取消</button><button id="confirmDisconnect" class="danger-outline">确认断开</button></div>',
  );
  $("cancelDisconnect").onclick = () => $("dialog").close();
  $("confirmDisconnect").onclick = () =>
    action(async () => {
      const result = await api("/api/v2/chatgpt/profiles/" + id, {}, "DELETE");
      $("dialog").close();
      await refresh();
      modelLoaded = false;
      fillSettings();
      toast(result.message);
    });
};
function fillSettings() {
  if (!state) return;
  const f = $("aiForm");
  $("aiProvider").value = state.preferences.ai_provider || "api_key";
  toggleProvider();
  f.model.value = state.preferences.ai_model;
  f.modelMode.value = "custom";
  toggleModelMode();
  if (connectionReady($("aiProvider").value) && !modelLoaded) loadModels(false);
  f.interval.value = state.preferences.ai_interval;
  f.enabled.checked = state.preferences.ai_enabled;
  f.intervene.checked = state.preferences.ai_intervene;
  f.evaluationInterval.value = state.preferences.evaluation_interval ?? 30;
  f.reauditThreshold.value = state.preferences.ai_reaudit_threshold_pct ?? 0.3;
  $("aiDataForm").enabled.checked = state.preferences.ai_data_enabled;
  $("aiDataForm").interval.value = String(state.preferences.ai_data_interval);
  renderAIDataStatus();
  $("openaiSaved").textContent = state.ai_key_saved ? "已保存" : "未配置";
  document
    .querySelectorAll("[data-delete-key]")
    .forEach(
      (b) =>
        (b.disabled = !(b.dataset.deleteKey === "openai"
          ? state.ai_key_saved
          : state.providers[b.dataset.deleteKey])),
    );
  for (const p of ["coinglass", "finnhub"])
    $(p + "Saved").textContent = state.providers[p] ? "已保存" : "未配置";
  $("evidenceForm").instrument.innerHTML = state.watchlist
    .map(
      (i) =>
        `<option value="${i.id}">${esc(i.label)} · ${exchangeName[i.exchange]}</option>`,
    )
    .join("");
}
function renderAIDataStatus() {
  if (!state) return;
  const status = state.ai_data_status || {};
  $("aiDataBadge").textContent =
    {
      disabled: "未启用",
      idle: "待检索",
      running: "检索中",
      ready: "已补全",
      empty: "未发现可用资料",
      error: "检索失败",
    }[status.state] || "未启用";
  $("aiDataBadge").className =
    "badge " +
    (status.state === "ready" ? "good" : status.state === "error" ? "bad" : "");
  $("aiDataStatus").textContent =
    (status.message || "未启用") +
    (status.last_attempt ? " · 最近尝试 " + time(status.last_attempt) : "");
  $("requestAIData").disabled =
    !state.preferences.ai_data_enabled || status.state === "running";
  errorAction("aiDataBilling", status.state === "error" ? status.error : null);
  $("dataDiagnostics").innerHTML =
    (status.diagnostics || [])
      .map(
        (d) =>
          `<p><b>${esc(state.watchlist.find((i) => i.id === d.instrument_id)?.label || d.instrument_id)} · ${esc(d.category)}</b><br>${esc(d.reason)}</p>`,
      )
      .join("") +
    Object.values(state.source_diagnostics || {})
      .map(
        (d) =>
          `<p><b>${esc(d.source)}</b> · ${d.status === "available" ? "已读取" : "暂不可用"}<br>${esc(d.reason)} · ${time(d.checked_at)}</p>`,
      )
      .join("");
  $("actionReviewStatus").innerHTML = (state.action_reviews || [])
    .slice(-6)
    .map(
      (d) =>
        `<p>${esc(state.accounts.find((a) => a.id === d.account_id)?.name || "共享交易对")} · ${esc(state.watchlist.find((i) => i.id === d.instrument_id)?.label || d.instrument_id)} · ${esc(d.action)} · ${{ pending: "审核中", ready: "已返回决策", error: "审核不可用" }[d.state] || esc(d.state)}${d.reason ? " · " + esc(d.reason) : ""}</p>`,
    )
    .join("");
}
$("aiDataForm").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  action(async () => {
    if (
      f.enabled.checked &&
      (!connectionReady(state.preferences.ai_provider) ||
        !state.preferences.ai_model)
    )
      throw Error("请先保存可用的 AI 连接和模型，再开启数据补全");
    await api(
      "/api/v2/preferences",
      {
        ...state.preferences,
        ai_data_enabled: f.enabled.checked,
        ai_data_interval: Number(f.interval.value),
      },
      "PUT",
    );
    await refresh();
    toast(
      state.preferences.ai_data_enabled
        ? "AI 补全已开启，将按设置间隔检索缺失资料"
        : "AI 补全已关闭，已有补全结果不再计入评分",
    );
  });
};
$("requestAIData").onclick = () =>
  action(async () => {
    const result = await api("/api/v2/ai/data", {});
    toast(result.message);
    await refresh();
  });
$("aiForm").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  action(async () => {
    await api(
      "/api/v2/preferences",
      {
        ...state.preferences,
        ai_provider: $("aiProvider").value,
        ai_data_enabled:
          state.preferences.ai_provider === $("aiProvider").value
            ? state.preferences.ai_data_enabled
            : false,
        ai_model:
          f.modelMode.value === "list"
            ? f.modelList.value
            : f.model.value.trim(),
        ai_interval: Number(f.interval.value),
        ai_enabled: f.enabled.checked,
        ai_intervene: f.intervene.checked,
        evaluation_interval: Number(f.evaluationInterval.value),
        ai_reaudit_threshold_pct: Number(f.reauditThreshold.value),
      },
      "PUT",
    );
    toast(
      state.preferences.ai_provider !== $("aiProvider").value
        ? "连接方式已切换，自动交易已暂停，请重新核对设置"
        : "AI 设置已保存",
    );
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
        modelLoaded = false;
        updateKeyStatus();
        if (
          f.dataset.provider === "openai" &&
          $("aiProvider").value === "api_key"
        )
          await loadModels(true);
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
let modelLoaded = false;
function toggleModelMode() {
  const list = $("aiForm").modelMode.value === "list";
  $("modelListLabel").hidden = !list;
  $("modelCustomLabel").hidden = list;
  $("aiForm").modelList.disabled = !list;
  $("aiForm").model.disabled = list;
}
async function loadModels(selectList = true) {
  const button = $("loadModels");
  if (button.disabled) return;
  button.disabled = true;
  $("modelMessage").textContent = "正在从所选连接读取模型…";
  const f = $("aiForm"),
    current =
      f.modelMode.value === "list" ? f.modelList.value : f.model.value.trim();
  const provider = $("aiProvider").value;
  try {
    const result = await api("/api/v2/models?provider=" + provider);
    if ($("aiProvider").value !== provider) return;
    modelLoaded = true;
    f.modelList.innerHTML =
      '<option value="">请选择模型</option>' +
      result.models
        .map((id) => `<option value="${esc(id)}">${esc(id)}</option>`)
        .join("");
    f.modelList.value = result.models.includes(current) ? current : "";
    if (selectList || result.models.includes(current))
      f.modelMode.value = "list";
    $("modelMessage").textContent = result.note;
  } catch (e) {
    if ($("aiProvider").value !== provider) return;
    f.modelMode.value = "custom";
    $("modelMessage").textContent = e.message + "。已切换为自定义填写。";
  } finally {
    button.disabled = false;
    toggleModelMode();
  }
}
$("aiForm").modelMode.onchange = toggleModelMode;
$("loadModels").onclick = () => loadModels();
function updateKeyStatus() {
  $("openaiSaved").textContent = state.ai_key_saved ? "已保存" : "未配置";
  for (const p of ["coinglass", "finnhub"])
    $(p + "Saved").textContent = state.providers[p]
      ? "已保存"
      : "未配置 · 使用公开来源";
  document
    .querySelectorAll("[data-delete-key]")
    .forEach(
      (b) =>
        (b.disabled = !(b.dataset.deleteKey === "openai"
          ? state.ai_key_saved
          : state.providers[b.dataset.deleteKey])),
    );
}
document.querySelectorAll("[data-delete-key]").forEach(
  (b) =>
    (b.onclick = () => {
      const provider = b.dataset.deleteKey;
      modal(
        "删除 API 密钥",
        `<p>确认删除 ${esc(provider)} 的服务器端密钥？</p><p class="muted">${provider === "openai" ? "当前使用 API Key 时，删除会关闭 AI 功能；官方登录连接独立保留。" : "删除后仅保留可用的公开数据来源。"}已有订单保护继续运行。</p><div class="dialog-footer"><button class="secondary" id="cancelKeyDelete">取消</button><button class="danger-outline" id="confirmKeyDelete">确认删除密钥</button></div>`,
      );
      $("cancelKeyDelete").onclick = () => $("dialog").close();
      $("confirmKeyDelete").onclick = () =>
        action(async () => {
          await api("/api/v2/keys/" + provider, {}, "DELETE");
          $("dialog").close();
          await refresh();
          updateKeyStatus();
          if (
            provider === "openai" &&
            state.preferences.ai_provider === "api_key"
          ) {
            modelLoaded = false;
            $("aiForm").enabled.checked = false;
            $("aiForm").intervene.checked = false;
            $("aiDataForm").enabled.checked = false;
            $("aiForm").modelList.innerHTML =
              '<option value="">请先保存密钥再获取</option>';
            $("aiForm").modelMode.value = "custom";
            toggleModelMode();
            $("modelMessage").textContent =
              "密钥已删除；可自定义填写模型 ID，保存新密钥后再启用。";
          }
          toast("API 密钥已删除");
        });
    }),
);
$("help").onclick = () =>
  modal(
    "从这里开始",
    `<div class="notice-box"><b>1 · 先看市场</b><p>点击顶部品种卡片切换 K 线；周线、日线、4 小时、1 小时共同参与机会评估。未收盘 K 线只展示，不参与评分。</p></div><div class="notice-box"><b>2 · 理解机会</b><p>达到你设置的有效分数且没有阻断项，机会才有效；默认 80 分，可在顶部修改。评分不等于胜率。两档止盈分别显示价格和比例；数据缺失、来源过期和风险事件都会影响有效性。</p></div><div class="notice-box"><b>3 · 用模拟账户验证</b><p>到账户页设置资金和仓位，再启用自动交易。系统会等待有效信号。重启后账户自动交易暂停，需要重新启用。</p></div><div class="notice-box"><b>4 · 连接真实账户</b><p>先添加账户、选择允许交易的品种、保存密钥并读取核对。真实下单需要服务端开关和当前账户的验收记录，界面会显示尚未满足的条件。</p></div>`,
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

$("accountMenu").onclick = () => {
  modal(
    "工作区菜单",
    `<div class="notice-box"><button id="menuHelp" class="text-button">查看使用指南</button></div><div class="notice-box"><a href="/legacy">打开旧版模拟账本</a></div><div class="notice-box"><button id="menuLogout" class="danger-outline">退出登录</button></div>`,
  );
  $("menuHelp").onclick = () => {
    $("dialog").close();
    $("help").click();
  };
  $("menuLogout").onclick = () => {
    $("dialog").close();
    $("logout").click();
  };
};
