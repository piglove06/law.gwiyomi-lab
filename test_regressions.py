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


class CitationNames(unittest.TestCase):
    LAWS = [{"name": "가나관리법", "level": "법률",
             "articles": [{"조문번호": "7", "조문가지번호": "", "조문제목": "신고", "조문내용": "제7조(신고) 법률 본문"}]},
            {"name": "가나관리법 시행령", "level": "시행령",
             "articles": [{"조문번호": "7", "조문가지번호": "", "조문제목": "신청", "조문내용": "제7조(신청) ① 영 본문"}]},
            {"name": "가나관리법 시행규칙", "level": "시행규칙",
             "articles": [{"조문번호": "12", "조문가지번호": "", "조문제목": "검사",
                           "조문내용": "제12조(검사) ① 다음 날부터 6개월 이내\n1. 「다라안전법」 제9조에 따른 완공검사에 합격한 날"}]}]

    def test_bare_level_name_uses_previous_law(self):
        got = main.verify_citations("「가나관리법 시행령」 제7조제1항에 따라 신청한다. 이후 「시행령」 제7조제3항에 따라 발급한다.", self.LAWS)
        self.assertEqual([(c["ok"], c["law"]) for c in got], [(True, "가나관리법 시행령")] * 2)

    def test_other_law_quoted_by_collected_article(self):
        got = main.verify_citations("「가나관리법 시행규칙」 제12조제1항에 따르면 「다라안전법」 제9조에 따른 완공검사일이 기준이다.", self.LAWS)
        self.assertTrue(got[1]["ok"] and got[1].get("ref"))
        self.assertIn("제12조", got[1]["title"])

    def test_other_law_not_quoted_stays_unverified(self):
        for cite in ("「다라안전법」 제10조", "「다라안전법」 제9조의2", "「다라안전법 시행령」 제9조"):
            got = main.verify_citations(cite + "에 따른다.", self.LAWS)
            self.assertFalse(got[0]["ok"], cite)


class ConclusionFix(unittest.TestCase):
    BODY = ("【근거】\n별표 8 | 주거지역 공사장 주간 65dB(A) 이하\n\n"
            "【계산】\n- 보정 후 규제기준: 65 + 5 = 70dB(A) 이하\n- 판정: 68dB(A)는 70dB(A) 이하이므로 규제기준을 초과하지 않음")

    def test_conflict_gone_after_conclusion_only_rewrite(self):
        text = "【결론】\n생활소음 규제기준을 초과한 것이 맞습니다.\n\n" + self.BODY
        self.assertTrue(main._verdict_conflict(text))
        keep = ai_client._call
        ai_client._call = lambda *a, **k: "【결론】\n규제기준을 초과하지 않습니다. 만약 초과라면 조치명령을 받습니다.\n\n【근거】\n덧붙인 블록"
        try:
            new = ai_client.fix_conclusion("초과한 건가요?", main._CONCL_BLOCK_RE.sub("", text, 1).strip())
        finally:
            ai_client._call = keep
        self.assertNotIn("덧붙인", new)
        merged = main._merge_conclusion(text, new)
        self.assertFalse(main._verdict_conflict(merged))
        self.assertIn("판정: 68dB(A)는 70dB(A) 이하", merged)

    def test_llm_failure_keeps_answer(self):
        keep = ai_client._call

        def boom(*a, **k):
            raise ai_client.AiError("x")
        ai_client._call = boom
        try:
            self.assertEqual(ai_client.fix_conclusion("q", self.BODY), "")
        finally:
            ai_client._call = keep
        self.assertEqual(main._merge_conclusion("【결론】\n네\n\n" + self.BODY, ""), "")


class LocalServerAddress(unittest.TestCase):
    def test_localhost_becomes_ipv4(self):
        self.assertEqual(ai_client._ipv4_local("http://localhost:8080/v1"), "http://127.0.0.1:8080/v1")
        self.assertEqual(ai_client._ipv4_local("http://localhost/v1"), "http://127.0.0.1/v1")
        for same in ("http://localhost.example.com/v1", "http://mylocalhost:8080/v1", "http://192.168.0.5:8080/v1"):
            self.assertEqual(ai_client._ipv4_local(same), same)

    def test_all_local_calls_share_one_client(self):
        import inspect
        import re
        import kv_reuse
        for mod in (ai_client, kv_reuse):
            self.assertFalse(re.search(r"(?m)^[^#\n]*httpx\.(get|post)\(", inspect.getsource(mod)), mod.__name__)


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


