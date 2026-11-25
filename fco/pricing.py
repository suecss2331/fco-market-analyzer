"""7단계: 매수 적정가 · 예상 매도가 · 수수료 차감 수익 (순수 계산).

입력: 4단계 weekly_stats 한 행, 6단계 stability_stats 한 행, buyPrice 설정, 매도 수수료율.

  평일 기준가 (basis_price)  basis = "mean" 평일 평균 / "median" 평일 중앙값 / "min" 둘 중 작은 값
  할인 계수 (factor)          30일 CV 로 선형 결정. CV ≤ cvForMaxFactor → 0.98, CV ≥ cvForMinFactor → 0.90
  매수 적정가 (buy_fair)      = 평일 기준가 × 할인 계수

  사용 주말 프리미엄          = max(0, min(공식 주말 프리미엄, 추세 보정 주별 중앙값))
                               단 '일시적' 이거나 반복성 판단 보류(transient 없음)면 0
  예상 매도가 (sell_expected) = 평일 기준가 × (1 + 사용 주말 프리미엄)

  매도 수수료                 = 예상 매도가 × 수수료율
  순수익                      = 매도가 × (1 - 수수료율) - 매수가
  ROI (%)                     = 순수익 / 매수가 × 100
  손익분기 매도가             = 매수가 / (1 - 수수료율)
  목표 수익률 최대 매수가     = 예상 매도가 × (1 - 수수료율) / (1 + 최소 목표 수익률)
  목표 매수가 (target_buy)    = min(매수 적정가, 목표 수익률 최대 매수가)
  필요 할인율                 = (평일 기준가 - 목표 매수가) / 평일 기준가 × 100

  판정
    buy_now      현재가 ≤ 목표 매수가 (현재가로 사도 목표 수익률 달성) 이고 6단계 위험 표시 없음
    risky        현재가 ≤ 목표 매수가 이지만 급락 / 지속 하락 / 가격 정체 / 당일 급락 중 하나 이상
                 (당일 급락 current_gap: 현재가가 마지막 일별 가격보다 sharp_move_pct 이상 낮음 —
                  일별 가격은 어제까지라 6단계 급락 판정에 오늘 움직임이 빠지기 때문)
                 → 수익 계산이 '가격이 평일 평균으로 돌아온다'는 가정에 기대므로 매수 추천하지 않음
    wait         현재가 > 목표 매수가 → 목표 매수가를 제시
    insufficient 평일 평균 또는 30일 CV 를 계산할 수 없음
"""
RISK_FLAGS = ("sharp_drop", "persistent_decline", "stale")


def net_profit(buy: float, sell: float, fee: float) -> float:
    return sell * (1 - fee) - buy


def roi(buy: float, sell: float, fee: float) -> float:
    return net_profit(buy, sell, fee) / buy * 100


def break_even_sell(buy: float, fee: float) -> float:
    return buy / (1 - fee)


def max_buy_for_roi(sell: float, fee: float, target_roi: float) -> float:
    return sell * (1 - fee) / (1 + target_roi)


def discount_factor(cv: float | None, cfg: dict) -> float | None:
    if cv is None:
        return None
    hi, lo = float(cfg["discountFactorMax"]), float(cfg["discountFactorMin"])
    c0, c1 = float(cfg["cvForMaxFactor"]), float(cfg["cvForMinFactor"])
    t = min(1.0, max(0.0, (cv - c0) / (c1 - c0)))
    return hi - (hi - lo) * t


def compute_pricing(w: dict, s: dict, cfg: dict, fee: float) -> dict:
    out = {k: None for k in (
        "basis_price", "factor", "buy_fair", "premium_used_pct", "sell_expected", "fee_amount",
        "net_at_current", "roi_at_current_pct", "break_even_sell_at_current",
        "max_buy_for_target", "target_buy", "net_at_target", "roi_at_target_pct", "required_discount_pct")}
    out["current_price"] = w.get("current_price")
    out["risk_flags"] = [f for f in RISK_FLAGS if s.get(f)]
    out["current_vs_last_daily_pct"] = None
    last = s.get("last_daily_price")
    if last and out["current_price"]:
        gap = (out["current_price"] - last) / last * 100
        out["current_vs_last_daily_pct"] = gap
        if gap <= -float(s.get("sharp_move_pct", 15)):
            out["risk_flags"].append("current_gap")

    if w.get("status") != "ok" or w.get("wd_mean") is None or s.get("l_cv") is None:
        out["verdict"] = "insufficient"
        return out

    basis = cfg.get("basis", "mean")
    if basis == "median":
        base = w["wd_median"]
    elif basis == "min":
        base = min(w["wd_mean"], w["wd_median"])
    else:
        base = w["wd_mean"]
    f = discount_factor(s["l_cv"], cfg)

    prem = 0.0
    if w.get("transient") == 0 and w.get("premium_pct") is not None and w.get("weekly_premium_median_pct") is not None:
        prem = max(0.0, min(w["premium_pct"], w["weekly_premium_median_pct"]))
    sell = base * (1 + prem / 100)
    target_roi = float(cfg["minTargetRoi"])
    mb = max_buy_for_roi(sell, fee, target_roi)
    buy_fair = base * f
    target = min(buy_fair, mb)

    out.update(basis_price=base, factor=f, buy_fair=buy_fair, premium_used_pct=prem, sell_expected=sell,
               fee_amount=sell * fee, max_buy_for_target=mb, target_buy=target,
               net_at_target=net_profit(target, sell, fee), roi_at_target_pct=roi(target, sell, fee),
               required_discount_pct=(base - target) / base * 100)
    cur = w.get("current_price")
    if cur:
        out.update(net_at_current=net_profit(cur, sell, fee), roi_at_current_pct=roi(cur, sell, fee),
                   break_even_sell_at_current=break_even_sell(cur, fee))
        if cur > target:
            out["verdict"] = "wait"
        else:
            out["verdict"] = "risky" if out["risk_flags"] else "buy_now"
    else:
        out["verdict"] = "wait"
    return out
