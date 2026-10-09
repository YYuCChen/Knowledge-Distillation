"""Review OCR against a local crop before the source fact is frozen."""
from copy import deepcopy
from io import BytesIO
import math

from .domain import SourceFact
from .ocr import decode_image


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
    if pending['snapshot'][start:end] != concern['text']:
        raise ValueError('来源确认已更新，请刷新后再操作。')
    updated = deepcopy(pending)
    updated['snapshot'] = pending['snapshot'][:start] + replacement + pending['snapshot'][end:]
    if row['source_fact_id'] is not None:
        if updated['snapshot'] != row['snapshot']:
            raise ValueError('来源已由另一次确认成立，不能覆盖原事实。')
        return store.resolve_confirmation(row['item_id'], row['confirmation_json'])
    delta = len(replacement) - (end-start)
    def shift(entry):
        if entry.get('start', -1) >= end:
            entry['start'] += delta
            entry['end'] += delta
    updated['concerns'].pop(index)
    for c in updated['concerns']:
        shift(c)
    updated['uncertainties'] = [u for u in updated['uncertainties']
                               if not (u.get('by') == 'ocr' and u.get('start') == start and u.get('end') == end)]
    for u in updated['uncertainties']:
        shift(u)
    for image in updated['lineage']['image_ocr']:
        if image.get('source_start', -1) >= end:
            image['source_start'] += delta
            image['source_end'] += delta
        for line in image['lines']:
            if line['start'] == start and line['end'] == end:
                line.update(original_text=line['text'], text=replacement, end=start+len(replacement), confirmed_by='human')
            else:
                shift(line)
    updated['resolved'].append({'by': 'human', 'member_id': concern['member_id'],
                                'text': concern['text'], 'replacement': replacement})
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
