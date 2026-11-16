# -*- coding: utf-8 -*-
"""평가·감시 프로그램이 같이 쓰는 서버 로그인 도우미. 표준 라이브러리만."""
import os
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def env_value(name: str) -> str:
    """.env 에서 값 하나를 읽습니다."""
    p = os.path.join(HERE, ".env")
    if not os.path.exists(p):
        return ""
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip()
    except OSError:
        pass
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def login(base: str) -> str:
    """APP_PASSWORD 로 로그인해서 인증 쿠키 값을 얻습니다. 실패하면 빈 문자열."""
    password = env_value("APP_PASSWORD")
    if not password:
        return ""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        base.rstrip("/") + "/login",
        data=urllib.parse.urlencode({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=10) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as e:
        headers = e.headers
    except Exception:                             # noqa: BLE001
        return ""
    for raw in headers.get_all("Set-Cookie") or []:
        if raw.startswith("lawfinder_auth="):
            return raw.split(";", 1)[0].split("=", 1)[1]
    return ""
