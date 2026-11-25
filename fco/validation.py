"""10단계: 추천 매물의 실제 수익률 검증 (순수 계산).

추천 기록 1건 (추천한 날, 판정, 그날 현재가, 목표 매수가) 을 그 뒤 horizon 일의 일별 가격으로 채점한다.
  매수 시점
    buy_now : 추천한 날 현재가로 산 것으로 본다
    wait    : 추천 다음 날부터 horizon 일 안에 일별 가격이 목표 매수가 이하인 첫날, 목표 매수가로 산 것으로 본다
              (도달하지 않으면 'not_filled' — 손익 없음)
  매도 시점 : 매수 다음 날 이후 첫 주말(토·일) 일별 가격 평균 (평일 매수 → 주말 매도 전략)
  ROI       = 매도가 × (1 − 수수료율) / 매수가 − 1
추천일 + horizon 일이 아직 지나지 않았으면 'pending', 필요한 일별 가격이 없으면 'no_data'.
일별 가격은 하루 평균이므로 실제 체결가와 다를 수 있다 (근사 검증).
"""
from datetime import date, timedelta


def evaluate_recommendation(rec: dict, daily: dict, fee: float, horizon: int, today: date) -> dict:
    d0 = date.fromisoformat(rec["day"])
    out = {"status": "pending", "entry_price": None, "filled_on": None, "exit_price": None, "roi_pct": None}
    if today <= d0 + timedelta(days=horizon):
        return out
    if rec["verdict"] == "buy_now":
        entry, filled = rec["current_price"], d0
    else:
        entry = filled = None
        for i in range(1, horizon + 1):
            d = d0 + timedelta(days=i)
            if d in daily and daily[d] <= rec["target_buy"]:
                entry, filled = rec["target_buy"], d
                break
        if entry is None:
            out["status"] = "not_filled"
            return out
    sat = filled + timedelta(days=(5 - filled.weekday()) % 7 or 7)  # 매수일 다음의 첫 토요일
    wk =[daily[d] for d in (sat, sat + timedelta(days=1)) if d in daily]
    out.update(entry_price=entry, filled_on=filled.isoformat())
    if not wk or not entry:
        out["status"] = "no_data"
        return out
    exit_p = sum(wk) / len(wk)
    out.update(status="evaluated", exit_price=exit_p, roi_pct=(exit_p * (1 - fee) / entry - 1) * 100)
    return out


def next_weekend(d0: date) -> tuple[date, date]:
    """d0 다음의 첫 토·일 (d0 가 토요일이면 다음 주 토요일)."""
    sat = d0 + timedelta(days=(5 - d0.weekday()) % 7 or 7)
    return sat, sat + timedelta(days=1)


def evaluate_rankerpick(rec: dict, daily: dict, fee: float, fee_coupon: float | None, today: date) -> dict:
    """랭커 픽 추천 1건 채점.

    매수 = 추천된 날의 현재가 (그날 처음 추천됐을 때 값)
    매도 = 추천일 다음 첫 주말(토·일) 일별 가격 평균
    ROI(수수료)  = 매도가 × (1 − 수수료율) / 매수가 − 1
    ROI(쿠폰)    = 매도가 × (1 − 쿠폰 적용 수수료율) / 매수가 − 1   (추천 당시 쓸 수 있던 가장 큰 쿠폰)
    그 일요일 일별 가격이 나오기 전(일요일 당일까지)은 'pending', 주말 가격이 하나도 없으면 'no_data'.
    """
    d0 = date.fromisoformat(rec["day"])
    sat, sun = next_weekend(d0)
    out = {"status": "pending", "sell_weekend": sat.isoformat(), "entry_price": rec["current_price"],
           "exit_price": None, "roi_pct": None, "roi_coupon_pct": None}
    if today <= sun:
        return out
    wk = [daily[d] for d in (sat, sun) if d in daily and daily[d]]
    entry = rec["current_price"]
    if not wk or not entry:
        out["status"] = "no_data"
        return out
    exit_p = sum(wk) / len(wk)
    out.update(status="evaluated", exit_price=exit_p, roi_pct=(exit_p * (1 - fee) / entry - 1) * 100,
               roi_coupon_pct=(exit_p * (1 - fee_coupon) / entry - 1) * 100 if fee_coupon is not None else None)
    return out
