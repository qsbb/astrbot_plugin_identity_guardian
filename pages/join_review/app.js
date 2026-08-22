"use strict";

let bridge = null;
let popoverTrigger = null;
const API_PREFIX = "join-review";
// 仅用于渲染层：当前展开行内驳回原因输入的申请 id。
let rejectConfirmId = null;

const state = {
  joinedGroups: [],
  configuredGroups: [],
  targetGroups: [],
  requests: [],
  groups: [],
  groupMap: new Map(),
  selected: new Set(),
  rowBusy: new Set(),
  requestBusy: new Set(),
  requestErrors: new Map(),
  batchBusy: false,
  legacyAvailable: false,
  // 当前打开设置悬浮窗的群 key；一次只开一个。
  popoverKey: null,
};

const API_ERROR_MESSAGES = {
  unauthorized: "当前 Dashboard 会话无权执行此操作",
  forbidden: "当前 Bot 或账号没有该群的审核权限",
  invalid_group_id: "群号格式不正确",
  invalid_platform_id: "Bot 平台标识无效",
  group_not_joined: "Bot 当前不在该群中",
  insufficient_permission: "Bot 当前不具备入群审核权限",
  specified_groups_required: "通知到指定群时，必须填写审核群白名单",
  push_group_not_joined: "推送群必须是当前 Bot 已加入的群",
  invalid_push_style: "推送样式无效",
  invalid_join_questions: "入群问答预设格式不正确",
  join_question_answers_required: "每条入群问答预设至少要有一条参考答案",
  too_many_join_questions: "入群问答预设最多 50 条",
  duplicate_group_config: "批量配置中包含重复群",
  expired: "该申请已过期，不能继续处理",
  already_processed: "该申请已由其他管理员处理",
  busy: "该申请正在被其他管理员处理",
  platform_error: "平台操作失败，申请仍未完成",
  guard_blocked: "插件已紧急停止或已熔断，恢复后才能处理入群申请",
  invalid_request: "请求参数不完整",
  invalid_answer: "请输入申请人答案",
  simulate_text_too_long: "问题或答案超长（上限 2048 字）",
  simulate_failed: "模拟判定失败，请查看插件日志",
  invalid_provider: "审核模型不在可用列表中",
  invalid_recall_flag: "知联动开关取值无效",
  settings_unavailable: "设置保存通道不可用",
  settings_save_failed: "设置保存失败，请查看插件日志",
  config_unavailable: "插件配置不可写",
  config_save_failed: "配置写入失败，请查看插件日志",
  config_save_superseded: "配置已被其他修改覆盖，请刷新后重试",
  invalid_enabled: "启用状态无效",
  platform_unavailable: "Bot 平台当前不可用",
  target_group_persist_failed: "目标群保存失败，请查看插件日志",
  target_group_not_configured: "目标群未登记或已停用",
  target_group_has_pending_invitation: "该目标群还有待处理邀请，请先处理邀请",
  invalid_user_id: "成员 QQ 号格式不正确",
  invite_member_unsupported: "当前 QQ 适配器不支持 Bot 主动邀请成员",
  invite_member_failed: "主动邀请失败，请查看插件日志",
};

function $(selector, root = document) {
  return root.querySelector(selector);
}

function $all(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  })[character]);
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value || {}, key);
}

async function resolveBridge(timeout = 3000) {
  if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
  if (typeof window.waitForAstrBotBridge === "function") {
    return window.waitForAstrBotBridge(timeout);
  }

  const startedAt = Date.now();
  while (Date.now() - startedAt < timeout) {
    await new Promise((resolve) => setTimeout(resolve, 50));
    if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
  }
  throw new Error("请从 AstrBot 插件管理页打开此页面");
}

function parseJsonResponse(value) {
  let response = value;
  if (typeof value === "string") {
    try {
      response = JSON.parse(value);
    } catch (_error) {
      throw new Error("服务端返回了无法识别的数据");
    }
  }
  if (!response || typeof response !== "object") {
    throw new Error("服务端返回了空响应");
  }
  if (response.success === false) {
    const code = String(response.error || response.code || "");
    throw new Error(API_ERROR_MESSAGES[code] || response.detail || response.message || code || "请求失败");
  }
  return response.data ?? response;
}

async function apiGet(name) {
  if (!bridge || typeof bridge.apiGet !== "function") {
    throw new Error("AstrBot 页面通信接口尚未就绪");
  }
  return parseJsonResponse(await bridge.apiGet(`${API_PREFIX}/${name}`));
}

async function apiPost(name, payload) {
  if (!bridge || typeof bridge.apiPost !== "function") {
    throw new Error("AstrBot 页面通信接口尚未就绪");
  }
  return parseJsonResponse(await bridge.apiPost(`${API_PREFIX}/${name}`, payload));
}

function listFrom(payload, keys) {
  if (Array.isArray(payload)) return payload;
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key];
  }
  return [];
}

function groupKey(platformId, groupId) {
  return `${String(platformId)}\u001f${String(groupId)}`;
}

function stringValue(...values) {
  const found = values.find((value) => value !== undefined && value !== null && String(value).trim() !== "");
  return found === undefined ? "" : String(found).trim();
}

function normalizeGroup(item, configured = false) {
  const config = item?.config && typeof item.config === "object" ? item.config : {};
  const merged = { ...item, ...config };
  const platformId = stringValue(merged.platform_id, merged.adapter_id);
  const groupId = stringValue(merged.group_id);
  const role = stringValue(merged.bot_role, merged.role, merged.bot_identity);
  const normalizedRole = role.toLowerCase();
  const explicitPermission = merged.can_review ?? merged.has_review_permission
    ?? merged.bot_can_review ?? merged.can_audit;
  const canReview = explicitPermission === undefined
    ? ["owner", "admin", "群主", "管理员"].includes(normalizedRole)
    : explicitPermission === true;
  return {
    ...merged,
    platform_id: platformId,
    group_id: groupId,
    group_name: stringValue(merged.group_name, merged.name) || "未知群名",
    bot_id: stringValue(merged.bot_id, merged.self_id),
    bot_name: stringValue(merged.bot_name, merged.bot_nickname, merged.self_name),
    bot_role: role || "未知身份",
    can_review: canReview,
    configured: merged.configured === true || configured,
    auto_audit_enabled: merged.auto_audit_enabled === true,
    review_send_enabled: merged.review_send_enabled === true,
    notify_target: ["target_group", "specified_groups", "both"].includes(merged.notify_target)
      ? merged.notify_target
      : "target_group",
    specified_group_ids: Array.isArray(merged.specified_group_ids)
      ? merged.specified_group_ids.map(String)
      : [],
    include_answer: merged.include_answer !== false,
    pinned: merged.pinned === true,
    push_group_ids: Array.isArray(merged.push_group_ids)
      ? merged.push_group_ids.map(String)
      : [],
    push_style: ["formatted", "natural"].includes(merged.push_style)
      ? merged.push_style
      : "natural",
    join_questions: Array.isArray(merged.join_questions)
      ? merged.join_questions
        .filter((item) => item && typeof item === "object")
        .map((item) => ({
          question: stringValue(item.question),
          answers: Array.isArray(item.answers) ? item.answers.map(String) : [],
        }))
      : [],
    pending_count: Number.isFinite(Number(merged.pending_count)) ? Number(merged.pending_count) : 0,
    joined: merged.joined !== false && !configured,
  };
}

