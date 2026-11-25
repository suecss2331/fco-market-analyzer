"""대시보드 로그인 (비밀번호 1개).

- 비밀번호는 원문을 저장하지 않고 PBKDF2-SHA256 해시(+ 무작위 salt)만 config/secrets.local.json 에 둔다.
- 로그인하면 서명된 쿠키(만료 시각 + HMAC)를 준다. 서버를 다시 켜도 유지되고, '모든 기기 로그아웃'은 서명 키를 바꾼다.
- 무차별 대입 방지: 같은 접속지에서 5번 틀리면 15분 잠금, 전체적으로 1시간에 20번 틀리면 1시간 동안 로그인 자체를 막는다.
  (숫자 4자리 비밀번호는 경우의 수가 1만 개뿐이라 이 잠금이 유일한 방어선이다.)

  python -m fco.auth set        비밀번호 설정 (표준 입력으로 받음, 화면에 안 보임)
  python -m fco.auth logout-all 모든 기기 로그아웃
"""
import base64
import getpass
import hashlib
import hmac
import json
import secrets
import sys
import threading
import time

from .config import CONFIG_DIR

SECRETS = CONFIG_DIR / "secrets.local.json"
COOKIE = "fco_session"
SESSION_DAYS = 30
ITER = 300_000

IP_MAX_FAILS, IP_LOCK_SEC = 5, 15 * 60
GLOBAL_MAX_FAILS, GLOBAL_WINDOW_SEC, GLOBAL_LOCK_SEC = 20, 3600, 3600

_lock = threading.Lock()
_ip_fails: dict[str, list] = {}       # ip → [틀린 횟수, 잠금 해제 시각]
_global_fails: list[float] = []       # 최근 틀린 시각들
_global_lock_until = 0.0


def _load() -> dict:
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save(d: dict) -> None:
    SECRETS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash(password: str, salt: bytes, iters: int = ITER) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)


def set_password(password: str) -> None:
    d = _load()
    salt = secrets.token_bytes(16)
    d["dashboardAuth"] = {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(_hash(password, salt)).decode(),
                          "iter": ITER, "key": secrets.token_hex(32)}
    _save(d)


def enabled() -> bool:
    return "dashboardAuth" in _load()


def logout_all() -> None:
    d = _load()
    if "dashboardAuth" in d:
        d["dashboardAuth"]["key"] = secrets.token_hex(32)
        _save(d)


def _cfg() -> dict:
    return _load().get("dashboardAuth") or {}


def check_password(password: str) -> bool:
    c = _cfg()
    if not c:
        return False
    salt = base64.b64decode(c["salt"])
    return hmac.compare_digest(_hash(password or "", salt, int(c.get("iter", ITER))), base64.b64decode(c["hash"]))


# --- 세션 쿠키 -----------------------------------------------------------------
def make_token(now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_cfg()["key"].encode(), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not exp.isdigit() or int(exp) < (now or time.time()):
        return False
    key = _cfg().get("key")
    if not key:
        return False
    good = hmac.new(key.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


def cookie_header(token: str, secure: bool) -> str:
    return (f"{COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict"
            + ("; Secure" if secure else ""))


def clear_cookie_header() -> str:
    return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"


# --- 잠금 ----------------------------------------------------------------------
def locked(ip: str, now: float | None = None) -> int:
    """잠겨 있으면 남은 초, 아니면 0."""
    now = now or time.time()
    with _lock:
        if _global_lock_until > now:
            return int(_global_lock_until - now)
        f = _ip_fails.get(ip)
        if f and f[1] > now:
            return int(f[1] - now)
    return 0


def record(ip: str, ok: bool, now: float | None = None) -> None:
    global _global_lock_until
    now = now or time.time()
    with _lock:
        if ok:
            _ip_fails.pop(ip, None)
            return
        f = _ip_fails.setdefault(ip, [0, 0.0])
        f[0] += 1
        if f[0] >= IP_MAX_FAILS:
            f[0], f[1] = 0, now + IP_LOCK_SEC
        _global_fails.append(now)
        while _global_fails and _global_fails[0] < now - GLOBAL_WINDOW_SEC:
            _global_fails.pop(0)
        if len(_global_fails) >= GLOBAL_MAX_FAILS:
            _global_lock_until = now + GLOBAL_LOCK_SEC
            _global_fails.clear()


def reset_limits() -> None:  # 테스트용
    global _global_lock_until
    with _lock:
        _ip_fails.clear()
        _global_fails.clear()
        _global_lock_until = 0.0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv[:1] == ["set"]:
        pw = sys.stdin.readline() if not sys.stdin.isatty() else getpass.getpass("새 비밀번호: ")
        pw = pw.lstrip("﻿").strip()  # PowerShell 파이프가 붙이는 BOM · 줄바꿈 제거
        if not pw:
            print("비밀번호가 비어 있습니다")
            return 1
        set_password(pw)
        print("비밀번호를 설정했습니다 (해시만 저장). 기존 로그인은 모두 풀립니다.")
        return 0
    if argv[:1] == ["logout-all"]:
        logout_all()
        print("모든 기기를 로그아웃했습니다.")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
