# -*- coding: utf-8 -*-
"""회귀 테스트 — LLM·법제처 호출 없이 몇 초. 지난 사고를 다시 내지 않는지 고정합니다.

  python test_regressions.py        실패가 있으면 종료 코드 1

사고 경위는 CODE_NOTES.md 4장의 같은 함수 항목에 있습니다.
"""
import os
import re
import subprocess
import sys
import unittest
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import ai_client  # noqa: E402
import answer_cache  # noqa: E402
import eval_run  # noqa: E402
import intent  # noqa: E402
import law_client  # noqa: E402
import main  # noqa: E402


class CleanOutput(unittest.TestCase):
    def test_article_number_spaces(self):
        got = ai_client._clean_output("【근거】\n「폐기물관리법」 제8 조제1 항에 따라")
        self.assertIn("제8조제1항", got)

    def test_number_line_not_joined(self):
        got = ai_client._clean_output("【근거】\n별표 4\n와 같다")
        self.assertIn("별표 4\n", got)

    def test_conclusion_wrapper_removed_bold_kept(self):
        got = ai_client._clean_output("**【결론】** 해야 합니다\n【근거】 x\n【설명】 **중요** 내용")
        self.assertTrue(got.startswith("【결론】"))
        self.assertIn("**중요**", got)

    def test_bold_inside_article_number(self):
        self.assertIn("제12조", ai_client._clean_output("【근거】 제**12**조"))

    def test_think_removed(self):
        self.assertEqual(ai_client._clean_output("<think>abc</think>답"), "답")


class LooksLikeLaw(unittest.TestCase):
    def test(self):
        self.assertFalse(ai_client._looks_like_law("누출검사방법"))
        self.assertTrue(ai_client._looks_like_law("석면안전관리에 관한 법률"))
        self.assertTrue(ai_client._looks_like_law("토양환경보전법"))


class FixYesNo(unittest.TestCase):
    def test_wh_question_untouched(self):
        self.assertEqual(ai_client._fix_yesno("누출검사를 언제 받았나요?", ["2020년"]), ["2020년"])

    def test_single_option_gets_no(self):
        self.assertEqual(ai_client._fix_yesno("석유류를 저장하나요?", ["석유류", "모름"]),
                         ["석유류", "아니오", "모름"])

    def test_negative_only_gets_positive(self):
        self.assertEqual(ai_client._fix_yesno("악취배출시설이 있나요?", ["악취배출시설이 없습니다", "모름"]),
                         ["악취배출시설이 있습니다", "악취배출시설이 없습니다", "모름"])

    def test_real_choices_kept(self):
        opts = ["지하매설", "지상·옥내", "모름"]
        self.assertEqual(ai_client._fix_yesno("저장시설이 지하에 있나요?", opts), opts)


class ClarifyText(unittest.TestCase):
    def test_split_line_keeps_middle_dot(self):
        self.assertEqual(ai_client._split_clarify_line("저장시설 종류|지하매설/지상·옥내/모름"),
                         ["저장시설 종류", "지하매설", "지상·옥내", "모름"])

    def test_unknown_is_not_todo(self):
        self.assertFalse(ai_client._is_todo_option("모르겠습니다"))
        self.assertTrue(ai_client._is_todo_option("확인이 필요합니다"))


class ConclusionQuestion(unittest.TestCase):
    CASES = [("다음 정기 누출검사를 언제까지 받아야 합니까?", True),
             ("언제 설치했습니까?", False),
             ("보정 적용 여부는?", True),
             ("소음 보정값을 적용해야 하나요?", True)]

    def test_ai_client(self):
        for q, want in self.CASES:
            self.assertEqual(bool(ai_client.CONCLUSION_Q.search(q)), want, q)

    def test_eval_run_same_rule(self):
        for q, want in self.CASES:
            self.assertEqual(bool(eval_run.CONCLUSION_Q.search(q)), want, q)


