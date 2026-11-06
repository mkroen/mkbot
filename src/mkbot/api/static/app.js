"use strict";
const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("mkbot-token") || "";
let revision = 0;
let modelsRevision = 0;
let currentView = "overview";
let memoryOffset = 0;
let memoryRevision = 0;
let editingMemory = null;
let forgettingMemory = null;
const titles = {
  overview: "运行概览",
  activities: "活动与计划",
  inbox: "留言收件箱",
  persona: "人格配置",
  memory: "记忆与画像",
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
  active: "有效记录",
  superseded: "已更正",
  forgotten: "已遗忘",
  recent: "近期",
  long: "长期",
  archived: "已归档",
  fact: "事实陈述",
  preference: "偏好",
  boundary: "边界",
  current_state: "临时近况",
  experience: "经历",
  opinion: "观点",
  inference: "推断",
  self_report: "本人陈述",
  third_party: "第三方陈述",
  operator: "人工来源",
  inferred: "推断",
  tool_report: "工具回执",
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
const splitIds = (value) =>
  value
    .split(/[,，\n]/)
    .map((x) => x.trim())
    .filter(Boolean);
const scopeText = (scope) =>
  scope
    .map((x) => (x === "*" ? "所有会话" : x === "@self" ? "角色内部" : x))
    .join("、");
function memoryCard(item, editable = true) {
  const expired = item.valid_until && new Date(item.valid_until) <= new Date();
  return `<article class="row-card memory-card">
    <div class="row-top"><strong>${escapeHtml(item.subjects.join(" · ") || "已清理人物信息")}<span class="type-label">${escapeHtml(labels[item.kind])}</span></strong><span class="badge">${escapeHtml(labels[item.tier] || "长期")}${item.bucket === "protected" ? " · 保护中" : ""}${expired ? " · 已过期" : ""}</span></div>
    <p>${escapeHtml(item.content || "内容已遗忘")}</p>
    <div class="row-meta">${escapeHtml(labels[item.confidence])} · 版本 ${item.revision}${item.current_activation !== undefined ? ` · 当前活跃度 ${Number(item.current_activation).toFixed(2)}` : ""} · ${when(item.occurred_at)}</div>
    <details><summary>范围与来源</summary><dl class="memory-details">
      ${item.use_scope ? `<dt>可用于</dt><dd>${escapeHtml(scopeText(item.use_scope))}</dd><dt>可表达给</dt><dd>${escapeHtml(scopeText(item.disclosure_scope))}</dd>` : ""}
      <dt>有效至</dt><dd>${item.valid_until ? when(item.valid_until) : "未设截止时间"}</dd>
      <dt>根来源</dt><dd>${item.source_refs.map(escapeHtml).join("<br>")}</dd>
      <dt>记录编号</dt><dd>${escapeHtml(item.id)}</dd></dl></details>
    ${editable ? `<div class="button-row">${item.status === "active" ? `<button data-memory-edit="${item.id}">编辑</button><button data-memory-forget="${item.id}" class="danger-text">遗忘</button>` : `<span class="badge">${escapeHtml(labels[item.status])}</span>`}<button data-memory-history="${item.id}">修订记录</button></div>` : ""}
  </article>`;
}
async function memoryList() {
  const params = new URLSearchParams({
    limit: "20",
    offset: String(memoryOffset),
  });
  for (const [key, id] of [
    ["tier", "memory-tier"],
    ["status", "memory-status"],
    ["subject", "memory-subject"],
  ]) {
    if ($(id).value.trim()) params.set(key, $(id).value.trim());
  }
  const rows = await api(`memory?${params}`);
  $("memory-list").innerHTML =
    rows.map((item) => memoryCard(item)).join("") ||
    empty("这个范围还没有记忆");
  $("memory-page").textContent =
    `第 ${memoryOffset / 20 + 1} 页 · ${rows.length} 条`;
  $("memory-prev").disabled = memoryOffset === 0;
  $("memory-next").disabled = rows.length < 20;
}
async function memory() {
  await memoryList();
  const config = await api("config/memory");
  memoryRevision = config.revision;
  $("memory-settings-json").value = JSON.stringify(config.content, null, 2);
  $("memory-settings-version").textContent = `版本 ${memoryRevision}`;
}
function localDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}
function memoryEditor(item = null) {
  editingMemory = item;
  $("memory-editor").hidden = false;
  $("memory-editor-title").textContent = item ? "修改记忆" : "录入记忆";
  $("memory-editor-version").textContent = item
    ? `基于版本 ${item.revision}`
    : "新建人工来源";
  $("memory-kind").value = item?.kind || "preference";
  $("memory-subjects").value = item?.subjects.join(", ") || "";
  $("memory-kind").disabled = $("memory-subjects").disabled = Boolean(item);
  $("memory-edit-tier").value = item?.tier === "long" ? "long" : "recent";
  $("memory-importance").value = item?.importance ?? 0.5;
  $("memory-content").value = item?.content || "";
  $("memory-use").value = item?.use_scope.join(", ") || "@self";
  $("memory-disclosure").value = item?.disclosure_scope.join(", ") || "";
  $("memory-expiry").value = localDate(item?.valid_until);
  $("memory-expiry").required = item?.kind === "current_state";
  $("memory-disclosure-field").hidden = $("memory-sharing-label").hidden =
    !item;
  $("memory-disclosure").required = Boolean(item);
  $("memory-use-label").textContent = item
    ? "可以用于哪些会话"
    : "可用与表达范围";
  $("memory-sharing").checked = false;
  $("memory-editor").scrollIntoView({ behavior: "smooth", block: "start" });
}
const loaders = {
  overview,
  activities,
  inbox,
  persona,
  memory,
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
$("memory-filter").addEventListener("submit", (e) => {
  e.preventDefault();
  memoryOffset = 0;
  action(memoryList);
});
$("memory-prev").addEventListener("click", () => {
  memoryOffset = Math.max(0, memoryOffset - 20);
  action(memoryList);
});
$("memory-next").addEventListener("click", () => {
  memoryOffset += 20;
  action(memoryList);
});
$("memory-new").addEventListener("click", () => memoryEditor());
$("memory-editor-close").addEventListener("click", () => {
  $("memory-editor").hidden = true;
});
$("memory-history-close").addEventListener("click", () => {
  $("memory-history").hidden = true;
});
$("memory-kind").addEventListener("change", () => {
  $("memory-expiry").required = $("memory-kind").value === "current_state";
});
$("memory-edit-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    const content = {
      content: $("memory-content").value,
      tier: $("memory-edit-tier").value,
      importance: Number($("memory-importance").value),
      valid_until: $("memory-expiry").value
        ? new Date($("memory-expiry").value).toISOString()
        : null,
    };
    if (editingMemory) {
      await api(`memory/${editingMemory.id}`, "PUT", {
        ...content,
        expected_revision: editingMemory.revision,
        use_scope: splitIds($("memory-use").value),
        disclosure_scope: splitIds($("memory-disclosure").value),
        allow_wider_sharing: $("memory-sharing").checked,
      });
    } else {
      await api("memory", "POST", {
        ...content,
        kind: $("memory-kind").value,
        subjects: splitIds($("memory-subjects").value),
        scope: splitIds($("memory-use").value),
      });
    }
    $("memory-editor").hidden = true;
    await memoryList();
    $("memory-preview-result").replaceChildren();
    notice("记忆已保存；引用旧版本的活动需要重新构建上下文");
  });
});
$("memory-list").addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  action(async () => {
    if (button.dataset.memoryEdit)
      memoryEditor(await api(`memory/${button.dataset.memoryEdit}`));
    if (button.dataset.memoryHistory) {
      const rows = await api(`memory/${button.dataset.memoryHistory}/history`);
      $("memory-history-content").innerHTML = rows
        .map((row) => memoryCard(row, false))
        .join("");
      $("memory-history").hidden = false;
      $("memory-history").scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
    if (button.dataset.memoryForget) {
      forgettingMemory = await api(
        `memory/${button.dataset.memoryForget}/forget-preview`,
      );
      $("memory-forget-impact").textContent =
        `将遗忘 ${forgettingMemory.affected_ids.length} 条记忆，阻止从 ${forgettingMemory.source_count} 个旧来源重新提炼。`;
      $("memory-forget-dialog").showModal();
    }
  });
});
$("memory-forget-cancel").addEventListener("click", () => {
  $("memory-forget-dialog").close();
  forgettingMemory = null;
});
$("memory-forget-confirm").addEventListener("click", () =>
  action(async () => {
    if (!forgettingMemory) return;
    const result = await api(`memory/${forgettingMemory.id}/forget`, "POST", {
      expected_revision: forgettingMemory.expected_revision,
    });
    $("memory-forget-dialog").close();
    forgettingMemory = null;
    $("memory-editor").hidden = $("memory-history").hidden = true;
    $("memory-preview-result").replaceChildren();
    $("memory-history-content").replaceChildren();
    $("memory-content").value = "";
    await memoryList();
    notice(`已遗忘 ${result.affected_ids.length} 条相关记忆，原始聊天日志保留`);
  }),
);
$("memory-preview-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    const audience = $("memory-audience").value.trim();
    const subject = $("memory-preview-subject").value.trim();
    let rows;
    let caption = "";
    if ($("memory-preview-mode").value === "profile") {
      if (!subject) throw new Error("查看画像需要填写人物 ID");
      const profile = await api("memory/profile", "POST", {
        subject,
        audience,
      });
      rows = profile.items;
      caption = `<p class="muted">当前范围内有 ${rows.length} 条长期记录，来自 ${profile.independent_sources} 个独立根来源。</p>`;
    } else {
      rows = await api("memory/search", "POST", {
        audience,
        query: $("memory-query").value,
        subjects: subject ? [subject] : null,
      });
    }
    $("memory-preview-result").innerHTML =
      caption +
      (rows.map((row) => memoryCard(row, false)).join("") ||
        empty("此受众范围内没有符合条件的记忆"));
  });
});
$("memory-settings-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    const config = await api("config/memory", "PUT", {
      expected_revision: memoryRevision,
      content: JSON.parse($("memory-settings-json").value),
    });
    memoryRevision = config.revision;
    $("memory-settings-version").textContent = `版本 ${memoryRevision}`;
    notice("记忆规则已保存");
  });
});
if (token)
  enter().catch(() => {
    token = "";
    sessionStorage.removeItem("mkbot-token");
  });
