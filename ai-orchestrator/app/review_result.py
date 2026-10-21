"""F3-02 reviewer result contract: one strict `conductor-review` block, parsed or rejected.

Only the reviewer step's terminal, successful `final_text` is parsed (never stream deltas).
A result that does not satisfy every rule is invalid; invalid is never success. Findings and
evidence are the model's own claims: untrusted data, not independent verification.
"""
from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata

FENCE = 'conductor-review'
MAX_BLOCK_BYTES = 32 * 1024
MAX_FINDINGS, MAX_EVIDENCE = 50, 10
SUMMARY_MAX, EVIDENCE_MAX, REASON_MAX = 500, 300, 500
VERDICTS = ('pass', 'changes_requested', 'blocked')
SEVERITIES = ('high', 'medium', 'low')
FINDING_ID = re.compile(r'F[0-9]{1,3}')

# Verbatim in the reviewer prompt: a valid instance, not a template with placeholders.
CONTRACT_EXAMPLE = ('```conductor-review\n'
                    '{"verdict":"changes_requested","findings":[{"id":"F1","severity":"high",'
                    '"summary":"Boş girişte bölme hatası","evidence":["src/calc.py:42",'
                    '"pytest tests/test_calc.py -k empty -> FAILED"]}]}\n'
                    '```')

_OPEN = re.compile(r'^```' + re.escape(FENCE) + r'[ \t\r]*$', re.M)   # CRLF answers are valid too
_CLOSE = re.compile(r'^```[ \t\r]*$', re.M)


class ReviewInvalid(ValueError):
    """`code` is a stable machine reason (shown in the panel and stored with the turn)."""

    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    findings: tuple = field(default_factory=tuple)
    blocked_reason: str | None = None

    def as_json(self):
        data = {'verdict': self.verdict, 'findings': [dict(f) for f in self.findings]}
        if self.blocked_reason is not None:
            data['blocked_reason'] = self.blocked_reason
        return data


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ReviewInvalid('duplicate_key', ', '.join(sorted({k for k in keys if keys.count(k) > 1})))
    return dict(pairs)


def _reject_constant(name):
    raise ReviewInvalid('non_finite', name)


def _text(value, limit, where):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ReviewInvalid('schema', f'{where} must be 1..{limit} characters and not blank')
    return value


def extract_block(final_text):
    """The single fenced block; it must be the last content of the answer."""
    if not isinstance(final_text, str):
        raise ReviewInvalid('no_block', 'no text')
    opens = list(_OPEN.finditer(final_text))
    if not opens:
        # A fence mentioned inline (not on its own line) still counts as an attempt at a second block.
        raise ReviewInvalid('multiple_blocks' if final_text.count('```' + FENCE) > 1 else 'no_block')
    if len(opens) > 1 or final_text.count('```' + FENCE) > 1:
        raise ReviewInvalid('multiple_blocks')
    start = opens[0].end()
    close = _CLOSE.search(final_text, start)
    if not close:
        raise ReviewInvalid('unclosed_block')
    if final_text[close.end():].strip():
        raise ReviewInvalid('trailing_text')
    body = final_text[start:close.start()].strip('\r\n')
    if len(body.encode('utf-8')) > MAX_BLOCK_BYTES:
        raise ReviewInvalid('too_large', f'> {MAX_BLOCK_BYTES} bytes')
    return body


def parse(final_text):
    body = extract_block(final_text)
    try:
        data = json.loads(body, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except ReviewInvalid:
        raise
    except (ValueError, RecursionError) as exc:
        raise ReviewInvalid('invalid_json', type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise ReviewInvalid('schema', 'root must be an object')
    allowed = {'verdict', 'findings', 'blocked_reason'}
    if set(data) - allowed:
        raise ReviewInvalid('schema', 'unknown fields: ' + ', '.join(sorted(set(data) - allowed)))
    verdict = data.get('verdict')
    if verdict not in VERDICTS:
        raise ReviewInvalid('schema', 'verdict must be one of ' + '/'.join(VERDICTS))
    findings = data.get('findings')
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise ReviewInvalid('schema', f'findings must be a list of at most {MAX_FINDINGS}')
    parsed, ids = [], set()
    for index, item in enumerate(findings):
        where = f'findings[{index}]'
        if not isinstance(item, dict) or set(item) - {'id', 'severity', 'summary', 'evidence'} \
                or not {'id', 'severity', 'summary', 'evidence'} <= set(item):
            raise ReviewInvalid('schema', f'{where} needs exactly id, severity, summary, evidence')
        if not isinstance(item['id'], str) or not FINDING_ID.fullmatch(item['id']):
            raise ReviewInvalid('schema', f'{where}.id must match F<1-3 digits>')
        if item['id'] in ids:
            raise ReviewInvalid('schema', f'duplicate finding id {item["id"]}')
        ids.add(item['id'])
        if item['severity'] not in SEVERITIES:
            raise ReviewInvalid('schema', f'{where}.severity must be high/medium/low')
        _text(item['summary'], SUMMARY_MAX, f'{where}.summary')
        evidence = item['evidence']
        if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
            raise ReviewInvalid('schema', f'{where}.evidence must be a list of at most {MAX_EVIDENCE}')
        for n, ref in enumerate(evidence):
            _text(ref, EVIDENCE_MAX, f'{where}.evidence[{n}]')
        parsed.append((('id', item['id']), ('severity', item['severity']),
                       ('summary', item['summary']), ('evidence', tuple(evidence))))
    if verdict == 'pass' and parsed:
        raise ReviewInvalid('schema', 'pass must not carry findings')
    if verdict == 'changes_requested' and not parsed:
        raise ReviewInvalid('schema', 'changes_requested needs at least one finding')
    reason = data.get('blocked_reason')
    if verdict == 'blocked':
        _text(reason, REASON_MAX, 'blocked_reason')
    elif 'blocked_reason' in data:
        raise ReviewInvalid('schema', 'blocked_reason is only allowed with blocked')
    return ReviewResult(verdict, tuple(parsed), reason)


def _fold(text):
    """Comparison key: case, Turkish İ/ı and diacritics ignored; whitespace collapsed.
    (Python's 'İ'.lower() is 'i' + U+0307, so plain lower() splits the same Turkish word.)"""
    decomposed = unicodedata.normalize('NFKD', text.casefold())
    bare = ''.join(ch for ch in decomposed if not unicodedata.combining(ch)).replace('ı', 'i')
    return ' '.join(bare.split())


def findings_hash(result):
    """No-progress key: severity + normalized summary, order-free; ids and evidence excluded."""
    items = sorted((dict(f)['severity'], _fold(dict(f)['summary'])) for f in result.findings)
    return hashlib.sha256(json.dumps(items, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
