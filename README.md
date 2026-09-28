# 법령 조회 도우미

법제처 국가법령정보 OPEN API 로 법령을 검색/조회하고, AI 로 질의응답을 도와주는 도구입니다.
실제 서비스: https://law.gwiyomi-lab.com

## AI 모델

이 PC 에서 직접 돌리는 로컬 모델만 씁니다. 질문 내용이 외부로 나가지 않고 사용 한도도 없습니다.

| 항목 | 값 |
|---|---|
| 서버 | llama.cpp `llama-server` (포트 8080, `start.bat` 이 같이 띄움) |
| 모델 | Qwen3.6-35B-A3B (Q4_K_M, MoE — 12GB VRAM 에서 약 55 토큰/초) |
| 컨텍스트 | 16,384 토큰 (`start.bat` 의 `-c`). 코드가 서버에서 읽어 조문 분량을 자동 조절 |

설정은 `.env` 의 `LOCAL_*` 항목입니다. 자세한 옵션은 `.env.example` 참고.

## 실행 방법

```bash
# 1. 가상환경 (C#의 프로젝트별 패키지 격리와 같은 개념)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Mac/Linux

# 2. 패키지 설치
pip install -r requirements.txt

# 3. 환경변수 파일 만들기
copy .env.example .env        # Windows
# .env 를 열어서 LAW_OC(법제처 인증키) 등을 채우세요

# 4. 로컬 LLM 서버 + 웹 서버 실행
_3_start_server.bat
```

브라우저에서 http://127.0.0.1:8000 접속.

`--reload` 는 코드 수정 시 자동 재시작입니다. 개발 중에만 쓰세요.

실제 배포/운영은 `_3_start_server.bat` (서버 + Cloudflare 터널) 과
`_4_start_watcher.bat` (자동 테스트 + 자동 커밋/푸시) 를 사용합니다.

## 파일 구조

| 파일 | 역할 |
|---|---|
| `law_client.py` | 법제처 API 호출 및 XML 파싱 |
| `ai_client.py` | 로컬 LLM 호출 (llama-server, 토큰 계산) |
| `applog.py` | 서버 로그 — 서울 시간, 질문별 START/END 블록, LLM 호출별 토큰·시간 |
| `main.py` | FastAPI 서버 및 라우팅 |
| `static/index.html` | 화면 전체 (HTML+CSS+JS 한 파일) |
| `watch_and_test.py` | 소스 변경 감지 → 테스트 → 자동 커밋/푸시 |
| `eval_run.py`, `eval_cases.json` | 자동 테스트 실행기와 시나리오 |

## 로그

- 서버 창과 `_runs/lawfinder_YYYYMMDD.log` 에 같은 내용이 남습니다 (`_runs/` 는 git 에 안 올라감).
- 질문 하나가 `▶ START #번호` ~ `■ END #번호` 로 묶이고, 되묻기 왕복도 같은 번호로 이어집니다.
- `.env` 의 `LLM_DEBUG=1` 이면 LLM 프롬프트·응답 원문을 `_runs/llm_debug_YYYYMMDD.log` 에 남깁니다.

## 주의

- `.env` 는 절대 git 에 올리지 마세요. `.gitignore` 에 이미 등록되어 있습니다.
- 답변은 반드시 오른쪽 조문 원문과 대조하세요. AI 는 틀립니다.
- 시행일 배지가 "미확인" 으로 뜨면 그 법령은 신뢰하지 마세요.
