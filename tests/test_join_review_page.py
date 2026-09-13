import json
from pathlib import Path


PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "join_review"


def read_page(name: str) -> str:
    return (PAGE_DIR / name).read_text(encoding="utf-8")


def test_page_loads_bridge_before_application_script():
    html = read_page("index.html")
    bridge = '<script src="/api/plugin/page/bridge-sdk.js"></script>'
    app = '<script src="./app.js?v=0.8.3-1"></script>'
    assert bridge in html
    assert app in html
    assert html.index(bridge) < html.index(app)


def test_page_uses_incremented_asset_cache_busters():
    html = read_page("index.html")
    for asset in ("style.css", "series-ui.css", "series-ui.js", "app.js"):
        assert f"{asset}?v=0.8.3-1" in html


def test_page_exposes_join_review_api_contract_and_scoped_fields():
    html = read_page("index.html")
    js = read_page("app.js")
    assert 'const API_PREFIX = "join-review"' in js
    assert "bridge.apiGet(`${API_PREFIX}/${name}`)" in js
    assert "bridge.apiPost(`${API_PREFIX}/${name}`, payload)" in js
    for endpoint in (
        'apiGet("joined-groups")',
        'apiGet("target-groups")',
        'apiPost("target-groups/add"',
        'apiPost("target-groups/remove"',
        'apiPost("target-groups/invite"',
        'apiGet("groups")',
        'apiGet("requests")',
        'apiPost("groups/update"',
        'apiPost("groups/batch"',
        "const payload = { request_id: requestId };",
        "apiPost(action, payload)",
    ):
        assert endpoint in js
    for field in (
        "platform_id",
        "group_id",
        "auto_audit_enabled",
        "review_send_enabled",
        "notify_target",
        "specified_group_ids",
        "include_answer",
        "pinned",
        "push_group_ids",
        "push_style",
        "join_questions",
    ):
        assert field in js
    assert 'runBatch("apply_legacy")' in js
    assert "legacyAvailable" in js
    assert "row-pinned" in js
    assert "state.targetGroups" in js
    assert "接受邀请" in js
    assert "拒绝邀请" in js
    assert "普通群成员也可以发起邀请" in html


def test_page_does_not_render_or_submit_onebot_flag():
    html = read_page("index.html")
    js = read_page("app.js")
    assert "flag" not in html.lower()
    assert "request.flag" not in js
    assert "onebot" not in js.lower()


def test_page_has_busy_guards_and_explicit_reject_confirmation():
    js = read_page("app.js")
    assert "state.rowBusy" in js
    assert "state.batchBusy" in js
    assert "state.requestBusy" in js
    assert "if (state.requestBusy.has(requestId)) return" in js
    # 驳回需先展开行内原因输入，再点“确认驳回”才提交；原因可选并随请求上送。
    assert "rejectConfirmId" in js
    assert "data-reject-reason" in js
    assert "data-reject-confirm" in js
    assert "data-reject-cancel" in js
    assert 'handleRequestAction(card, "reject", reason)' in js
    assert "payload.reason = reason" in js
    assert 'setAttribute("aria-busy", String(busy))' in js


def test_page_validates_whitelist_and_unknown_optional_profile_fields():
    js = read_page("app.js")
    assert "function validateQqId" in js
    assert "function parseSpecifiedGroups" in js
    assert "specified_groups_required" in js
    assert 'stringValue(request.nickname) || "未知"' in js
    assert 'stringValue(request.level) || "未知"' in js
    assert '"已按群配置隐藏"' in js


