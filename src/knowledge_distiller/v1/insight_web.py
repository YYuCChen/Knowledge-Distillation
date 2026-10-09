from datetime import datetime
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, request, redirect, url_for
from .insights import InsightLibrary


def insight_blueprint(store, obsidian_url, publication_file=lambda *args: None):
    blueprint = Blueprint('insights', __name__)
    library = InsightLibrary(store)

    def view(item):
        item['operation_id'] = uuid4().hex
        item['sources'] = list({p['knowledge_result_id']: dict(p,
            obsidian_url=obsidian_url(p['published_vault'], p['published_path']),
            publication_saved=publication_file(p['published_vault'], p['published_path']) is not None)
            for p in item['sources']}.values())
        def formatted(value):
            if not value:
                return ''
            date = datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai'))
            return f'{date.year} 年 {date.month} 月 {date.day} 日 {date:%H:%M}'
        item['date'] = formatted(item['time'])
        item['annotation_date'] = formatted(item['annotation_time'])
        for note in item['notes']:
            note['date'] = formatted(note['created_at'])
        return item

    @blueprint.get('/insights')
    def index():
        state = request.args.get('state', 'pending')
        if state not in ('pending', 'interesting', 'rethink'):
            return '无效的新知筛选。', 400
        try:
            items = [view(item) for item in library.list(state)]
        except (ValueError, sqlite3.Error):
            return render_template('insights.html', items=[], state=state, error='暂时无法完整读取新知，请稍后重试。'), 503
        return render_template('insights.html', items=items, state=state, error=None)

    @blueprint.post('/insights/<int:version_id>/<action>')
    def mutate(version_id, action):
        try:
            if action == 'judge':
                library.judge(version_id, request.form.get('decision'), request.form.get('text'))
                state = 'pending'
            elif action == 'reconsider':
                library.reconsider(version_id, request.form.get('operation_id'), request.form.get('text', ''))
                state = 'rethink'
            elif action == 'idea':
                library.add_idea(version_id, request.form.get('operation_id'), request.form.get('text', ''))
                state = 'interesting'
            else:
                return '这个操作不可用。', 404
        except (ValueError, sqlite3.Error):
            return '本次未能保存，请保留输入后重试；已有记录没有改变。', 409
        if request.headers.get('X-Requested-With') == 'insight':
            if action == 'idea':
                return render_template('insight_card.html', item=view(library.read(version_id)), number=request.form.get('number', ''), expanded=True)
            return '', 204
        return redirect(url_for('insights.index', state=state))

    return blueprint
