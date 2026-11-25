"""외부(휴대폰) 접속용 Cloudflare 임시 터널 관리.

  python -m fco.tunnel          대시보드 서버가 꺼져 있으면 켜고, 터널을 열어 주소를 기록한 뒤 계속 지켜본다.
                                터널이 끊기면 다시 연다 (그때 주소가 바뀐다).

- cloudflared (tools/cloudflared.exe, Cloudflare 공식 배포판) 의 'quick tunnel' 을 쓴다:
  계정·도메인 없이 https://xxxx.trycloudflare.com 주소가 생기고, 터널을 다시 열 때마다 주소가 바뀐다.
- 대시보드 서버는 계속 127.0.0.1 에만 열려 있고, 터널만 바깥과 연결된다 (공유기 포트를 열지 않음).
- 지금 주소: data/tunnel.json, 바탕화면 'FC 매물 분석 외부 주소.txt', 대시보드 3탭.
- 로그인(비밀번호)이 설정돼 있지 않으면 터널을 열지 않는다.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import auth
from .config import DATA_DIR, ROOT

EXE = ROOT / "tools" / "cloudflared.exe"
STATE = DATA_DIR / "tunnel.json"
LOG = DATA_DIR / "tunnel.log"
LOCAL = "http://127.0.0.1:8765"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
NO_WINDOW = 0x08000000


def desktop_file() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / "FC 매물 분석 외부 주소.txt"


def write_state(url: str | None, status: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    st = {"url": url, "status": status, "updated_at": datetime.now().isoformat(timespec="seconds"), "pid": os.getpid()}
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    try:
        desktop_file().write_text(
            (f"휴대폰에서 이 주소로 접속하세요 (비밀번호 필요):\n{url}\n\n" if url else "외부 접속 주소 없음 (터널 꺼짐)\n")
            + f"기록 시각: {st['updated_at']}\nPC를 다시 켜거나 터널이 다시 열리면 주소가 바뀝니다.\n", encoding="utf-8")
    except OSError:
        pass


def read_state() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"url": None, "status": "off"}
    return st


def pid_alive(pid: int) -> bool:
    """프로세스가 살아 있는지만 확인 (Windows 에서 os.kill(pid, 0) 은 프로세스를 끝내 버리므로 쓰지 않는다)."""
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def dashboard_alive() -> bool:
    try:
        with urllib.request.urlopen(LOCAL + "/login", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_dashboard() -> None:
    if dashboard_alive():
        return
    pyw = Path(sys.executable).with_name("pythonw.exe")
    with open(DATA_DIR / "dashboard.log", "ab") as lf:
        subprocess.Popen([str(pyw if pyw.exists() else sys.executable), "-m", "fco.dashboard"], cwd=str(ROOT),
                         stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW | 0x00000008)
    for _ in range(60):
        if dashboard_alive():
            return
        time.sleep(0.5)


def _notify(url: str, log) -> None:
    """새 주소를 카카오톡 '나와의 채팅'으로 (연결돼 있을 때만). 실패해도 터널에는 영향 없음."""
    try:
        from . import kakao
        res = kakao.notify_tunnel_url(url)
    except Exception as e:
        res = f"카카오 전송 실패: {e}"
    try:
        log.write(f"[kakao] {res}\n")
        log.flush()
    except Exception:
        pass


def run_once(log) -> None:
    """터널 1개를 열고, 끝날 때까지 출력에서 주소를 찾아 기록한다."""
    p = subprocess.Popen([str(EXE), "tunnel", "--no-autoupdate", "--url", LOCAL], cwd=str(ROOT),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
    url = None
    for line in p.stdout:
        log.write(line)
        log.flush()
        m = URL_RE.search(line)
        if m and m.group(0) != url:
            url = m.group(0)
            write_state(url, "on")
            threading.Thread(target=_notify, args=(url, log), daemon=True).start()  # 카카오톡 나에게 보내기
    p.wait()
    write_state(None, "restarting")


def main() -> int:
    if not EXE.exists():
        print("tools/cloudflared.exe 가 없습니다", file=sys.stderr)
        return 1
    if not auth.enabled():
        print("비밀번호가 없으면 외부 접속을 열지 않습니다. 먼저: python -m fco.auth set", file=sys.stderr)
        return 1
    st = read_state()
    if st.get("status") in ("on", "restarting") and st.get("pid") and st["pid"] != os.getpid() and pid_alive(st["pid"]):
        print("이미 터널이 돌고 있습니다:", st.get("url"))  # 다른 관리 프로세스가 돌고 있으면 그대로 둔다
        return 0
    os.chdir(ROOT)
    with open(LOG, "a", encoding="utf-8") as log:
        while True:
            ensure_dashboard()
            log.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} 터널 시작 ===\n")
            run_once(log)
            time.sleep(10)  # 끊기면 잠시 뒤 다시


if __name__ == "__main__":
    sys.exit(main())
