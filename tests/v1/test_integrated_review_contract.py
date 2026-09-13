"""G1 regression expectations: P03/P09/P10 and H02/H03/H25.

Synthetic sources only. Expected behavior comes from the unified construction
contract, not the current validator output.
"""
import json
import pytest

from knowledge_distiller.primary import PrimaryRecovery
from knowledge_distiller.v1.reviewer import build_reviewer


class EchoClient:
    model = "synthetic-echo"

    def complete(self, **kwargs):
        text = json.loads(kwargs["user"].split("\n", 1)[1])
        return json.dumps({"candidate_text": text, "issues": [], "repairs": []})


def test_failed_attempt_questions_survive_a_separate_retry(tmp_path):
    class Client(EchoClient):
        calls = 0
        def complete(self, **kwargs):
            self.calls += 1
            payload = json.loads(super().complete(**kwargs))
            if self.calls == 1:
                payload['issues'] = [
                    {'issue_text': '15', 'occurrence': 0, 'reason': 'number unclear',
                     'meaning_may_change': True},
                    {'issue_text': 'missing quote', 'occurrence': 0, 'reason': 'unlocated issue',
                     'meaning_may_change': True}]
            return json.dumps(payload)
    client = Client()
    source = PrimaryRecovery('The dose is 15 mg.', 'en', ())
    reviewer = build_reviewer(client)
    first = reviewer.review_in_directory(source, tmp_path)
    assert first.failure
    second = reviewer.review_in_directory(source, tmp_path)
    assert second.failure
    assert second.incomplete_candidate.concerns[0].text == '15'
    assert len(second.response_chain) > len(first.response_chain)
    assert any(d.get('issue', {}).get('reason') == 'unlocated issue'
               for d in second.incomplete_candidate.diagnostics)


def test_provider_schema_accepts_full_semantic_evidence():
    from jsonschema import validate
    from knowledge_distiller.v1.reviewer import _REVIEW_FORMAT
    from tests.v1.test_v12_source_integrity import spelling_assessment
    validate({'candidate_text': 'transcription', 'issues': [], 'resolutions': [],
        'repairs': [{'original_text': 'transcripton', 'source_occurrence': 0,
            'replacement': 'transcription', 'occurrence': 0, 'reason': 'spelling',
            'evidence': 'transcripton', 'evidence_spans': [],
            'evidence_quotes': ['transcripton'], 'meaning_may_change': False,
            'assessment': spelling_assessment()}]}, _REVIEW_FORMAT['schema'])


def test_explicit_antecedent_evidence_need_not_repeat_the_typo():
    from knowledge_distiller.semantic_support import assess_correction
    from tests.v1.test_v12_source_integrity import spelling_assessment
    source = '我们去人民公园，公共的公、花园的园。人民公元门口有新路牌。'
    assert assess_correction(source, '人民公元', '人民公园',
        ['我们去人民公园，公共的公、花园的园。'], {**spelling_assessment(),
        'same_referent_analysis': '前句释字的地点即后句门口所在地点。'}) is None


def test_segmented_identity_preserves_all_original_whitespace(tmp_path):
    text = "  \n" + "def example():\n    return 15  # source\n\n" * 160 + "\n  "
    result = build_reviewer(EchoClient()).review_in_directory(PrimaryRecovery(text, "en", ()), tmp_path)
    assert result.failure is None
    assert result.candidate.text == text
    assert result.candidate.repairs == ()


def test_invalid_json_is_not_completed_review_on_first_or_restart(tmp_path):
    class BrokenClient:
        model = "synthetic-broken"

        def complete(self, **kwargs):
            return "not json"

    for _ in range(2):
        result = build_reviewer(BrokenClient()).review_in_directory(
            PrimaryRecovery("The dose is 15 mg.", "en", ()), tmp_path)
        assert str(result.failure) == "invalid_output"
        assert result.candidate is None


def test_empty_issue_does_not_create_whole_source_question(tmp_path):
    class EmptyIssue(EchoClient):
        def complete(self, **kwargs):
            payload = json.loads(super().complete(**kwargs))
            payload["issues"] = [{}]
            return json.dumps(payload)

    result = build_reviewer(EmptyIssue()).review_in_directory(
        PrimaryRecovery("The dose is 15 mg.", "en", ()), tmp_path)
    assert str(result.failure) == "invalid_output"
    assert result.candidate is None


def test_retry_omission_keeps_same_question_after_restart(tmp_path):
    class RetryClient(EchoClient):
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            payload = json.loads(super().complete(**kwargs))
            if self.calls == 1:
                payload['issues'] = [{'issue_text': '15', 'occurrence': 0,
                    'reason': 'The source number is unclear.', 'meaning_may_change': True}]
                payload['repairs'] = [{}]  # format retry is not a resolution
            return json.dumps(payload)

    source = PrimaryRecovery('The dose is 15 mg.', 'en', ())
    client = RetryClient()
    first = build_reviewer(client).review_in_directory(source, tmp_path)
    assert len(first.candidate.concerns) == 1
    resumed = build_reviewer(client).review_in_directory(source, tmp_path)
    assert resumed == first
    assert client.calls == 2


