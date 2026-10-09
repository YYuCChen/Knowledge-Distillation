"""Review OCR against a local crop before the source fact is frozen."""
from copy import deepcopy
from io import BytesIO
import math

from .domain import SourceFact
from .ocr import decode_image, OcrError
from .ocr_review_policy import relocate_ocr, _primary_sha


def pending_review(fact, lineage):
    if 'image_ocr' not in lineage:
        return None
    concerns = []
    for uncertainty in fact.uncertainties:
        if uncertainty.get('by') != 'ocr' or uncertainty.get('status') != 'unresolved':
            continue
        image = next(i for i in lineage['image_ocr'] if i['member_id'] == uncertainty['member_id'])
        line = next(l for l in image['lines'] if l['start'] == uncertainty['start'] and l['end'] == uncertainty['end'])
        concerns.append({**uncertainty, 'polygon': line['polygon'],
                         'audio_name': f"ocr-{len(concerns)+1}", 'candidates': [uncertainty['text'], *line.get('alternatives', [])]})
    return {'kind': 'image', 'snapshot': fact.snapshot, 'lineage': lineage,
            'uncertainties': list(fact.uncertainties), 'concerns': concerns,
            'resolved': [], 'review_required': False} if concerns else None


def resolve_review(store, row, pending, action, value, concern_id, *, decision=None):
    index = next((n for n,c in enumerate(pending['concerns']) if c['audio_name'] == concern_id), -1)
    if index < 0:
        raise ValueError('疑点已更新，请刷新后再操作。')
    concern = pending['concerns'][index]
    if action == 'candidate' and value == concern['text']:
        replacement = value
    elif action == 'manual' and value.strip():
        replacement = value.strip()
    else:
        raise ValueError('请对照原图确认当前文字，或填写正确文字。')
    start, end = concern['start'], concern['end']
    try:
        _primary_sha(pending['snapshot'], pending['lineage'])
    except OcrError as error:
        raise ValueError('来源确认已更新，请刷新后再操作。') from error
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(pending['snapshot'])
            or pending['snapshot'][start:end] != concern['text']):
        raise ValueError('来源确认已更新，请刷新后再操作。')
    updated = deepcopy(pending)
    if row['source_fact_id'] is not None:
        if pending['snapshot'][:start] + replacement + pending['snapshot'][end:] != row['snapshot']:
            raise ValueError('来源已由另一次确认成立，不能覆盖原事实。')
        return store.resolve_confirmation(row['item_id'], row['confirmation_json'])
    edits = [] if replacement == concern['text'] else [{
        'start': start, 'end': end, 'text': concern['text'], 'replacement': replacement,
        'member_id': concern['member_id'], 'by': 'human', 'action': action,
        'concern_uid': concern.get('concern_uid'), 'source_version_id': concern.get('source_version_id'),
        'original_span': concern.get('original_span', [start, end])}]
    remaining = [u for u in pending['uncertainties'] if not (
        u.get('by') == 'ocr' and u.get('member_id') == concern['member_id']
        and u.get('start') == start and u.get('end') == end)]
    try:
        updated['snapshot'], updated['lineage'], updated['uncertainties'], updated['concerns'], updated['resolved'] = relocate_ocr(
            pending['snapshot'], pending['lineage'], remaining, edits,
            concerns=pending['concerns'][:index] + pending['concerns'][index+1:], resolved=pending['resolved'])
    except OcrError as error:
        raise ValueError('来源确认已更新，请刷新后再操作。') from error
    final_start = start
    final_end = start + len(replacement)
    updated['resolved'].append({'by': 'human', 'member_id': concern['member_id'],
        'text': concern['text'], 'replacement': replacement, 'action': action,
        'start': final_start, 'end': final_end, 'confirmed_span': [start, end],
        'original_span': deepcopy(concern.get('original_span', [start, end])),
        **{key: deepcopy(concern[key]) for key in ('concern_uid', 'source_version_id',
            'review_round_id', 'original_review_hash', 'reason', 'evidence') if key in concern}})
    if updated['concerns']:
        return store.resolve_confirmation(row['item_id'], row['confirmation_json'], decision=decision, next_confirmation=updated)
    return store.resolve_confirmation(row['item_id'], row['confirmation_json'], decision=decision,
        fact=SourceFact(updated['snapshot'], tuple(updated['uncertainties']+updated['resolved'])),
        lineage=updated['lineage'])


def crop_original(member, concern):
    image = decode_image(member['content'], member['mime_type'])
    xs, ys = zip(*concern['polygon'])
    height = max(1, max(ys)-min(ys))
    # Include about one neighboring line above/below, never the whole long image.
    box = (max(0, math.floor(min(xs)-height*.4)), max(0, math.floor(min(ys)-height*1.4)),
           min(image.width, math.ceil(max(xs)+height*.4)), min(image.height, math.ceil(max(ys)+height*1.4)))
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError('图片疑点位置无效。')
    output = BytesIO()
    image.crop(box).save(output, format='PNG')
    output.seek(0)
    return output
