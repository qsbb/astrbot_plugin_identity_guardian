"""Persistent per-group settings and pending join-review requests.

The store intentionally separates its internal request representation (which
contains the OneBot ``flag``) from the public projection returned to Plugin
Pages.  Callers must use :meth:`JoinRequest.to_public_dict` for any response.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

STORE_VERSION = 1
DEFAULT_STORE_FILENAME = "join_review.json"
NOTIFICATION_TARGETS = frozenset({"target_group", "specified_groups", "both"})
REQUEST_STATUSES = frozenset(
    {"pending", "approved", "rejected", "expired", "platform_error"}
)
ACTIONABLE_STATUSES = frozenset({"pending", "platform_error"})
FINAL_STATUSES = frozenset({"approved", "rejected", "expired"})
MAX_PLATFORM_ID_LENGTH = 128
MAX_TEXT_LENGTH = 2048
MAX_NICKNAME_LENGTH = 128
MAX_LEVEL_LENGTH = 64
MAX_FLAG_LENGTH = 4096
MAX_ERROR_LENGTH = 512
MAX_SPECIFIED_GROUPS = 100
PUSH_STYLES = frozenset({"formatted", "natural"})
MAX_PUSH_REFS = 200
MAX_MESSAGE_ID_LENGTH = 64
MAX_JOIN_QUESTIONS = 50
MAX_JOIN_QUESTION_ANSWERS = 50

_DECIMAL_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")


class JoinReviewStoreError(RuntimeError):
    """Base error raised by the join-review store."""


class ValidationError(JoinReviewStoreError, ValueError):
    """Input failed strict storage validation."""


class RequestNotActionable(JoinReviewStoreError):
    """The request is absent, expired, finalized, or already being processed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normalize_string(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    required: bool = True,
    allow_multiline: bool = False,
) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValidationError(f"invalid_{field_name}")
    normalized = str(value).strip()
    if required and not normalized:
        raise ValidationError(f"invalid_{field_name}")
    if len(normalized) > maximum:
        raise ValidationError(f"{field_name}_too_long")
    if any(
        ord(char) < 0x20 and (not allow_multiline or char not in "\n\t")
        for char in normalized
    ):
        raise ValidationError(f"invalid_{field_name}")
    return normalized


def normalize_platform_id(value: Any) -> str:
    return _normalize_string(value, "platform_id", maximum=MAX_PLATFORM_ID_LENGTH)


def normalize_qq_id(value: Any, field_name: str = "group_id") -> str:
    normalized = _normalize_string(value, field_name, maximum=20)
    if not _DECIMAL_ID_RE.fullmatch(normalized):
        raise ValidationError(f"invalid_{field_name}")
    return normalized


def _normalize_optional_text(value: Any, field_name: str, maximum: int) -> str:
    if value is None:
        return ""
    return _normalize_string(
        value,
        field_name,
        maximum=maximum,
        required=False,
        allow_multiline=True,
    )


def _normalize_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"invalid_{field_name}")
    return value


