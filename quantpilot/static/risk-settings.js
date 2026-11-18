/* Account limits use the server schema and the same values used by execution. */
function riskSettings(a) {
  const schema = state.risk_schema;
  const policy = a?.risk_policy || {
    limits: schema.presets.balanced,
    default_add_count: 2,
  };
  const ai = a?.risk_ai || {};
  return `<fieldset class="position-review-settings risk-settings"><legend>账户资金与风险额度</legend>
    <p class="fine">仅影响此账户。百分比以本钱包权益计算；名义敞口是持仓总价值，不是保证金。关闭某项后不再使用该限制。实际可用资金、订单权限和止损必须先于强平等基础检查始终生效。</p>
    <div class="risk-preset"><label>填写预设（可继续编辑）<select id="riskPreset"><option value="conservative">保守 · 多留储备</option><option value="balanced" selected>平衡 · 均衡利用</option><option value="aggressive">激进 · 提高利用</option></select></label><button type="button" id="applyRiskPreset" class="secondary">填入预设值</button></div>
    <div class="risk-limit-grid">${Object.entries(schema.limits)
      .map(([k, spec]) => {
        const c = policy.limits[k];
        return `<div class="risk-limit" data-risk-limit="${k}"><label class="check"><input class="risk-enabled" type="checkbox" ${c.enabled ? "checked" : ""}>${esc(spec[0])}</label><label class="risk-value-label">限值 / ${esc(spec[1])}<input class="risk-value" type="number" min="${spec[2]}" max="${spec[3]}" step="any" value="${c.value}" required></label><small>${k === "group_multiple" ? "加密资产、股票类资产分别计算，按组取总名义敞口。" : k === "trade_loss_pct" ? "本次增量预计止损损失，含估算手续费和滑点。" : k === "portfolio_loss_pct" ? "所有仓位合计的预计止损损失，含估算费用。" : k === "daily_loss_pct" ? "按 UTC 日初权益计算；触及后暂停新增风险。" : k.includes("liq") ? "可调整的额外缓冲；基础强平安全检查仍生效。" : "开仓与加仓共用；已有仓位占用也计入。"}</small></div>`;
      })
      .join("")}</div>
    <label>未单独设置时，默认最多加仓次数<input name="riskDefaultAdds" type="number" min="0" max="5" step="1" value="${policy.default_add_count}" required></label>
    <p class="fine">页面顶部的“信号评估风格”只影响机会评分，不再修改账户资金限额。旧账户按升级前有效额度迁移；逐交易对的分配参数可在下方查看。</p>
    </fieldset>
    <fieldset class="position-review-settings risk-ai-settings"><legend>AI 账户额度管理 · 独立开关</legend>
    <label class="check"><input name="riskAIEnabled" type="checkbox" ${ai.enabled ? "checked" : ""}>成交和资金变化后，自动评估并应用额度</label>
    <div class="form-grid"><label>AI 资金利用偏好<select name="riskAILevel">${[
      ["conservative", "保守 · 预留较多资金"],
      ["balanced", "平衡 · 保持储备"],
      ["aggressive", "激进 · 提高可行资金利用"],
    ]
      .map(
        ([v, t]) =>
          `<option value="${v}" ${ai.level === v || (!ai.level && v === "balanced") ? "selected" : ""}>${t}</option>`,
      )
      .join("")}</select></label>
    <label>最小评估间隔 / 秒<input name="riskAIInterval" type="number" min="30" max="3600" step="1" value="${ai.min_interval || 60}" required></label>
    <label>评估有效期 / 秒<input name="riskAIValidity" type="number" min="60" max="86400" step="1" value="${ai.validity_seconds || 1800}" required></label>
    <label>权益变化触发 / %<input name="riskAIEquity" type="number" min="0.1" max="50" step="any" value="${ai.equity_change_pct || 2}" required></label></div>
    <p class="fine">开启后会将本账户的匿名化资金、持仓及行情发送给当前 AI 服务，消耗对应额度。确认成交且资金同步后触发；连续开平仓合并评估。等待新评估期间暂停开仓和加仓，既有止盈止损继续。激进仍遵守下面授权的边界，不保证收益。手动“评估并应用”不要求开启自动开关。</p>
    <details class="risk-authority"><summary>AI 可修改项目与范围（逐项授权）</summary><p class="fine">默认不授权。上下界单位与账户限额一致；调整这里不会立即改变账户限额。AI 不能修改杠杆、保证金模式、交易对授权或自己的权限。</p>
    ${Object.entries(schema.limits)
      .map(([k, spec]) => {
        const grant = ai.authority?.[k] || {
          minimum: policy.limits[k].value,
          maximum: policy.limits[k].value,
        };
        return `<div class="risk-authority-row" data-risk-authority="${k}"><label class="check"><input class="authority-enabled" type="checkbox" ${grant.allowed ? "checked" : ""}>允许修改 ${esc(spec[0])}</label><div class="form-grid"><label>最小值 / ${esc(spec[1])}<input class="authority-min" type="number" min="${spec[2]}" max="${spec[3]}" step="any" value="${grant.minimum}" required></label><label>最大值 / ${esc(spec[1])}<input class="authority-max" type="number" min="${spec[2]}" max="${spec[3]}" step="any" value="${grant.maximum}" required></label></div><label class="check"><input class="authority-switch" type="checkbox" ${grant.allow_disable ? "checked" : ""}>也允许 AI 开关此限制</label></div>`;
      })
      .join("")}</details>
    <p class="fine">先保存设置，再使用账户卡片的“评估并应用”。每次实际调整都会备份旧额度并说明原因，保留最近 100 次；未调整不重复提醒。</p></fieldset>`;
}