function mergeGroups() {
  const merged = new Map();
  for (const item of state.joinedGroups) {
    const group = normalizeGroup(item, false);
    if (group.platform_id && group.group_id) {
      group.joined = true;
      merged.set(groupKey(group.platform_id, group.group_id), group);
    }
  }
  for (const item of state.configuredGroups) {
    const config = normalizeGroup(item, true);
    if (!config.platform_id || !config.group_id) continue;
    const key = groupKey(config.platform_id, config.group_id);
    const discovered = merged.get(key);
    merged.set(key, {
      ...(discovered || config),
      ...config,
      group_name: discovered?.group_name || config.group_name,
      bot_id: discovered?.bot_id || config.bot_id,
      bot_name: discovered?.bot_name || config.bot_name,
      bot_role: discovered?.bot_role || config.bot_role,
      can_review: discovered ? discovered.can_review : config.can_review,
      joined: Boolean(discovered),
      configured: true,
    });
  }

  const pendingCounts = new Map();
  for (const request of state.requests) {
    if (!["pending", "platform_error"].includes(String(request.status || "pending"))) continue;
    const key = groupKey(request.platform_id, request.group_id);
    pendingCounts.set(key, (pendingCounts.get(key) || 0) + 1);
  }
  state.groups = Array.from(merged.values()).map((group) => ({
    ...group,
    pending_count: Math.max(group.pending_count, pendingCounts.get(groupKey(group.platform_id, group.group_id)) || 0),
  })).sort((left, right) => (
    // 手动置顶的群排在最前；其次有审核权限的群在前，无审核权限的排在下面。
    Number(right.pinned) - Number(left.pinned)
      || Number(right.can_review) - Number(left.can_review)
      || left.platform_id.localeCompare(right.platform_id, "zh-CN")
      || Number(left.group_id) - Number(right.group_id)
  ));
  state.groupMap = new Map(state.groups.map((group) => [groupKey(group.platform_id, group.group_id), group]));
  state.selected = new Set(Array.from(state.selected).filter((key) => {
    const group = state.groupMap.get(key);
    return group?.joined && group?.can_review;
  }));
}

function updateStats() {
  const setStat = (name, value) => {
    const element = $(`[data-stat="${name}"]`);
    if (element) {
      element.classList.remove("skeleton");
      element.textContent = String(value);
    }
  };
  setStat("total_groups", state.groups.length);
  setStat("reviewable_groups", state.groups.filter((group) => group.joined && group.can_review).length);
  setStat(
    "pending_requests",
    state.requests.filter((request) =>
      ["pending", "platform_error"].includes(String(request.status || "pending"))
    ).length,
  );
}

function setButtonBusy(button, busy, busyLabel = "处理中…") {
  if (!button) return;
  if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent.trim();
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  button.textContent = busy ? busyLabel : button.dataset.idleLabel;
}

function showPageError(error) {
  const element = $("#page-error");
  element.textContent = error?.message || String(error || "操作失败");
  element.classList.remove("hidden");
  clearTimeout(showPageError.timer);
  showPageError.timer = setTimeout(() => element.classList.add("hidden"), 6000);
}

function clearPageError() {
  $("#page-error").classList.add("hidden");
  $("#page-error").textContent = "";
}

function showStatus(message) {
  const element = $("#page-status");
  element.textContent = message;
  element.classList.remove("hidden");
  clearTimeout(showStatus.timer);
  showStatus.timer = setTimeout(() => element.classList.add("hidden"), 3500);
}

function permissionBadge(group) {
  if (!group.joined) return '<span class="status-badge warn">未发现在线群</span>';
  return group.can_review
    ? '<span class="status-badge good">可审核</span>'
    : '<span class="status-badge bad">无审核权限</span>';
}

function switchMarkup(name, checked, disabled, label) {
  return `<label class="toggle-field"><input class="editable-control" type="checkbox" data-field="${name}"`
    + `${checked ? " checked" : ""}${disabled ? " disabled" : ""}><span>${escapeHtml(label)}</span></label>`;
}

function renderGroupRow(group) {
  const key = groupKey(group.platform_id, group.group_id);
  const busy = state.rowBusy.has(key);
  const unavailable = !group.joined || !group.can_review;
  const fieldsDisabled = busy || unavailable;
  const selected = state.selected.has(key);
  const popoverOpen = state.popoverKey === key;
  const botLabel = group.bot_name || (group.bot_id ? `QQ ${group.bot_id}` : group.platform_id);
  const statusMarkup = [
    group.configured ? '<span class="status-badge good">已配置</span>' : '<span class="status-badge">未配置</span>',
    `<span class="status-badge ${group.pending_count ? "warn" : ""}">待审 ${group.pending_count}</span>`,
  ].join("");
  return `<tr data-group-key="${escapeHtml(key)}" class="${unavailable ? "row-disabled" : ""}${group.pinned ? " row-pinned" : ""}">
    <td class="select-cell" data-label="选择"><input type="checkbox" data-select-group aria-label="选择群 ${escapeHtml(group.group_id)}"${selected ? " checked" : ""}${fieldsDisabled ? " disabled" : ""}></td>
    <td class="group-cell" data-label="群"><button class="group-link" type="button" data-open-settings aria-haspopup="dialog" aria-expanded="${popoverOpen}"${fieldsDisabled ? " disabled" : ""} aria-label="配置群 ${escapeHtml(group.group_id)}">${escapeHtml(group.group_name)}</button><span class="secondary-value">${escapeHtml(group.group_id)}</span></td>
    <td data-label="Bot / 权限"><span class="primary-value">${escapeHtml(botLabel)}</span><span class="secondary-value">${escapeHtml(group.bot_role)} · ${escapeHtml(group.platform_id)}</span>${permissionBadge(group)}</td>
    <td data-label="状态">${statusMarkup}</td>
    <td data-label="操作"><button class="button compact" type="button" data-open-settings aria-haspopup="dialog" aria-expanded="${popoverOpen}"${fieldsDisabled ? " disabled" : ""}>设置</button></td>
  </tr>`;
}

function renderGroups() {
  mergeGroups();
  const body = $("#groups-body");
  body.innerHTML = state.groups.length
    ? state.groups.map(renderGroupRow).join("")
    : '<tr class="empty-row"><td colspan="5">当前 aiocqhttp Bot 暂无可显示的群，请刷新已加入群。</td></tr>';
  const configuredCount = state.groups.filter((group) => group.configured).length;
  $("#group-summary").textContent = `共 ${state.groups.length} 个群，${configuredCount} 个已配置；点群名打开设置悬浮窗，未配置群默认关闭两个开关。`;
  $("#legacy-notice").classList.toggle("hidden", !state.legacyAvailable);
  updateBatchUi();
  updateStats();
  renderSimulateGroupOptions();
  renderOpenPopover();
}