class FollowUp(unittest.TestCase):
    PREV = ("어느 법의 규정을 찾으시나요?: 토양환경보전법 / 저장시설은 어디에 있습니까?: 지하매설 / "
            "오염 이력이 있습니까?: 모름 / 추가 설명: 제조업 공장")

    def test_known_conditions_carried_unknown_and_law_pick_dropped(self):
        self.assertEqual(main._prev_conditions(self.PREV), "저장시설은 어디에 있습니까?: 지하매설 / 추가 설명: 제조업 공장")
        self.assertEqual(main._prev_conditions(""), "")

    def test_carried_question_not_asked_again(self):
        already = main._asked_questions("앞 질문: x / " + main._prev_conditions(self.PREV))
        self.assertTrue(main._is_repeat_question("저장시설은 어디에 있습니까?", already))
        self.assertFalse(main._is_repeat_question("오염 이력이 있습니까?", already))

    def test_request_and_screen_pass_prev_answered(self):
        self.assertEqual(main.AskRequest(question="q").prev_answered, "")
        html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        for word in ("prev_answered", "function looksLikeFollowUp", "autoFollow(base)"):
            self.assertIn(word, html)


class ClarifyRepeats(unittest.TestCase):
    def test_same_round_duplicate_dropped(self):
        asks = [{"question": "누출검사대상시설로 인정받았습니까?"},
                {"question": "누출검사대상시설로 인정받지 못했거나 모름입니까?"},
                {"question": "저장 용량은 얼마입니까?"}]
        self.assertEqual([a["question"] for a in main._drop_repeats(asks, "")],
                         ["누출검사대상시설로 인정받았습니까?", "저장 용량은 얼마입니까?"])

    def test_already_answered_dropped(self):
        self.assertEqual(main._drop_repeats([{"question": "저장 용량은 얼마입니까?"}], "저장 용량은 얼마입니까?: 모름"), [])


class VerdictHeadingLine(unittest.TestCase):
    """11-17 v1.58 소음 5시간: 【계산】 끝에 '※ 오류 수정 및 재판정' 제목 줄이 붙어 결론 모순을 놓쳤음."""
    TEXT = ("【결론】\n생활소음 규제기준을 초과했습니다.\n\n【계산】\n- 적용 규제기준: 70dB(A) 이하\n"
            "- 판정: 68dB(A)는 70dB(A) 이하이므로 초과하지 않습니다.\n\n※ **오류 수정 및 재판정**\n"
            "위 계산 과정에서 질문의 전제와 조문의 적용을 다시 확인합니다.")

    def test_conflict_found_past_heading_line(self):
        self.assertTrue(main._verdict_conflict(self.TEXT))

    def test_self_correction_trace_found(self):
        for t in ("※ **오류 수정 및 재판정**", "'보정' 항목을 다시 확인합니다.", "제가 조문 해석을 잘못했는지"):
            self.assertTrue(main._SELF_CORR_RE.search(t), t)
        self.assertFalse(main._SELF_CORR_RE.search("근거 조문 원문으로 확인하세요. 측정값은 68dB(A)입니다."))


class AnswerFirst(unittest.TestCase):
    def test_enumerated_choices_can_be_answered_by_cases(self):
        self.assertFalse(main._needs_value({"options": ["예", "아니오", "모름"]}))
        self.assertFalse(main._needs_value({"options": ["지하매설", "지상·옥내", "모름"]}))

    def test_value_questions_must_be_asked_first(self):
        for opts in (["날짜 입력", "모름"], ["2020년 3월", "모름"], ["모름"], []):
            self.assertTrue(main._needs_value({"options": opts}), opts)

    def _run(self, answers):
        calls = []
        ask_after = [{"question": "등록대상동물입니까?", "options": ["예", "아니오", "모름"]}]

        def fake(base, payload, timeout=600.0):
            calls.append(payload)
            if len(calls) == 1:
                return {"answer": "【결론】\n경우별 답", "clarify_after": ask_after, "round": 1, "answered": ""}
            return {"answer": "【결론】\n좁힌 답"}
        keep = eval_run.ask
        eval_run.ask = fake
        try:
            data = eval_run.run_case("http://x", {"question": "q", "answers": answers, "default": "모름"}, verbose=False)
        finally:
            eval_run.ask = keep
        return calls, data

    def test_eval_picks_known_answer_under_first_answer(self):
        calls, data = self._run({"등록대상동물": "예"})
        self.assertEqual(len(calls), 2)
        self.assertIn("등록대상동물입니까?: 예", calls[1]["answered"])
        self.assertEqual((data["answer"], data["_after_rounds"]), ("【결론】\n좁힌 답", 1))

    def test_eval_keeps_first_answer_when_nothing_known(self):
        calls, data = self._run({})
        self.assertEqual(len(calls), 1)
        self.assertEqual(data["_rounds"], [])
        self.assertEqual(data["_clarify_items"][0]["question"], "등록대상동물입니까?")

    def test_screen_and_result_carry_questions_under_answer(self):
        html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        for word in ('id="clarifyAfter"', "data.clarify_after", "cq-need"):
            self.assertIn(word, html)
        self.assertIn("**_pending", open(os.path.join(HERE, "main.py"), encoding="utf-8").read())


