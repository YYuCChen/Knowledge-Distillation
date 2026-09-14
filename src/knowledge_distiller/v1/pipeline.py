from __future__ import annotations

import json
import re
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from knowledge_distiller.faithful_review import FaithfulReviewer, ReviewConcern
from knowledge_distiller.media import VerifiedTemporaryMedia
from knowledge_distiller.primary import AudioNormalizer, PrimaryRecognizer, PrimaryRecovery, PrimaryChunk, StandardAudio

from .chrome import ChromeSessionError
from .confirmation import ConfirmationAudioError, FFmpegConfirmationClipper, locate_concern_audio
from .domain import CapturedMaterial, SourceFact
from .douyin import DouyinSource, DouyinSourceError
from .source_files import SourceCopyError
from .file_sources import parse_submitted_source
from .source_parsing import SourceReadError
from .knowledge_model import AnthropicKnowledgeModel, KnowledgeModelError
from .publisher import PublicationState, publish
from .reviewer import RecordedReviewer
from .store import Store, SourceReviewConflict
from .source_versions import SourceVersionError
from .youtube import YouTubeSourceError, youtube_identity
from .bilibili import BilibiliSourceError
from .xiaohongshu import XiaohongshuSourceError, xiaohongshu_input
from .xpost import XPostSourceError, xpost_identity
from .zhihu import ZhihuSourceError, zhihu_identity
from .weibo import WeiboSourceError, weibo_identity
from .ocr import OcrError, default_ocr_runner
from .image_source import image_source_fact


@dataclass(frozen=True)
class DistillResult:
    item_id: int
    state: str


class DistillError(RuntimeError):
    pass


