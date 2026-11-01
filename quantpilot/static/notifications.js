"use strict";
const notificationCategories = {
  open: "开单",
  take_profit: "止盈",
  stop_loss: "止损",
  add: "加仓",
  reduce: "减仓 / 平仓",
  opportunity: "有效机会",
  ai: "AI 介入干预",
};
let notificationDirty = false,
  notificationCursor = null,
  notificationBusy = false,
  soundContext = null;
$("notificationChannels").innerHTML =
  "<b>提醒类型</b><b>Telegram</b><b>网页声音</b>" +
  Object.entries(notificationCategories)
    .map(
      ([key, label]) =>
        `<span>${label}</span><input type="checkbox" name="telegram_${key}" aria-label="Telegram ${label}"><input type="checkbox" name="sound_${key}" aria-label="网页声音 ${label}">`,
    )
    .join("");
$("notificationForm").oninput = () => (notificationDirty = true);
function renderNotifications() {
  const n = state.notifications;
  if (!n) return;
  if (!notificationDirty) {
    const f = $("notificationForm");
    f.telegram_enabled.checked = n.settings.telegram_enabled;
    f.sound_enabled.checked = n.settings.sound_enabled;
    for (const k in notificationCategories) {
      f.elements.namedItem("telegram_" + k).checked = n.settings.telegram[k];
      f.elements.namedItem("sound_" + k).checked = n.settings.sound[k];
    }
  }
  $("telegramState").textContent = n.configured
    ? `已加密保存 · Chat ID ${n.chat_id} · 最近未确认/失败 ${n.failed_count} 条`
    : "尚未配置 Telegram";
  $("soundState").textContent =
    soundContext?.state === "running"
      ? "此浏览器已解锁声音"
      : "请点击「启用 / 测试声音」；手机锁屏或关闭页面后，声音可能无法播放。";
  if (notificationCursor === null) notificationCursor = n.cursor;
}
function playNotice() {
  if (!soundContext || soundContext.state !== "running") return;
  const t = soundContext.currentTime;
  [660, 880].forEach((hz, i) => {
    const osc = soundContext.createOscillator(),
      gain = soundContext.createGain();
    osc.frequency.value = hz;
    gain.gain.setValueAtTime(0, t + i * 0.18);
    gain.gain.linearRampToValueAtTime(0.12, t + i * 0.18 + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.001, t + i * 0.18 + 0.15);
    osc.connect(gain);
    gain.connect(soundContext.destination);
    osc.start(t + i * 0.18);
    osc.stop(t + i * 0.18 + 0.16);
  });
}
$("soundTest").onclick = () =>
  action(async () => {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) throw Error("此浏览器不支持网页声音");
    soundContext ||= new Audio();
    await soundContext.resume();
    playNotice();
    renderNotifications();
  });
$("notificationForm").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const f = e.currentTarget,
      body = {
        telegram_enabled: f.telegram_enabled.checked,
        sound_enabled: f.sound_enabled.checked,
        telegram: {},
        sound: {},
      };
    for (const k in notificationCategories) {
      body.telegram[k] = f.elements.namedItem("telegram_" + k).checked;
      body.sound[k] = f.elements.namedItem("sound_" + k).checked;
    }
    await api("/api/v2/notifications", body, "PUT");
    notificationDirty = false;
    await refresh();
    toast("提醒设置已保存");
  });
};
$("telegramForm").onsubmit = (e) => {
  e.preventDefault();
  action(async () => {
    const f = e.currentTarget;
    await api(
      "/api/v2/notifications/telegram",
      { token: f.token.value.trim(), chat_id: f.chat_id.value.trim() },
      "PUT",
    );
    f.token.value = "";
    await refresh();
    toast("已加密保存；请测试连接，再启用 Telegram 提醒");
  });
};
$("telegramDelete").onclick = () =>
  action(async () => {
    await api("/api/v2/notifications/telegram", {}, "DELETE");
    notificationDirty = false;
    $("telegramForm").reset();
    await refresh();
    toast("Telegram 配置已删除，发送已停用");
  });
$("telegramTest").onclick = () =>
  action(async () => {
    await api("/api/v2/notifications/test", {});
    toast("测试消息已发送");
  });
async function pollNotifications() {
  if (
    !state ||
    notificationCursor === null ||
    notificationBusy ||
    $("app").hidden
  )
    return;
  notificationBusy = true;
  try {
    const n = await api("/api/v2/notifications?after=" + notificationCursor);
    notificationCursor = n.cursor;
    const fresh = n.events.filter((e) => Date.now() / 1000 - e.created < 60);
    if (
      n.settings.sound_enabled &&
      fresh.some((e) => n.settings.sound[e.category])
    )
      playNotice();
    if (fresh.length)
      toast(fresh.at(-1).message.split("\n").slice(0, 3).join(" · "));
  } catch {
  } finally {
    notificationBusy = false;
  }
}
setInterval(pollNotifications, 3000);