// ------------------------------------------------------------------
// 模拟申请诊断：零副作用，走真实三段审核链路
// ------------------------------------------------------------------

const SIMULATE_STAGE_LABELS = { preset: "预设判定", knowledge: "知联动判定", fallback: "兜底" };
const SIMULATE_OUTCOME_BADGES = { passed: ["通过", "good"], failed: ["未通过", "bad"], skipped: ["跳过", ""] };
const SIMULATE_WOULD_LABELS = {
  approve: "实际发生时：批准该申请",
  left_on_platform: "实际发生时：保持平台待审（忽略）",
  pending_review: "实际发生时：转人工待审并推送",
  ignored: "实际发生时：忽略（该群两个开关均关闭）",
};
const SIMULATE_VERDICT_LABELS = { correct: "建议通过", incorrect: "建议拒绝", uncertain: "不确定", unavailable: "不可用" };
const SIMULATE_PREVIEW_STYLE_BADGES = {
  natural: ["自然文案（LLM 生成）", "good"],
  formatted: ["格式化模板", ""],
  natural_fallback_formatted: ["自然文案生成失败，已回退格式化模板", "bad"],
  natural_redacted_formatted: ["已隐藏答案，使用安全格式化模板", "warn"],
};
const SIMULATE_OPINION_SOURCE_LABELS = {
  llm: "看法：LLM 生成",
  decision: "看法：自动审核结论",
  none: "看法：无",
};

function renderSimulateResultReplyPreview(data) {
  const replies = data?.result_reply_preview && typeof data.result_reply_preview === "object" ? data.result_reply_preview : null;
  if (!replies) {
    return "";
  }
  const row = (label, item) => {
    const entry = item && typeof item === "object" ? item : {};
    const badge = entry.fallback ? ' <span class="status-badge">模板</span>' : "";
    return `<div class="simulate-stage"><span class="simulate-detail"><strong>${label}</strong>${badge} ${escapeHtml(entry.text || "")}</span></div>`;
  };
  return `<div class="simulate-reply-preview">
      <span class="simulate-detail"><strong>结果回复预览</strong>（管理员引用回复后 bot 的回应，仅预览，未发送）</span>
      ${row("若同意：", replies.approved)}
      ${row("若拒绝：", replies.rejected)}
    </div>`;
}

function renderSimulatePreview(data) {
  const preview = data?.push_preview && typeof data.push_preview === "object" ? data.push_preview : null;
  if (data?.would !== "pending_review") {
    return `<div class="simulate-preview"><span class="simulate-detail">该申请不会触发推送，无推送与结果回复预览。</span></div>`;
  }
  if (!preview) {
    return `<div class="simulate-preview"><span class="simulate-detail">推送文案预览生成失败，请查看插件日志。</span></div>`;
  }
  const [styleLabel, badgeClass] = SIMULATE_PREVIEW_STYLE_BADGES[preview.style] || [String(preview.style || ""), ""];
  const meta = [
    SIMULATE_OPINION_SOURCE_LABELS[preview.opinion_source] || "看法：无",
    preview.persona_used ? "人格：已带入" : "人格：未配置",
    preview.provider ? `LLM：${preview.provider}` : "LLM：默认（主对话）",
    `近期群消息：${Number(preview.contexts_used) || 0} 条`,
  ];
  return `<div class="simulate-preview">
      <span class="status-badge ${badgeClass}">推送文案预览 · ${escapeHtml(styleLabel)}</span>
      <pre class="simulate-preview-text">${escapeHtml(preview.text || "")}</pre>
      <span class="simulate-detail">${escapeHtml(meta.join(" · "))}（仅预览，未发送）</span>
    </div>
    ${renderSimulateResultReplyPreview(data)}`;
}

function renderSimulateResult(data) {
  const result = $("#simulate-result");
  const stages = Array.isArray(data?.stages) ? data.stages : [];
  const final = data?.final && typeof data.final === "object" ? data.final : {};
  const rows = stages.map((stage) => {
    const stageName = SIMULATE_STAGE_LABELS[stage.stage] || String(stage.stage || "未知阶段");
    const [outcomeLabel, badgeClass] = SIMULATE_OUTCOME_BADGES[stage.outcome] || [String(stage.outcome || ""), ""];
    return `<div class="simulate-stage"><span class="status-badge ${badgeClass}">${stageName} · ${outcomeLabel}</span><span class="simulate-detail">${escapeHtml(stage.detail)}</span></div>`;
  }).join("");
  const verdict = SIMULATE_VERDICT_LABELS[final.verdict] || String(final.verdict || "未知");
  const confidence = Number.isFinite(Number(final.confidence)) ? Number(final.confidence).toFixed(2) : "0.00";
  const would = SIMULATE_WOULD_LABELS[data?.would] || "";
  const presetsSource = { group: "按群预设", global: "全局回退预设", none: "无预设" }[data?.presets_source] || "";
  result.innerHTML = `${rows}
    <div class="simulate-final">
      <strong>最终结论：${escapeHtml(verdict)}（置信度 ${confidence}）</strong>
      <span class="simulate-detail">${escapeHtml(final.reason || "")}${presetsSource ? ` · 预设来源：${presetsSource}` : ""}</span>
      <span class="simulate-would">${escapeHtml(would)}（仅说明，未执行任何操作）</span>
    </div>
    ${renderSimulatePreview(data)}`;
  result.classList.remove("hidden");
}

function renderSimulateGroupOptions() {
  const select = $("#simulate-group");
  if (!select) return;
  const current = select.value;
  const options = state.groups.filter((group) => group.joined && group.can_review);
  select.innerHTML = options.length
    ? options.map((group) => {
      const key = groupKey(group.platform_id, group.group_id);
      return `<option value="${escapeHtml(key)}">${escapeHtml(group.group_name)}（${escapeHtml(group.group_id)}）</option>`;
    }).join("")
    : '<option value="">暂无可审核群</option>';
  if (options.some((group) => groupKey(group.platform_id, group.group_id) === current)) {
    select.value = current;
  }
}

async function runSimulate() {
  const button = $("#simulate-run");
  if (button.getAttribute("aria-busy") === "true") return;
  const errorEl = $("#simulate-error");
  errorEl.classList.add("hidden");
  $("#simulate-result").classList.add("hidden");
  const group = state.groupMap.get($("#simulate-group").value);
  if (!group) {
    errorEl.textContent = "请选择要模拟的群";
    errorEl.classList.remove("hidden");
    return;
  }
  const answer = $("#simulate-answer").value.trim();
  if (!answer) {
    errorEl.textContent = "请输入申请人答案";
    errorEl.classList.remove("hidden");
    return;
  }
  setButtonBusy(button, true, "判定中…");
  try {
    const data = await apiPost("simulate", {
      platform_id: group.platform_id,
      group_id: group.group_id,
      question: $("#simulate-question").value.trim(),
      answer: answer,
    });
    renderSimulateResult(data);
  } catch (error) {
    errorEl.textContent = error?.message || String(error || "模拟失败");
    errorEl.classList.remove("hidden");
  } finally {
    setButtonBusy(button, false);
  }
}

