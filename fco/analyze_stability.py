"""6단계 실행: 모든 카드의 가격 안정성을 계산해 stability_stats 에 저장하고 요약을 출력한다.

실행:  python -m fco.analyze_stability [--preset 10억-100억]
"""
import argparse
import sys
from collections import defaultdict
from datetime import date
from statistics import median

from .config import load_settings, active_price_range
from .db import connect, log, now_kst
from .stability import compute_stability

WIN_KEYS = ["n", "coverage", "max_gap_days", "mean", "std", "cv", "change_pct", "range_pct",
            "trend_pct_per_day", "trend"]


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
    cfg = settings["stability"]
    lo, hi, preset = active_price_range(settings)
    con = connect()

    end_s = con.execute("SELECT MAX(day) FROM price_daily").fetchone()[0]
    if not end_s:
        raise SystemExit("일별 가격이 없습니다. 먼저 python -m fco.collect_prices 를 실행하세요.")
    end = date.fromisoformat(end_s)
    series = defaultdict(list)
    for r in con.execute("SELECT spid, grade, day, price FROM price_daily"):
        series[(r["spid"], r["grade"])].append((date.fromisoformat(r["day"]), r["price"]))

    ts = now_kst()
    rows = []
    for (spid, grade), pts in series.items():
        card_end = max(d for d, _ in pts)  # 카드별 기준일 (그 카드의 마지막 일별 가격 날짜)
        s = compute_stability(pts, card_end, cfg)
        rows.append((spid, grade, ts, card_end.isoformat(),
                     *[s["short"][k] for k in WIN_KEYS], *[s["long"][k] for k in WIN_KEYS],
                     s["max_daily_drop_pct"], s["max_daily_rise_pct"],
                     int(s["sharp_drop"]), int(s["sharp_rise"]), int(s["persistent_decline"]),
                     s["flat_run_days"], int(s["stale"]),
                     s["status"], s["note"]))
    con.execute("DROP TABLE IF EXISTS stability_stats")  # 열 추가 반영 (매번 전체 재계산하는 표)
    con.close()
    con = connect()
    con.executemany(f"INSERT INTO stability_stats VALUES ({','.join('?' * len(rows[0]))})", rows)
    con.commit()
    log(con, "analyze_stability", True, f"{len(rows)}장, 기준일 {end_s}")

    by_status = defaultdict(int)
    for r in rows:
        by_status[r[-2]] += 1
    print(f"[stability] 기준일 {end_s} / {len(rows)}장: " + ", ".join(f"{k} {v}" for k, v in by_status.items()))

    sel = con.execute(
        """SELECT t.*, k.player_name, k.class_name, k.grade, k.ovr, p.current_price
           FROM stability_stats t JOIN card k USING (spid, grade) JOIN price_status p USING (spid, grade)
           WHERE p.status='ok' AND p.current_price BETWEEN ? AND ?""", (lo, hi)).fetchall()
    okr = [r for r in sel if r["l_cv"] is not None]
    print(f"\n[{preset}] {len(sel)}장 (30일 지표 계산 가능 {len(okr)}장)")
    if okr:
        cvs = sorted(r["l_cv"] for r in okr)
        q = lambda p: cvs[min(len(cvs) - 1, int(p * len(cvs)))]
        print(f"  30일 CV 분포: 하위25% {q(.25):.3f} / 중앙 {median(cvs):.3f} / 상위25% {q(.75):.3f}")
        print(f"  30일 추세: 상승 {sum(r['l_trend']=='상승' for r in okr)} / 보합 {sum(r['l_trend']=='보합' for r in okr)}"
              f" / 하락 {sum(r['l_trend']=='하락' for r in okr)}")
    print(f"  최근 7일 급락(하루 -{cfg['sharpMovePct']}% 이상) {sum(r['sharp_drop'] for r in sel)}장,"
          f" 급등 {sum(r['sharp_rise'] for r in sel)}장, 지속 하락 {sum(r['persistent_decline'] for r in sel)}장")
    gaps = [r for r in sel if r["l_coverage"] is not None and r["l_coverage"] < 1]
    print(f"  30일 중 빠진 날이 있는 카드 {len(gaps)}장")
    print(f"  가격 정체(같은 값 {cfg['staleDays']}일 이상 연속) {sum(r['stale'] for r in sel)}장 → 안정 목록에서 제외")

    def line(r):
        return (f"  {r['player_name']} {r['class_name'].split(' (')[0]} {r['grade']}강 | 현재 {eok(r['current_price'])}"
                f" | CV 7일 {r['s_cv']:.3f} 30일 {r['l_cv']:.3f} | 30일 변동 {pct(r['l_change_pct'])}"
                f" 추세 {r['l_trend']}({pct(r['l_trend_pct_per_day'])}/일)"
                f" | 7일 최대 하루 하락 {pct(r['max_daily_drop_pct'])}")

    print("\n30일 CV 가장 낮은(안정적인) 10장 — 가격 정체·급변 카드 제외")
    for r in sorted([r for r in okr if r["s_cv"] is not None and not r["stale"]
                     and not r["sharp_drop"] and not r["sharp_rise"]], key=lambda r: r["l_cv"])[:10]:
        print(line(r))
    print("\n최근 7일 급락 카드 (하락 폭 큰 순) 8장")
    for r in sorted([r for r in sel if r["sharp_drop"] and r["s_cv"] is not None and r["l_cv"] is not None],
                    key=lambda r: r["max_daily_drop_pct"])[:8]:
        print(line(r))
    print("\n지속 하락 카드 (30일 변동 큰 순) 5장")
    for r in sorted([r for r in sel if r["persistent_decline"] and r["s_cv"] is not None],
                    key=lambda r: r["l_change_pct"])[:5]:
        print(line(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
