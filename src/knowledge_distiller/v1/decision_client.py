"""Internal text SystemOne contract; deliberately not connected to product callers.

No fallback, truncation, logging of state/credentials, or threshold migration.
Multiple Clef fields are jointly scored, not statistically independent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
from typing import Callable, Literal, TypeAlias
from urllib.parse import urlsplit

JSONValue: TypeAlias = str | int | float | bool | None | list['JSONValue'] | dict[str, 'JSONValue']
TextState: TypeAlias = str | list[JSONValue] | dict[str, JSONValue]
PROTOCOL = 'systemone-text-v1'
JEV_ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
CLEF_REVISION = '5c646a43ed30b5d79822b16e11c6b3d621800b94'


class DecisionError(RuntimeError):
    """Stable non-sensitive code; no server body or original exception is exposed."""


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 for c in value)


def _json(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json(item)
        return
    if type(value) is dict and all(isinstance(k, str) for k in value):
        for item in value.values():
            _json(item)
        return
    raise DecisionError('decision_request_invalid')


def _structured(value: object, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, (str, dict, list)) or not value:
        raise DecisionError('decision_request_invalid')
    _json(value)


def _probability(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise DecisionError('decision_response_invalid')
    return float(value)


@dataclass(frozen=True)
class DecisionProfile:
    provider: Literal['jev', 'clef']
    endpoint: str
    model: str
    auth_ref: str | None = None
    protocol: str = PROTOCOL
    timeout_seconds: float = 20.0
    token_budget: int = 16384

    def __post_init__(self) -> None:
        valid = (self.provider in ('jev', 'clef') and self.protocol == PROTOCOL
                 and _text(self.model) and len(self.model) <= 128
                 and type(self.token_budget) is int and 0 < self.token_budget <= 16384
                 and type(self.timeout_seconds) in (int, float)
                 and math.isfinite(self.timeout_seconds) and 0 < self.timeout_seconds <= 600
                 and (self.auth_ref is None or (isinstance(self.auth_ref, str)
                      and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', self.auth_ref))))
        if not valid or not isinstance(self.endpoint, str):
            raise DecisionError('decision_profile_invalid')
        try:
            url = urlsplit(self.endpoint)
            port = url.port
            common = (url.path == '/v1/systemone' and not url.query and not url.fragment
                      and not url.username and not url.password and _text(self.endpoint)
                      and (port is None or 1 <= port <= 65535))
            if self.provider == 'clef':
                endpoint_ok = (url.scheme == 'http' and url.hostname == '127.0.0.1'
                               and url.netloc == ('127.0.0.1' if port is None else f'127.0.0.1:{port}'))
            else:
                endpoint_ok = self.endpoint == JEV_ENDPOINT and self.auth_ref is not None
        except ValueError:
            raise DecisionError('decision_profile_invalid') from None
        if not common or not endpoint_ok:
            raise DecisionError('decision_profile_invalid')


@dataclass(frozen=True)
class ChoiceQuestion:
    instructions: TextState
    criteria: dict[str, TextState | None]

    def wire(self) -> dict:
        _structured(self.instructions)
        if (not isinstance(self.criteria, dict) or not 2 <= len(self.criteria) <= 255
                or not all(_text(key) for key in self.criteria)):
            raise DecisionError('decision_request_invalid')
        for value in self.criteria.values():
            _structured(value, nullable=True)
        return {'type': 'choice', 'instructions': self.instructions, 'criteria': self.criteria}


@dataclass(frozen=True)
class NoulQuestion:
    instructions: TextState
    criteria: dict[str, TextState] | None = None

    def wire(self) -> dict:
        _structured(self.instructions)
        body = {'type': 'noul', 'instructions': self.instructions}
        if self.criteria is not None:
            if not isinstance(self.criteria, dict) or not set(self.criteria) <= {'true', 'false'}:
                raise DecisionError('decision_request_invalid')
            for value in self.criteria.values():
                _structured(value)
            body['criteria'] = self.criteria
        return body


Question: TypeAlias = ChoiceQuestion | NoulQuestion


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float
    confidence_semantics: Literal['jev-normalized-concentration', 'clef-max-probability']

    @property
    def probability(self) -> float:
        return self.probabilities[self.choice]


@dataclass(frozen=True)
class NoulAnswer:
    noul: float  # P(yes); intentionally no confidence member.


@dataclass(frozen=True)
class BudgetCheck:
    units: int
    method: str
    exact: bool = False


def _render(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def clef_template_upper_bound(body: dict) -> BudgetCheck:
    """Pinned text template UTF-8 byte upper bound for the byte-BPE tokenizer.

    Includes every separately tokenized segment. This is not a token count.
    Deployment must verify the pinned tokenizer's byte-BPE/no-added-token contract;
    service --max-length/--no-truncate remains the exact-token authority.
    """
    system = ('Read the complete state and schema. Decide every field jointly. Each answer '
              "must be exactly one of that field's allowed options.")
    segments = [f'<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\nSTATE:\n',
                _render(body['state']), '\n\nSCHEMA FIELDS:\n']
    for qi, (qid, question) in enumerate(body['questions'].items(), 1):
        segments.extend([f"\nFIELD {qi}\nID: {qid}\nTYPE: {question['type']}\nINSTRUCTION: ",
                         _render(question['instructions']), '\nALLOWED OPTIONS:\n'])
        if question['type'] == 'choice':
            options = sorted(question['criteria'].items())
        else:
            criteria = {'true': 'The proposition is true or the answer is yes.',
                        'false': 'The proposition is false or the answer is no.'}
            criteria.update(question.get('criteria') or {})
            options = [(key, criteria[key]) for key in ('true', 'false')]
        for oi, (key, description) in enumerate(options, 1):
            semantics = {'option_id': key}
            if description is not None:
                semantics['description'] = description
            segments.extend([f'OPTION {oi}: ', _render(semantics), '\n'])
        segments.append('END FIELD\n')
    segments.append('\n<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\nJOINT SCHEMA DECISIONS:')
    return BudgetCheck(sum(len(part.encode('utf-8')) for part in segments),
                       'clef-pinned-template-utf8-byte-upper-bound')


@dataclass(frozen=True)
class DecisionResult:
    provider: str
    protocol: str
    requested_model: str
    model: str
    answers: dict[str, ChoiceAnswer | NoulAnswer]
    usage: dict
    budget_check: BudgetCheck


@dataclass
class DecisionClient:
    profile: DecisionProfile
    secret: Callable[[str], str] = field(default=lambda ref: '', repr=False)
    post: Callable | None = field(default=None, repr=False)
    get: Callable | None = field(default=None, repr=False)

    def verify_local_model(self) -> str:
        """Explicit served-name probe, separate from inference's model echo.

        A Clef health transport must be explicitly injected. This method never
        silently contacts an existing local model service via a default GET.
        A served name still is not cryptographic proof of a weight revision.
        """
        if self.profile.provider != 'clef':
            raise DecisionError('decision_probe_not_applicable')
        if self.get is None:
            raise DecisionError('decision_probe_transport_required')
        import httpx
        headers = {'Accept': 'application/json'}
        if self.profile.auth_ref:
            headers.update(self._auth_headers())
        try:
            response = self.get(self.profile.endpoint.removesuffix('/v1/systemone') + '/health',
                                headers=headers, timeout=self.profile.timeout_seconds,
                                follow_redirects=False, trust_env=False)
        except httpx.TimeoutException:
            raise DecisionError('decision_timeout') from None
        except (httpx.HTTPError, OSError):
            raise DecisionError('decision_request_failed') from None
        self._check_status(response.status_code)
        try:
            data = response.json()
            if not isinstance(data, dict) or data.get('status') != 'ok' or not _text(data.get('model')):
                raise DecisionError('decision_response_invalid')
            if data['model'] != self.profile.model:
                raise DecisionError('decision_model_mismatch')
            return data['model']
        except (ValueError, TypeError, KeyError, AttributeError):
            raise DecisionError('decision_response_invalid') from None

    def _auth_headers(self) -> dict:
        headers = {'Content-Type': 'application/json'}
        if self.profile.auth_ref:
            try:
                key = self.secret(self.profile.auth_ref)
            except Exception:
                raise DecisionError('decision_secret_unavailable') from None
            if (not isinstance(key, str) or not key.strip() or key != key.strip()
                    or any(ord(c) < 32 or ord(c) > 126 for c in key)):
                raise DecisionError('decision_secret_unavailable')
            headers['Authorization'] = 'Bearer ' + key
        elif self.profile.provider == 'jev':
            raise DecisionError('decision_secret_unavailable')
        return headers

    @staticmethod
    def _check_status(status: int) -> None:
        if 300 <= status < 400:
            raise DecisionError('decision_redirect_refused')
        if status == 413:
            raise DecisionError('decision_budget_exceeded')
        if status in (401, 403):
            raise DecisionError('decision_unauthorized')
        if status in (429, 529):
            raise DecisionError('decision_busy')
        if status in (400, 422):
            raise DecisionError('decision_request_invalid')
        if status != 200:
            raise DecisionError('decision_request_failed')

    def ask(self, state: TextState, questions: dict[str, Question]) -> DecisionResult:
        try:
            if not isinstance(state, (str, dict, list)):
                raise DecisionError('decision_request_invalid')
            _json(state)
        except (RecursionError, OverflowError):
            raise DecisionError('decision_request_invalid') from None
        if (not isinstance(questions, dict) or not questions or not all(_text(k) for k in questions)
                or not all(isinstance(q, (ChoiceQuestion, NoulQuestion)) for q in questions.values())):
            raise DecisionError('decision_request_invalid')
        # Freeze mutable caller values before budgeting and transport.
        try:
            body = {'model': self.profile.model, 'state': state,
                    'questions': {key: question.wire() for key, question in questions.items()}}
            body = json.loads(json.dumps(body, ensure_ascii=False, allow_nan=False))
            if self.profile.provider == 'clef':
                body['truncate'] = False
                budget = clef_template_upper_bound(body)
            else:
                budget = BudgetCheck(len(json.dumps(body, ensure_ascii=False).encode('utf-8')),
                                     'jev-request-utf8-byte-conservative-admission')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise DecisionError('decision_request_invalid') from None
        if budget.units > self.profile.token_budget:
            raise DecisionError('decision_budget_exceeded')
        headers = self._auth_headers()
        import httpx
        try:
            if self.post is None:
                with httpx.Client(follow_redirects=False, trust_env=False) as client:
                    response = client.post(self.profile.endpoint, headers=headers, json=body,
                                           timeout=self.profile.timeout_seconds)
            else:
                response = self.post(self.profile.endpoint, headers=headers, json=body,
                                     timeout=self.profile.timeout_seconds, follow_redirects=False,
                                     trust_env=False)
        except httpx.TimeoutException:
            raise DecisionError('decision_timeout') from None
        except (httpx.HTTPError, OSError):
            raise DecisionError('decision_request_failed') from None
        self._check_status(response.status_code)
        try:
            data = response.json()
            return self._parse(data, body['questions'], budget)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise DecisionError('decision_response_invalid') from None

    def _parse(self, data: dict, questions: dict, budget: BudgetCheck) -> DecisionResult:
        if not isinstance(data, dict) or not _text(data.get('model')):
            raise DecisionError('decision_response_invalid')
        if self.profile.provider == 'clef' and data['model'] != self.profile.model:
            raise DecisionError('decision_model_mismatch')
        raw, usage = data['answers'], data['usage']
        if not isinstance(raw, dict) or set(raw) != set(questions) or not isinstance(usage, dict):
            raise DecisionError('decision_response_invalid')
        for key in ('input_tokens', 'output_tokens'):
            if type(usage.get(key)) is not int or usage[key] < 0:
                raise DecisionError('decision_response_invalid')
        if usage['input_tokens'] > self.profile.token_budget:
            raise DecisionError('decision_budget_exceeded')
        try:
            _json(usage)
        except (DecisionError, RecursionError):
            raise DecisionError('decision_response_invalid') from None
        answers = {}
        for qid, question in questions.items():
            answer = raw[qid]
            if not isinstance(answer, dict) or answer.get('type') != question['type']:
                raise DecisionError('decision_response_invalid')
            if question['type'] == 'noul':
                if 'confidence' in answer:
                    raise DecisionError('decision_response_invalid')
                answers[qid] = NoulAnswer(_probability(answer['noul']))
                continue
            probabilities = answer['probabilities']
            if not isinstance(probabilities, dict) or set(probabilities) != set(question['criteria']):
                raise DecisionError('decision_response_invalid')
            probabilities = {key: _probability(value) for key, value in probabilities.items()}
            # Clef rounds each option to 4 decimals; retain raw values, do not renormalize.
            tolerance = len(probabilities) * 0.00005 + 1e-9 if self.profile.provider == 'clef' else 1e-6
            if abs(math.fsum(probabilities.values()) - 1) > tolerance:
                raise DecisionError('decision_response_invalid')
            choice, confidence = answer['choice'], _probability(answer['confidence'])
            if not isinstance(choice, str) or choice not in probabilities:
                raise DecisionError('decision_response_invalid')
            if probabilities[choice] != max(probabilities.values()):
                raise DecisionError('decision_response_invalid')
            if self.profile.provider == 'clef':
                if abs(confidence - probabilities[choice]) > 1e-9:
                    raise DecisionError('decision_response_invalid')
                semantics = 'clef-max-probability'
            else:
                semantics = 'jev-normalized-concentration'
            answers[qid] = ChoiceAnswer(choice, probabilities, confidence, semantics)
        return DecisionResult(self.profile.provider, self.profile.protocol, self.profile.model,
                              data['model'], answers, dict(usage), budget)