function bindRiskSettings() {
  const update = () => {
    document.querySelectorAll("[data-risk-limit]").forEach((r) => {
      r.querySelector(".risk-value").disabled =
        !r.querySelector(".risk-enabled").checked;
    });
    document.querySelectorAll("[data-risk-authority]").forEach((r) => {
      r.querySelectorAll(
        ".authority-min,.authority-max,.authority-switch",
      ).forEach(
        (i) => (i.disabled = !r.querySelector(".authority-enabled").checked),
      );
    });
  };
  document
    .querySelectorAll(".risk-enabled,.authority-enabled")
    .forEach((i) => (i.onchange = update));
  $("applyRiskPreset").onclick = () => {
    const p = state.risk_schema.presets[$("riskPreset").value];
    document.querySelectorAll("[data-risk-limit]").forEach((r) => {
      const c = p[r.dataset.riskLimit];
      r.querySelector(".risk-enabled").checked = c.enabled;
      r.querySelector(".risk-value").value = c.value;
    });
    update();
    toast("预设已填入，保存后生效；AI 授权范围保持原设置");
  };
  update();
}

function readRiskSettings(f) {
  const limits = {},
    authority = {};
  document.querySelectorAll("[data-risk-limit]").forEach(
    (r) =>
      (limits[r.dataset.riskLimit] = {
        enabled: r.querySelector(".risk-enabled").checked,
        value: Number(r.querySelector(".risk-value").value),
      }),
  );
  document.querySelectorAll("[data-risk-authority]").forEach((r) => {
    const v = {
      allowed: r.querySelector(".authority-enabled").checked,
      minimum: Number(r.querySelector(".authority-min").value),
      maximum: Number(r.querySelector(".authority-max").value),
      allow_disable: r.querySelector(".authority-switch").checked,
    };
    if (v.minimum > v.maximum) throw Error("AI 授权最小值不能大于最大值");
    authority[r.dataset.riskAuthority] = v;
  });
  return {
    risk_policy: { limits, default_add_count: Number(f.riskDefaultAdds.value) },
    risk_ai: {
      enabled: f.riskAIEnabled.checked,
      level: f.riskAILevel.value,
      min_interval: Number(f.riskAIInterval.value),
      validity_seconds: Number(f.riskAIValidity.value),
      equity_change_pct: Number(f.riskAIEquity.value),
      authority,
    },
  };
}

function allocationFields(config) {
  const c = { available_margin_pct: 95, ...config };
  return `<details class="pair-allocation"><summary>查看 / 调整此交易对资金分配</summary><p class="fine">这些是明确的分配参数，不叠加隐藏的等级折扣。可用资金分配控制本次保证金及手续费预算；其他项留空继承账户额度，填写后取更严格的一项。升级前的系统仓位折扣已转换为这里的数值。</p><div class="form-grid">${[
    ["available_margin_pct", "本次可用资金分配 / %", 0.01, 100],
    ["trade_loss_pct", "本次止损预算 / 权益 %", 0.001, 100],
    ["exposure_multiple", "总名义敞口 / 权益倍数", 0.01, 1000],
    ["group_multiple", "同类敞口 / 权益倍数", 0.01, 1000],
  ]
    .map(
      ([k, t, min, max]) =>
        `<label>${t}<input data-allocation="${k}" type="number" min="${min}" max="${max}" step="any" value="${c[k] ?? ""}" placeholder="继承账户额度" ${k === "available_margin_pct" ? "required" : ""}></label>`,
    )
    .join("")}</div></details>`;
}
function readAllocation(row) {
  return Object.fromEntries(
    [...row.querySelectorAll("[data-allocation]")].map((i) => [
      i.dataset.allocation,
      i.value === "" ? null : Number(i.value),
    ]),
  );
}

