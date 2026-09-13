"""Freeze OCR text together with its immutable image coordinates."""
from .domain import SourceFact
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import platform
import sys


def _recognize(member, runner, checkpoint_dir):
    if checkpoint_dir is None:
        return runner.recognize_bytes(member['content'], member['mime_type'])
    from .ocr import OcrError, OcrLine, OcrResult, OCR_VERSION, PADDLE_VERSION, PADDLEX_VERSION
    from .vision_ocr import VISION_REVISION
    from .local_records import write_record
    # Content and adapter contract, not source ordering or a transient filename,
    # own a completed image result. Incomplete output is never reused.
    key = [hashlib.sha256(member['content']).hexdigest(), member['mime_type'],
           type(runner).__module__, type(runner).__qualname__, OCR_VERSION,
           PADDLE_VERSION, PADDLEX_VERSION, sys.platform, platform.mac_ver()[0], VISION_REVISION, 1]
    path = Path(checkpoint_dir) / (hashlib.sha256(json.dumps(key).encode()).hexdigest()+'.json')
    if path.parent.is_symlink() or path.is_symlink():
        raise OcrError('ocr_checkpoint_unavailable')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as cause:
        raise OcrError('ocr_checkpoint_unavailable') from cause
    if not path.is_symlink() and path.is_file():
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            payload = record['result']
            if record['key'] == key and record['sha256'] == hashlib.sha256(
                    json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest():
                payload['lines'] = tuple(OcrLine(line['text'],
                    tuple(tuple(p) for p in line['polygon']), line['confidence'],
                    tuple(line['alternatives']), tuple(tuple(p) for p in line['original_polygon']))
                    for line in payload['lines'])
                return OcrResult(**payload)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # An unreadable checkpoint cannot become accepted OCR evidence.
    result = runner.recognize_bytes(member['content'], member['mime_type'])
    payload = asdict(result)
    try:
        write_record(path, {'key': key, 'result': payload, 'sha256': hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
    except OSError as cause:
        error = OcrError('ocr_checkpoint_unavailable')
        error.line_diagnostics = [dict(line, evidence_valid=True) for line in payload['lines']]
        raise error from cause
    return result


def image_source_fact(native_text, members, runner, *, inline_images=False, checkpoint_dir=None):
    snapshot = '' if inline_images else native_text
    cursor = 0
    images = []
    uncertainties = []
    for member in members:
        if not member['mime_type'].startswith('image/'):
            continue
        from .ocr import OcrError
        try:
            result = _recognize(member, runner, checkpoint_dir)
        except OcrError as error:
            error.member_id = member['member_id']
            error.completed_members = [image['member_id'] for image in images]
            error.completed_images = images
            raise
        if inline_images:
            marker = f"〔图片 {member['member_id'].split('-')[-1]}〕"
            position = native_text.find(marker, cursor)
            if position < 0:
                raise ValueError('article image position is missing')
            snapshot += native_text[cursor:position+len(marker)]
            cursor = position+len(marker)
        image = {'member_id': member['member_id'], 'sha256': member['sha256'],
                 'width': result.width, 'height': result.height,
                 'engine': result.engine, 'runtime_version': result.runtime_version,
                 'detection_model': result.detection_model,
                 'recognition_model': result.recognition_model, 'lines': []}
        if inline_images:
            image['source_start'] = len(snapshot) - len(marker)
            image['source_end'] = len(snapshot)
        if result.lines:
            snapshot += ('\n\n' if snapshot else '') + f"[图片 {member['member_id']} OCR]\n"
        for index, line in enumerate(result.lines):
            if index:
                snapshot += '\n'
            start = len(snapshot)
            snapshot += line.text
            entry = {'start': start, 'end': len(snapshot), 'text': line.text,
                     'polygon': line.polygon, 'confidence': line.confidence}
            if line.original_polygon:
                entry['original_polygon'] = line.original_polygon
                entry['coordinate_adjustment'] = 'vision_boundary_roundoff_1e-4px'
            if line.alternatives:
                entry['alternatives'] = list(line.alternatives)
            image['lines'].append(entry)
            if line.confidence < .8 or line.alternatives:
                # Vision's normalized score is not a calibrated error probability.
                # Preserve it without borrowing Paddle's blocking threshold.
                blocking = bool(line.alternatives) or result.engine != 'apple_vision'
                uncertainties.append({'start': start, 'end': len(snapshot),
                    'text': line.text, 'status': 'unresolved' if blocking else 'advisory', 'by': 'ocr',
                    'reason': '存在同分识别候选，需对照原图' if line.alternatives else
                        '图片文字识别置信度较低' if blocking else 'Apple Vision 原始评分，仅作诊断，不代表文字错误',
                    'member_id': member['member_id']})
        images.append(image)
        if inline_images and result.lines:
            snapshot += '\n\n'
    if inline_images:
        snapshot += native_text[cursor:]
    return SourceFact(snapshot if snapshot.strip() else '[原始图片来源]', tuple(uncertainties)), {'image_ocr': images}