class LongAppendix(unittest.TestCase):
    """별표가 6,000자를 넘어도 뒤쪽 행과 비고를 근거로 쓸 수 있어야 합니다(v1.60)."""

    def _appendix(self):
        import xml.etree.ElementTree as ET
        rows = "".join(f"{i}. 시설{i} 시간당 {i}세제곱미터 이상\n" for i in range(1, 700))
        body = rows + "700. 세차시설 시간당 1세제곱미터 이상\n비고\n1. 세차시설은 자동식만 해당한다."
        xml = ("<법령><조문><조문단위><조문번호>1</조문번호><조문내용>제1조(목적) 본문</조문내용></조문단위></조문>"
               "<별표><별표단위><별표번호>0004</별표번호><별표구분>별표</별표구분><별표제목>폐수배출시설</별표제목>"
               f"<별표내용><![CDATA[{body}]]></별표내용></별표단위></별표></법령>")
        return law_client._extract_articles(ET.fromstring(xml))[-1]

    def test_full_text_kept_beside_display_text(self):
        bp = self._appendix()
        self.assertEqual(len(bp["조문내용"]), law_client.BP_SHOW_CHARS)
        self.assertIn("자동식만", bp["별표전문"])

    def test_late_row_and_notes_reach_context(self):
        bp = self._appendix()
        kw = main._bp_keywords("세차시설 폐수배출시설 신고 대상인가요?")
        cut = main._fit_keep_notes(main._compact_bp(bp["조문내용"]), main.BP_CTX_CHARS, kw)
        full = main._fit_keep_notes(main._compact_bp(bp["별표전문"]), main.BP_CTX_CHARS, kw)
        self.assertNotIn("700. 세차시설", cut)
        self.assertIn("700. 세차시설", full)
        self.assertIn("자동식만", full)
        self.assertLessEqual(len(full), main.BP_CTX_CHARS + 60)

    def test_short_appendix_has_no_extra_copy(self):
        import xml.etree.ElementTree as ET
        xml = ("<법령><별표><별표단위><별표번호>0001</별표번호><별표제목>짧은 표</별표제목>"
               "<별표내용><![CDATA[1. 가 10 이하]]></별표내용></별표단위></별표></법령>")
        self.assertNotIn("별표전문", law_client._extract_articles(ET.fromstring(xml))[-1])

    def test_full_text_not_sent_to_screen_and_cache_refetches(self):
        bp = self._appendix()
        pub = main._public_result({"answer": "a", "laws": [{"name": "x", "articles": [bp]}]})
        self.assertNotIn("별표전문", pub["laws"][0]["articles"][0])
        self.assertIn("별표전문", bp)
        import law_cache
        self.assertTrue(law_cache._path("eflaw", "1", "2", "20260101").endswith("_b2.json"))


class AnswerCacheKey(unittest.TestCase):
    def test_follow_up_conditions_change_the_key(self):
        a = main.AskRequest(question="그럼 과태료는?", prev_question="앞 질문")
        b = main.AskRequest(question="그럼 과태료는?", prev_question="앞 질문", prev_answered="지하매설입니까?: 예")
        self.assertNotEqual(answer_cache._qkey(a), answer_cache._qkey(b))


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


