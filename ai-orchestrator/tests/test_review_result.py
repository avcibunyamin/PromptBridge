"""F3-02 §4: reviewer result parser. Every invalid shape is rejected; none becomes success."""
import json
import unittest
from app import review_result as rr


def block(obj_or_text):
    body = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text, ensure_ascii=False)
    return f'İnceleme notlarım.\n\n```conductor-review\n{body}\n```\n'


FINDING = {'id': 'F1', 'severity': 'high', 'summary': 'Boş girişte bölme hatası', 'evidence': ['src/calc.py:42']}


class ValidResults(unittest.TestCase):
    def test_contract_example_is_itself_valid(self):
        r = rr.parse('Açıklama.\n' + rr.CONTRACT_EXAMPLE + '\n')
        self.assertEqual((r.verdict, len(r.findings)), ('changes_requested', 1))

    def test_pass_changes_blocked(self):
        self.assertEqual(rr.parse(block({'verdict': 'pass', 'findings': []})).verdict, 'pass')
        r = rr.parse(block({'verdict': 'changes_requested', 'findings': [FINDING]}))
        self.assertEqual(dict(r.findings[0])['id'], 'F1')
        b = rr.parse(block({'verdict': 'blocked', 'findings': [], 'blocked_reason': 'Klasör erişilemiyor'}))
        self.assertEqual(b.blocked_reason, 'Klasör erişilemiyor')

    def test_whitespace_after_block_and_crlf_are_fine(self):
        text = 'x\r\n```conductor-review\r\n{"verdict":"pass","findings":[]}\r\n```\r\n  \n'
        self.assertEqual(rr.parse(text).verdict, 'pass')


class InvalidResults(unittest.TestCase):
    def code(self, text):
        with self.assertRaises(rr.ReviewInvalid) as ctx:
            rr.parse(text)
        return ctx.exception.code

    def test_block_shape(self):
        self.assertEqual(self.code('Her şey yolunda, pass.'), 'no_block')
        self.assertEqual(self.code(None), 'no_block')
        self.assertEqual(self.code('```conductor-review\n{"verdict":"pass","findings":[]}\n'), 'unclosed_block')
        self.assertEqual(self.code(block({'verdict': 'pass', 'findings': []}) + 'Ek not.'), 'trailing_text')
        two = block({'verdict': 'pass', 'findings': []}).replace('İnceleme notlarım.',
              'Örnek:\n```conductor-review\n{"verdict":"pass","findings":[]}\n```\nGerçek:')
        self.assertEqual(self.code(two), 'multiple_blocks')
        inline = 'Şu biçimi kullanırım: ```conductor-review ... ' + block({'verdict': 'pass', 'findings': []})
        self.assertEqual(self.code(inline), 'multiple_blocks')

    def test_json_strictness(self):
        self.assertEqual(self.code(block('{"verdict":"pass","findings":[],}')), 'invalid_json')
        self.assertEqual(self.code(block('{"verdict":"pass","verdict":"pass","findings":[]}')), 'duplicate_key')
        nan = '{"verdict":"changes_requested","findings":[{"id":"F1","severity":"low","summary":"x","evidence":[],"n":NaN}]}'
        self.assertEqual(self.code(block(nan)), 'non_finite')
        self.assertEqual(self.code(block('["pass"]')), 'schema')
        self.assertEqual(self.code(block('{"verdict":"pass","findings":[]} // yorum')), 'invalid_json')

    def test_schema_rules(self):
        cases = [
            {'verdict': 'PASS', 'findings': []},
            {'verdict': 'pass'},
            {'verdict': 'pass', 'findings': [], 'extra': 1},
            {'verdict': 'pass', 'findings': [FINDING]},                      # contradictory
            {'verdict': 'changes_requested', 'findings': []},                 # nothing to change
            {'verdict': 'blocked', 'findings': []},                           # reason missing
            {'verdict': 'pass', 'findings': [], 'blocked_reason': 'x'},       # reason not allowed
            {'verdict': 'changes_requested', 'findings': [FINDING, dict(FINDING)]},            # duplicate id
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, id='F1000')]},
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, id='1')]},
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, severity='critical')]},
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, summary='   ')]},
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, summary='x' * 501)]},
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, evidence=['e'] * 11)]},
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, evidence=['e' * 301])]},
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, evidence='src/a.py')]},
            {'verdict': 'changes_requested', 'findings': [{k: v for k, v in FINDING.items() if k != 'evidence'}]},
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, link='x')]},
            {'verdict': 'changes_requested', 'findings': [dict(FINDING, id=f'F{i}') for i in range(51)]},
        ]
        for case in cases:
            with self.subTest(case=str(case)[:80]):
                self.assertEqual(self.code(block(case)), 'schema')

    def test_size_limit_counts_utf8_bytes(self):
        many = [dict(FINDING, id=f'F{i}', summary='ğ' * 400, evidence=['ş' * 150] * 2) for i in range(40)]
        self.assertEqual(self.code(block({'verdict': 'changes_requested', 'findings': many})), 'too_large')

    def test_invalid_is_never_success(self):
        # Whatever the rejection, callers only ever get an exception — there is no default verdict.
        for text in ('pass', '```conductor-review\n{"verdict":"pass"}\n```', block('{}')):
            with self.assertRaises(rr.ReviewInvalid):
                rr.parse(text)


class FindingsHash(unittest.TestCase):
    def parse(self, findings):
        return rr.parse(block({'verdict': 'changes_requested', 'findings': findings}))

    def test_order_ids_case_and_whitespace_do_not_matter(self):
        a = self.parse([FINDING, {'id': 'F2', 'severity': 'low', 'summary': 'İsimlendirme', 'evidence': []}])
        b = self.parse([{'id': 'F9', 'severity': 'low', 'summary': '  isimlendirme ', 'evidence': ['x']},
                        dict(FINDING, id='F3', summary='boş  girişte BÖLME hatası')])
        self.assertEqual(rr.findings_hash(a), rr.findings_hash(b))

    def test_turkish_dotless_i_and_capitals_fold_together(self):
        lower = self.parse([dict(FINDING, summary='ışık ayarı kırık')])
        upper = self.parse([dict(FINDING, summary='IŞIK AYARI KIRIK')])
        self.assertEqual(rr.findings_hash(lower), rr.findings_hash(upper))

    def test_severity_or_text_change_changes_hash(self):
        base = rr.findings_hash(self.parse([FINDING]))
        self.assertNotEqual(base, rr.findings_hash(self.parse([dict(FINDING, severity='low')])))
        self.assertNotEqual(base, rr.findings_hash(self.parse([dict(FINDING, summary='Başka hata')])))


if __name__ == '__main__':
    unittest.main()
