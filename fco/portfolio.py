"""10단계: 보유 매물 평가 · 실현 손익 (순수 계산).

보유 1건(lot) = 같은 카드를 같은 가격에 산 묶음.
  매수 원가          = 수량 × 1장당 매수가
  현재 평가액        = 수량 × 현재가
  수수료 차감 회수액 = 수량 × 현재가 × (1 − 수수료율)   (지금 팔면 실제로 받는 BP)
  평가 손익          = 수수료 차감 회수액 − 매수 원가
  손익분기 매도가    = 1장당 매수가 / (1 − 수수료율)
  예상 매도가 매도 시 손익 = 수량 × (예상 매도가 × (1 − 수수료율) − 1장당 매수가)
  직전 갱신 대비     = (현재가 − 직전 관측 현재가) / 직전 관측 현재가
실현 손익 (매도 기록) = 수량 × (매도가 × (1 − 수수료율) − 매수가)
"""


def lot_metrics(qty: int, buy_price: float, current: float | None, fee: float,
                sell_expected: float | None, prev_price: float | None) -> dict:
    cost = qty * buy_price
    out = {"cost": cost, "break_even_sell": buy_price / (1 - fee),
           "market_value": None, "liquidation_value": None, "unrealized": None, "unrealized_roi_pct": None,
           "profit_at_expected": None, "change_vs_prev_pct": None}
    if current:
        liq = qty * current * (1 - fee)
        out.update(market_value=qty * current, liquidation_value=liq, unrealized=liq - cost,
                   unrealized_roi_pct=(liq - cost) / cost * 100 if cost else None)
        if prev_price:
            out["change_vs_prev_pct"] = (current - prev_price) / prev_price * 100
    if sell_expected:
        out["profit_at_expected"] = qty * (sell_expected * (1 - fee) - buy_price)
    return out


def limit_metrics(qty: int, buy_price: float, upper: float | None, lower: float | None,
                  fee: float, fee_coupon: float | None) -> dict:
    """이적시장 상한가·하한가 기준 손익.

    상한가 매도 손익        = 수량 × (상한가 × (1 − 수수료율) − 매수가)
    상한가 매도 손익(쿠폰)  = 수량 × (상한가 × (1 − 쿠폰 적용 수수료율) − 매수가)
    하한가 매도 손익        = 수량 × (하한가 × (1 − 수수료율) − 매수가)   (급하게 팔 때 최악)
    상한가까지 남은 차이    = 손익분기 매도가 − 상한가   (0 이하면 상한가 매도로 본전 이상)
    """
    out = {"profit_at_upper": None, "profit_at_upper_coupon": None, "profit_at_lower": None,
           "upper_gap_to_break_even": None, "upper_gap_to_break_even_coupon": None}
    if upper:
        out["profit_at_upper"] = qty * (upper * (1 - fee) - buy_price)
        out["upper_gap_to_break_even"] = buy_price / (1 - fee) - upper
        if fee_coupon is not None:
            out["profit_at_upper_coupon"] = qty * (upper * (1 - fee_coupon) - buy_price)
            out["upper_gap_to_break_even_coupon"] = (buy_price / (1 - fee_coupon) if fee_coupon < 1 else 0) - upper
    if lower:
        out["profit_at_lower"] = qty * (lower * (1 - fee) - buy_price)
    return out


def flip_roi(lower: float, upper: float, fee: float) -> float:
    """하한가에 사서 (같은 시세의) 상한가에 팔 때 ROI. 시세가 오르지 않으면 이 값이 한계."""
    return (upper * (1 - fee) - lower) / lower


def realized(qty: int, buy_price: float, sell_price: float, fee: float) -> dict:
    net = qty * (sell_price * (1 - fee) - buy_price)
    return {"net_profit": net, "roi_pct": net / (qty * buy_price) * 100 if buy_price else None,
            "fee_paid": qty * sell_price * fee}
