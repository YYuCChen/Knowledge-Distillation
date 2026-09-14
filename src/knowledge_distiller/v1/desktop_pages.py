"""Presence of app-owned documents; never inspect the user's browser tabs.

Transport liveness, an ACK, and a visible document are separate observations.
No timeout turns an unknown document into proof that the browser closed it.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import Response, abort, request


@dataclass
class Page:
    page_id: str
    document_id: str
    connection_epoch: str
    registration_seq: int
    route: str = '/'
    browser_hint: str = 'unknown'
    last_visible_seq: int = 0
    last_interaction_seq: int = 0
    transport_state: str = 'connected'
    visibility: bool = False
    focused: bool = False
    last_seen: float = 0
    leaving_at: float | None = None
    navigating: bool = False
    browser_instance: str | None = None
    launch_id: str | None = None


@dataclass(frozen=True)
class ReopenOutcome:
    status: str
    request_id: str
    target: str | None = None
    connection_epoch: str | None = None
    generation: int = 0
    received: bool = False
    visible: bool = False
    focused: bool = False
    # Native activation and visible_reported do NOT establish actual foreground.
    foreground_verified: bool = False
    reason: str = ''


class DesktopPages:
    def __init__(self, *, probe_timeout=1.0, navigation_grace=2.0,
                 recovery_timeout=3.0, opening_timeout=10.0):
        self.token = secrets.token_urlsafe(32)
        self.condition = threading.Condition(threading.RLock())
        self.pages: dict[str, Page] = {}
        self.sequence = 0
        self.generation = 0
        self.target = None
        self.request_id = None
        self.probe_until = 0
        self.ack = None
        self.opening_id = None
        self.opening_page = None
        self.opening_until = 0
        self.opening_failed = False
        self.probe_timeout = probe_timeout
        self.navigation_grace = navigation_grace
        self.recovery_timeout = recovery_timeout
        self.opening_timeout = opening_timeout
        self.requested = False
        self.reopen_lock = threading.Lock()
        self.explicit_requests = set()
        self.launch_browsers = {}

    @property
    def connected(self):
        with self.condition:
            return {key for key, page in self.pages.items() if page.transport_state == 'connected'}

    @property
    def selected(self):
        with self.condition:
            candidates = list(self.pages.values())
            return max(candidates, key=lambda p: (max(p.last_visible_seq, p.last_interaction_seq),
                                                   -p.registration_seq)).page_id if candidates else None

    def _next(self):
        self.sequence += 1
        return self.sequence

    def connect(self, page, document, *, route='/', browser_hint='unknown', launch=None):
        with self.condition:
            previous = self.pages.get(page)
            if previous and previous.document_id != document and previous.leaving_at is None:
                # A navigation's new fetch can overtake its pagehide beacon.
                # Wait briefly for that exact predecessor; a copied tab still
                # receives a distinct ID when its source document remains live.
                self.condition.wait_for(lambda: previous.leaving_at is not None or
                    self.pages.get(page) is not previous, timeout=min(self.probe_timeout, .25))
                previous = self.pages.get(page)
            browser_instance = previous.browser_instance if previous else self.launch_browsers.get(launch)
            launch_id = previous.launch_id if previous else launch
            # sessionStorage is copied when a tab is duplicated. Only the same
            # document, or an explicitly departing document, can replace an epoch.
            if previous and previous.document_id != document and previous.leaving_at is None:
                page = secrets.token_urlsafe(18)
                previous = None
            current = Page(page, document, secrets.token_urlsafe(18),
                           previous.registration_seq if previous else self._next(),
                           route, browser_hint, last_seen=time.monotonic(),
                           browser_instance=browser_instance, launch_id=launch_id)
            if previous:
                current.last_visible_seq = previous.last_visible_seq
                current.last_interaction_seq = previous.last_interaction_seq
            self.pages[page] = current
            if self.target == page:
                self.ack = None
            if launch and launch == self.opening_id:
                self.opening_page = page
                self.opening_until = 0
            logging.info('desktop handshake time=%s page=%s epoch=%s launch=%s',
                         time.monotonic(), page, current.connection_epoch, launch or '')
            self.condition.notify_all()
            return current

    def associate_launch(self, launch, browser_instance):
        """Only the native openURL path calls this, never an HTTP/UA hint."""
        with self.condition:
            self.launch_browsers[launch] = browser_instance
            for page in self.pages.values():
                if page.launch_id == launch:
                    page.browser_instance = browser_instance

    def browser_exited(self, browser_instance):
        """Native NSRunningApplication has proved this exact instance exited."""
        with self.condition:
            for page_id, page in list(self.pages.items()):
                if page.browser_instance == browser_instance:
                    # Native process termination is definitive; navigation grace
                    # applies only to ambiguous page/transport departure signals.
                    del self.pages[page_id]
                    if self.target == page_id:
                        self.ack = None
            self.condition.notify_all()

    def _matching(self, page, epoch):
        record = self.pages.get(page)
        return record if record and record.connection_epoch == epoch else None

    def disconnect(self, page, epoch):
        with self.condition:
            record = self._matching(page, epoch)
            if record:
                record.transport_state = 'disconnected'
                if self.target == page:
                    self.ack = None
                record.last_seen = time.monotonic()
                self.condition.notify_all()

    def leave(self, page, epoch, *, navigating=False):
        with self.condition:
            record = self._matching(page, epoch)
            if record:
                record.leaving_at = time.monotonic()
                record.navigating = navigating
                self.condition.notify_all()

    def acknowledge(self, page, epoch, generation=0, *, request_id=None,
                    visible=False, focused=False, interaction=False):
        with self.condition:
            record = self._matching(page, epoch)
            if not record or record.transport_state != 'connected':
                return False
            record.last_seen = time.monotonic()
            record.visibility, record.focused = visible, focused
            if visible and focused:
                record.last_visible_seq = self._next()
            if visible and focused and interaction:
                record.last_interaction_seq = self._next()
            if (generation and generation == self.generation and page == self.target
                    and request_id == self.request_id and time.monotonic() < self.probe_until):
                self.ack = (page, epoch, generation, visible, focused)
            self.condition.notify_all()
            return True

    def _retire_departed(self):
        now = time.monotonic()
        for key, record in list(self.pages.items()):
            # Both an explicit pagehide and termination of that stream are
            # required. A known navigation holds its reservation until claimed.
            if (record.leaving_at is not None and not record.navigating
                    and record.transport_state == 'disconnected'
                    and now - record.leaving_at >= self.navigation_grace):
                del self.pages[key]

    def _outcome(self, status, request_id, *, reason=''):
        record = self.pages.get(self.target)
        ack = self.ack
        self.probe_until = 0
        return ReopenOutcome(status, request_id, self.target,
            record.connection_epoch if record else None, self.generation,
            bool(ack), bool(ack and ack[3]), bool(ack and ack[4]), reason=reason)

    def reopen(self, open_page, activate=lambda page: None, *, explicit_request=None):
        """Run on a worker. Callbacks marshal native operations to the main loop.

        open_page receives a launch nonce; the new document must echo it in its
        handshake. explicit_request is a recovery panel action, not a Dock click.
        """
        with self.reopen_lock:
            request_id = secrets.token_urlsafe(18)
            with self.condition:
                if explicit_request:
                    if explicit_request in self.explicit_requests:
                        return ReopenOutcome('opening', explicit_request, reason='duplicate_action')
                    self.explicit_requests.add(explicit_request)
                elif self.opening_id and not self.opening_page:
                    status = 'unknown' if self.opening_failed else 'opening'
                    return ReopenOutcome(status, self.opening_id, reason='awaiting_open_handshake')
                deadline = time.monotonic() + self.recovery_timeout
                while True:
                    self._retire_departed()
                    departing = [p for p in self.pages.values() if p.leaving_at is not None and not p.navigating]
                    if not departing or time.monotonic() >= deadline or explicit_request:
                        break
                    self.condition.wait(timeout=min(0.05, max(0, deadline-time.monotonic())))
                if self.pages and not explicit_request:
                    self.target = self.selected
                    self.generation += 1
                    self.request_id = request_id
                    self.probe_until = deadline
                    self.ack = None
                    selected = self.pages[self.target]
                    self.condition.notify_all()
                else:
                    selected = None
            if selected:
                activate(selected)
                with self.condition:
                    # A hidden ACK may later become visible during the same probe.
                    remaining = max(0, deadline-time.monotonic())
                    self.condition.wait_for(lambda: bool(self.ack and self.ack[3] and self.ack[4]),
                                            timeout=min(self.probe_timeout, remaining))
                    self._retire_departed()
                    if self.ack:
                        return self._outcome('visible_reported' if self.ack[3] and self.ack[4] else 'online', request_id)
                    if self.pages:
                        return self._outcome('unknown', request_id, reason='page_not_responding')
            with self.condition:
                self.probe_until = 0
                self.opening_id, self.opening_page = request_id, None
                self.opening_failed = False
                self.opening_until = time.monotonic() + self.opening_timeout
            try:
                open_page(request_id)
            except Exception:
                with self.condition:
                    self.opening_id = None
                    self.opening_until = 0
                return ReopenOutcome('failed', request_id, reason='browser_open_failed')
            with self.condition:
                self.condition.wait_for(lambda: self.opening_page is not None,
                    timeout=max(0, self.opening_until-time.monotonic()))
                if self.opening_page:
                    record = self.pages.get(self.opening_page)
                    return ReopenOutcome('online', request_id, self.opening_page,
                        record.connection_epoch if record else None, reason='opened_handshake')
                self.opening_failed = True
                return ReopenOutcome('unknown', request_id, reason='open_handshake_timeout')

    def queue_reopen(self):
        with self.condition:
            self.requested = True

    def take_request(self):
        with self.condition:
            pending, self.requested = self.requested, False
            return pending


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 100


def _route(value):
    if not isinstance(value, str) or len(value) > 2000 or '\\' in value:
        return '/'
    try:
        parsed = urlsplit(value)
    except ValueError:
        return '/'
    return parsed.path if value.startswith('/') and not value.startswith('//') and not parsed.netloc and not parsed.scheme else '/'


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

    def identity(data):
        if not _identifier(data.get('page')) or not _identifier(data.get('epoch')):
            abort(400)

    @app.post('/desktop/reopen', endpoint='desktop_reopen')
    def reopen():
        payload()
        pages.queue_reopen()
        return '', 202

    @app.post('/desktop/ack', endpoint='desktop_ack')
    def acknowledge():
        data = payload()
        identity(data)
        if type(data.get('generation', 0)) is not int:
            abort(400)
        pages.acknowledge(data['page'], data['epoch'], data.get('generation', 0),
            request_id=data.get('request_id'), visible=data.get('visible') is True,
            focused=data.get('focused') is True, interaction=data.get('interaction') is True)
        return '', 204

    @app.post('/desktop/close', endpoint='desktop_close')
    def close():
        data = payload()
        identity(data)
        pages.leave(data['page'], data['epoch'], navigating=data.get('navigating') is True)
        return '', 204

    @app.get('/desktop/events', endpoint='desktop_events')
    def events():
        if request.headers.get('X-Desktop-Token') != pages.token:
            abort(403)
        page, document = request.args.get('page'), request.args.get('document')
        if not _identifier(page) or not _identifier(document):
            abort(400)
        route = _route(request.args.get('route', '/'))
        hint = request.args.get('browser')
        hint = hint if hint in {'chrome', 'safari', 'edge', 'firefox'} else 'unknown'
        launch = request.args.get('launch')

        def stream():
            record = pages.connect(page, document, route=route, browser_hint=hint, launch=launch)
            sent = 0
            try:
                yield 'data: '+json.dumps({'type': 'hello', 'page': record.page_id,
                    'epoch': record.connection_epoch})+'\n\n'
                while True:
                    with pages.condition:
                        if pages._matching(record.page_id, record.connection_epoch) is not record:
                            return
                        generation, target, request_id = pages.generation, pages.target, pages.request_id
                        probing = time.monotonic() < pages.probe_until
                    if probing and generation != sent and target == record.page_id:
                        sent = generation
                        yield 'data: '+json.dumps({'type': 'show', 'generation': generation,
                            'request_id': request_id})+'\n\n'
                    else:
                        yield ': alive\n\n'
                    with pages.condition:
                        pages.condition.wait(timeout=0.25)
            finally:
                pages.disconnect(record.page_id, record.connection_epoch)
        return Response(stream(), mimetype='text/event-stream', headers={'Cache-Control': 'no-store'})
    return pages
