# CODE_NOTES — 코드 설명서

> 코드를 고치기 전에 읽는 문서. AI와 사람이 같이 씀.
> - **수정 이력**(무엇을, 언제, 왜, 되돌리기) → `CHANGELOG.md`
> - **설명**(흐름, 함수가 하는 일, 이렇게 만든 이유, 과거 사고) → 이 문서
> - **코드 안 주석** → 바꿀 수 없는 외부 사실만(llama-server·Ollama·Qwen·법제처 API·Windows 동작, 다른 파일과 맞춰야 하는 규칙, `# noqa`). 나머지는 이름·상수·테스트로 표현

## 0. 수정할 때 규칙

1. 고칠 함수의 항목(아래 4장 `### 파일 :: 함수`)과 해당 흐름(1~2장)을 먼저 읽는다.
2. 코드를 고친 뒤 이 문서의 해당 항목을 같이 고친다. 함수를 새로 만들면 항목을 추가하고, 이름을 바꾸면 제목도 바꾼다.
3. 고친 내용과 되돌리는 방법은 `CHANGELOG.md`에 적는다. 코드에는 `★ 버전` 주석을 달지 않는다.
4. `python check_code_notes.py`로 문서와 코드가 어긋나지 않았는지 확인한다(문서에 있는데 코드에 없는 함수 = 실패).
5. 주석만 옮기는 작업은 고치기 전후 `ast.dump`가 같아야 한다(동작 변화 없음 증명).

## 1. 큰 흐름

```
[브라우저 static/index.html] ──HTTPS──> cloudflared 터널 ──> uvicorn main:app :8000 (--reload)
                                                               │
                         /api/ask (스트리밍, 단계별 진행 표시) ─┤
                                                               ├─ 법제처 OPEN API (law_client.py, 원문 캐시 law_cache.py)
                                                               └─ 로컬 LLM llama-server :8080 (ai_client.py → kv_reuse.py)
                                                                    Qwen3.6-35B-A3B Q4, -c 16384, -np 1
실행: _3_start_server.bat → start.bat (LLM 없으면 llm_loop.bat 로 llama-server, uvicorn, 터널)
      _5_restart_llm.bat  : LLM만 다시 켬 (ai_client.restart_local_llm 워치독도 이걸 부름)
      _4_start_watcher.bat: watch_and_test.py — *.py 바뀌면 eval_run.py 실행 → _eval\latest.md, 커밋·푸시 (_eval\COMMIT 파일 = 커밋 요청)
로그: _runs\lawfinder_날짜.log (질문 블록 ▶START~■END, applog.py), llm_server_날짜.log, tunnel_날짜.log
```

## 2. 질문 처리 흐름 (/api/ask)

`api_ask` → `_ask_logged`(로그 블록, 답변 캐시 앞뒤) → `_ask_sync`(검색) → `_ask_after_search`(조문·되묻기·답변)

| # | 단계 | 함수 | LLM stage | 스위치 |
|---|---|---|---|---|
| 1 | 같은 질문 저장본 묻기 | `answer_cache.before/after` | – | flags/answer_cache |
| 2 | 앞 질문에 이어서 묻기 조건 붙이기 | `_ask_sync` 앞부분 | – | – |
| 3 | 질문 유형(규칙) | `intent.classify` | – | – |
| 3a | 조문 직접 조회("○○법 제N조") | `_ask_article` | – | – |
| 4 | 법령 질문인지·섞인 질문이면 법령 부분만 | `ai_client.scope_split` | scope | SCOPE_CHECK, SCOPE_TRIM |
| 5 | 검색어 변환(법령명·용어·측면) | `ai_client.extract_terms` | terms | – |
| 6 | 법제처 지능형 검색(질문 원문) | `law_client.ai_search` | – | – |
| 7 | 측면별 법 고르기 / 주 법령 고르기 | `pick_aspect_laws` / `pick_main_laws` | aspect_laws / main_laws | LAW_PICK |
| 8 | 법령 확정·약칭 복구·체계도(법→령→규칙→위임 행정규칙)·원문 | `law_client.search/resolve_abbrev/get_hierarchy`, `law_cache.get_detail` | – | TREE_MAX_LAWS, FETCH_MAX_LAWS, flags/law_cache |
| 9 | 조문 목록 → LLM 선별 | `ai_client.select_articles` | select | SELECT_ARTICLES, SELECT_MAX |
| 10 | 보강: 지능형 검색 조문, 질문에 나온 조문 보호, 상위·위임·참조 조문, 별표 | `_ask_after_search` 중반, `rag_select.augment/ref_expand`, `ai_ext.merge`, `ctx_guard.cited_in_question` | – | flags/rag_select, rag_refs, rag_ai_ext |
| 11 | 되묻기(최대 CLARIFY_ROUNDS 라운드) | `ai_client.clarify` | clarify | CLARIFY_ROUNDS, CLARIFY_WHY, flags/rag_clarify |
| 12 | 참고 해석례·판례 고르기·본문 확인 | `_case_candidates`(→`ai_client.pick_cases`), `confirm_cases` | cases / case_confirm | RELATED_CASES, CASE_IN_ANSWER |
| 13 | 컨텍스트 조립(토큰 예산, 별표 축약, 조문 되돌려 넣기) | `_ask_after_search` 안 `_assemble`, `_fit_keep_notes`, `ctx_guard` | – | MIN_CTX_BUDGET, ANSWER_RESERVE |
| 14 | 답변 | `ai_client.answer` | answer | MAXTOK_ANSWER, flags/kv_* |
| 15 | 답변 검토 → 문제 있으면 한 번 다시 쓰기 | `review_answer` → `answer_revised` | review / answer | ANSWER_REVIEW |
| 16 | 인용 검증·별표 경고·계산 검산 | `verify_citations`, `_bp_used_as_basis`, `verify_calc` | – | – |
| 17 | 섞인 질문이면 【답하지 않은 부분】 붙이기 | `_scope_note` (`_ask_logged`) | – | SCOPE_TRIM |

되묻기 라운드: 화면이 `round`, `answered`를 붙여 같은 질문을 다시 보냄 → 검색 결과는 `_SEARCH_CACHE`(10분), 섞인 질문의 법령 부분은 `_SCOPE_TRIM`(1시간)에서 재사용.

## 3. 중간 흐름

