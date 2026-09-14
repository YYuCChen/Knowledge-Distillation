"""Model-body JSON only; never repairs content or applies a business schema."""
from dataclasses import dataclass
import hashlib
import json
import re

PARSER_VERSION = 'model-json-1'


class ModelJSONError(ValueError):
    def __init__(self, category):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class ParsedModelJSON:
    value: object
    envelope: str
    parser_version: str
    raw_sha256: str


def parse_model_json(raw):
    if not isinstance(raw, str):
        raise ModelJSONError('json_syntax_invalid')
    digest = hashlib.sha256(raw.encode()).hexdigest()
    try:
        value = json.loads(raw)
        envelope = 'bare'
    except ValueError:
        trimmed = raw.strip()
        match = re.fullmatch(r'```json\r?\n(.*)\r?\n```', trimmed, re.DOTALL)
        if match is None:
            raise ModelJSONError('envelope_invalid' if '```' in trimmed else 'json_syntax_invalid') from None
        try:
            value = json.loads(match[1])
        except ValueError:
            raise ModelJSONError('json_syntax_invalid') from None
        envelope = 'json_fence'
    return ParsedModelJSON(value, envelope, PARSER_VERSION, digest)
