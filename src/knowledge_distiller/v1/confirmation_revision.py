"""Concern revisions ignore shifting offsets but bind choices and review identity."""
import hashlib
import json


class ConfirmationConflict(ValueError):
    def __init__(self, message, *, affected_member_uids=()):
        super().__init__(message)
        self.affected_member_uids = tuple(affected_member_uids)


def revision(pending, concern):
    payload = [pending.get('review_identity', pending.get('token')), concern.get('audio_name'),
               concern.get('text'), concern.get('candidates'), concern.get('member_id')]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