**LLM 호출 (ai_client.py)**
```
각 단계 함수 → call_json(스키마 있으면 JSON 강제) 또는 _call → _call_local
  _call_local: 잠금(kv_reuse 와 공유, -np 1) → 고정 앞부분 복원(kv_reuse ②) → POST /v1/chat/completions
             → 503 Loading model 이면 대기 / 연결 실패 계속되면 restart_local_llm(워치독)
             → JSON 강제 거절(400/422 형식 오류)이면 그 요청만 텍스트로 재시도
             → _clean_output(사고 과정·코드펜스 제거, 답변이면 강조 유지, 숫자 뒤 공백 붙이기)
answer → /completion 으로 생성 토큰 기억(kv_reuse ①) → review 가 같은 대화에 이어서 검토 (flags/kv_chain_on 일 때만)
```

**되묻기 (ai_client.clarify)**
```
JSON 강제 성공 → asked/known/질문들(basis·changes) → basis 가 [관련 조문]에 없으면 버림 → _finalize_clarify
JSON 실패 → 텍스트 "질문|보기|보기" 파싱 → 여러 보정(_repair_*, _fix_yesno, _expand_slash_options) → _finalize_clarify
_finalize_clarify(마지막 그물): 중복·결론 되묻기(CONCLUSION_Q)·법 고르기 질문·질문에 이미 있는 사실(_known_in_question) 버림, 보기 정리(기타·모름)
```

**검색 (main._ask_sync)**
```
extract_terms → ai_search(질문 원문, 비면 재시도 → 용어로) → 측면 2개 이상이면 측면별 법 → 주 법령 선택
→ 사용자 지정 법령 반영 → 법령명 검증·약칭 복구 → 체계도(get_hierarchy)·조문(get_detail)·위임 → _ask_after_search
```

## 4. 함수별 설명

## ai_client.py

### ai_client.py :: MAXTOK_TERMS

- (정의 바로 위)
  > ★ v1.49 kv_reuse — 프롬프트 캐시(KV) 재사용(이어 쓰기·고정 앞부분 복원)
- (정의 바로 위)
  > ── 출력 토큰 상한 ────────────────────────────────────────────
  > 상한이 없으면 모델이 계속 생성합니다.
  > 단계마다 필요한 분량이 다르므로 따로 잡습니다.
- (`MAXTOK_TERMS   = int(os.getenv("MAXTOK_TERMS",   "420"))    # 두 줄` 줄 끝)
  > 두 줄

### ai_client.py :: MAXTOK_SELECT

- (`MAXTOK_SELECT  = int(os.getenv("MAXTOK_SELECT",  "400"))    # 번호 나열` 줄 끝)
  > 번호 나열

### ai_client.py :: MAXTOK_CLARIFY

- (`MAXTOK_CLARIFY = int(os.getenv("MAXTOK_CLARIFY", "800"))    # 질문 몇 줄` 줄 끝)
  > 질문 몇 줄

### ai_client.py :: MAXTOK_ANSWER

- (`MAXTOK_ANSWER  = int(os.getenv("MAXTOK_ANSWER",  "4000"))   # 근거 + 설명` 줄 끝)
  > 근거 + 설명

### ai_client.py :: SELECT_MAX

- (정의 바로 위)
  > 조문 선별에서 받아들일 최대 개수. 프롬프트도 5~20개를 요구합니다.
  > ★ 2026-09-29 — 예전에는 40개까지 받아서, 답변 프롬프트가 컨텍스트(16,384)를
  >   넘는 원인 중 하나였습니다.

### ai_client.py :: LOCAL_BASE_URL

- (정의 바로 위)
  > ── 로컬 LLM 서버 ─────────────────────────────────────────────

### ai_client.py :: LOCAL_TIMEOUT

- (`LOCAL_TIMEOUT = float(os.getenv("LOCAL_TIMEOUT", "300"))   # 로컬은 느리므로 ` 줄 끝)
  > 로컬은 느리므로 넉넉히

### ai_client.py :: LOCAL_NO_THINK

- (정의 바로 위)
  > 사고 과정(Thinking)을 끌지. 이 프로그램은 형식 준수가 중요하지
  > 추론이 필요한 작업이 아니므로 끄는 편이 훨씬 빠릅니다.

### ai_client.py :: _srv

- (정의 바로 위)
  > 어느 서버인지. 비워두면 주소로 판별합니다 (포트 11434 → ollama, 그 외 → llamacpp).

### ai_client.py :: LLM_DEBUG

- (정의 바로 위)
  > 1 이면 프롬프트와 응답 전문을 _runs/llm_debug_YYYYMMDD.log 에 남깁니다.
  > (콘솔에는 찍지 않습니다 — 콘솔이 읽을 수 없을 만큼 길어졌습니다)

### ai_client.py :: TERM_PROMPT

- (정의 바로 위)
  > --- 프롬프트 --------------------------------------------------------
  > 이 프로그램의 핵심입니다. 여기가 부실하면 AI가 아는 척하며 지어냅니다.

### ai_client.py :: TERM_SCHEMA

- (`"aspects": {"type": "array", "maxItems": 3, "items": {` 앞)
  > v1.38 — 여러 측면을 묻는 질문의 하위 질문(측면별 검색용). 한 가지만 물으면 [].

### ai_client.py :: server_ctx

- (`if now - _ctx_cache.get("fail_t", 0.0) < 30:` 앞)
  > ★ v1.42 (C-20261021-1) — 실패도 30초 기억합니다. LLM 이 꺼져 있으면 부를 때마다 5초씩 기다렸습니다.
  >   되돌리기: 아래 두 줄과 except 의 fail_t 기록 삭제.
- (`_ctx_cache["fail_t"] = now                           # ★ v1.42 (C-2026` 줄 끝)
  > ★ v1.42 (C-20261021-1)

### ai_client.py :: _JSON_MODE

- (정의 바로 위)
  > ── JSON 출력 강제 (v1.31) ───────────────────────────────────
  > ★ 2026-10-07 — 되묻기·선별·검색어 출력이 자유 텍스트라서 형식이 깨질 때마다
  >   정규식으로 고쳐 왔습니다(되묻기 후처리만 약 600줄). llama-server 는
  >   response_format 에 JSON Schema 를 주면 **문법으로 출력 형식을 강제**합니다
  >   (토큰을 고를 때 스키마에 맞지 않는 토큰을 아예 못 고르게 함).
  >   서버가 이 옵션을 거절하면(구버전·다른 서버) 한 번 끄고 예전 텍스트 방식으로
  >   돌아갑니다. 호출하는 쪽은 결과가 None 이면 기존 파서를 씁니다.
  >   LLM_JSON=0 이면 처음부터 끕니다.

### ai_client.py :: _parse_json

