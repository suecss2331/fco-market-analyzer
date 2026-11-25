"""분석 재계산과 추천 기록 (대시보드 설정 저장 · 자동 갱신이 함께 쓴다)."""
import contextlib
import io

from . import analyze_deal, analyze_popularity, analyze_pricing, analyze_stability, analyze_weekly
from .config import active_price_range, load_settings
from .db import now_kst, today_kst


def rerun_analysis() -> None:
    """4~8단계를 다시 계산한다 (수 초). 수집은 하지 않는다."""
    with contextlib.redirect_stdout(io.StringIO()):
        for mod in (analyze_weekly, analyze_popularity, analyze_stability, analyze_pricing, analyze_deal):
            mod.main([])


def log_recommendations(con) -> int:
    """오늘의 추천을 rec_log 에 남긴다 (카드별 하루 1번, 처음 기록이 유지된다).
    대상: 활성 가격대의 '지금 매수 가능' 전부 + Deal Score 상위 recommendationLogTopN (목표가 대기·위험 포함)."""
    s = load_settings()
    lo, hi, _ = active_price_range(s)
    top_n = int(s["refresh"]["recommendationLogTopN"])
    fee = float(s["fee"]["sellFeeRate"])
    rows = con.execute(
        """SELECT p.spid, p.grade, p.verdict, d.deal_score, p.current_price, p.target_buy, p.sell_expected
           FROM pricing_stats p JOIN deal_stats d USING (spid, grade)
           WHERE p.current_price BETWEEN ? AND ? AND p.verdict <> 'insufficient'
           ORDER BY d.deal_score DESC""", (lo, hi)).fetchall()
    pick = [r for r in rows if r["verdict"] == "buy_now"] + list(rows[:top_n])
    day, ts = today_kst(), now_kst()
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO rec_log (day, spid, grade, verdict, deal_score, current_price, target_buy, "
        "sell_expected, fee_rate, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(day, r["spid"], r["grade"], r["verdict"], r["deal_score"], r["current_price"], r["target_buy"],
          r["sell_expected"], fee, ts) for r in pick])
    con.commit()
    return con.total_changes - before
