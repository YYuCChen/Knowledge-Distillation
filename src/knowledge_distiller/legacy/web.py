from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from ..accepted_insight_library import (
    AcceptedDetailKind,
    AcceptedInsightLibraryError,
    list_accepted_insights,
    list_topic_auxiliary_insights,
    read_accepted_detail,
    search_accepted_insights,
)
from .accepted_insight_publisher import (
    AcceptedPublicationKind,
    publish_accepted_insight,
)
from .candidate_a import build_candidate_a_adapters
from ..database import (
    create_task,
    get_task,
    get_task_current_source_fact,
    initialize_database,
    record_task_source_failure,
)
from ..faithful_review import FaithfulReviewer, build_faithful_reviewer
from .faithful_review_service import ReviewProcessingKind, review_task_primary
from ..identity import MaterialIdentityResolver
from .identity_service import IdentityProcessingKind, confirm_task_identity
from ..insight_judgment_service import (
    InsightJudgmentService,
    JudgmentResultKind,
)
from ..knowledge_derivation import KnowledgeDeriver, build_knowledge_deriver
from .knowledge_derivation_service import (
    DerivationProcessingKind,
    derive_task_knowledge,
)
from ..knowledge_library import (
    KnowledgeLibraryError,
    RecentKnowledgeRecord,
    read_all_formal_knowledge,
    read_recent_formal_knowledge,
    search_formal_points,
)
from .knowledge_qualification import KnowledgeQualifier, build_knowledge_qualifier
from .knowledge_result_service import (
    KnowledgeResultProcessingKind,
    qualify_and_establish_knowledge_result,
)
from ..growth_modeling import (
    HistoricalRecallPlanner,
    RelationInsightPlanner,
    build_historical_recall_planner,
    build_relation_insight_planner,
)
from ..media import MaterialMediaAcquirer, VerifiedTemporaryMedia
from .media_service import MediaProcessingKind, prepare_task_media
from .obsidian_publisher import PublicationKind, publish_task_knowledge
from .orchestration import NextBoundary, decide_next_boundary
from ..organization_models import (
    InputKind,
    OrganizationFailureCode,
    decode_relation_payload,
)
from ..organization_service import (
    OrganizationReadError,
    OrganizationService,
    OrganizationStartKind,
    list_pending_candidates,
)
from ..primary import (
    AudioNormalizer,
    FFmpegAudioNormalizer,
    PrimaryRecognizer,
    PrimaryRecovery,
    StandardAudio,
    build_qwen_primary,
)
from .primary_service import PrimaryProcessingKind, recover_task_primary
from ..secondary import (
    FFmpegLocalAudioClipper,
    LocalAudioClipper,
    SecondaryResolver,
    build_seed_secondary_resolver,
)
from .source_fact_service import (
    HumanResolutionRequest,
    SourceFactProcessingKind,
    SourceFactProcessingResult,
    apply_human_resolution,
    produce_task_source_fact,
)
from ..topic_indexing import TopicIndexer, build_topic_indexer
from ..topic_library import (
    TopicLibrary,
    TopicLibraryError,
    TopicPointProjection,
    TopicRefreshKind,
)


_UNABLE_TO_CONFIRM = "__unable_to_confirm__"
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
_URL_TRAILING_PUNCTUATION = ".,!?;:，。！？；：、)]}）】》」』"


@dataclass(frozen=True)
class _PendingHumanResolution:
    ticket: str
    task_id: int
    media: VerifiedTemporaryMedia
    recovery: PrimaryRecovery
    standard_audio: StandardAudio
    request: HumanResolutionRequest


@dataclass(frozen=True)
class RecentKnowledgeItem:
    knowledge_result_id: int
    title: str
    summary: str
    core_point_statements: tuple[str, ...]
    source_label: str
    platform: str
    published_at: str
    display_time: str
    published_path: str
    obsidian_uri: str | None


@dataclass(frozen=True)
class TopicPointItem:
    position: int
    statement: str
    knowledge_title: str
    knowledge_summary: str
    source_line: str | None
    obsidian_uri: str | None


@dataclass(frozen=True)
class KnowledgeBrowseItem:
    position: int
    knowledge_result_id: int
    title: str
    summary: str
    core_point_statements: tuple[str, ...]
    source_line: str | None
    published_at: str
    display_time: str
    obsidian_uri: str | None


def default_database_path() -> Path:
    data_dir = Path(
        os.environ.get(
            "KNOWLEDGE_DISTILLER_DATA_DIR",
            Path.home() / ".knowledge-distiller",
        )
    )
    return data_dir / "knowledge-distiller.sqlite3"


def default_runtime_root() -> Path:
    return Path(
        os.environ.get(
            "KNOWLEDGE_DISTILLER_TEMP_DIR",
            Path(tempfile.gettempdir()) / "knowledge-distiller",
        )
    )


def default_obsidian_vault_root() -> Path | None:
    value = os.environ.get("KNOWLEDGE_DISTILLER_OBSIDIAN_VAULT")
    return Path(value) if value else None


