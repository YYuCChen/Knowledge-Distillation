"""TypeSafe Jev: closed-choice decisions with calibrated probabilities.

Jev answers typed questions (choice / score / noul) about the given state and
returns probabilities, never text (https://docs.typesafe.ai/api). The app uses
it only where a task is a closed choice; anything that writes text stays with
the configured LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import time
from typing import Callable

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-latest'
SECRET_ACCOUNT = 'jev-api-key'
logger = logging.getLogger(__name__)


class JevError(RuntimeError):
    """A stable code only: never the key, the request or the model's answer."""


@dataclass(frozen=True)
class JevChoice:
    choice: str
    probability: float
    probabilities: dict
    confidence: float
    model: str
    provider: str = 'jev'
    confidence_semantics: str = 'jev-normalized-concentration'


@dataclass
class ActiveDecisionAdapter:
    """Expose the existing business API without relabeling provider probabilities."""

    client: object
    profile_id: str

    def ask(self, state, questions: dict):
        from .decision_client import ChoiceQuestion, NoulQuestion, ChoiceAnswer, DecisionError
        try:
            typed = {}
            for key, question in questions.items():
                if question.get('type') == 'choice' and set(question) == {'type', 'instructions', 'criteria'}:
                    typed[key] = ChoiceQuestion(question['instructions'], question['criteria'])
                elif question.get('type') == 'noul' and set(question) <= {'type', 'instructions', 'criteria'}:
                    typed[key] = NoulQuestion(question['instructions'], question.get('criteria'))
                else:
                    raise DecisionError('decision_request_invalid')
            result = self.client.ask(state, typed)
            answers = {}
            for key, answer in result.answers.items():
                if isinstance(answer, ChoiceAnswer):
                    answers[key] = dict(type='choice', choice=answer.choice,
                        probabilities=answer.probabilities, confidence=answer.confidence,
                        provider=result.provider, confidence_semantics=answer.confidence_semantics)
                else:
                    answers[key] = dict(type='noul', noul=answer.noul, provider=result.provider)
            return answers, result.model
        except DecisionError as error:
            raise JevError(str(error)) from None
        except (KeyError, TypeError, AttributeError):
            raise JevError('decision_request_invalid') from None

    def choose(self, state, *, instructions: str, options: dict) -> JevChoice:
        answers, model = self.ask(state, {'answer': dict(type='choice', instructions=instructions, criteria=options)})
        answer = answers['answer']
        return JevChoice(answer['choice'], answer['probabilities'][answer['choice']],
            answer['probabilities'], answer['confidence'], model,
            answer['provider'], answer['confidence_semantics'])


@dataclass
class UnavailableDecisionAdapter:
    """Keep factory errors inside the business caller's existing error boundary."""

    code: str

    def ask(self, *args, **kwargs):
        raise JevError(self.code)

    def choose(self, *args, **kwargs):
        raise JevError(self.code)


@dataclass
class JevClient:
    secret: Callable[[], str]
    endpoint: str = ENDPOINT
    model: str = MODEL
    timeout_seconds: float = 20.0
    retries: int = 2
    post: Callable | None = None  # httpx.post-compatible; injected in tests
    sleep: Callable[[float], None] = time.sleep
    on_unauthorized: Callable[[], None] = lambda: None

    def choose(self, state, *, instructions: str, options: dict) -> JevChoice:
        """One choice question; ``options`` maps each option name to its description."""
        answers, model = self.ask(state, {'answer': {'type': 'choice', 'instructions': instructions,
                                                     'criteria': options}})
        try:
            answer = answers['answer']
            probabilities = {name: float(value) for name, value in answer['probabilities'].items()}
            choice, confidence = answer['choice'], float(answer['confidence'])
            valid = (answer.get('type', 'choice') == 'choice' and choice in options
                     and set(probabilities) <= set(options) and choice in probabilities
                     and all(math.isfinite(p) and 0 <= p <= 1 for p in probabilities.values())
                     and abs(sum(probabilities.values()) - 1) <= 0.02 and 0 <= confidence <= 1)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise JevError('jev_response_invalid') from error
        if not valid:
            raise JevError('jev_response_invalid')
        return JevChoice(choice, probabilities[choice], probabilities, confidence, model)

    def ask(self, state, questions: dict):
        """(answers, model version) for the given questions, retrying 429/529 with backoff."""
        try:
            key = self.secret()
        except Exception as error:
            raise JevError('jev_secret_unavailable') from error
        if not key:
            raise JevError('jev_secret_unavailable')
        import httpx
        post = self.post or httpx.post
        body = {'model': self.model, 'state': state, 'questions': questions}
        for attempt in range(self.retries + 1):
            try:
                response = post(self.endpoint, headers={'Authorization': 'Bearer ' + key,
                                                        'Content-Type': 'application/json'},
                                json=body, timeout=self.timeout_seconds)
            except httpx.HTTPError as error:
                logger.warning('Jev request failed: %s', type(error).__name__)
                raise JevError('jev_request_failed') from error
            status = response.status_code
            if status in {429, 529} and attempt < self.retries:
                self.sleep(2 ** attempt)
                continue
            if status in {401, 403}:
                self.on_unauthorized()
                raise JevError('jev_unauthorized')
            if status == 422:
                raise JevError('jev_request_invalid')
            if status in {429, 529}:
                raise JevError('jev_busy')
            if status >= 400:
                raise JevError('jev_request_failed')
            try:
                data = response.json()
                answers, model = data['answers'], data.get('model') or self.model
                if not isinstance(answers, dict) or not isinstance(model, str):
                    raise TypeError
            except (ValueError, KeyError, TypeError) as error:
                raise JevError('jev_response_invalid') from error
            return answers, model
        raise JevError('jev_busy')
