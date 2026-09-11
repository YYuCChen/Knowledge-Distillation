"""App-owned page presence; no browser inspection or macOS privacy permissions."""
from __future__ import annotations

import json
import secrets
import threading
import time

from flask import Response, abort, request


class DesktopPages:
    def __init__(self, *, probe_timeout=0.8):
        self.token = secrets.token_urlsafe(32)
        self.condition = threading.Condition(threading.RLock())
        self.connected = set()
        self.selected = None
        self.generation = 0
        self.ack = 0
        self.target = None
        self.opening_until = 0
        self.probe_timeout = probe_timeout
        self.last_disconnect = 0
        self.requested = False
        self.reopen_lock = threading.Lock()

    def connect(self, page):
        with self.condition:
            self.connected.add(page)
            self.selected = page
            self.opening_until = 0
            self.condition.notify_all()

    def disconnect(self, page):
        with self.condition:
            self.connected.discard(page)
            self.last_disconnect = time.monotonic()
            self.condition.notify_all()

    def acknowledge(self, page, generation, *, visible=False):
        with self.condition:
            if page in self.connected:
                if visible:
                    self.selected = page
                if generation == self.generation and page == self.target:
                    self.ack = generation
                    self.condition.notify_all()

    def reopen(self, open_page, activate=lambda: None):
        # Separate lock serializes rapid Dock clicks and concurrent second launches.
        with self.reopen_lock:
            with self.condition:
                if self.opening_until > time.monotonic():
                    return 'opening'
                if not self.connected and time.monotonic()-self.last_disconnect < 0.4:
                    self.condition.wait_for(lambda: bool(self.connected), timeout=0.4)
                if self.connected:
                    activate()
                    self.generation += 1
                    generation = self.generation
                    self.target = self.selected if self.selected in self.connected else sorted(self.connected)[0]
                    self.condition.notify_all()
                    self.condition.wait_for(lambda: self.ack == generation or not self.connected,
                                            timeout=self.probe_timeout)
                    if self.ack == generation:
                        return 'reused'
                    if self.connected:
                        # A suspended tab is still a page; never discard user context
                        # or create a duplicate merely because its JS is throttled.
                        return 'unresponsive'
                self.opening_until = time.monotonic()+10
            try:
                open_page()
            except Exception:
                with self.condition:
                    self.opening_until = 0
                raise
            return 'opened'

    def queue_reopen(self):
        with self.condition:
            self.requested = True

    def take_request(self):
        with self.condition:
            pending, self.requested = self.requested, False
            return pending


def install(app):
    pages = DesktopPages()
    app.extensions['desktop_pages'] = pages
    app.context_processor(lambda: {'desktop_token': pages.token})

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        if data.get('token') != pages.token:
            abort(403)
        return data

    @app.post('/desktop/reopen', endpoint='desktop_reopen')
    def reopen():
        payload()
        pages.queue_reopen()
        return '', 202

    @app.post('/desktop/ack', endpoint='desktop_ack')
    def acknowledge():
        data = payload()
        if not isinstance(data.get('page'), str) or type(data.get('generation')) is not int:
            abort(400)
        pages.acknowledge(data['page'], data['generation'], visible=data.get('visible') is True)
        return '', 204

    @app.post('/desktop/close', endpoint='desktop_close')
    def close():
        data = payload()
        if not isinstance(data.get('page'), str):
            abort(400)
        pages.disconnect(data['page'])
        return '', 204

    @app.get('/desktop/events', endpoint='desktop_events')
    def events():
        if request.headers.get('X-Desktop-Token') != pages.token:
            abort(403)
        page = request.args.get('page', '')
        if not page or len(page)>100:
            abort(400)
        def stream():
            pages.connect(page)
            sent = 0
            try:
                while True:
                    with pages.condition:
                        generation = pages.generation
                        target = pages.target
                    if generation != sent and target == page:
                        sent = generation
                        yield 'data: '+json.dumps({'generation':generation})+'\n\n'
                    else:
                        # Flush detects closed/terminated browsers without waiting
                        # for background-tab JS timers; no durable fact is stored.
                        yield ': alive\n\n'
                    with pages.condition:
                        pages.condition.wait(timeout=0.25)
            finally:
                pages.disconnect(page)
        return Response(stream(), mimetype='text/event-stream', headers={'Cache-Control':'no-store'})
    return pages