- (`m = re.search(r"\{[\s\S]*\}", t)            # 앞뒤에 말이 붙은 경우` 줄 끝)
  > 앞뒤에 말이 붙은 경우

### ai_client.py :: LLM_AUTO_RESTART

- (정의 바로 위)
  > ── v1.33 — 로컬 LLM 자동 재시작 (워치독) ─────────────────────────
  > 2026-10-08 새벽: llama-server 가 메모리 부족으로 두 번 꺼지고, 한 번은 로그 창이 멈춰
  > (콘솔 "빠른 편집" 선택) 응답 없이 매달렸습니다. 사람이 없으면 평가·서비스가 그대로 멈춥니다.
  > 실제 LLM 호출이 LLM_RESTART_GRACE 초 넘게 계속 실패(연결 안 됨·시간 초과)하면
  > lawfinder-llm 창과 llama-server 를 정리하고 _5_restart_llm.bat 으로 다시 켭니다.
  > Windows 에서만 동작합니다. .env 에 LLM_AUTO_RESTART=0 이면 끕니다. 10분에 한 번까지만.

### ai_client.py :: _call_local

- (`n_prompt = count_tokens(prompt) + 32             # 채팅 템플릿이 붙이는 토큰 여유` 줄 끝)
  > 채팅 템플릿이 붙이는 토큰 여유
- (`"think": False,          # ★ 사고 과정 끄기` 줄 끝)
  > ★ 사고 과정 끄기
- (`if LOCAL_SERVER == "llamacpp" and stage in kv_reuse.PREFIX_STAGES:` 앞)
  > ★ v1.49 kv_reuse ② — 되묻기처럼 앞부분 지시문이 매번 같은 단계는 저장해 둔 앞부분 상태를 복원해 이어 읽습니다.
  >   쓸 수 없으면(None) 아래 예전 방식 그대로. 되돌리기: 이 블록 삭제.
- (`with kv_reuse.LOCK:` 앞)
  > ★ v1.49 kv_reuse — 앞부분 복원과 그 호출 사이에 다른 요청이 끼지 않게 모든 LLM 호출이 같은 잠금을 씁니다
  >   (llama-server 는 -np 1 이라 원래 한 번에 하나만 처리 — 처리량은 같음). 원래: 잠금 없이 post
- (`if code == 503 and "loading" in body.lower() and _wait < 24:` 앞)
  > ★ v1.33 — 재시작 직후 모델을 올리는 중(503 "Loading model")이면 5초씩, 최대 2분 기다립니다.
- (`_bl = body.lower()` 앞)
  > 서버가 JSON 형식 강제를 못 받는 경우 — 끄고 같은 요청을 한 번 다시 보냅니다.
  > ★ v1.42 (C-20261021-5) — 400/422 한 번에 **프로세스 전체**에서 영구히 꺼졌습니다.
  >   오류 본문이 형식 강제(response_format·json_schema·grammar) 문제일 때만 영구히 끄고,
  >   그 밖의 400/422 는 이번 요청만 텍스트 방식으로 다시 보냅니다.
  >   원래: 무조건 _JSON_MODE["ok"] = False

### ai_client.py :: _clean_output

- (정의 바로 위)
  > ── 응답 정리 ────────────────────────────────────────────────
- (`t = re.sub(r"<think>[\s\S]*?</think>", "", t, flags=re.I)` 앞)
  > 사고 과정 블록
- (`t = re.sub(r"<think>[\s\S]*$", "", t, flags=re.I)` 앞)
  > 닫히지 않은 <think> 는 그 뒤를 전부 사고 과정으로 봅니다