def _normalize_timestamp(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValidationError(f"invalid_{field_name}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid_{field_name}") from exc
    if result < 0 or result != result:
        raise ValidationError(f"invalid_{field_name}")
    return result


def _normalize_join_questions(value: Any) -> tuple[dict[str, Any], ...]:
    """Validate per-group join-question presets: [{question, answers}].

    question 允许空串（匹配任意入群问题）；answers 去空去重，至少一条。
    """
    if isinstance(value, (str, bytes)):
        raise ValidationError("invalid_join_questions")
    try:
        raw_items = list(value)
    except TypeError as exc:
        raise ValidationError("invalid_join_questions") from exc
    if len(raw_items) > MAX_JOIN_QUESTIONS:
        raise ValidationError("too_many_join_questions")
    items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise ValidationError("invalid_join_question")
        question = _normalize_optional_text(
            item.get("question"), "join_question", MAX_TEXT_LENGTH
        )
        raw_answers = item.get("answers", ())
        if isinstance(raw_answers, (str, bytes)):
            raise ValidationError("invalid_join_question_answers")
        try:
            answer_values = list(raw_answers)
        except TypeError as exc:
            raise ValidationError("invalid_join_question_answers") from exc
        stripped = [str(a).strip() for a in answer_values if str(a).strip()]
        for answer in stripped:
            if len(answer) > MAX_TEXT_LENGTH:
                raise ValidationError("join_question_answer_too_long")
        answers = tuple(dict.fromkeys(stripped))
        if not answers:
            raise ValidationError("join_question_answers_required")
        if len(answers) > MAX_JOIN_QUESTION_ANSWERS:
            raise ValidationError("too_many_join_question_answers")
        items.append({"question": question, "answers": answers})
    return tuple(items)


def _normalize_push_refs(value: Any) -> tuple[dict[str, str], ...]:
    """Validate push-message refs: iterable of {group_id, message_id} mappings."""
    if isinstance(value, (str, bytes)):
        raise ValidationError("invalid_push_refs")
    try:
        raw_refs = list(value)
    except TypeError as exc:
        raise ValidationError("invalid_push_refs") from exc
    if len(raw_refs) > MAX_PUSH_REFS:
        raise ValidationError("too_many_push_refs")
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_refs:
        if not isinstance(item, Mapping):
            raise ValidationError("invalid_push_ref")
        group = normalize_qq_id(item.get("group_id"), "push_ref_group_id")
        message_id = _normalize_string(
            item.get("message_id"), "push_ref_message_id", maximum=MAX_MESSAGE_ID_LENGTH
        )
        key = (group, message_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"group_id": group, "message_id": message_id})
    return tuple(refs)