def test_page_group_settings_popover():
    """群配置表瘦身为 5 列，细分设置收进点击群名打开的悬浮窗。"""
    html = read_page("index.html")
    js = read_page("app.js")
    css = read_page("style.css")
    assert 'id="group-popover"' in html
    assert 'colspan="5"' in html
    assert 'colspan="14"' not in html
    # 行内只保留群名/操作入口，编辑控件全部移入悬浮窗
    assert "data-open-settings" in js
    assert "openGroupPopover" in js
    assert "closeGroupPopover" in js
    assert "renderOpenPopover" in js
    assert "state.popoverKey" in js
    assert "configPayloadFromForm" in js
    assert "data-popover-save" in js
    assert "data-popover-cancel" in js
    assert '"Escape"' in js
    assert 'apiPost("groups/update", payload)' in js
    assert ".group-popover" in css
    assert ".group-link" in css


def test_page_has_target_group_management():
    html = read_page("index.html")
    js = read_page("app.js")
    css = read_page("style.css")
    for marker in (
        'id="target-groups-title"',
        'id="target-platform"',
        'id="target-group-id"',
        'id="add-target-group"',
        'id="target-groups-list"',
        'id="invite-target-group"',
        'id="invite-user-id"',
        'id="invite-target-member"',
        "renderTargetGroups",
        "addTargetGroup",
        "removeTargetGroup",
    ):
        assert marker in html or marker in js
    assert ".target-group-row" in css


def test_page_popover_has_focus_and_ready_failure_feedback():
    html = read_page("index.html")
    js = read_page("app.js")
    css = read_page("style.css")
    series_css = read_page("series-ui.css")
    assert 'role="dialog" aria-labelledby="group-popover-title"' in html
    assert 'aria-haspopup="dialog"' in js
    assert 'anchor?.setAttribute("aria-expanded", "true")' in js
    assert 'trigger.focus({ preventScroll: true })' in js
    assert "页面通信初始化超时，可点击刷新重试" in js
    assert "@media (hover: hover) and (pointer: fine)" in css
    assert 'href="./series-ui.css?v=' in html
    assert "body[data-series-ui] button:active" in series_css
    assert "transform: translateY(0)" in series_css


def test_popover_has_join_question_preset_editor():
    """悬浮窗内的入群问答预设编辑区：增删条目、问题可留空、答案每行一个。"""
    js = read_page("app.js")
    css = read_page("style.css")
    assert "data-jq-list" in js
    assert "data-jq-item" in js
    assert "data-jq-question" in js
    assert "data-jq-answers" in js
    assert "data-jq-add" in js
    assert "data-jq-remove" in js
    assert "jqItemMarkup" in js
    assert "join_questions: joinQuestions" in js
    assert "每条入群问答预设至少要有一条参考答案" in js
    assert ".jq-item" in css


def test_page_has_simulate_card():
    """模拟申请卡片：选群、问题、答案、开始测试按钮与结果区。"""
    html = read_page("index.html")
    js = read_page("app.js")
    css = read_page("style.css")
    for marker in (
        'id="simulate-group"',
        'id="simulate-question"',
        'id="simulate-answer"',
        'id="simulate-run"',
        'id="simulate-result"',
        'id="simulate-error"',
    ):
        assert marker in html
    assert 'apiPost("simulate"' in js
    assert "renderSimulateGroupOptions" in js
    assert "renderSimulateResult" in js
    assert "SIMULATE_WOULD_LABELS" in js
    assert "runSimulate" in js
    assert ".simulate-result" in css


def test_page_simulate_result_renders_push_preview():
    """模拟结果含推送文案预览块：样式徽章、原文、人格/LLM/群消息元信息。"""
    js = read_page("app.js")
    css = read_page("style.css")
    for marker in (
        "renderSimulatePreview",
        "SIMULATE_PREVIEW_STYLE_BADGES",
        "SIMULATE_OPINION_SOURCE_LABELS",
        "push_preview",
        "opinion_source",
        "natural_fallback_formatted",
        "推送文案预览",
        "看法：LLM 生成",
        "看法：自动审核结论",
        "该申请不会触发推送，无推送与结果回复预览",
        "仅预览，未发送",
    ):
        assert marker in js
    assert ".simulate-preview" in css
    assert ".simulate-preview-text" in css
    assert "pre-wrap" in css


