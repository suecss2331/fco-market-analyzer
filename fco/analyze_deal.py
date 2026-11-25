"""8단계 실행: 4~7단계 결과를 모아 Deal Score 를 계산하고 deal_stats 에 저장한다.
먼저 analyze_weekly, analyze_popularity, analyze_stability, analyze_pricing 이 실행되어 있어야 한다.

가격대 그룹은 현재가 기준 고정 구간이므로 가격대 프리셋을 바꿔도 점수는 그대로다 (보이는 카드만 달라짐).

실행:  python -m fco.analyze_deal [--preset 10억-100억] [--top 20]
"""
import argparse
import json
import sys

from .config import load_settings, active_price_range
from .db import connect, log, now_kst
from .deal import compute_deal_scores

VERDICT = {"buy_now": "지금 매수 가능", "risky": "위험(회복 가정)", "wait": "목표가 대기", "insufficient": "데이터 부족"}


def eok(v) -> str:
    return "-" if v is None else f"{v / 1e8:,.2f}억"


def pct(v) -> str:
    return "-" if v is None else f"{v:+.1f}%"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--blend", type=float, help="popularityBlendTop200 미리보기 (저장값은 바뀜)")
    ap.add_argument("--buyable", action="store_true", help="판정이 '지금 매수 가능'인 카드만")
    args = ap.parse_args(argv)
    settings = load_settings()
    if args.preset:
        settings["priceRange"]["activePreset"] = args.preset
    cfg = settings["dealScore"]
    if args.blend is not None:
        cfg["popularityBlendTop200"] = args.blend
    fee = float(settings["fee"]["sellFeeRate"])
    lo, hi, preset = active_price_range(settings)
    con = connect()

    rows = con.execute(
        """SELECT k.spid, k.grade, p.current_price, p.buy_fair, p.roi_at_current_pct, p.premium_used_pct,
                  p.risk_flags, w.status AS weekly_status, w.discount_pct, w.transient,
                  t.l_cv, t.stale, q.score AS pop_score, q.users_top_universe, q.drop_alert, s.days AS price_days
           FROM card k
           JOIN pricing_stats p USING (spid, grade)
           JOIN weekly_stats w USING (spid, grade)
           JOIN stability_stats t USING (spid, grade)
           JOIN popularity_stats q USING (spid, grade)
           JOIN price_status s USING (spid, grade)
           WHERE s.status = 'ok' AND p.current_price IS NOT NULL""").fetchall()
    if not rows:
        raise SystemExit("4~7단계 결과가 없습니다. analyze_weekly / popularity / stability / pricing 을 먼저 실행하세요.")
    cards = [dict(r, key=(r["spid"], r["grade"])) for r in rows]
    res = compute_deal_scores(cards, cfg, fee)

    ts = now_kst()
    out = []
    for (spid, grade), r in res.items():
        parts = r["parts"]
        out.append((spid, grade, ts, r["group"],
                    parts["popularity"]["points"], parts["discount"]["points"], parts["weekend"]["points"],
                    parts["stability"]["points"], r["base"], r["penalty"], r["final"],
                    r["reliability_pct"], r["reliability"], ",".join(r["warnings"]),
                    json.dumps(parts, ensure_ascii=False)))
    con.execute("DELETE FROM deal_stats")
    con.executemany(f"INSERT INTO deal_stats VALUES ({','.join('?' * 15)})", out)
    con.commit()
    log(con, "analyze_deal", True, f"{len(out)}장 Deal Score 계산")

    w = cfg["weights"]
    print(f"[deal] 배점: 인기 {w['popularity']} / 할인 {w['discount']} / 주말 {w['weekend']} / 안정 {w['stability']}"
          f" · 위험 감점 {cfg['riskPenalty']} · 가격대 그룹 백분위")
    sel = con.execute(
        """SELECT d.*, k.player_name, k.class_name, k.ovr, k.main_position, p.current_price, p.buy_fair,
                  p.target_buy, p.sell_expected, p.net_at_current, p.roi_at_current_pct, p.verdict,
                  w.discount_pct, p.premium_used_pct, t.l_cv, q.score AS pop_score, q.ranks,
                  q.users_top_universe AS users
           FROM deal_stats d JOIN card k USING (spid, grade) JOIN pricing_stats p USING (spid, grade)
           JOIN weekly_stats w USING (spid, grade) JOIN stability_stats t USING (spid, grade)
           JOIN popularity_stats q USING (spid, grade)
           WHERE p.current_price BETWEEN ? AND ? ORDER BY d.deal_score DESC""", (lo, hi)).fetchall()
    rel = {}
    for r in sel:
        rel[r["reliability"]] = rel.get(r["reliability"], 0) + 1
    groups = {}
    for r in sel:
        groups[r["price_group"]] = groups.get(r["price_group"], 0) + 1
    print(f"\n[{preset}] {len(sel)}장 · 데이터 신뢰도 {rel} · 그룹 {groups}")

    def f1(v):
        return "  - " if v is None else f"{v:4.1f}"

    if args.buyable:
        sel = [r for r in sel if r["verdict"] == "buy_now"]
    print(f"  판정별: " + ", ".join(f"{VERDICT[v]} {sum(1 for r in sel if r['verdict'] == v)}" for v in VERDICT))
    print(f"\nDeal Score TOP {args.top}" + (" (지금 매수 가능만)" if args.buyable else "")
          + f" · 인기 지표 1~200위 혼합 비율 {cfg.get('popularityBlendTop200', 0)}")
    print("  순위 점수(기본-감점) | 카드 | 인기/할인/주말/안정 점수 | 근거 | 가격 | 판정 | 경고")
    for i, r in enumerate(sel[: args.top], 1):
        print(f"  {i:2d}. {r['deal_score']:5.1f} ({r['base_score']:.1f}-{r['penalty']:.0f})"
              f" | {r['player_name']} {r['class_name'].split(' (')[0]} {r['grade']}강 OVR{r['ovr']} {r['main_position']}"
              f" | {f1(r['pop_points'])}/{f1(r['discount_points'])}/{f1(r['weekend_points'])}/{f1(r['stability_points'])}"
              f" | 인기 {r['pop_score']:.1f}점({r['ranks'] or '-'}) 1~200위 {r['users']}명 할인 {pct(r['discount_pct'])}"
              f" 주말 {pct(r['premium_used_pct'])} CV {r['l_cv']:.3f}"
              f" | 현재 {eok(r['current_price'])} 목표 {eok(r['target_buy'])} 매도 {eok(r['sell_expected'])}"
              f" 순익 {eok(r['net_at_current'])} ROI {pct(r['roi_at_current_pct'])}"
              f" | {VERDICT[r['verdict']]} | {r['warnings'] or '-'} [신뢰 {r['reliability']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