class SlimReport(unittest.TestCase):
    def test_keeps_context_and_cited_only(self):
        data = {"debug": {"context_keys": ["가법 제3조", "가법 시행규칙 [별표 8] 기준(제20조 관련)"]},
                "cited_keys": ["가법|5|"],
                "laws": [{"name": "가법", "id": "1", "delegated": {"x": 1}, "articles": [
                             {"조문번호": "3", "조문내용": "a"}, {"조문번호": "4", "조문내용": "b"},
                             {"조문번호": "5", "조문내용": "c"}, {"조문번호": "3", "조문가지번호": "2", "조문내용": "d"}]},
                         {"name": "가법 시행규칙", "articles": [
                             {"조문번호": "", "조문제목": "[별표 8] 기준(제20조 관련)", "조문내용": "표"},
                             {"조문번호": "", "조문제목": "[별표 9] 다른 것", "조문내용": "표"}]}]}
        got = eval_run._slim_laws(data)
        self.assertEqual([a["조문내용"] for a in got[0]["articles"]], ["a", "c"])
        self.assertEqual(got[0]["articles_total"], 4)
        self.assertNotIn("delegated", got[0])
        self.assertEqual([a["조문제목"][:6] for a in got[1]["articles"]], ["[별표 8]"])


class ScreenFixes(unittest.TestCase):
    """v1.64 — 화면: 답이 기댄 별표를 펼쳐 보이게, 버전 표시가 늘 빨갛던 것."""

    def test_cited_appendix_keys(self):
        laws = [{"name": "소음ㆍ진동관리법 시행규칙"}, {"name": "토양환경보전법 시행규칙"}]
        t = ("【근거】\n「소음ㆍ진동관리법 시행규칙」 별표 8 | 주거지역 공사장 주간 **65dB** 이하\n"
             "「토양환경보전법 시행규칙」 [별표 3의2] | 권장 기준\n「없는법 시행규칙」 별표 1 | x")
        self.assertEqual(main._bp_cited_keys(t, laws),
                         ["소음ㆍ진동관리법 시행규칙|별표 8", "토양환경보전법 시행규칙|별표 3의2"])

    def test_pdf_keeps_only_cited_articles(self):
        import pdf_maker
        law = {"name": "가법 시행규칙", "articles": [
            {"조문번호": "20", "조문내용": "제20조 본문"}, {"조문번호": "21", "조문내용": "제21조 본문"},
            {"조문번호": "", "조문제목": "[별표 8] 기준", "조문내용": "표"},
            {"조문번호": "", "조문제목": "[별표 3의2] 다른 표", "조문내용": "표"}]}
        self.assertEqual([pdf_maker.cite_key(law, a) for a in law["articles"]],
                         ["가법 시행규칙|20|", "가법 시행규칙|21|", "가법 시행규칙|별표 8", "가법 시행규칙|별표 3의2"])
        html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        self.assertIn("cited_keys: data.cited_keys", html)
        self.assertIn("cited_keys", main.PdfRequest.model_fields)

    def test_ui_version_read_from_page(self):
        html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        self.assertTrue(main._ui_version())
        self.assertIn(f'const UI_VERSION = "{main._ui_version()}"', html)
        self.assertIn("d.ui && d.ui !== UI_VERSION", html)
        self.assertIn("function citeKey", html)


class ConclusionLast(unittest.TestCase):
    """v1.63 — 결론을 맨 끝에 쓰게 하고 화면에는 맨 위로 옮깁니다."""

    def test_moved_to_front_and_body_kept(self):
        t = "【적용 조건】\n- 시설: x\n\n【계산】\n판정: 초과하지 않음\n\n【설명】\n1. z\n\n【결론】\n규제기준을 초과하지 않습니다."
        got = ai_client.conclusion_first(t)
        self.assertTrue(got.startswith("【결론】\n규제기준을 초과하지 않습니다."))
        for part in ("【적용 조건】\n- 시설: x", "【계산】\n판정: 초과하지 않음", "【설명】\n1. z"):
            self.assertIn(part, got)
        self.assertEqual(got.count("【결론】"), 1)

    def test_already_first_or_missing_unchanged(self):
        for t in ("【결론】\n앞\n\n【근거】\nx", "결론 없는 답"):
            self.assertEqual(ai_client.conclusion_first(t), t)

    def test_old_order_prompt_unchanged_and_new_order_puts_conclusion_last(self):
        keep = ai_client.ANSWER_CONCL_LAST
        try:
            ai_client.ANSWER_CONCL_LAST = False
            old = ai_client.answer_prompt("Q", "C")
            ai_client.ANSWER_CONCL_LAST = True
            new = ai_client.answer_prompt("Q", "C")
        finally:
            ai_client.ANSWER_CONCL_LAST = keep
        head = lambda p: p[p.index("━━ 답변 형식"):]
        self.assertLess(head(old).index("【결론】"), head(old).index("【근거】"))
        self.assertGreater(head(new).rindex("【결론】\n질문에"), head(new).index("【설명】"))
        self.assertNotIn("{", old.replace("{question}", "").replace("{context}", "")[:50])