def obsidian_open_uri(
    vault_root: Path | None,
    published_path: str,
) -> str | None:
    if vault_root is None:
        return None
    relative_path = Path(published_path)
    if relative_path.is_absolute():
        return None
    try:
        resolved_root = vault_root.resolve(strict=True)
        resolved_target = (resolved_root / relative_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved_target.is_relative_to(resolved_root) or not resolved_target.is_file():
        return None
    return "obsidian://open?path=" + quote(str(resolved_target), safe="")


def configured_obsidian_open_uri(
    vault_root: Path | None,
    published_path: str,
) -> str | None:
    """Form a safe configured handoff without reading the user's Vault."""
    if vault_root is None:
        return None
    relative_path = PurePosixPath(published_path)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        return None
    root = Path(os.path.abspath(os.fspath(vault_root)))
    target = root.joinpath(*relative_path.parts)
    return "obsidian://open?path=" + quote(str(target), safe="")


def format_recent_time(
    published_at: str | datetime,
    *,
    now: datetime | None = None,
) -> str:
    try:
        published = (
            published_at
            if isinstance(published_at, datetime)
            else datetime.fromisoformat(published_at)
        )
    except (TypeError, ValueError):
        return "时间待确认"
    rendered_at = now or datetime.now().astimezone()
    if published.tzinfo is None or rendered_at.tzinfo is None:
        return "时间待确认"
    local_published = published.astimezone(rendered_at.tzinfo)
    elapsed_seconds = (rendered_at - local_published).total_seconds()
    if elapsed_seconds < 0:
        return "时间待确认"
    if elapsed_seconds < 60:
        return "刚刚"
    if elapsed_seconds < 60 * 60:
        return f"{int(elapsed_seconds // 60)} 分钟前"
    if elapsed_seconds < 24 * 60 * 60:
        return f"{int(elapsed_seconds // (60 * 60))} 小时前"
    if local_published.year == rendered_at.year:
        return f"{local_published.month} 月 {local_published.day} 日"
    return (
        f"{local_published.year} 年 {local_published.month} 月 "
        f"{local_published.day} 日"
    )


def recent_knowledge_item(
    record: RecentKnowledgeRecord,
    *,
    vault_root: Path | None,
    rendered_at: datetime,
) -> RecentKnowledgeItem:
    return RecentKnowledgeItem(
        knowledge_result_id=record.knowledge_result_id,
        title=record.title,
        summary=record.summary,
        core_point_statements=record.core_point_statements,
        source_label=record.source_label,
        platform=record.platform,
        published_at=record.published_at,
        display_time=format_recent_time(record.published_at, now=rendered_at),
        published_path=record.published_path,
        obsidian_uri=obsidian_open_uri(vault_root, record.published_path),
    )


def formal_source_line(platform: str, source_label: str) -> str | None:
    source_parts: list[str] = []
    platform_label = {"douyin": "抖音"}.get(platform, platform).strip()
    if platform_label:
        source_parts.append(platform_label)
    normalized_source_label = source_label.strip()
    if normalized_source_label and normalized_source_label != "来源信息未标注":
        source_parts.append(normalized_source_label)
    return " · ".join(source_parts) or None


def knowledge_browse_item(
    record: RecentKnowledgeRecord,
    *,
    position: int,
    vault_root: Path | None,
    rendered_at: datetime,
) -> KnowledgeBrowseItem:
    return KnowledgeBrowseItem(
        position=position,
        knowledge_result_id=record.knowledge_result_id,
        title=record.title,
        summary=record.summary,
        core_point_statements=record.core_point_statements,
        source_line=formal_source_line(record.platform, record.source_label),
        published_at=record.published_at,
        display_time=format_recent_time(record.published_at, now=rendered_at),
        obsidian_uri=obsidian_open_uri(vault_root, record.published_path),
    )


def topic_point_item(
    point: TopicPointProjection,
    *,
    position: int,
    vault_root: Path | None,
) -> TopicPointItem:
    card = point.card
    return TopicPointItem(
        position=position,
        statement=card.statement,
        knowledge_title=card.knowledge_title,
        knowledge_summary=point.knowledge_summary,
        source_line=formal_source_line(card.platform, card.source_label),
        obsidian_uri=obsidian_open_uri(vault_root, card.published_path),
    )


def _is_supported_douyin_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False

    supported_host = (
        host == "douyin.com"
        or host.endswith(".douyin.com")
        or host == "iesdouyin.com"
        or host.endswith(".iesdouyin.com")
    )
    has_target_path = bool(parsed.path.strip("/"))
    return parsed.scheme in {"http", "https"} and supported_host and has_target_path


def validate_douyin_url(raw_url: str) -> str:
    supported_urls: list[str] = []
    for match in _URL_PATTERN.finditer(raw_url.strip()):
        candidate = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        if candidate and _is_supported_douyin_url(candidate):
            supported_urls.append(candidate)
    if not supported_urls:
        raise ValueError("请输入有效的抖音作品链接。")
    if len(supported_urls) > 1:
        raise ValueError("当前一次只能提交一条抖音作品链接。")
    return supported_urls[0]


def accepted_historical_reason_label(reason: str | None) -> str | None:
    return {
        "newer_accepted_current": (
            "同一新知的更新认可版本曾成为当前表达，旧版因此永久进入历史"
        ),
        "basis_invalid": "维持这版新知所需的必要基础已经失效",
        "refuted": "后续正式依据已经反驳这版新知",
        "identity_replaced": "这条核心主张已经由另一条新知正式替代",
        "born_older_than_current": "认可到达时，同一新知已有更新的当前版本",
        "born_after_newer_ever_current": "更新版本曾经成为当前表达，旧版不再恢复",
    }.get(reason, "这版新知已进入历史，仅保留用于回溯" if reason else None)


def accepted_detail_view(detail, *, vault_root: Path | None):
    source_by_identity = {
        (item.knowledge_result_id, item.point_id): item
        for item in detail.source_leaves
    }
    node_by_version = {
        item.insight_version_id: item for item in detail.recursive_lineage
    }
    participants = []
    for participant in detail.top_level_participants:
        if participant.input_kind is InputKind.SOURCE_KNOWLEDGE:
            source = source_by_identity.get(
                (participant.knowledge_result_id, participant.point_id)
            )
            if source is None:
                raise ValueError("Accepted participant source is missing")
            participants.append(
                {
                    "kind": "来源型知识",
                    "title": source.title,
                    "statement": source.statement,
                    "contribution": participant.contribution_text,
                    "insight_version_id": None,
                }
            )
        else:
            node = node_by_version.get(participant.accepted_insight_version_id)
            if node is None:
                raise ValueError("Accepted participant insight is missing")
            participants.append(
                {
                    "kind": "AI 衍生新知",
                    "title": node.payload.claim,
                    "statement": None,
                    "contribution": participant.contribution_text,
                    "insight_version_id": node.insight_version_id,
                }
            )
    sources = tuple(
        {
            "leaf": item,
            "obsidian_uri": configured_obsidian_open_uri(
                vault_root,
                item.published_path,
            ),
        }
        for item in detail.source_leaves
    )
    used_relations = tuple(
        {
            "edge": item,
            "payload": decode_relation_payload(item.payload_json),
        }
        for item in detail.used_relations
    )
    additional_exit_facts = tuple(
        {
            "fact": fact,
            "label": accepted_historical_reason_label(fact.fact_kind),
        }
        for fact in detail.additional_exit_facts
    )
    return {
        "detail": detail,
        "participants": tuple(participants),
        "sources": sources,
        "used_relations": used_relations,
        "additional_exit_facts": additional_exit_facts,
        "historical_reason_label": accepted_historical_reason_label(
            detail.historical_reason
        ),
        "publication": detail.publication,
        "publication_obsidian_uri": (
            configured_obsidian_open_uri(
                vault_root,
                detail.publication.relative_path,
            )
            if detail.publication is not None
            else None
        ),
        "vault_configured": vault_root is not None,
    }


def create_app(
    database_path: Path | None = None,
    identity_resolver: MaterialIdentityResolver | None = None,
    media_acquirer: MaterialMediaAcquirer | None = None,
    runtime_root: Path | None = None,
    audio_normalizer: AudioNormalizer | None = None,
    primary_recognizer: PrimaryRecognizer | None = None,
    faithful_reviewer: FaithfulReviewer | None = None,
    knowledge_deriver: KnowledgeDeriver | None = None,
    knowledge_qualifier: KnowledgeQualifier | None = None,
    obsidian_vault_root: Path | None = None,
    secondary_resolver: SecondaryResolver | None = None,
    secondary_audio_clipper: LocalAudioClipper | None = None,
    topic_indexer: TopicIndexer | None = None,
    historical_recall_planner: HistoricalRecallPlanner | None = None,
    relation_insight_planner: RelationInsightPlanner | None = None,
    insight_judgment_service: InsightJudgmentService | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = database_path or default_database_path()
    initialize_database(app.config["DATABASE_PATH"])
    cookie_file_value = os.environ.get("KNOWLEDGE_DISTILLER_DOUYIN_COOKIE_FILE")
    default_identity_resolver, default_media_acquirer = build_candidate_a_adapters(
        Path(cookie_file_value) if cookie_file_value else None
    )
    app.config["IDENTITY_RESOLVER"] = identity_resolver or default_identity_resolver
    app.config["MEDIA_ACQUIRER"] = media_acquirer or default_media_acquirer
    app.config["RUNTIME_ROOT"] = runtime_root or default_runtime_root()
    app.config["AUDIO_NORMALIZER"] = audio_normalizer or FFmpegAudioNormalizer()
    app.config["PRIMARY_RECOGNIZER"] = primary_recognizer or build_qwen_primary()
    app.config["FAITHFUL_REVIEWER"] = faithful_reviewer or build_faithful_reviewer()
    app.config["KNOWLEDGE_DERIVER"] = knowledge_deriver or build_knowledge_deriver()
    app.config["KNOWLEDGE_QUALIFIER"] = (
        knowledge_qualifier or build_knowledge_qualifier()
    )
    app.config["OBSIDIAN_VAULT_ROOT"] = (
        obsidian_vault_root or default_obsidian_vault_root()
    )
    app.config["SECONDARY_RESOLVER"] = (
        secondary_resolver or build_seed_secondary_resolver()
    )
    app.config["SECONDARY_AUDIO_CLIPPER"] = (
        secondary_audio_clipper or FFmpegLocalAudioClipper()
    )
    resolved_topic_indexer = topic_indexer or build_topic_indexer()
    app.config["TOPIC_LIBRARY"] = TopicLibrary(
        app.config["DATABASE_PATH"], resolved_topic_indexer
    )
    app.config["ORGANIZATION_SERVICE"] = OrganizationService(
        app.config["DATABASE_PATH"],
        topic_indexer=resolved_topic_indexer,
        recall_planner=(
            historical_recall_planner or build_historical_recall_planner()
        ),
        relation_insight_planner=(
            relation_insight_planner or build_relation_insight_planner()
        ),
    )
    app.config["INSIGHT_JUDGMENT_SERVICE"] = (
        insight_judgment_service
        or InsightJudgmentService(app.config["DATABASE_PATH"])
    )
    pending_human_resolutions: dict[str, _PendingHumanResolution] = {}
    pending_ticket_by_task: dict[int, str] = {}

    def pending_for_task(task_id: int) -> _PendingHumanResolution | None:
        ticket = pending_ticket_by_task.get(task_id)
        return pending_human_resolutions.get(ticket) if ticket is not None else None

    def remove_pending(pending: _PendingHumanResolution) -> None:
        pending_human_resolutions.pop(pending.ticket, None)
        if pending_ticket_by_task.get(pending.task_id) == pending.ticket:
            pending_ticket_by_task.pop(pending.task_id, None)
        pending.request.audio.path.unlink(missing_ok=True)

    def register_pending(
        source_result: SourceFactProcessingResult,
        media: VerifiedTemporaryMedia,
        recovery: PrimaryRecovery,
        standard_audio: StandardAudio,
    ) -> None:
        resolution = source_result.human_resolution
        if resolution is None:
            raise AssertionError("Human confirmation result has no question")
        previous = pending_for_task(source_result.task_id)
        ticket = secrets.token_urlsafe(24)
        pending = _PendingHumanResolution(
            ticket,
            source_result.task_id,
            media,
            recovery,
            standard_audio,
            resolution,
        )
        pending_human_resolutions[ticket] = pending
        pending_ticket_by_task[source_result.task_id] = ticket
        if previous is not None:
            pending_human_resolutions.pop(previous.ticket, None)
            if previous.request.audio.path != resolution.audio.path:
                previous.request.audio.path.unlink(missing_ok=True)

    def render_home(*, status_code: int = 200, **context):
        recent_items: tuple[RecentKnowledgeItem, ...] = ()
        unreadable_count = 0
        recent_read_error = False
        try:
            recent = read_recent_formal_knowledge(app.config["DATABASE_PATH"])
            rendered_at = datetime.now().astimezone()
            recent_items = tuple(
                recent_knowledge_item(
                    record,
                    vault_root=app.config["OBSIDIAN_VAULT_ROOT"],
                    rendered_at=rendered_at,
                )
                for record in recent.records
            )
            unreadable_count = recent.unreadable_count
        except KnowledgeLibraryError:
            recent_read_error = True
        return render_template(
            "index.html",
            recent_items=recent_items,
            recent_unreadable_count=unreadable_count,
            recent_read_error=recent_read_error,
            **context,
        ), status_code

    def process_publication(task_id: int) -> tuple[int, str | None]:
        vault_root = app.config["OBSIDIAN_VAULT_ROOT"]
        if vault_root is None:
            return task_id, "knowledge_ready"
        result = publish_task_knowledge(
            app.config["DATABASE_PATH"],
            task_id,
            vault_root,
        )
        notices = {
            PublicationKind.PUBLISHED: "obsidian_saved",
            PublicationKind.RECOVERED: "obsidian_saved",
            PublicationKind.ALREADY_PUBLISHED: "obsidian_saved",
            PublicationKind.CONFLICT: "obsidian_conflict",
            PublicationKind.FAILED: "obsidian_failed",
        }
        return result.task_id, notices[result.kind]

    def process_knowledge(task_id: int) -> tuple[int, str | None]:
        source_fact = get_task_current_source_fact(
            app.config["DATABASE_PATH"],
            task_id,
        )
        if source_fact is None:
            raise AssertionError("Knowledge derivation has no current SourceFact")
        result = derive_task_knowledge(
            app.config["DATABASE_PATH"],
            task_id,
            int(source_fact["source_fact_id"]),
            app.config["KNOWLEDGE_DERIVER"],
        )
        if result.kind is not DerivationProcessingKind.READY:
            return result.task_id, None
        if result.candidate is None:
            raise AssertionError("Ready knowledge derivation has no candidate")
        knowledge_result = qualify_and_establish_knowledge_result(
            app.config["DATABASE_PATH"],
            result.task_id,
            int(source_fact["source_fact_id"]),
            result.candidate,
            app.config["KNOWLEDGE_QUALIFIER"],
        )
        notice = (
            "knowledge_ready"
            if knowledge_result.kind is KnowledgeResultProcessingKind.ESTABLISHED
            else None
        )
        if (
            knowledge_result.kind is KnowledgeResultProcessingKind.ESTABLISHED
            and app.config["OBSIDIAN_VAULT_ROOT"] is not None
        ):
            return process_publication(knowledge_result.task_id)
        return knowledge_result.task_id, notice

    def continue_after_source(
        source_result: SourceFactProcessingResult,
        media: VerifiedTemporaryMedia,
        recovery: PrimaryRecovery,
        standard_audio: StandardAudio,
    ) -> tuple[int, str | None]:
        if source_result.kind is SourceFactProcessingKind.ESTABLISHED:
            return process_knowledge(source_result.task_id)
        if source_result.kind is SourceFactProcessingKind.NEEDS_HUMAN_CONFIRMATION:
            register_pending(source_result, media, recovery, standard_audio)
        return source_result.task_id, None

    def process_task(task_id: int) -> tuple[int, str | None]:
        next_boundary = decide_next_boundary(app.config["DATABASE_PATH"], task_id)
        if next_boundary is NextBoundary.COMPLETE:
            return task_id, "obsidian_saved"
        if next_boundary is NextBoundary.OBSIDIAN_PUBLISHING:
            return process_publication(task_id)
        if next_boundary is NextBoundary.KNOWLEDGE_DERIVATION:
            return process_knowledge(task_id)
        if pending_for_task(task_id) is not None:
            return task_id, None

        identity_result = confirm_task_identity(
            app.config["DATABASE_PATH"],
            task_id,
            app.config["IDENTITY_RESOLVER"],
        )
        if identity_result.kind is not IdentityProcessingKind.CONFIRMED:
            return identity_result.task_id, None
        confirmed_boundary = decide_next_boundary(
            app.config["DATABASE_PATH"], identity_result.task_id
        )
        if confirmed_boundary is not NextBoundary.SOURCE_FACT_PRODUCTION:
            return process_task(identity_result.task_id)

        media_result = prepare_task_media(
            app.config["DATABASE_PATH"],
            identity_result.task_id,
            app.config["MEDIA_ACQUIRER"],
            app.config["RUNTIME_ROOT"],
        )
        if media_result.kind is not MediaProcessingKind.READY:
            return media_result.task_id, None
        if media_result.media is None:
            raise AssertionError("Ready media processing has no media")

        primary_result = recover_task_primary(
            app.config["DATABASE_PATH"],
            media_result.task_id,
            media_result.media,
            app.config["AUDIO_NORMALIZER"],
            app.config["PRIMARY_RECOGNIZER"],
            app.config["RUNTIME_ROOT"],
        )
        if primary_result.kind is not PrimaryProcessingKind.READY:
            return primary_result.task_id, None
        if primary_result.recovery is None or primary_result.audio is None:
            raise AssertionError("Ready Primary processing has no recovery material")

        review_result = review_task_primary(
            app.config["DATABASE_PATH"],
            primary_result.task_id,
            primary_result.recovery,
            app.config["FAITHFUL_REVIEWER"],
        )
        if review_result.kind is not ReviewProcessingKind.READY:
            return review_result.task_id, None
        if review_result.candidate is None:
            raise AssertionError("Ready review processing has no candidate")

        source_result = produce_task_source_fact(
            app.config["DATABASE_PATH"],
            review_result.task_id,
            media_result.media,
            primary_result.recovery,
            review_result.candidate,
            standard_audio=primary_result.audio,
            secondary_resolver=app.config["SECONDARY_RESOLVER"],
            audio_clipper=app.config["SECONDARY_AUDIO_CLIPPER"],
        )
        return continue_after_source(
            source_result,
            media_result.media,
            primary_result.recovery,
            primary_result.audio,
        )

    @app.get("/")
    def index():
        notice = {
            "empty": "当前没有尚未覆盖的新知识。",
        }.get(request.args.get("organization_notice", ""))
        return render_home(organization_notice=notice)

    @app.get("/knowledge")
    def knowledge_library():
        query = request.args.get("q", "")
        if query.strip():
            try:
                search_result = search_formal_points(
                    app.config["DATABASE_PATH"],
                    query,
                )
            except KnowledgeLibraryError:
                return render_template(
                    "knowledge.html",
                    query=query,
                    cards=(),
                    searched=True,
                    read_error=True,
                    active_library_section=None,
                ), 500
            card_views = tuple(
                {
                    "card": card,
                    "platform_label": {"douyin": "抖音"}.get(
                        card.platform, card.platform
                    ),
                    "obsidian_uri": obsidian_open_uri(
                        app.config["OBSIDIAN_VAULT_ROOT"],
                        card.published_path,
                    ),
                }
                for card in search_result.results
            )
            return render_template(
                "knowledge.html",
                query=query,
                cards=card_views,
                searched=True,
                unreadable_count=search_result.unreadable_count,
                active_library_section=None,
            )

        if request.args.get("view") == "knowledge":
            try:
                knowledge_result = read_all_formal_knowledge(
                    app.config["DATABASE_PATH"]
                )
            except KnowledgeLibraryError:
                return render_template(
                    "knowledge.html",
                    query=query,
                    knowledge_items=(),
                    knowledge_read_error=True,
                    active_library_section="knowledge",
                ), 500
            rendered_at = datetime.now().astimezone()
            knowledge_items = tuple(
                knowledge_browse_item(
                    record,
                    position=position,
                    vault_root=app.config["OBSIDIAN_VAULT_ROOT"],
                    rendered_at=rendered_at,
                )
                for position, record in enumerate(knowledge_result.records, start=1)
            )
            return render_template(
                "knowledge.html",
                query=query,
                knowledge_items=knowledge_items,
                knowledge_unreadable_count=knowledge_result.unreadable_count,
                active_library_section="knowledge",
            )

        topic_library = app.config["TOPIC_LIBRARY"]
        try:
            snapshot = topic_library.snapshot()
            indexer_available = topic_library.indexer_available()
        except TopicLibraryError:
            return render_template(
                "knowledge.html",
                query=query,
                cards=(),
                topic_read_error=True,
                active_library_section="topics",
            ), 500

        notice_key = request.args.get("topic_notice", "")
        topic_notices = {
            TopicRefreshKind.CURRENT.value: "主题已经是最新状态。",
            TopicRefreshKind.REFRESHED.value: "主题已根据当前正式知识更新。",
            TopicRefreshKind.EMPTY.value: "当前还没有可整理的正式知识。",
            TopicRefreshKind.UNAVAILABLE.value: (
                "主题整理当前不可用，本地搜索仍然可用。"
            ),
            TopicRefreshKind.FAILED.value: (
                "这次主题整理没有完成，原有知识和主题都没有被修改。"
            ),
            TopicRefreshKind.KNOWLEDGE_CHANGED.value: (
                "整理期间正式知识发生了变化，请稍后重新整理。"
            ),
            TopicRefreshKind.BASELINE_CHANGED.value: (
                "整理期间主题基线发生了变化，请重新发起整理。"
            ),
        }
        topic_notice = topic_notices.get(notice_key)
        visible_topics = tuple(
            topic for topic in snapshot.topics if len(topic.cards) >= 2
        )
        auto_refresh = bool(
            not topic_notice
            and not snapshot.empty_knowledge
            and (not snapshot.has_index or not snapshot.current)
            and indexer_available
        )
        return render_template(
            "knowledge.html",
            query=query,
            cards=(),
            snapshot=snapshot,
            topics=visible_topics,
            indexer_available=indexer_available,
            topic_notice=topic_notice,
            auto_refresh=auto_refresh,
            active_library_section="topics",
        )

    @app.post("/knowledge/topics/refresh")
    def refresh_knowledge_topics():
        force = request.form.get("force", "").lower() == "true"
        result = app.config["TOPIC_LIBRARY"].refresh(force=force)
        return redirect(
            url_for("knowledge_library", topic_notice=result.kind.value),
            code=303,
        )

    @app.get("/knowledge/topics/<int:topic_id>")
    def knowledge_topic(topic_id: int):
        try:
            topic, snapshot = app.config["TOPIC_LIBRARY"].topic(topic_id)
        except TopicLibraryError:
            return render_template("topic.html", read_error=True), 500
        if topic is None or len(topic.cards) < 2:
            abort(404)
        point_items = tuple(
            topic_point_item(
                point,
                position=position,
                vault_root=app.config["OBSIDIAN_VAULT_ROOT"],
            )
            for position, point in enumerate(topic.points, start=1)
        )
        auxiliary_insights = ()
        auxiliary_unreadable_count = 0
        auxiliary_read_error = False
        try:
            auxiliary = list_topic_auxiliary_insights(
                app.config["DATABASE_PATH"], topic_id
            )
            if auxiliary.topic_found:
                auxiliary_insights = auxiliary.insights
                auxiliary_unreadable_count = auxiliary.unreadable_count
        except AcceptedInsightLibraryError:
            auxiliary_read_error = True
        return render_template(
            "topic.html",
            topic=topic,
            snapshot=snapshot,
            points=point_items,
            auxiliary_insights=auxiliary_insights,
            auxiliary_unreadable_count=auxiliary_unreadable_count,
            auxiliary_read_error=auxiliary_read_error,
            active_library_section="topics",
        )

    @app.post("/knowledge/organization-events")
    def start_knowledge_organization():
        if request.form.get("intent") != "start":
            abort(400)
        started = app.config["ORGANIZATION_SERVICE"].start_or_reuse()
        if started.kind is OrganizationStartKind.EMPTY:
            return redirect(url_for("index", organization_notice="empty"), code=303)
        if started.kind is OrganizationStartKind.READ_FAILED or started.event_id is None:
            return render_template("organization_event.html", read_error=True), 500
        try:
            result = app.config["ORGANIZATION_SERVICE"].drive(started.event_id)
        except (LookupError, OrganizationReadError):
            return render_template("organization_event.html", read_error=True), 500
        if result.event.failure_code is OrganizationFailureCode.TOPIC_BASELINE_CHANGED:
            return render_template(
                "organization_event.html", event=result.event
            ), 409
        return redirect(
            url_for("knowledge_organization_event", event_id=started.event_id),
            code=303,
        )

    @app.get("/knowledge/organization-events/<int:event_id>")
    def knowledge_organization_event(event_id: int):
        try:
            view = app.config["ORGANIZATION_SERVICE"].read_event_view(event_id)
        except OrganizationReadError:
            return render_template("organization_event.html", read_error=True), 500
        if view.event is None:
            abort(404)
        return render_template(
            "organization_event.html",
            event=view.event,
            event_driver_active=view.driver_active,
        )

    @app.get("/knowledge/insight-candidates")
    def knowledge_insight_candidates():
        try:
            candidates = list_pending_candidates(app.config["DATABASE_PATH"])
        except OrganizationReadError:
            return render_template("insight_candidates.html", read_error=True), 500
        return render_template(
            "insight_candidates.html",
            candidates=candidates.candidates,
            unreadable_count=candidates.unreadable_count,
            judgment_notice=(
                "rethink" if request.args.get("judgment_notice") == "rethink" else None
            ),
        )

    @app.post("/knowledge/insight-candidates/<int:version_id>/judgments")
    def judge_knowledge_insight_candidate(version_id: int):
        decision = request.form.get("decision")
        if decision not in {"interesting", "rethink"}:
            abort(400)
        try:
            result = app.config["INSIGHT_JUDGMENT_SERVICE"].record_judgment(
                version_id,
                decision,
                request.form.get("annotation"),
            )
        except Exception:
            return render_template(
                "insight_candidates.html",
                candidates=(),
                judgment_error="unreadable",
            ), 500
        if result.kind in {
            JudgmentResultKind.RECORDED,
            JudgmentResultKind.ALREADY_RECORDED,
        }:
            if decision == "interesting":
                return redirect(
                    url_for(
                        "knowledge_accepted_insight",
                        version_id=version_id,
                    ),
                    code=303,
                )
            return redirect(
                url_for(
                    "knowledge_insight_candidates",
                    judgment_notice="rethink",
                ),
                code=303,
            )
        if result.kind is JudgmentResultKind.CONFLICT:
            return render_template(
                "insight_candidates.html",
                candidates=(),
                judgment_error="conflict",
            ), 409
        if result.kind in {
            JudgmentResultKind.NOT_FOUND,
            JudgmentResultKind.NOT_ESTABLISHED,
        }:
            return render_template(
                "insight_candidates.html",
                candidates=(),
                judgment_error=result.kind.value,
            ), 404
        return render_template(
            "insight_candidates.html",
            candidates=(),
            judgment_error="unreadable",
        ), 500

    @app.get("/knowledge/insights")
    def knowledge_accepted_insights():
        query = request.args.get("q", "")
        searched = bool(query.strip())
        try:
            result = (
                search_accepted_insights(app.config["DATABASE_PATH"], query)
                if searched
                else list_accepted_insights(app.config["DATABASE_PATH"])
            )
        except AcceptedInsightLibraryError:
            return render_template(
                "accepted_insights.html",
                query=query,
                searched=searched,
                current=(),
                historical=(),
                read_error=True,
                active_library_section="insights",
            ), 500
        return render_template(
            "accepted_insights.html",
            query=query,
            searched=searched,
            current=result.current,
            historical=result.historical,
            unreadable_count=result.unreadable_count,
            accepted_historical_reason_label=accepted_historical_reason_label,
            active_library_section="insights",
        )

    @app.get("/knowledge/insights/<int:version_id>")
    def knowledge_accepted_insight(version_id: int):
        try:
            result = read_accepted_detail(
                app.config["DATABASE_PATH"], version_id
            )
        except AcceptedInsightLibraryError:
            return render_template(
                "accepted_insight_detail.html",
                unreadable=True,
                active_library_section="insights",
            ), 500
        if result.kind is AcceptedDetailKind.NOT_FOUND:
            abort(404)
        if result.kind is AcceptedDetailKind.UNREADABLE or result.detail is None:
            return render_template(
                "accepted_insight_detail.html",
                unreadable=True,
                active_library_section="insights",
            ), 500
        try:
            context = accepted_detail_view(
                result.detail,
                vault_root=app.config["OBSIDIAN_VAULT_ROOT"],
            )
        except (TypeError, ValueError):
            return render_template(
                "accepted_insight_detail.html",
                unreadable=True,
                active_library_section="insights",
            ), 500
        publication_notice = None
        if context["publication"] is not None:
            publication_notice = {
                AcceptedPublicationKind.PUBLISHED.value: (
                    "新文件与正式发布成功事实已建立。"
                ),
                AcceptedPublicationKind.RECOVERED.value: (
                    "已按落位收据恢复正式发布成功事实，既有文件未被改写。"
                ),
                AcceptedPublicationKind.ALREADY_PUBLISHED.value: (
                    "这版新知此前已成功发布；本次没有检查、修复或改写用户文件。"
                ),
            }.get(request.args.get("publication_notice", ""))
        return render_template(
            "accepted_insight_detail.html",
            active_library_section="insights",
            publication_notice=publication_notice,
            **context,
        )

    @app.post("/knowledge/insights/<int:version_id>/publish")
    def publish_knowledge_accepted_insight(version_id: int):
        vault_root = app.config["OBSIDIAN_VAULT_ROOT"]
        try:
            result = publish_accepted_insight(
                app.config["DATABASE_PATH"],
                version_id,
                vault_root,
            )
        except Exception:
            return render_template(
                "accepted_insight_detail.html",
                publication_problem="failed",
                active_library_section="insights",
            ), 500
        if result.kind in {
            AcceptedPublicationKind.PUBLISHED,
            AcceptedPublicationKind.RECOVERED,
            AcceptedPublicationKind.ALREADY_PUBLISHED,
        }:
            return redirect(
                url_for(
                    "knowledge_accepted_insight",
                    version_id=version_id,
                    publication_notice=result.kind.value,
                ),
                code=303,
            )
        if result.kind is AcceptedPublicationKind.CONFLICT:
            return render_template(
                "accepted_insight_detail.html",
                publication_problem="conflict",
                active_library_section="insights",
            ), 409
        if result.kind is AcceptedPublicationKind.NOT_ELIGIBLE:
            return render_template(
                "accepted_insight_detail.html",
                publication_problem="not_eligible",
                active_library_section="insights",
            ), 422
        return render_template(
            "accepted_insight_detail.html",
            publication_problem="failed",
            active_library_section="insights",
        ), 500

    @app.post("/submissions")
    def submit():
        try:
            submitted_url = validate_douyin_url(request.form.get("url", ""))
        except ValueError as error:
            return render_home(
                status_code=400,
                error=str(error),
                submitted_url=request.form.get("url", ""),
            )

        task_id = create_task(app.config["DATABASE_PATH"], submitted_url)
        result_task_id, notice = process_task(task_id)
        return redirect(
            url_for("task_feedback", task_id=result_task_id, notice=notice),
            code=303,
        )

    @app.post("/tasks/<int:task_id>/continue")
    def continue_task(task_id: int):
        result_task_id, notice = process_task(task_id)
        return redirect(
            url_for("task_feedback", task_id=result_task_id, notice=notice),
            code=303,
        )

    @app.get("/tasks/<int:task_id>/source-confirmation/<ticket>/audio")
    def source_confirmation_audio(task_id: int, ticket: str):
        pending = pending_human_resolutions.get(ticket)
        if pending is None or pending.task_id != task_id:
            abort(404)
        audio_path = pending.request.audio.path
        if not audio_path.is_file():
            abort(404)
        response = send_file(audio_path, mimetype="audio/wav", conditional=True)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/tasks/<int:task_id>/source-confirmation")
    def confirm_source_content(task_id: int):
        ticket = request.form.get("ticket", "")
        pending = pending_human_resolutions.get(ticket)
        if pending is None or pending.task_id != task_id:
            abort(409)
        choice = request.form.get("choice", "")
        if choice == _UNABLE_TO_CONFIRM:
            remove_pending(pending)
            record_task_source_failure(
                app.config["DATABASE_PATH"],
                task_id,
                "human_source_confirmation_unconfirmed",
            )
            return redirect(url_for("task_feedback", task_id=task_id), code=303)
        try:
            candidate = apply_human_resolution(pending.request, choice)
        except ValueError:
            abort(400)

        remove_pending(pending)
        source_result = produce_task_source_fact(
            app.config["DATABASE_PATH"],
            task_id,
            pending.media,
            pending.recovery,
            candidate,
            standard_audio=pending.standard_audio,
            secondary_resolver=app.config["SECONDARY_RESOLVER"],
            audio_clipper=app.config["SECONDARY_AUDIO_CLIPPER"],
        )
        result_task_id, notice = continue_after_source(
            source_result,
            pending.media,
            pending.recovery,
            pending.standard_audio,
        )
        return redirect(
            url_for("task_feedback", task_id=result_task_id, notice=notice),
            code=303,
        )

    @app.get("/tasks/<int:task_id>")
    def task_feedback(task_id: int):
        task = get_task(app.config["DATABASE_PATH"], task_id)
        if task is None:
            abort(404)

        next_boundary = decide_next_boundary(app.config["DATABASE_PATH"], task_id)
        if next_boundary is NextBoundary.KNOWLEDGE_DERIVATION:
            if task["waiting_reason"] in {
                "knowledge_derivation_unavailable",
                "knowledge_qualification_unavailable",
            }:
                feedback = "knowledge_derivation_unavailable"
            elif task["last_failure_reason"] == "knowledge_candidate_rejected":
                feedback = "knowledge_qualification_rejected"
            elif str(task["last_failure_reason"] or "").startswith(
                "knowledge_qualification_"
            ):
                feedback = "knowledge_qualification_failed"
            elif task["last_failure_boundary"] == "knowledge_derivation":
                feedback = "knowledge_derivation_failed"
            else:
                feedback = "source_ready"
        elif next_boundary is NextBoundary.OBSIDIAN_PUBLISHING:
            if task["last_failure_reason"] == "obsidian_target_conflict":
                feedback = "obsidian_conflict"
            elif task["last_failure_boundary"] == "obsidian_publishing":
                feedback = "obsidian_failed"
            else:
                feedback = "knowledge_ready"
        elif next_boundary is NextBoundary.COMPLETE:
            feedback = "obsidian_saved"
        elif next_boundary is not NextBoundary.SOURCE_FACT_PRODUCTION:
            abort(500)
        elif task["waiting_reason"] == "douyin_login_required":
            feedback = "login_required"
        elif task["waiting_reason"] == "faithful_review_unavailable":
            feedback = "review_unavailable"
        elif task["waiting_reason"] == "secondary_unavailable":
            feedback = "secondary_unavailable"
        elif task["waiting_reason"] == "human_source_confirmation_required":
            feedback = (
                "human_source_confirmation_required"
                if pending_for_task(task_id) is not None
                else "human_source_confirmation_expired"
            )
        elif str(task["last_failure_reason"] or "").startswith("review_"):
            feedback = "review_failed"
        elif task["last_failure_reason"] == "snapshot_needs_local_resolution":
            feedback = "snapshot_needs_local_resolution"
        elif task["last_failure_reason"] == "human_source_confirmation_unconfirmed":
            feedback = "human_source_confirmation_unconfirmed"
        elif task["last_failure_reason"] == "snapshot_rejected":
            feedback = "snapshot_rejected"
        elif str(task["last_failure_reason"] or "").startswith("primary_"):
            feedback = "primary_failed"
        elif str(task["last_failure_reason"] or "").startswith("media_"):
            feedback = "media_failed"
        elif task["material_id"] is not None:
            feedback = "identity_confirmed"
        elif task["last_failure_reason"] is not None:
            feedback = "identity_failed"
        else:
            feedback = "submission_received"

        return render_home(
            task=task,
            feedback=feedback,
            submitted_url=task["submitted_url"],
            human_resolution=pending_for_task(task_id),
            unable_to_confirm_value=_UNABLE_TO_CONFIRM,
        )

    return app
