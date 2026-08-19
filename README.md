# 법령 조회 도우미 (프로토타입)

법제처 국가법령정보 OPEN API + Gemini 로 만든 법령 검색 도구입니다.

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
# .env 를 열어서 GEMINI_API_KEY 를 채우세요

# 4. 서버 실행
uvicorn main:app --reload
```

브라우저에서 http://127.0.0.1:8000 접속.

`--reload` 는 코드 수정 시 자동 재시작입니다. 개발 중에만 쓰세요.

## 처음 할 일: XML 태그 이름 확인

법제처 응답의 태그 이름을 확인하지 못한 상태로 만들었습니다.
서버 켠 뒤 아래 주소로 원본 XML 을 보고, 실제 태그 이름을 확인하세요.

```
http://127.0.0.1:8000/api/raw?target=law&value=토양환경보전법&mode=search
http://127.0.0.1:8000/api/raw?target=law&value=000160&mode=detail
```

확인되면 `main.py` 의 `law_client.pick(...)` 후보 목록을 실제 태그로 좁히고,
`_raw` 필드를 지우세요.

## 파일 구조

| 파일 | 역할 |
|---|---|
| `law_client.py` | 법제처 API 호출 및 XML 파싱 |
| `ai_client.py` | Gemini 호출. 다른 모델로 바꾸려면 여기만 수정 |
| `main.py` | FastAPI 서버 및 라우팅 |
| `static/index.html` | 화면 전체 (HTML+CSS+JS 한 파일) |

## 주의

- `.env` 는 절대 git 에 올리지 마세요. `.gitignore` 에 추가하세요.
- 답변은 반드시 오른쪽 조문 원문과 대조하세요. AI 는 틀립니다.
- 시행일 배지가 "미확인" 으로 뜨면 그 법령은 신뢰하지 마세요.