class HierarchyLookupCache(unittest.TestCase):
    """법령 체계도 검색(_stmd_rows)을 한 질문 안에서 여러 번 부르던 것을 10분 기억합니다(v1.62)."""

    def test_same_name_hits_api_once_and_errors_not_cached(self):
        calls = []
        keep = law_client.search

        def fake(target, q, *a, **k):
            calls.append(q)
            if q.startswith("오류"):
                raise law_client.LawApiError("x")
            return [{"법령명": q}] if q.startswith("있는") else []
        law_client.search = fake
        try:
            for nm in ("있는법zz", "없는법zz", "오류법zz"):
                main._SEARCH_CACHE.pop(("stmd", nm), None)
            self.assertEqual(main._stmd_rows("있는법zz"), [{"법령명": "있는법zz"}])
            self.assertEqual(main._stmd_rows("있는법zz"), [{"법령명": "있는법zz"}])
            self.assertEqual(calls.count("있는법zz"), 1)
            main._stmd_rows("없는법zz")
            n = len(calls)
            main._stmd_rows("없는법zz")
            self.assertEqual(len(calls), n)
            errs = []
            main._stmd_rows("오류법zz", errs)
            main._stmd_rows("오류법zz", errs)
            self.assertEqual(calls.count("오류법zz"), 2)
            self.assertEqual(len(errs), 2)
        finally:
            law_client.search = keep
            for nm in ("있는법zz", "없는법zz", "오류법zz"):
                main._SEARCH_CACHE.pop(("stmd", nm), None)


class ConclusionDates(unittest.TestCase):
    """11-17 v1.60 누출검사: 다시 쓴 답에서 【결론】만 가져와 결론 날짜가 계산과 달라졌음."""
    BODY = ("【적용 조건】\n- 설치일: 2010년 3월 15일\n- 첫 누출검사: 2020년 8월 20일\n\n"
            "【계산】\n기준일 2020년 3월 15일 + 8년 = 다음 검사 기준일 **2028년 3월 15일**\n= 2028년 6월 13일까지")

    def test_date_missing_from_body_is_flagged(self):
        self.assertEqual(main._concl_date_mismatch("【결론】\n**2028년 8월 20일** 이후 90일 이내\n\n" + self.BODY), "2028-08-20")
        self.assertTrue(main._concl_conflict("【결론】\n2028년 8월 20일 이후\n\n" + self.BODY))

    def test_matching_or_dateless_conclusion_passes(self):
        self.assertEqual(main._concl_date_mismatch("【결론】\n2028년 3월 15일부터 2028.6.13까지\n\n" + self.BODY), "")
        self.assertEqual(main._concl_date_mismatch("【결론】\n2028년 8월 20일\n\n【설명】\n날짜 없음"), "")


class NotesOfMatchingTable(unittest.TestCase):
    """별표에 표·비고가 둘(소음·진동)이면 질문에 맞는 표의 비고를 넣어야 합니다(v1.61, 11-17 소음 일요일)."""
    TEXT = ("1. 생활소음 규제기준\n" + "\n".join(f"가. 주거지역 공사장 주간 {60 + i} 이하 행{i}" for i in range(12))
            + "\n비고\n1. 소음 측정 기준\n7. 공사장의 규제기준 중 주거지역은 공휴일에만 -5㏈을 보정한다.\n"
            + "2. 생활진동 규제기준\n" + "\n".join(f"가. 기타 지역 심야 진동 {50 + i} 이하 행{i}" for i in range(12))
            + "\n비고\n1. 진동 측정 기준\n4. 진동 작업시간 2시간 초과 4시간 이하 +5dB")

    def test_noise_question_gets_noise_notes(self):
        kw = main._bp_keywords("주거지역 공사장 일요일 주간 소음 초과인가요?")
        out = main._fit_keep_notes(self.TEXT * 3, 900, kw)
        self.assertIn("공휴일에만 -5", out)


