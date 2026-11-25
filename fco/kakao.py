"""카카오톡 '나에게 보내기' (카카오 공식 API: 카카오톡 메시지 > 나에게 보내기).

이 API 는 구조상 로그인한 본인의 '나와의 채팅'에만 보낼 수 있다 (다른 사람에게 갈 수 없음).

설정 (사용자가 직접):
  1) developers.kakao.com 에서 애플리케이션 추가
  2) 카카오 로그인 활성화 + Redirect URI:  http://localhost:8765/kakao/callback
  3) 동의항목: '카카오톡 메시지 전송 (talk_message)' 사용
  4) 플랫폼 > Web 사이트 도메인: http://localhost:8765
  5) 대시보드 3탭 '카카오톡 알림' 에 REST API 키 (와 Client Secret 을 켰다면 그 값) 입력 → '카카오 연결' → 카카오 로그인·동의

저장: config/secrets.local.json 의 "kakao" (키 · 토큰). 토큰은 만료 전에 자동 갱신.
"""
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import CONFIG_DIR

SECRETS = CONFIG_DIR / "secrets.local.json"
REDIRECT = "http://localhost:8765/kakao/callback"
AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
_pending_state = {"value": None, "at": 0.0}


def _load() -> dict:
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save_kakao(k: dict) -> None:
    d = _load()
    d["kakao"] = k
    SECRETS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def cfg() -> dict:
    return _load().get("kakao") or {}


def status() -> dict:
    k = cfg()
    return {"configured": bool(k.get("rest_api_key")), "connected": bool(k.get("refresh_token")),
            "refresh_expires_at": k.get("refresh_expires_at"), "last_sent_at": k.get("last_sent_at"),
            "last_error": k.get("last_error"), "redirect_uri": REDIRECT,
            "only_when_running": bool(k.get("only_when_running", True)), "pc_kakao_running": pc_kakao_running()}


def set_keys(rest_api_key: str, client_secret: str = "") -> None:
    """빈 칸은 저장된 값을 유지 (화면에 '저장됨'으로만 보이므로)."""
    k = cfg()
    rest = (rest_api_key or "").strip() or k.get("rest_api_key", "")
    if not rest:
        raise ValueError("REST API 키를 입력하세요")
    if k.get("rest_api_key") != rest:  # 키가 바뀌면 기존 연결은 무효
        k = {}
    k["rest_api_key"] = rest
    if (client_secret or "").strip():
        k["client_secret"] = client_secret.strip()
    k.setdefault("only_when_running", True)  # 사용자 요청: PC 카카오톡이 실행 중일 때 보냄
    _save_kakao(k)


def set_only_when_running(on: bool) -> None:
    k = cfg()
    k["only_when_running"] = bool(on)
    _save_kakao(k)


def _post(url: str, form: dict, token: str | None = None) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"카카오 응답 {e.code}: {body[:300]}") from None


def login_url() -> str:
    k = cfg()
    if not k.get("rest_api_key"):
        raise RuntimeError("REST API 키를 먼저 입력하세요")
    _pending_state.update(value=secrets.token_urlsafe(24), at=time.time())
    q = {"client_id": k["rest_api_key"], "redirect_uri": REDIRECT, "response_type": "code",
         "scope": "talk_message", "state": _pending_state["value"]}
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def valid_state(state: str | None) -> bool:
    """로그인을 시작한 이 서버가 만든 state 인지 (10분 안). 한 번 쓰면 버린다."""
    ok = bool(state) and state == _pending_state["value"] and time.time() - _pending_state["at"] < 600
    if ok:
        _pending_state["value"] = None
    return ok


def _store_tokens(resp: dict) -> None:
    k = cfg()
    now = time.time()
    k["access_token"] = resp["access_token"]
    k["expires_at"] = now + int(resp.get("expires_in", 0)) - 60
    if resp.get("refresh_token"):  # 갱신 때는 만료가 1달 안 남았을 때만 새로 온다
        k["refresh_token"] = resp["refresh_token"]
        k["refresh_expires_at"] = now + int(resp.get("refresh_token_expires_in", 0))
    k.pop("last_error", None)
    _save_kakao(k)


def exchange_code(code: str) -> None:
    k = cfg()
    form = {"grant_type": "authorization_code", "client_id": k["rest_api_key"], "redirect_uri": REDIRECT, "code": code}
    if k.get("client_secret"):
        form["client_secret"] = k["client_secret"]
    _store_tokens(_post(TOKEN_URL, form))


def _access_token() -> str:
    k = cfg()
    if not k.get("refresh_token"):
        raise RuntimeError("카카오 연결이 안 돼 있습니다 (3탭 '카카오 연결')")
    if k.get("access_token") and time.time() < float(k.get("expires_at", 0)):
        return k["access_token"]
    form = {"grant_type": "refresh_token", "client_id": k["rest_api_key"], "refresh_token": k["refresh_token"]}
    if k.get("client_secret"):
        form["client_secret"] = k["client_secret"]
    _store_tokens(_post(TOKEN_URL, form))
    return cfg()["access_token"]


def pc_kakao_running() -> bool:
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq KakaoTalk.exe", "/NH"], capture_output=True, text=True,
                             timeout=10, creationflags=0x08000000).stdout
        return "KakaoTalk.exe" in out
    except Exception:
        return False


def send_to_me(text: str, url: str | None = None, button: str | None = None) -> None:
    """나와의 채팅에 텍스트 메시지. button 을 주면 버튼도 붙인다 (기본은 버튼 없이 본문만)."""
    tmpl = {"object_type": "text", "text": text[:200],
            "link": {"web_url": url or "http://localhost:8765", "mobile_web_url": url or "http://localhost:8765"}}
    if button:
        tmpl["button_title"] = button
    try:
        res = _post(MEMO_URL, {"template_object": json.dumps(tmpl, ensure_ascii=False)}, _access_token())
        if res.get("result_code", 0) != 0:
            raise RuntimeError(f"카카오 전송 실패: {res}")
        k = cfg()
        k["last_sent_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        k.pop("last_error", None)
        _save_kakao(k)
    except Exception as e:
        k = cfg()
        k["last_error"] = f"{time.strftime('%m-%d %H:%M')} {e}"[:400]
        _save_kakao(k)
        raise


def notify_tunnel_url(url: str) -> str:
    """외부 접속 주소가 새로 생겼을 때. 연결 안 됐으면 조용히 건너뜀. 결과 문자열을 돌려준다 (로그용)."""
    k = cfg()
    if not k.get("refresh_token"):
        return "카카오 미연결 — 건너뜀"
    if k.get("only_when_running", True):
        for _ in range(15):  # 로그인 직후엔 카톡이 터널보다 늦게 켜질 수 있어 최대 5분 기다림
            if pc_kakao_running():
                break
            time.sleep(20)
        else:
            return "PC 카카오톡이 꺼져 있어 건너뜀"
    # 사용자 요청: 링크만 보낸다
    try:
        send_to_me(url, url)
    except RuntimeError:
        send_to_me(url, None)  # 링크 도메인(trycloudflare.com)이 거절되면 등록된 도메인으로 (본문은 그대로 주소)
    return "카카오 전송 완료"
