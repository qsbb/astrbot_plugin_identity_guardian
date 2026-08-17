"use strict";

let bridge = null;
const API_PREFIX = "join-review";

const state = {
  joinedGroups: [],
  configuredGroups: [],
  requests: [],
  groups: [],
  groupMap: new Map(),
  selected: new Set(),
  rowBusy: new Set(),
  requestBusy: new Set(),
  requestErrors: new Map(),
  batchBusy: false,
  legacyAvailable: false,
};

const API_ERROR_MESSAGES = {
  unauthorized: "当前 Dashboard 会话无权执行此操作",
  forbidden: "当前 Bot 或账号没有该群的审核权限",
  invalid_group_id: "群号格式不正确",
  invalid_platform_id: "Bot 平台标识无效",
  group_not_joined: "Bot 当前不在该群中",
  insufficient_permission: "Bot 当前不具备入群审核权限",
  specified_groups_required: "通知到指定群时，必须填写审核群白名单",
  duplicate_group_config: "批量配置中包含重复群",
  expired: "该申请已过期，不能继续处理",
  already_processed: "该申请已由其他管理员处理",
  busy: "该申请正在被其他管理员处理",
  platform_error: "平台操作失败，申请仍未完成",
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
    left.platform_id.localeCompare(right.platform_id, "zh-CN")
      || Number(left.group_id) - Number(right.group_id)
  ));
  state.groupMap = new Map(state.groups.map((group) => [groupKey(group.platform_id, group.group_id), group]));
  state.selected = new Set(Array.from(state.selected).filter((key) => {
    const group = state.groupMap.get(key);
    return group?.joined && group?.can_review;
  }));
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
  const notificationDisabled = group.notify_target === "target_group" || fieldsDisabled;
  const selected = state.selected.has(key);
  const botLabel = group.bot_name || (group.bot_id ? `QQ ${group.bot_id}` : group.platform_id);
  return `<tr data-group-key="${escapeHtml(key)}" class="${unavailable ? "row-disabled" : ""}">
    <td class="select-cell" data-label="选择"><input type="checkbox" data-select-group aria-label="选择群 ${escapeHtml(group.group_id)}"${selected ? " checked" : ""}${fieldsDisabled ? " disabled" : ""}></td>
    <td class="group-cell" data-label="群"><span class="primary-value">${escapeHtml(group.group_name)}</span><span class="secondary-value">${escapeHtml(group.group_id)}</span></td>
    <td data-label="Bot / 权限"><span class="primary-value">${escapeHtml(botLabel)}</span><span class="secondary-value">${escapeHtml(group.bot_role)} · ${escapeHtml(group.platform_id)}</span>${permissionBadge(group)}</td>
    <td data-label="配置">${group.configured ? '<span class="status-badge good">已配置</span>' : '<span class="status-badge">未配置</span>'}</td>
    <td data-label="自动审核">${switchMarkup("auto_audit_enabled", group.auto_audit_enabled, fieldsDisabled, group.auto_audit_enabled ? "开启" : "关闭")}</td>
    <td data-label="发送审核">${switchMarkup("review_send_enabled", group.review_send_enabled, fieldsDisabled, group.review_send_enabled ? "开启" : "关闭")}</td>
    <td data-label="通知位置"><select class="inline-select editable-control" data-field="notify_target"${fieldsDisabled ? " disabled" : ""}>
      <option value="target_group"${group.notify_target === "target_group" ? " selected" : ""}>申请所属群</option>
      <option value="specified_groups"${group.notify_target === "specified_groups" ? " selected" : ""}>指定审核群</option>
      <option value="both"${group.notify_target === "both" ? " selected" : ""}>两边发送</option>
    </select></td>
    <td data-label="指定审核群白名单"><input class="group-whitelist editable-control" data-field="specified_group_ids" type="text" inputmode="numeric" maxlength="2099" placeholder="群号，逗号分隔" value="${escapeHtml(group.specified_group_ids.join(", "))}"${notificationDisabled ? " disabled" : ""}><span class="field-error" data-row-error></span></td>
    <td data-label="显示答案">${switchMarkup("include_answer", group.include_answer, fieldsDisabled, group.include_answer ? "显示" : "隐藏")}</td>
    <td data-label="待审"><span class="status-badge ${group.pending_count ? "warn" : ""}">${group.pending_count}</span></td>
    <td data-label="操作"><button class="button compact" type="button" data-save-group${fieldsDisabled ? " disabled" : ""} aria-busy="${busy}">${busy ? "保存中…" : "保存"}</button></td>
  </tr>`;
}