def test_retry_only_legacy_record_is_reaudited(tmp_path):
    from knowledge_distiller.v1.reviewer import ReviewBinding
    import hashlib
    source = PrimaryRecovery('The dose is 15 mg.', 'en', ())
    raw = json.dumps({'candidate_text': source.text, 'issues': [], 'repairs': []})
    class Counting(EchoClient):
        calls = 0
        def complete(self, **kwargs):
            self.calls += 1
            return super().complete(**kwargs)
    client = Counting()
    (tmp_path / 'review-response.retry.json').write_text(json.dumps({
        'identity': ReviewBinding(client).identity(source.text), 'primary_text': source.text,
        'text': raw, 'response_sha256': hashlib.sha256(raw.encode()).hexdigest()}))
    result = build_reviewer(client).review_in_directory(source, tmp_path)
    assert result.failure is None
    assert client.calls == 1


def test_local_edit_survives_unrelated_full_candidate_error():
    from knowledge_distiller.review_validation import validate_response
    from tests.v1.test_v12_source_integrity import repair, spelling_assessment
    source = 'transcripton transcription. Keep this sentence.'
    row = repair('transcripton', 'transcription', source, assessment=spelling_assessment())
    result = validate_response(source, json.dumps({'candidate_text':'broken unrelated output',
        'repairs':[row], 'issues':[]}))
    assert result.text == 'transcription transcription. Keep this sentence.'
    assert result.repairs[0]['start'] == 0
    assert result.repairs[0]['source_start'] == 0


@pytest.mark.parametrize('original,replacement', [('苹杲','苹果'), ('Clodcode','Claude Code')])
def test_correction_contract_has_no_language_length_or_word_count_gate(original, replacement):
    from knowledge_distiller.semantic_support import assess_correction
    from tests.v1.test_v12_source_integrity import spelling_assessment
    source = f'{original}：此处指的是{replacement}。'
    assessment = {**spelling_assessment(),
        'original_reading_analysis':f'此处{original}与紧接的解释不一致。',
        'same_referent_analysis':'冒号后的解释明确是此词的同一指称。',
        'source_support_analysis':f'此处的解释逐字给出{replacement}。',
        'alternatives_analysis':'本合成样本没有另一个对象。'}
    assert assess_correction(source, original, replacement, [source], assessment) is None


def test_competing_meaning_and_grammar_cannot_use_similarity_as_authority():
    from knowledge_distiller.semantic_support import assess_correction
    from tests.v1.test_v12_source_integrity import spelling_assessment
    source = 'hypertension is high blood pressure; Hypotension is low blood pressure.'
    assessment = {**spelling_assessment(), 'original_reading_possible':True,
                  'competing_readings':['hypertension','Hypotension']}
    assert assess_correction(source,'hypertension','Hypotension',[source],assessment)
    assert assess_correction('How we doing?', 'How we', 'How are we', ['How we doing?'],
                             {**spelling_assessment(), 'kind':'grammar'})
    assert assess_correction(source,'hypertension','Hypotension',[source],{'reliable':True})


def test_explicit_source_dismissal_is_persisted_and_reused(tmp_path):
    from knowledge_distiller.review_validation import issue_identity
    source = PrimaryRecovery('The dose is 15 mg, explicitly fifteen milligrams.', 'en', ())
    class CorrectMisreading(EchoClient):
        calls = 0
        def complete(self, **kwargs):
            self.calls += 1
            payload = json.loads(super().complete(**kwargs))
            if self.calls == 1:
                payload.update(issues=[{'issue_text':'15','occurrence':0,
                    'reason':'15 or 50?', 'meaning_may_change':True}], repairs=[{}])
            else:
                start = source.text.index('15')
                identity = issue_identity(source.text, start, start+2)
                assert identity in kwargs['system']
                payload['resolutions'] = [{'issue_id':identity, 'action':'dismissed',
                    'retained_reading':'15','original_issue_possible':False,
                    'question_analysis':'The source explicitly expands 15 as fifteen milligrams.',
                    'reason':'The initial 50 alternative contradicts the explicit expansion.',
                    'evidence_quotes':[source.text]}]
            return json.dumps(payload)
    client = CorrectMisreading()
    first = build_reviewer(client).review_in_directory(source,tmp_path)
    assert first.failure is None and not first.candidate.concerns
    assert any(d['code']=='issue_resolution' for d in first.candidate.diagnostics)
    resumed = build_reviewer(client).review_in_directory(source,tmp_path)
    assert resumed == first and client.calls == 2
    assert len(first.response_chain) == 2


def test_overlapping_questions_stay_local():
    from knowledge_distiller.review_validation import validate_response
    source = 'prefix. The dose is 15 mg. unrelated suffix.'
    issues = [{'issue_text':text,'occurrence':0,'reason':reason,'meaning_may_change':True}
              for text,reason in [('15 mg','dose unclear'),('15','number unclear')]]
    result=validate_response(source,json.dumps({'candidate_text':source,'issues':issues,'repairs':[]}))
    assert len(result.concerns)==1
    assert result.concerns[0].text=='15 mg'
    assert 'dose unclear' in result.concerns[0].reason and 'number unclear' in result.concerns[0].reason


def test_unlocated_substantive_issue_cannot_disappear_on_retry(tmp_path):
    class MissingLocation(EchoClient):
        calls = 0
        def complete(self, **kwargs):
            self.calls += 1
            payload=json.loads(super().complete(**kwargs))
            if self.calls % 2:
                payload['issues']=[{'issue_text':'50','occurrence':0,'reason':'dose unclear',
                                   'meaning_may_change':True}]
            return json.dumps(payload)
    result=build_reviewer(MissingLocation()).review_in_directory(PrimaryRecovery('15 mg','en',()),tmp_path)
    assert result.failure=='invalid_output' and result.candidate is None
    assert not result.incomplete_candidate.concerns
    assert any(d.get('issue',{}).get('reason')=='dose unclear' for d in result.incomplete_candidate.diagnostics)
