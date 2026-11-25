"""가격 수집 현황 + 활성 가격대 필터 결과.

실행:  python -m fco.price_report [--preset 10억-100억]
"""
import argparse
import sys

from .config import load_settings, active_price_range
from .db import connect


def eok(v: int) -> str:
    return f"{v / 1e8:,.2f}억"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", help="settings 의 다른 가격대 프리셋으로 미리보기")
    args = ap.parse_args(argv)
    settings = load_settings()
    if args.preset:
        settings["priceRange"]["activePreset"] = args.preset
    lo, hi, preset = active_price_range(settings)
    con = connect()

    total = con.execute("SELECT COUNT(*) FROM card").fetchone()[0]
    print(f"카드 마스터 {total}장")
    for r in con.execute("SELECT status, COUNT(*) n, MAX(fetched_at) last FROM price_status GROUP BY status"):
        print(f"  {r['status']:8s} {r['n']:5d}장  (마지막 갱신 {r['last']})")
    warn = con.execute("SELECT COUNT(*) FROM price_status WHERE date_check IS NOT NULL AND date_check <> 'ok'").fetchone()[0]
    print(f"  날짜 역산 경고 {warn}장")

    print(f"\n현재가 구간별 카드 수 (ok 카드 기준)")
    bands = [(0, 1e8, "1억 미만"), (1e8, 1e9, "1억-10억"), (1e9, 1e10, "10억-100억"), (1e10, 1e15, "100억 이상")]
    for a, b, name in bands:
        n = con.execute("SELECT COUNT(*) FROM price_status WHERE status='ok' AND current_price >= ? AND current_price < ?",
                        (a, b)).fetchone()[0]
        print(f"  {name:10s} {n:5d}장")

    rows = con.execute(
        """SELECT p.spid, p.grade, k.player_name, k.class_name, k.ovr, k.main_position, p.current_price, p.days,
                  (SELECT COUNT(DISTINCT s.rank) FROM squad_entry s WHERE s.spid=p.spid AND s.grade=p.grade
                     AND s.snap_id=(SELECT MAX(snap_id) FROM ranking_snapshot)) AS users,
                  (SELECT COUNT(DISTINCT s.rank) FROM squad_entry s WHERE s.spid=p.spid AND s.grade=p.grade
                     AND s.rank<=20 AND s.snap_id=(SELECT MAX(snap_id) FROM ranking_snapshot)) AS top20
           FROM price_status p JOIN card k USING (spid, grade)
           WHERE p.status='ok' AND p.current_price BETWEEN ? AND ?
           ORDER BY top20 DESC, users DESC LIMIT 15""", (lo, hi)).fetchall()
    print(f"\n[{preset}] 구간 카드 중 랭커 사용 많은 순 15장")
    for r in rows:
        print(f"  {r['player_name']} {r['class_name'].split(' (')[0]} {r['grade']}강 OVR{r['ovr']} {r['main_position']}"
              f" | 현재가 {eok(r['current_price'])} | 기록 {r['days']}일 | 1~200위 {r['users']}명, 1~20위 {r['top20']}명")
    return 0


if __name__ == "__main__":
    sys.exit(main())