def test_page_simulate_result_renders_result_reply_preview():
    """模拟结果含审批结果回复预览块：若同意/若拒绝两行，回退带模板标注。"""
    js = read_page("app.js")
    css = read_page("style.css")
    for marker in (
        "renderSimulateResultReplyPreview",
        "result_reply_preview",
        "结果回复预览",
        "若同意：",
        "若拒绝：",
        "模板",
    ):
        assert marker in js
    assert ".simulate-reply-preview" in css


def test_page_has_settings_card():
    """全局设置卡片：审核模型下拉、知联动开关、保存按钮与错误条。"""
    html = read_page("index.html")
    js = read_page("app.js")
    for marker in (
        'id="settings-title"',
        'id="settings-audit-provider"',
        'id="settings-recall"',
        'id="settings-save"',
        'id="settings-error"',
        'id="settings-providers-hint"',
        "默认（主对话 LLM）",
    ):
        assert marker in html
    # 设置卡在群配置卡之前
    assert html.index('id="settings-title"') < html.index('id="groups-title"')
    for marker in (
        'apiGet("settings"',
        'apiPost("settings/update"',
        "loadSettings",
        "saveSettings",
        "当前生效，未在列表",
        "全局设置已保存，立即生效。",
    ):
        assert marker in js


def test_mobile_layout_uses_non_overlapping_labeled_rows():
    css = read_page("style.css")
    assert "@media (max-width: 900px)" in css
    assert ".data-table td::before" in css
    assert "content: attr(data-label)" in css
    assert ".request-grid" in css
    assert "grid-template-columns: 1fr" in css
    assert "overflow-wrap: anywhere" in css
    assert "prefers-reduced-motion" in css


def test_bridge_timeout_timer_is_released():
    js = read_page("app.js")
    assert "let timer;" in js
    assert "clearTimeout(timer);" in js


