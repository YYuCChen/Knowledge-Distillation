"""Freeze OCR text together with its immutable image coordinates."""
from .domain import SourceFact


def image_source_fact(native_text, members, runner, *, inline_images=False):
    snapshot = '' if inline_images else native_text
    cursor = 0
    images = []
    uncertainties = []
    for member in members:
        if not member['mime_type'].startswith('image/'):
            continue
        result = runner.recognize_bytes(member['content'], member['mime_type'])
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