class SecretValues(unittest.TestCase):
    """.env 의 실제 값(OC·옛 Gemini 키 등)이 새로 커밋되는 줄에 들어가면 막고, 값은 출력하지 않습니다(v1.60)."""

    def test_values_from_env_and_backups_by_name(self):
        import tempfile
        import check_secrets as cs
        d = tempfile.mkdtemp()
        keep = cs.HERE
        cs.HERE = d
        try:
            with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
                f.write("LAW_OC=realoc123\nAPP_PASSWORD='hunter22x'\nLOCAL_MODEL=Qwen3.6-35B\nLAW_OC2=발급받은_인증키\n")
            os.makedirs(os.path.join(d, "_env_backup"))
            with open(os.path.join(d, "_env_backup", ".env_old"), "w", encoding="utf-8") as f:
                f.write("GEMINI_API_KEY=oldgeminikey99\n")
            s = cs.env_secrets()
        finally:
            cs.HERE = keep
        self.assertEqual(sorted(s), ["APP_PASSWORD", "GEMINI_API_KEY", "LAW_OC"])
        self.assertEqual(cs.leaked("url ?OC=realoc123&x oldgeminikey99", s), ["GEMINI_API_KEY", "LAW_OC"])
        self.assertEqual(cs.leaked("model Qwen3.6-35B", s), [])


class LastFinishPerRequest(unittest.TestCase):
    def test_new_request_starts_clean(self):
        import applog
        keep = applog.emit
        applog.emit = lambda *a, **k: None
        try:
            applog.llm("answer", 10, 5, 0.1, finish="length")
            self.assertEqual(applog.last_finish("answer"), "length")
            applog.begin(("zz-test", "law"), "zz-test", 0)
            self.assertEqual(applog.last_finish("answer"), "")
        finally:
            applog.clear()
            applog._sessions.pop(("zz-test", "law"), None)
            applog.emit = keep


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



class V165FollowUpSearch(unittest.TestCase):
    """v1.65 A1 — 짧은 이어 묻기는 앞 질문을 붙여 지능형 검색 (11-17 [R18])."""

    def test_short_follow_up_gets_prev_question(self):
        q = main._ai_query("그럼 그 검사를 안 받으면 어떻게 돼?", "주유소 지하 저장탱크 누출검사 주기는?")
        self.assertEqual(q, "주유소 지하 저장탱크 누출검사 주기는? 그럼 그 검사를 안 받으면 어떻게 돼?")

    def test_long_or_new_question_unchanged(self):
        long_q = "공장에서 하루 20톤의 폐합성수지를 재활용하려는데 폐기물처리업 허가가 필요한지 알고 싶습니다"
        self.assertEqual(main._ai_query(long_q, "앞 질문"), long_q)
        self.assertEqual(main._ai_query("누출검사 주기는?", ""), "누출검사 주기는?")


class V165VerdictNegation(unittest.TestCase):
    """v1.65 A9 — 판정 낱말에 붙은 부정만 셈."""

    @staticmethod
    def _t(concl, later):
        return f"【결론】 {concl}\n【계산】\n- 판정: {later}\n【설명】 x"

    def test_unrelated_eopseum_is_not_negation(self):
        self.assertEqual(main._verdict_conflict(self._t("신고 대상입니다.", "제외 규정이 없으므로 신고 대상이다")), "")
        self.assertEqual(main._verdict_conflict(self._t("초과 여부와 관계없이 신고 대상입니다.", "신고 대상이다")), "")

    def test_clause_negation_and_principle_exception(self):
        # 교차 검토(11-20)에서 나온 꼴 — 둘 다 부정 / 원칙·예외 한 문장 / '예외 없이'
        self.assertEqual(main._verdict_conflict(self._t("이 시설은 신고 대상에 해당하지 않습니다.", "신고 대상이 아니다")), "")
        self.assertEqual(main._verdict_conflict(self._t(
            "원칙적으로 폐기물처리업 허가가 필요하나, 다만 생활폐기물을 재활용하는 경우라면 허가가 필요하지 않습니다.",
            "따라서 원칙적으로 허가가 필요하다")), "")
        self.assertEqual(main._verdict_conflict(self._t("허가 대상에 해당하여 예외 없이 허가를 받아야 합니다.",
                                                        "허가 대상에 해당")), "")
        self.assertTrue(main._verdict_conflict(self._t("허가가 필요하다고 볼 수 없습니다.", "따라서 허가가 필요하다")))

    def test_real_conflicts_still_caught(self):
        self.assertTrue(main._verdict_conflict(self._t("기준을 초과합니다.", "68dB < 70dB 이므로 초과하지 않음")))
        self.assertTrue(main._verdict_conflict(self._t("허가 대상이 아닙니다.", "업으로 하지 않으므로 허가 대상이다")))
        self.assertTrue(main._verdict_conflict(self._t("규제기준 미초과입니다.", "기준 초과")))
        self.assertTrue(main._verdict_conflict(self._t("허가가 필요 없습니다.", "허가 필요")))


