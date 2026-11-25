"""판매 수수료 · 수수료 할인 쿠폰 (순수 계산).

수수료 규칙 (FC STATS 수수료 계산기 안내 기준, 게임 이벤트로 바뀔 수 있어 값은 설정에서 변경):
  기본 수수료 40%. 프리미엄 PC방 30%, TOP CLASS 20%, 쿠폰 X% 는 '기본 수수료를 깎는 비율' 이고 곱하지 않고 더한다.
  실효 수수료율 = 기본 × max(0, 1 − (상시 할인 합 + 쿠폰))
  예) PC방 + TOP CLASS = 50% 할인 → 20%,  여기에 30% 쿠폰 → 8%,  50% 쿠폰 → 0%

쿠폰 1장이 아끼는 BP = 판매가 × 기본 수수료 × 실효 쿠폰 할인
  실효 쿠폰 할인 = min(쿠폰, 1 − 상시 할인 합)   (수수료가 0 밑으로 내려가지 않으므로 그 이상은 버려짐)
→ 아끼는 BP 는 판매가에 비례한다. 그래서 '큰 쿠폰을 비싼 판매에' 짝지으면 총 절약이 가장 크다
  (판매가 × 할인율 곱의 합은 둘 다 큰 순서로 짝지을 때 최대 — 재배열 부등식).
쿠폰은 판매 1건(카드 1장)에 1장 쓰는 것으로 본다.
"""


def effective_fee(base: float, other: float, coupon: float = 0.0) -> float:
    return base * max(0.0, 1.0 - other - coupon)


def coupon_saving(price: float, base: float, other: float, coupon: float) -> float:
    return price * base * min(coupon, max(0.0, 1.0 - other))


def assign_coupons(sales: list[dict], coupons: list[dict], base: float, other: float) -> dict:
    """sales: [{key, unit_price, qty, ...}], coupons: [{discount, count}]
    판매 1장 단위로 펼쳐 비싼 순, 쿠폰을 큰 순으로 짝짓는다. 실효 할인이 0 이면 쓰지 않는다."""
    room = max(0.0, 1.0 - other)
    units = []
    for s in sales:
        units += [s] * int(s["qty"])
    units.sort(key=lambda s: -s["unit_price"])
    pool = []
    for c in coupons:
        pool += [float(c["discount"])] * int(c["count"])
    pool.sort(reverse=True)
    out, used = [], 0
    if room > 0:
        for s, c in zip(units, pool):
            eff = min(c, room)
            saving = s["unit_price"] * base * eff
            out.append(dict(s, qty=1, coupon=c, effective_discount=eff, wasted_discount=c - eff,
                            fee_without=s["unit_price"] * base * room,
                            fee_with=s["unit_price"] * base * (room - eff), saving=saving))
            used += 1
    left = {}
    for c in pool[used:]:
        left[c] = left.get(c, 0) + 1
    return {"assignments": out, "total_saving": sum(a["saving"] for a in out),
            "unused": [{"discount": d, "count": n} for d, n in sorted(left.items(), reverse=True)]}
