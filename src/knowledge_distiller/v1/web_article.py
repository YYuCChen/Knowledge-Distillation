"""Internal R05 static reader. No routing, persistence, identity allocation or browser.

HTML, metadata and extracted Markdown are untrusted source material. Nothing in
them is executed or dereferenced. See docs/engineering/web-article.md.
"""
from __future__ import annotations

import codecs
from collections import Counter
from dataclasses import dataclass, replace
import hashlib
from html.parser import HTMLParser
import importlib
import ipaddress
import json
import math
from queue import Empty, Queue
import re
import socket
import ssl
from threading import Thread
import time
from typing import Callable
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit
import zlib

from .source_parsing import ParsedSource, SourceReadError


@dataclass(frozen=True)
class WebLimits:
    redirects: int = 5
    total_seconds: float = 30.0
    wire_bytes: int = 4 * 1024 * 1024
    html_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        if (not isinstance(self.redirects, int) or isinstance(self.redirects, bool)
                or not 0 <= self.redirects <= 10
                or not isinstance(self.total_seconds, (int, float))
                or isinstance(self.total_seconds, bool)
                or not math.isfinite(self.total_seconds) or self.total_seconds <= 0
                or any(not isinstance(v, int) or isinstance(v, bool) or v <= 0
                       for v in (self.wire_bytes, self.html_bytes))):
            raise ValueError("invalid web reader limits")


@dataclass(frozen=True)
class WebHop:
    url: str
    status: int
    location: str | None
    address: str


@dataclass(frozen=True)
class WebCapture:
    submitted_url: str
    final_url: str
    hops: tuple[WebHop, ...]
    raw_html: bytes
    wire_body: bytes
    charset: str | None = None
    charset_provenance: str | None = None


@dataclass(frozen=True)
class ExtractedArticle:
    """Extractor seam, not a stable source identity or persistence schema."""
    body: str
    title: str | None = None
    author: str | None = None
    date: str | None = None
    extracted_url: str | None = None
    tables: int = 0
    images: tuple[str, ...] = ()
    body_xml: bytes = b""
    extractor: str = "injected-test-extractor"
    repetition_audit: dict | None = None


@dataclass(frozen=True)
class WebArticle:
    capture: WebCapture
    parsed: ParsedSource


class WebReadError(SourceReadError):
    def __init__(self, code: str, *, capture: WebCapture | None = None, retryable=False,
                 coverage: dict | None = None):
        super().__init__(code, retryable=retryable)
        self.capture = capture
        self.coverage = coverage


class _Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self, timeout=None):
        left = self.end - time.monotonic()
        if left <= 0:
            raise WebReadError("web_timeout", retryable=True)
        return left if timeout is None else min(left, timeout)


def _bounded_call(call, deadline):
    # libc DNS has no per-call timeout. A daemon prevents waiting past the reader
    # deadline; at most one call is outstanding in an invocation, with no retry.
    queue = Queue(maxsize=1)

    def run():
        try:
            queue.put((True, call()))
        except Exception as error:
            queue.put((False, error))

    deadline.remaining()
    Thread(target=run, daemon=True, name="kd-web-read").start()
    try:
        ok, value = queue.get(timeout=deadline.remaining())
    except Empty as error:
        raise WebReadError("web_timeout", retryable=True) from error
    deadline.remaining()
    if not ok:
        raise value
    return value


def _target(value: str) -> tuple[str, str, int]:
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value):
        raise WebReadError("web_invalid_url")
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        host = parts.hostname
        if scheme not in {"http", "https"} or not host:
            raise ValueError()
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise WebReadError("web_credentials_forbidden")
        port = parts.port if parts.port is not None else (443 if scheme == "https" else 80)
        if port != (443 if scheme == "https" else 80):
            raise WebReadError("web_port_forbidden")
        if "%" in host or host.endswith("."):
            raise ValueError()
        host = host.encode("idna").decode("ascii").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise WebReadError("web_address_forbidden")
        netloc = f"[{host}]" if ":" in host else host
        return urlunsplit((scheme, netloc, parts.path or "/", parts.query, "")), host, port
    except (ValueError, UnicodeError) as error:
        raise WebReadError("web_invalid_url") from error