class V165ConclusionNumbers(unittest.TestCase):
    """v1.65 A3 — 결론의 수치가 본문에 없으면 잡음 (11-17 [R18] 300만원 vs 200만원)."""

    def test_amount_mismatch(self):
        t = ("【결론】 300만원 이하의 과태료가 부과됩니다.\n【근거】 제90조 200만원 이하의 과태료\n"
             "【설명】 1차 100만원, 2차 150만원, 3차 200만원")
        self.assertEqual(main._concl_num_mismatch(t), "300만원")

    def test_db_mismatch(self):
        self.assertEqual(main._concl_num_mismatch("【결론】 60dB 이하입니다.\n【계산】 65dB, 70dB > 65dB"), "60dB")

    def test_law_style_numbers(self):
        self.assertEqual(main._concl_num_mismatch("【결론】 2,000만원 이하의 벌금\n【근거】 2천만원 이하의 벌금"), "")
        self.assertEqual(main._concl_num_mismatch("【결론】 1,000㎡ 이상이면 신고\n【근거】 1천제곱미터 이상"), "")
        self.assertEqual(main._concl_num_mismatch("【결론】 5천만원 이하 벌금\n【근거】 3천만원 이하의 벌금"), "5,000만원")
        self.assertEqual(main._cn_kor("1억5천만"), "150000000")
        self.assertEqual(main._cn_kor("3천5백만"), "35000000")

    def test_year_only_fraction_and_question_facts(self):
        self.assertEqual(main._concl_num_mismatch(
            "【결론】 설치일부터 8년이 되는 2028년 중에 검사\n【계산】 2020년 8월 20일 + 8년 = 2028년 8월 20일"), "")
        self.assertEqual(main._concl_num_mismatch("【결론】 과태료를 50% 감경\n【근거】 2분의 1 범위에서 감경"), "")
        self.assertEqual(main._concl_num_mismatch("【결론】 하루 20톤을 처리하므로 허가 대상\n【근거】 제25조 허가",
                                                  "하루 20톤 재활용"), "")
        self.assertEqual(main._concl_num_mismatch("【결론】 3년마다 검사\n【근거】 1) 1년마다 검사"), "3년")

    def test_no_false_alarm(self):
        for t in ("【결론】 합계 25,000리터로 신고 대상입니다.\n【계산】 15,000 + 10,000 = 25,000",
                  "【결론】 68dB(A) 로 초과합니다.\n【계산】 측정 68 dB, 기준 65dB",
                  "【결론】 2028년 8월 20일까지 검사를 받아야 합니다.\n【계산】 2020년 8월 20일 + 8년",
                  "【결론】 300만원 이하 과태료\n【근거】 3,000,000원 이하의 과태료",
                  "【결론】 해당합니다.\n【근거】 제3조"):
            self.assertEqual(main._concl_num_mismatch(t), "", t)


class V165AnswerFirstHint(unittest.TestCase):
    """v1.65 A2 — 먼저 답하고 묻기에서 원칙 → 예외 순서 지시."""

    def test_hint_text(self):
        self.assertIn("원칙", main.ANSWER_FIRST_HINT)
        self.assertIn("다만", main.ANSWER_FIRST_HINT)
        self.assertIn("한쪽만 결론으로 쓰지 않는다", main.ANSWER_FIRST_HINT)

    def test_hint_wired_when_pending(self):
        src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
        self.assertRegex(src, r"if _pending:\s*\n(?:\s*#.*\n)*\s*_hint \+= ANSWER_FIRST_HINT")



