"use strict";
const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("mkbot-token") || "";
let revision = 0;
let modelsRevision = 0;
let currentView = "overview";
const titles = {
  overview: "运行概览",
  activities: "活动与计划",
  inbox: "留言收件箱",
  persona: "人格配置",
  models: "模型与路由",
  plugins: "能力插件",
  usage: "资源额度",
  audit: "运行记录",
};
const labels = {
  awake: "清醒",
  winding_down: "正在收尾",
  sleeping: "睡眠",
  waking: "正在醒来",
  pending: "待审阅",
  deferred: "稍后处理",
  reviewed: "已审阅",
  ignored: "已忽略",
  running: "执行中",
  unknown: "待核对",
  queued: "待安排",
  completed: "已完成",
  cancelled: "已取消",
  failed: "失败",
  paused: "已暂停",
  waiting: "等待中",
  interrupted: "执行中断",
};
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const when = (value) =>
  value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
function notice(text, error = false) {
  $("notice").hidden = false;
  $("notice").className = error ? "error" : "";
  $("notice").textContent = text;
}
async function api(path, method = "GET", body) {
  const response = await fetch(`/api/${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok)
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : JSON.stringify(result.detail),
    );
  return result;
}
async function action(callback) {
  try {
    await callback();
  } catch (error) {
    notice(error.message, true);
  }
}
function empty(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}
async function overview() {
  const state = await api("status");
  $("phase-badge").textContent = state.paused
    ? "运行已暂停"
    : labels[state.phase];
  document.querySelectorAll("[data-action]").forEach((button) => {
    const allowed = {
      wake: !state.paused && ["sleeping", "winding_down"].includes(state.phase),
      awake: !state.paused && state.phase === "waking",
      pause: !state.paused,
      resume: state.paused,
    };
    button.disabled = !allowed[button.dataset.action];
  });
  const copy = {
    awake: [
      "清醒着，等待新的故事",
      "她可以查看留言，继续计划，也可以安静地等一会儿。",
    ],
    winding_down: [
      "把手头的事情做完",
      "正在等待当前工作收尾，随后按保存的计划入睡。",
    ],
    sleeping: ["正在休息", "留言会妥善保存，醒来之后再决定要回应哪些内容。"],
    waking: [
      "慢慢醒来，看看近况",
      "先看看最近的留言和未完成事项，再安排接下来的活动。",
    ],
  };
  $("phase-title").textContent = state.paused
    ? "运行已暂停"
    : copy[state.phase][0];
  $("phase-description").textContent = state.paused
    ? "角色状态已经保留，可以在准备好后恢复。"
    : copy[state.phase][1];
  $("pending-count").textContent = state.inbox_counts
    .filter((x) => ["pending", "deferred"].includes(x.status))
    .reduce((a, x) => a + x.count, 0);
  $("running-count").textContent =
    state.activity_counts.find((x) => x.status === "running")?.count || 0;
  $("work-count").textContent = state.work.length;
  $("wake-description").textContent = state.wake_at
    ? `计划醒来：${when(state.wake_at)}`
    : "目前没有唤醒计划";
  $("work-list").innerHTML = state.work.length
    ? state.work
        .map(
          (x) =>
            `<div class="row-card"><div class="row-top"><strong>${escapeHtml(x.kind)}</strong><span class="badge">${labels[x.status]}</span></div><div class="row-meta">${when(x.started_at)}</div><p>${escapeHtml(x.detail || "正在执行")}</p></div>`,
        )
        .join("")
    : empty("所有工作都已妥善收尾");
}
async function activities() {
  const rows = await api("activities");
  $("activity-list").innerHTML = rows.length
    ? rows
        .map(
          (x) =>
            `<article class="row-card"><div class="row-top"><strong>${escapeHtml(x.goal)}</strong><span class="badge">${escapeHtml(labels[x.status] || x.status)}</span></div><div class="row-meta">${escapeHtml(x.kind)} · ${when(x.created_at)} · ${x.steps} 步</div>${x.checkpoint.agent?.result?.text ? `<p>${escapeHtml(x.checkpoint.agent.result.text)}</p>` : ""}${x.checkpoint.agent?.stopped_reason ? `<p class="muted">${escapeHtml(x.checkpoint.agent.stopped_reason)}</p>` : ""}<div class="button-row">${["queued", "paused", "waiting", "interrupted", "running"].includes(x.status) ? `<button data-run="${x.id}" class="primary">运行这一轮</button>` : ""}${["queued", "paused", "waiting", "interrupted"].includes(x.status) ? `<button data-claim="${x.id}">手动接取</button>` : ""}${x.status === "running" ? `<button data-transition="${x.id}" data-status="completed">标记完成</button><button data-transition="${x.id}" data-status="paused">保存并暂停</button>` : ""}${!["completed", "cancelled", "failed"].includes(x.status) ? `<button data-transition="${x.id}" data-status="cancelled">取消</button>` : ""}</div></article>`,
        )
        .join("")
    : empty("还没有计划，可以先记下想做的事情");
}
async function inbox() {
  const rows = await api("inbox");
  $("inbox-list").innerHTML = rows.length
    ? rows
        .map(
          (x) =>
            `<article class="row-card"><div class="row-top"><strong>${escapeHtml(x.conversation_id)}</strong><span class="badge">${escapeHtml(labels[x.status] || x.status)}</span></div><p>${escapeHtml(x.payload.text)}</p><div class="row-meta">${escapeHtml(x.sender_id)} · ${when(x.received_at)}${x.reason ? ` · ${escapeHtml(x.reason)}` : ""}</div>${["pending", "deferred"].includes(x.status) ? `<div class="button-row"><button data-review="${x.id}">看过了，无需回复</button></div>` : ""}</article>`,
        )
        .join("")
    : empty("收件箱很安静");
}
async function persona() {
  const item = await api("config/persona");
  revision = item.revision;
  $("config-revision").textContent = `版本 ${revision}`;
  $("persona-json").value = JSON.stringify(item.content, null, 2);
}
async function usage() {
  const rows = await api("usage");
  $("usage-list").innerHTML = rows.length
    ? `<table><thead><tr><th>范围</th><th>计量项</th><th>上限</th><th>已结算</th><th>预留</th></tr></thead><tbody>${rows.map((x) => `<tr><td>${escapeHtml(x.scope)}</td><td>${escapeHtml(x.metric)}</td><td>${x.limit_value}</td><td>${x.settled}</td><td>${x.reserved}</td></tr>`).join("")}</tbody></table>`
    : empty("尚未配置资源额度");
}
async function audit() {
  const rows = await api("audit");
  $("audit-list").innerHTML =
    rows
      .map(
        (x) =>
          `<div class="log-row"><time>${when(x.occurred_at)}</time><div>${escapeHtml(x.event_type)}<p>${escapeHtml(JSON.stringify(x.data))}</p></div></div>`,
      )
      .join("") || empty("暂无运行记录");
}
async function models() {
  const [config, calls] = await Promise.all([
    api("config/models"),
    api("model-calls"),
  ]);
  modelsRevision = config.revision;
  $("models-revision").textContent = `版本 ${modelsRevision}`;
  $("models-json").value = JSON.stringify(config.content, null, 2);
  $("model-call-list").innerHTML = calls.length
    ? calls
        .map(
          (call) =>
            `<div class="row-card"><div class="row-top"><strong>${escapeHtml(call.model)}</strong><span class="badge">${escapeHtml(labels[call.status] || call.status)}</span></div><p>${escapeHtml(call.provider)} · ${escapeHtml(call.role)} · 第 ${call.attempt} 次 · ${call.elapsed_ms ?? "—"} ms</p><div class="row-meta">${when(call.started_at)} · ${call.usage ? `输入 ${call.usage.input_tokens} / 输出 ${call.usage.output_tokens} token` : "用量未知，保留预留"}${call.error_kind ? ` · ${escapeHtml(call.error_kind)}` : ""}</div></div>`,
        )
        .join("")
    : empty("还没有模型调用记录");
}
async function plugins() {
  const rows = await api("plugins");
  $("plugin-list").innerHTML = rows.length
    ? rows
        .map(
          (item) =>
            `<article class="row-card"><div class="row-top"><strong>${escapeHtml(item.name)}</strong><span class="badge">${escapeHtml(item.version)}</span></div><p>${escapeHtml(item.description)}</p><p class="muted">${escapeHtml(item.instructions)}</p><div class="row-meta">${escapeHtml(item.tools.join(" · "))}</div></article>`,
        )
        .join("")
    : empty("还没有加载能力插件");
}
const loaders = {
  overview,
  activities,
  inbox,
  persona,
  models,
  plugins,
  usage,
  audit,
};
async function loadView() {
  await loaders[currentView]();
  $("updated").textContent =
    `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
}
async function enter() {
  await api("status");
  sessionStorage.setItem("mkbot-token", token);
  $("login").hidden = true;
  $("workspace").hidden = false;
  await loadView();
}
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  token = $("token").value.trim();
  try {
    await enter();
    $("token").value = "";
  } catch (error) {
    $("login-error").textContent = error.message;
  }
});
$("logout").addEventListener("click", () => {
  sessionStorage.removeItem("mkbot-token");
  token = "";
  location.reload();
});
document.querySelectorAll("nav button").forEach((button) =>
  button.addEventListener("click", () =>
    action(async () => {
      currentView = button.dataset.view;
      document
        .querySelectorAll(".view")
        .forEach((v) => (v.hidden = v.id !== currentView));
      document
        .querySelectorAll("nav button")
        .forEach((b) => b.classList.toggle("selected", b === button));
      $("page-title").textContent = titles[currentView];
      $("notice").hidden = true;
      await loadView();
    }),
  ),
);
$("refresh").addEventListener("click", () => action(loadView));
document.querySelectorAll("[data-action]").forEach((button) =>
  button.addEventListener("click", () =>
    action(async () => {
      await api(`lifecycle/${button.dataset.action}`, "POST");
      await overview();
      notice("状态已更新");
    }),
  ),
);
$("sleep-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    const wake = new Date($("wake-time").value).toISOString();
    await api("lifecycle/sleep", "POST", {
      earliest_wake_at: wake,
      latest_wake_at: wake,
      reason: "管理面板设置的休息计划",
    });
    await overview();
    notice("睡眠计划已保存");
  });
});
$("activity-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    await api("activities", "POST", {
      kind: "plan",
      goal: $("goal").value,
      origin: "operator",
    });
    $("goal").value = "";
    await activities();
    notice("计划已保存");
  });
});
$("activity-list").addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  action(async () => {
    button.disabled = true;
    try {
      if (button.dataset.run) {
        notice("正在执行这一轮活动…");
        const result = await api(
          `activities/${button.dataset.run}/run`,
          "POST",
        );
        notice(result.reason || "这一轮活动已完成");
      } else if (button.dataset.claim)
        await api(`activities/${button.dataset.claim}/claim`, "POST");
      else
        await api(
          `activities/${button.dataset.transition}/transition`,
          "POST",
          { status: button.dataset.status },
        );
      await activities();
    } finally {
      button.disabled = false;
    }
  });
});
$("message-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    await api("inbox", "POST", {
      source: "local-panel",
      external_id: crypto.randomUUID(),
      conversation_id: $("conversation").value,
      sender_id: "local-friend",
      text: $("message").value,
      occurred_at: new Date().toISOString(),
    });
    $("message").value = "";
    await inbox();
    notice("留言已保存");
  });
});
$("inbox-list").addEventListener("click", (e) => {
  const button = e.target.closest("[data-review]");
  if (button)
    action(async () => {
      await api("inbox/review", "POST", {
        event_ids: [button.dataset.review],
        decision: "no_reply",
        reason: "操作者已查看，无需回复",
      });
      await inbox();
    });
});
$("persona-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    const content = JSON.parse($("persona-json").value);
    const saved = await api("config/persona", "PUT", {
      expected_revision: revision,
      content,
    });
    revision = saved.revision;
    $("config-revision").textContent = `版本 ${revision}`;
    notice("新版本已保存");
  });
});
$("reload-persona").addEventListener("click", () => action(persona));
$("models-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    const content = JSON.parse($("models-json").value);
    const saved = await api("config/models", "PUT", {
      expected_revision: modelsRevision,
      content,
    });
    modelsRevision = saved.revision;
    $("models-revision").textContent = `版本 ${modelsRevision}`;
    notice("模型配置已保存，下一次新请求生效");
  });
});
$("reload-models").addEventListener("click", () => action(models));
if (token)
  enter().catch(() => {
    token = "";
    sessionStorage.removeItem("mkbot-token");
  });
