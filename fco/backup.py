"""사용자가 직접 입력한 기록(보유 매물 · 매도 기록 · 쿠폰) 자동 백업과 복원.

대시보드에서 기록을 추가·수정·삭제할 때마다 data/backups/user_records_<시각>.json 을 남긴다 (최근 100개 보관).
복원:  python -m fco.backup restore data\\backups\\user_records_....json
       → 그 파일에 있고 지금 DB 에 없는 행(id 기준)만 다시 넣는다. 기존 행은 건드리지 않는다.
목록:  python -m fco.backup list
"""
import json
import sys
from datetime import datetime

from .config import BACKUP_DIR
from .db import KST, connect

TABLES = ("holding", "trade", "coupon", "watch")
KEEP = 100


def backup_user_records(con, reason: str = "") -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    data = {"saved_at": datetime.now(KST).isoformat(timespec="seconds"), "reason": reason}
    for t in TABLES:
        data[t] = [dict(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY id")]
    name = f"user_records_{datetime.now(KST).strftime('%Y%m%d_%H%M%S_%f')}.json"
    path = BACKUP_DIR / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    files = sorted(BACKUP_DIR.glob("user_records_*.json"))
    for old in files[:-KEEP]:
        old.unlink(missing_ok=True)
    return str(path)


def restore(path: str) -> dict:
    data = json.loads(open(path, encoding="utf-8").read())
    con = connect()
    added = {}
    for t in TABLES:
        n = 0
        for row in data.get(t, []):
            if con.execute(f"SELECT 1 FROM {t} WHERE id = ?", (row["id"],)).fetchone():
                continue
            cols = ",".join(row)
            con.execute(f"INSERT INTO {t} ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
            n += 1
        added[t] = n
    con.commit()
    backup_user_records(con, f"복원 후 ({path})")
    return added


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv[:1] == ["list"]:
        for f in sorted(BACKUP_DIR.glob("user_records_*.json"))[-20:]:
            d = json.loads(f.read_text(encoding="utf-8"))
            print(f.name, d.get("reason", ""), {t: len(d.get(t, [])) for t in TABLES})
        return 0
    if argv[:1] == ["restore"] and len(argv) == 2:
        print("다시 넣은 행:", restore(argv[1]))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