// ------------------------------------------------------------------
// 群设置悬浮窗：一次只开一个，ESC / 点击外部关闭
// ------------------------------------------------------------------

function popoverElement() {
  return $("#group-popover");
}

function jqItemMarkup(item, disabled) {
  return `<div class="jq-item" data-jq-item>
    <input class="group-whitelist editable-control" data-jq-question type="text" maxlength="2048" placeholder="入群问题（留空 = 匹配任意问题）" value="${escapeHtml(item.question)}"${disabled ? " disabled" : ""}>
    <textarea class="group-whitelist jq-answers editable-control" data-jq-answers rows="2" maxlength="2048" placeholder="参考答案，每行一个"${disabled ? " disabled" : ""}>${escapeHtml(item.answers.join("\n"))}</textarea>
    <button class="button compact danger-quiet" type="button" data-jq-remove${disabled ? " disabled" : ""}>删除</button>
  </div>`;
}

function popoverFieldMarkup(group, disabled) {
  const notificationDisabled = group.notify_target === "target_group" || disabled;
  return `
  <div class="popover-heading">
    <strong id="group-popover-title">${escapeHtml(group.group_name)}</strong>
    <span class="secondary-value">${escapeHtml(group.group_id)}</span>
  </div>
  <div class="popover-grid">
    ${switchMarkup("auto_audit_enabled", group.auto_audit_enabled, disabled, group.auto_audit_enabled ? "自动审核：开启" : "自动审核：关闭")}
    ${switchMarkup("review_send_enabled", group.review_send_enabled, disabled, group.review_send_enabled ? "发送审核：开启" : "发送审核：关闭")}
    ${switchMarkup("include_answer", group.include_answer, disabled, group.include_answer ? "显示答案" : "隐藏答案")}
    ${switchMarkup("pinned", group.pinned, disabled, group.pinned ? "已置顶" : "置顶")}
    <label class="popover-field"><span>通知位置</span><select class="inline-select editable-control" data-field="notify_target"${disabled ? " disabled" : ""}>
      <option value="target_group"${group.notify_target === "target_group" ? " selected" : ""}>申请所属群</option>
      <option value="specified_groups"${group.notify_target === "specified_groups" ? " selected" : ""}>指定审核群</option>
      <option value="both"${group.notify_target === "both" ? " selected" : ""}>两边发送</option>
    </select></label>
    <label class="popover-field"><span>指定审核群白名单</span><input class="group-whitelist editable-control" data-field="specified_group_ids" type="text" inputmode="numeric" maxlength="2099" placeholder="群号，逗号分隔" value="${escapeHtml(group.specified_group_ids.join(", "))}"${notificationDisabled ? " disabled" : ""}></label>
    <label class="popover-field"><span>推送群（留空回退申请所属群）</span><input class="group-whitelist editable-control" data-field="push_group_ids" type="text" inputmode="numeric" maxlength="2099" placeholder="群号，逗号分隔" value="${escapeHtml(group.push_group_ids.join(", "))}"${disabled ? " disabled" : ""}></label>
    <label class="popover-field"><span>推送样式</span><select class="inline-select editable-control" data-field="push_style"${disabled ? " disabled" : ""}>
      <option value="formatted"${group.push_style === "formatted" ? " selected" : ""}>格式化</option>
      <option value="natural"${group.push_style === "natural" ? " selected" : ""}>自然语言</option>
    </select></label>
    <div class="popover-field jq-editor">
      <span>入群问答预设（问题留空 = 匹配任意问题；答案每行一个）</span>
      <div class="jq-list" data-jq-list>${group.join_questions.map((item) => jqItemMarkup(item, disabled)).join("")}</div>
      <button class="button compact secondary" type="button" data-jq-add${disabled ? " disabled" : ""}>+ 添加预设</button>
    </div>
  </div>
  <div class="field-error" data-row-error></div>
  <div class="popover-actions">
    <button class="button compact" type="button" data-popover-save${disabled ? " disabled" : ""} aria-busy="${state.rowBusy.has(groupKey(group.platform_id, group.group_id))}">${state.rowBusy.has(groupKey(group.platform_id, group.group_id)) ? "保存中…" : "保存"}</button>
    <button class="button compact secondary" type="button" data-popover-cancel>关闭</button>
  </div>`;
}

function positionGroupPopover(anchor) {
  const popover = popoverElement();
  if (!popover || popover.classList.contains("hidden")) return;
  const margin = 8;
  const rect = anchor ? anchor.getBoundingClientRect() : null;
  const width = popover.offsetWidth;
  const height = popover.offsetHeight;
  let left = rect ? rect.left : (window.innerWidth - width) / 2;
  left = Math.max(margin, Math.min(left, window.innerWidth - width - margin));
  let top = rect ? rect.bottom + 6 : (window.innerHeight - height) / 2;
  if (top + height > window.innerHeight - margin) {
    top = Math.max(margin, (rect ? rect.top : window.innerHeight / 2) - height - 6);
  }
  popover.style.left = `${left}px`;
  popover.style.top = `${top}px`;
}

function openGroupPopover(key, anchor) {
  const group = state.groupMap.get(key);
  if (!group) return;
  if (state.popoverKey === key) {
    closeGroupPopover();
    return;
  }
  if (popoverTrigger && document.contains(popoverTrigger)) {
    popoverTrigger.setAttribute("aria-expanded", "false");
  }
  state.popoverKey = key;
  popoverTrigger = anchor;
  anchor?.setAttribute("aria-expanded", "true");
  const popover = popoverElement();
  popover.dataset.groupKey = key;
  const busy = state.rowBusy.has(key);
  popover.innerHTML = popoverFieldMarkup(group, busy || !group.joined || !group.can_review);
  popover.classList.remove("hidden");
  positionGroupPopover(anchor);
  $(".editable-control", popover)?.focus();
}

function closeGroupPopover() {
  const trigger = popoverTrigger;
  state.popoverKey = null;
  popoverTrigger = null;
  const popover = popoverElement();
  popover.classList.add("hidden");
  popover.innerHTML = "";
  delete popover.dataset.groupKey;
  if (trigger && document.contains(trigger)) {
    trigger.setAttribute("aria-expanded", "false");
    trigger.focus({ preventScroll: true });
  }
}

// 行数据刷新后同步已打开的悬浮窗内容；群消失则关闭。
function renderOpenPopover() {
  if (!state.popoverKey) return;
  const group = state.groupMap.get(state.popoverKey);
  if (!group) {
    closeGroupPopover();
    return;
  }
  const popover = popoverElement();
  const busy = state.rowBusy.has(state.popoverKey);
  popover.innerHTML = popoverFieldMarkup(group, busy || !group.joined || !group.can_review);
  const anchor = $(`tr[data-group-key="${CSS.escape(state.popoverKey)}"] .group-link`);
  popoverTrigger = anchor || popoverTrigger;
  positionGroupPopover(anchor);
}