def _public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise WebReadError("web_dns_failed", retryable=True) from error
    # IPv6 translation/tunnel prefixes can route to IPv4 destinations; refuse
    # them instead of treating their externally global prefix as sufficient.
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified or address.is_loopback or address.is_link_local
            or (isinstance(address, ipaddress.IPv6Address)
                and (address.ipv4_mapped is not None or address.sixtofour is not None
                     or address.teredo is not None
                     or address in ipaddress.ip_network("64:ff9b::/96")
                     or address in ipaddress.ip_network("64:ff9b:1::/48")))):
        raise WebReadError("web_address_forbidden")
    # Conservative exclusions cover special-use ranges misclassified as global
    # by older CPython 3.11 ipaddress databases (do not rely on a newer runtime).
    special = ("192.0.0.0/24", "192.88.99.0/24") if address.version == 4 else (
        "2001::/23", "3fff::/20")
    if any(address in ipaddress.ip_network(prefix) for prefix in special):
        raise WebReadError("web_address_forbidden")
    return str(address)


def _resolve(host: str, port: int, resolver, deadline) -> str:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            values = _bounded_call(lambda: resolver(host, port), deadline)
        except (OSError, ValueError) as error:
            raise WebReadError("web_dns_failed", retryable=True) from error
    else:
        values = [host]
    if not values:
        raise WebReadError("web_dns_failed", retryable=True)
    # Validate ALL results before choosing one. Mixed public/private is rejected.
    addresses = tuple(_public_address(v) for v in values)
    return addresses[0]


def _system_resolver(host, port):
    return [row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


class _PinnedStream:
    def __init__(self, sock, core, deadline, hostname):
        self.sock, self.core, self.deadline, self.hostname = sock, core, deadline, hostname

    def read(self, max_bytes, timeout=None):
        self.sock.settimeout(self.deadline.remaining(timeout))
        try:
            return self.sock.recv(max_bytes)
        except socket.timeout as error:
            raise self.core.ReadTimeout() from error
        except OSError as error:
            raise self.core.ReadError() from error

    def write(self, buffer, timeout=None):
        self.sock.settimeout(self.deadline.remaining(timeout))
        try:
            self.sock.sendall(buffer)
        except socket.timeout as error:
            raise self.core.WriteTimeout() from error
        except OSError as error:
            raise self.core.WriteError() from error

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        if server_hostname != self.hostname:
            raise WebReadError("web_transport_mismatch")
        self.sock.settimeout(self.deadline.remaining(timeout))
        try:
            self.sock = ssl_context.wrap_socket(self.sock, server_hostname=server_hostname)
        except socket.timeout as error:
            self.close()
            raise self.core.ConnectTimeout() from error
        except OSError as error:
            self.close()
            raise self.core.ConnectError() from error
        return self

    def close(self):
        self.sock.close()

    def get_extra_info(self, info):
        if info == "ssl_object":
            return getattr(self.sock, "_sslobj", None)
        if info == "server_addr":
            return self.sock.getpeername()
        if info == "socket":
            return self.sock
        return None


class _PinnedBackend:
    def __init__(self, hostname, address, port, deadline, core):
        self.hostname, self.address, self.port = hostname, address, port
        self.deadline, self.core = deadline, core

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.hostname or port != self.port or local_address is not None:
            raise WebReadError("web_transport_mismatch")
        _public_address(self.address)
        family = socket.AF_INET6 if ":" in self.address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.deadline.remaining(timeout))
            for option in socket_options or ():
                sock.setsockopt(*option)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Numeric sockaddr goes straight to connect(2): no getaddrinfo,
            # create_connection or second lookup of the original hostname.
            sockaddr = (self.address, port, 0, 0) if family == socket.AF_INET6 else (self.address, port)
            sock.connect(sockaddr)
            if ipaddress.ip_address(sock.getpeername()[0]) != ipaddress.ip_address(self.address):
                raise WebReadError("web_transport_mismatch")
        except socket.timeout as error:
            sock.close()
            raise self.core.ConnectTimeout() from error
        except OSError as error:
            sock.close()
            raise self.core.ConnectError() from error
        except Exception:
            sock.close()
            raise
        return _PinnedStream(sock, self.core, self.deadline, self.hostname)

    def connect_unix_socket(self, *args, **kwargs):
        raise WebReadError("web_transport_mismatch")


