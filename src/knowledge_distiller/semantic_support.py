"""Validate the evidence contract for an AI recognition correction.

Exact quotations establish provenance, not semantic truth. The assessment is
still an AI judgment, retained for review and measured with source/model tests.
There is deliberately no spelling similarity, word-count or language gate.
"""


ASSESSMENT_PROMPT = '''
每个有内容变化的repair还须给出assessment对象：
{"kind":"recognition_error","original_reading_possible":false,
 "original_reading_analysis":"原读法在此处为何不能成立，不能只说是错字",
 "same_referent_analysis":"引用如何指向本次同一个对象，而非相邻另一术语",
 "source_support_analysis":"具体来源怎样支持此替换；未听/未看不能声称听辨/目视",
 "competing_readings":[],"alternatives_analysis":"已考虑哪些其他解释，为何可排除",
 "meaning_changes":[]}
original_reading_possible为true、仍有competing_readings或meaning_changes时保留原文，
有真实疑问才另记issue；语法润色、改正作者观点、仅字形相似或术语共现不能算recognition_error。
meaning_changes列出主体、数字、单位、否定、条件、因果、立场等实际变化，不得隐去。
analysis必须解释具体来源和此次修改；结构完整不等于判断正确，保留残余不确定。
evidence_quotes可给出多条逐字原文引用，由程序定位，不需你计算偏移；不能拼接省略假引文。
格式重试中，省略原疑点不等于解决。若原文证据确实推翻首次疑点，可返回resolutions数组：
每项含反馈给出的issue_id、action（dismissed或resolved）、retained_reading（原文读法）、
original_issue_possible:false、question_analysis（来源怎样推翻原疑点）、reason及evidence_quotes。
可以使用原来已有的证据，但必须针对同一个问题明确解释；仍有竞争读法就保留问题。
'''


def assess_correction(source, original, replacement, quotes, assessment):
    """Return a precise failing field, or None for a supported AI proposal."""
    if original == replacement:
        return None
    if not isinstance(quotes, list) or not quotes or any(
        not isinstance(q, str) or not q.strip() or q not in source for q in quotes
    ):
        return 'evidence_quotes'
    # The operation already binds original_text to its exact source occurrence.
    # Supporting evidence can be a separate explicit spelling or antecedent;
    # forcing it to repeat the erroneous reading rejects legitimate corrections.
    if not isinstance(assessment, dict):
        return 'assessment'
    if assessment.get('kind') != 'recognition_error':
        return 'assessment.kind'
    if assessment.get('original_reading_possible') is not False:
        return 'assessment.original_reading_possible'
    for field in ('original_reading_analysis', 'same_referent_analysis',
                  'source_support_analysis', 'alternatives_analysis'):
        if not isinstance(assessment.get(field), str) or not assessment[field].strip():
            return 'assessment.' + field
    for field in ('competing_readings', 'meaning_changes'):
        if assessment.get(field) != []:
            return 'assessment.' + field
    return None