function accountRiskCard(a) {
  const r = a.risk_review || {},
    b = a.risk_budget || {};
  return `<div class="account-risk"><p class="fine"><b>AI 额度管理：${a.risk_ai?.enabled ? "自动" : "手动"}</b> · ${esc(r.message || "尚未评估")}${r.reviewed_at ? " · " + time(r.reviewed_at) : ""}</p><div class="risk-card-actions"><button class="secondary" data-risk-review="${a.id}">评估并应用</button><button class="text-button" data-risk-history="${a.id}">额度明细 / 调整记录</button></div><p class="fine">${b.valuation_valid === false ? "资金数据待核验；当前额度不会替代基础风控。" : "额度在账户设置中独立调整，开仓和加仓共用。"}</p></div>`;
}

function bindAccountRisk() {
  document.querySelectorAll("[data-risk-review]").forEach(
    (b) =>
      (b.onclick = () =>
        action(async () => {
          b.disabled = true;
          try {
            const r = await api(
              `/api/v2/accounts/${b.dataset.riskReview}/risk-review`,
              {},
            );
            toast(r.message);
            await refresh();
          } finally {
            b.disabled = false;
          }
        })),
  );
  document.querySelectorAll("[data-risk-history]").forEach(
    (b) =>
      (b.onclick = () =>
        action(async () => {
          const a = state.accounts.find((x) => x.id === b.dataset.riskHistory),
            history = await api(`/api/v2/accounts/${a.id}/risk-history`);
          const budget = a.risk_budget || {};
          const show = (v, unit) => (v.enabled ? `${v.value} ${unit}` : "关闭");
          modal(
            `${a.name} · 账户额度`,
            `<p class="fine">资金单位：${esc(a.settle)}。风控估值权益 ${money(budget.equity, a.settle)}，可用 ${money(budget.available, a.settle)}。交易所账户采用可验证的保守资金口径；名义敞口不等于保证金。明细按最近服务器快照估算；最终下单重新校验。</p><div class="risk-usage">${(budget.limits || []).map((r) => `<div><span>${esc(r.label)}</span><strong>${r.enabled ? esc(r.value + " " + state.risk_schema.limits[r.key][1]) : "关闭"}</strong><small>${r.enabled && r.used !== null ? `已用 ${money(r.used, a.settle)} / 额度 ${money(r.limit, a.settle)} · 剩余 ${money(r.remaining, a.settle)}` : ""}</small></div>`).join("") || esc(budget.message || "等待数据")}</div><h3>调整记录与旧额度备份</h3><p class="fine">保留最近 100 次实际修改。下载内容包含旧额度和新额度，可在账户设置中核对恢复；不包含密钥。</p><button type="button" id="downloadRiskHistory" class="secondary">导出额度备份</button>${
              history
                .map(
                  (r) =>
                    `<article class="risk-history"><b>${time(r.created)} · ${r.source === "ai" ? "AI" : "手动"}</b>${Object.entries(
                      r.changes,
                    )
                      .map(
                        ([k, v]) =>
                          `<p>${esc(state.risk_schema.limits[k][0])}：<del>${esc(show(v.before, state.risk_schema.limits[k][1]))}</del> → <b>${esc(show(v.after, state.risk_schema.limits[k][1]))}</b></p>`,
                      )
                      .join(
                        "",
                      )}<p>${esc(r.reason)}</p><button type="button" class="secondary" data-risk-restore="${r.id}">载入此版本的旧额度</button></article>`,
                )
                .join("") || '<p class="empty">尚无额度修改记录。</p>'
            }`,
          );
          document.querySelectorAll("[data-risk-restore]").forEach(
            (button) =>
              (button.onclick = () => {
                const saved = history.find(
                  (r) => r.id === Number(button.dataset.riskRestore),
                );
                if (!saved) return;
                editAccount({ ...a, risk_policy: saved.before_policy });
                toast(
                  "旧额度已填入，请核对后保存；AI 授权和交易对配置保持当前设置",
                );
              }),
          );
          $("downloadRiskHistory").onclick = () => {
            const url = URL.createObjectURL(
              new Blob(
                [
                  JSON.stringify(
                    {
                      schema: 1,
                      account: a.name,
                      settlement: a.settle,
                      current: a.risk_policy,
                      history,
                    },
                    null,
                    2,
                  ),
                ],
                { type: "application/json" },
              ),
            );
            const link = document.createElement("a");
            link.href = url;
            link.download = "quantpilot-risk-backup.json";
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          };
        })),
  );
}
