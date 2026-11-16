# -*- coding: utf-8 -*-
"""회귀 테스트 — LLM·법제처 호출 없이 몇 초. 지난 사고를 다시 내지 않는지 고정합니다.

  python test_regressions.py        실패가 있으면 종료 코드 1

사고 경위는 CODE_NOTES.md 4장의 같은 함수 항목에 있습니다.
"""
import os
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

    @unittest.expectedFailure
    def test_eval_run_same_rule(self):
        # REFACTOR_PLAN A-1: eval_run 사본이 첫 갈래만 있음. 고치면 이 표시를 지운다.
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
    res = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if res.wasSuccessful() else 1)
