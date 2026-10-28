"use strict";
let logicState = null,
  logicDirty = false,
  reviewDirty = false,
  archiveDirty = false,
  adminDirty = false,
  displayDirty = false,
  checkedUpdate = null,
  managementBusy = false;
window.refreshSeconds = 5;
const parameterFields = [
  ["ema_fast", "快速 EMA 周期", 10, 30, 1, "用于周线、日线、4H 和 1H。"],
  ["ema_slow", "慢速 EMA 周期", 40, 60, 1, "与快速 EMA 比较，判断趋势方向。"],
  ["rsi_period", "RSI 周期", 7, 21, 1, "多头动量区间 43–70，空头 30–57。"],
  [
    "volume_multiplier",
    "成交量确认倍数",
    1,
    2,
    0.05,
    "乘以风险偏好的成交量门槛；1 表示原门槛。",
  ],
  [
    "atr_stop",
    "ATR 止损倍数",
    1.5,
    3,
    0.1,
    "止损距离 = ATR × 此倍数。仓位仍受风险额度限制。",
  ],
  [
    "target_1",
    "止盈 1 / 风险倍数",
    1.2,
    2.5,
    0.1,
    "1.5 表示止盈距离是初始止损距离的 1.5 倍。",
  ],
  [
    "target_2",
    "止盈 2 / 风险倍数",
    2.5,
    5,
    0.1,
    "须高于第一档；剩余仓位在这一档平仓。",
  ],
  [
    "target_1_ratio",
    "止盈 1 平仓比例",
    0.25,
    0.75,
    0.05,
    "0.5 = 平仓 50%，第二档自动平剩余 50%。",
  ],
];
$("logicFields").innerHTML = parameterFields
  .map(
    ([key, label, min, max, step, hint]) =>
      `<label>${label}<input name="${key}" type="number" min="${min}" max="${max}" step="${step}" required /><small>${hint}</small></label>`,
  )
  .join("");
