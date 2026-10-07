from datetime import datetime
import sqlite3
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, request
from .insights import InsightLibrary


def insight_blueprint(store, obsidian_url, publication_file=lambda *args: None):
    blueprint = Blueprint('insights', __name__)
    library = InsightLibrary(store)

    def view(item):
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
        # V3 keeps the complete V1 history available for reading while the
        # user-confirmed migration is prepared separately. Keeping the old URL
        # explicit prevents stale tabs and scripted clients from writing it.
        return '历史新知已转为只读；已有判断和想法保持不变。', 410

    return blueprint
