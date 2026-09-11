from __future__ import annotations

from datetime import datetime
import sqlite3
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, render_template, request
from knowledge_distiller.knowledge_library import normalize_search_text
from .topics import TopicLibrary, TopicError


def search_records(snapshot, points, query):
    normalized = normalize_search_text(query)
    terms = tuple(dict.fromkeys(normalized.split()))
    if not terms:
        return [], []
    topics = [topic for topic in snapshot['topics'] if all(
        term in normalize_search_text(topic['name'] + ' ' + topic['scope']) for term in terms)]
    matches = []
    for point in points:
        statement = normalize_search_text(point['statement'])
        argument = normalize_search_text(point['argument'])
        context = normalize_search_text(point['title'] + ' ' + point['summary'])
        hits = [term in statement or term in argument for term in terms]
        if any(hits) and all(hit or term in context for hit, term in zip(hits, terms)):
            rank = (statement == normalized, normalized in statement,
                    sum(term in statement for term in terms), sum(term in argument for term in terms),
                    point['role'] == 'core')
            matches.append((rank, point))
    matches.sort(key=lambda item: (*(-int(value) for value in item[0]),
                                    item[1]['knowledge_result_id'], item[1]['point_id']))
    return topics, [point for _, point in matches]


def _date(value):
    current = datetime.now(ZoneInfo('Asia/Shanghai'))
    date = datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai'))
    year = f'{date.year} 年 ' if date.year != current.year else ''
    return f'更新于 {year}{date.month} 月 {date.day} 日'


def topic_blueprint(store, obsidian_url):
    blueprint = Blueprint('topics', __name__)
    library = TopicLibrary(store)

    def point_view(point):
        value = dict(point)
        kind = point['source_kind']
        from .intake import LABELS
        label = '整段文本' if kind == 'direct_text' else LABELS.get(kind, kind)
        author = point['metadata'].get('author')
        if isinstance(author, dict):
            author = author.get('display_name')
        value['source_label'] = '来源：' + label + (f' · {author}' if isinstance(author, str) and author.strip() else '')
        value['obsidian_url'] = obsidian_url(point['published_vault'], point['published_path'])
        url = point['submitted_url'] if kind == 'xiaohongshu' else point['canonical_url']
        value['source_url'] = url if urlsplit(url).scheme in ('https', 'http') else None
        value['file_source'] = kind in ('markdown', 'pdf', 'epub')
        return value

    @blueprint.get('/topics')
    def index():
        query = request.args.get('q', '')
        try:
            snapshot = library.snapshot()
            topic_rows = snapshot['topics']
            points = []
            insights = []
            searching = bool(normalize_search_text(query))
            if searching:
                topic_rows, points = search_records(snapshot, library.read_points(), query)
                from .insights import InsightLibrary
                terms = normalize_search_text(query).split()
                insights = [item for item in InsightLibrary(store).list('interesting')
                    if all(term in normalize_search_text(item['payload'].claim + ' ' + item['payload'].short_discussion) for term in terms)]
            for topic in topic_rows:
                topic['date'] = _date(topic['updated_at'])
            return render_template('topics.html', snapshot=snapshot, topics=topic_rows,
                points=[point_view(p) for p in points], insights=insights, query=query, searching=searching, error=None)
        except (TopicError, sqlite3.Error, ValueError):
            return render_template('topics.html', snapshot=None, topics=[], points=[], query=query,
                searching=bool(query.strip()), error='暂时无法完整读取知识，请稍后重试。'), 503

    @blueprint.get('/topics/<int:topic_id>')
    def detail(topic_id):
        try:
            snapshot = library.snapshot()
            topic = next((t for t in snapshot['topics'] if t['id'] == topic_id), None)
            if topic is None:
                abort(404)
            all_points = {(p['knowledge_result_id'], p['point_id']): p for p in library.read_points(knowledge_ids={m['knowledge_result_id'] for m in topic['members']})}
            points = [point_view(all_points[(m['knowledge_result_id'], m['point_id'])]) for m in topic['members']]
            topic['date'] = _date(topic['updated_at'])
            return render_template('topic_detail.html', topic=topic, points=points)
        except (TopicError, sqlite3.Error, KeyError, ValueError):
            return render_template('topics.html', snapshot=None, topics=[], points=[], query='',
                searching=False, error='暂时无法完整读取这个主题，已有主题没有改变。'), 503

    return blueprint
