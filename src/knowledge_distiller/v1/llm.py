from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Mapping


logger = logging.getLogger(__name__)


class LLMRequestError(RuntimeError):
    pass


def _check_service_status(status, mark_unavailable):
    if status == 401:
        mark_unavailable()
        raise LLMRequestError('llm_config_unavailable')
    if status == 403:
        raise LLMRequestError('llm_access_denied')
    if status == 404:
        raise LLMRequestError('llm_endpoint_or_model_unavailable')
    if not 200 <= status < 300:
        raise LLMRequestError('llm_request_failed')


@dataclass(frozen=True)
class AnthropicMessagesClient:
    base_url: str
    model: str
    secret: Callable[[], str]
    mark_unavailable: Callable[[], None] = lambda: None
    timeout_seconds: float = 120.0

    def complete(self, *, system: str, user: str | list, max_tokens: int) -> str:
        if not self.base_url.strip() or not self.model.strip():
            raise LLMRequestError("llm_not_configured")
        api_key = self.secret()
        if not api_key:
            raise LLMRequestError("llm_secret_unavailable")
        try:
            import httpx
        except ImportError as error:
            raise LLMRequestError("llm_request_failed") from error

        try:
            response = httpx.post(
                f"{self.base_url.rstrip('/')}/v1/messages",
                headers={
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                    "x-api-key": api_key,
                },
                json={
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "thinking": {"type": "disabled"},
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as error:
            logger.warning("LLM request failed: %s", type(error).__name__)
            raise LLMRequestError("llm_request_failed") from error

        _check_service_status(response.status_code, self.mark_unavailable)
        try:
            payload = response.json()
            blocks = payload["content"]
            if payload.get("stop_reason") != "end_turn" or not isinstance(blocks, list):
                raise LLMRequestError("llm_response_incomplete")
            text = "".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, Mapping) and block.get("type") == "text"
            )
        except LLMRequestError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise LLMRequestError("llm_response_invalid") from error
        if not text.strip():
            raise LLMRequestError("llm_response_invalid")
        return text


@dataclass(frozen=True)
class OpenAIResponsesClient:
    base_url: str
    model: str
    secret: Callable[[], str]
    mark_unavailable: Callable[[], None] = lambda: None
    timeout_seconds: float = 120.0
    reasoning_effort: str | None = None
    text_format: dict | None = None

    def complete(self, *, system: str, user: str | list, max_tokens: int) -> str:
        if not self.base_url.strip() or not self.model.strip():
            raise LLMRequestError('llm_not_configured')
        key = self.secret()
        if not key:
            raise LLMRequestError('llm_secret_unavailable')
        import httpx
        content = []
        try:
            if isinstance(user, str):
                content = [{'type': 'input_text', 'text': user}]
            else:
                for block in user:
                    if block['type'] == 'text':
                        content.append({'type': 'input_text', 'text': block['text']})
                    elif block['type'] == 'image':
                        source = block['source']
                        if source['type'] != 'base64' or source['media_type'] not in {
                                'image/jpeg', 'image/png', 'image/webp', 'image/gif'}:
                            raise ValueError('unsupported image')
                        content.append({'type': 'input_image', 'image_url':
                            'data:' + source['media_type'] + ';base64,' + source['data']})
                    else:
                        raise ValueError('unsupported input')
        except (KeyError, TypeError, ValueError) as error:
            raise LLMRequestError('llm_response_invalid') from error
        base = self.base_url.rstrip('/')
        endpoint = base + ('/responses' if base.endswith('/v1') else '/v1/responses')
        request = {'model': self.model, 'instructions': system,
            'input': [{'role': 'user', 'content': content}],
            'max_output_tokens': max_tokens, 'store': False}
        if self.text_format is not None:
            request['text'] = {'format': self.text_format}
        if self.reasoning_effort is not None:
            request['reasoning'] = {'effort': self.reasoning_effort}
        try:
            response = httpx.post(endpoint, headers={'Authorization': 'Bearer ' + key,
                'Content-Type': 'application/json'}, json=request, timeout=self.timeout_seconds)
        except httpx.HTTPError as error:
            logger.warning('OpenAI request failed: %s', type(error).__name__)
            raise LLMRequestError('llm_request_failed') from error
        _check_service_status(response.status_code, self.mark_unavailable)
        try:
            payload = response.json()
            if payload.get('status') != 'completed':
                status = payload.get('status')
                details = payload.get('incomplete_details')
                reason = details.get('reason') if isinstance(details, dict) else None
                logger.warning('Responses did not complete: status=%s reason=%s',
                    status if status in {'incomplete', 'failed', 'cancelled', 'queued', 'in_progress'} else 'unknown',
                    reason if reason in {'max_output_tokens', 'content_filter'} else 'unknown')
                raise LLMRequestError('llm_response_incomplete')
            text = ''.join(block['text'] for item in payload['output']
                if item.get('type') == 'message' and item.get('role') == 'assistant'
                for block in item['content'] if block.get('type') == 'output_text')
            if not text.strip():
                raise LLMRequestError('llm_response_invalid')
            return text
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise LLMRequestError('llm_response_invalid') from error