function updateBatchUi() {
  const selectedCount = state.selected.size;
  const availableCount = state.groups.filter((group) => group.joined && group.can_review).length;
  $("#selected-count").textContent = `已选择 ${selectedCount} 个群`;
  $("#selected-count").classList.toggle("active", selectedCount > 0);
  const selectAll = $("#select-all");
  selectAll.checked = availableCount > 0 && selectedCount === availableCount;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < availableCount;
  selectAll.disabled = state.batchBusy || availableCount === 0;
  for (const button of $all("[data-batch-action]")) {
    button.disabled = state.batchBusy || selectedCount === 0;
    button.setAttribute("aria-busy", String(state.batchBusy));
  }
  const legacyButton = $("#apply-legacy");
  legacyButton.disabled = state.batchBusy || selectedCount === 0;
  legacyButton.setAttribute("aria-busy", String(state.batchBusy));
}

function requestStatus(status) {
  const labels = {
    pending: ["待审核", "warn"],
    approved: ["已批准", "good"],
    rejected: ["已驳回", "bad"],
    expired: ["已过期", ""],
    platform_error: ["平台操作失败", "bad"],
  };
  return labels[String(status)] || [String(status || "未知"), ""];
}

function formatTime(timestamp) {
  const number = Number(timestamp);
  if (!Number.isFinite(number) || number <= 0) return "未知时间";
  const date = new Date(number < 1e12 ? number * 1000 : number);
  return Number.isNaN(date.getTime()) ? "未知时间" : date.toLocaleString("zh-CN", { hour12: false });
}

function groupDisplayForRequest(request) {
  const group = state.groupMap.get(groupKey(request.platform_id, request.group_id));
  const target = state.targetGroups.find((item) => (
    String(item.platform_id) === String(request.platform_id)
      && String(item.group_id) === String(request.group_id)
  ));
  const name = stringValue(request.group_name, group?.group_name, target?.group_name) || "未知群名";
  return `${name}（${request.group_id || "未知群号"}）`;
}

function renderRequestCard(request) {
  const requestId = String(request.request_id || "");
  const busy = state.requestBusy.has(requestId);
  const actionable = ["pending", "platform_error"].includes(String(request.status || "pending"));
  const invitation = request.request_kind === "invitation" || request.sub_type === "invite";
  const [statusLabel, statusClass] = requestStatus(request.status || "pending");
  const nickname = stringValue(request.nickname) || "未知";
  const userId = stringValue(request.user_id) || "未知";
  const level = stringValue(request.level) || "未知";
  const question = stringValue(request.question) || "未提供问题";
  const answer = hasOwn(request, "answer") ? (stringValue(request.answer) || "未填写") : "已按群配置隐藏";
  const error = state.requestErrors.get(requestId) || "";
  const rejectOpen = rejectConfirmId === requestId && actionable && !busy;
  const actionLabel = invitation ? "接受邀请" : "批准";
  const rejectLabel = invitation ? "拒绝邀请" : "驳回";
  const actionsMarkup = rejectOpen
    ? `<div class="reject-panel">
        <input class="reject-reason-input" type="text" data-reject-reason maxlength="256"
          placeholder="驳回原因（可选，将反馈给申请者）" aria-label="驳回原因">
        <div class="reject-panel-actions">
          <button class="button compact danger" type="button" data-reject-confirm>${rejectLabel}</button>
          <button class="button compact" type="button" data-reject-cancel>取消</button>
        </div>
      </div>`
    : `<div class="request-actions">
        <button class="button compact" type="button" data-request-action="approve"${!actionable || busy ? " disabled" : ""} aria-busy="${busy}">${busy ? "处理中…" : actionLabel}</button>
        <button class="button compact danger" type="button" data-request-action="reject"${!actionable || busy ? " disabled" : ""} aria-busy="${busy}">${busy ? "处理中…" : rejectLabel}</button>
      </div>`;
  return `<article class="request-card" data-request-id="${escapeHtml(requestId)}">
    <div class="request-meta">
      <div class="request-person"><strong>${invitation ? "邀请 Bot 加入" : escapeHtml(nickname)}</strong><span class="secondary-value">${invitation ? `邀请人 QQ ${escapeHtml(userId)}` : `QQ ${escapeHtml(userId)} · 等级 ${escapeHtml(level)}`}</span></div>
      <span class="status-badge group-badge">${escapeHtml(groupDisplayForRequest(request))}</span>
      <span class="status-badge ${statusClass}">${escapeHtml(statusLabel)}</span>
      <time class="request-time">${escapeHtml(formatTime(request.created_at))}</time>
    </div>
    <dl class="request-grid">
      <div class="request-field"><dt>Bot 平台</dt><dd>${escapeHtml(request.platform_id || "未知")}</dd></div>
      ${invitation
    ? '<div class="request-field request-invitation-note"><dt>类型</dt><dd>收到群邀请；默认不会自动接受</dd></div>'
    : `<div class="request-field"><dt>问题</dt><dd>${escapeHtml(question)}</dd></div>
      <div class="request-field"><dt>答案</dt><dd>${escapeHtml(answer)}</dd></div>`}
    </dl>
    <div class="request-footer">
      <div class="request-error" role="alert">${escapeHtml(error)}</div>
      ${actionsMarkup}
    </div>
  </article>`;
}

function renderRequests() {
  const list = $("#requests-list");
  list.innerHTML = state.requests.length
    ? state.requests.map(renderRequestCard).join("")
    : '<p class="empty-state">暂无入群申请。</p>';
  const actionable = state.requests.filter((request) => ["pending", "platform_error"].includes(String(request.status || "pending"))).length;
  $("#request-summary").textContent = `${actionable} 条待处理，共 ${state.requests.length} 条记录。`;
  updateStats();
}

function renderTargetPlatformOptions() {
  const select = $("#target-platform");
  if (!select) return;
  const current = select.value;
  const platforms = Array.from(new Map(
    [
      ...state.joinedGroups.filter((group) => group.platform_id),
      ...state.targetGroups.filter((group) => group.platform_id),
    ].map((group) => [String(group.platform_id), group]),
  ).values());
  select.innerHTML = platforms.length
    ? platforms.map((group) => `<option value="${escapeHtml(group.platform_id)}">${escapeHtml(group.platform_id)} · ${escapeHtml(group.bot_name || "Bot")}</option>`).join("")
    : '<option value="">请先刷新已加入群</option>';
  if (platforms.some((group) => String(group.platform_id) === current)) select.value = current;
}