- (`t = re.sub(r"```[a-zA-Z]*\n?", "", t)` 앞)
  > 코드펜스
- (`t = re.sub(r"\*\*[ \t]*(【[^】\n]{1,12}】)[ \t]*\*\*", r"\1", t)` 앞)
  > ── 마크다운 정리 ────────────────────────────────────────────
  > ★ 2026-08-18 — 여기서 `**굵게**` 를 **무조건 지우고 있었습니다.**
  >   원래 의도는 용어추출·되묻기 단계에서 모델이 `**법령명:**` 처럼 라벨을
  >   감싸 파싱이 깨지는 것을 막는 것이었는데, 이 함수는 **답변에도 똑같이**
  >   적용됩니다. 그래서 프롬프트로 "수치는 굵게, 의무는 밑줄" 을 시키고
  >   화면에도 `**`→<b>, `__`→<u> 변환이 멀쩡히 있는데도, 그 사이에서
  >   마커가 전부 지워져 **강조가 한 번도 화면에 안 나왔습니다.**
  >   (v1.7 에서 "지시가 실제로 안 들어갔다"고 고쳤던 것과 같은 자리인데,
  >    이번엔 지시는 들어갔고 출력 정리 단계에서 지워지고 있었습니다.)
  >
  >   답변인지 아닌지는 【근거】/【설명】/【결론】 머리표로 구분합니다.
  >   용어추출·되묻기·조문선택 응답에는 이 머리표가 없습니다.
  >   ★ 2026-08-19 — 머리표를 굵게 감싸는 일이 잦습니다(`**【결론】**`).
  >     그대로 두면 화면에서 `**`→<b> 가 먼저 돌아 `<b>【결론】</b>` 이 되고,
  >     그 다음 소제목 정규식 `^【…】$` 가 안 걸려 **결론 상자와 모든
  >     소제목이 통째로 사라집니다.** 판정 전에 벗겨냅니다.
- (`t = re.sub(r"^\s*#+\s*", "", t, flags=re.M)        # 제목(#) 만 제거` 앞)
  > 답변 — 강조와 글머리표를 살립니다.
- (`t = re.sub(r"^\s*#+\s*", "", t, flags=re.M)        # 제목(#) 만 제거` 줄 끝)
  > 제목(#) 만 제거
- (`t = re.sub(r"^\s*>+\s*", "", t, flags=re.M)        # 인용부호(>) 제거` 줄 끝)
  > 인용부호(>) 제거
- (`t = re.sub(r"^(\s*)\*\s+", r"\1- ", t, flags=re.M)  # * 글머리표 → -` 줄 끝)
  > * 글머리표 → -
- (`t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)` 앞)
  > 파싱 대상 응답 — 마커가 있으면 형식이 깨지므로 전부 걷어냅니다.
- (`t = t.replace("：", ":").replace("，", ",")` 앞)
  > 전각 콜론·쉼표를 반각으로
- (`t = re.sub(r"(제[ \t]*\d+)[ \t]+(조|항|호|목|장|절|편|관)", r"\1\2", t)` 앞)
  > ★ 숫자 뒤에 끼는 공백을 붙입니다.
- (`t = re.sub(r"(제[ \t]*\d+)[ \t]+(조|항|호|목|장|절|편|관)", r"\1\2", t)` 앞)
  >
  >   읽기만 나쁜 것이 아닙니다. 화면의 조문 링크 정규식이 "제\d+조" 를
  >   찾는데 "제8 조" 는 걸리지 않아, **답변의 하이퍼링크가 전부 사라집니다.**
  >   실제로 그렇게 됐습니다. 서버의 인용 검증 정규식은 공백을 허용해서
  >   한쪽만 동작하고 있었습니다.
  >   ★ 2026-08-19 — `\s` 대신 `[ \t]` 를 씁니다. `\s` 는 줄바꿈도 먹어서
  >     숫자로 끝난 줄이 다음 줄과 통째로 붙었습니다. 실측 사례:
  >         「…시행규칙」 별표 4
  >         일반기준에 따라 매 8년마다 …
  >       → "「…시행규칙」 별표 4일반기준에 따라 매 8년마다 …"
  >     근거 두 줄이 한 줄로 뭉개지고, 별표 번호와 수치가 같은 문장에
  >     들어가 버려서 main.py 의 별표 경고까지 **멀쩡한 답변에** 떴습니다.

### ai_client.py :: _LABEL_LAW

- (정의 바로 위)
  > 라벨 표기 흔들림 대응. 모델마다 "법령명" 을 "법령", "법률명" 등으로 씁니다.

### ai_client.py :: _JUNK_RE

- (정의 바로 위)
  > 프롬프트의 양식 자리표시자나 모델의 사고 과정 조각이 섞여 들어오는 것을 막습니다.
  > 실제로 관측된 예: "용어1", "LawA", "Term3", "용어2 (Wait", "`누출검사`."
- (`r"^(용어|법령명|term|law|keyword|보기|option|item)\s*\d*$"   # 자리표시자` 줄 끝)
  > 자리표시자
- (`r"|^\d+$"                                                   # 숫자만` 줄 끝)
  > 숫자만
- (`r"|^[a-zA-Z\s]+$"                                           # 영어만` 줄 끝)
  > 영어만
- (`r"|wait|but |usually|related|however|note:|example",          # 사고 과정 ` 줄 끝)
  > 사고 과정 조각

### ai_client.py :: _is_junk

- (`if not re.search(r"[가-힣]", s):` 앞)
  > 한글이 하나도 없으면 법령 용어가 아닙니다.

### ai_client.py :: _looks_like_law

- (`if re.search(r"(방법|용법|기법|공법|수법|요법)$", s):` 앞)
  > ★ 2026-08-19 — "…방법/용법/기법" 이 '법' 으로 끝난다는 이유로 법령명으로
  >   승격돼, "누출검사방법" 같은 용어로 lsStmd 를 조회하고 있었습니다.
  >   헛 조회로 끝나긴 하지만 용어 목록에서 빠져 검색 품질이 떨어집니다.
- (`return bool(re.search(r"(법|법률|령|규칙|조례|고시|지침|예규|훈령)$", s))` 앞)
  > ★ 2026-09-29 — "…에 관한 법률" 은 '률' 로 끝나서 법령명으로 안 잡혔습니다.
  >   되묻기 보기에 "광산피해의 방지 및 복구에 관한 법률" 이 그대로 나갔습니다.

### ai_client.py :: SCOPE_PROMPT

- (정의 바로 위)
  > ── v1.37 — 법령과 무관한 질문 거르기 ─────────────────────────────
  > 실사용 2026-10-13: "공무원 시험 합격은 에듀윌이야?" 에 공무원임용시험령을 찾아 답했습니다.
  > 법령으로 답할 수 없는 질문(업체·학원·상품 추천, 공부법, 잡담 등)은 검색 전에 거릅니다.
  > 애매하면 통과(법령 질문으로 봄) — 진짜 법령 질문을 막는 쪽이 더 나쁩니다.

### ai_client.py :: SCOPE_SCHEMA

- (`"law_part": {"type": "string", "maxLength": 300},` 앞)
  > ★ v1.50 — 섞인 질문에서 법령 부분 / 뺀 부분

### ai_client.py :: scope_split

- (`obj, raw = call_json(SCOPE_PROMPT.format(question=str(question or "")[` 앞)
  > v1.50 — law_part 를 적어야 해서 출력 한도 80 → 200

### ai_client.py :: CASE_PICK_PROMPT

- (정의 바로 위)
  > ── v1.38 — 참고 사례 고르기 ─────────────────────────────────────
  > 해석례·판례 후보(제목)를 질문과 같은 쟁점인지 LLM 이 고릅니다. 불용어·점수 규칙을 대체.

### ai_client.py :: CASE_CONFIRM_PROMPT

- (정의 바로 위)
  > ── v1.40 — 참고 해석례 확인 (답변에 넣기 전) ───────────────────────
  > 제목만 보고 고른 사례 중 쟁점이 다른 것(세차장 질문에 '학교환경위생 정화구역')이 섞여, 답변에 넣기 전에
  > 질의요지·회답 본문을 보고 한 번 더 거릅니다.

### ai_client.py :: confirm_cases

- (`return None                 # ★ v1.45 (C-27) 원래: return []` 줄 끝)
  > ★ v1.45 (C-27) 원래: return []

### ai_client.py :: ASPECT_LAW_PROMPT

- (정의 바로 위)
  > ── v1.38c — 측면별 법 고르기 (U-19) ──────────────────────────────
  > 측면마다 후보 법(LLM 추측 + 그 측면 지능형 검색 상위)을 보여 주고 가장 직접 적용되는 법 하나를 고르게 합니다.
  > (추측만 믿으면 '형사 → 형법'(조문 0개), 검색 1위만 믿으면 '형사 → 아동ㆍ청소년성보호법'(미성년자 없음)이 붙었음)

### ai_client.py :: MAIN_LAW_PROMPT

- (정의 바로 위)
  > ── v1.40 — 주 법령 고르기 (L-1751-1·1751-3, L-1016-1, U-8) ─────────────────────
  > 검색어 변환 LLM 의 법령 추측과 지능형 검색 순위가 엇갈릴 때, 예전에는 규칙(지능형 1위 앞세우기·
  > 신뢰 낮음 비율·'어느 법?' 되묻기)으로 정했습니다. 지방공무원 육아휴직에 남녀고용평등법(민간 근로자 법)이
  > 1위로 오거나, 외부강의 사례금에 청탁금지법이 3위라 빠지는 일이 생겨, 두 목록을 합쳐 LLM 이 고르게 합니다.

### ai_client.py :: extract_terms

- (`obj, _ = call_json(TERM_PROMPT.format(question=question, output_rules=` 앞)
  > ★ v1.31 — JSON 형식 강제를 먼저 시도합니다. 안 되면 예전 텍스트 방식.
- (`for a in (obj.get("aspects") or [])[:3]:` 앞)
  > v1.38 — 측면별 하위 질문(2개 이상일 때만 의미가 있음)
- (`for line in raw.splitlines():` 앞)
  > 1) 라벨이 붙은 줄
- (`if not out["법령명"] and not out["용어"]:` 앞)
  > 2) 라벨이 하나도 없으면 줄 단위로 추정합니다.