@dataclass(frozen=True, slots=True)
class GroupReviewConfig:
    """Configuration scoped by one platform instance and one QQ group."""

    platform_id: str
    group_id: str
    auto_audit_enabled: bool = False
    review_send_enabled: bool = False
    notify_target: str = "target_group"
    specified_group_ids: tuple[str, ...] = ()
    include_answer: bool = True
    pinned: bool = False
    push_group_ids: tuple[str, ...] = ()
    push_style: str = "natural"
    # 按群入群问答预设：{"question": str(可空=任意问题), "answers": tuple[str, ...]}
    join_questions: tuple[dict[str, Any], ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0
    configured: bool = True

    @classmethod
    def default(cls, platform_id: Any, group_id: Any) -> GroupReviewConfig:
        """Build a non-persistent, both-switches-off configuration view."""
        return cls(
            platform_id=normalize_platform_id(platform_id),
            group_id=normalize_qq_id(group_id),
            configured=False,
        )

    @classmethod
    def create(
        cls,
        *,
        platform_id: Any,
        group_id: Any,
        auto_audit_enabled: Any = False,
        review_send_enabled: Any = False,
        notify_target: Any = "target_group",
        specified_group_ids: Iterable[Any] = (),
        include_answer: Any = True,
        pinned: Any = False,
        push_group_ids: Iterable[Any] = (),
        push_style: Any = "natural",
        join_questions: Iterable[Any] = (),
        created_at: Any = 0.0,
        updated_at: Any = 0.0,
        configured: bool = True,
    ) -> GroupReviewConfig:
        platform = normalize_platform_id(platform_id)
        group = normalize_qq_id(group_id)
        auto_enabled = _normalize_bool(auto_audit_enabled, "auto_audit_enabled")
        send_enabled = _normalize_bool(review_send_enabled, "review_send_enabled")
        answer_enabled = _normalize_bool(include_answer, "include_answer")
        target = _normalize_string(notify_target, "notify_target", maximum=32)
        if target not in NOTIFICATION_TARGETS:
            raise ValidationError("invalid_notify_target")
        if isinstance(specified_group_ids, (str, bytes)):
            raise ValidationError("invalid_specified_group_ids")
        try:
            raw_groups = list(specified_group_ids)
        except TypeError as exc:
            raise ValidationError("invalid_specified_group_ids") from exc
        if len(raw_groups) > MAX_SPECIFIED_GROUPS:
            raise ValidationError("too_many_specified_groups")
        groups = tuple(
            dict.fromkeys(
                normalize_qq_id(item, "specified_group_id") for item in raw_groups
            )
        )
        if target in {"specified_groups", "both"} and not groups:
            raise ValidationError("specified_groups_required")
        if isinstance(push_group_ids, (str, bytes)):
            raise ValidationError("invalid_push_group_ids")
        try:
            raw_push_groups = list(push_group_ids)
        except TypeError as exc:
            raise ValidationError("invalid_push_group_ids") from exc
        if len(raw_push_groups) > MAX_SPECIFIED_GROUPS:
            raise ValidationError("too_many_push_groups")
        push_groups = tuple(
            dict.fromkeys(
                normalize_qq_id(item, "push_group_id") for item in raw_push_groups
            )
        )
        style = _normalize_string(push_style, "push_style", maximum=32)
        if style not in PUSH_STYLES:
            raise ValidationError("invalid_push_style")
        return cls(
            platform_id=platform,
            group_id=group,
            auto_audit_enabled=auto_enabled,
            review_send_enabled=send_enabled,
            notify_target=target,
            specified_group_ids=groups,
            include_answer=answer_enabled,
            pinned=_normalize_bool(pinned, "pinned"),
            push_group_ids=push_groups,
            push_style=style,
            join_questions=_normalize_join_questions(join_questions),
            created_at=_normalize_timestamp(created_at, "created_at"),
            updated_at=_normalize_timestamp(updated_at, "updated_at"),
            configured=bool(configured),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["specified_group_ids"] = list(self.specified_group_ids)
        value["push_group_ids"] = list(self.push_group_ids)
        value["join_questions"] = [
            {"question": item["question"], "answers": list(item["answers"])}
            for item in self.join_questions
        ]
        return value

    @property
    def notification_target(self) -> str:
        """Descriptive alias used by Page/API adapters."""
        return self.notify_target

    @property
    def specified_groups(self) -> tuple[str, ...]:
        """Descriptive alias for the immutable notification allowlist."""
        return self.specified_group_ids


@dataclass(frozen=True, slots=True)
class JoinRequest:
    """Internal request model. Never serialize this object directly to a Page."""

    request_id: str
    platform_id: str
    group_id: str
    user_id: str
    nickname: str
    level: str
    question: str
    answer: str
    sub_type: str
    flag: str = field(repr=False)
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    status: str = "pending"
    platform_error: str = ""
    notified_targets: tuple[str, ...] = ()
    # 推送消息映射：{"group_id": 推送群, "message_id": 推送消息 ID}，
    # 供群内引用回复审批定位申请；随申请本身过期（expires_at）。
    push_refs: tuple[dict[str, str], ...] = ()

    @classmethod
    def create(
        cls,
        *,
        request_id: Any | None = None,
        platform_id: Any,
        group_id: Any,
        user_id: Any,
        nickname: Any = "",
        level: Any = "",
        question: Any = "",
        answer: Any = "",
        sub_type: Any = "add",
        flag: Any,
        created_at: Any | None = None,
        updated_at: Any | None = None,
        expires_at: Any,
        status: Any = "pending",
        platform_error: Any = "",
        notified_targets: Iterable[Any] = (),
        push_refs: Iterable[Any] = (),
    ) -> JoinRequest:
        now = time.time() if created_at is None else created_at
        changed = now if updated_at is None else updated_at
        opaque_id = (
            secrets.token_urlsafe(24)
            if request_id is None
            else _normalize_string(request_id, "request_id", maximum=128)
        )
        request_status = _normalize_string(status, "status", maximum=32)
        if request_status not in REQUEST_STATUSES:
            raise ValidationError("invalid_status")
        if isinstance(notified_targets, (str, bytes)):
            raise ValidationError("invalid_notified_targets")
        try:
            target_values = tuple(
                dict.fromkeys(
                    _normalize_string(item, "notified_target", maximum=256)
                    for item in notified_targets
                )
            )
        except TypeError as exc:
            raise ValidationError("invalid_notified_targets") from exc
        ref_values = _normalize_push_refs(push_refs)
        return cls(
            request_id=opaque_id,
            platform_id=normalize_platform_id(platform_id),
            group_id=normalize_qq_id(group_id),
            user_id=normalize_qq_id(user_id, "user_id"),
            nickname=_normalize_optional_text(
                nickname, "nickname", MAX_NICKNAME_LENGTH
            ),
            level=_normalize_optional_text(level, "level", MAX_LEVEL_LENGTH),
            question=_normalize_optional_text(question, "question", MAX_TEXT_LENGTH),
            answer=_normalize_optional_text(answer, "answer", MAX_TEXT_LENGTH),
            sub_type=_normalize_string(sub_type, "sub_type", maximum=32),
            flag=_normalize_string(flag, "flag", maximum=MAX_FLAG_LENGTH),
            created_at=_normalize_timestamp(now, "created_at"),
            updated_at=_normalize_timestamp(changed, "updated_at"),
            expires_at=_normalize_timestamp(expires_at, "expires_at"),
            status=request_status,
            platform_error=_normalize_optional_text(
                platform_error, "platform_error", MAX_ERROR_LENGTH
            ),
            notified_targets=target_values,
            push_refs=ref_values,
        )

    def to_internal_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["notified_targets"] = list(self.notified_targets)
        value["push_refs"] = [dict(ref) for ref in self.push_refs]
        return value

    def to_public_dict(self, *, include_answer: bool = True) -> dict[str, Any]:
        """Return the allowlisted Page representation, excluding OneBot secrets."""
        value: dict[str, Any] = {
            "request_id": self.request_id,
            "platform_id": self.platform_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "nickname": self.nickname or "未知",
            "level": self.level or "未知",
            "question": self.question,
            "sub_type": self.sub_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }
        if include_answer:
            value["answer"] = self.answer
        return value


@dataclass(frozen=True, slots=True)
class RequestClaim:
    request_id: str
    token: str = field(repr=False)
    request: JoinRequest = field(repr=False)


@dataclass(frozen=True, slots=True)
class NotificationClaim:
    request_id: str
    target_key: str
    token: str = field(repr=False)


class JoinReviewStore:
    """Async-locked JSON store for join-review configuration and requests."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        ttl_seconds: float = 24 * 60 * 60,
        filename: str = DEFAULT_STORE_FILENAME,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(ttl_seconds, bool) or float(ttl_seconds) <= 0:
            raise ValidationError("invalid_ttl_seconds")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        base = Path(data_dir)
        self.path = base if base.suffix == ".json" else base / filename
        self._lock = asyncio.Lock()
        self._groups: dict[tuple[str, str], GroupReviewConfig] = {}
        self._requests: dict[str, JoinRequest] = {}
        self._request_claims: dict[str, str] = {}
        self._notification_claims: dict[tuple[str, str], str] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, UnicodeError):
            return
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            return
        groups = raw.get("groups", [])
        if isinstance(groups, list):
            for item in groups:
                if not isinstance(item, dict):
                    continue
                try:
                    config = GroupReviewConfig.create(**item)
                except (TypeError, ValidationError):
                    continue
                self._groups[(config.platform_id, config.group_id)] = config
        requests = raw.get("requests", [])
        if isinstance(requests, list):
            for item in requests:
                if not isinstance(item, dict):
                    continue
                try:
                    request = JoinRequest.create(**item)
                except (TypeError, ValidationError):
                    continue
                self._requests[request.request_id] = request
        if self._expire_locked(self._clock()):
            self._save_locked()

    def _payload(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "groups": [
                config.to_dict()
                for config in sorted(
                    self._groups.values(),
                    key=lambda item: (item.platform_id, int(item.group_id)),
                )
            ],
            "requests": [
                request.to_internal_dict()
                for request in sorted(
                    self._requests.values(), key=lambda item: item.created_at
                )
            ],
        }

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.tmp")
        serialized = json.dumps(
            self._payload(), ensure_ascii=False, indent=2, sort_keys=True
        )
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _replace_request(request: JoinRequest, **changes: Any) -> JoinRequest:
        values = request.to_internal_dict()
        values.update(changes)
        return JoinRequest.create(**values)

    async def get_group_config(
        self, platform_id: Any, group_id: Any
    ) -> GroupReviewConfig:
        key = (normalize_platform_id(platform_id), normalize_qq_id(group_id))
        async with self._lock:
            return self._groups.get(key) or GroupReviewConfig.default(*key)

    async def list_group_configs(
        self, platform_id: Any | None = None
    ) -> list[GroupReviewConfig]:
        platform = None if platform_id is None else normalize_platform_id(platform_id)
        async with self._lock:
            return sorted(
                (
                    config
                    for config in self._groups.values()
                    if platform is None or config.platform_id == platform
                ),
                key=lambda item: (item.platform_id, int(item.group_id)),
            )

    async def upsert_group_config(
        self,
        *,
        platform_id: Any,
        group_id: Any,
        auto_audit_enabled: Any = False,
        review_send_enabled: Any = False,
        notify_target: Any = "target_group",
        specified_group_ids: Iterable[Any] = (),
        include_answer: Any = True,
        pinned: Any = False,
        push_group_ids: Iterable[Any] = (),
        push_style: Any = "natural",
        join_questions: Iterable[Any] = (),
    ) -> GroupReviewConfig:
        now = self._clock()
        key = (normalize_platform_id(platform_id), normalize_qq_id(group_id))
        async with self._lock:
            old = self._groups.get(key)
            config = GroupReviewConfig.create(
                platform_id=key[0],
                group_id=key[1],
                auto_audit_enabled=auto_audit_enabled,
                review_send_enabled=review_send_enabled,
                notify_target=notify_target,
                specified_group_ids=specified_group_ids,
                include_answer=include_answer,
                pinned=pinned,
                push_group_ids=push_group_ids,
                push_style=push_style,
                join_questions=join_questions,
                created_at=old.created_at if old is not None else now,
                updated_at=now,
            )
            self._groups[key] = config
            try:
                self._save_locked()
            except Exception:
                if old is None:
                    self._groups.pop(key, None)
                else:
                    self._groups[key] = old
                raise
            return config

    async def batch_upsert_group_configs(
        self, configs: Iterable[Mapping[str, Any]]
    ) -> list[GroupReviewConfig]:
        """Validate the complete batch before atomically applying it."""
        if isinstance(configs, (str, bytes, Mapping)):
            raise ValidationError("invalid_group_configs")
        try:
            values = list(configs)
        except TypeError as exc:
            raise ValidationError("invalid_group_configs") from exc
        now = self._clock()
        async with self._lock:
            if not values:
                return []
            parsed: list[GroupReviewConfig] = []
            seen: set[tuple[str, str]] = set()
            for item in values:
                if not isinstance(item, Mapping):
                    raise ValidationError("invalid_group_config")
                unexpected = set(item) - {
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
                }
                if unexpected:
                    raise ValidationError("unexpected_group_config_fields")
                try:
                    platform = normalize_platform_id(item.get("platform_id"))
                    group = normalize_qq_id(item.get("group_id"))
                except ValidationError:
                    raise
                key = (platform, group)
                if key in seen:
                    raise ValidationError("duplicate_group_config")
                seen.add(key)
                old = self._groups.get(key)
                parsed.append(
                    GroupReviewConfig.create(
                        platform_id=platform,
                        group_id=group,
                        auto_audit_enabled=item.get("auto_audit_enabled", False),
                        review_send_enabled=item.get("review_send_enabled", False),
                        notify_target=item.get("notify_target", "target_group"),
                        specified_group_ids=item.get("specified_group_ids", ()),
                        include_answer=item.get("include_answer", True),
                        pinned=item.get("pinned", False),
                        push_group_ids=item.get("push_group_ids", ()),
                        push_style=item.get("push_style", "formatted"),
                        join_questions=item.get("join_questions", ()),
                        created_at=old.created_at if old is not None else now,
                        updated_at=now,
                    )
                )
            previous = dict(self._groups)
            for config in parsed:
                self._groups[(config.platform_id, config.group_id)] = config
            try:
                self._save_locked()
            except Exception:
                self._groups = previous
                raise
            return parsed

    async def add_request(
        self,
        *,
        platform_id: Any,
        group_id: Any,
        user_id: Any,
        nickname: Any = "",
        level: Any = "",
        question: Any = "",
        answer: Any = "",
        sub_type: Any = "add",
        flag: Any,
        request_id: Any | None = None,
        created_at: Any | None = None,
    ) -> JoinRequest:
        now = self._clock() if created_at is None else created_at
        request = JoinRequest.create(
            request_id=request_id,
            platform_id=platform_id,
            group_id=group_id,
            user_id=user_id,
            nickname=nickname,
            level=level,
            question=question,
            answer=answer,
            sub_type=sub_type,
            flag=flag,
            created_at=now,
            updated_at=now,
            expires_at=float(now) + self._ttl_seconds,
        )
        async with self._lock:
            for existing in self._requests.values():
                if (
                    existing.platform_id == request.platform_id
                    and existing.group_id == request.group_id
                    and existing.user_id == request.user_id
                    and existing.sub_type == request.sub_type
                    and existing.flag == request.flag
                ):
                    return existing
            if request.request_id in self._requests:
                raise ValidationError("duplicate_request_id")
            self._requests[request.request_id] = request
            try:
                self._save_locked()
            except Exception:
                self._requests.pop(request.request_id, None)
                raise
            return request

    async def get_request(
        self, request_id: Any, *, expire: bool = True
    ) -> JoinRequest | None:
        opaque_id = _normalize_string(request_id, "request_id", maximum=128)
        async with self._lock:
            if expire:
                if self._expire_locked(self._clock()):
                    self._save_locked()
            return self._requests.get(opaque_id)

    async def list_requests(
        self,
        *,
        platform_id: Any | None = None,
        group_id: Any | None = None,
        status: str | Iterable[str] | None = None,
    ) -> list[JoinRequest]:
        platform = None if platform_id is None else normalize_platform_id(platform_id)
        group = None if group_id is None else normalize_qq_id(group_id)
        if group is not None and platform is None:
            raise ValidationError("platform_id_required")
        if status is None:
            statuses = None
        elif isinstance(status, str):
            statuses = {status}
        else:
            statuses = set(status)
        if statuses is not None and not statuses <= REQUEST_STATUSES:
            raise ValidationError("invalid_status")
        async with self._lock:
            changed = self._expire_locked(self._clock())
            if changed:
                self._save_locked()
            return sorted(
                (
                    request
                    for request in self._requests.values()
                    if (platform is None or request.platform_id == platform)
                    and (group is None or request.group_id == group)
                    and (statuses is None or request.status in statuses)
                ),
                key=lambda item: item.created_at,
                reverse=True,
            )

    async def list_public_requests(
        self,
        *,
        include_answer: bool = True,
        platform_id: Any | None = None,
        group_id: Any | None = None,
        status: str | Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        requests = await self.list_requests(
            platform_id=platform_id, group_id=group_id, status=status
        )
        return [
            request.to_public_dict(include_answer=include_answer)
            for request in requests
        ]

    def _expire_locked(self, now: float) -> bool:
        changed = False
        for request_id, request in tuple(self._requests.items()):
            if request.status in ACTIONABLE_STATUSES and request.expires_at <= now:
                self._requests[request_id] = self._replace_request(
                    request,
                    status="expired",
                    updated_at=now,
                    platform_error="",
                )
                self._request_claims.pop(request_id, None)
                changed = True
        return changed

    async def cleanup_expired(self, *, now: float | None = None) -> int:
        current = self._clock() if now is None else _normalize_timestamp(now, "now")
        async with self._lock:
            before = sum(
                request.status == "expired" for request in self._requests.values()
            )
            changed = self._expire_locked(current)
            after = sum(
                request.status == "expired" for request in self._requests.values()
            )
            if changed:
                self._save_locked()
            return after - before

    async def claim_request(self, request_id: Any) -> RequestClaim:
        opaque_id = _normalize_string(request_id, "request_id", maximum=128)
        async with self._lock:
            changed = self._expire_locked(self._clock())
            if changed:
                self._save_locked()
            request = self._requests.get(opaque_id)
            if request is None:
                raise RequestNotActionable("not_found")
            if request.status == "expired":
                raise RequestNotActionable("expired")
            if request.status not in ACTIONABLE_STATUSES:
                raise RequestNotActionable("already_processed")
            if opaque_id in self._request_claims:
                raise RequestNotActionable("busy")
            token = secrets.token_urlsafe(24)
            self._request_claims[opaque_id] = token
            return RequestClaim(opaque_id, token, request)

    async def finish_request(
        self,
        claim: RequestClaim,
        *,
        platform_succeeded: bool,
        status: Literal["approved", "rejected"],
        error: Any = "",
    ) -> JoinRequest:
        if status not in {"approved", "rejected"}:
            raise ValidationError("invalid_final_status")
        if not isinstance(platform_succeeded, bool):
            raise ValidationError("invalid_platform_succeeded")
        async with self._lock:
            if self._request_claims.get(claim.request_id) != claim.token:
                raise RequestNotActionable("invalid_claim")
            request = self._requests.get(claim.request_id)
            if request is None:
                self._request_claims.pop(claim.request_id, None)
                raise RequestNotActionable("not_found")
            now = self._clock()
            if platform_succeeded:
                updated = self._replace_request(
                    request, status=status, updated_at=now, platform_error=""
                )
            elif request.expires_at <= now:
                updated = self._replace_request(
                    request, status="expired", updated_at=now, platform_error=""
                )
            else:
                updated = self._replace_request(
                    request,
                    status="platform_error",
                    updated_at=now,
                    platform_error=_normalize_optional_text(
                        error, "platform_error", MAX_ERROR_LENGTH
                    ),
                )
            self._requests[claim.request_id] = updated
            self._request_claims.pop(claim.request_id, None)
            try:
                self._save_locked()
            except Exception:
                # Keep a successful platform result finalized in memory so it
                # cannot be sent to OneBot a second time in this process.
                if not platform_succeeded:
                    self._requests[claim.request_id] = request
                raise
            return updated

    async def complete_request(
        self,
        claim: RequestClaim,
        *,
        platform_succeeded: bool,
        status: Literal["approved", "rejected"],
        error: Any = "",
    ) -> JoinRequest:
        """Compatibility spelling for :meth:`finish_request`."""
        return await self.finish_request(
            claim,
            platform_succeeded=platform_succeeded,
            status=status,
            error=error,
        )

    async def release_request(self, claim: RequestClaim) -> bool:
        async with self._lock:
            if self._request_claims.get(claim.request_id) != claim.token:
                return False
            self._request_claims.pop(claim.request_id, None)
            return True

    async def process_request(
        self,
        request_id: Any,
        *,
        status: Literal["approved", "rejected"],
        platform_action: Callable[[JoinRequest], Awaitable[Any] | Any],
    ) -> JoinRequest:
        """Claim, perform the platform action once, and commit its outcome.

        The callback may return ``bool`` or OneBotClient's ``(bool, error)``.
        Exceptions are converted to ``platform_error`` without exposing their
        details beyond the internal bounded error field.
        """
        claim = await self.claim_request(request_id)
        succeeded = False
        error = ""
        try:
            result = platform_action(claim.request)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, tuple):
                succeeded = result[0] is True if result else False
                if len(result) > 1 and result[1]:
                    error = str(result[1])
            else:
                succeeded = result is True
            if not succeeded and not error:
                error = "platform_action_failed"
        except asyncio.CancelledError:
            await self.release_request(claim)
            raise
        except Exception as exc:
            error = type(exc).__name__
        return await self.finish_request(
            claim,
            platform_succeeded=succeeded,
            status=status,
            error=error,
        )

    async def claim_notification(
        self, request_id: Any, target_key: Any
    ) -> NotificationClaim | None:
        opaque_id = _normalize_string(request_id, "request_id", maximum=128)
        target = _normalize_string(target_key, "target_key", maximum=256)
        key = (opaque_id, target)
        async with self._lock:
            request = self._requests.get(opaque_id)
            if request is None:
                raise RequestNotActionable("not_found")
            if target in request.notified_targets or key in self._notification_claims:
                return None
            token = secrets.token_urlsafe(24)
            self._notification_claims[key] = token
            return NotificationClaim(opaque_id, target, token)

    async def finish_notification(
        self, claim: NotificationClaim, *, succeeded: bool
    ) -> bool:
        if not isinstance(succeeded, bool):
            raise ValidationError("invalid_succeeded")
        key = (claim.request_id, claim.target_key)
        async with self._lock:
            if self._notification_claims.get(key) != claim.token:
                return False
            self._notification_claims.pop(key, None)
            if not succeeded:
                return False
            request = self._requests.get(claim.request_id)
            if request is None:
                return False
            if claim.target_key in request.notified_targets:
                return True
            updated = self._replace_request(
                request,
                notified_targets=(*request.notified_targets, claim.target_key),
                updated_at=self._clock(),
            )
            self._requests[claim.request_id] = updated
            try:
                self._save_locked()
            except Exception:
                self._requests[claim.request_id] = request
                raise
            return True

    async def release_notification(self, claim: NotificationClaim) -> bool:
        """Release one in-flight notification after cancellation or shutdown."""
        key = (claim.request_id, claim.target_key)
        async with self._lock:
            if self._notification_claims.get(key) != claim.token:
                return False
            self._notification_claims.pop(key, None)
            return True

    async def notification_sent(self, request_id: Any, target_key: Any) -> bool:
        opaque_id = _normalize_string(request_id, "request_id", maximum=128)
        target = _normalize_string(target_key, "target_key", maximum=256)
        async with self._lock:
            request = self._requests.get(opaque_id)
            return request is not None and target in request.notified_targets

    async def record_push_ref(
        self, request_id: Any, group_id: Any, message_id: Any
    ) -> bool:
        """Record one pushed message ref for reply-based review. Idempotent."""
        opaque_id = _normalize_string(request_id, "request_id", maximum=128)
        group = normalize_qq_id(group_id, "push_ref_group_id")
        mid = _normalize_string(
            message_id, "push_ref_message_id", maximum=MAX_MESSAGE_ID_LENGTH
        )
        async with self._lock:
            request = self._requests.get(opaque_id)
            if request is None:
                return False
            ref = {"group_id": group, "message_id": mid}
            if ref in request.push_refs:
                return True
            if len(request.push_refs) >= MAX_PUSH_REFS:
                raise ValidationError("too_many_push_refs")
            updated = self._replace_request(
                request,
                push_refs=(*request.push_refs, ref),
                updated_at=self._clock(),
            )
            self._requests[opaque_id] = updated
            try:
                self._save_locked()
            except Exception:
                self._requests[opaque_id] = request
                raise
            return True

    async def find_request_by_push_ref(
        self, platform_id: Any, group_id: Any, message_id: Any
    ) -> JoinRequest | None:
        """Locate a request by one pushed message ref; pending TTL applies."""
        platform = normalize_platform_id(platform_id)
        group = normalize_qq_id(group_id, "push_ref_group_id")
        mid = _normalize_string(
            message_id, "push_ref_message_id", maximum=MAX_MESSAGE_ID_LENGTH
        )
        async with self._lock:
            changed = self._expire_locked(self._clock())
            if changed:
                self._save_locked()
            for request in self._requests.values():
                if request.platform_id != platform:
                    continue
                for ref in request.push_refs:
                    if ref["group_id"] == group and ref["message_id"] == mid:
                        return request
            return None

    async def close(self) -> None:
        """Drop process-local claims during plugin termination or hot reload."""
        async with self._lock:
            self._request_claims.clear()
            self._notification_claims.clear()


__all__ = [
    "ACTIONABLE_STATUSES",
    "FINAL_STATUSES",
    "GroupReviewConfig",
    "JoinRequest",
    "JoinReviewStore",
    "JoinReviewStoreError",
    "NotificationClaim",
    "NOTIFICATION_TARGETS",
    "REQUEST_STATUSES",
    "RequestClaim",
    "RequestNotActionable",
    "ValidationError",
    "normalize_platform_id",
    "normalize_qq_id",
]
