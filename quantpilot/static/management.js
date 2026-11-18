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
let parameterFields = [];
function renderParameterFields(schema) {
  const properties = schema.properties;
  parameterFields = Object.entries(properties)
    .filter(([k]) => !["overrides", "semantics_version"].includes(k))
    .map(([k, v]) => [k, v.title, v.minimum, v.maximum, v.type]);
  const groups = [
    ...new Set(
      Object.values(properties)
        .map((v) => v.group)
        .filter(Boolean),
    ),
  ];
  $("logicFields").innerHTML = groups
    .map(
      (group) =>
        `<details class="parameter-group"><summary>${esc(group)}</summary><div class="parameter-grid">${parameterFields
          .filter(([k]) => properties[k].group === group)
          .map(([key, label, min, max, type]) =>
            type === "boolean"
              ? `<label class="check"><input name="${key}" type="checkbox">${esc(label)}</label>`
              : `<div class="parameter-field"><label for="logic_${key}">${esc(label)}</label><input id="logic_${key}" name="${key}" type="number" min="${min}" max="${max}" step="${type === "integer" ? 1 : "any"}" required>${!["ttl_override", "target_1_ratio"].includes(key) ? `<label class="override-row"><input type="checkbox" name="override_${key}" aria-label="启用 ${esc(label)}" checked>${properties[key].optional ? "启用此条件 · 关闭后不参与" : "使用此参数 · 关闭时停用依赖计算"}</label>` : `<small class="fine">所属模块开启时生效</small>`}<button type="button" class="text-button parameter-default" data-default="${key}">恢复默认 ${properties[key].default}</button></div>`,
          )
          .join("")}</div></details>`,
    )
    .join("");
  $("logicFields")
    .querySelectorAll("[data-default]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          $("logicForm").elements.namedItem(b.dataset.default).value =
            properties[b.dataset.default].default;
          logicDirty = true;
        }),
    );
}
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
  no_safe_proposal: "未提供可应用修改（旧记录）",
  no_proposal: "未提出修改",
  evidence_missing: "交易证据不足",
  unchanged_proposal: "参数无变化",
  invalid_proposal: "方案校验未通过",
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
    const override = form.elements.namedItem("override_" + key);
    if (override) override.checked = values.overrides?.[key] !== false;
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
        "cooldown_hours",
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
      if (!parameterFields.length)
        renderParameterFields(fresh.parameter_schema);
      $("logicVersion").textContent = fresh.current.id.slice(0, 8);
      $("logicCreated").textContent =
        `${sources[fresh.current.source]} · ${time(fresh.current.created_at)}`;
      $("logicStatus").textContent =
        `${fresh.running ? "正在复盘，完成后自动刷新" : fresh.status}${fresh.history_scope?.generation ? " · 样本起点：" + time(fresh.history_scope.started_at) : ""}${fresh.next_review_at ? " · 下次检查：" + time(fresh.next_review_at) : ""}`;
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
        ["已实现净盈亏 · USD 参考", fmt(m.net_pnl)],
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
      $("logicEvidence").textContent =
        `已记录完整交易过程 ${fresh.evidence_summary.recorded_complete_trades} 笔；当前统计中约 ${fresh.evidence_summary.legacy_missing} 笔缺少新格式证据。权益与浮盈浮亏按 5 秒采样，历史缺失不会由 AI 补造。`;
      $("logicMigration").textContent = fresh.migration_note || "";
      $("logicMigration").hidden = !fresh.migration_note;
      const labels = fresh.parameter_schema.properties;
      $("effectiveLogic").innerHTML =
        `<p class="fine">这是已保存并实际生效的配置。未保存编辑不影响运行。模块关闭时所属参数仅保留、不执行。</p><div class="effective-rules">${Object.entries(
          fresh.effective.switches,
        )
          .map(
            ([k, v]) =>
              `<div><span>${esc(labels[k]?.title || k)}</span><b class="${v ? "positive" : "muted"}">${v ? "启用" : "停用"}</b></div>`,
          )
          .join(
            "",
          )}</div><details><summary>查看已保存的参数值</summary><div class="effective-rules">${Object.entries(
          fresh.effective.values,
        )
          .map(
            ([k, v]) =>
              `<div><span>${esc(labels[k]?.title || k)}</span><b>${v == null ? "停用" : esc(String(v))}</b></div>`,
          )
          .join("")}</div></details>`;
      if (!reviewDirty) fillForm($("logicReviewForm"), fresh.settings);
      if (!archiveDirty)
        $("archiveForm").max_backups.value = fresh.settings.max_backups;
      $("logicReviews").innerHTML = fresh.reviews.length
        ? `<table class="management-table"><thead><tr><th>时间 / 版本</th><th>评分 / 合格线</th><th>结果</th><th>净盈亏 / USD 参考</th><th>盈利因子</th><th>盈亏比</th><th>完整交易</th><th>复盘说明</th></tr></thead><tbody>${[
            ...fresh.reviews,
          ]
            .reverse()
            .map(
              (r) =>
                `<tr><td>${time(r.at)}<small>${esc(r.version.slice(0, 8))}</small></td><td>${scoreText(r.score)}<small>合格线 ${r.passing_score}</small></td><td><span class="badge">${esc(actions[r.action] || r.action)}</span></td>${metricCells(r.metrics)}<td class="review-summary">${esc(r.summary)}${r.proposal_reason ? `<p>${esc(r.proposal_reason)}</p>` : ""}${r.validation ? `<p class="fine">${esc(r.validation)}</p>` : ""}${r.comparison ? `<p class="fine">历史入场规则复算 ${r.comparison.samples} 笔，决策变化 ${r.comparison.changed_decisions} 笔；不是收益回测。</p>` : ""}${r.changes ? `<details><summary>查看修改前后 · ${Object.keys(r.changes).length} 项</summary><pre class="logic-diff">${esc(JSON.stringify(r.changes, null, 2))}</pre></details>` : ""}${r.weaknesses?.length ? `<ul>${r.weaknesses.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</td></tr>`,
            )
            .join("")}</tbody></table>`
        : '<div class="management-empty">还没有复盘记录。连接 AI 后可立即复盘，或开启定期复盘。</div>';
      $("logicArchives").innerHTML = fresh.archives.length
        ? `<table class="management-table"><thead><tr><th>存档时间 / 版本</th><th>来源</th><th>原版本评分</th><th>净盈亏 / USD 参考</th><th>盈利因子</th><th>盈亏比</th><th>完整交易</th><th>操作</th></tr></thead><tbody>${[
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
        parameterFields.map(([k, , , , type]) => [
          k,
          type === "boolean"
            ? f.elements.namedItem(k).checked
            : Number(f.elements.namedItem(k).value),
        ]),
      );
    parameters.overrides = Object.fromEntries(
      parameterFields
        .filter(([, , , , t]) => t !== "boolean")
        .filter(([k]) => f.elements.namedItem("override_" + k))
        .map(([k]) => [k, f.elements.namedItem("override_" + k).checked]),
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

$("showLogicEvidence").onclick = () =>
  action(async () => {
    const e = await api("/api/v2/logic/evidence");
    modal(
      "交易证据与分组统计",
      `<p class="fine">账户已匿名化。跨版本仓位单独标识，交易所未提供的费用或盈亏显示未知。旧交易只恢复已有记录，不补造当时指标。</p><div class="table-wrap"><table class="management-table"><thead><tr><th>账户 / 品种</th><th>方向 / 版本</th><th>交易数</th><th>净盈亏 / 结算币</th><th>最大盈利占比</th></tr></thead><tbody>${e.groups.map((g) => `<tr><td>${esc(g.account)} · ${esc(g.symbol)}<small>${esc(g.kind)}</small></td><td>${esc(g.side)} · ${esc({ trend_long: "多周期偏多", trend_short: "多周期偏空", mixed_or_flat: "分歧 / 震荡", unknown: "历史状态缺失" }[g.entry_regime] || "未知")}<small>${esc(g.entry_version?.slice(0, 8))} · ${g.attribution === "mixed" ? "跨版本" : "单版本"}</small></td><td>${g.trades}</td><td>${money(g.net_pnl, g.settlement_currency)} ${esc(g.settlement_currency || "USDT")}</td><td>${g.largest_profit_share == null ? "未知" : (g.largest_profit_share * 100).toFixed(1) + "%"}</td></tr>`).join("")}</tbody></table></div><h3>权益采样</h3>${e.equity_curves.map((c) => `<p>${esc(c.account)} · ${esc(c.settlement_currency || "USDT")} · ${c.samples} 个样本 · 观察回撤 ${(c.marked_drawdown_fraction * 100).toFixed(2)}%<br><small>包含入金出金影响；不作为已校正收益率</small></p>`).join("")}<h3>最近完整交易</h3>${e.closed_trade_evidence.map((t) => `<details class="parameter-group"><summary>${esc(t.signal?.symbol || "未知品种")} · ${esc(t.signal?.direction || "未知")} · ${time(t.closed)} · 净盈亏 ${money(t.outcome?.trade_net_pnl, t.settlement_currency)} ${esc(t.settlement_currency || "USDT")}</summary><p>账户 ${esc(t.account)} · 关联版本 ${esc((t.versions || []).map((v) => v?.slice(0, 8)).join(" → "))}</p><p>采样最大浮盈 ${fmt(t.mfe)} / 最大浮亏 ${fmt(t.mae)}</p><p class="fine">缺失：${esc((t.missing || []).join("；") || "已记录入场快照")}</p><details><summary>入场依据与风险快照</summary><pre class="logic-diff">${esc(JSON.stringify({ signal: t.signal, risk: t.risk_and_sizing, account_before: t.account_before, effective_parameters: t.effective_parameters }, null, 2))}</pre></details>${(t.events || []).map((v) => `<details><summary>${time(v.at)} · ${esc({ open: "开仓", add: "加仓", close: "平仓 / 减仓", funding: "资金费", plan_change: "点位调整", strategy_transition: "策略版本变更", native_protection_fill: "交易所保护成交", exchange_fill: "交易所开仓确认" }[v.action] || v.action)}</summary><pre class="logic-diff">${esc(JSON.stringify(v.detail, null, 2))}</pre></details>`).join("")}</details>`).join("") || "<p>暂无完整证据，后续交易将自动记录。</p>"}<h3>最近被拒绝的机会</h3>${e.rejected_opportunities.map((r) => `<p>${time(r.at)} · ${esc(r.detail.signal?.symbol || r.instrument_id)} · ${esc(r.detail.reason)}</p>`).join("") || "<p>暂无记录</p>"}<button type="button" class="secondary" id="closeEvidence">关闭</button>`,
    );
    $("closeEvidence").onclick = () => $("dialog").close();
  });