class Distiller:
    def __init__(
        self,
        *,
        store: Store,
        source: DouyinSource,
        normalizer: AudioNormalizer,
        recognizer: PrimaryRecognizer,
        reviewer: FaithfulReviewer,
        confirmation_clipper: FFmpegConfirmationClipper,
        knowledge_model: AnthropicKnowledgeModel,
        runtime_root: Path,
        vault: Path | None,
        youtube_source=None,
        bilibili_source=None,
        xiaohongshu_source=None,
        xpost_source=None,
        zhihu_source=None,
        weibo_source=None,
        ocr=None,
        documents=None,
    ):
        self.store = store
        self.source = source
        self.youtube_source = youtube_source
        self.bilibili_source = bilibili_source
        self.xiaohongshu_source = xiaohongshu_source
        self.xpost_source = xpost_source
        self.zhihu_source = zhihu_source
        self.weibo_source = weibo_source
        self.normalizer = normalizer
        self.recognizer = recognizer
        self.reviewer = reviewer
        self.confirmation_clipper = confirmation_clipper
        self.knowledge_model = knowledge_model
        self.runtime_root = runtime_root
        self.vault = vault
        self.ocr = ocr if ocr is not None else default_ocr_runner()
        self.documents = documents

    def run(self, item_id: int) -> DistillResult:
        row = self._item(item_id)
        if row["published_path"] is not None:
            self.store.mark_succeeded(item_id)
            return DistillResult(item_id, "succeeded")
        try:
            if row["source_fact_id"] is None:
                self._establish_source(item_id, row)
            return self._finish(item_id)
        except SourceReviewConflict:
            return DistillResult(item_id, self._item(item_id)['state'])
        except SourceCopyError:
            self.store.mark_failed(item_id, self._item(item_id)["phase"], "source_copy_unavailable")
            return DistillResult(item_id, "failed")
        except (OcrError, SourceVersionError, ChromeSessionError, DouyinSourceError, YouTubeSourceError, BilibiliSourceError, XiaohongshuSourceError, XPostSourceError, ZhihuSourceError, WeiboSourceError, KnowledgeModelError, DistillError) as error:
            if self._item(item_id)["state"] == "waiting_user":
                return DistillResult(item_id, "waiting_user")
            if isinstance(error, OcrError) and hasattr(error, 'partial_review'):
                from .local_records import write_record
                directory = self.runtime_root / "items" / str(item_id)
                directory.mkdir(parents=True, exist_ok=True)
                write_record(directory / 'ocr-review-incomplete.json', error.partial_review)
            if isinstance(error, OcrError) and hasattr(error, 'member_id'):
                from .local_records import write_record
                directory = self.runtime_root / "items" / str(item_id)
                directory.mkdir(parents=True, exist_ok=True)
                write_record(directory / 'ocr-diagnostic.json', {
                    'code': error.code, 'member_id': error.member_id,
                    'completed_members': error.completed_members,
                    'completed_images': getattr(error, 'completed_images', []),
                    'line_diagnostics': getattr(error, 'line_diagnostics', [])})
            code = error.args[0] if error.args else "distill_failed"
            self.store.mark_failed(item_id, self._item(item_id)["phase"], str(code),
                                   rejection_reason=error.rejection_reason if isinstance(error, KnowledgeModelError) else None)
            return DistillResult(item_id, "failed")
        finally:
            from .temporary_artifacts import TemporaryArtifacts
            TemporaryArtifacts(self.store, self.runtime_root).clean_item(item_id)

    def suggest_candidates(self, item_id: int, *, token: str, concern_id: str = "") -> None:
        row = self._item(item_id)
        pending = _pending_confirmation(row, token)
        concerns = [c for c in pending['concerns'] if not concern_id or c.get('audio_name') == concern_id]
        if not concerns:
            raise ValueError('疑点已更新，请刷新后再操作。')
        suggestions = self.reviewer.suggest_candidates(pending['snapshot'], concerns)
        # Updating suggestions does not resolve concerns, establish facts or enqueue work.
        for concern in concerns:
            choices = suggestions[concern['audio_name']]
            concern['candidates'] = [choice['text'] for choice in choices]
            concern['candidate_explanations'] = {choice['text']: choice['meaning_zh'] for choice in choices}
        self.store.update_confirmation_suggestions(item_id, row['confirmation_json'], pending)

    def resolve(self, item_id, action, value="", *, token="", concern_id="", concern_revision=""):
        from .confirmation_revision import ConfirmationConflict, revision
        if not concern_revision:
            return self._resolve_once(item_id, action, value, token=token, concern_id=concern_id)
        for _ in range(4):
            state = self.store.confirmation_decision(item_id, concern_revision, action, value)
            if state:
                return DistillResult(item_id, state)
            row = self._item(item_id)
            pending = json.loads(row['confirmation_json']) if row['confirmation_json'] else {}
            concern = next((c for c in pending.get('concerns', []) if c.get('audio_name') == concern_id), None)
            if concern is None or revision(pending, concern) != concern_revision:
                raise ValueError('该疑点已在另一端更新，请查看当前状态；输入已保留。')
            try:
                return self._resolve_once(item_id, action, value, token=pending['token'],
                                          concern_id=concern_id, decision=(concern_revision, action, value))
            except ConfirmationConflict:
                continue
        raise ValueError('其他操作正在保存，请稍后重试；输入已保留。')

    def resolve_group(self, item_id, action, value="", *, token, request_id,
                      group_id, group_revision, selected_member_uids, actor="local", _legacy_decision=None):
        """One explicit selection, one Store transaction, bounded unrelated CAS retries."""
        from .confirmation_groups import plan_group
        from .confirmation_revision import ConfirmationConflict
        if (not isinstance(request_id, str) or not request_id or len(request_id)>128
                or not isinstance(selected_member_uids, list) or not selected_member_uids
                or any(not isinstance(u,str) for u in selected_member_uids)
                or len(set(selected_member_uids)) != len(selected_member_uids)
                or actor not in {'local', 'feishu'} or not isinstance(group_id,str)
                or not isinstance(group_revision,str) or not isinstance(value,str)):
            raise ValueError('group_request_invalid')
        request = dict(request_id=request_id,group_id=group_id,group_revision=group_revision,
                       selected_member_uids=selected_member_uids,action=action,value=value)
        for attempt in range(4):
            prior = self.store.group_decision(item_id, request)
            if prior is not None:
                return DistillResult(item_id, prior)
            row = self._item(item_id)
            # Authenticate the first request with its current displayed token.
            # Only this already-authenticated call can rebase an unrelated CAS.
            if not token or row['state'] != 'waiting_user' or row['confirmation_json'] is None:
                raise ConfirmationConflict('group_revision_conflict')
            if attempt == 0 and json.loads(row['confirmation_json']).get('token') != token:
                raise ConfirmationConflict('group_token_stale')
            pending = self.store.confirmation_view(item_id)
            if pending is None:
                continue  # A concurrent commit may now be available in the ledger.
            plan = plan_group(pending, request, actor=actor)
            if row['source_fact_id'] is not None:
                raise ConfirmationConflict('source_fact_already_established')
            # Preserve exact original replay anchors before any text mutation.
            for member, audit in zip(plan.selected, plan.audit):
                try:
                    audio_range = self.transcript_location(item_id, token=pending['token'],
                        start=member['start'], end=member['end'])
                except ValueError:
                    audio_range = None
                if audio_range is not None:
                    plan.pending['correction_locations'].append({'start':audit['result_span'][0],
                        'end':audit['result_span'][1],'audio_range':audio_range})
            committed = {**request, 'audit': list(plan.audit)}
            try:
                if plan.can_establish_fact:
                    state = self.store.resolve_confirmation(item_id, row['confirmation_json'],
                        fact=SourceFact(plan.pending['snapshot'], tuple(plan.pending['uncertainties']+plan.pending['resolved'])),
                        lineage=plan.pending.get('lineage'), group_decision=committed, decision=_legacy_decision)
                else:
                    state = self.store.resolve_confirmation(item_id, row['confirmation_json'],
                        next_confirmation=plan.pending, group_decision=committed, decision=_legacy_decision)
            except ConfirmationConflict:
                # Every retry reconstructs the complete plan and rechecks the
                # whole group's semantic revision, including unselected members.
                if attempt == 3:
                    raise ConfirmationConflict('group_save_busy')
                continue
            if action != 'unable':
                self._remove_confirmation_audio(item_id, list(plan.selected))
            return DistillResult(item_id, state)
        raise ConfirmationConflict('group_save_busy')

    def _resolve_once(
        self, item_id: int, action: str, value: str = "", *, token: str = "", concern_id: str = "", decision=None
    ) -> DistillResult:
        row = self._item(item_id)
        pending = _pending_confirmation(row, token)
        concerns = pending["concerns"]
        if pending.get('kind') == 'image':
            from .image_confirmation import resolve_review
            state = resolve_review(self.store, row, pending, action, value, concern_id, decision=decision)
            return DistillResult(item_id, state)
        index = next((i for i, c in enumerate(concerns) if c.get("audio_name") == concern_id), -1) if concern_id else 0
        if index < 0 or index >= len(concerns):
            raise ValueError("疑点已更新，请刷新后再操作。")
        concern = concerns[index]
        if pending.get('group_confirmation_contract'):
            from .confirmation_schema import digest
            view = self.store.confirmation_view(item_id)
            member = next((m for m in view['concerns'] if m.get('audio_name') == concern.get('audio_name')), None)
            group = next((g for g in view['groups'] if member and member['concern_uid'] in g['member_uids']), None)
            if group is None:
                raise ValueError('group_revision_conflict')
            return self.resolve_group(item_id, action, value, token=token,
                request_id='legacy-' + digest([group['group_id'], group['group_revision'],
                    member['concern_uid'], action, value, decision]),
                group_id=group['group_id'], group_revision=group['group_revision'],
                selected_member_uids=[member['concern_uid']], _legacy_decision=decision)
        if action not in {"candidate", "manual", "unable"}:
            raise ValueError("unknown source confirmation action")
        if action == "unable":
            replacement = "[听辨不清]"
        elif action == "candidate":
            replacement = value
            if replacement not in concern["candidates"]:
                raise ValueError("candidate does not belong to current concern")
        elif action == "manual":
            replacement = value.strip()
            if not replacement:
                raise ValueError("请输入正确文字")
        start, end = int(concern["start"]), int(concern["end"])
        snapshot = pending["snapshot"]
        if snapshot[start:end] != concern["text"]:
            raise ValueError("source confirmation no longer matches snapshot")
        updated = snapshot[:start] + replacement + snapshot[end:]
        if row["source_fact_id"] is not None:
            if updated != row["snapshot"]:
                raise ValueError("这份来源已由另一次确认成立；当前纠正尚未保存，不能覆盖原事实。")
            state = self.store.resolve_confirmation(item_id, row["confirmation_json"], decision=decision)
            self._remove_confirmation_audio(item_id, concerns)
            return DistillResult(item_id, state)
        delta = len(replacement) - (end - start)
        locations = self._corrected_audio_locations(item_id, pending, start, end, replacement)
        remaining = [dict(c) if i < index else {**c, "start": c["start"] + delta, "end": c["end"] + delta} for i, c in enumerate(concerns) if i != index]
        uncertainties = [
            {**entry, "start": entry["start"] + delta, "end": entry["end"] + delta}
            if entry["start"] >= end else entry
            for entry in pending.get("uncertainties", [])
            if not (entry.get("status") == "unresolved" and entry["start"] < end and entry["end"] > start)
        ]
        deferred = [
            {**c, "start": c["start"] + delta, "end": c["end"] + delta}
            if c["start"] >= end else c
            for c in pending.get("deferred_concerns", [])
            if c["audio_name"] != concern["audio_name"]
        ]
        resolved = list(pending.get("resolved", []))
        if action == "unable":
            deferred.append({**concern, "start": start, "end": start + len(replacement),
                             "text": replacement})
            deferred.sort(key=lambda c: c["start"])
            uncertainties.append({
                "start": start, "end": start + len(replacement), "text": replacement,
                "original_text": concern["text"], "reason": concern["reason"],
                "status": "unresolved", "by": "human",
            })
        else:
            resolved.append({"text": concern["text"], "replacement": replacement, "by": "human"})
        if remaining or deferred or pending.get("review_required"):
            state = self.store.resolve_confirmation(
                item_id, row["confirmation_json"], decision=decision,
                next_confirmation={
                    **pending,
                    "correction_locations": locations,
                    "snapshot": updated, "concerns": remaining,
                    "resolved": resolved, "uncertainties": uncertainties,
                    "deferred_concerns": deferred,
                },
            )
        else:
            state = self.store.resolve_confirmation(
                item_id, row["confirmation_json"], decision=decision,
                fact=SourceFact(updated, tuple(uncertainties + resolved)),
                lineage=pending.get("lineage"),
            )
        if action != "unable":
            self._remove_confirmation_audio(item_id, [concern])
        return DistillResult(item_id, state)

    def correct_transcript(self, item_id: int, *, token: str, start: int, end: int,
                           original: str, replacement: str) -> DistillResult:
        row = self._item(item_id)
        pending = _pending_confirmation(row, token)
        if row["source_fact_id"] is not None:
            raise ValueError("来源已经成立，不能修改原事实。")
        snapshot = pending["snapshot"]
        if not (0 <= start < end <= len(snapshot)) or snapshot[start:end] != original:
            raise ValueError("选中文字已变化，请刷新后重新选择。")
        replacement = replacement.strip()
        if not replacement or "[听辨不清]" in replacement:
            raise ValueError("请输入回听确认的文字；听不清的疑点请使用“无法确认”。")
        # Overlapping pending decisions retain their own explicit confirmation.
        if any(c["start"] < end and c["end"] > start
               for c in pending["concerns"]):
            raise ValueError("选区包含待确认疑点，请先处理疑点，或只选未标出的文字。")
        delta = len(replacement) - (end - start)
        def shifted(entry):
            return {**entry, "start": entry["start"] + delta, "end": entry["end"] + delta} if entry["start"] >= end else entry
        uncertainties = []
        for entry in pending.get("uncertainties", []):
            if entry["start"] < end and entry["end"] > start:
                if not (start <= entry["start"] and entry["end"] <= end):
                    raise ValueError("选区跨过一个疑点边界，请完整选择该片段。")
                continue
            uncertainties.append(shifted(entry))
        updated = {**pending, "snapshot": snapshot[:start] + replacement + snapshot[end:],
                   "correction_locations": self._corrected_audio_locations(item_id, pending, start, end, replacement),
                   "concerns": [shifted(c) for c in pending["concerns"]],
                   "deferred_concerns": [shifted(c) for c in pending.get("deferred_concerns", [])
                       if not (start <= c["start"] and c["end"] <= end)],
                   "uncertainties": uncertainties,
                   "resolved": pending.get("resolved", []) + [{"text": original, "replacement": replacement,
                       "by": "human", "action": "local_transcription", "revision_start": start,
                       "revision_end": end}], "review_required": True}
        state = self.store.resolve_confirmation(item_id, row["confirmation_json"], next_confirmation=updated)
        return DistillResult(item_id, state)

    def finish_transcript(self, item_id: int, *, token: str) -> DistillResult:
        row = self._item(item_id)
        pending = _pending_confirmation(row, token)
        if pending["concerns"]:
            raise ValueError("请先完成所有来源疑点确认。")
        if pending.get('group_confirmation_contract') and (pending.get('deferred_concerns')
                or any(u.get('status')=='unresolved' for u in pending.get('uncertainties',[]))):
            raise ValueError('group_unresolved_members_require_review')
        if pending.get("deferred_concerns"):
            state = self.store.resolve_confirmation(item_id, row["confirmation_json"],
                next_confirmation={**pending, "review_required": False})
        else:
            state = self.store.resolve_confirmation(item_id, row["confirmation_json"],
                fact=SourceFact(pending["snapshot"], tuple(pending.get("uncertainties", []) + pending.get("resolved", []))),
                lineage=pending.get("lineage"))
        return DistillResult(item_id, state)

    def restore_group_deferred(self, item_id, *, token, selected_member_uids=None):
        """Make unresolved group members actionable again without editing text."""
        from copy import deepcopy
        from .confirmation_revision import ConfirmationConflict
        row=self._item(item_id)
        _pending_confirmation(row,token)
        pending=self.store.confirmation_view(item_id)
        if pending is None or not pending.get('group_confirmation_contract'):
            raise ValueError('group_deferred_recovery_not_available')
        deferred=pending.get('deferred_concerns',[])
        ids=[c['concern_uid'] for c in deferred]
        selection=ids if selected_member_uids is None else selected_member_uids
        if (not isinstance(selection,list) or not selection or any(not isinstance(u,str) for u in selection)
                or len(set(selection))!=len(selection) or not set(selection)<=set(ids)):
            raise ConfirmationConflict('group_member_not_actionable')
        updated=deepcopy(pending)
        current={c['concern_uid'] for c in updated['concerns']}
        updated['concerns'].extend(deepcopy(c) for c in deferred if c['concern_uid'] in selection and c['concern_uid'] not in current)
        updated['concerns'].sort(key=lambda c:c['start'])
        updated['deferred_concerns']=[c for c in updated['deferred_concerns'] if c['concern_uid'] not in selection]
        state=self.store.resolve_confirmation(item_id,row['confirmation_json'],next_confirmation=updated)
        return DistillResult(item_id,state)

    def transcript_audio(self, item_id: int, *, token: str) -> Path:
        pending = _pending_confirmation(self._item(item_id), token)
        path = self.runtime_root / "items" / str(item_id) / "audio" / "standard.wav"
        if not pending.get("audio_timeline") or not path.is_file() or path.is_symlink():
            raise ValueError("原音不可用，请查看原来源或重新识别。")
        return path

    def transcript_location(self, item_id: int, *, token: str, start: int, end: int):
        pending = _pending_confirmation(self._item(item_id), token)
        if not 0 <= start < end <= len(pending["snapshot"]):
            raise ValueError("请先选中要回听的文字。")
        path = self.transcript_audio(item_id, token=token)
        for location in pending.get("correction_locations", []):
            if location["start"] <= start < end <= location["end"]:
                return tuple(location["audio_range"])
        timeline = pending["audio_timeline"]
        recovery = PrimaryRecovery(timeline["text"], None,
            tuple(PrimaryChunk(**chunk) for chunk in timeline["chunks"]),
            timeline_status=timeline.get('timeline_status', 'unverified'))
        concern = ReviewConcern(start, end, pending["snapshot"][start:end], "人工校对", True)
        result = locate_concern_audio(StandardAudio(path, timeline["duration_seconds"]),
            recovery, pending["snapshot"], concern)
        if result is None:
            raise ValueError("这段文字尚未可靠定位，请重试局部原音恢复或查看来源。")
        return result

    def recover_confirmation_audio(self, item_id, *, token, concern_id):
        row = self._item(item_id)
        pending = _pending_confirmation(row, token)
        concern = next((c for c in pending['concerns'] if c.get('audio_name') == concern_id), None)
        if concern is None or pending.get('kind') == 'image':
            raise ValueError('疑点已更新，请查看当前状态。')
        timeline = pending.get('audio_timeline')
        if not timeline:
            raise ValueError('原音定位信息不可用，已有文字和判断已保留。')
        directory = self.runtime_root / 'items' / str(item_id)
        from .file_lock import acquire
        from .audio_location_recovery import recover_locations
        audio = StandardAudio(self.transcript_audio(item_id, token=token), timeline['duration_seconds'])
        recovery = PrimaryRecovery(timeline['text'], None,
            tuple(PrimaryChunk(**chunk) for chunk in timeline['chunks']),
            timeline_status=timeline.get('timeline_status', 'unverified'))
        issue = ReviewConcern(concern['start'], concern['end'], concern['text'], concern['reason'], True)
        name = f'concern-1-{uuid4().hex}.wav'
        output = directory / 'confirmation' / name
        try:
            with acquire(directory / '.audio-recovery.lock'):
                if locate_concern_audio(audio, recovery, pending['snapshot'], issue) is None:
                    recovery = recover_locations(self.recognizer, audio, recovery, directory)
                self.confirmation_clipper.clip(audio, recovery, pending['snapshot'], issue, output)
                concern.update(audio_file=name, audio_recovery_required=False)
                concern['reason'] = concern['reason'].removesuffix(' 局部原音定位恢复未完成，已保留疑点与原文，可重试恢复。')
                pending['audio_alignment'] = 'local_preview_10s_v3'
                pending['audio_timeline'] = {**timeline, 'chunks': [asdict(c) for c in recovery.chunks],
                                             'timeline_status': recovery.timeline_status}
                self.store.update_confirmation_suggestions(item_id, row['confirmation_json'], pending)
        except (OSError, ValueError, EOFError, wave.Error, ConfirmationAudioError) as error:
            output.unlink(missing_ok=True)
            raise ValueError('局部原音尚未恢复，已有文字、候选和人工判断均已保留。') from error

    def _corrected_audio_locations(self, item_id, pending, start, end, replacement):
        # Preserve the pre-edit replay anchor even when no corrected character
        # occurs in the ASR text (e.g. 要不然 → 腰板). This belongs to the draft.
        try:
            audio_range = self.transcript_location(item_id, token=pending["token"], start=start, end=end)
        except ValueError:
            audio_range = None
        delta = len(replacement) - (end - start)
        locations = [({**entry, "start": entry["start"] + delta, "end": entry["end"] + delta}
            if entry["start"] >= end else entry) for entry in pending.get("correction_locations", [])
            if not (entry["start"] < end and entry["end"] > start)]
        if audio_range is not None:
            locations.append({"start": start, "end": start + len(replacement), "audio_range": audio_range})
        return locations

    def confirmation_audio(self, item_id: int, concern_id: str = "") -> Path | None:
        row = self.store.item_bundle(item_id)
        if row is None:
            return None
        if row["state"] != "waiting_user" or row["confirmation_json"] is None:
            return None
        pending = json.loads(row["confirmation_json"])
        concerns = pending.get("concerns", [])
        if not concerns:
            return None
        concern = next((c for c in concerns if c.get("audio_name") == concern_id), None) if concern_id else concerns[0]
        name = concern.get("audio_file", concern.get("audio_name")) if concern else None
        if not _is_confirmation_name(name):
            return None
        path = self.runtime_root / "items" / str(item_id) / "confirmation" / name
        timeline = pending.get('audio_timeline')
        if timeline and pending.get('audio_alignment') not in {'asr_chunk_v2', 'local_preview_10s_v3'}:
            # Upgrade only the playback artifact when requested. Never retry,
            # confirm, or rewrite an existing user task during app upgrade.
            aligned = path.with_suffix('.v2.wav')
            if not aligned.is_file():
                recovery = PrimaryRecovery(timeline['text'], None,
                    tuple(PrimaryChunk(**chunk) for chunk in timeline['chunks']),
                    timeline_status=timeline.get('timeline_status', 'unverified'))
                audio = StandardAudio(path.parent.parent / 'audio' / 'standard.wav', timeline['duration_seconds'])
                try:
                    self.confirmation_clipper.clip(audio, recovery, pending['snapshot'],
                        ReviewConcern(concern['start'], concern['end'], concern['text'], concern['reason'], True), aligned)
                except ConfirmationAudioError:
                    return None
            return aligned
        if path.is_file() and not path.is_symlink():
            return path
        # Missing local playback is a recovery state, never a full-audio task.
        return None

    def rerecognize_group(self, item_id, *, token, request_id, group_id, group_revision,
                          selected_member_uids, actor="local"):
        """Re-recognize only explicitly selected local clips as advisory text.

        This is not whole-material recognition and never replaces the snapshot.
        All references are published together after the original group CAS.
        """
        from copy import deepcopy
        import hashlib
        import tempfile
        from .confirmation_groups import plan_group
        from .confirmation_schema import digest
        from .confirmation_revision import ConfirmationConflict
        if (not isinstance(request_id,str) or not request_id or len(request_id)>128
                or not isinstance(selected_member_uids,list) or not selected_member_uids
                or any(not isinstance(u,str) for u in selected_member_uids)
                or len(set(selected_member_uids))!=len(selected_member_uids)
                or actor not in {'local','feishu'} or not isinstance(group_id,str)
                or not isinstance(group_revision,str)):
            raise ValueError('group_request_invalid')
        request=dict(request_id=request_id,group_id=group_id,group_revision=group_revision,
                     selected_member_uids=selected_member_uids,action='rerecognize_reference',value='')
        references=None
        for attempt in range(4):
            prior=self.store.group_decision(item_id,request)
            if prior is not None:return DistillResult(item_id,prior)
            row=self._item(item_id)
            if row['state']!='waiting_user' or row['confirmation_json'] is None:
                raise ConfirmationConflict('group_revision_conflict')
            if attempt==0 and (not token or json.loads(row['confirmation_json']).get('token')!=token):
                raise ConfirmationConflict('group_token_stale')
            pending=self.store.confirmation_view(item_id)
            if pending is None:continue
            check=plan_group(pending,{**request,'action':'keep'},actor=actor)
            if references is None:
                references={}
                if self.recognizer is None:raise ValueError('member_recognition_unavailable')
                for member in check.selected:
                    path=self.confirmation_audio(item_id,member['audio_name'])
                    if path is None or member.get('audio_recovery_required') or path.is_symlink():
                        raise ValueError('member_recognition_audio_unavailable')
                    try:
                        raw=path.read_bytes()
                        with tempfile.TemporaryDirectory(prefix='member-recognition-',dir=self.runtime_root) as temporary:
                            local=Path(temporary)/'clip.wav';local.write_bytes(raw)
                            with wave.open(str(local),'rb') as stream:
                                duration=stream.getnframes()/stream.getframerate()
                                if (stream.getframerate()!=16000 or stream.getnchannels()!=1
                                        or stream.getsampwidth()!=2 or not 0<duration<=60):
                                    raise ValueError('member_recognition_audio_invalid')
                            recognition=self.recognizer.recognize(StandardAudio(local,duration))
                        recovery=recognition.recovery
                        if (recognition.failure is not None or recovery is None or recovery.truncated
                                or not recovery.completed_normally or not recovery.text.strip()):
                            raise ValueError('member_recognition_incomplete')
                        references[member['concern_uid']]={
                            'kind':'local_clip_asr_reference','text':recovery.text,
                            'source_version_id':member['source_version_id'],
                            'audio_sha256':hashlib.sha256(raw).hexdigest(),'duration_seconds':duration,
                            'engine':type(self.recognizer).__name__,'request_id':request_id,
                            'notice':'局部原音重新识别参考，可能包含前后文；尚未替换来源或确认疑点。'}
                    except (OSError,ValueError,EOFError,wave.Error) as error:
                        raise ValueError('member_recognition_unavailable') from error
            updated=deepcopy(pending)
            updated['group_confirmation_contract']=1
            selected=set(selected_member_uids)
            for member in updated['concerns']:
                if member['concern_uid'] in selected:
                    member['recognition_reference']=references[member['concern_uid']]
                    member['decision_basis']={'kind':'local_recognition_reference',
                        'reference_hash':digest(references[member['concern_uid']])}
            groups=[];superseded=[]
            for group in updated['groups']:
                remaining=[u for u in group['member_uids'] if u not in selected]
                if remaining:groups.append({**group,'member_uids':remaining})
                elif group['member_uids']:superseded.append(group['group_id'])
                else:groups.append(group)
            for member in check.selected:
                uid=member['concern_uid']
                groups.append({'group_id':digest(['recognition-reference',pending['review_round_id'],uid,request_id]),
                    'member_uids':[uid],'equivalence_basis':{'kind':'single_member'},'formation_version':1})
            updated['groups']=groups
            updated['superseded_group_ids']=list(dict.fromkeys(pending.get('superseded_group_ids',[])+superseded))
            audit=[{**a,'action':'rerecognize_reference','recognition_reference':references[a['concern_uid']]} for a in check.audit]
            try:
                state=self.store.resolve_confirmation(item_id,row['confirmation_json'],
                    next_confirmation=updated,group_decision={**request,'audit':audit})
                return DistillResult(item_id,state)
            except ConfirmationConflict:
                if attempt==3:raise ConfirmationConflict('group_save_busy')
        raise ConfirmationConflict('group_save_busy')

    def rerecognize(self, item_id: int, *, token: str = "") -> DistillResult:
        row = self._item(item_id)
        pending = _pending_confirmation(row, token, allow_legacy=True)
        state = self.store.resolve_confirmation(item_id, row["confirmation_json"])
        self._remove_confirmation_audio(item_id, pending.get("concerns", []) + pending.get("deferred_concerns", []))
        return DistillResult(item_id, state)

    def _establish_source(self, item_id: int, row) -> None:
        if row["input_kind"] in {"direct_text", "markdown", "pdf", "epub", "image"}:
            self.store.mark_working(item_id, "reviewing")
            review_revision = self._item(item_id)['review_revision']
            source = self.store.submitted_source(item_id)
            try:
                parsed = parse_submitted_source(source, converter=self.documents, ocr=self.ocr)
            except SourceReadError as error:
                if not error.retryable:
                    self.store.reject_submitted_source(item_id, str(error))
                raise DistillError(str(error)) from error
            from .ocr_review_policy import review_parsed
            try:
                parsed = review_parsed(parsed, self.reviewer)
            except OcrError as error:
                if hasattr(error, 'partial_review'):
                    self.store.commit_source_review(item_id, review_revision, source.source_key,
                        {'failure': str(error), **error.partial_review})
                raise
            self.store.establish_submitted_fact(item_id, source, parsed,
                expected_revision=review_revision, review_result={'schema': 1,
                    'snapshot': parsed.snapshot, 'uncertainties': parsed.uncertainties,
                    'lineage': parsed.lineage})
            if self._item(item_id)["state"] == "waiting_user":
                raise DistillError("source_confirmation_required")
            return
        pending = json.loads(row["confirmation_json"]) if row["confirmation_json"] else None
        if pending and pending.get('group_confirmation_contract'):
            if row['state'] != 'waiting_user':
                self.store.mark_waiting(item_id,pending)
            return  # Only explicit member decisions and final review can release this gate.
        if pending and not pending["concerns"] and pending.get("deferred_concerns"):
            self._finish_partial_source(item_id, row, pending)
            return
        self.store.mark_working(item_id, "collecting")
        work_dir = self.runtime_root / "items" / str(item_id)
        captured = None
        from .intake import platform_for_url
        kind = platform_for_url(row['submitted_url'])
        if kind is None:
            raise DistillError('source_platform_unsupported')
        source = {'douyin': self.source, 'youtube': self.youtube_source,
                  'xiaohongshu': self.xiaohongshu_source, 'x': self.xpost_source,
                  'zhihu': self.zhihu_source, 'weibo': self.weibo_source,
                  'bilibili': self.bilibili_source}.get(kind)
        if source is None:
            raise DistillError(kind + '_runtime_unavailable')
        source_options = {'expected_authority': json.loads(row['platform_authority_json'])} if kind in {'youtube', 'xiaohongshu', 'x', 'zhihu', 'weibo'} else {}
        from .temporary_artifacts import TemporaryArtifacts
        try:
            TemporaryArtifacts(self.store,self.runtime_root).prepare(item_id)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DistillError('source_cleanup_failed') from error
        if row["material_id"] is not None:
            if row["source_kind"] != kind:
                raise DistillError("material_source_mismatch")
            metadata = json.loads(row["metadata_json"])
            if not isinstance(metadata, dict):
                raise DistillError("material_metadata_invalid")
            captured = source.reuse_retained(
                source_key=row["source_key"],
                submitted_url=row["submitted_url"],
                canonical_url=row["canonical_url"],
                metadata=metadata,
                work_dir=work_dir,
                **source_options,
            )
        if captured is None:
            captured = source.capture(row["submitted_url"], work_dir, **source_options)
        if captured.source_kind != kind:
            raise DistillError('material_source_mismatch')
        material_id = self.store.attach_material(item_id, captured)
        existing = self._item(item_id)
        if existing["source_fact_id"] is not None:
            return

        if kind in {'douyin', 'xiaohongshu', 'x', 'zhihu', 'weibo'} and captured.metadata.get('note_kind') == 'normal':
            review_revision = self._item(item_id)['review_revision']
            snapshot = captured.metadata['original_description'] if kind in {'x', 'zhihu', 'weibo'} else captured.metadata['source_title'] + '\n\n' + captured.metadata['original_description']
            fact, lineage = image_source_fact(snapshot, self.store.media_members(material_id), self.ocr,
                inline_images=captured.metadata.get('native_kind') == 'article',
                checkpoint_dir=self.runtime_root / 'items' / str(item_id) / 'ocr')
            if isinstance(self.reviewer, RecordedReviewer):
                from .ocr_review_policy import review_ocr
                try:
                    fact, lineage = review_ocr(fact, lineage, self.reviewer.binding.client)
                except OcrError as error:
                    if hasattr(error, 'partial_review'):
                        self.store.commit_source_review(item_id, review_revision, captured.source_key,
                            {'failure': str(error), **error.partial_review})
                    raise
            from .image_confirmation import pending_review
            pending = pending_review(fact, lineage)
            self.store.commit_source_review(item_id, review_revision, captured.source_key,
                {'schema': 1, 'snapshot': fact.snapshot, 'uncertainties': fact.uncertainties,
                 'lineage': lineage}, fact=fact if pending is None else None,
                lineage=lineage, confirmation=pending)
            return
        media = VerifiedTemporaryMedia(
            captured.source_kind,
            captured.source_key,
            captured.media_path,
            captured.duration_seconds,
        )
        self.store.mark_working(item_id, "reviewing")
        normalized = self.normalizer.normalize(media, work_dir / "audio")
        if normalized.failure is not None or normalized.audio is None:
            raise DistillError(f"audio_{normalized.failure or 'invalid'}")
        from .primary_cache import recognize_segmented
        from .subtitle_baseline import select_subtitle
        from knowledge_distiller.primary import PrimaryRecognition
        subtitle, source_lineage = select_subtitle(captured,normalized.audio)
        try:
            recognition = (PrimaryRecognition.succeeded(subtitle) if subtitle is not None
                else recognize_segmented(self.recognizer, normalized.audio, work_dir))
        except OSError as error:
            raise DistillError('asr_checkpoint_unavailable') from error
        if recognition.failure is not None or recognition.recovery is None:
            raise DistillError(f"asr_{recognition.failure or 'invalid'}")
        if kind == 'xiaohongshu':
            self.store.record_video_transcript(material_id, recognition.recovery.chunks)
        review_revision = self._item(item_id)['review_revision']
        review = (self.reviewer.review_in_directory(recognition.recovery, work_dir)
            if isinstance(self.reviewer, RecordedReviewer) else self.reviewer.review(recognition.recovery))
        import hashlib
        review_identity = hashlib.sha256(recognition.recovery.text.encode()).hexdigest()
        stage_result = {'schema': 1, 'source_sha256': review_identity,
            'user_revision': review_revision, **asdict(review)}
        if review.failure is not None or review.candidate is None:
            self.store.commit_source_review(item_id, review_revision, review_identity, stage_result)
            raise DistillError(f"review_{review.failure or 'invalid'}")
        lineage = {**source_lineage, ('primary_subtitle' if subtitle is not None else 'primary_asr'): asdict(recognition.recovery),
                   'ai_repairs': list(review.candidate.repairs),
                   'review_diagnostics': list(review.candidate.diagnostics)}
        blocking = []
        replay_recovery = recognition.recovery
        if any(c.meaning_may_change and locate_concern_audio(normalized.audio,
                replay_recovery, review.candidate.text, c) is None for c in review.candidate.concerns):
            from .audio_location_recovery import recover_locations
            try:
                replay_recovery = recover_locations(self.recognizer, normalized.audio,
                    recognition.recovery, work_dir)
            except (OSError, ValueError, EOFError, wave.Error):
                pass  # Keep the completed review and its unresolved local fields.
        generation = uuid4().hex
        for index, concern in enumerate(review.candidate.concerns, start=1):
            if not concern.meaning_may_change:
                continue
            name = f"concern-{index}-{generation}.wav"
            replay_note = ""
            try:
                self.confirmation_clipper.clip(
                    normalized.audio,
                    replay_recovery,
                    review.candidate.text,
                    concern,
                    work_dir / "confirmation" / name,
                )
            except ConfirmationAudioError:
                replay_note = " 局部原音定位恢复未完成，已保留疑点与原文，可重试恢复。"

            blocking.append(
                _compact_concern({
                    "start": concern.start_offset,
                    "end": concern.end_offset,
                    "text": concern.text,
                    "reason": concern.reason + replay_note,
                    "candidate_explanations": dict(concern.candidate_explanations),
                    "candidates": list(
                        dict.fromkeys((concern.text, *concern.candidate_readings))
                    ),
                    "audio_name": name,
                    "member_id": "primary-audio",
                    "audio_recovery_required": bool(replay_note),
                })
            )
        uncertainties = tuple(
            {
                "start": concern.start_offset,
                "end": concern.end_offset,
                "text": concern.text,
                "reason": concern.reason,
            }
            for concern in review.candidate.concerns
            if not concern.meaning_may_change
        ) + tuple(review.candidate.repairs)
        if not blocking:
            self.store.commit_source_review(item_id, review_revision, review_identity, stage_result,
                fact=SourceFact(review.candidate.text, uncertainties), lineage=lineage)
            return
        from .confirmation_groups import form_groups
        confirmation = {
                "snapshot": review.candidate.text, "concerns": blocking, "lineage": lineage,
                "review_identity": review_identity,
                "review_required": False,
                "audio_alignment": "local_preview_10s_v3",
                "uncertainties": list(uncertainties),
                "audio_timeline": {"text": recognition.recovery.text,
                    "chunks": [asdict(c) for c in replay_recovery.chunks],
                    "timeline_status": replay_recovery.timeline_status,
                    "duration_seconds": normalized.audio.duration_seconds},
            }
        group_client = self.reviewer.binding.client if isinstance(self.reviewer, RecordedReviewer) else None
        confirmation = form_groups(confirmation, item_id, group_client)
        self.store.commit_source_review(item_id, review_revision, review_identity,
                                        stage_result, confirmation=confirmation)

    def _finish_partial_source(self, item_id: int, row, pending: dict) -> None:
        if pending.get('group_confirmation_contract'):
            raise DistillError('group_unresolved_members_require_review')
        # Judge the remaining clear content before freezing an incomplete SourceFact.
        self.store.mark_working(item_id, "distilling")
        candidate = SourceFact(pending['snapshot'], tuple(pending['uncertainties']))
        if row['source_kind'] == 'xiaohongshu':
            from .xiaohongshu import native_video_fact
            candidate = native_video_fact(json.loads(row['metadata_json']), candidate)
        try:
            knowledge = self._knowledge_for_item(item_id).derive(candidate.snapshot, candidate.uncertainties)
        except KnowledgeModelError as error:
            if error.args != ("knowledge_not_qualified",):
                raise
            concerns = [{**c, "reason": "剩余明确内容不足以形成可靠知识，请补充此处文字或重新识别。"}
                        for c in pending["deferred_concerns"]]
            self.store.mark_waiting(item_id, {**pending, "concerns": concerns, "review_required": True})
            return
        fact = SourceFact(pending["snapshot"], tuple(pending["uncertainties"] + pending["resolved"]))
        fact_id = self.store.establish_source_fact(row["material_id"], fact)
        self.store.establish_knowledge(fact_id, knowledge)
        self._remove_confirmation_audio(item_id, pending["deferred_concerns"])

    def _finish(self, item_id: int) -> DistillResult:
        row = self._item(item_id)
        if row["source_fact_id"] is None:
            return DistillResult(item_id, "waiting_user")
        if row["knowledge_result_id"] is None:
            if self.store.prepare_image_review(item_id):
                row = self._item(item_id)
                if row['state'] == 'waiting_user':
                    return DistillResult(item_id, 'waiting_user')
            self.store.mark_working(item_id, "distilling")
            try:
                members = self.store.media_members(row['material_id']) if row['source_kind'] in {'xiaohongshu', 'x', 'weibo'} else []
                if any(m['mime_type'].startswith('image/') for m in members) and 'image_ocr' not in json.loads(row['lineage_json']):
                    raise DistillError('ocr_legacy_source_requires_review')
                knowledge = self._knowledge_for_item(item_id).derive(
                    row["snapshot"], json.loads(row["uncertainties_json"])
                )
            except KnowledgeModelError:
                raise
            self.store.establish_knowledge(int(row["source_fact_id"]), knowledge)
        self.store.mark_working(item_id, "publishing")
        if self.vault is None:
            raise DistillError("vault_not_configured")
        try:
            publication = publish(self.store, item_id, self.vault)
        except ValueError as error:
            raise DistillError(str(error)) from error
        if publication.state is PublicationState.CONFLICT:
            raise DistillError("obsidian_target_conflict")
        self.store.mark_succeeded(item_id)
        return DistillResult(item_id, "succeeded")

    def _knowledge_for_item(self, item_id):
        scope = getattr(self.knowledge_model, 'for_item', None)
        return scope(self.runtime_root / 'items' / str(item_id)) if callable(scope) else self.knowledge_model

    def _item(self, item_id: int):
        row = self.store.item_bundle(item_id)
        if row is None:
            raise LookupError(f"distill item {item_id} does not exist")
        return row

    def _remove_confirmation_audio(
        self, item_id: int, concerns: list[dict[str, object]]
    ) -> None:
        root = self.runtime_root / "items" / str(item_id) / "confirmation"
        for concern in concerns:
            for name in {concern.get("audio_name"), concern.get('audio_file')}:
                if _is_confirmation_name(name):
                    try:
                        (root / name).unlink(missing_ok=True)
                        (root / name).with_suffix('.v2.wav').unlink(missing_ok=True)
                    except OSError:
                        pass