function renderTargetGroups() {
  renderTargetPlatformOptions();
  const list = $("#target-groups-list");
  if (!list) return;
  if (!state.targetGroups.length) {
    list.innerHTML = '<p class="empty-state">暂无目标群。添加后才能接收该群审核推送或处理 Bot 邀请。</p>';
    return;
  }
  list.innerHTML = state.targetGroups.map((target) => {
    const joinedLabel = target.joined ? `已加入 · ${target.bot_role || "未知身份"}` : "尚未加入，等待邀请";
    const key = groupKey(target.platform_id, target.group_id);
    return `<div class="target-group-row" data-target-key="${escapeHtml(key)}">
      <div><strong>${escapeHtml(target.group_name || "未知群名")}</strong><span class="secondary-value">${escapeHtml(target.group_id)} · ${escapeHtml(target.platform_id)}</span></div>
      <div class="target-group-meta"><span class="status-badge ${target.joined ? "good" : "warn"}">${escapeHtml(joinedLabel)}</span><button class="button compact danger-quiet" type="button" data-remove-target>移除</button></div>
    </div>`;
  }).join("");
}

function renderInviteGroupOptions() {
  const select = $("#invite-target-group");
  if (!select) return;
  const current = select.value;
  const options = state.groups.filter((group) => group.joined && group.can_review);
  select.innerHTML = options.length
    ? options.map((group) => `<option value="${escapeHtml(groupKey(group.platform_id, group.group_id))}">${escapeHtml(group.group_name)}（${escapeHtml(group.group_id)}）</option>`).join("")
    : '<option value="">暂无可邀请的已加入群</option>';
  if (options.some((group) => groupKey(group.platform_id, group.group_id) === current)) select.value = current;
}

async function inviteTargetMember() {
  const button = $("#invite-target-member");
  if (button.getAttribute("aria-busy") === "true") return;
  const group = state.groupMap.get($("#invite-target-group").value);
  const userId = $("#invite-user-id").value.trim();
  if (!group) {
    showPageError("请选择已加入且有管理权限的目标群");
    return;
  }
  if (!validateQqId(userId)) {
    showPageError("被邀请成员 QQ 号格式不正确");
    return;
  }
  setButtonBusy(button, true, "邀请中…");
  try {
    await apiPost("target-groups/invite", {
      platform_id: group.platform_id,
      group_id: group.group_id,
      user_id: userId,
    });
    $("#invite-user-id").value = "";
    showStatus(`已向群 ${group.group_id} 发出成员邀请。`);
  } catch (error) {
    showPageError(error);
  } finally {
    setButtonBusy(button, false);
  }
}

async function loadTargetGroups() {
  const payload = await apiGet("target-groups");
  state.targetGroups = listFrom(payload, ["groups", "target_groups", "items"]);
  renderTargetGroups();
}

async function addTargetGroup() {
  const button = $("#add-target-group");
  if (button.getAttribute("aria-busy") === "true") return;
  const platformId = $("#target-platform").value.trim();
  const groupId = $("#target-group-id").value.trim();
  const groupName = $("#target-group-name").value.trim();
  if (!platformId) {
    showPageError("请先刷新并选择 Bot 平台");
    return;
  }
  if (!validateQqId(groupId)) {
    showPageError("目标群号格式不正确");
    return;
  }
  setButtonBusy(button, true, "添加中…");
  try {
    await apiPost("target-groups/add", {
      platform_id: platformId,
      group_id: groupId,
      group_name: groupName,
      enabled: true,
    });
    $("#target-group-id").value = "";
    $("#target-group-name").value = "";
    await loadTargetGroups();
    showStatus(`目标群 ${groupId} 已添加。`);
  } catch (error) {
    showPageError(error);
  } finally {
    setButtonBusy(button, false);
  }
}

async function removeTargetGroup(row) {
  const key = row?.dataset.targetKey || "";
  const target = state.targetGroups.find((item) => groupKey(item.platform_id, item.group_id) === key);
  if (!target || !window.confirm(`确认移除目标群 ${target.group_id}？移除后不会再处理该群的新邀请。`)) return;
  const button = $("[data-remove-target]", row);
  setButtonBusy(button, true, "移除中…");
  try {
    await apiPost("target-groups/remove", { platform_id: target.platform_id, group_id: target.group_id });
    await loadTargetGroups();
    showStatus(`目标群 ${target.group_id} 已移除。`);
  } catch (error) {
    showPageError(error);
  } finally {
    setButtonBusy(button, false);
  }
}

function validateQqId(value) {
  return /^[1-9][0-9]{0,19}$/.test(String(value));
}

function parseGroupIdList(value, label) {
  const items = String(value || "").split(/[,，;；\s]+/).map((item) => item.trim()).filter(Boolean);
  if (items.length > 100) throw new Error(`${label}最多 100 个`);
  for (const groupId of items) {
    if (!validateQqId(groupId)) throw new Error(`${label}“${groupId}”格式不正确`);
  }
  return Array.from(new Set(items));
}

function parseSpecifiedGroups(value) {
  return parseGroupIdList(value, "审核群号");
}

function configPayloadFromForm(container) {
  const key = container.dataset.groupKey;
  const group = state.groupMap.get(key);
  if (!group) throw new Error("群信息已变化，请刷新后重试");
  if (!group.platform_id || group.platform_id.length > 128) throw new Error("Bot 平台标识无效");
  if (!validateQqId(group.group_id)) throw new Error("群号格式不正确");
  if (!group.joined) throw new Error("Bot 当前不在该群中");
  if (!group.can_review) throw new Error("Bot 当前不具备该群的审核权限");

  const notifyTarget = $("[data-field='notify_target']", container).value;
  if (!["target_group", "specified_groups", "both"].includes(notifyTarget)) {
    throw new Error("通知位置无效");
  }
  const specifiedGroups = parseSpecifiedGroups($("[data-field='specified_group_ids']", container).value);
  if (["specified_groups", "both"].includes(notifyTarget) && specifiedGroups.length === 0) {
    throw new Error("通知到指定群时，必须填写审核群白名单");
  }
  const pushStyle = $("[data-field='push_style']", container).value;
  if (!["formatted", "natural"].includes(pushStyle)) {
    throw new Error("推送样式无效");
  }
  const joinQuestions = [];
  for (const item of $all("[data-jq-item]", container)) {
    const question = $("[data-jq-question]", item).value.trim();
    const answers = Array.from(new Set(
      $("[data-jq-answers]", item).value.split(/\n+/).map((line) => line.trim()).filter(Boolean),
    ));
    if (!question && answers.length === 0) continue; // 完全空白的条目视为未添加
    if (answers.length === 0) throw new Error("每条入群问答预设至少要有一条参考答案");
    if (answers.some((answer) => answer.length > 2048)) throw new Error("参考答案单条最长 2048 字");
    joinQuestions.push({ question, answers });
  }
  if (joinQuestions.length > 50) throw new Error("入群问答预设最多 50 条");
  return {
    platform_id: group.platform_id,
    group_id: group.group_id,
    auto_audit_enabled: $("[data-field='auto_audit_enabled']", container).checked,
    review_send_enabled: $("[data-field='review_send_enabled']", container).checked,
    notify_target: notifyTarget,
    specified_group_ids: specifiedGroups,
    include_answer: $("[data-field='include_answer']", container).checked,
    pinned: $("[data-field='pinned']", container).checked,
    push_group_ids: parseGroupIdList($("[data-field='push_group_ids']", container).value, "推送群号"),
    push_style: pushStyle,
    join_questions: joinQuestions,
  };
}

