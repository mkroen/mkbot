"use strict";
const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("mkbot-token") || "";
let revision = 0;
let modelsRevision = 0;
let currentView = "overview";
let schedulerRevision = 0;
let schedulerContent = {};
let personaContent = {};
let statisticsHours = 24;
let memoryOffset = 0;
let memoryRevision = 0;
let editingMemory = null;
let forgettingMemory = null;
let editingPlan = null;
let peopleOffset = 0;
let selectedPerson = null;
let editingPerson = null;
let personRequest = 0;
let relationshipRevision = 0;
let relationshipContent = {};
const titles = {
  overview: "运行概览",
  scheduler: "调度与作息",
  activities: "运行活动",
  plans: "目标与计划",
  inbox: "留言收件箱",
  persona: "人格配置",
  memory: "记忆与画像",
  people: "人物关系",
  models: "模型与路由",
  plugins: "能力插件",
  usage: "资源额度",
  audit: "运行记录",
};
const labels = {
  ready: "待执行",
  yielded: "已让出前台",
  held: "需要处理",
  done: "已结束",
  chat: "会话留言",
  idle: "自主活动",
  plan: "计划",
  succeeded: "成功",
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
const descriptions = {
  overview: "照看角色的日常，也看清每一次行动。",
  activities: "查看每轮执行、暂停原因和实际结果。",
  plans: "记下想做的事，把每一次进展留给之后的自己。",
  inbox: "按会话查看留言与审阅结果，无需逐条回应。",
  persona: "定义角色是谁、如何表达，以及对什么感兴趣。",
  memory: "管理相处中的记忆、来源和分享边界。",
  people: "看看认识的人、相处的变化，以及彼此的边界。",
  plugins: "通过能力描述和工具，让角色学会新的技能。",
  scheduler: "安排清醒时的活动，留出等待和休息的时间。",
  models: "统一管理主会话与能力调用的模型、价格和备用路由。",
  usage: "查看已结算用量与预留额度，未知用量会继续占用预留。",
  audit: "从持久记录中追踪状态变化、模型请求和工具行动。",
};
const iconPaths = {
  home: '<path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  chat: '<path d="M4 4h16v12H9l-5 4V4Z"/><path d="M8 8h8M8 12h5"/>',
  person: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  people:
    '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v3"/>',
  memory:
    '<path d="M8 3h8l5 5v8l-5 5H8l-5-5V8Z"/><circle cx="12" cy="12" r="3"/><path d="M12 3v6m0 6v6M3 12h6m6 0h6"/>',
  cube: '<path d="m12 3 9 5v9l-9 5-9-5V8l9-5Zm-9 5 9 5 9-5m-9 5v9M7 5l10 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6l4 2"/>',
  sliders:
    '<path d="M3 6h7m4 0h7M3 12h12m4 0h2M3 18h3m4 0h11M10 3v6m5 0v6M6 15v6"/>',
  chart: '<path d="M4 3v17h17M8 15v-4m5 4V6m5 9V9"/>',
  file: '<path d="M5 3h9l5 5v13H5Z"/><path d="M14 3v6h5M9 13h6m-6 4h6"/>',
  moon: '<path d="M20 14A9 9 0 0 1 10 3a9 9 0 1 0 10 11Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  refresh:
    '<path d="M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 14-2l1 3M4 15l1 3a8 8 0 0 0 14-2"/>',
  wallet: '<path d="M3 5h16v15H3V5Zm0 0 13-2v2M15 10h6v6h-6Z"/>',
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name] || iconPaths.file}</svg>`;
document.querySelectorAll("[data-icon]").forEach((element) => {
  element.innerHTML = icon(element.dataset.icon);
});
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("mkbot-theme", theme);
  $("theme-toggle").innerHTML = icon(theme === "dark" ? "sun" : "moon");
  $("theme-toggle").setAttribute(
    "aria-label",
    theme === "dark" ? "切换到浅色主题" : "切换到深色主题",
  );
}
function setCollapsed(collapsed) {
  $("workspace").classList.toggle("sidebar-collapsed", collapsed);
  $("sidebar").inert =
    collapsed && window.matchMedia("(max-width: 700px)").matches;
  $("toggle-sidebar").setAttribute("aria-expanded", String(!collapsed));
  $("toggle-sidebar").setAttribute(
    "aria-label",
    collapsed ? "展开导航" : "收起导航",
  );
}
setTheme(localStorage.getItem("mkbot-theme") === "dark" ? "dark" : "light");
setCollapsed(window.matchMedia("(max-width: 700px)").matches);
window.matchMedia("(max-width: 700px)").addEventListener("change", (event) => {
  setCollapsed(event.matches);
});
$("theme-toggle").addEventListener("click", () =>
  setTheme(
    document.documentElement.dataset.theme === "dark" ? "light" : "dark",
  ),
);
$("toggle-sidebar").addEventListener("click", () =>
  setCollapsed(!$("workspace").classList.contains("sidebar-collapsed")),
);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && window.matchMedia("(max-width: 700px)").matches)
    setCollapsed(true);
});
const shortNumber = (value) =>
  new Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
const money = (value) =>
  "$" +
  (Number(value) / 1_000_000).toLocaleString("en", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  });
function renderChart(id, points, kind) {
  const total = points.reduce(
    (sum, point) =>
      sum +
      (kind === "calls"
        ? point.calls
        : point.input_tokens + point.output_tokens),
    0,
  );
  const values = points.map((point) =>
    kind === "calls" ? point.calls : point.input_tokens + point.output_tokens,
  );
  const top = Math.max(1, ...values);
  const w = 530,
    h = 180,
    left = 38,
    right = 12,
    bottom = 27,
    yTop = 10;
  const plotH = h - bottom - yTop,
    plotW = w - left - right;
  const x = (index) => left + (index * plotW) / Math.max(1, points.length - 1);
  const y = (value) => yTop + plotH * (1 - value / top);
  let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${kind === "calls" ? "请求" : "Token"}趋势，总计 ${total}">`;
  for (let i = 0; i < 4; i++) {
    const value = (top * i) / 3;
    svg += `<line class="gridline" x1="${left}" x2="${w - right}" y1="${y(value)}" y2="${y(value)}"/><text x="${left - 8}" y="${y(value) + 3}" text-anchor="end">${shortNumber(value)}</text>`;
  }
  if (total && kind === "calls") {
    const path = values
      .map((value, index) => `${index ? "L" : "M"}${x(index)},${y(value)}`)
      .join(" ");
    svg += `<path class="trend-fill" d="${path} L${x(points.length - 1)},${y(0)} L${left},${y(0)} Z"/><path class="trend-line" d="${path}"/>`;
  } else if (total) {
    const width = plotW / points.length;
    points.forEach((point, index) => {
      const xpos = left + width * index + width * 0.2;
      const inputH = (plotH * point.input_tokens) / top,
        outputH = (plotH * point.output_tokens) / top;
      svg += `<rect class="bar-input" x="${xpos}" y="${y(0) - inputH}" width="${width * 0.6}" height="${inputH}"/><rect class="bar-output" x="${xpos}" y="${y(0) - inputH - outputH}" width="${width * 0.6}" height="${outputH}"/>`;
    });
  }
  for (const index of [
    ...new Set([
      0,
      Math.floor(points.length / 4),
      Math.floor(points.length / 2),
      Math.floor(points.length * 0.75),
      points.length - 1,
    ]),
  ]) {
    const date = new Date(points[index].at);
    const label =
      statisticsHours === 24
        ? date.toLocaleTimeString("zh-CN", {
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          })
        : `${date.getMonth() + 1}/${date.getDate()}`;
    svg += `<text x="${x(index)}" y="${h - 4}" text-anchor="${index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"}">${label}</text>`;
  }
  $(id).innerHTML =
    svg +
    "</svg>" +
    (total
      ? ""
      : `<div class="chart-empty"><span>${kind === "calls" ? "此时段暂无模型请求" : "此时段暂无已报告的 Token 用量"}</span></div>`);
}
async function statistics() {
  const data = await api(`statistics?hours=${statisticsHours}`);
  const summary = data.summary;
  $("stat-calls").textContent = summary.calls.toLocaleString("zh-CN");
  $("stat-result").textContent =
    `成功 ${summary.succeeded} · 失败 ${summary.failed} · 执行中 ${summary.running}`;
  $("stat-tokens").textContent = shortNumber(
    summary.input_tokens + summary.output_tokens,
  );
  $("stat-token-detail").textContent =
    `输入 ${shortNumber(summary.input_tokens)} / 输出 ${shortNumber(summary.output_tokens)}`;
  $("stat-cost").textContent = money(summary.settled_micro_usd);
  $("stat-reserved").textContent =
    `另有 ${money(summary.reserved_micro_usd)} 预留待结算`;
  $("stat-latency").textContent =
    summary.average_ms === null
      ? "—"
      : `${(summary.average_ms / 1000).toFixed(2)} s`;
  $("stat-unknown").textContent = summary.unknown_usage
    ? `${summary.unknown_usage} 次请求用量未知`
    : "成功请求的完整耗时";
  renderChart("request-chart", data.trend, "calls");
  renderChart("token-chart", data.trend, "tokens");
  document.querySelectorAll("[data-hours]").forEach((button) => {
    const selected = Number(button.dataset.hours) === statisticsHours;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}
function lifecycleView(state) {
  $("phase-badge").textContent = state.paused
    ? "运行已暂停"
    : labels[state.phase];
  $("instance-state").textContent =
    `本地实例 · ${state.paused ? "已暂停" : labels[state.phase]}`;
  $("app-version").textContent = `mkbot · ${state.app_version || "alpha"}`;
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
      "清醒着，看看近况",
      "可以查看留言、继续计划，也可以安静地等一会儿。",
    ],
    winding_down: [
      "把手头的事做完",
      "等待当前工作收尾，随后按保存的计划入睡。",
    ],
    sleeping: ["正在休息", "留言会先保存，醒来之后再决定回应哪些内容。"],
    waking: ["醒来，看看新留言", "按会话审阅积压留言，再安排接下来的活动。"],
  };
  $("phase-title").textContent = state.paused
    ? "运行已暂停"
    : copy[state.phase][0];
  $("phase-description").textContent = state.paused
    ? "状态已经保留，可以在准备好后恢复。"
    : copy[state.phase][1];
  $("pending-count").textContent = state.inbox_counts
    .filter((x) => ["pending", "deferred"].includes(x.status))
    .reduce((sum, x) => sum + x.count, 0);
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
            `<div class="job-line"><div><strong>${escapeHtml(x.kind)}</strong><p>${when(x.started_at)} · ${escapeHtml(x.detail || "正在执行")}</p></div><span class="badge">${labels[x.status]}</span></div>`,
        )
        .join("")
    : empty("没有待核对的工作，所有行动都已收尾");
}
function jobLines(jobs, controls = false) {
  return jobs.length
    ? jobs
        .map(
          (job) =>
            `<div class="job-line"><div><strong title="${escapeHtml(job.goal)}">${escapeHtml(job.goal)}</strong><p>${escapeHtml(labels[job.lane])} · ${escapeHtml(job.reason)}${job.due_at ? ` · ${when(job.due_at)}` : ""}</p>${controls && job.status === "held" ? `<button class="text-button" data-scheduler-resume="${escapeHtml(job.activity_id)}">核对后继续调度 →</button>` : ""}</div><span class="badge">${escapeHtml(labels[job.status] || job.status)}</span></div>`,
        )
        .join("")
    : empty("还没有调度活动");
}
function schedulerSummary(state) {
  $("scheduler-badge").textContent = state.settings.enabled
    ? "已开启"
    : "未开启";
  $("scheduler-reason").textContent = state.settings.enabled
    ? state.last_reason
    : "自动调度尚未开启，可先配置模型与角色。";
  $("scheduler-next").textContent = state.next_at
    ? when(state.next_at)
    : "等待事件";
  $("scheduler-count").textContent =
    `${state.idle_started_today ?? state.idle_started} / ${state.settings.idle_max_runs_per_day}`;
  $("recent-jobs").innerHTML = jobLines(state.jobs.slice(0, 3));
  $("scheduler-job-list").innerHTML = jobLines(state.jobs.slice(0, 20), true);
}
async function overview() {
  const [state, schedule] = await Promise.all([
    api("status"),
    api("scheduler"),
    statistics(),
  ]);
  lifecycleView(state);
  schedulerSummary(schedule);
}
function fillScheduler(config) {
  schedulerRevision = config.revision;
  schedulerContent = config.content;
  $("scheduler-revision").textContent = `版本 ${schedulerRevision}`;
  for (const [id, key] of [
    ["enabled", "enabled"],
    ["chat", "chat_enabled"],
    ["idle", "autonomous_enabled"],
    ["plans", "run_plans"],
  ])
    $("scheduler-" + id).checked = config.content[key];
  $("scheduler-debounce").value = config.content.debounce_seconds;
  $("scheduler-cooldown").value = config.content.conversation_cooldown_seconds;
  $("scheduler-idle-interval").value =
    config.content.idle_interval_seconds / 60;
  $("scheduler-idle-limit").value = config.content.idle_max_runs_per_day;
  $("scheduler-goal").value = config.content.idle_goal;
  $("scheduler-json").value = JSON.stringify(config.content, null, 2);
}
async function scheduler() {
  const [config, schedule, state] = await Promise.all([
    api("config/scheduler"),
    api("scheduler"),
    api("status"),
  ]);
  fillScheduler({
    ...config,
    content: { ...schedule.settings, ...config.content },
  });
  schedulerSummary(schedule);
  lifecycleView(state);
}
async function saveScheduler(content) {
  const saved = await api("config/scheduler", "PUT", {
    expected_revision: schedulerRevision,
    content,
  });
  fillScheduler(saved);
  schedulerSummary(await api("scheduler"));
  notice("调度配置已保存，下一次安全调度时生效");
}
async function activities() {
  const rows = (await api("activities")).filter(
    (item) => item.kind !== "long_term_goal",
  );
  $("activity-list").innerHTML = rows.length
    ? rows
        .map(
          (x) =>
            `<article class="row-card"><div class="row-top"><strong>${escapeHtml(x.goal)}</strong><span class="badge">${escapeHtml(labels[x.status] || x.status)}</span></div><div class="row-meta">${escapeHtml(x.kind)} · ${when(x.created_at)} · ${x.steps} 步</div>${x.checkpoint.agent?.result?.text ? `<p>${escapeHtml(x.checkpoint.agent.result.text)}</p>` : ""}${x.checkpoint.agent?.stopped_reason ? `<p class="muted">${escapeHtml(x.checkpoint.agent.stopped_reason)}</p>` : ""}<div class="button-row">${["queued", "paused", "waiting", "interrupted", "running"].includes(x.status) ? `<button data-run="${x.id}" class="primary">运行这一轮</button>` : ""}${["queued", "paused", "waiting", "interrupted"].includes(x.status) ? `<button data-claim="${x.id}">手动接取</button>` : ""}${x.status === "running" ? `<button data-transition="${x.id}" data-status="completed">标记完成</button><button data-transition="${x.id}" data-status="paused">保存并暂停</button>` : ""}${!["completed", "cancelled", "failed"].includes(x.status) ? `<button data-transition="${x.id}" data-status="cancelled">取消</button>` : ""}</div></article>`,
        )
        .join("")
    : empty("还没有运行活动");
}
async function plans() {
  const rows = await api("plans");
  const active = rows.filter(
    (item) => !["completed", "cancelled", "failed"].includes(item.status),
  );
  $("plan-summary").textContent =
    `${active.length} 个未结束目标 · ${active.filter((item) => item.due_at).length} 个已安排时间`;
  $("plan-list").innerHTML =
    rows
      .map((item) => {
        const ended = ["completed", "cancelled", "failed"].includes(
          item.status,
        );
        const runs = item.runs
          .map(
            (run) =>
              `<div class="plan-run"><span>${when(run.created_at)} · ${escapeHtml(labels[run.status] || run.status)}${run.reason ? ` · ${escapeHtml(run.reason)}` : ""}</span>${run.scheduler_status === "held" ? `<button data-plan-resume="${escapeHtml(run.id)}">继续这一轮</button>` : ""}</div>`,
          )
          .join("");
        return `<article class="row-card"><div class="row-top"><strong>${escapeHtml(item.goal)}</strong><span class="badge">${escapeHtml(labels[item.status] || item.status)}</span></div><div class="row-meta">${escapeHtml(item.conversation_id || "角色自己的目标")} · ${item.due_at ? `下次 ${when(item.due_at)}` : "未安排下次时间"} · 已执行 ${item.runs.length} 轮</div><dl class="memory-details"><dt>下一步</dt><dd class="plan-text">${escapeHtml(item.next_step || "尚未安排")}</dd><dt>目前进度</dt><dd class="plan-text">${escapeHtml(item.progress || "还没有进展记录")}</dd>${item.last_result ? `<dt>最近结果</dt><dd class="plan-text">${escapeHtml(item.last_result)}</dd>` : ""}</dl>${runs ? `<details><summary>执行记录（${item.runs.length}）</summary>${runs}<button data-plan-activities>查看运行活动</button></details>` : ""}${!ended ? `<div class="button-row"><button data-plan-edit="${escapeHtml(item.id)}">编辑目标</button></div>` : ""}</article>`;
      })
      .join("") || empty("还没有持续目标，可以先记下一件想慢慢完成的事");
}
function planEditor(item = null) {
  editingPlan = item;
  $("plan-editor").hidden = false;
  $("plan-editor-title").textContent = item ? "编辑目标" : "新建目标";
  $("plan-editor-version").textContent = item
    ? `版本 ${item.version} · 已有执行尚未收尾时不能改写目标`
    : "保存目标不会立即调用模型；需开启自动调度与到期计划。";
  $("plan-goal").value = item?.goal || "";
  $("plan-next").value = item?.next_step || "";
  $("plan-next").required = !item || item.status === "waiting";
  $("plan-conversation").value = item?.conversation_id || "";
  $("plan-conversation").disabled = Boolean(item);
  $("plan-due").value = localDate(item?.due_at);
  $("plan-status").value = item?.status || "waiting";
  $("plan-progress").value = item?.progress || "";
  $("plan-status-field").hidden = $("plan-progress-field").hidden = !item;
  $("plan-due").disabled = Boolean(item && item.status !== "waiting");
  $("plan-editor").scrollIntoView({ behavior: "smooth", block: "start" });
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
function fillPersona(item) {
  revision = item.revision;
  personaContent = item.content;
  $("config-revision").textContent = `版本 ${revision}`;
  $("persona-json").value = JSON.stringify(item.content, null, 2);
  $("persona-name").value = item.content.name || "";
  $("persona-identity").value = item.content.identity || "";
  $("persona-style").value = item.content.style || "";
  $("persona-interests").value = Array.isArray(item.content.interests)
    ? item.content.interests.join("，")
    : "";
}
async function persona() {
  fillPersona(await api("config/persona"));
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
const relationshipAxes = {
  familiarity: "熟悉程度",
  affinity: "亲近倾向",
  trust: "信任倾向",
};
function relationshipScores(scores) {
  return Object.entries(relationshipAxes)
    .map(([axis, label]) => {
      const value = Number(scores[axis]);
      const minimum = axis === "familiarity" ? 0 : -100;
      return `<div class="relationship-score"><span>${label}</span><strong>${value}</strong><meter min="${minimum}" max="100" value="${value}" aria-label="${label} ${value}"></meter><small>${minimum} ～ 100</small></div>`;
    })
    .join("");
}
function renderPerson(item) {
  $("person-name").textContent =
    selectedPerson.label || selectedPerson.subject_id;
  $("person-identity").textContent = selectedPerson.subject_id;
  $("person-version").textContent = `版本 ${selectedPerson.version}`;
  $("person-scores").innerHTML = relationshipScores(item.scores);
  $("person-preview-caption").textContent = item.audience
    ? `当前范围：${item.audience} · 仅显示该受众可用的关系依据。`
    : "管理者总览包含各个会话的有效依据。角色聊天时会按受众重新筛选。";
  $("person-changes").innerHTML =
    item.changes
      .map((change) => {
        let status = change.status === "pending" ? "等待本轮完成" : "已记录";
        if (change.status === "discarded") status = "未采用";
        else if (change.status === "applied" && !change.evidence_valid)
          status = "依据已失效";
        else if (change.status === "applied" && !change.contributes)
          status = "已被新基准替代";
        const requested = change.status === "pending";
        const scores = Object.entries(relationshipAxes)
          .map(([axis, label]) => {
            const value = Number(
              requested ? change.requested[axis] : change[axis],
            );
            return `${label} ${change.kind === "delta" && value > 0 ? "+" : ""}${value}`;
          })
          .join(" · ");
        return `<div class="relationship-change"><div class="row-top"><strong>${change.kind === "baseline" ? "人工设定基准" : "相处中的变化"}</strong><span class="badge">${status}</span></div><p>${escapeHtml(change.reason)}</p><div class="row-meta">${escapeHtml(scores)}<br>${when(change.applied_at || change.created_at)} · ${escapeHtml(change.use_scope.join("、"))}</div></div>`;
      })
      .join("") || empty("这个范围里还没有可用的关系记录");
}
async function selectPerson(subject) {
  const request = ++personRequest;
  const changed = selectedPerson?.subject_id !== subject;
  const item = await api(`people/${encodeURIComponent(subject)}`);
  if (request !== personRequest) return;
  selectedPerson = item;
  if (changed) {
    $("person-preview-mode").value = "all";
    $("person-audience-field").hidden = true;
    $("person-audience").required = false;
  }
  $("person-detail").hidden = false;
  $("person-empty").hidden = true;
  document.querySelectorAll("[data-person]").forEach((button) => {
    const selected = button.dataset.person === subject;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  const mode = $("person-preview-mode").value;
  const audience =
    mode === "self" ? "@self" : $("person-audience").value.trim();
  const view =
    mode === "all"
      ? item
      : await api(
          `people/${encodeURIComponent(subject)}?${new URLSearchParams({ audience })}`,
        );
  if (request === personRequest) renderPerson(view);
}
async function peopleList() {
  const params = new URLSearchParams({
    query: $("people-query").value.trim(),
    offset: String(peopleOffset),
  });
  const rows = await api(`people?${params}`);
  $("people-list").innerHTML =
    rows
      .map(
        (item) =>
          `<button class="person-entry" data-person="${escapeHtml(item.subject_id)}"><span class="person-avatar">${icon("person")}</span><span><strong>${escapeHtml(item.label || item.subject_id)}</strong><small>${escapeHtml(item.subject_id)}</small><small>熟悉 ${Number(item.scores.familiarity)} · 亲近 ${Number(item.scores.affinity)}</small></span></button>`,
      )
      .join("") || empty("还没有匹配的人物");
  $("people-count").textContent = `本页 ${rows.length} 人`;
  $("people-page").textContent = `第 ${peopleOffset / 50 + 1} 页`;
  $("people-prev").disabled = peopleOffset === 0;
  $("people-next").disabled = rows.length < 50;
  const chosen =
    rows.find((item) => item.subject_id === selectedPerson?.subject_id) ||
    rows[0];
  if (chosen) await selectPerson(chosen.subject_id);
  else {
    personRequest++;
    selectedPerson = null;
    $("person-detail").hidden = true;
    $("person-empty").hidden = false;
  }
}
function fillRelationshipSettings(config) {
  relationshipRevision = config.revision;
  relationshipContent = config.content;
  $("relationship-version").textContent = `版本 ${config.revision}`;
  $("relationship-auto").checked = config.content.automatic_updates;
  $("relationship-bonus").value = config.content.attention_bonus;
  for (const axis of Object.keys(relationshipAxes))
    for (const period of ["step", "daily"]) {
      $(`relationship-${axis}-${period}`).value =
        config.content[`${axis}_${period}`];
    }
}
async function people() {
  const [config] = await Promise.all([
    api("config/relationships"),
    peopleList(),
  ]);
  fillRelationshipSettings(config);
}
function personEditor(item = null) {
  editingPerson = item;
  $("person-editor").hidden = false;
  $("person-editor-title").textContent = item ? "调整关系基准" : "登记人物";
  $("person-editor-version").textContent = item
    ? `基于版本 ${item.version}`
    : "初次登记";
  $("person-id").value = item?.subject_id || "";
  $("person-id").disabled = Boolean(item);
  $("person-label").value = item?.label || "";
  for (const axis of Object.keys(relationshipAxes))
    $(`person-${axis}`).value = item?.scores[axis] ?? 0;
  $("person-scope").value = "@self";
  $("person-reason").value = "";
  $("person-editor").scrollIntoView({ behavior: "smooth", block: "start" });
}
const loaders = {
  overview,
  plans,
  scheduler,
  activities,
  inbox,
  persona,
  memory,
  people,
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
  lifecycleView(await api("status"));
  sessionStorage.setItem("mkbot-token", token);
  $("login").hidden = true;
  $("workspace").hidden = false;
  await navigate(location.hash.slice(1));
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
async function navigate(view) {
  currentView = titles[view] ? view : "overview";
  document.querySelectorAll(".view").forEach((element) => {
    element.hidden = element.id !== currentView;
  });
  document.querySelectorAll("nav button").forEach((button) => {
    const selected = button.dataset.view === currentView;
    button.classList.toggle("selected", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $("page-title").textContent = $("breadcrumb-title").textContent =
    titles[currentView];
  $("page-description").textContent = descriptions[currentView];
  $("notice").hidden = true;
  history.replaceState(null, "", "#" + currentView);
  if (window.matchMedia("(max-width: 700px)").matches) setCollapsed(true);
  window.scrollTo({ top: 0 });
  await loadView();
}
document.querySelectorAll("nav button").forEach((button) => {
  button.title = titles[button.dataset.view];
  button.setAttribute("aria-label", titles[button.dataset.view]);
  button.addEventListener("click", () =>
    action(() => navigate(button.dataset.view)),
  );
});
document
  .querySelectorAll("[data-go]")
  .forEach((button) =>
    button.addEventListener("click", () =>
      action(() => navigate(button.dataset.go)),
    ),
  );
document.querySelectorAll("[data-hours]").forEach((button) =>
  button.addEventListener("click", () =>
    action(async () => {
      statisticsHours = Number(button.dataset.hours);
      await statistics();
    }),
  ),
);
$("sidebar")
  .querySelector(".brand")
  .addEventListener("click", (event) => {
    event.preventDefault();
    action(() => navigate("overview"));
  });
window.addEventListener("hashchange", () => {
  if (token) action(() => navigate(location.hash.slice(1)));
});
$("scheduler-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(() =>
    saveScheduler({
      ...schedulerContent,
      enabled: $("scheduler-enabled").checked,
      chat_enabled: $("scheduler-chat").checked,
      autonomous_enabled: $("scheduler-idle").checked,
      run_plans: $("scheduler-plans").checked,
      debounce_seconds: Number($("scheduler-debounce").value),
      conversation_cooldown_seconds: Number($("scheduler-cooldown").value),
      idle_interval_seconds: Math.round(
        Number($("scheduler-idle-interval").value) * 60,
      ),
      idle_max_runs_per_day: Number($("scheduler-idle-limit").value),
      idle_goal: $("scheduler-goal").value,
    }),
  );
});
$("reload-scheduler").addEventListener("click", () => action(scheduler));
$("save-scheduler-json").addEventListener("click", () =>
  action(() => saveScheduler(JSON.parse($("scheduler-json").value))),
);
$("scheduler-job-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-scheduler-resume]");
  if (button)
    action(async () => {
      await api(`scheduler/${button.dataset.schedulerResume}/resume`, "POST");
      await scheduler();
      notice("已接回调度；原有预算与步骤计数继续保留");
    });
});
$("persona-fields-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const content = {
      ...personaContent,
      name: $("persona-name").value,
      identity: $("persona-identity").value,
      style: $("persona-style").value,
      interests: splitIds($("persona-interests").value),
    };
    fillPersona(
      await api("config/persona", "PUT", {
        expected_revision: revision,
        content,
      }),
    );
    notice("人格配置已保存，新活动将读取这个版本");
  });
});
$("refresh").addEventListener("click", () => action(loadView));
document.querySelectorAll("[data-action]").forEach((button) =>
  button.addEventListener("click", () =>
    action(async () => {
      await api(`lifecycle/${button.dataset.action}`, "POST");
      lifecycleView(await api("status"));
      schedulerSummary(await api("scheduler"));
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
    lifecycleView(await api("status"));
    schedulerSummary(await api("scheduler"));
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
    notice("活动已保存");
  });
});
$("plan-new").addEventListener("click", () => planEditor());
$("plan-editor-close").addEventListener("click", () => {
  $("plan-editor").hidden = true;
});
$("plan-status").addEventListener("change", () => {
  const stopped = $("plan-status").value !== "waiting";
  $("plan-next").required = !stopped;
  $("plan-due").disabled = stopped;
  if (stopped) $("plan-due").value = "";
});
$("plan-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const data = {
      goal: $("plan-goal").value,
      next_step: $("plan-next").value,
      due_at: $("plan-due").value
        ? new Date($("plan-due").value).toISOString()
        : null,
    };
    if (editingPlan) {
      await api(`plans/${editingPlan.id}`, "PUT", {
        ...data,
        expected_version: editingPlan.version,
        progress: $("plan-progress").value,
        status: $("plan-status").value,
      });
    } else {
      await api("plans", "POST", {
        ...data,
        conversation_id: $("plan-conversation").value.trim() || null,
      });
    }
    $("plan-editor").hidden = true;
    await plans();
    notice("目标已保存");
  });
});
$("plan-list").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  action(async () => {
    if (button.dataset.planEdit)
      planEditor(await api(`plans/${button.dataset.planEdit}`));
    else if (button.dataset.planResume) {
      await api(`scheduler/${button.dataset.planResume}/resume`, "POST");
      await plans();
      notice("已接回原来的执行轮次，累计额度保留");
    } else if (button.hasAttribute("data-plan-activities"))
      await navigate("activities");
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
    fillPersona(saved);
    notice("人格完整配置已保存，新活动将读取这个版本");
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
$("people-filter").addEventListener("submit", (e) => {
  e.preventDefault();
  peopleOffset = 0;
  action(peopleList);
});
$("people-prev").addEventListener("click", () => {
  peopleOffset = Math.max(0, peopleOffset - 50);
  action(peopleList);
});
$("people-next").addEventListener("click", () => {
  peopleOffset += 50;
  action(peopleList);
});
$("people-list").addEventListener("click", (e) => {
  const button = e.target.closest("[data-person]");
  if (button) action(() => selectPerson(button.dataset.person));
});
$("person-new").addEventListener("click", () => personEditor());
$("person-editor-close").addEventListener("click", () => {
  $("person-editor").hidden = true;
});
$("person-edit").addEventListener("click", () =>
  action(async () => {
    personEditor(
      await api(`people/${encodeURIComponent(selectedPerson.subject_id)}`),
    );
  }),
);
$("person-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    const subject = $("person-id").value.trim();
    const payload = {
      expected_version: editingPerson?.version ?? 0,
      label: $("person-label").value.trim(),
      reason: $("person-reason").value,
      scope: splitIds($("person-scope").value),
    };
    for (const axis of Object.keys(relationshipAxes))
      payload[axis] = Number($(`person-${axis}`).value);
    selectedPerson = await api(
      `people/${encodeURIComponent(subject)}`,
      "PUT",
      payload,
    );
    $("person-editor").hidden = true;
    $("person-preview-mode").value = "all";
    $("person-audience-field").hidden = true;
    $("person-audience").required = false;
    $("people-query").value = subject;
    peopleOffset = 0;
    await peopleList();
    notice("已保存关系基准，后续变化从此开始。");
  });
});
$("person-preview-mode").addEventListener("change", () => {
  const conversation = $("person-preview-mode").value === "conversation";
  $("person-audience-field").hidden = !conversation;
  $("person-audience").required = conversation;
});
$("person-preview-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (selectedPerson) action(() => selectPerson(selectedPerson.subject_id));
});
$("person-memories").addEventListener("click", () =>
  action(async () => {
    $("memory-subject").value = selectedPerson.subject_id;
    $("memory-tier").value = "";
    $("memory-status").value = "active";
    memoryOffset = 0;
    await navigate("memory");
  }),
);
$("relationship-settings-form").addEventListener("submit", (e) => {
  e.preventDefault();
  action(async () => {
    const content = {
      ...relationshipContent,
      automatic_updates: $("relationship-auto").checked,
      attention_bonus: Number($("relationship-bonus").value),
    };
    for (const axis of Object.keys(relationshipAxes))
      for (const period of ["step", "daily"]) {
        content[`${axis}_${period}`] = Number(
          $(`relationship-${axis}-${period}`).value,
        );
      }
    fillRelationshipSettings(
      await api("config/relationships", "PUT", {
        expected_revision: relationshipRevision,
        content,
      }),
    );
    notice("已保存关系变化规则。");
  });
});
if (token)
  enter().catch(() => {
    token = "";
    sessionStorage.removeItem("mkbot-token");
  });