const sources = {
  initial: "初始版本",
  manual: "手动编辑",
  ai: "AI 优化",
  restore: "恢复存档",
  import: "导入",
};
const actions = {
  passing: "合格 · 保持",
  below: "待优化",
  optimized: "已优化",
  insufficient: "样本不足",
  cooldown: "冷却中",
  external_active: "交易所持仓保护中",
  no_safe_proposal: "无可用方案",
};
function scoreText(v) {
  return v === null || v === undefined ? "待评分" : v + " / 100";
}
function metricCells(m) {
  return `<td class="number">${fmt(m.net_pnl)}</td><td class="number">${fmt(m.profit_factor)}</td><td class="number">${fmt(m.payoff_ratio)}</td><td class="number">${m.trades}</td>`;
}
function fillForm(form, values) {
  Object.entries(values).forEach(([key, value]) => {
    const input = form.elements.namedItem(key);
    if (!input) return;
    if (input.type === "checkbox") input.checked = value;
    else input.value = value;
  });
}
function reviewValues() {
  const f = $("logicReviewForm");
  return {
    ...logicState.settings,
    enabled: f.enabled.checked,
    auto_optimize: f.auto_optimize.checked,
    ...Object.fromEntries(
      [
        "passing_score",
        "interval_hours",
        "minimum_trades",
        "lookback_days",
      ].map((k) => [k, Number(f.elements.namedItem(k).value)]),
    ),
  };
}
async function loadDisplayPreference() {
  try {
    const info = await api("/api/v2/admin");
    window.refreshSeconds = info.display.refresh_seconds;
  } catch {
    window.refreshSeconds = 5;
  }
}
async function loadManagement(force = false) {
  if (managementBusy) return;
  managementBusy = true;
  try {
    if (page === "logic") {
      const fresh = await api("/api/v2/logic");
      const versionChanged =
        logicState && fresh.current.id !== logicState.current.id;
      if (versionChanged && logicDirty)
        toast("策略版本已更新，你的未保存修改保留；保存时会检查版本冲突");
      // A dirty editor retains the version it was opened against.
      if (!logicDirty) $("logicForm").dataset.version = fresh.current.id;
      logicState = fresh;
      $("logicVersion").textContent = fresh.current.id.slice(0, 8);
      $("logicCreated").textContent =
        `${sources[fresh.current.source]} · ${time(fresh.current.created_at)}`;
      $("logicStatus").textContent =
        `${fresh.running ? "正在复盘，完成后自动刷新" : fresh.status}${fresh.next_review_at ? " · 下次检查：" + time(fresh.next_review_at) : ""}`;
      $("reviewLogic").disabled = fresh.running;
      const m = fresh.metrics,
        latest = [...fresh.reviews]
          .reverse()
          .find(
            (r) =>
              r.version === fresh.current.id &&
              r.score !== null &&
              r.policy?.profile === fresh.policy?.profile &&
              r.policy?.min_score === fresh.policy?.min_score,
          );
      $("logicMetrics").innerHTML = [
        ["当前逻辑评分", scoreText(latest?.score)],
        [
          "完整交易 / 最低样本",
          `${m.trades} / ${fresh.settings.minimum_trades}`,
        ],
        ["已实现净盈亏 · USDT", fmt(m.net_pnl)],
        [
          "盈利因子 / 盈亏比",
          `${fmt(m.profit_factor)} / ${fmt(m.payoff_ratio)}`,
        ],
      ]
        .map(
          ([label, value]) =>
            `<div><span>${label}</span><strong>${value}</strong></div>`,
        )
        .join("");
      if (!logicDirty) fillForm($("logicForm"), fresh.current.parameters);
      if (!reviewDirty) fillForm($("logicReviewForm"), fresh.settings);
      if (!archiveDirty)
        $("archiveForm").max_backups.value = fresh.settings.max_backups;
      $("logicReviews").innerHTML = fresh.reviews.length
        ? `<table class="management-table"><thead><tr><th>时间 / 版本</th><th>评分 / 合格线</th><th>结果</th><th>净盈亏</th><th>盈利因子</th><th>盈亏比</th><th>完整交易</th><th>复盘说明</th></tr></thead><tbody>${[
            ...fresh.reviews,
          ]
            .reverse()
            .map(
              (r) =>
                `<tr><td>${time(r.at)}<small>${esc(r.version.slice(0, 8))}</small></td><td>${scoreText(r.score)}<small>合格线 ${r.passing_score}</small></td><td><span class="badge">${esc(actions[r.action] || r.action)}</span></td>${metricCells(r.metrics)}<td class="review-summary">${esc(r.summary)}${r.weaknesses?.length ? `<ul>${r.weaknesses.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</td></tr>`,
            )
            .join("")}</tbody></table>`
        : '<div class="management-empty">还没有复盘记录。连接 AI 后可立即复盘，或开启定期复盘。</div>';
      $("logicArchives").innerHTML = fresh.archives.length
        ? `<table class="management-table"><thead><tr><th>存档时间 / 版本</th><th>来源</th><th>原版本评分</th><th>净盈亏</th><th>盈利因子</th><th>盈亏比</th><th>完整交易</th><th>操作</th></tr></thead><tbody>${[
            ...fresh.archives,
          ]
            .reverse()
            .map(
              (a) =>
                `<tr><td>${time(a.archived_at)}<small>${esc(a.version.id.slice(0, 8))}</small></td><td>${esc(sources[a.version.source])}${a.imported ? "<small>外部导入 · 仅供参考</small>" : ""}</td><td>${scoreText(a.score)}</td>${metricCells(a.metrics)}<td><button class="text-button" data-archive="${esc(a.version.id)}">查看 / 恢复</button></td></tr>`,
            )
            .join("")}</tbody></table>`
        : '<div class="management-empty">首次修改后会在这里自动保存旧版本。</div>';
      $("logicArchives")
        .querySelectorAll("[data-archive]")
        .forEach((b) => (b.onclick = () => showArchive(b.dataset.archive)));
    } else if (page === "admin") {
      const info = await api("/api/v2/admin");
      if (!adminDirty) $("identityForm").username.value = info.username;
      if (!displayDirty) fillForm($("displayForm"), info.display);
      $("systemVersion").textContent = "v" + info.version;
      $("systemInfo").innerHTML =
        `<dl><dt>访问地址</dt><dd>${esc(info.origin)}</dd><dt>服务监听</dt><dd>${esc(info.host)}:${info.port}</dd><dt>更新分支</dt><dd>${esc(info.channel)}</dd></dl>`;
      $("updateStatus").textContent =
        `${info.update.message}${info.update.at ? " · " + time(info.update.at) : ""}`;
      $("applyUpdate").disabled =
        !checkedUpdate?.newer ||
        ["running", "unavailable"].includes(info.update.state);
    }
  } finally {
    managementBusy = false;
  }
}
$("logicForm").oninput = () => (logicDirty = true);
$("logicReviewForm").oninput = () => (reviewDirty = true);
$("archiveForm").oninput = () => (archiveDirty = true);
$("identityForm").oninput = () => (adminDirty = true);
$("displayForm").oninput = () => (displayDirty = true);
$("logicDiscard").onclick = () =>
  action(async () => {
    logicDirty = false;
    $("logicForm").note.value = "";
    await loadManagement(true);
    toast("已载入当前生效参数");
  });
$("logicForm").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const f = e.currentTarget,
      parameters = Object.fromEntries(
        parameterFields.map(([k]) => [
          k,
          Number(f.elements.namedItem(k).value),
        ]),
      );
    await api(
      "/api/v2/logic",
      {
        expected_version: f.dataset.version,
        parameters,
        note: f.note.value || "手动调整",
      },
      "PUT",
    );
    logicDirty = false;
    f.note.value = "";
    await loadManagement(true);
    toast("旧版本已备份，新版本已生效");
  });
};
$("logicReviewForm").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    await api("/api/v2/logic/settings", reviewValues(), "PUT");
    reviewDirty = false;
    await loadManagement(true);
    toast("复盘设置已保存");
  });
};
$("reviewLogic").onclick = () =>
  action(async () => {
    await api("/api/v2/logic/review", {});
    await loadManagement();
    toast("复盘已提交，结果将在本页自动更新");
  });
$("archiveForm").onsubmit = (e) => {
  e.preventDefault();
  const max = Number(e.currentTarget.max_backups.value);
  const save = async () => {
    await api(
      "/api/v2/logic/settings",
      { ...logicState.settings, max_backups: max },
      "PUT",
    );
    archiveDirty = false;
    $("dialog").close();
    await loadManagement(true);
    toast("存档保留数量已更新");
  };
  if (max < logicState.archives.length) {
    modal(
      "调整存档数量",
      `<p>将删除最旧的 ${logicState.archives.length - max} 份存档。可先导出策略与存档保留副本。</p><button class="danger-outline" id="confirmPrune">确认保留 ${max} 份</button>`,
    );
    $("confirmPrune").onclick = () => action(save);
  } else action(save);
};
function showArchive(id) {
  const a = logicState.archives.find((x) => x.version.id === id);
  if (!a) return;
  const expected = logicState.current.id;
  modal(
    "版本 " + id.slice(0, 8),
    `<p class="muted">${esc(a.version.note)} · ${time(a.version.created_at)}</p><dl class="archive-details">${parameterFields.map(([key, label]) => `<dt>${label}</dt><dd>${fmt(a.version.parameters[key], 2)}</dd>`).join("")}</dl><p class="fine">恢复前会先备份当前版本。历史盈亏只供参考，新版本从本机重新积累交易。</p><div class="dialog-footer"><button class="primary" id="confirmRestore">备份当前版本并恢复</button></div>`,
  );
  $("confirmRestore").onclick = () =>
    action(async () => {
      await api("/api/v2/logic/restore", {
        expected_version: expected,
        archive_id: id,
      });
      logicDirty = false;
      $("dialog").close();
      await loadManagement(true);
      toast("已恢复为新版本");
    });
}
$("exportLogic").onclick = () =>
  action(async () => {
    const bundle = await api("/api/v2/logic/export"),
      url = URL.createObjectURL(
        new Blob([JSON.stringify(bundle, null, 2)], {
          type: "application/json",
        }),
      );
    const a = document.createElement("a");
    a.href = url;
    a.download =
      "quantpilot-logic-" + new Date().toISOString().slice(0, 10) + ".json";
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast("策略与存档文件已导出");
  });
$("importLogic").onclick = () => $("logicFile").click();
$("logicFile").onchange = (e) =>
  action(async () => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    if (file.size > 950000) throw Error("文件过大，最多 950 KB");
    let bundle;
    try {
      bundle = JSON.parse(await file.text());
    } catch {
      throw Error("请选择有效的 JSON 策略导出文件");
    }
    if (
      bundle.format !== "quantpilot-logic-v1" ||
      !bundle.current?.parameters ||
      !Array.isArray(bundle.archives)
    )
      throw Error("不是支持的 QuantPilot 策略文件");
    const expected = logicState.current.id;
    modal(
      "导入策略",
      `<p>将导入 ${esc(file.name)}，包含 ${bundle.archives.length} 份历史存档。</p><p class="muted">先备份当前版本，再应用导入参数。按当前存档上限保留最新记录；不导入账号、密钥、自动优化开关和本机交易证据。</p><button class="primary" id="confirmImport">备份并导入</button>`,
    );
    $("confirmImport").onclick = () =>
      action(async () => {
        await api("/api/v2/logic/import", {
          expected_version: expected,
          bundle,
        });
        logicDirty = false;
        $("dialog").close();
        await loadManagement(true);
        toast("导入成功，等待新版本信号");
      });
  });
$("identityForm").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const f = e.currentTarget;
    if (f.new_password.value !== f.confirm_password.value)
      throw Error("两次新密码不一致");
    try {
      await api(
        "/api/v2/admin/identity",
        {
          username: f.username.value,
          current_password: f.current_password.value,
          new_password: f.new_password.value,
        },
        "PUT",
      );
    } finally {
      f.current_password.value =
        f.new_password.value =
        f.confirm_password.value =
          "";
    }
    stop();
    csrf = "";
    state = null;
    adminDirty = false;
    $("app").hidden = true;
    $("login").hidden = false;
    $("loginForm").username.value = f.username.value;
    toast("账号已更新，请使用新信息重新登录");
  });
};
$("displayForm").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const result = await api(
      "/api/v2/admin/display",
      { refresh_seconds: Number(e.currentTarget.refresh_seconds.value) },
      "PUT",
    );
    window.refreshSeconds = result.refresh_seconds;
    clearInterval(timer);
    timer = setInterval(refresh, window.refreshSeconds * 1000);
    displayDirty = false;
    toast("界面设置已保存");
  });
};
$("checkUpdate").onclick = () =>
  action(async () => {
    $("latestUpdate").textContent = "正在检查 GitHub…";
    try {
      checkedUpdate = await api("/api/v2/admin/update/check", {});
      $("latestUpdate").textContent =
        `${checkedUpdate.newer ? "可更新至" : "当前发布版本"} v${checkedUpdate.version} · ${checkedUpdate.commit.slice(0, 10)}`;
      await loadManagement();
    } catch (e) {
      $("latestUpdate").textContent = e.message;
      throw e;
    }
  });
$("applyUpdate").onclick = () => {
  if (!checkedUpdate?.newer) return;
  const commit = checkedUpdate.commit;
  modal(
    "更新 QuantPilot",
    `<p>安装 v${esc(checkedUpdate.version)} · ${esc(commit.slice(0, 10))}。需先暂停自动交易并确认无持仓、无待核对订单。</p><p class="muted">安装会备份数据并重启服务，网页可能暂时断开。更新后自动开仓保持暂停。</p><form id="updateConfirmForm"><label>当前管理员密码<input type="password" name="password" autocomplete="current-password" required maxlength="256" /></label><div class="dialog-footer"><button class="primary">确认备份并更新</button></div></form>`,
  );
  $("updateConfirmForm").onsubmit = (e) => {
    e.preventDefault();
    action(async () => {
      const f = e.currentTarget;
      try {
        const result = await api("/api/v2/admin/update", {
          commit,
          current_password: f.password.value,
        });
        $("updateStatus").textContent = result.message;
        checkedUpdate = null;
        $("applyUpdate").disabled = true;
        $("dialog").close();
        toast(result.message);
      } finally {
        f.password.value = "";
      }
    });
  };
};
