"""9·10단계: 투자 자금 배분 (순수 계산).

  총 운용 BP     = 순 BP(현금) + 보유 매물 현재 평가액            (호출하는 쪽에서 계산해 total_bp 로 넘김)
  카드 한도      = 총 운용 BP × maxPerCardRatio − 그 카드를 이미 보유한 평가액
  추천 수량      = floor( min(카드 한도, 남은 순 BP) / 1장당 매수가 ), 단 max_qty(카드당 최대 수량) 이하
                   (max_qty 는 거래량 데이터가 없어 한 카드를 많이 사고팔 수 있는지 확인할 수 없어 둔 제한. None = 없음)
  총 매수 비용   = 수량 × 1장당 매수가                             → 순 BP 에서만 차감
  예상 순수익    = 수량 × (예상 매도가 × (1 − 수수료율) − 1장당 매수가)
  투자 비중      = 총 매수 비용 / 총 운용 BP
후보 순서: '지금 매수' (Deal Score 순) → '목표가 주문' (Deal Score 순). 목표가 주문은 체결 전 계획이지만 BP 를 미리 떼어 둔다.
"""
import math


def allocate(cands: list[dict], total_bp: float, cash_bp: float, max_ratio: float, fee: float,
             max_qty: int | None = None, held_value: dict | None = None) -> dict:
    held_value = held_value or {}
    cap_total = total_bp * max_ratio
    remaining = cash_bp
    rows = []
    for x in cands:
        price = x["unit_price"]
        held = held_value.get(x["key"] if not isinstance(x["key"], list) else tuple(x["key"]), 0)
        cap = max(0.0, cap_total - held)
        budget = min(cap, remaining)
        qty = math.floor(budget / price) if price > 0 else 0
        note = ""
        if max_qty is not None and qty > max_qty:
            qty, note = int(max_qty), f"카드당 최대 수량 {int(max_qty)}장"
        cost = qty * price
        if qty == 0:
            if held and cap < price:
                note = "이미 보유 — 카드당 한도 소진"
            elif price > cap_total:
                note = "1장 가격이 카드당 한도보다 큼"
            else:
                note = "순 BP 부족"
        remaining -= cost
        unit_net = x["sell_expected"] * (1 - fee) - price
        rows.append(dict(x, qty=qty, total_cost=cost, unit_net=unit_net, net_total=qty * unit_net,
                         weight=cost / total_bp if total_bp else 0, remaining_after=remaining,
                         held_value=held, note=note))
    return {"total_bp": total_bp, "cash_bp": cash_bp, "cap_per_card": cap_total, "rows": rows,
            "invested": cash_bp - remaining, "remaining": remaining}


def allocate_rankerpick(cands: list[dict], total_bp: float, cash_bp: float, max_ratio: float,
                        fee: float, fee_coupon: float | None, coupons: int, min_roi: float,
                        max_qty: int | None, transfers_per_unit: int, held_value: dict | None = None) -> dict:
    """랭커 픽 추천 순서대로 자금 배분.

    후보 = 랭커 픽 추천 카드 (점수 높은 순). 1장당 매수가 = 현재가, 예상 매도가 = 지난 주말 평균.
    카드마다:
      수수료 20% 로 1장 순익 ≥ 매수가 × 목표 수익률  → 쿠폰 없이 매수
      아니면 쿠폰(남은 장수 안에서, 1장 판매에 1장)을 쓰면 넘는 경우 → 쿠폰 쓰는 매수 (수량 ≤ 남은 쿠폰)
      둘 다 아니면 건너뜀 (사유 기록)
    수량 = floor(min(카드 한도, 남은 순 BP) / 매수가), 단
           ≤ max_qty  그리고  ≤ max(1, 2시간 거래 // transfers_per_unit)  (거래가 적은 카드는 조금만)
    """
    held_value = held_value or {}
    cap_total = total_bp * max_ratio
    remaining, coupons_left = cash_bp, coupons
    rows, skipped = [], []
    for x in cands:
        price, sell = x["unit_price"], x["sell_expected"]
        if not price or not sell:
            continue
        need = price * min_roi
        net_plain = sell * (1 - fee) - price
        net_coupon = sell * (1 - fee_coupon) - price if fee_coupon is not None else None
        if net_plain >= need:
            use_coupon, unit_net = False, net_plain
        elif net_coupon is not None and net_coupon >= need and coupons_left > 0:
            use_coupon, unit_net = True, net_coupon
        else:
            skipped.append(dict(x, reason="쿠폰을 다 써서 목표 수익률 미달" if net_coupon is not None and net_coupon >= need
                                else "주말 가격에 팔아도 목표 수익률 미달"))
            continue
        held = held_value.get(tuple(x["key"]), 0)
        budget = min(max(0.0, cap_total - held), remaining)
        qty = math.floor(budget / price)
        vol_cap = max(1, int(x.get("transfers_2h") or 0) // max(1, transfers_per_unit))
        caps = [(vol_cap, f"2시간 거래 {x.get('transfers_2h')}건 → 최대 {vol_cap}장")]
        if max_qty is not None:
            caps.append((int(max_qty), f"카드당 최대 {int(max_qty)}장"))
        if use_coupon:
            caps.append((coupons_left, f"남은 쿠폰 {coupons_left}장"))
        note = ""
        for c, why in caps:
            if qty > c:
                qty, note = c, why
        if qty <= 0:
            skipped.append(dict(x, reason="이미 보유 — 카드당 한도 소진" if held else
                                ("1장 가격이 카드당 한도보다 큼" if price > cap_total else "순 BP 부족")))
            continue
        cost = qty * price
        remaining -= cost
        if use_coupon:
            coupons_left -= qty
        rows.append(dict(x, qty=qty, total_cost=cost, unit_net=unit_net, net_total=qty * unit_net,
                         coupon=use_coupon, weight=cost / total_bp if total_bp else 0,
                         remaining_after=remaining, held_value=held, note=note))
    return {"total_bp": total_bp, "cash_bp": cash_bp, "cap_per_card": cap_total, "rows": rows, "skipped": skipped,
            "invested": cash_bp - remaining, "remaining": remaining, "coupons_used": coupons - coupons_left,
            "coupons_left": coupons_left}
