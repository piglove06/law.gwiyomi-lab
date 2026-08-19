# 법령 조회 도우미

법제처 국가법령정보 OPEN API 로 법령을 검색/조회하고, AI 로 질의응답을 도와주는 도구입니다.
실제 서비스: https://law.gwiyomi-lab.com

## AI 백엔드

`.env` 의 `LLM_BACKEND` 한 줄로 전환합니다.

| 값 | 설명 |
|---|---|
| `local` (현재 기본) | Ollama 로컬 모델 사용. 데이터가 PC 밖으로 안 나갑니다. `LOCAL_MODEL` (기본 `qwen3.5:9b-q4_K_M`) 이 실행 중이어야 합니다. |
| `gemini` | Google Gemini 사용. `GEMINI_API_KEY` 필요. 질문 내용이 구글로 전송됩니다. |

단계별(`terms`/`select`/`clarify`/`answer`)로 다른 백엔드를 섞어 쓰려면
`LLM_BACKEND_TERMS` 등 개별 값을 `.env` 에서 주석 해제하세요. 자세한 옵션은
`.env.example` 참고.

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
# .env 를 열어서 LLM_BACKEND 와 그에 맞는 값을 채우세요

# 4. 서버 실행
uvicorn main:app --reload
```

브라우저에서 http://127.0.0.1:8000 접속.

`--reload` 는 코드 수정 시 자동 재시작입니다. 개발 중에만 쓰세요.

실제 배포/운영은 `_3_start_server.bat` (서버 + Cloudflare 터널) 과
`_4_start_watcher.bat` (자동 테스트 + 자동 커밋/푸시) 를 사용합니다.

## 파일 구조

| 파일 | 역할 |
|---|---|
| `law_client.py` | 법제처 API 호출 및 XML 파싱 |
| `ai_client.py` | AI 호출 (로컬 Ollama 또는 Gemini, `.env` 로 전환) |
| `main.py` | FastAPI 서버 및 라우팅 |
| `static/index.html` | 화면 전체 (HTML+CSS+JS 한 파일) |
| `watch_and_test.py` | 소스 변경 감지 → 테스트 → 자동 커밋/푸시 |

## 주의

- `.env` 는 절대 git 에 올리지 마세요. `.gitignore` 에 이미 등록되어 있습니다.
- 답변은 반드시 오른쪽 조문 원문과 대조하세요. AI 는 틀립니다.
- 시행일 배지가 "미확인" 으로 뜨면 그 법령은 신뢰하지 마세요.
