"""바탕화면 바로가기용 실행기 (pythonw 로 실행되어 창이 뜨지 않는다).

1) 대시보드 서버(127.0.0.1:8765)가 이미 켜져 있으면 그대로 쓴다.
2) 꺼져 있으면 서버를 백그라운드로 켠다 (이 실행기가 끝나도 서버는 계속 돈다).
3) 서버가 응답하면 기본 브라우저로 대시보드를 연다.
문제가 생기면 data/launcher.log 에 남기고 알림 창을 띄운다.
"""
import os
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8765"
LOG = ROOT / "data" / "launcher.log"


def alive() -> bool:
    try:
        with urllib.request.urlopen(URL + "/login", timeout=2) as r:  # 로그인 없이 열리는 주소로 확인
            return r.status == 200
    except Exception:
        return False


def notify(msg: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "FC 매물 분석", 0x10)
    except Exception:
        pass


def main() -> None:
    (ROOT / "data").mkdir(exist_ok=True)
    if not alive():
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw if pyw.exists() else sys.executable)
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        with open(ROOT / "data" / "dashboard.log", "ab") as lf:
            subprocess.Popen([exe, "-m", "fco.dashboard"], cwd=str(ROOT), stdout=lf, stderr=lf,
                             stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        for _ in range(60):  # 최대 30초 대기
            if alive():
                break
            time.sleep(0.5)
        else:
            notify("대시보드 서버를 켜지 못했습니다.\n자세한 내용: data\\dashboard.log")
            return
    webbrowser.open(URL)


if __name__ == "__main__":
    os.chdir(ROOT)
    try:
        main()
    except Exception:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        notify("실행 중 오류가 났습니다.\n자세한 내용: data\\launcher.log")