def test_page_has_dashboard_i18n_metadata():
    plugin_root = PAGE_DIR.parents[1]
    metadata = json.loads(
        (plugin_root / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8")
    )
    page = metadata["pages"]["join_review"]
    assert page["title"] == "入群审核"
    assert page["description"]


def test_groups_table_matches_current_five_column_model():
    css = read_page("style.css")
    assert "min-width: 1220px" not in css
    assert css.count(".groups-table th:nth-child(") == 5
    assert "th:nth-child(6)" not in css
    assert css.index("@media (max-width: 780px)") < css.index("@media (max-width: 560px)")


def test_page_uses_top_level_tabs_to_reduce_height():
    html = read_page("index.html")
    js = read_page("app.js")
    css = read_page("style.css")
    assert html.count('role="tab" aria-selected') == 4
    assert html.count('role="tabpanel"') == 4
    for panel in ("pending", "groups", "targets", "settings"):
        assert f'data-page-tab="{panel}"' in html
        assert f'data-page-panel="{panel}"' in html
    assert "function switchPageTab(" in js
    assert "function bindPageTabs(" in js
    assert "bindPageTabs();" in js
    assert ".page-tabs" in css
    assert ".page-panel[hidden]" in css


def test_remove_target_group_uses_shared_confirmation():
    js = read_page("app.js")
    assert "window.confirm" not in js
    assert "window.SeriesUI.confirm" in js
    assert 'title: "移除目标群"' in js


def test_large_lists_use_client_side_progressive_rendering():
    js = read_page("app.js")
    css = read_page("style.css")
    assert "const PAGE_SIZE = window.matchMedia" in js
    assert "const progressiveState = { requests: PAGE_SIZE, history: PAGE_SIZE, groups: PAGE_SIZE, targets: PAGE_SIZE };" in js
    assert "function progressiveFooter(" in js
    assert "function handleProgressiveClick(" in js
    assert "data-show-more" in js
    assert "data-collapse" in js
    assert "全选作用于全部群" in js
    assert "filteredRequests" in js
    assert "request-status-filter" in js
    assert ".progressive-actions" in css
    assert ".progressive-row td" in css


def test_pending_requests_use_dual_audit_columns_and_summary_filters():
    html = read_page("index.html")
    js = read_page("app.js")
    css = read_page("style.css")

    assert 'class="audit-columns"' in html
    assert 'id="requests-list"' in html
    assert 'id="history-requests-list"' in html
    assert "已处理（今天）" in html
    assert 'id="request-history-mode"' in html
    for chip in ("pending", "approved", "rejected", "platform_error"):
        assert f'data-request-chip="{chip}"' in html
    for status in ("approved", "rejected", "expired"):
        assert f'<option value="{status}">' in html
    assert "function renderHistoryItem(request)" in js
    assert "request.review_reason" in js
    assert "request.processed_at" in js
    assert "function updateRequestChips()" in js
    assert "requestHistoryAll" in js
    assert ".audit-columns" in css
    assert ".audit-history-item" in css


def test_targets_and_groups_have_filters_and_reset_on_full_load():
    js = read_page("app.js")
    html = read_page("index.html")

    # 目标群筛选
    assert 'id="target-status-filter"' in html
    assert 'id="target-search"' in html
    assert "function filteredTargetGroups()" in js
    assert 'targetStatusFilter === "joined"' in js
    assert 'targetStatusFilter === "pending"' in js
    assert "const filtered = filteredTargetGroups();" in js
    assert "没有匹配的目标群" in js

    # 完整加载时筛选条件与输入框一起复位
    load_all = js.split("async function loadAll()", 1)[1].split("async function refreshJoinedGroups", 1)[0]
    assert 'targetStatusFilter = "";' in load_all
    assert 'targetQuery = "";' in load_all
    assert 'groupStatusFilter = "";' in load_all
    assert 'groupSearchField.value = "";' in load_all
    assert 'targetSearchField.value = "";' in load_all

    # 变更筛选后回到第一页
    assert 'progressiveState.targets = PAGE_SIZE;' in js
    assert 'progressiveState.groups = PAGE_SIZE;' in js
    assert ".filter-note" in read_page("style.css")


def test_status_and_errors_prefer_shared_series_toast():
    js = read_page("app.js")
    assert 'window.SeriesUI?.toast' in js
    assert 'window.SeriesUI.toast(message, "error")' in js
    assert 'window.SeriesUI.toast(message, "info")' in js
    assert 'id="page-error"' in read_page("index.html")


def test_mobile_top_metrics_are_compacted_into_status_strip():
    css = read_page("style.css")
    narrow = css[css.index("@media (max-width: 560px)") : css.index("@media (prefers-reduced-motion: reduce)")]

    assert ".cards {" in narrow
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in narrow
    assert ".metric strong { font-size: 20px; }" in narrow
    assert ".cards { grid-template-columns: 1fr; }" not in narrow


def test_pending_requests_support_keyboard_quick_actions():
    js = read_page("app.js")
    html = read_page("index.html")
    css = read_page("style.css")

    assert "let keyboardRequestId" in js
    assert "function visibleRequestCards()" in js
    assert "function highlightRequestCard(requestId" in js
    assert "function handleRequestKeyboard(event)" in js
    assert 'document.addEventListener("keydown", handleRequestKeyboard);' in js
    # 只在待审面板可见、且焦点不在输入控件时生效
    assert 'document.getElementById("page-panel-pending")' in js
    assert 'target.closest?.("input, textarea, select")' in js
    assert 'if (!["j", "k", "a", "d"].includes(key)) return;' in js
    assert "event.preventDefault();" in js
    # A 走既有批准按钮，D 走既有行内驳回流程，不新增接口
    assert '\'[data-request-action="approve"]\'' in js
    assert '\'[data-request-action="reject"]\'' in js
    assert "button.click();" in js
    assert "highlightRequestCard(keyboardRequestId, { scroll: false });" in js
    assert "keyboard-hint" in html
    assert ".is-keyboard-current" in css