function showFormError(container, error) {
  const element = $("[data-row-error]", container);
  if (element) element.textContent = error?.message || String(error || "");
}

async function refreshStoredData() {
  const [groupsPayload, requestsPayload, targetsPayload] = await Promise.all([
    apiGet("groups"),
    apiGet("requests"),
    apiGet("target-groups"),
  ]);
  state.configuredGroups = listFrom(groupsPayload, ["groups", "configs", "group_configs"]);
  state.requests = listFrom(requestsPayload, ["requests", "items"]);
  state.targetGroups = listFrom(targetsPayload, ["groups", "target_groups", "items"]);
  state.legacyAvailable = Boolean(
    groupsPayload?.legacy_available
      ?? groupsPayload?.has_legacy_config
      ?? groupsPayload?.legacy
      ?? groupsPayload?.legacy_config,
  );
  renderGroups();
  renderInviteGroupOptions();
  renderTargetGroups();
  renderRequests();
}

async function loadAll() {
  clearPageError();
  const [joinedPayload, groupsPayload, requestsPayload, targetsPayload] = await Promise.all([
    apiGet("joined-groups"),
    apiGet("groups"),
    apiGet("requests"),
    apiGet("target-groups"),
  ]);
  state.joinedGroups = listFrom(joinedPayload, ["groups", "joined_groups", "items"]);
  state.configuredGroups = listFrom(groupsPayload, ["groups", "configs", "group_configs"]);
  state.requests = listFrom(requestsPayload, ["requests", "items"]);
  state.targetGroups = listFrom(targetsPayload, ["groups", "target_groups", "items"]);
  state.legacyAvailable = Boolean(
    groupsPayload?.legacy_available
      ?? groupsPayload?.has_legacy_config
      ?? groupsPayload?.legacy
      ?? groupsPayload?.legacy_config,
  );
  renderGroups();
  renderInviteGroupOptions();
  renderTargetGroups();
  renderRequests();
}

async function refreshJoinedGroups() {
  const button = $("#refresh-joined");
  if (button.getAttribute("aria-busy") === "true") return;
  setButtonBusy(button, true, "刷新中…");
  clearPageError();
  try {
    const payload = await apiGet("joined-groups");
    state.joinedGroups = listFrom(payload, ["groups", "joined_groups", "items"]);
    await loadTargetGroups();
    renderGroups();
    renderInviteGroupOptions();
    showStatus("已刷新当前 aiocqhttp Bot 加入的群；本次只读操作未修改配置。");
  } catch (error) {
    showPageError(error);
  } finally {
    setButtonBusy(button, false);
  }
}

// ------------------------------------------------------------------
// 全局设置：审核模型 / 知联动开关（与插件配置互通，保存立即生效）
// ------------------------------------------------------------------

async function loadSettings() {
  const select = $("#settings-audit-provider");
  const recall = $("#settings-recall");
  const errorEl = $("#settings-error");
  try {
    const data = await apiGet("settings");
    const providers = Array.isArray(data?.providers) ? data.providers : [];
    const current = String(data?.audit_llm_provider || "");
    const options = [{ id: "", label: "默认（主对话 LLM）" }];
    for (const provider of providers) {
      const id = String(provider?.id || "");
      if (id) options.push({ id, label: String(provider?.label || id) });
    }
    if (current && !options.some((option) => option.id === current)) {
      options.push({ id: current, label: `${current}（当前生效，未在列表）` });
    }
    select.innerHTML = options.map((option) =>
      `<option value="${escapeHtml(option.id)}">${escapeHtml(option.label)}</option>`
    ).join("");
    select.value = current;
    recall.checked = data?.enable_active_learner_recall === true;
    // 模型列表不可用时只保留默认与当前生效值，并给出提示。
    $("#settings-providers-hint").classList.toggle("hidden", providers.length > 0);
    errorEl.classList.add("hidden");
  } catch (error) {
    errorEl.textContent = error?.message || String(error || "读取设置失败");
    errorEl.classList.remove("hidden");
  }
}

async function saveSettings() {
  const button = $("#settings-save");
  if (button.getAttribute("aria-busy") === "true") return;
  const errorEl = $("#settings-error");
  errorEl.classList.add("hidden");
  setButtonBusy(button, true, "保存中…");
  try {
    await apiPost("settings/update", {
      audit_llm_provider: $("#settings-audit-provider").value,
      enable_active_learner_recall: $("#settings-recall").checked,
    });
    showStatus("全局设置已保存，立即生效。");
  } catch (error) {
    errorEl.textContent = error?.message || String(error || "保存失败");
    errorEl.classList.remove("hidden");
  } finally {
    setButtonBusy(button, false);
  }
}

async function saveGroup(container) {
  const key = container.dataset.groupKey;
  if (!key || state.rowBusy.has(key)) return;
  showFormError(container, "");
  let payload;
  try {
    payload = configPayloadFromForm(container);
  } catch (error) {
    showFormError(container, error);
    return;
  }
  state.rowBusy.add(key);
  renderGroups();
  try {
    await apiPost("groups/update", payload);
    await refreshStoredData();
    closeGroupPopover();
    showStatus(`群 ${payload.group_id} 的审核配置已保存。`);
  } catch (error) {
    showPageError(error);
  } finally {
    state.rowBusy.delete(key);
    renderGroups();
  }
}

function selectedGroupPayloads() {
  if (state.selected.size === 0) throw new Error("请先选择至少一个群");
  return Array.from(state.selected).map((key) => {
    const group = state.groupMap.get(key);
    if (!group || !group.platform_id || !validateQqId(group.group_id)) {
      throw new Error("所选群信息无效，请刷新后重试");
    }
    if (!group.joined || !group.can_review) {
      throw new Error(`Bot 在群 ${group.group_id} 中不具备审核权限`);
    }
    return { platform_id: group.platform_id, group_id: group.group_id };
  });
}

async function runBatch(action) {
  if (state.batchBusy) return;
  clearPageError();
  let groups;
  try {
    groups = selectedGroupPayloads();
  } catch (error) {
    showPageError(error);
    return;
  }
  state.batchBusy = true;
  updateBatchUi();
  try {
    await apiPost("groups/batch", { action, groups });
    await refreshStoredData();
    showStatus(`已完成 ${groups.length} 个群的批量配置。`);
  } catch (error) {
    showPageError(error);
  } finally {
    state.batchBusy = false;
    updateBatchUi();
  }
}