- (`if not out["법령명"]:` 앞)
  > 3) 용어만 나왔는데 그중 법령처럼 생긴 게 있으면 옮깁니다.
- (`bad = [x for x in out["법령명"] if not _looks_like_law(x)]` 앞)
  > 법령명 자리에 법령처럼 생기지 않은 것이 들어왔으면 용어로 내립니다.

### ai_client.py :: extract_terms.add

- (`for item in re.split(r"[,、/]|\s{2,}", chunk):` 앞)
  > ★ 2026-09-29 — 가운뎃점(·)으로는 나누지 않습니다. "소음·진동관리법" 이
  >   "소음" / "진동관리법" 으로 쪼개져 엉뚱한 법을 찾았습니다.
- (`item = re.sub(r"^\d+[.)]\s*", "", item)      # "1. " 같은 번호 제거` 줄 끝)
  > "1. " 같은 번호 제거
- (`item = re.sub(r"\s*\(.*$", "", item)          # "용어2 (Wait" 같은 꼬리 제거` 줄 끝)
  > "용어2 (Wait" 같은 꼬리 제거

### ai_client.py :: answer

- (`if LOCAL_SERVER == "llamacpp" and kv_reuse.enabled():` 앞)
  > ★ v1.49 kv_reuse ① — /completion 으로 불러 생성 토큰을 기억해 두고(답변 검토가 이어서 씀), 고정 지시문은 ② 로 복원.
  >   쓸 수 없으면 None → 아래 예전 방식. 되돌리기: 이 블록 삭제.

### ai_client.py :: REVIEW_PROMPT

- (정의 바로 위)
  > ── v1.40 — 답변 검토 (U-20·22·23, L-2244-1·3, U-7) ─────────────────────
  > 답변 지시문에 규칙을 더 붙여도 로컬 모델이 긴 지시문을 다 지키지 못합니다(지시문이 이미 200줄).
  > 대신 다 쓴 답변을 **한 번 더 읽혀** 네 가지 오류만 찾게 하고, 오류마다 원문·질문·답변에서 그대로 복사한
  > 근거 글자를 붙이게 합니다. 코드가 그 글자가 실제로 있는지 확인해(검토자가 지어낸 지적은 버림),
  > 남은 지적이 있으면 그 지적을 붙여 답변을 한 번 다시 씁니다.

### ai_client.py :: REVIEW_SCHEMA

- (`"claims": {"type": "array", "maxItems": 6, "items": {  # ★ v1.43 (L-05` 줄 끝)
  > ★ v1.43 (L-0552-3②) 4 → 6

### ai_client.py :: REVIEW_CHAIN_PROMPT

- (정의 바로 위)
  > ★ v1.49 kv_reuse ① — 답변에 **이어서** 보내는 검토 지시(위 REVIEW_PROMPT 와 같은 내용, 질문·조문·답변은 이미 대화 안에 있음)

### ai_client.py :: MAXTOK_REVIEW

- (`MAXTOK_REVIEW = int(os.getenv("MAXTOK_REVIEW", "1100"))   # ★ v1.43 주장` 줄 끝)
  > ★ v1.43 주장 6개라 800 → 1100

### ai_client.py :: REVIEW_STATS

- (`REVIEW_STATS = {"claims": 0, "dropped": 0}      # 마지막 검토의 주장 수·근거 못 찾아` 줄 끝)
  > 마지막 검토의 주장 수·근거 못 찾아 버린 지적 수(처리 과정 표시용)

### ai_client.py :: review_answer

- (`REVIEW_STATS["ok"] = False      # ★ v1.45 (C-20261028-14) 검토 호출 성공 여부(` 줄 끝)
  > ★ v1.45 (C-20261028-14) 검토 호출 성공 여부(실패를 '문제 없음' 으로 보이지 않게)
- (`if json_enabled() and LOCAL_SERVER == "llamacpp":` 앞)
  > ★ v1.49 kv_reuse ① — 방금 쓴 답변에 이어서 검토 지시만 보냅니다(조문·질문·답변 약 8~10k 토큰을 다시 읽지 않음).
  >   쓸 수 없으면 아래 예전 방식. 되돌리기: 이 블록 삭제.
- (`out.append({"kind": "원문없음", "quote": claim, "fix": fix})` 앞)
  > 수치·기한 주장인데 원문에 근거가 없으면 지어낸 것일 가능성이 큼

### ai_client.py :: CLARIFY_SCHEMA

- (`"asked": {"type": "string", "maxLength": 100},` 앞)
  > ★ v1.32 — 질문에 이미 있는 사실을 **먼저** 적게 합니다(구조화된 출력 안의 짧은 추론).
  >   실제 사례: "평일 오후 3시 · 하루 2시간 · 주거지역" 이라고 적힌 질문에
  >   측정 시각·사용 시간·지역을 3라운드에 걸쳐 되물었고, 답변까지 꼬였습니다.
  > ★ v1.40 — 질문자가 알고 싶은 것(asked)을 먼저 적고, 되묻기마다 근거 조문 문구(basis)와
  >   '그 답에 따라 asked 의 답이 어떻게 달라지는지'(changes)를 적게 합니다. basis 가 [관련 조문]에
  >   실제로 없으면 코드가 그 질문을 버립니다(조문에 없는 성별·신청 기간 같은 되묻기 — L-1751-1).

### ai_client.py :: _Q_TIME_OF_DAY

- (정의 바로 위)
  > ── v1.32 — 질문 원문에 이미 있는 사실을 되묻는 항목 거르기 (마지막 그물) ──────────
  >   프롬프트·known 으로 1차로 막고, 그래도 나오면 여기서 버립니다. 표면 신호만 봅니다.

### ai_client.py :: _known_in_question