def _dependencies():
    try:
        httpx, core = importlib.import_module("httpx"), importlib.import_module("httpcore")
    except ImportError as error:
        raise WebReadError("web_dependency_missing") from error
    if httpx.__version__ != "0.28.1" or core.__version__ != "1.0.9":
        raise WebReadError("web_dependency_version")
    return httpx, core


def _transport(httpx, core, host, address, port, deadline):
    class BodyStream(httpx.SyncByteStream):
        def __init__(self, response):
            self.response = response

        def __iter__(self):
            yield from self.response.iter_stream()

        def close(self):
            self.response.close()

    class Transport(httpx.BaseTransport):
        def __init__(self):
            self.pool = core.ConnectionPool(
                ssl_context=ssl.create_default_context(), max_connections=1,
                max_keepalive_connections=0, retries=0, http2=False,
                network_backend=_PinnedBackend(host, address, port, deadline, core),
            )

        def handle_request(self, request):
            response = self.pool.handle_request(core.Request(
                method=request.method,
                url=core.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
                headers=request.headers.raw, content=request.stream, extensions=request.extensions,
            ))
            return httpx.Response(response.status, headers=response.headers,
                                  stream=BodyStream(response), extensions=response.extensions)

        def close(self):
            self.pool.close()

    return Transport()


def _entity(response, limits, deadline, used, decoded_used):
    length = response.headers.get("content-length")
    if length is not None:
        if not length.isdecimal():
            raise WebReadError("web_response_invalid")
        if int(length) + used > limits.wire_bytes:
            raise WebReadError("web_response_too_large")
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise WebReadError("web_encoding_unsupported")
    decoder = None if encoding == "identity" else zlib.decompressobj(31 if encoding == "gzip" else 15)
    wire, html = bytearray(), bytearray()
    try:
        for chunk in response.iter_raw():
            deadline.remaining()
            if len(wire) + len(chunk) + used > limits.wire_bytes:
                raise WebReadError("web_response_too_large")
            wire.extend(chunk)
            decoded = chunk if decoder is None else decoder.decompress(chunk, limits.html_bytes - decoded_used - len(html) + 1)
            html.extend(decoded)
            if len(html) + decoded_used > limits.html_bytes or (decoder is not None and decoder.unconsumed_tail):
                raise WebReadError("web_response_too_large")
        if decoder is not None and (not decoder.eof or decoder.unused_data):
            # No concatenated members or unchecked trailing bytes.
            raise WebReadError("web_compression_invalid")
    except zlib.error as error:
        raise WebReadError("web_compression_invalid") from error
    deadline.remaining()
    return bytes(wire), bytes(html)