async function handleRequestAction(card, action, reason = "") {
  const requestId = card.dataset.requestId;
  if (!requestId) return;
  if (state.requestBusy.has(requestId)) return;
  rejectConfirmId = null;

  state.requestBusy.add(requestId);
  state.requestErrors.delete(requestId);
  renderRequests();
  try {
    const payload = { request_id: requestId };
    if (action === "reject" && reason) payload.reason = reason;
    await apiPost(action, payload);
    await refreshStoredData();
    const invitation = card.querySelector(".request-invitation-note") !== null
      || card.textContent.includes("邀请 Bot 加入");
    showStatus(invitation
      ? (action === "approve" ? "邀请已接受，等待 Bot 进群事件确认。" : "邀请已拒绝。")
      : (action === "approve" ? "申请已批准。" : "申请已驳回。"));
  } catch (error) {
    state.requestErrors.set(requestId, error?.message || String(error));
  } finally {
    state.requestBusy.delete(requestId);
    renderRequests();
  }
}

function syncToggleLabel(input) {
  const label = input.closest("label")?.querySelector("span");
  if (!label) return;
  const field = input.dataset.field;
  const checked = input.checked;
  if (field === "include_answer") label.textContent = checked ? "显示答案" : "隐藏答案";
  else if (field === "pinned") label.textContent = checked ? "已置顶" : "置顶";
  else if (field === "auto_audit_enabled") label.textContent = checked ? "自动审核：开启" : "自动审核：关闭";
  else if (field === "review_send_enabled") label.textContent = checked ? "发送审核：开启" : "发送审核：关闭";
}

function bindEvents() {
  $("#refresh-joined").addEventListener("click", refreshJoinedGroups);
  $("#refresh-target-groups").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (button.getAttribute("aria-busy") === "true") return;
    setButtonBusy(button, true, "刷新中…");
    try {
      await loadTargetGroups();
      showStatus("目标群列表已刷新。");
    } catch (error) {
      showPageError(error);
    } finally {
      setButtonBusy(button, false);
    }
  });
  $("#add-target-group").addEventListener("click", addTargetGroup);
  $("#invite-target-member").addEventListener("click", inviteTargetMember);
  $("#target-groups-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-target]");
    if (button) removeTargetGroup(button.closest("[data-target-key]"));
  });
  $("#simulate-run").addEventListener("click", runSimulate);
  $("#refresh-requests").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (button.getAttribute("aria-busy") === "true") return;
    setButtonBusy(button, true, "刷新中…");
    clearPageError();
    try {
      const payload = await apiGet("requests");
      state.requests = listFrom(payload, ["requests", "items"]);
      renderGroups();
      renderRequests();
    } catch (error) {
      showPageError(error);
    } finally {
      setButtonBusy(button, false);
    }
  });

  $("#select-all").addEventListener("change", (event) => {
    state.selected = event.currentTarget.checked
      ? new Set(state.groups
        .filter((group) => group.joined && group.can_review)
        .map((group) => groupKey(group.platform_id, group.group_id)))
      : new Set();
    renderGroups();
  });

  $("#groups-body").addEventListener("change", (event) => {
    const row = event.target.closest("tr[data-group-key]");
    if (!row) return;
    if (event.target.matches("[data-select-group]")) {
      if (event.target.checked) state.selected.add(row.dataset.groupKey);
      else state.selected.delete(row.dataset.groupKey);
      updateBatchUi();
    }
  });

  $("#groups-body").addEventListener("click", (event) => {
    const opener = event.target.closest("[data-open-settings]");
    if (!opener) return;
    const row = opener.closest("tr[data-group-key]");
    if (row) openGroupPopover(row.dataset.groupKey, row);
  });

  // 悬浮窗内交互：字段变更、通知位置联动、保存与关闭。
  const popover = popoverElement();
  popover.addEventListener("change", (event) => {
    if (event.target.matches("[data-field='notify_target']")) {
      const whitelist = $("[data-field='specified_group_ids']", popover);
      const group = state.groupMap.get(popover.dataset.groupKey);
      whitelist.disabled = event.target.value === "target_group" || !group?.can_review;
      showFormError(popover, "");
    }
    if (event.target.matches("[data-field='auto_audit_enabled'], [data-field='review_send_enabled'], [data-field='include_answer'], [data-field='pinned']")) {
      syncToggleLabel(event.target);
    }
  });

  popover.addEventListener("input", () => showFormError(popover, ""));

  popover.addEventListener("click", (event) => {
    if (event.target.closest("[data-jq-add]")) {
      // 增删只动 DOM，保存时才随 configPayloadFromForm 采集校验。
      const list = $("[data-jq-list]", popover);
      list.insertAdjacentHTML("beforeend", jqItemMarkup({ question: "", answers: [] }, false));
      return;
    }
    const jqRemove = event.target.closest("[data-jq-remove]");
    if (jqRemove) {
      jqRemove.closest("[data-jq-item]")?.remove();
      return;
    }
    if (event.target.closest("[data-popover-save]")) {
      saveGroup(popover);
      return;
    }
    if (event.target.closest("[data-popover-cancel]")) {
      closeGroupPopover();
    }
  });

  document.addEventListener("click", (event) => {
    if (!state.popoverKey) return;
    if (event.target.closest("#group-popover")) return;
    if (event.target.closest("[data-open-settings]")) return;
    closeGroupPopover();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.popoverKey) closeGroupPopover();
  });

  for (const button of $all("[data-batch-action]")) {
    button.addEventListener("click", () => runBatch(button.dataset.batchAction));
  }
  $("#apply-legacy").addEventListener("click", () => runBatch("apply_legacy"));
  $("#settings-save").addEventListener("click", saveSettings);

  $("#requests-list").addEventListener("click", (event) => {
    const confirmButton = event.target.closest("[data-reject-confirm]");
    if (confirmButton) {
      const card = confirmButton.closest("[data-request-id]");
      if (!card) return;
      const reason = $("[data-reject-reason]", card)?.value.trim() || "";
      handleRequestAction(card, "reject", reason);
      return;
    }
    if (event.target.closest("[data-reject-cancel]")) {
      rejectConfirmId = null;
      renderRequests();
      return;
    }
    const button = event.target.closest("[data-request-action]");
    if (!button) return;
    const card = button.closest("[data-request-id]");
    if (button.dataset.requestAction === "reject") {
      // 驳回改为行内展开原因输入，点“确认驳回”才真正提交。
      const requestId = String(card.dataset.requestId || "");
      if (!requestId || state.requestBusy.has(requestId)) return;
      rejectConfirmId = requestId;
      renderRequests();
      $(`[data-request-id="${CSS.escape(requestId)}"] [data-reject-reason]`)?.focus();
      return;
    }
    handleRequestAction(card, button.dataset.requestAction);
  });
}

async function init() {
  bindEvents();
  bridge = await resolveBridge();
  if (typeof bridge.ready === "function") {
    let timer;
    try {
      await Promise.race([
        bridge.ready(),
        new Promise((_, reject) => {
          timer = setTimeout(
            () => reject(new Error("页面通信初始化超时，可点击刷新重试")),
            5000,
          );
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }
  await loadAll();
  // 设置读取失败只影响设置卡内联提示，不阻断主页面。
  await loadSettings();
}

init().catch(showPageError);
