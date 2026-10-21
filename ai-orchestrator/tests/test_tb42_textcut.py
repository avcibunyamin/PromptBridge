"""Unit tests for structure-preserving UTF-8 truncation in app.textcut."""

from __future__ import annotations

import unittest

from app.textcut import head, tail


class TestTB42TextCut(unittest.TestCase):
    def test_fitting_text_unchanged_and_nonpositive_budget(self) -> None:
        sample = "# Başlık\n\nMerhaba dünya 🚀\n```python\nx = 'ğüşıöç'\n```\nSon satır.\n"
        size = len(sample.encode("utf-8"))

        self.assertEqual(head(sample, size), sample)
        self.assertEqual(head(sample, size + 500), sample)
        self.assertEqual(tail(sample, size), sample)
        self.assertEqual(tail(sample, size + 500), sample)

        self.assertEqual(head(sample, 0), "")
        self.assertEqual(head(sample, -10), "")
        self.assertEqual(tail(sample, 0), "")
        self.assertEqual(tail(sample, -10), "")
        self.assertEqual(head("", 100), "")
        self.assertEqual(tail("", 100), "")

    def test_utf8_byte_limit_respected_across_all_budgets(self) -> None:
        samples = [
            "Çalışma ğüşıöçĞÜŞİÖÇ 🚀🔥🧠🛠️✅ satır 1\n\n# Bölüm 2\nSatır 2 🌍\n",
            (
                "Giriş paragrafı Türkçe karakterlerle: ğüşıöç.\n\n"
                "```python\n"
                "def hesapla(değer: str) -> str:\n"
                "    return f'Sonuç: {değer} 🚀'\n"
                "```\n\n"
                "# İkinci Başlık\n"
                "Bitiş metni 🎉.\n"
            ),
            (
                "~~~bash\n"
                "echo 'ğüşıöç 🚀'\n"
                "echo 'ikinci satır'\n"
                "~~~\n"
                "Normal metin.\n"
            ),
            "🚀🔥🧠" * 40,
        ]

        for sample in samples:
            total = len(sample.encode("utf-8"))
            for max_bytes in range(0, total + 15):
                h = head(sample, max_bytes)
                t = tail(sample, max_bytes)
                self.assertLessEqual(
                    len(h.encode("utf-8")),
                    max_bytes,
                    f"head exceeded max_bytes={max_bytes}",
                )
                self.assertLessEqual(
                    len(t.encode("utf-8")),
                    max_bytes,
                    f"tail exceeded max_bytes={max_bytes}",
                )
                self.assertEqual(h, head(sample, max_bytes))
                self.assertEqual(t, tail(sample, max_bytes))

    def test_code_block_mid_cut_keeps_even_backtick_fences(self) -> None:
        code_lines = "\n".join(f"deger_{i} = 'ğüşıöç 🚀 {i}'" for i in range(40))
        doc = (
            "# Özet\n\n"
            "Açıklama satırı burada yer alıyor.\n\n"
            f"```python\n{code_lines}\n```\n\n"
            "# Sonuç\n"
            "Son paragraf metni.\n"
        )
        total = len(doc.encode("utf-8"))

        for max_bytes in range(1, total + 5):
            h = head(doc, max_bytes)
            t = tail(doc, max_bytes)
            self.assertLessEqual(len(h.encode("utf-8")), max_bytes)
            self.assertLessEqual(len(t.encode("utf-8")), max_bytes)
            self.assertEqual(
                h.count("```") % 2,
                0,
                f"head produced odd fence count at max_bytes={max_bytes}: {h!r}",
            )
            self.assertEqual(
                t.count("```") % 2,
                0,
                f"tail produced odd fence count at max_bytes={max_bytes}: {t!r}",
            )

    def test_head_cuts_before_heading_when_in_window(self) -> None:
        prefix = "Birinci bölüm satırı.\n" * 12
        heading_and_after = "# İkinci Başlık\nAlt satır bir.\nAlt satır iki.\n"
        doc = prefix + heading_and_after

        prefix_bytes = len(prefix.encode("utf-8"))
        max_bytes = prefix_bytes + 30
        result = head(doc, max_bytes)

        self.assertLessEqual(len(result.encode("utf-8")), max_bytes)
        self.assertEqual(result, prefix)
        self.assertNotIn("# İkinci Başlık", result)

    def test_tail_starts_at_heading_when_in_window(self) -> None:
        before = "Önceki satır.\n"
        after = "# Yeni Başlık\n" + ("İçerik satırı.\n" * 12)
        doc = before + after

        after_bytes = len(after.encode("utf-8"))
        max_bytes = after_bytes + 10
        result = tail(doc, max_bytes)

        self.assertLessEqual(len(result.encode("utf-8")), max_bytes)
        self.assertEqual(result, after)
        self.assertTrue(result.startswith("# Yeni Başlık\n"))

    def test_fence_line_with_info_string_does_not_close_open_block(self) -> None:
        doc = (
            "```markdown\n"
            + ("satır\n" * 20)
            + "```json\n"
            + '{"anahtar": "değer"}\n'
            + ("devam satırı\n" * 20)
            + "```\n"
        )
        mid_after_json = len(
            (
                "```markdown\n"
                + ("satır\n" * 20)
                + "```json\n"
                + '{"anahtar": "değer"}\n'
                + ("devam satırı\n" * 5)
            ).encode("utf-8")
        )
        h = head(doc, mid_after_json)
        self.assertLessEqual(len(h.encode("utf-8")), mid_after_json)
        self.assertTrue(h.endswith("\n```"))

        tail_budget = len(
            (
                "```json\n"
                + '{"anahtar": "değer"}\n'
                + ("devam satırı\n" * 20)
                + "```\n"
            ).encode("utf-8")
        ) + 10
        t = tail(doc, tail_budget)
        self.assertLessEqual(len(t.encode("utf-8")), tail_budget)
        self.assertTrue(t.startswith("```\n"))

    def test_history_package_truncates_large_code_block_within_budget(self) -> None:
        from app import work_prompt

        code_body = "\n".join(f"satir_{i} = 'ğüşıöç 🚀 {i}'" for i in range(200))
        metin = (
            "# Rapor\n\n"
            "Ön açıklama paragrafı.\n\n"
            f"```python\n{code_body}\n```\n\n"
            "# Sonuç\n"
            "Bitiş.\n"
        )
        butce = 2000
        pkg = work_prompt.history_package([("run", "r1", metin)], butce)
        encoded_bytes = work_prompt.size(work_prompt._encode(pkg))

        self.assertLessEqual(encoded_bytes, butce)
        self.assertTrue(pkg["truncated"])
        item = pkg["items"][0]
        self.assertEqual(item["included"], "truncated")
        self.assertEqual(item["text"].count("```") % 2, 0)
        self.assertEqual(
            item["omitted_bytes"],
            work_prompt.size(metin) - work_prompt.size(item["text"]),
        )


if __name__ == "__main__":
    unittest.main()