- (`if re.search(r"(주택|주거)", ask) and re.search(r"(주거\s*지역|아파트|주택가|주택\s*단지` 앞)
  > "주거지역·아파트 공사장" 인데 "300m 안에 주택이 있습니까?" — 지시문에 예로 넣어도 계속 물어서 코드로 막습니다.
- (`if re.search(r"(언제|날짜|일자|연월일|시점)", ask):` 앞)
  > 날짜: 질문에 "2020년 8월 20일에 … 검사" 처럼 그 일의 날짜가 이미 있으면 묻지 않습니다.
- (`kws = [k for k in ("완공", "착공", "준공", "취득", "등록", "허가", "신고", "설치", "사용` 앞)
  > 가장 구체적인 낱말 하나로 맞춥니다("완공검사일" 은 "완공" 으로 — "검사" 로 맞추면 엉뚱한 날짜에 걸림).
- (`hit = _option_in_question(q, item.get("options", []))` 앞)
  > ★ v1.35 — 보기 가운데 하나가 질문에 이미 나와 있으면(같은 말·같은 뜻) 묻지 않습니다.
  >   (2026-10-10 실사용: "땅을 파다가 기름이 나왔어" 인데
  >    "어떤 물질로 오염되었습니까? [석유류 / 유기용제류 / 기타 / 모름]" 을 물음)

### ai_client.py :: _OPT_SYNONYMS

- (정의 바로 위)
  > 보기 낱말 ↔ 질문에서 같은 뜻으로 쓰이는 말. 보기에 왼쪽 말이 들어 있고 질문에 오른쪽 패턴이 있으면 같은 것.

### ai_client.py :: _OPT_SKIP

- (정의 바로 위)
  > 고를 수 있는 내용이 없는 보기(예/아니오·모름·기타·직접 입력)는 비교하지 않습니다.

### ai_client.py :: CONCLUSION_Q

- (정의 바로 위)
  > 되묻기가 "결론"(기한·의무)을 질문자에게 되묻는 꼴. clarify() 마지막 그물에서 버립니다.
  >   "…언제까지 받아야 합니까?" / "…설치해야 합니까?" / "…신고해야 하나요?"
  > 사실을 묻는 "언제 설치했습니까?" "용량은 얼마입니까?" 는 걸리지 않습니다.
- (`r"|(적용|보정|가산|감경|면제)\s*(여부|대상인|되나요|됩니까|되는지)"` 앞)
  > v1.32 — "보정 적용 여부는?", "기준일이 설치일로부터 계산됩니까?" 처럼
  >   조문을 보고 이 도구가 판단할 것을 묻는 꼴

### ai_client.py :: _YESNO_RE

- (정의 바로 위)
  > "…에 해당하나요?" 같은 예/아니오 질문인지 판별합니다.
  > 예/아니오로 답하는 질문의 어미.
  > ★ 2026-08-18 — "지정되었나요?" 처럼 `되었나요`·`했나요`·`인가?` 로 끝나는
  >   물음이 빠져 있어서, 부정 보기가 없는 채로 화면에 나갔습니다
  >   ("시설이 …로 지정되었나요?  ○ 지정됨  ○ 모름" — 아니라고 답할 방법이 없음).

### ai_client.py :: _WH_RE

- (정의 바로 위)
  > 의문사. 이게 있으면 예/아니오로 답하는 질문이 아닙니다.
  > (어미는 같은데 답이 전혀 다릅니다 — "언제 받았나요?" 에 "아니오" 는 답이 아님)

### ai_client.py :: _OPTION_WORDS

- (정의 바로 위)
  > 보기로 흔히 쓰이는 낱말. 질문 자리에 이런 것이 오면 그 줄은 버립니다.

### ai_client.py :: _LABEL_ONLY_RE

- (정의 바로 위)
  > 출력 형식 틀의 라벨. "질문|보기1|보기2" 라는 틀을 로컬 모델이 그대로 베낍니다.

### ai_client.py :: _split_clarify_line

- (`continue                       # "질문" / "보기1" 같은 형식 라벨은 버립니다` 줄 끝)
  > "질문" / "보기1" 같은 형식 라벨은 버립니다
- (`tail = pieces[-1] if pieces else ""` 앞)
  > 조각이 모자랍니다. 마지막 조각이 "질문?: 보기 / 보기 / 모름" 인지 봅니다.
  > ★ 구분자는 "/" 만 씁니다. "·" 로 자르면 "지상·옥내 저장시설" 이 쪼개집니다.
- (`if len(pieces) == 2:` 앞)
  > ★ 2026-08-19 — 콜론 없이 보기를 한 칸에 "/" 로만 몰아넣는 형태.
  >     저장시설 종류|지하매설/지상·옥내/모름
  >   조각이 2개라 3조각 조건에 걸려 **줄째로 버려지고 있었습니다.**

### ai_client.py :: _OPT_LOOKS_LIKE_Q_RE

- (정의 바로 위)
  > 확실한 물음 어미. 보기 자리에 오면 안 됩니다.

### ai_client.py :: _OPT_Q_MARK_RE

- (정의 바로 위)
  > ★ 2026-08-18 — 물음표가 **붙은** 어미. 위 목록에 "인가/인지/는가" 를 그냥
  >   넣으면 "이중벽인지", "2만리터 이상인지" 같은 멀쩡한 보기까지 버려집니다
  >   (예전에 실제로 그랬습니다). 물음표를 요구하면 그 사고 없이
  >   "지상·옥내 저장시설인가?" 만 정확히 걸러집니다.

### ai_client.py :: _Q_TAIL_RE

- (정의 바로 위)
  > 물음 어미를 떼어내 보기 문구로 만들 때 씁니다.

### ai_client.py :: _A_OR_B_RE

- (정의 바로 위)
  > "A인지 B인지" 형태의 질문. 여기서 보기를 뽑아낼 수 있습니다.

### ai_client.py :: _TODO_TAIL_RE

- (정의 바로 위)
  > "…확인이 필요합니다" 처럼 **할 일**로 쓴 꼬리말.
  > 프롬프트에 "할 일을 보기로 만들지 마라" 고 적어 뒀지만 로컬 모델이 지키지
  > 않습니다. 코드에서 걸러야 합니다.

### ai_client.py :: _WISH_TAIL_RE

- (정의 바로 위)
  > "…궁금합니다 / …알고 싶습니다" — 질문이 아니라 하소연입니다.

### ai_client.py :: _A_OR_B_LOOSE_RE

