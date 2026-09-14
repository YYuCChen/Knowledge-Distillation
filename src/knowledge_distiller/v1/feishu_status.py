"""Mutually exclusive task counts and six receipt business states."""


def project(receipt, items, parts=(), *, actionable=False):
    counts = dict(succeeded=0, unqualified=0, failed=0, waiting=0, unfinished=0)
    unknown = False
    for row in items:
        state = row['state']
        if state == 'succeeded': key = 'succeeded'
        elif state == 'failed': key = 'unqualified' if row['error_code'] == 'knowledge_not_qualified' else 'failed'
        elif state == 'waiting_user': key = 'waiting'
        else:
            key = 'unfinished'
            unknown |= state not in {'working', 'queued'}
        counts[key] += 1
    part_failures = sum(bool(part['error']) for part in parts)
    if actionable:
        label, summary = '待你操作', '请完成下方当前可执行的选择或确认。'
    elif counts['failed'] or part_failures or unknown or receipt['state'] in {'needs_desktop', 'rejected'}:
        label, summary = '需处理', '部分内容需要处理；已保存的其他结果保留。'
    elif any(row['state'] == 'working' for row in items):
        label, summary = '处理中', '电脑正在处理本次内容。'
    elif counts['unfinished'] or not items:
        label, summary = '已接收／等待中', '内容已保存，等待电脑处理。'
    elif counts['waiting']:
        label, summary = '需处理', '待确认内容暂时没有可执行入口，请在电脑检查。'
    elif counts['succeeded']:
        label = '已完成'
        summary = '部分形成知识，其余内容未达到知识要求。' if counts['unqualified'] else '本次内容已形成知识。'
    else:
        label, summary = '未形成知识', '内容未达到知识要求，未生成知识笔记。'
    count_text = ' · '.join(f'{label} {counts[key]}' for key, label in (
        ('succeeded', '成功'), ('unqualified', '不合格'), ('failed', '失败'),
        ('waiting', '待确认任务'), ('unfinished', '未完成')))
    if part_failures:
        count_text += f'；另有 {part_failures} 项接收失败（尚未建立任务）'
    return {'label': label, 'summary': summary, 'counts': counts, 'count_text': count_text}