class ScopePart(unittest.TestCase):
    Q = "9급 공무원 시험 응시 자격(나이·학력)과 관련한 조문들이 궁금하고 더해서 점심 식사는 뭘해야해??"

    def test_part_from_question_ok(self):
        self.assertTrue(ai_client._scope_part_ok("9급 공무원 시험 응시 자격(나이·학력)과 관련한 조문들이 궁금", self.Q))

    def test_invented_part_rejected(self):
        self.assertFalse(ai_client._scope_part_ok("점심 식사 시 금연 의무", self.Q))


class IntentClassify(unittest.TestCase):
    def test(self):
        for q, kind in [("음식물을 무단투기한 경우 과태료는?", "SANCTION"),
                        ("소비기한 지난 원재료 보관 시 처분은?", "OTHER"),
                        ("폐기물관리법 제25조 내용 알려줘", "ARTICLE_LOOKUP"),
                        ("식품위생법 제21조제3항 단서에 따라 갱신해 줄 수 있나요?", "OTHER"),
                        ("특정대기유해물질이 뭐야?", "DEFINITION")]:
            self.assertEqual(intent.classify(q).kind, kind, q)


class Citations(unittest.TestCase):
    def found(self, s):
        return [m.group(0).strip() for m in main.CITE_RE.finditer(s)]

    def test_two_articles_in_parens(self):
        self.assertEqual(len(self.found("(제3조 및 제4조)")), 2)

    def test_bracket_law_name(self):
        m = next(main.CITE_RE.finditer("[토양환경보전법 시행규칙] 제12조제2항"))
        self.assertEqual(m.group(1), "[토양환경보전법 시행규칙]")

    def test_list_with_middle_dot(self):
        self.assertEqual(self.found("제54조·제55조제1항·제63조"), ["제54조", "제55조제1항", "제63조"])


class CalcCheck(unittest.TestCase):
    def test_wrong_sum_and_invented_date(self):
        w = main.verify_calc("【계산】\n기준일: 2010년 9월 23일\n+ 주기: 8년\n= 다음 검사: 2038년 9월 23일",
                             "설치 15년 경과")
        self.assertEqual(len(w), 2)

    def test_correct_calc_passes(self):
        self.assertEqual(main.verify_calc(
            "【계산】\n기준일: 2010년 3월 15일\n+ 주기: 8년\n= 다음 검사: 2018년 3월 15일",
            "2010년 3월 15일에 설치"), [])


class ClarifyAnswers(unittest.TestCase):
    def test_excluded_terms_only_kind_questions(self):
        got = main._excluded_terms("배출하는 폐기물이 방사성폐기물에 해당되나요?: 아니오 / 허가를 받았나요?: 아니오")
        self.assertEqual(got, ["방사성폐기물"])

    def test_repeat_question(self):
        already = {main._norm_q("누출검사를 언제 받았습니까?")}
        self.assertTrue(main._is_repeat_question("마지막 누출검사를 언제 받았습니까?", already))
        self.assertFalse(main._is_repeat_question("저장 용량은 얼마입니까?", already))

    def test_cache_mark_uses_last_colon(self):
        got = answer_cache._split_mark(answer_cache.MARK + " 이전 답: 10/22 02:36에 저장: 예 / 용량: 2만L")
        self.assertEqual(got, ("용량: 2만L", "yes"))


class EvalAutoAnswer(unittest.TestCase):
    def test_longer_key_first(self):
        self.assertEqual(eval_run.pick_option("재해취약지역에 포함됩니까?", ["예", "아니오", "모름"],
                                              {"지역": "관리지역", "재해취약지역": "아니오"}, "모름"), "아니오")

    def test_positive_option_is_term(self):
        self.assertEqual(eval_run.pick_option("등록대상동물입니까?", ["등록대상동물", "아니오", "모름"],
                                              {"등록대상동물": "예"}, "모름"), "등록대상동물")


