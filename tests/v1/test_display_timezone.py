"""Display-only timezone boundaries; no database, Vault, browser or service."""
from copy import deepcopy
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from flask import Flask
import pytest

from knowledge_distiller.v1 import insight_web, topic_web


@pytest.mark.parametrize('value,expected', [
    ('2025-12-31T15:59:00+00:00', '更新于 2025 年 12 月 31 日'),
    ('2025-12-31T16:00:00+00:00', '更新于 1 月 1 日'),
    ('2025-12-31T09:00:00-07:00', '更新于 1 月 1 日'),
    ('2026-01-01T00:30:00+09:00', '更新于 2025 年 12 月 31 日'),
])
def test_topic_date_uses_taipei_for_both_now_and_timestamp(monkeypatch, value, expected):
    zones = []

    def zone(name):
        zones.append(name)
        return ZoneInfo(name)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz.key == 'Asia/Taipei'
            return cls(2026, 1, 1, 0, 5, tzinfo=tz)

    monkeypatch.setattr(topic_web, 'datetime', FixedDatetime)
    monkeypatch.setattr(topic_web, 'ZoneInfo', zone)
    assert topic_web._date(value) == expected
    assert zones == ['Asia/Taipei', 'Asia/Taipei']


@pytest.mark.parametrize('annotation_time,expected_annotation', [
    ('2026-10-07T09:05:00-07:00', '2026 年 10 月 8 日 00:05'),
    (None, ''),
])
def test_insight_route_formats_event_annotation_and_notes_in_taipei(
        monkeypatch, annotation_time, expected_annotation):
    item = dict(sources=[], time='2026-10-07T16:03:00+00:00',
                annotation_time=annotation_time,
                notes=[dict(created_at='2026-10-08T00:06:00+09:00')])
    before = deepcopy(item)
    zones = []

    class SyntheticLibrary:
        def __init__(self, store):
            assert store is sentinel

        def list(self, state):
            assert state == 'pending'
            return [deepcopy(item)]

    def zone(name):
        zones.append(name)
        return ZoneInfo(name)

    def render(template, **context):
        assert template == 'insights.html' and context['error'] is None
        return json.dumps(context['items'], ensure_ascii=False)

    sentinel = object()
    monkeypatch.setattr(insight_web, 'InsightLibrary', SyntheticLibrary)
    monkeypatch.setattr(insight_web, 'ZoneInfo', zone)
    monkeypatch.setattr(insight_web, 'render_template', render)
    app = Flask(__name__)
    app.register_blueprint(insight_web.insight_blueprint(sentinel, lambda *_: ''))
    response = app.test_client().get('/insights')
    assert response.status_code == 200
    displayed = json.loads(response.text)[0]
    assert displayed['date'] == '2026 年 10 月 8 日 00:03'
    assert displayed['annotation_date'] == expected_annotation
    assert displayed['notes'][0]['date'] == '2026 年 10 月 7 日 23:06'
    assert zones == ['Asia/Taipei'] * (3 if annotation_time else 2)
    assert item == before
