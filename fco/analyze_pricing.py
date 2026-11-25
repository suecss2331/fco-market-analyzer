"""7단계 실행: 매수 적정가 · 예상 매도가 · 수수료 차감 수익을 계산해 pricing_stats 에 저장한다.
먼저 analyze_weekly, analyze_stability 가 실행되어 있어야 한다.

추가 검증 (도달 가능성): 최근 reachLookbackDays 일 동안 일별 가격이 목표 매수가 이하였던 날 수.
  일별 가격은 하루 평균이라 장중 최저가는 더 낮을 수 있다 → 0 일이어도 '불가능'이 아니라 '평균으로는 미도달'.

실행:  python -m fco.analyze_pricing [--preset 10억-100억]
"""
import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from .config import load_settings, active_price_range
from .db import connect, log, now_kst
from .pricing import compute_pricing


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
    cfg = settings["buyPrice"]
    fee = float(settings["fee"]["sellFeeRate"])
    look = int(cfg["reachLookbackDays"])
    lo, hi, preset = active_price_range(settings)
    con = connect()

    weekly = {(r["spid"], r["grade"]): dict(r) for r in con.execute("SELECT * FROM weekly_stats")}
    stab = {(r["spid"], r["grade"]): dict(r) for r in con.execute("SELECT * FROM stability_stats")}
    if not weekly or not stab:
        raise SystemExit("먼저 python -m fco.analyze_weekly 와 python -m fco.analyze_stability 를 실행하세요.")
    end = date.fromisoformat(con.execute("SELECT MAX(day) FROM price_daily").fetchone()[0])
    since = (end - timedelta(days=look + 30)).isoformat()  # 카드별 기준일이 달라도 충분히 넉넉하게 읽는다
    by_card = defaultdict(list)
    for r in con.execute("SELECT spid, grade, day, price FROM price_daily WHERE day >= ? ORDER BY day", (since,)):
        by_card[(r["spid"], r["grade"])].append((date.fromisoformat(r["day"]), r["price"]))
    recent, last_daily = {}, {}
    for k, pts in by_card.items():
        card_end = pts[-1][0]  # 카드별 기준일 = 그 카드의 마지막 일별 가격 날짜
        recent[k] = [p for d, p in pts if d > card_end - timedelta(days=look)]
        last_daily[k] = pts[-1][1]
    sharp = float(settings["stability"]["sharpMovePct"])

    ts = now_kst()
    rows = []
    for k, w in weekly.items():
        s = dict(stab.get(k, {}), last_daily_price=last_daily.get(k), sharp_move_pct=sharp)
        p = compute_pricing(w, s, cfg, fee)
        prices = recent.get(k, [])
        reach = None if p["target_buy"] is None else sum(1 for x in prices if x <= p["target_buy"])
        rows.append((k[0], k[1], ts, fee, float(cfg["minTargetRoi"]),
                     p["current_price"], p["basis_price"], p["factor"], p["buy_fair"], p["premium_used_pct"],
                     p["sell_expected"], p["fee_amount"], p["net_at_current"], p["roi_at_current_pct"],
                     p["break_even_sell_at_current"], p["max_buy_for_target"], p["target_buy"],
                     p["net_at_target"], p["roi_at_target_pct"], p["required_discount_pct"],
                     reach, look, min(prices) if prices else None,
                     p["current_vs_last_daily_pct"], ",".join(p["risk_flags"]), p["verdict"]))
    con.execute("DROP TABLE IF EXISTS pricing_stats")  # 매번 전체 재계산하는 표 (열 변경 반영)
    con.close()
    con = connect()
    con.executemany(f"INSERT INTO pricing_stats VALUES ({','.join('?' * len(rows[0]))})", rows)
    con.commit()
    log(con, "analyze_pricing", True, f"{len(rows)}장, 수수료 {fee:.0%}, 목표 ROI {cfg['minTargetRoi']:.0%}")

    sel = con.execute(
        """SELECT p.*, k.player_name, k.class_name, k.grade AS g, w.wd_mean, w.we_mean, w.weeks_positive,
                  w.weeks_total, t.l_cv, t.sharp_drop, t.persistent_decline, t.stale
           FROM pricing_stats p JOIN card k USING (spid, grade)
           JOIN weekly_stats w USING (spid, grade) JOIN stability_stats t USING (spid, grade)
           WHERE p.current_price BETWEEN ? AND ?""", (lo, hi)).fetchall()
    ok = [r for r in sel if r["verdict"] != "insufficient"]
    print(f"[pricing] 수수료 {fee:.0%} / 최소 목표 ROI {cfg['minTargetRoi']:.0%} / 기준 {cfg['basis']}"
          f" / 할인 계수 {cfg['discountFactorMin']}~{cfg['discountFactorMax']}")
    print(f"  본전 조건: 매도가 ≥ 매수가 × {1 / (1 - fee):.3f}  (산 값보다 {1 / (1 - fee) - 1:.0%} 이상 비싸게 팔아야 본전)")
    print(f"\n[{preset}] {len(sel)}장 (계산 가능 {len(ok)}장)")
    print(f"  현재가로 사서 예상 매도가에 팔 때 ROI 중앙값 {pct(median(r['roi_at_current_pct'] for r in ok))}")
    print(f"  사용 주말 프리미엄 중앙값 {pct(median(r['premium_used_pct'] for r in ok))},"
          f" 0보다 큰 카드 {sum(1 for r in ok if r['premium_used_pct'] > 0)}장")
    print(f"  목표 매수가까지 필요한 할인(평일 평균 대비) 중앙값 {median(r['required_discount_pct'] for r in ok):.1f}%")
    print(f"  판정: 지금 매수 가능 {sum(r['verdict']=='buy_now' for r in ok)}장 / "
          f"위험(급락·지속하락·정체, 회복 가정) {sum(r['verdict']=='risky' for r in ok)}장 / "
          f"목표가 대기 {sum(r['verdict']=='wait' for r in ok)}장")
    reach = [r for r in ok if r["reach_days"]]
    print(f"  최근 {look}일 중 일별 가격이 목표 매수가 이하였던 날이 있는 카드 {len(reach)}장")

    def line(r):
        names = {"sharp_drop": "급락", "persistent_decline": "지속하락", "stale": "정체", "current_gap": "당일급락"}
        flags = [names[f] for f in (r["risk_flags"] or "").split(",") if f]
        return (f"  {r['player_name']} {r['class_name'].split(' (')[0]} {r['g']}강 | 현재 {eok(r['current_price'])}"
                f" 평일avg {eok(r['wd_mean'])} | 적정가 {eok(r['buy_fair'])}(×{r['factor']:.3f}) 목표가 {eok(r['target_buy'])}"
                f" | 예상매도 {eok(r['sell_expected'])}(+{r['premium_used_pct']:.1f}%) 수수료 {eok(r['fee_amount'])}"
                f" | 현재가 매수 시 순익 {eok(r['net_at_current'])} ROI {pct(r['roi_at_current_pct'])}"
                f" | 목표가 도달 {r['reach_days']}/{r['reach_lookback']}일"
                f" | 어제 대비 현재가 {pct(r['current_vs_last_daily_pct'])}" + (f" | ⚠ {','.join(flags)}" if flags else ""))

    buy = sorted([r for r in ok if r["verdict"] == "buy_now"], key=lambda r: -r["roi_at_current_pct"])
    print(f"\n지금 매수 가능 (위험 표시 없음, ROI 높은 순) {min(10, len(buy))}장")
    for r in buy[:10]:
        print(line(r))
    risky = sorted([r for r in ok if r["verdict"] == "risky"], key=lambda r: -r["roi_at_current_pct"])
    print(f"\n위험 판정 예시 (계산상 ROI 는 높지만 급락 후 회복을 가정) 3장")
    for r in risky[:3]:
        print(line(r))
    near = sorted([r for r in ok if r["verdict"] == "wait"], key=lambda r: r["current_price"] / r["target_buy"])
    print("\n목표 매수가에 가장 가까운 대기 카드 8장 (현재가 / 목표가 비율 순)")
    for r in near[:8]:
        print(line(r) + f" | 현재가가 목표가의 {r['current_price'] / r['target_buy']:.2f}배")
    return 0


if __name__ == "__main__":
    sys.exit(main())