class _Declarations(HTMLParser):
    """Read declarations/access signals, never substitute for body extraction."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.charsets = []
        self.canonical = []
        self.login = False
        self.render = False
        self.paywall = False
        self.challenge = False
        self.table_count = 0
        self.image_count = 0
        self.json_ld = []
        self._json = None
        self._noscript = False
        self._inert_text = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self._inert_text = True
        if tag == "meta":
            if attrs.get("charset"):
                self.charsets.append(attrs["charset"])
            if attrs.get("http-equiv", "").lower() == "content-type":
                self.charsets.extend(_charset_values(attrs.get("content", "")))
            if attrs.get("http-equiv", "").lower() == "refresh":
                self.render = True
        if tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
            if attrs.get("href"):
                self.canonical.append(attrs["href"])
        if tag == "input" and attrs.get("type", "").lower() == "password":
            self.login = True
        if tag == "table":
            self.table_count += 1
        if tag == "img":
            self.image_count += 1
        if tag == "noscript":
            self._noscript = True
        marker = " ".join(attrs.get(k, "") for k in ("id", "class")).lower()
        if re.search(r"(?:^|[\s_-])(?:paywall|subscription-wall|metered-wall)(?:$|[\s_-])", marker):
            self.paywall = True
        if any(v in marker for v in ("cf-chl", "captcha", "challenge-platform")):
            self.challenge = True
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self._json = []

    def handle_endtag(self, tag):
        if tag == "script" and self._json is not None:
            self.json_ld.append("".join(self._json))
            self._json = None
        if tag == "noscript":
            self._noscript = False
        if tag in {"script", "style"}:
            self._inert_text = False

    def handle_data(self, data):
        if self._json is not None:
            self._json.append(data)
        if self._noscript and re.search(r"enable javascript|javascript.*required|请.*启用.*javascript", data, re.I):
            self.render = True
        if self._json is None and not self._inert_text:
            if re.search(r"(?:sign|log) in to (?:read|continue)|请登录后阅读", data, re.I):
                self.login = True
            if re.search(r"subscribe to (?:read|continue)|unlock this article|订阅后阅读", data, re.I):
                self.paywall = True
            if re.search(r"verify (?:that )?you are human", data, re.I):
                self.challenge = True


def _charset_values(value):
    return re.findall(r"charset\s*=\s*[\"']?([^\s;\"']+)", value, re.I)


def _scan(html):
    declarations = _Declarations()
    try:
        declarations.feed(html)
        declarations.close()
    except (AssertionError, ValueError) as error:
        raise WebReadError("web_response_invalid") from error
    return declarations


def _decode(raw, content_type):
    # HTML encoding declarations are ASCII-compatible and limited to the head
    # prefix. Never let a detector silently choose an uncertain legacy encoding.
    prefix = raw[:1024].decode("ascii", errors="ignore")
    declarations = _scan(prefix)
    declared = _charset_values(content_type) + declarations.charsets
    if "charset" in content_type.lower() and not _charset_values(content_type):
        raise WebReadError("web_charset_invalid")
    if raw.startswith(codecs.BOM_UTF8):
        declared.append("utf-8-sig")
    try:
        names = [codecs.lookup(v).name for v in declared]
        comparable = {"utf-8" if v == "utf-8-sig" else v for v in names}
        if len(comparable) > 1:
            raise WebReadError("web_charset_conflict")
        charset = names[0] if names else "utf-8"
        if raw.startswith(codecs.BOM_UTF8) and charset == "utf-8":
            charset = "utf-8-sig"
        # These are the deliberately narrow, deterministic supported encodings.
        if charset not in {"utf-8", "utf-8-sig", "ascii", "iso8859-1", "cp1252", "gb18030", "gbk", "big5", "shift_jis"}:
            raise WebReadError("web_charset_unsupported")
        text = raw.decode(charset, errors="strict")
    except (LookupError, UnicodeError) as error:
        raise WebReadError("web_charset_invalid" if declared else "web_charset_missing") from error
    if "\ufffd" in text or "\x00" in text:
        raise WebReadError("web_charset_invalid")
    return text, charset, "http-or-html-declared" if declared else "strict-utf8-fallback"


def _has_paywall_json(value):
    if isinstance(value, dict):
        free = value.get("isAccessibleForFree")
        if free is False or (isinstance(free, str) and free.lower() == "false"):
            return True
        return any(_has_paywall_json(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_paywall_json(v) for v in value)
    return False


def _paragraph_text(element):
    # Comparison only: never rewrite the source or the output. Joining without
    # invented inline spaces matches Trafilatura's _elem_text comparison.
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", "".join(element.itertext()))).strip()


def _audit_repetition(html, body, module):
    """Reject proven duplicate-paragraph loss; never reconstruct an extractor.

    Reuse the pinned library's pre-extraction comment/follow-up exclusions.
    A unique article/articleBody supplies an explicit bounded source scope.
    Ambiguous scopes can only establish a risk, not a completeness guarantee.
    """
    from trafilatura.htmlprocessing import prune_unwanted_nodes
    from trafilatura.xpaths import RAW_TREE_PRUNE_XPATH, REMOVE_COMMENTS_AND_LISTS_XPATH

    source = module.load_html(html)
    if source is None:
        raise WebReadError("web_extraction_failed")
    original_paths = {node: source.getroottree().getpath(node)
                      for node in source.xpath(".//p|.//article|.//*[@itemprop='articleBody']")}
    source = prune_unwanted_nodes(source, RAW_TREE_PRUNE_XPATH + REMOVE_COMMENTS_AND_LISTS_XPATH)
    for node in source.xpath(".//nav|.//aside|.//footer|.//script|.//style|.//template"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    scopes = source.xpath(".//article[not(ancestor::article)]")
    if len(scopes) != 1:
        scopes = source.xpath(".//*[@itemprop='articleBody']")
    bounded = len(scopes) == 1
    scope = scopes[0] if bounded else source
    occurrences = {}
    for paragraph in scope.iter("p"):
        text = _paragraph_text(paragraph)
        if text:
            occurrences.setdefault(text, []).append(original_paths[paragraph])
    emitted = Counter(_paragraph_text(paragraph) for paragraph in body.iter("p"))
    repeated = []
    for text, paths in occurrences.items():
        if len(paths) > 1:
            repeated.append({"text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                             "source_paths": tuple(paths), "source_occurrences": len(paths),
                             "extracted_paragraph_occurrences": emitted[text]})
    audit = {"scope": original_paths[scope] if bounded else None,
             "scope_kind": scope.tag if bounded else "ambiguous",
             "status": "checked-repeated-paragraphs" if bounded else "unverified-source-scope",
             "repeated_groups": tuple(repeated),
             "normalization": "comparison-only-nfc-whitespace-inline-concatenation"}
    # A surviving same-text paragraph proves this text was selected as content.
    # Missing repetitions in that group must not be silently qualified. If the
    # whole group disappeared or was structurally merged, refuse as unverified
    # rather than inventing counts from a substring in another source region.
    gaps = [group for group in repeated
            if group["extracted_paragraph_occurrences"] < group["source_occurrences"]]
    if gaps:
        proven = bounded and any(group["extracted_paragraph_occurrences"] > 0 for group in gaps)
        code = "web_repetition_loss" if proven else "web_repetition_unverified"
        audit["status"] = "rejected-loss" if proven else "rejected-unverified"
        raise WebReadError(code, coverage={"article_completeness": "rejected",
                           "source_qualified": False, "next_step": "review-original-html",
                           "repetition_audit": audit})
    return audit


def _trafilatura(html, url):
    try:
        module = importlib.import_module("trafilatura")
        from importlib.metadata import version
        if version("trafilatura") != "2.3.1":
            raise WebReadError("web_dependency_version")
        from trafilatura.xml import xmltotxt
        from lxml.etree import tostring
    except ImportError as error:
        raise WebReadError("web_dependency_missing") from error
    document = module.extract_with_metadata(
        html, url=url, output_format="xml", include_comments=False,
        include_tables=True, include_images=True, include_links=True,
        include_formatting=True, deduplicate=False,
        date_extraction_params={"extensive_search": False, "original_date": True},
    )
    if document is None or document.body is None:
        return None
    audit = _audit_repetition(html, document.body, module)
    body = xmltotxt(document.body, include_formatting=True)
    return ExtractedArticle(
        body, document.title, document.author, document.date, document.url,
        sum(1 for _ in document.body.iter("table")),
        tuple(e.get("src", "") for e in document.body.iter("graphic")),
        tostring(document.body, encoding="utf-8"), "trafilatura/2.3.1", audit,
    )


def read_web_article(submitted_url: str, *, limits: WebLimits | None = None,
                     resolver: Callable = _system_resolver, extractor: Callable | None = None,
                     transport_factory: Callable | None = None) -> WebArticle:
    """Read one public static article; fixture seams never allocate source IDs.

    transport_factory receives (httpx, httpcore, hostname, validated_ip, port,
    deadline). It is for isolated fixtures only; production uses the pinned
    backend. No cookie/credential/profile state is accepted by this API.
    """
    limits = limits or WebLimits()
    deadline = _Deadline(limits.total_seconds)
    url, host, port = _target(submitted_url)
    httpx, core = _dependencies()
    hops = []
    used = 0
    decoded_used = 0
    capture = None
    try:
        for index in range(limits.redirects + 1):
            deadline.remaining()
            address = _resolve(host, port, resolver, deadline)
            transport = (transport_factory or _transport)(httpx, core, host, address, port, deadline)
            # A fresh client per hop means Set-Cookie is never replayed. No auth,
            # proxies, netrc, browser or automatic redirects are available.
            with httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                              timeout=deadline.remaining()) as client:
                with client.stream("GET", url, headers={"Accept": "text/html,application/xhtml+xml",
                                   "Accept-Encoding": "identity", "User-Agent": "KnowledgeDistiller-R05/1"}) as response:
                    location = response.headers.get("location")
                    hops.append(WebHop(url, response.status_code, location, address))
                    capture = WebCapture(submitted_url, url, tuple(hops), b"", b"")
                    wire, raw = _entity(response, limits, deadline, used, decoded_used)
                    used += len(wire)
                    decoded_used += len(raw)
                    capture = WebCapture(submitted_url, url, tuple(hops), raw, wire)
                    status = response.status_code
                    content_type = response.headers.get("content-type", "")
            if status in {301, 302, 303, 307, 308}:
                if index >= limits.redirects:
                    raise WebReadError("web_redirect_limit")
                if not location:
                    raise WebReadError("web_redirect_invalid")
                url, host, port = _target(urljoin(url, location))
                continue
            if status == 401:
                raise WebReadError("web_login_required")
            if status == 402:
                raise WebReadError("web_paywall")
            if status in {403, 429}:
                raise WebReadError("web_access_blocked", retryable=status == 429)
            if status < 200 or status >= 300:
                raise WebReadError("web_http_error", retryable=status >= 500)
            if content_type.split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
                raise WebReadError("web_content_type_unsupported")
            break
        html, charset, provenance = _bounded_call(lambda: _decode(capture.raw_html, content_type), deadline)
        capture = replace(capture, charset=charset, charset_provenance=provenance)
        declarations = _bounded_call(lambda: _scan(html), deadline)
        for value in declarations.json_ld:
            try:
                if _has_paywall_json(json.loads(value)):
                    declarations.paywall = True
            except (ValueError, RecursionError):
                pass
        if declarations.challenge:
            raise WebReadError("web_access_blocked")
        if declarations.paywall:
            raise WebReadError("web_paywall")
        if declarations.login:
            raise WebReadError("web_login_required")
        if declarations.render:
            raise WebReadError("web_render_required")
        try:
            article = _bounded_call(lambda: (extractor or _trafilatura)(html, url), deadline)
        except WebReadError:
            raise
        except Exception as error:
            raise WebReadError("web_extraction_failed") from error
        if article is None or not article.body.strip():
            raise WebReadError("web_body_missing")
        # Canonical is an untrusted declaration, never a request destination.
        canonical = []
        for value in declarations.canonical:
            try:
                canonical.append(urljoin(url, value))
            except ValueError:
                canonical.append(value)
        canonical = tuple(canonical)
        coverage = {
            "mode": "static-http", "http_body_complete": True,
            "article_completeness": "unverified", "comments": "excluded",
            "repetition_audit": article.repetition_audit or {"status": "not-run-injected-extractor"},
            "tables_in_html": declarations.table_count, "tables_extracted": article.tables,
            "images_in_html": declarations.image_count, "images_extracted": len(article.images),
            "linked_resources_fetched": False, "rendered": False,
            "warnings": ["table-retention-needs-review"] if declarations.table_count > article.tables else [],
        }
        metadata = {"submitted_url": submitted_url, "final_url": url,
                    "canonical_url": canonical[0] if len(canonical) == 1 else None,
                    "canonical_declarations": canonical, "source_title": article.title,
                    "canonical_provenance": "html-link-declared-unverified",
                    "author": {"display_name": article.author, "provenance": "page-extracted"},
                    "date": {"value": article.date, "provenance": "page-extracted-unverified"},
                    "extracted_url": article.extracted_url,
                    "extractor": article.extractor, "coverage": coverage,
                    "charset": charset, "charset_provenance": provenance,
                    "image_references": article.images,
                    "redirect_chain": tuple(vars(hop) for hop in hops)}
        lineage = {"kind": "web-article-internal-candidate", "raw_sha256": hashlib.sha256(capture.raw_html).hexdigest(),
                   "snapshot_sha256": hashlib.sha256(article.body.encode("utf-8")).hexdigest(),
                   "mapping": "extracted-document-no-raw-byte-offset-claim",
                   "extractor": article.extractor, "extracted_body_xml": article.body_xml}
        return WebArticle(capture, ParsedSource(article.body, metadata, lineage))
    except WebReadError as error:
        if error.capture is None:
            error.capture = capture
        if error.coverage is not None and capture is not None:
            error.coverage = {"mode": "static-http", "http_body_complete": True,
                              **error.coverage}
        raise
    except (core.TimeoutException, httpx.TimeoutException) as error:
        raise WebReadError("web_timeout", capture=capture, retryable=True) from error
    except (core.NetworkError, core.ProtocolError, httpx.HTTPError, OSError) as error:
        raise WebReadError("web_network_error", capture=capture, retryable=True) from error