def _compact_concern(concern: dict) -> dict:
    if concern.get("candidate_explanations") or any(c.isascii() and c.isalpha() for c in concern["text"]):
        return concern
    readings = list(dict.fromkeys(concern["candidates"]))
    if len(readings) < 2 or all(len(s) <= 4 for s in readings):
        return concern
    prefix = 0
    while all(len(s) > prefix and s[prefix] == readings[0][prefix] for s in readings):
        prefix += 1
    suffix = 0
    while all(len(s) - prefix > suffix and s[-suffix - 1] == readings[0][-suffix - 1] for s in readings):
        suffix += 1
    if any(len(s) == prefix + suffix for s in readings):
        if prefix:
            prefix -= 1
        elif suffix:
            suffix -= 1
    short = [s[prefix:len(s) - suffix if suffix else None] for s in readings]
    if not all(0 < len(s) <= 4 for s in short):
        return concern
    return {**concern, "start": concern["start"] + prefix,
            "end": concern["end"] - suffix, "text": short[0], "candidates": short}


def _pending_confirmation(row, token: str, *, allow_legacy: bool = False) -> dict:
    if row["state"] != "waiting_user" or row["confirmation_json"] is None:
        raise ValueError("item is not waiting for source confirmation")
    pending = json.loads(row["confirmation_json"])
    current = pending.get("token", "")
    if token != current or (not current and not allow_legacy):
        from .confirmation_revision import ConfirmationConflict
        raise ConfirmationConflict("旧版待确认请重新识别。" if not current else "来源确认已更新，请查看当前疑点；输入已保留。")
    pending.setdefault("review_required", True)
    return pending


def _is_confirmation_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"concern-[1-9][0-9]*(?:-[0-9a-f]{32})?\.wav", value) is not None
    )
