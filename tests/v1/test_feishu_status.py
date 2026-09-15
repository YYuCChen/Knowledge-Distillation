from knowledge_distiller.v1.feishu_status import project


def row(state, error=None):
    return {'state': state, 'error_code': error}


def test_mixed_counts_are_exclusive_and_action_has_priority():
    items=[row('succeeded'), row('failed','knowledge_not_qualified'), row('failed','network'),
           row('waiting_user'), row('queued'), row('working')]
    value=project({'state':'received'},items,actionable=True)
    assert value['label']=='待你操作'
    assert value['counts']==dict(succeeded=1,unqualified=1,failed=1,waiting=1,unfinished=2)
    assert sum(value['counts'].values())==len(items)
    assert project({'state':'received'},items)['label']=='需处理'


def test_terminal_states_do_not_call_content_rejection_technical_failure():
    receipt={'state':'received'}
    assert project(receipt,[row('failed','knowledge_not_qualified')])['label']=='未形成知识'
    mixed=project(receipt,[row('succeeded'),row('failed','knowledge_not_qualified')])
    assert mixed['label']=='已完成'
    assert '部分形成知识' in mixed['summary']
    assert project(receipt,[row('working')])['label']=='处理中'
    assert project(receipt,[row('queued')])['label']=='已接收／等待中'
    assert project(receipt,[row('unrecognized')])['label']=='需处理'


def test_uncreated_parts_never_inflate_task_counts():
    value=project({'state':'needs_desktop'},[row('succeeded')],[{'error':'download failed'}])
    assert value['counts']['failed']==0
    assert '尚未建立任务' in value['count_text']
    assert value['label']=='需处理'