- (정의 바로 위)
  > "A인지 B인지" — 앞의 군말 길이를 제한하지 않는 느슨한 판. 보기를 뽑는 데 씁니다.

### ai_client.py :: _strip_todo

- (`while t != prev:                      # "확인이 필요합니다" + "." 처럼 겹칠 수 있음` 줄 끝)
  > "확인이 필요합니다" + "." 처럼 겹칠 수 있음

### ai_client.py :: _UNKNOWN_OPTS

- (정의 바로 위)
  > "모름" 과 같은 뜻으로 쓰이는 보기. 이건 버리면 안 됩니다.

### ai_client.py :: _is_todo_option

- (`if _is_unknown_option(s):` 앞)
  > ★ 2026-08-19 — "모르겠습니다" 는 _WISH_TAIL_RE 에 걸려 '할 일'로 판정돼
  >   **삭제되고 있었습니다.** 그러면 화면에 "모름" 계열 보기가 하나도 안 남고,
  >   index.html 은 그럴 때 **마지막 보기를 기본 선택**하므로, 사용자가 그냥
  >   "이대로 검색" 을 누르면 모르는 사실을 확정 조건으로 넣게 됩니다.

### ai_client.py :: _clean_question

- (`for sep in (":", "："):` 앞)
  > (1) 새어 들어온 답 잘라내기
- (`cleaned = _strip_todo(q)` 앞)
  > (2) 할 일·하소연 꼬리말 떼기
- (`if len(cleaned) >= 4:                 # 너무 짧아지면 원문을 둡니다` 줄 끝)
  > 너무 짧아지면 원문을 둡니다

### ai_client.py :: _repair_all_questions

- (`others = [o for o in others if not _is_todo_option(o)]` 앞)
  > ★ 2026-08-19 — 물음 어미만 떼면 "면제 대상인지 확인해야 하나요?" 가
  >   "면제 대상인지 확인해야 하" 라는 **잘린 조각**으로 남습니다.
  >   할 일 문장은 여기서 미리 걸러냅니다.

### ai_client.py :: _expand_slash_options

- (`unit_slash = re.compile(r"\d\s*[a-zA-Z가-힣㎥㎡㎎㎍ℓ%]*\s*/")` 앞)
  > 숫자+단위 뒤에 바로 "/" 가 붙는 형태 (㎥/일, mg/L, 톤/년 …)

### ai_client.py :: _fix_yesno

- (`if _WH_RE.search(q):` 앞)
  > ★ 2026-08-19 — 어미만 보면 **의문사 질문**까지 걸립니다.
  >   "마지막 누출검사를 언제 받았나요?" 도 `받았나요?` 로 끝나거든요.
  >   그러면 보기에 "아니오" 가 붙어서
  >       마지막 누출검사를 언제 받았나요?  ○ 아래 칸에 날짜 입력  ○ 아니오
  >   가 됩니다. 사용자가 "아니오" 를 고르면
  >   "마지막 누출검사를 언제 받았나요?: 아니오" 가 확인된 조건으로 답변
  >   프롬프트에 들어가고, 거기서 다음 검사일을 계산하려 듭니다.
  >   하필 이게 우리가 **최우선으로 묻게 만든** 질문이라 더 나쁩니다.
- (`_UNK = ("모름", "모르겠음", "모르겠어요", "잘모름", "확인안됨", "미확인")` 앞)
  > ★ 2026-09-29 — 실질 보기가 **부정 하나뿐**인 경우.
  >   실사례: "악취배출시설이 있습니까?" → [악취배출시설이 없습니다 / 모름]
  >   "있다" 를 고를 수 없었습니다. 부정 문장을 긍정으로 바꿔 앞에 넣습니다.
- (`return opts                      # 부정 보기가 이미 있으면 그대로` 줄 끝)
  > 부정 보기가 이미 있으면 그대로
- (`real = [o for o, f in zip(opts, flat) if f not in UNKNOWN]` 앞)
  > "모름" 을 뺀 실질 보기가 둘 이상이면 대립 선택지로 보고 유지합니다.
- (`if real:` 앞)
  > ★ 2026-08-18 — 실질 보기가 하나뿐일 때 예전에는 통째로
  >   ["예", "아니오", "모름"] 으로 **갈아치웠습니다.** 그래서
  >       저장된 물질이 석유류인가요?  ○ 석유류  ○ 모름
  >   이 이렇게 바뀌었습니다.
  >       저장된 물질이 석유류인가요?  ○ 예  ○ 아니오  ○ 모름
  >   모델이 애써 뽑아 준 "석유류" 라는 구체적인 말이 사라지고, 담당자는
  >   질문을 다시 읽어야 무엇에 "예" 인지 알 수 있습니다. 정보가 줄어듭니다.
  >   (실사용 지적: "두 번째 되묻기에 예 라고 글자가 잘못 들어간다")
  >
  >   → 있는 보기는 그대로 두고 **부정만 채웁니다.**
  >       저장된 물질이 석유류인가요?  ○ 석유류  ○ 아니오  ○ 모름
- (`return ["예", "아니오", "모름"]` 앞)
  > 실질 보기가 아예 없을 때만 예/아니오로 세웁니다.

### ai_client.py :: _finalize_clarify

- (`_why = str(item.get("why") or "")       # ★ v1.41 — 아래에서 item 을 새로 만들므` 줄 끝)
  > ★ v1.41 — 아래에서 item 을 새로 만들므로 미리 보관
  > v1.34 — 한 라운드에 같은 질문이 두 번 나오는 경우(실측)
- (`left = [o for o in left` 앞)
  > 잘린 조각이 남지 않게 너무 짧은 보기도 버립니다.
  > 단 "예"/"네" 는 한 글자여도 정상 보기입니다.
- (`unk = [o for o in left if _is_unknown_option(o)]` 앞)
  > ★ 2026-09-29 — "모름" 계열 보기가 둘 이상이면("모르겠음 / 모름") 하나만 남겨 맨 뒤로.
- (`if not re.search(r"(\?|？|까|요|나|가|지|니|죠)\s*$", item["question"].strip()` 앞)
  > ★ 2026-09-29 — 물음이 아닌 질문("우려기준 초과")은 버립니다.
  >   사용자가 무엇을 골라야 하는지 알 수 없습니다.
- (`q_bare = re.sub(r"[\s?？.]+$", "", item["question"])` 앞)
  > ★ 2026-09-29 — "어느 법이 적용되냐" 를 묻는 항목은 버립니다.
  >   질문자는 법을 모르고, 법을 가리는 건 이 도구의 일입니다.
  >   실제 사례: 질문 "토양환경보전법" / 보기 "광산피해의 … 법률 | 모름"