function renderGroups() {
  mergeGroups();
  const body = $("#groups-body");
  body.innerHTML = state.groups.length
    ? state.groups.map(renderGroupRow).join("")
    : '<tr class="empty-row"><td colspan="11">当前 aiocqhttp Bot 暂无可显示的群，请刷新已加入群。</td></tr>';
  const configuredCount = state.groups.filter((group) => group.configured).length;
  $("#group-summary").textContent = `共 ${state.groups.length} 个群，${configuredCount} 个已配置；未配置群默认关闭两个开关。`;
  $("#legacy-notice").classList.toggle("hidden", !state.legacyAvailable);
  updateBatchUi();
}

function updateBatchUi() {
  const selectedCount = state.selected.size;
  const availableCount = state.groups.filter((group) => group.joined && group.can_review).length;
  $("#selected-count").textContent = `已选择 ${selectedCount} 个群`;
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
  const name = stringValue(request.group_name, group?.group_name) || "未知群名";
  return `${name}（${request.group_id || "未知群号"}）`;
}

function renderRequestCard(request) {
  const requestId = String(request.request_id || "");
  const busy = state.requestBusy.has(requestId);
  const actionable = ["pending", "platform_error"].includes(String(request.status || "pending"));
  const [statusLabel, statusClass] = requestStatus(request.status || "pending");
  const nickname = stringValue(request.nickname) || "未知";
  const userId = stringValue(request.user_id) || "未知";
  const level = stringValue(request.level) || "未知";
  const question = stringValue(request.question) || "未提供问题";
  const answer = hasOwn(request, "answer") ? (stringValue(request.answer) || "未填写") : "已按群配置隐藏";
  const error = state.requestErrors.get(requestId) || "";
  return `<article class="request-card" data-request-id="${escapeHtml(requestId)}">
    <div class="request-meta">
      <div class="request-person"><strong>${escapeHtml(nickname)}</strong><span class="secondary-value">QQ ${escapeHtml(userId)} · 等级 ${escapeHtml(level)}</span></div>
      <span class="status-badge ${statusClass}">${escapeHtml(statusLabel)}</span>
      <span class="status-badge">${escapeHtml(groupDisplayForRequest(request))}</span>
      <time class="request-time">${escapeHtml(formatTime(request.created_at))}</time>
    </div>
    <dl class="request-grid">
      <div class="request-field"><dt>Bot 平台</dt><dd>${escapeHtml(request.platform_id || "未知")}</dd></div>
      <div class="request-field"><dt>问题</dt><dd>${escapeHtml(question)}</dd></div>
      <div class="request-field"><dt>答案</dt><dd>${escapeHtml(answer)}</dd></div>
    </dl>
    <div class="request-footer">
      <div class="request-error" role="alert">${escapeHtml(error)}</div>
      <div class="request-actions">
        <button class="button compact" type="button" data-request-action="approve"${!actionable || busy ? " disabled" : ""} aria-busy="${busy}">${busy ? "处理中…" : "批准"}</button>
        <button class="button compact danger" type="button" data-request-action="reject"${!actionable || busy ? " disabled" : ""} aria-busy="${busy}">${busy ? "处理中…" : "驳回"}</button>
      </div>
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
}

function validateQqId(value) {
  return /^[1-9][0-9]{0,19}$/.test(String(value));
}

function parseSpecifiedGroups(value) {
  const items = String(value || "").split(/[,，;；\s]+/).map((item) => item.trim()).filter(Boolean);
  if (items.length > 100) throw new Error("指定审核群最多 100 个");
  for (const groupId of items) {
    if (!validateQqId(groupId)) throw new Error(`审核群号“${groupId}”格式不正确`);
  }
  return Array.from(new Set(items));
}

function configPayloadFromRow(row) {
  const key = row.dataset.groupKey;
  const group = state.groupMap.get(key);
  if (!group) throw new Error("群信息已变化，请刷新后重试");
  if (!group.platform_id || group.platform_id.length > 128) throw new Error("Bot 平台标识无效");
  if (!validateQqId(group.group_id)) throw new Error("群号格式不正确");
  if (!group.joined) throw new Error("Bot 当前不在该群中");
  if (!group.can_review) throw new Error("Bot 当前不具备该群的审核权限");

  const notifyTarget = $("[data-field='notify_target']", row).value;
  if (!["target_group", "specified_groups", "both"].includes(notifyTarget)) {
    throw new Error("通知位置无效");
  }
  const specifiedGroups = parseSpecifiedGroups($("[data-field='specified_group_ids']", row).value);
  if (["specified_groups", "both"].includes(notifyTarget) && specifiedGroups.length === 0) {
    throw new Error("通知到指定群时，必须填写审核群白名单");
  }
  return {
    platform_id: group.platform_id,
    group_id: group.group_id,
    auto_audit_enabled: $("[data-field='auto_audit_enabled']", row).checked,
    review_send_enabled: $("[data-field='review_send_enabled']", row).checked,
    notify_target: notifyTarget,
    specified_group_ids: specifiedGroups,
    include_answer: $("[data-field='include_answer']", row).checked,
  };
}

function showRowError(row, error) {
  const element = $("[data-row-error]", row);
  if (element) element.textContent = error?.message || String(error || "");
}

async function refreshStoredData() {
  const [groupsPayload, requestsPayload] = await Promise.all([
    apiGet("groups"),
    apiGet("requests"),
  ]);
  state.configuredGroups = listFrom(groupsPayload, ["groups", "configs", "group_configs"]);
  state.requests = listFrom(requestsPayload, ["requests", "items"]);
  state.legacyAvailable = Boolean(
    groupsPayload?.legacy_available
      ?? groupsPayload?.has_legacy_config
      ?? groupsPayload?.legacy
      ?? groupsPayload?.legacy_config,
  );
  renderGroups();
  renderRequests();
}

async function loadAll() {
  clearPageError();
  const [joinedPayload, groupsPayload, requestsPayload] = await Promise.all([
    apiGet("joined-groups"),
    apiGet("groups"),
    apiGet("requests"),
  ]);
  state.joinedGroups = listFrom(joinedPayload, ["groups", "joined_groups", "items"]);
  state.configuredGroups = listFrom(groupsPayload, ["groups", "configs", "group_configs"]);
  state.requests = listFrom(requestsPayload, ["requests", "items"]);
  state.legacyAvailable = Boolean(
    groupsPayload?.legacy_available
      ?? groupsPayload?.has_legacy_config
      ?? groupsPayload?.legacy
      ?? groupsPayload?.legacy_config,
  );
  renderGroups();
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
    renderGroups();
    showStatus("已刷新当前 aiocqhttp Bot 加入的群；本次只读操作未修改配置。");
  } catch (error) {
    showPageError(error);
  } finally {
    setButtonBusy(button, false);
  }
}

async function saveGroup(row) {
  const key = row.dataset.groupKey;
  if (!key || state.rowBusy.has(key)) return;
  showRowError(row, "");
  let payload;
  try {
    payload = configPayloadFromRow(row);
  } catch (error) {
    showRowError(row, error);
    return;
  }
  state.rowBusy.add(key);
  renderGroups();
  try {
    await apiPost("groups/update", payload);
    await refreshStoredData();
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

async function handleRequestAction(card, action) {
  const requestId = card.dataset.requestId;
  if (!requestId) return;
  if (state.requestBusy.has(requestId)) return;
  if (action === "reject" && !window.confirm("确定驳回这条入群申请吗？平台成功后状态才会改变。")) return;

  state.requestBusy.add(requestId);
  state.requestErrors.delete(requestId);
  renderRequests();
  try {
    await apiPost(action, { request_id: requestId });
    await refreshStoredData();
    showStatus(action === "approve" ? "申请已批准。" : "申请已驳回。");
  } catch (error) {
    state.requestErrors.set(requestId, error?.message || String(error));
  } finally {
    state.requestBusy.delete(requestId);
    renderRequests();
  }
}

function bindEvents() {
  $("#refresh-joined").addEventListener("click", refreshJoinedGroups);
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
      return;
    }
    if (event.target.matches("[data-field='notify_target']")) {
      const whitelist = $("[data-field='specified_group_ids']", row);
      const group = state.groupMap.get(row.dataset.groupKey);
      whitelist.disabled = event.target.value === "target_group" || !group?.can_review;
      showRowError(row, "");
    }
    if (event.target.matches("[data-field='auto_audit_enabled'], [data-field='review_send_enabled'], [data-field='include_answer']")) {
      const label = event.target.closest("label")?.querySelector("span");
      if (label) {
        const field = event.target.dataset.field;
        label.textContent = field === "include_answer"
          ? (event.target.checked ? "显示" : "隐藏")
          : (event.target.checked ? "开启" : "关闭");
      }
    }
  });

  $("#groups-body").addEventListener("input", (event) => {
    const row = event.target.closest("tr[data-group-key]");
    if (row) showRowError(row, "");
  });

  $("#groups-body").addEventListener("click", (event) => {
    const button = event.target.closest("[data-save-group]");
    if (button) saveGroup(button.closest("tr[data-group-key]"));
  });

  for (const button of $all("[data-batch-action]")) {
    button.addEventListener("click", () => runBatch(button.dataset.batchAction));
  }
  $("#apply-legacy").addEventListener("click", () => runBatch("apply_legacy"));

  $("#requests-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-request-action]");
    if (!button) return;
    const card = button.closest("[data-request-id]");
    handleRequestAction(card, button.dataset.requestAction);
  });
}

async function init() {
  bindEvents();
  bridge = await resolveBridge();
  if (typeof bridge.ready === "function") await bridge.ready();
  await loadAll();
}

init().catch(showPageError);
