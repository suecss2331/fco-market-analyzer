"""매수 · 관망 · 매도 판단 (모바일 표시용, 순수 계산). 기존 계산식만 조합한다.

공통: 수수료율 fee (평소 20%), 쿠폰 수수료율 fee_c (가장 큰 보유 쿠폰, 없으면 None), 목표 수익률 r (설정, 5%)

매수 (보유하지 않은 카드)
  예상 판매가 = 지난 주말(토·일) 일별 가격 평균                         (랭커 픽 기준)
  적정 매수가 = 예상 판매가 × (1 − fee) ÷ (1 + r)                         (기존 '목표 매수가' 식과 같은 꼴)
  쿠폰 적정 매수가 = 예상 판매가 × (1 − fee_c) ÷ (1 + r)
  예상 순이익 = 예상 판매가 × (1 − 수수료) − 현재가
  매수: 랭커 픽 추천 조건 충족 + 현재가 ≤ 적정 매수가 (안 되면 쿠폰 적정 매수가 이하일 때 '쿠폰 사용 시')
  관망: 그 밖 (이유 표시)

매도 (보유 카드)
  목표 매도가 = 매수가 × (1 + r) ÷ (1 − fee)          (쿠폰 있으면 ÷ (1 − fee_c) 도 함께)
  매도: 상한가 ≥ 목표 매도가 (상한가에 구매 대기가 있으면 바로 팔림)
  관망: 그 밖 (목표까지 남은 금액)
"""


def fmt_bp(v: float) -> str:
    """BP 를 화면과 같은 꼴로 (1.52억 / 4,200만)."""
    a = abs(v)
    s = "−" if v < 0 else ""
    if a >= 1e8:
        return f"{s}{a / 1e8:.2f}억"
    return f"{s}{a / 1e4:,.0f}만"


def judge_buy(price, sell, fee: float, fee_c, r: float, recommended: bool, why_not: str | None = None) -> dict:
    out = {"verdict": "관망", "reason": "", "sell": sell, "buy_fair": None, "buy_fair_coupon": None,
           "net": None, "roi": None, "net_coupon": None, "roi_coupon": None}
    if not price:
        out["reason"] = "현재가 없음"
        return out
    if not sell:
        out["reason"] = "지난 주말 가격 없음"
        return out
    out["buy_fair"] = sell * (1 - fee) / (1 + r)
    out["net"] = sell * (1 - fee) - price
    out["roi"] = out["net"] / price * 100
    if fee_c is not None:
        out["buy_fair_coupon"] = sell * (1 - fee_c) / (1 + r)
        out["net_coupon"] = sell * (1 - fee_c) - price
        out["roi_coupon"] = out["net_coupon"] / price * 100
    if not recommended:
        out["reason"] = why_not or "랭커 픽 추천 조건 미충족"
    elif price <= out["buy_fair"]:
        out["verdict"], out["reason"] = "매수", f"수수료 후 순이익 {out['roi']:.1f}% (목표 {r * 100:.0f}% 이상)"
    elif out["buy_fair_coupon"] is not None and price <= out["buy_fair_coupon"]:
        out["verdict"], out["reason"] = "매수", f"쿠폰 쓰면 순이익 {out['roi_coupon']:.1f}% (수수료 그대로면 {out['roi']:.1f}%)"
    else:
        out["reason"] = "인기·거래 조건은 충족, 수수료 후 수익 부족 — 적정 매수가 이하에서"
    return out


def judge_sell(buy: float, upper, fee: float, fee_c, r: float, buy_at_upper: int | None = None) -> dict:
    out = {"verdict": "관망", "reason": "", "target_sell": buy * (1 + r) / (1 - fee),
           "target_sell_coupon": buy * (1 + r) / (1 - fee_c) if fee_c is not None and fee_c < 1 else (buy * (1 + r) if fee_c is not None else None)}
    if not upper:
        out["reason"] = "상한가 정보 없음"
        return out
    if upper >= out["target_sell"]:
        out["verdict"] = "매도"
        out["reason"] = f"상한가에 팔면 수수료 후 +{(upper * (1 - fee) - buy) / buy * 100:.1f}%"
    elif out["target_sell_coupon"] is not None and upper >= out["target_sell_coupon"]:
        out["verdict"] = "매도"
        out["reason"] = f"쿠폰 쓰고 상한가에 팔면 +{(upper * (1 - fee_c) - buy) / buy * 100:.1f}%"
    else:
        tgt = out["target_sell_coupon"] if out["target_sell_coupon"] is not None else out["target_sell"]
        out["reason"] = f"상한가가 목표 매도가보다 {fmt_bp(tgt - upper)} 낮음"
    if out["verdict"] == "매도" and buy_at_upper == 0:
        out["reason"] += " · 상한가 구매 대기 0건이라 바로 안 팔릴 수 있음"
    return out