class TableCompact(unittest.TestCase):
    RAW = ("┌────┬────┬────┐\n"
           "│구분    │소음원  │주간    │\n"
           "├────┼────┼────┤\n"
           "│가. 주거│공장    │55 이하 │\n"
           "\n"
           "│지역    ├────┼────┤\n"
           "│        │공사장  │65 이하 │\n"
           "├────┼────┼────┤\n"
           "│나. 그 밖│공장    │65 이하 │\n"
           "│의 지역 ├────┼────┤\n"
           "│        │공사장  │70 이하 │\n"
           "└────┴────┴────┘")

    def test_merged_first_cell_becomes_group_label(self):
        lines = main._BOX_CHARS.sub("", main._hoist_row_labels(self.RAW)).split("\n")
        lines = [re.sub(r"\s*│\s*", "│", ln.strip()) for ln in lines if ln.strip("│| ")]
        self.assertIn("▶ 가. 주거지역", lines)
        self.assertIn("▶ 나. 그 밖의 지역", lines)
        i, j = lines.index("▶ 가. 주거지역"), lines.index("▶ 나. 그 밖의 지역")
        self.assertTrue(any("공사장" in ln and "65" in ln for ln in lines[i:j]))
        self.assertTrue(any("공사장" in ln and "70" in ln for ln in lines[j:]))

    def test_plain_rows_unchanged(self):
        self.assertNotIn("▶", main._compact_bp("┌──┬──┐\n│가 │1   │\n├──┼──┤\n│나 │2   │\n└──┴──┘"))


class ConclusionMerge(unittest.TestCase):
    def test_only_conclusion_replaced(self):
        a = "【결론】\n네, 초과했습니다.\n\n【근거】\nx\n\n【계산】\n판정: 초과하지 않았습니다."
        got = main._merge_conclusion(a, "【결론】\n아니오, 초과하지 않았습니다.\n\n【근거】\nz")
        self.assertTrue(got.startswith("【결론】\n아니오"))
        self.assertIn("【계산】\n판정: 초과하지 않았습니다.", got)
        self.assertNotIn("z", got)

    def test_no_conclusion_in_rewrite(self):
        self.assertEqual(main._merge_conclusion("【결론】\n네\n【근거】 x", "다시 쓴 답"), "")


class SplitTerms(unittest.TestCase):
    def test(self):
        self.assertEqual(ai_client._split_terms("소음·진동관리법, 1. 누출검사 / 토양오염 (설명)"),
                         ["소음·진동관리법", "누출검사", "토양오염"])


class Flags(unittest.TestCase):
    def test_env_wins_then_file(self):
        import flags
        os.environ["ZZ_TEST_FLAG"] = "1"
        self.assertTrue(flags.on("zz_no_such_flag", "ZZ_TEST_FLAG"))
        os.environ["ZZ_TEST_FLAG"] = "0"
        self.assertFalse(flags.on("zz_no_such_flag", "ZZ_TEST_FLAG"))
        os.environ["ZZ_TEST_FLAG"] = " "
        self.assertFalse(flags.on("zz_no_such_flag", "ZZ_TEST_FLAG"))
        del os.environ["ZZ_TEST_FLAG"]


class VerdictConflict(unittest.TestCase):
    def test_conclusion_vs_calc(self):
        a = ("【결론】\n네, 규제기준을 초과했습니다. 초과 시 조치를 받습니다.\n\n【계산】\n- 적용 기준: 75dB(A)\n"
             "- 판정: 68dB(A) < 75dB(A) 이므로 기준 미달 (초과 아님)\n\n【설명】\nx")
        self.assertTrue(main._verdict_conflict(a))

    def test_consistent(self):
        a = "【결론】\n초과하지 않았습니다.\n\n【계산】\n- 판정: 68 < 75 이므로 초과 아님\n"
        self.assertEqual(main._verdict_conflict(a), "")

    def test_thinking_trace_is_self_correction(self):
        for t in ("*잠깐, 다시 확인해 보겠습니다.*", "**재검토:**", "**결론 수정:**", "**최종 결론:**"):
            self.assertTrue(main._SELF_CORR_RE.search(t), t)

    def test_explanation_verdict_when_no_calc(self):
        a = ("【결론】\n예, 규제기준을 초과했습니다.\n\n【설명】\n1. 기준은 75dB 입니다.\n"
             "2. 68dB < 75dB 이므로 초과가 아닙니다.\n")
        self.assertTrue(main._verdict_conflict(a))
        self.assertEqual(main._verdict_conflict(a.replace("이므로 초과가 아닙니다", "이므로 초과했습니다")), "")

    def test_conditional_sentence_ignored(self):
        a = "【결론】\n초과하지 않았습니다.\n\n【설명】\n만약 기준이 65dB 이면 초과이므로 조치를 받습니다.\n"
        self.assertEqual(main._verdict_conflict(a), "")

    def test_revision_notes_removed(self):
        a = "【결론】\n초과합니다.\n\n【설명】\n1. 기준 65dB.\n\n※ 검토 의견 반영 및 최종 수정\n이전 답변의 결론을 정정합니다."
        got = ai_client._strip_revision_notes(a)
        self.assertNotIn("검토 의견", got)
        self.assertIn("【설명】\n1. 기준 65dB.", got)


