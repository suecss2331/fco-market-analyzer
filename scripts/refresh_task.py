"""작업 스케줄러가 창 없이(pythonw) 실행하는 진입점.  사용: pythonw refresh_task.py quick|daily
프로젝트 폴더로 이동한 뒤 fco.refresh 를 실행하고, 출력은 data/refresh_task.log 에 남긴다."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
(ROOT / "data").mkdir(exist_ok=True)
with open(ROOT / "data" / "refresh_task.log", "a", encoding="utf-8") as log:
    sys.stdout = sys.stderr = log
    from fco.db import now_kst
    from fco.refresh import main
    print(f"--- {now_kst()} {' '.join(sys.argv[1:])}", flush=True)
    code = main(sys.argv[1:])
    print(f"--- exit {code}", flush=True)
sys.exit(code)
