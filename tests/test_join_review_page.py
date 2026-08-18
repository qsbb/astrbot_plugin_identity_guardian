import json
from pathlib import Path


PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "join_review"


def read_page(name: str) -> str:
    return (PAGE_DIR / name).read_text(encoding="utf-8")


def test_page_loads_bridge_before_application_script():
    html = read_page("index.html")
    bridge = '<script src="/api/plugin/page/bridge-sdk.js"></script>'
    app = '<script src="./app.js"></script>'
    assert bridge in html
    assert app in html
    assert html.index(bridge) < html.index(app)


def test_page_exposes_join_review_api_contract_and_scoped_fields():
    js = read_page("app.js")
    assert 'const API_PREFIX = "join-review"' in js
    assert "bridge.apiGet(`${API_PREFIX}/${name}`)" in js
    assert "bridge.apiPost(`${API_PREFIX}/${name}`, payload)" in js
    for endpoint in (
        'apiGet("joined-groups")',
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


def test_mobile_layout_uses_non_overlapping_labeled_rows():
    css = read_page("style.css")
    assert "@media (max-width: 900px)" in css
    assert ".data-table td::before" in css
    assert "content: attr(data-label)" in css
    assert ".request-grid" in css
    assert "grid-template-columns: 1fr" in css
    assert "overflow-wrap: anywhere" in css
    assert "prefers-reduced-motion" in css


def test_page_has_dashboard_i18n_metadata():
    plugin_root = PAGE_DIR.parents[1]
    metadata = json.loads(
        (plugin_root / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8")
    )
    page = metadata["pages"]["join_review"]
    assert page["title"] == "入群审核"
    assert page["description"]
