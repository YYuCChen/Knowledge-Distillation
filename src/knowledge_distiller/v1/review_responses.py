"""Source-review response receipts, separate from the validated review result."""
from .response_receipts import ResponseReceipts
from .model_json import parse_model_json, ModelJSONError


def review_receipts(binding, source):
    from knowledge_distiller.review_validation import RULE_VERSION
    return ResponseReceipts(
        binding.record_path.with_suffix('.responses') if binding.record_path else None,
        operation='source_review', source=source,
        contract={'review_identity': binding.identity(source), 'feedback': binding.feedback,
                  'max_tokens': 4096},
        model_identity={'model': getattr(binding.client, 'model', None),
                        'endpoint': getattr(binding.client, 'base_url', None)},
        parent_response_hash=getattr(binding, 'parent_response_hash', None),
        validator_version=RULE_VERSION)


def complete_review_response(binding, source, **request):
    receipts = review_receipts(binding, source)
    text = receipts.pending() if not binding.feedback or binding.parent_response_hash else None
    if text is None:
        request_id = receipts.begin()
        text = binding.client.complete(**request)
        receipts.receive(request_id, text)
    try:
        parse_model_json(text)
    except ModelJSONError as error:
        receipts.mark(text, 'parse_failed', category=error.category)
        return text
    from knowledge_distiller.review_validation import validate_response
    candidate = validate_response(source, text)
    if any(row.get('code') == 'invalid_json_or_shape' for row in candidate.diagnostics):
        receipts.mark(text, 'validation_failed', category='schema_invalid')
    else:
        receipts.mark(text, 'prepared')
    # The adapter still validates, merges and preserves all unresolved evidence.
    # prepared does not claim a completed review or a database commit.
    return text