class V166FollowUpContext(unittest.TestCase):
    """v1.66 A5 — 이어 묻기 맥락이 라운드마다 쌓이거나 앞앞 결론이 '확인된 조건' 으로 넘어가지 않게."""

    def test_strip_prev_removes_context_and_dupes(self):
        a = ("앞 질문: 누출검사 주기는? / 앞 답변 결론: 8년마다 / 탱크 용량: 30,000리터 / "
             "앞 질문: 누출검사 주기는? / 탱크 용량: 30,000리터 / 추가 설명: 지하매설")
        self.assertEqual(main._strip_prev(a), "탱크 용량: 30,000리터 / 추가 설명: 지하매설")

    def test_prev_note_separator_and_returned_answered(self):
        class R:
            prev_question = "누출검사 주기는? / 주유소"
            prev_conclusion = "8년마다 받습니다 / 단 신고는 별도"
            prev_answered = "지하매설입니까?: 예"
        note = main._make_prev_note(R())
        self.assertEqual(note, "앞 질문: 누출검사 주기는?, 주유소 / 앞 답변 결론: 8년마다 받습니다, 단 신고는 별도"
                               " / 지하매설입니까?: 예")
        # 화면에 돌려줄 answered 에서는 이어 받은 조건도 뺌(다음 라운드에 두 번 들어가지 않게)
        ans = note + " / 용량?: 3만L / 지하매설입니까?: 예"
        self.assertEqual(main._strip_prev(ans, note), "용량?: 3만L")

    def test_prev_conditions_drops_old_conclusion(self):
        pa = ("그 앞 질문: Q0 / 앞 질문: Q1 / 앞 답변 결론: 틀렸을 수 있는 결론 / 탱크 용량: 30,000리터 / "
              "그 앞 질문: Q1 / 설치일: 모름")
        got = main._prev_conditions(pa)
        self.assertEqual(got, "그 앞 질문: Q1 / 탱크 용량: 30,000리터")
        self.assertNotIn("결론", got)


class V166AiHitOrder(unittest.TestCase):
    """v1.66 A6 — 지능형 검색 조문 정렬 코드가 들어 있는지(전체 흐름은 평가로 확인)."""

    def test_wired(self):
        src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
        self.assertIn('"name": "지능형 검색 조문 정렬"', src)
        self.assertIn("else {_law_key(ai_laws[0])}", src)       # 지능형 1위가 약하면(weak_ai) 1위 법을 주 법령으로 보지 않음
        self.assertIn('if f.get("_main")', src)                  # 선별 목록이 넘칠 때 주 법령 끝까지 남김



class V166CatalogTrim(unittest.TestCase):
    """v1.66 B2 — 삭제 조문·별지/서식은 선별 목록에서 뺌."""

    def test_deleted_article(self):
        for t in ("제5조 삭제 <2010. 5. 31.>", "제5조의2 삭제 〈2015.1.20〉", "제12조(정의) 삭제", "제8조 삭제<2019.12.10>"):
            self.assertTrue(main._is_deleted_article({"조문내용": t}), t)
        self.assertFalse(main._is_deleted_article({"조문내용": "제7조(신고) ① 누구든지 삭제된 규정을 근거로 …"}))

    def test_form_article(self):
        self.assertTrue(main._is_form_article({"구분": "별지", "조문제목": "[별지 제1호서식] 신고서"}))
        self.assertFalse(main._is_form_article({"조문제목": "[서식 1] 대장"}))   # 행정규칙 서식은 남김(재포함 경로 없음)
        self.assertFalse(main._is_form_article({"구분": "별표", "조문제목": "[별표 1] 기준"}))


class V166ByeolpyoCache(unittest.TestCase):
    """v1.66 B5e — 같은 법령의 별표 목록은 라운드마다 다시 받지 않음, 빈 결과는 기억 안 함."""

    def test_cached_and_empty_not_cached(self):
        calls = []
        orig = main.law_client.get_byeolpyo

        def fake(target, mid, name):
            calls.append((target, mid, name))
            return [{"no": "1", "body": "x"}] if mid == "A" else []
        main.law_client.get_byeolpyo = fake
        try:
            for k in [k for k in main._SEARCH_CACHE if k and k[0] == "byl"]:
                main._SEARCH_CACHE.pop(k, None)
            main._byeolpyo_cached("law", "A", "가법")
            main._byeolpyo_cached("law", "A", "가법")
            main._byeolpyo_cached("law", "B", "나법")
            main._byeolpyo_cached("law", "B", "나법")
        finally:
            main.law_client.get_byeolpyo = orig
        self.assertEqual(calls.count(("law", "A", "가법")), 1)
        self.assertEqual(calls.count(("law", "B", "나법")), 2)


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=1, warnings="ignore").result
    sys.exit(0 if res.wasSuccessful() else 1)