class LastFinish(unittest.TestCase):
    def test_truncation_is_remembered_per_stage(self):
        import applog
        old = applog.emit
        applog.emit = lambda *a, **k: None
        try:
            applog.llm("answer", 10, 10, 0.1, finish="length")
            applog.llm("review", 10, 10, 0.1, finish="stop")
            self.assertEqual(applog.last_finish("answer"), "length")
            applog.llm("answer", 10, 10, 0.1, finish="stop")
            self.assertEqual(applog.last_finish("answer"), "stop")
        finally:
            applog.emit = old


class TableRecords(unittest.TestCase):
    RAW = ("┌─────┬────┬────┐\n"
           "│대상 지역 │소음원  │주간    │\n"
           "├─────┼────┼────┤\n"
           "│가. 주거  │공장    │55 이하 │\n"
           "│지역      ├────┼────┤\n"
           "│          │공사장  │65 이하 │\n"
           "├─────┼────┼────┤\n"
           "│나. 그 밖 │공장    │65 이하 │\n"
           "│의 지역   ├────┼────┤\n"
           "│          │공사장  │70 이하 │\n"
           "└─────┴────┴────┘")

    def test_each_row_carries_group_and_column_names(self):
        got = main._compact_bp(self.RAW).split("\n")
        self.assertIn("대상 지역: 가. 주거 지역 | 소음원: 공사장 | 주간: 65 이하", got)
        self.assertIn("대상 지역: 나. 그 밖의 지역 | 소음원: 공사장 | 주간: 70 이하", got)

    def test_separate_entries_in_one_band_fall_back(self):
        raw = ("┌──────────┬────┐\n│시설                │기준    │\n├──────────┼────┤\n"
               "│가) 큰 시설         │1.7     │\n│나) 작은 시설       │1.9     │\n└──────────┴────┘")
        got = main._compact_bp(raw)
        self.assertNotIn("한 줄이 한 행", got)
        self.assertIn("│가) 큰 시설│1.7│", got)

    def test_flow_boxes_untouched(self):
        raw = "┌───┐  ┌──┐\n│작성  │?│접수│\n└───┘  └──┘"
        self.assertNotIn("한 줄이 한 행", main._compact_bp(raw))


class AiHitProtect(unittest.TestCase):
    def test_match_by_law_and_number(self):
        f = {"name": "악취방지법"}
        a1 = {"조문번호": "8", "조문가지번호": "2"}
        a2 = {"조문번호": "8", "조문가지번호": ""}
        got = main._ai_hit_articles([("악취방지법", "0008", "02"), ("없는법", "1", "")], [(f, a2), (f, a1)])
        self.assertEqual(got, [(f, a1)])


class LawClient(unittest.TestCase):
    def test_html_entities(self):
        self.assertEqual(law_client._fix_html_entities("소음&middot;진동 &foo; &amp;"), "소음·진동 &amp;foo; &amp;")

    def test_jo_code(self):
        self.assertEqual(law_client._jo_code("10", "2"), "001002")
        self.assertEqual(law_client._jo_code("000400"), "000400")

    def test_parser_fixtures(self):
        r = subprocess.run([sys.executable, "test_law_client_patch.py"], cwd=HERE,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout[-800:])


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=1, warnings="ignore").result
    sys.exit(0 if res.wasSuccessful() else 1)
