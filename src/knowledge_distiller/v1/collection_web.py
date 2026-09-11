from flask import Blueprint, abort, redirect, render_template, request, url_for

from .chrome import ChromeSessionError
from .collections import PreviewChanged, PartialConfirmation
from .douyin_collections import CollectionError
from .bilibili import BilibiliSourceError

MESSAGES = {
    'collection_runtime_unavailable': '集合采集尚未就绪，请从日常启动入口打开程序。',
    'collection_input_unsupported': '请提交抖音主页、合集，或同一话题的抖音作品链接。',
    'collection_membership_incomplete': '没有取得完整的内容范围，本次没有提交。可以重新读取。',
    'collection_identity_mismatch': '来源身份与链接不一致，本次没有提交。',
    'collection_scope_changed': '范围已变化，请核对新范围后重新确认。',
    'collection_connection_changed': '抖音连接已变化，请重新读取并确认范围。',
    'collection_no_supported_members': '这个范围没有当前可处理的作品，本次没有提交。',
    'collection_empty': '所选合集目前没有内容，本次没有提交。可以返回选择其他合集。',
    'collection_preview_expired': '这份范围预览已失效，请重新投递链接。',
    'collection_confirmation_mismatch': '确认内容与预览不一致，请重新读取范围。',
    'collection_topic_confirmation_required': '请明确确认这些作品属于同一话题。',
    'collection_command_stale': '状态已经变化，请按当前页面重新操作。',
    'collection_nothing_to_retry': '没有可继续或重试的成员；请先处理来源疑点，或重新提交新的范围。',
    'collection_upstream_failed': '抖音暂时没有返回可用的范围数据，本次没有提交。',
    'collection_browser_unavailable': '浏览器连接不可用，请检查抖音连接后重试。',
    'collection_member_changed': '这条内容已变化，原有范围已保留；处理新版本需要重新投递并确认。',
    'collection_combined_not_qualified': '全部成员已收录，但没有形成有充分依据的集合综合。',
    'collection_combined_invalid': '全部成员已收录，但综合结果的来源依据未通过校验。',
    'collection_combined_failed': '全部成员已收录，集合综合暂未完成，可以重试综合。',
    'collection_processing_failed': '集合处理未完成，已保存的结果保留，可以重试。',
}
STATE_LABELS = {'queued':'等待中','working':'处理中','waiting_user':'待确认','partial':'部分完成',
                'failed':'需要处理','succeeded':'已完成','cancelled':'已停止'}


def cards(service):
    return [card(item) for item in service.list()]


def card(item):
    members = item['members']
    counts = {state:sum(m['state']==state for m in members) for state in ('queued','working','waiting_user','failed','succeeded')}
    return {**item,'label':STATE_LABELS[item['state']],'counts':counts,'total':len(members)}


def collection_blueprint(store, service, wake, error_text):
    bp = Blueprint('collections', __name__)
    def message(error):
        return error_text.get(str(error),MESSAGES.get(str(error),'本次操作未完成，已有内容保留。'))

    @bp.get('/collections/preview/<token>')
    def preview(token):
        try:
            value = service._draft(token)
        except CollectionError as error:
            return render_template('collection_preview.html',preview=None,error=message(error)),400
        return render_template('collection_preview.html',preview=value,error=MESSAGES['collection_scope_changed'] if request.args.get('changed') else None)

    @bp.post('/collections/select')
    def select():
        mode = request.form.get('mode')
        selected = request.form.getlist('collection') if mode=='selected' else [mode] if mode in {'full_profile','all_collections'} else []
        try:
            value = service.select(request.form.get('token',''),selected)
        except (CollectionError,ChromeSessionError,BilibiliSourceError) as error:
            return render_template('collection_preview.html',preview=None,error=message(error)),400
        return redirect(url_for('collections.preview',token=value['token']))

    @bp.post('/collections/confirm')
    def confirm():
        try:
            operations = service.confirm(request.form.get('token',''),request.form.getlist('signature'),
                                         same_topic=request.form.get('same_topic')=='yes')
        except PartialConfirmation as error:
            if wake:wake()
            return render_template('collection_preview.html',preview=None,
                error=f'上次确认中断，已接收 {len(error.operations)} / {error.expected} 个合集。已接收的内容可在首页查看；其余范围请重新投递确认。'),409
        except PreviewChanged as error:
            return redirect(url_for('collections.preview',token=error.preview['token'],changed=1))
        except (CollectionError,ChromeSessionError,BilibiliSourceError) as error:
            return render_template('collection_preview.html',preview=None,error=message(error)),400
        if wake:wake()
        if len(operations)==1:
            return redirect(url_for('collections.detail',operation=operations[0]))
        return redirect(url_for('home'))

    @bp.post('/collections/dismiss')
    def dismiss():
        from .web import _home_context
        token = request.form.get('token', '')
        try:
            draft = service._draft(token)['submitted_text']
        except CollectionError as error:
            return render_template('home.html', **_home_context(
                store, None, form_error=message(error))), 400
        service.dismiss(token)
        return render_template('home.html', **_home_context(store, None, draft=draft))

    def view(operation, error=None):
        try:
            info=card(service.detail(operation))
        except LookupError:
            abort(404)
        titles={m['item_id']:m for m in info['manifest']['members']}
        for member in info['members']:
            member['title']=titles[member['native_id']]['title'] or '作品 '+member['native_id']
            member['url']=titles[member['native_id']].get('url') or 'https://www.douyin.com/video/' + member['native_id']
            member['label']='暂不支持' if member['known_unsupported'] else STATE_LABELS[member['state']]
            member['error']=error_text.get(member['error_code'],MESSAGES.get(member['error_code'],'本条暂未完成。')) if member['error_code'] else None
        info['error']=error_text.get(info['error_code'],MESSAGES.get(info['error_code'],'集合暂未完成。')) if info['error_code'] else None
        if info['result']:
            ids={m['native_id']:m['item_id'] for m in info['members']}
            for point in info['result']['points']:
                for ref in point['supports']:ref['item_id']=ids[ref['native_id']]
        return render_template('collection_detail.html',collection=info,error=error)

    @bp.get('/collections/<int:operation>')
    def detail(operation):return view(operation)

    @bp.post('/collections/<int:operation>/<action>')
    def command(operation,action):
        try:
            revision=int(request.form.get('revision',''))
            if action=='cancel':service.cancel(operation,revision)
            elif action=='resume':service.resume(operation,revision)
            else:abort(404)
        except (ValueError,CollectionError) as error:
            return view(operation,message(error)),409
        if wake:wake()
        return redirect(url_for('collections.detail',operation=operation))

    return bp