- (`if CONCLUSION_Q.search(item["question"]):` 앞)
  > ★ 2026-10-02 — 결론(기한·의무)을 질문자에게 되묻는 항목은 버립니다.
  >   실제 사례: 누출검사 주기를 물었는데 되묻기가
  >   "다음 정기 누출검사를 언제까지 받아야 합니까?" — 질문자가 물은 것 그 자체.
- (`q_txt = item["question"]` 앞)
  > ★ v1.34 — 보기 손질 (2026-10-08 실사용 피드백: "환경영향평가를 받아야 하나요?")
  >   · "별표 3 기준 이상/미만" 처럼 조문·별표를 가리키는 보기 → 질문자는 그 기준을 모릅니다.
  >   · "규모는 얼마입니까?" 인데 보기에 숫자가 없음 → 질문과 보기가 안 맞습니다.
  >   → 수치를 묻는 질문이면 직접 입력으로 바꾸고, 아니면 그 질문을 버립니다.
- (`real = [o for o in left if not _is_unknown_option(o)]` 앞)
  > 보기가 범주 목록이면(예/아니오·수치 범위가 아니면) "기타" 를, 그리고 항상 "모름" 을 둡니다.
  >   (실사용: "사업의 종류" 보기에 맞는 것이 없어 아무거나 고른 뒤 답이 엉뚱해짐)
- (`why = _known_in_question(question, {"question": item["question"], "opt` 앞)
  > ★ v1.32 — 질문 원문에 이미 적힌 사실(시각·사용 시간·지역·요일·수치)을 되묻는 항목은 버립니다.

### ai_client.py :: clarify

- (`obj, _ = call_json(CLARIFY_PROMPT.format(output_rules=_CLARIFY_RULES_J` 앞)
  > ★ v1.31 — JSON 형식 강제를 먼저 시도합니다. 성공하면 형식 보정 단계를
  >   건너뛰고 마지막 그물(_finalize_clarify)만 통과시킵니다.
- (`b = _qnorm(it.get("basis") or "")` 앞)
  > v1.40 — 근거 조문 문구가 [관련 조문]에 실제로 있는 되묻기만 남깁니다.
  >   (날짜 입력 칸 질문은 조문 문구가 '설치 후 ○년' 처럼 있으므로 같은 기준을 씁니다)
- (`if _clarify_why_on() and str(it.get("changes") or "").strip():` 앞)
  > ★ v1.41 — 되묻기마다 '왜 묻는지'(changes: 답에 따라 결론이 어떻게 달라지는지)를 화면에 보여 줍니다.
  >   기본 꺼짐(_cache/flags/clarify_why 파일 또는 CLARIFY_WHY=1). 원복: 아래 2줄 삭제.
- (`head = raw.strip().splitlines()[0].strip() if raw.strip() else ""` 앞)
  > "OK" 만 나오면 더 물을 게 없다는 뜻입니다.
  > 로컬 모델은 "OK." "OK 입니다" 처럼 덧붙이기도 합니다.
- (`if not re.search(r"[가-힣]", line):` 앞)
  > ★ 2026-08-19 — 모델이 마크다운 표로 답할 때가 있습니다.
  >   정렬 행("|:---|:---:|---:|")이 그대로 질문·보기로 올라가
  >   화면에 ":---" 가 보기로 뜹니다. 한글이 한 글자도 없으면 버립니다.
- (`parts = _split_clarify_line(line)` 앞)
  > 모델이 형식 틀("질문|보기1|…")의 낱말을 그대로 베끼거나
  > "질문: 보기 / 보기 / 모름" 으로 흘러도 읽어냅니다.
- (`if len(parts) < 3:` 앞)
  > ★ 최소 3조각(질문 + 보기 2개)이어야 합니다.
  >   2개로 완화하면 모델이 질문 없이 "예|아니오|모름" 만 뱉은 줄까지
  >   받아들여 "예" 가 질문이 되어버립니다.
- (`if _looks_like_option(q):` 앞)
  > 첫 조각이 보기 같은 낱말이면 질문이 아닙니다. 그 줄은 버립니다.
- (`q2 = _clean_question(q, opts)` 앞)
  > ★ 질문에 새어 들어온 이전 답변·할 일 꼬리말을 먼저 걷어냅니다.
  >   이걸 안 하면 아래 보정들이 모두 오염된 질문을 기준으로 돕니다.
- (`opts = _expand_slash_options(opts)` 앞)
  > 한 칸에 "/" 로 몰아넣은 보기를 먼저 폅니다.
- (`todo_split = _promote_todo_options(q, opts)` 앞)
  > ★ 보기 자리에 "…확인이 필요합니다" 같은 할 일이 여러 개 들어온 줄은
  >   여러 개의 질문으로 폅니다. 제일 먼저 봅니다 — 이 형태는 아래
  >   보정들이 전부 그냥 통과시켜 버립니다(물음 어미가 없어서).
- (`fixed = _repair_all_questions(q, opts)` 앞)
  > ★ 질문과 보기가 둘 다 물음인 줄("A인가?|B인가?|모름")을 먼저 봅니다.
  >   _repair_a_or_b 보다 앞에 둡니다 — 그쪽은 보기의 질문을 **버리는**
  >   방향이라, 여기서 살릴 수 있는 선택지가 먼저 사라집니다.
- (`opts = _repair_a_or_b(q, opts)` 앞)
  > 보기 자리에 질문이 들어온 줄을 고칩니다.
- (`out = _finalize_clarify(out, question)` 앞)
  > ★ 마지막 그물 — 여기까지 와서도 "…확인이 필요합니다" 가 남아 있으면
  >   화면에 내보내지 않습니다. 고를 수 없는 보기를 보여 주느니
  >   그 질문을 안 묻는 편이 낫습니다.
  >   ★ 2026-08-19 — 예전에는 이 그물이 마지막 분기 안에만 있어서,
  >     `continue` 로 빠져나가는 _promote_todo_options / _repair_all_questions
  >     결과는 **검사를 통째로 건너뛰었습니다.** 루프 밖으로 뺐습니다.

### ai_client.py :: select_articles

- (`cleaned = re.sub(r"제\s*\d+\s*(조|항|호)", " ", raw)` 앞)
  > 숫자만 뽑습니다. 모델이 설명을 덧붙여도 번호는 건집니다.
  > 다만 "제8조" 같은 조문 번호가 섞이지 않도록, 조/항/호 앞뒤 숫자는 제외합니다.
