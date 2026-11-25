"""4단계 실행: 모든 카드의 평일·주말 지표를 계산해 weekly_stats 에 저장하고 요약을 출력한다.

분석 기준 마지막 날 = DB 에 있는 가장 최근 일별 가격 날짜 (보통 어제).
현재가 = price_current 의 가장 최근 관측값.

실행:  python -m fco.analyze_weekly [--preset 10억-100억]
"""
import argparse
import sys
from collections import defaultdict
from datetime import date
from statistics import median

from .config import load_settings, active_price_range
from .db import connect, log, now_kst
from .weekly import compute_weekly


def eok(v) -> str:
    return "-" if v is None else f"{v / 1e8:,.2f}억"


def pct(v) -> str:
    return "-" if v is None else f"{v:+.1f}%"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset")
    args = ap.parse_args(argv)
    settings = load_settings()
    if args.preset:
        settings["priceRange"]["activePreset"] = args.preset
    cfg = settings["weekly"]
    lo, hi, preset = active_price_range(settings)
    con = connect()

    end_s = con.execute("SELECT MAX(day) FROM price_daily").fetchone()[0]
    if not end_s:
        raise SystemExit("일별 가격이 없습니다. 먼저 python -m fco.collect_prices 를 실행하세요.")
    end = date.fromisoformat(end_s)

    series = defaultdict(list)
    for r in con.execute("SELECT spid, grade, day, price FROM price_daily ORDER BY spid, grade, day"):
        series[(r["spid"], r["grade"])].append((date.fromisoformat(r["day"]), r["price"]))
    current = {}
    for r in con.execute("""SELECT c.spid, c.grade, c.price, c.observed_at FROM price_current c
                            JOIN (SELECT spid, grade, MAX(observed_at) m FROM price_current GROUP BY spid, grade) x
                              ON x.spid = c.spid AND x.grade = c.grade AND x.m = c.observed_at"""):
        current[(r["spid"], r["grade"])] = (r["price"], r["observed_at"])

    ts = now_kst()
    rows = []
    for key, pts in series.items():
        cur, cur_at = current.get(key, (None, None))
        # 기준일은 카드마다 그 카드의 마지막 일별 가격 날짜 (일부 카드만 갱신돼도 다른 카드가 '빠진 날'로 보이지 않게)
        s = compute_weekly(pts, max(d for d, _ in pts), cfg, cur)
        rows.append((key[0], key[1], ts, s["window_start"], s["window_end"], cur, cur_at,
                     s["wd_n"], s["wd_mean"], s["wd_median"], s["we_n"], s["we_mean"], s["we_median"],
                     s["premium_pct"], s["discount_pct"], s["weeks_total"], s["weeks_positive"],
                     s["weekly_premium_median_pct"],
                     None if s["transient"] is None else int(s["transient"]), s["status"], s["note"]))
    con.execute("DELETE FROM weekly_stats")
    con.executemany(f"INSERT INTO weekly_stats VALUES ({','.join('?' * 21)})", rows)
    con.commit()

    ok = [r for r in rows if r[19] == "ok"]
    log(con, "analyze_weekly", True, f"{len(rows)}장 분석 (ok {len(ok)}), 기준일 {end_s}")
    print(f"[weekly] 기준 기간 {rows[0][3]} ~ {end_s} ({cfg['windowDays']}일) / 분석 {len(rows)}장, "
          f"충분 {len(ok)}장, 데이터 부족 {len(rows) - len(ok)}장")

    # --- 시장 전체 경향 (참고) -------------------------------------------
    prem = [r[13] for r in ok]
    wk = [r[17] for r in ok if r[17] is not None]
    print(f"\n[시장 전체 참고] 기획서 공식 주말 프리미엄 중앙값 {pct(median(prem))} / "
          f"추세 보정 주별 중앙값 {pct(median(wk)) if wk else '-'}")

    # --- 활성 가격대 ------------------------------------------------------
    sel = con.execute(
        """SELECT w.*, k.player_name, k.class_name, k.ovr, k.main_position,
                  (SELECT COUNT(DISTINCT s.rank) FROM squad_entry s WHERE s.spid=w.spid AND s.grade=w.grade
                     AND s.snap_id=(SELECT MAX(snap_id) FROM ranking_snapshot)) AS users
           FROM weekly_stats w JOIN card k USING (spid, grade)
           WHERE w.current_price BETWEEN ? AND ?""", (lo, hi)).fetchall()
    good = [r for r in sel if r["status"] == "ok"]
    cand = [r for r in good if r["premium_pct"] > 0 and r["transient"] == 0]
    print(f"\n[{preset}] {len(sel)}장 중 지표 계산 가능 {len(good)}장, 데이터 부족 {len(sel) - len(good)}장")
    print(f"  주말 평균 > 평일 평균: {sum(1 for r in good if r['premium_pct'] > 0)}장"
          f" → 그중 반복성 있음(일시적 아님): {len(cand)}장")
    print(f"  현재가 < 평일 평균: {sum(1 for r in good if (r['discount_pct'] or 0) > 0)}장")

    def line(r):
        return (f"  {r['player_name']} {r['class_name'].split(' (')[0]} {r['grade']}강 OVR{r['ovr']}"
                f" | 현재 {eok(r['current_price'])} 평일avg {eok(r['wd_mean'])} 주말avg {eok(r['we_mean'])}"
                f" | 프리미엄 {pct(r['premium_pct'])} (주별 {r['weeks_positive']}/{r['weeks_total']}주 상승,"
                f" 중앙 {pct(r['weekly_premium_median_pct'])}) | 할인 {pct(r['discount_pct'])} | 랭커 {r['users']}명")

    print("\n주말 프리미엄 상위 (반복성 있는 카드, 주별 상승 비율 → 주별 중앙값 순) 15장")
    cand.sort(key=lambda r: (-(r["weeks_positive"] / r["weeks_total"]), -(r["weekly_premium_median_pct"] or 0)))
    for r in cand[:15]:
        print(line(r))
    print("\n'일시적' 으로 걸러진 예 (평균은 주말이 높지만 매주 반복되지 않음) 5장")
    trans = sorted([r for r in good if r["transient"] == 1], key=lambda r: -r["premium_pct"])
    for r in trans[:5]:
        print(line(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
