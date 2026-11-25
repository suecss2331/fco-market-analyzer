"""9·10단계: 로컬 웹 대시보드 서버 (표준 라이브러리만 사용).

  python -m fco.dashboard [--port 8765]   →  http://127.0.0.1:8765

이 PC 에서만 접속된다 (127.0.0.1 에만 열림). 쓰기 요청(POST)은 사용자 지정 헤더가 있어야 받아들이므로
다른 웹사이트가 몰래 설정·보유 매물을 바꾸는 요청은 브라우저 단계에서 막힌다.

API
  GET  /api/overview          총 운용 BP(순 BP + 보유 평가액), 카드 수, 갱신 시각, 자금 배분 계획
  GET  /api/cards             활성 가격대 카드 표
  GET  /api/card?spid=&grade= 카드 상세
  GET  /api/portfolio         보유 매물 평가 + 매도 기록
  GET  /api/search?q=         선수 검색 (보유 매물 등록용, 공식 메타데이터)
  GET  /api/validation        추천 기록 채점 결과
  GET  /api/refresh           갱신 실행 여부 + 최근 갱신 기록
  GET  /api/settings          설정 화면 값
  POST /api/settings          설정 저장 → 4~8단계 재계산
  POST /api/holding           보유 매물 추가 {spid, grade, qty, buy_price, bought_at, memo}
  POST /api/holding/update    수정 {id, qty, buy_price, bought_at, memo}
  POST /api/holding/delete    삭제 {id}
  POST /api/holding/sell      매도 기록 {id, qty, sell_price, sold_at, memo}
  POST /api/refresh           빠른 갱신 시작 (백그라운드)
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from html import escape as html_escape

from . import auth, fionhub, kakao, longterm, rankerpick
from . import http as fhttp
from .allocation import allocate_rankerpick
from .backup import backup_user_records
from .collect_prices import ensure_card, fetch_cards
from .coupons import assign_coupons, effective_fee
from .config import CONFIG_DIR, DB_PATH, ROOT, active_price_range, load_settings
from .db import connect, log, now_kst, today_kst
from .pipeline import rerun_analysis
from .judge import judge_buy, judge_sell
from .portfolio import flip_roi, limit_metrics, lot_metrics, realized
from .refresh import LOCK
from .validation import evaluate_rankerpick

STATIC = Path(__file__).resolve().parent / "static"
CSRF_HEADER = "X-FCO-Request"

CARD_SQL = """
SELECT k.spid, k.grade, k.player_name, k.class_name, k.ovr, k.main_position,
       p.current_price, p.basis_price, p.buy_fair, p.target_buy, p.sell_expected, p.fee_amount,
       p.net_at_current, p.roi_at_current_pct, p.net_at_target, p.roi_at_target_pct,
       p.break_even_sell_at_current, p.max_buy_for_target, p.required_discount_pct, p.premium_used_pct,
       p.reach_days, p.reach_lookback, p.current_vs_last_daily_pct, p.risk_flags, p.verdict, p.factor,
       w.wd_mean, w.wd_median, w.we_mean, w.we_median, w.premium_pct, w.discount_pct,
       w.weeks_total, w.weeks_positive, w.weekly_premium_median_pct, w.transient,
       w.window_start, w.window_end, w.current_at,
       t.l_cv, t.s_cv, t.l_trend, t.l_trend_pct_per_day, t.l_change_pct, t.s_change_pct,
       t.max_daily_drop_pct, t.max_daily_rise_pct, t.stale, t.flat_run_days, t.l_coverage,
       q.score AS pop_score, q.appearances, q.starter_appearances, q.ranks, q.users_top_universe,
       q.drop_alert, q.snap_at,
       d.deal_score, d.base_score, d.penalty, d.reliability, d.reliability_pct, d.warnings, d.price_group,
       d.pop_points, d.discount_points, d.weekend_points, d.stability_points,
       s.days AS price_days, s.fetched_at AS price_fetched_at,
       (SELECT c2.price FROM price_current c2 WHERE c2.spid = k.spid AND c2.grade = k.grade
         ORDER BY c2.observed_at DESC LIMIT 1 OFFSET 1) AS prev_price,
       (SELECT c2.observed_at FROM price_current c2 WHERE c2.spid = k.spid AND c2.grade = k.grade
         ORDER BY c2.observed_at DESC LIMIT 1 OFFSET 1) AS prev_observed_at
FROM card k
JOIN pricing_stats p USING (spid, grade) JOIN weekly_stats w USING (spid, grade)
JOIN stability_stats t USING (spid, grade) JOIN popularity_stats q USING (spid, grade)
JOIN deal_stats d USING (spid, grade) JOIN price_status s USING (spid, grade)
"""

EDITABLE = {
    # 화면 키: (settings 경로, 형변환, 최소, 최대)
    "cashBp": (("cashBp",), int, 0, 10**15),
    "baseRate": (("fee", "baseRate"), float, 0.0, 0.9),
    "premiumPcBang": (("fee", "premiumPcBang"), lambda v: v in (True, 1, "1", "true", "on"), None, None),
    "pcBangDiscount": (("fee", "pcBangDiscount"), float, 0.0, 1.0),
    "topClass": (("fee", "topClass"), lambda v: v in (True, 1, "1", "true", "on"), None, None),
    "topClassDiscount": (("fee", "topClassDiscount"), float, 0.0, 1.0),
    "activePreset": (("priceRange", "activePreset"), str, None, None),
    "maxPerCardRatio": (("allocation", "maxPerCardRatio"), float, 0.01, 1.0),
    "maxQtyPerCard": (("allocation", "maxQtyPerCard"), lambda v: None if v in (None, "", 0, "0") else int(v), 1, 10000),
    "minTargetRoi": (("buyPrice", "minTargetRoi"), float, 0.0, 2.0),
    "discountFactorMin": (("buyPrice", "discountFactorMin"), float, 0.5, 1.0),
    "discountFactorMax": (("buyPrice", "discountFactorMax"), float, 0.5, 1.0),
}


class BadRequest(ValueError):
    pass


def _get(d, path):
    for p in path:
        d = d[p]
    return d


def settings_view(s: dict) -> dict:
    out = {}
    for k, (path, *_rest) in EDITABLE.items():
        try:
            out[k] = _get(s, path)
        except KeyError:
            out[k] = None
    out["presets"] = list(s["priceRange"]["presets"].keys())
    out["weights"] = s["dealScore"]["weights"]
    out["sellFeeRate"] = s["fee"]["sellFeeRate"]
    return out


def standing_discount(f: dict) -> float:
    """쿠폰을 뺀 상시 할인 합 (PC방 + TOP CLASS)."""
    return (f["pcBangDiscount"] if f.get("premiumPcBang") else 0.0) + (f["topClassDiscount"] if f.get("topClass") else 0.0)


def save_settings(changes: dict) -> dict:
    s = load_settings()
    for k, v in changes.items():
        if k not in EDITABLE:
            raise BadRequest(f"바꿀 수 없는 설정: {k}")
        path, cast, lo, hi = EDITABLE[k]
        try:
            v = cast(v)
        except (TypeError, ValueError):
            raise BadRequest(f"{k} 값이 올바르지 않습니다: {v}") from None
        if k == "activePreset" and v not in s["priceRange"]["presets"]:
            raise BadRequest(f"없는 가격대 프리셋: {v}")
        if lo is not None and v is not None and not (lo <= v <= hi):
            raise BadRequest(f"{k} 값 {v} 는 {lo}~{hi} 범위여야 합니다")
        tgt = s
        for p in path[:-1]:
            tgt = tgt[p]
        tgt[path[-1]] = v
    if s["buyPrice"]["discountFactorMin"] > s["buyPrice"]["discountFactorMax"]:
        raise BadRequest("매수 할인 계수 최소값이 최대값보다 큽니다")
    f = s["fee"]
    f["sellFeeRate"] = round(effective_fee(f["baseRate"], standing_discount(f)), 6)  # 쿠폰 없는 평소 수수료
    with open(CONFIG_DIR / "settings.json", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    if set(changes) - {"cashBp"}:  # 순 BP 만 바뀌면 분석 재계산 불필요
        rerun_analysis()
    return s


def cards(con, lo, hi) -> list[dict]:
    rows = con.execute(CARD_SQL + " WHERE p.current_price BETWEEN ? AND ? ORDER BY d.deal_score DESC",
                       (lo, hi)).fetchall()
    out = [dict(r) for r in rows]
    attach_market(con, out)
    return out


# --- 보유 매물 -------------------------------------------------------------
def latest_prices(con, keys) -> dict:
    out = {}
    for spid, grade in keys:
        rows = con.execute("SELECT price, observed_at FROM price_current WHERE spid=? AND grade=? "
                           "ORDER BY observed_at DESC LIMIT 2", (spid, grade)).fetchall()
        out[(spid, grade)] = {
            "current": rows[0]["price"] if rows else None, "current_at": rows[0]["observed_at"] if rows else None,
            "prev": rows[1]["price"] if len(rows) > 1 else None, "prev_at": rows[1]["observed_at"] if len(rows) > 1 else None}
    return out


def portfolio(con) -> dict:
    s = load_settings()
    fee = float(s["fee"]["sellFeeRate"])
    lots = [dict(r) for r in con.execute(
        """SELECT h.*, k.player_name, k.class_name, k.ovr, k.main_position,
                  p.sell_expected, p.target_buy, p.verdict, p.risk_flags, d.deal_score, d.warnings,
                  ps.status AS price_status, ps.message AS price_message
           FROM holding h LEFT JOIN card k USING (spid, grade)
           LEFT JOIN pricing_stats p USING (spid, grade) LEFT JOIN deal_stats d USING (spid, grade)
           LEFT JOIN price_status ps USING (spid, grade)
           ORDER BY h.bought_at DESC, h.id DESC""")]
    prices = latest_prices(con, {(l["spid"], l["grade"]) for l in lots})
    f = s["fee"]
    best = best_coupon(con)
    fee_coupon = effective_fee(float(f["baseRate"]), standing_discount(f), best) if best else None
    tot = {"cost": 0.0, "market_value": 0.0, "liquidation_value": 0.0, "unrealized": 0.0, "unpriced": 0,
           "upper_value": 0.0, "profit_at_upper": 0.0, "profit_at_upper_coupon": 0.0, "no_market": 0}
    for l in lots:
        pr = prices[(l["spid"], l["grade"])]
        l.update(current_price=pr["current"], current_at=pr["current_at"], prev_price=pr["prev"], prev_at=pr["prev_at"])
        l.update(lot_metrics(l["qty"], l["buy_price"], pr["current"], fee, l["sell_expected"], pr["prev"]))
        mk = fionhub.latest(con, l["spid"], l["grade"])
        l["market"] = mk
        l.update(limit_metrics(l["qty"], l["buy_price"], mk and mk["upper_limit"], mk and mk["lower_limit"], fee, fee_coupon))
        tot["cost"] += l["cost"]
        if l["market_value"] is None:
            tot["unpriced"] += 1
        else:
            for k in ("market_value", "liquidation_value", "unrealized"):
                tot[k] += l[k]
        l["judge"] = judge_sell(l["buy_price"], mk and mk["upper_limit"], fee, fee_coupon, float(s["buyPrice"]["minTargetRoi"]),
                                mk and mk["buy_at_upper"])
        if l["profit_at_upper"] is None:
            tot["no_market"] += 1
        else:
            tot["upper_value"] += l["qty"] * mk["upper_limit"]
            tot["profit_at_upper"] += l["profit_at_upper"]
            tot["profit_at_upper_coupon"] += l["profit_at_upper_coupon"] or 0
    trades = [dict(r) for r in con.execute(
        """SELECT t.*, k.player_name, k.class_name FROM trade t LEFT JOIN card k USING (spid, grade)
           ORDER BY t.sold_at DESC, t.id DESC""")]
    rt = {"count": len(trades), "net_profit": sum(t["net_profit"] for t in trades),
          "wins": sum(1 for t in trades if t["net_profit"] > 0),
          "cost": sum(t["qty"] * t["buy_price"] for t in trades)}
    rt["roi_pct"] = rt["net_profit"] / rt["cost"] * 100 if rt["cost"] else None
    cash = float(s.get("cashBp", 0))
    return {"fee": fee, "cash": cash, "lots": lots, "totals": tot, "trades": trades, "realized": rt,
            "best_coupon": best, "fee_coupon": fee_coupon,
            "total_bp": cash + tot["market_value"], "total_bp_after_fee": cash + tot["liquidation_value"]}


_RP_CACHE = {"at": 0.0, "value": None}
_RP_LOCK = threading.Lock()


def rp_evaluate(con) -> dict:
    """랭커 픽 평가 결과를 30초 동안 재사용 (화면 한 번 열 때 여러 API 가 같은 계산을 하지 않게). 기록이 바뀌면 비움."""
    with _RP_LOCK:
        if _RP_CACHE["value"] is None or time.time() - _RP_CACHE["at"] > 30:
            _RP_CACHE.update(value=rankerpick.evaluate(con), at=time.time())
        return _RP_CACHE["value"]


def rp_cache_clear() -> None:
    _RP_CACHE["value"] = None


def coupon_count(con, discount: float | None) -> int:
    """만료 안 된 그 할인율 쿠폰 장수."""
    if not discount:
        return 0
    r = con.execute("SELECT SUM(qty) FROM coupon WHERE discount = ? AND (expires_at IS NULL OR expires_at = '' OR expires_at >= ?)",
                    (discount, today_kst())).fetchone()
    return int(r[0] or 0)


def best_coupon(con) -> float | None:
    """지금 쓸 수 있는 (만료 안 된) 가장 큰 쿠폰 할인율."""
    r = con.execute("SELECT MAX(discount) FROM coupon WHERE qty > 0 AND (expires_at IS NULL OR expires_at = '' OR expires_at >= ?)",
                    (today_kst(),)).fetchone()
    return r[0] if r and r[0] else None


def refresh_market(con, b: dict) -> dict:
    """보유 매물 (또는 지정 카드) 의 fionhub 상한가·하한가를 바로 받아 온다."""
    if b.get("spid") is not None:
        keys = [(_int(b.get("spid"), "spid"), _int(b.get("grade"), "강화", 1))]
    else:
        keys = [(r[0], r[1]) for r in con.execute("SELECT DISTINCT spid, grade FROM holding")]
    res = fionhub.collect(con, keys, progress=False)
    return {"ok": True, "updated": res["ok"], "failed": [f"{s}/{g}: {m}" for s, g, m in res["failed"]]}


def attach_market(con, rows: list[dict]) -> None:
    """카드 목록에 가장 최근 fionhub 시세를 붙인다 (없으면 None)."""
    latest = {}
    for r in con.execute(
            """SELECT m.spid, m.grade, m.price, m.lower_limit, m.upper_limit, m.transfers_2h, m.buy_levels, m.fetched_at
               FROM market_snapshot m JOIN (SELECT spid, grade, MAX(fetched_at) f FROM market_snapshot GROUP BY spid, grade) x
               ON m.spid = x.spid AND m.grade = x.grade AND m.fetched_at = x.f"""):
        levels = json.loads(r["buy_levels"] or "[]")
        latest[(r["spid"], r["grade"])] = {
            "mk_price": r["price"], "mk_lower": r["lower_limit"], "mk_upper": r["upper_limit"],
            "mk_transfers_2h": r["transfers_2h"], "mk_fetched_at": r["fetched_at"],
            "mk_buy_at_upper": sum(c or 0 for p, c in levels if p == r["upper_limit"])}
    empty = dict.fromkeys(["mk_price", "mk_lower", "mk_upper", "mk_transfers_2h", "mk_fetched_at", "mk_buy_at_upper"])
    for c in rows:
        c.update(latest.get((c["spid"], c["grade"]), empty))


def _int(v, name, lo=1):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} 는 정수여야 합니다") from None
    if v < lo:
        raise BadRequest(f"{name} 는 {lo} 이상이어야 합니다")
    return v


def _day(v, name):
    if not v:
        return today_kst()
    try:
        return date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise BadRequest(f"{name} 날짜 형식은 YYYY-MM-DD 입니다") from None


def _adj(b: dict) -> bool:
    """보유 BP 에 반영할지 (화면의 '보유 BP에 반영' 체크, 기본 반영)."""
    v = b.get("adjust_cash", True)
    return v in (True, 1, "1", "true", "on")


def adjust_cash(delta: float) -> float:
    """보유 순 BP(설정 cashBp)를 delta 만큼 바꾼다. 0 밑으로는 내리지 않는다. 바뀐 값을 돌려준다."""
    cur = float(load_settings().get("cashBp", 0))
    new = max(0, int(round(cur + delta)))
    if new != cur:
        save_settings({"cashBp": new})  # cashBp 만 바뀌면 분석 재계산 안 함
    return new


def add_holding(con, b: dict) -> dict:
    spid, grade = _int(b.get("spid"), "spid"), _int(b.get("grade"), "강화 단계")
    if not 1 <= grade <= 13:
        raise BadRequest("강화 단계는 1~13 입니다")
    if not con.execute("SELECT 1 FROM meta_player WHERE spid=?", (spid,)).fetchone():
        raise BadRequest("공식 메타데이터에 없는 선수입니다")
    qty, price = _int(b.get("qty"), "수량"), _int(b.get("buy_price"), "매수가")
    ensure_card(con, spid, grade)
    con.execute("INSERT INTO holding (spid, grade, qty, buy_price, bought_at, memo, created_at) VALUES (?,?,?,?,?,?,?)",
                (spid, grade, qty, price, _day(b.get("bought_at"), "매수일"), (b.get("memo") or "")[:200], now_kst()))
    con.commit()
    cash = adjust_cash(-qty * price) if _adj(b) else None  # 산 만큼 보유 BP 에서 뺌
    fetched = False
    if not con.execute("SELECT 1 FROM price_status WHERE spid=? AND grade=? AND status='ok'", (spid, grade)).fetchone():
        # 처음 보는 카드 → 데이터센터에서 시세를 바로 받아 분석에 포함 (요청 1~2건)
        s = load_settings()
        fetch_cards(con, [(spid, grade)], fhttp.Throttle(float(s["collection"]["datacenterMinIntervalSec"])), False)
        rerun_analysis()
        fetched = True
    return {"ok": True, "fetched_price": fetched, "cash": cash}


def update_holding(con, b: dict) -> dict:
    hid = _int(b.get("id"), "id")
    old = con.execute("SELECT qty, buy_price FROM holding WHERE id=?", (hid,)).fetchone()
    if not old:
        raise BadRequest("없는 보유 매물입니다")
    qty, price = _int(b.get("qty"), "수량"), _int(b.get("buy_price"), "매수가")
    con.execute("UPDATE holding SET qty=?, buy_price=?, bought_at=?, memo=? WHERE id=?",
                (qty, price, _day(b.get("bought_at"), "매수일"), (b.get("memo") or "")[:200], hid))
    con.commit()
    # 매수 금액이 바뀐 만큼만 보유 BP 조정 (더 비싸게/더 많이 샀으면 더 뺌)
    cash = adjust_cash(old["qty"] * old["buy_price"] - qty * price) if _adj(b) else None
    return {"ok": True, "cash": cash}


def delete_holding(con, b: dict) -> dict:
    """잘못 넣은 보유 기록 지우기 → (체크 시) 매수 금액을 보유 BP 로 되돌림. 판 것은 '매도'로 기록."""
    hid = _int(b.get("id"), "id")
    old = con.execute("SELECT qty, buy_price FROM holding WHERE id=?", (hid,)).fetchone()
    con.execute("DELETE FROM holding WHERE id=?", (hid,))
    con.commit()
    cash = adjust_cash(old["qty"] * old["buy_price"]) if old and _adj(b) else None
    return {"ok": True, "cash": cash}


def sell_holding(con, b: dict) -> dict:
    hid = _int(b.get("id"), "id")
    lot = con.execute("SELECT * FROM holding WHERE id=?", (hid,)).fetchone()
    if not lot:
        raise BadRequest("없는 보유 매물입니다")
    qty, price = _int(b.get("qty"), "매도 수량"), _int(b.get("sell_price"), "매도가")
    if qty > lot["qty"]:
        raise BadRequest(f"보유 수량({lot['qty']}장)보다 많이 팔 수 없습니다")
    # 쿠폰: coupon_id (보유 쿠폰에서 장수 차감) 또는 coupon_discount (이미 쓴 쿠폰을 기록만). 1장 판매에 1장씩 쓴다.
    coupon, cid = 0.0, b.get("coupon_id")
    if cid:
        row = con.execute("SELECT discount, qty FROM coupon WHERE id=?", (_int(cid, "쿠폰"),)).fetchone()
        if not row:
            raise BadRequest("없는 쿠폰입니다")
        if row["qty"] < qty:
            raise BadRequest(f"쿠폰이 {row['qty']}장뿐입니다 (판매 {qty}장에 {qty}장 필요)")
        coupon = row["discount"]
    elif b.get("coupon_discount"):
        coupon = _discount(b.get("coupon_discount"))
    fee = sale_fee(coupon)
    r = realized(qty, lot["buy_price"], price, fee)
    con.execute("INSERT INTO trade (spid, grade, qty, buy_price, sell_price, fee_rate, net_profit, bought_at, sold_at, "
                "memo, created_at, coupon_discount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (lot["spid"], lot["grade"], qty, lot["buy_price"], price, fee, r["net_profit"], lot["bought_at"],
                 _day(b.get("sold_at"), "매도일"), (b.get("memo") or "")[:200], now_kst(), coupon))
    if cid:
        use_coupon(con, {"id": cid, "count": qty})
    if qty == lot["qty"]:
        con.execute("DELETE FROM holding WHERE id=?", (hid,))
    else:
        con.execute("UPDATE holding SET qty = qty - ? WHERE id=?", (qty, hid))
    con.commit()
    cash = adjust_cash(qty * price * (1 - fee)) if _adj(b) else None  # 판매 대금(수수료 뺀 받은 BP)을 더함
    return {"ok": True, "net_profit": r["net_profit"], "fee_rate": fee, "cash": cash}


def sale_fee(coupon: float = 0.0) -> float:
    f = load_settings()["fee"]
    return effective_fee(float(f["baseRate"]), standing_discount(f), coupon)


def update_trade(con, b: dict) -> dict:
    """매도 기록 고치기: 매도가 · 매도일 · 쓴 쿠폰 할인율(0 = 없음) · 메모. 수수료와 손익을 다시 계산한다.
    (쿠폰 보유 장수는 바꾸지 않는다 — 이미 쓴 쿠폰을 기록에 반영하는 용도)"""
    tid = _int(b.get("id"), "id")
    t = con.execute("SELECT * FROM trade WHERE id=?", (tid,)).fetchone()
    if not t:
        raise BadRequest("없는 매도 기록입니다")
    price = _int(b.get("sell_price", t["sell_price"]), "매도가")
    cd = b.get("coupon_discount", t["coupon_discount"])
    coupon = _discount(cd) if cd not in (None, "", 0, "0") else 0.0
    fee = sale_fee(coupon)
    r = realized(t["qty"], t["buy_price"], price, fee)
    con.execute("UPDATE trade SET sell_price=?, fee_rate=?, net_profit=?, coupon_discount=?, sold_at=?, memo=? WHERE id=?",
                (price, fee, r["net_profit"], coupon, _day(b.get("sold_at") or t["sold_at"], "매도일"),
                 (b.get("memo", t["memo"]) or "")[:200], tid))
    con.commit()
    # 받은 BP 가 바뀐 만큼 보유 BP 조정 (매도가 · 쿠폰을 고쳤을 때)
    old_recv, new_recv = t["qty"] * t["sell_price"] * (1 - t["fee_rate"]), t["qty"] * price * (1 - fee)
    cash = adjust_cash(new_recv - old_recv) if _adj(b) else None
    return {"ok": True, "net_profit": r["net_profit"], "fee_rate": fee, "cash": cash}


def search_players(con, q: str) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 1:
        return []
    rows = con.execute(
        """SELECT p.spid, p.name, c.name AS class_name,
                  (SELECT GROUP_CONCAT(grade) FROM card k WHERE k.spid = p.spid) AS known_grades
           FROM meta_player p LEFT JOIN meta_class c ON c.class_id = p.spid / 1000000
           WHERE p.name LIKE ? ORDER BY (p.name = ?) DESC, p.spid DESC LIMIT 40""", (f"%{q}%", q)).fetchall()
    return [dict(r) for r in rows]


# --- 수수료 쿠폰 -------------------------------------------------------------
def _discount(v) -> float:
    """쿠폰 할인율: 30 / "30%" / 0.3 모두 30% 로 읽는다. 5% 단위, 5~100%."""
    try:
        x = float(str(v).replace("%", "").strip())
    except ValueError:
        raise BadRequest(f"쿠폰 할인율을 읽을 수 없습니다: {v}") from None
    if x <= 1:
        x *= 100
    if not (5 <= x <= 100) or abs(x / 5 - round(x / 5)) > 1e-9:
        raise BadRequest("쿠폰 할인율은 5% 단위로 5~100% 입니다")
    return round(x) / 100


def add_coupon(con, b: dict) -> dict:
    exp = b.get("expires_at") or None
    con.execute("INSERT INTO coupon (discount, qty, expires_at, memo, created_at) VALUES (?,?,?,?,?)",
                (_discount(b.get("discount")), _int(b.get("qty"), "장수"), _day(exp, "만료일") if exp else None,
                 (b.get("memo") or "")[:200], now_kst()))
    con.commit()
    return {"ok": True}


def update_coupon(con, b: dict) -> dict:
    cid = _int(b.get("id"), "id")
    exp = b.get("expires_at") or None
    con.execute("UPDATE coupon SET discount=?, qty=?, expires_at=?, memo=? WHERE id=?",
                (_discount(b.get("discount")), _int(b.get("qty"), "장수"), _day(exp, "만료일") if exp else None,
                 (b.get("memo") or "")[:200], cid))
    con.commit()
    return {"ok": True}


def delete_coupon(con, b: dict) -> dict:
    con.execute("DELETE FROM coupon WHERE id=?", (_int(b.get("id"), "id"),))
    con.commit()
    return {"ok": True}


def use_coupon(con, b: dict) -> dict:
    """쿠폰 1장(또는 n장)을 썼다고 기록 → 장수 감소, 0 이 되면 삭제."""
    cid, n = _int(b.get("id"), "id"), _int(b.get("count", 1), "사용 장수")
    row = con.execute("SELECT qty FROM coupon WHERE id=?", (cid,)).fetchone()
    if not row:
        raise BadRequest("없는 쿠폰입니다")
    if n >= row["qty"]:
        con.execute("DELETE FROM coupon WHERE id=?", (cid,))
    else:
        con.execute("UPDATE coupon SET qty = qty - ? WHERE id=?", (n, cid))
    con.commit()
    return {"ok": True}


HYPOTHETICAL_TIERS = (0.30, 0.40, 0.50)


def coupon_advice(con) -> dict:
    """쿠폰을 어디에 쓰면 가장 많이 아끼는지 + 쿠폰이 있어야 수익이 나는 매수 후보."""
    s = load_settings()
    f = s["fee"]
    base, other = float(f["baseRate"]), standing_discount(f)
    fee_now = effective_fee(base, other)
    lo, hi, preset = active_price_range(s)
    min_roi = float(s["buyPrice"]["minTargetRoi"])
    max_qty = s["allocation"].get("maxQtyPerCard") or 1
    today = today_kst()
    inv = [dict(r) for r in con.execute("SELECT * FROM coupon ORDER BY discount DESC, expires_at")]
    for c in inv:
        c["expired"] = bool(c["expires_at"] and c["expires_at"] < today)
        c["fee_with"] = effective_fee(base, other, c["discount"])
        c["wasted"] = max(0.0, c["discount"] - max(0.0, 1 - other))
    usable = [{"discount": c["discount"], "count": c["qty"]} for c in inv if not c["expired"]]

    # A) 보유 매물을 팔 때: 지금 실제로 팔 수 있는 현재가 기준 (예상 매도가는 급락 카드에선 회복 가정이라 부풀려질 수 있음)
    pf = portfolio(con)
    hold_sales = []
    for l in pf["lots"]:
        # 상한가를 알면 상한가 매도 기준 (구매 대기가 있으면 바로 팔리는 가격), 모르면 현재가
        up = l["market"] and l["market"]["upper_limit"]
        price = up or l["current_price"] or l["sell_expected"]
        if price:
            hold_sales.append({"key": f"hold:{l['id']}", "kind": "보유 매물 상한가 매도" if up else "보유 매물 매도",
                               "unit_price": price, "qty": l["qty"],
                               "name": l["player_name"], "cls": l["class_name"], "grade": l["grade"],
                               "buy_price": l["buy_price"], "spid": l["spid"]})

    # B) 쿠폰이 있으면 '지금 매수 가능' 이 되는 카드 (위험 표시 없음). 쿠폰은 나중에 팔 때 쓴다.
    #    살 수 있는 카드만: 1장 가격 ≤ min(카드당 한도, 순 BP). 수량도 자금 배분과 같은 한도를 따른다.
    cap = pf["total_bp"] * float(s["allocation"]["maxPerCardRatio"])
    budget = min(cap, pf["cash"])
    # 신규 후보 = 랭커 픽 추천 카드 (인기 팀컬러 · 2시간 거래 · 주말보다 쌈). 예상 매도가 = 지난 주말 평균.
    rows = [dict(spid=x["spid"], grade=x["grade"], current_price=x["current_price"], sell_expected=x["weekend_avg"],
                 player_name=x["player_name"], class_name=x["class_name"], deal_score=x["rp_score"],
                 transfers_2h=x["transfers_2h"], lower=x["lower"])
            for x in rp_evaluate(con)["rows"]]
    unaffordable = sum(1 for r in rows if r["current_price"] > budget)
    rows = [r for r in rows if r["current_price"] <= budget]
    tiers = sorted({c["discount"] for c in inv if not c["expired"]}, reverse=True) or list(HYPOTHETICAL_TIERS)
    by_tier = []
    for t in tiers:
        fee_c = effective_fee(base, other, t)
        cands = []
        for r in rows:
            sell = r["sell_expected"]
            target_c = sell * (1 - fee_c) / (1 + min_roi)   # 쿠폰 쓰고 목표 수익률을 맞추는 최대 매수가
            if r["current_price"] <= target_c:
                profit = sell * (1 - fee_c) - r["current_price"]
                without = sell * (1 - fee_now) - r["current_price"]
                cands.append(dict(r, fee=fee_c, target_with=target_c, profit_with=profit,
                                  roi_with=profit / r["current_price"] * 100, profit_without=without,
                                  only_with_coupon=without < r["current_price"] * min_roi))
        cands.sort(key=lambda x: -(x["deal_score"] or 0))  # 랭커 픽 점수 순
        by_tier.append({"discount": t, "fee": fee_c, "hypothetical": not usable, "count": len(cands),
                        "new_count": sum(1 for x in cands if x["only_with_coupon"]), "cards": cands[:10]})

    # C) 전체 최적 배정: 보유 매물 매도 + (가장 큰 쿠폰 기준) 신규 후보의 예상 매도까지 합쳐 큰 쿠폰 → 비싼 판매 순
    new_sales = []
    if by_tier and usable:
        for x in by_tier[0]["cards"]:
            qty = min(int(max_qty), int(budget // x["current_price"]))
            if qty < 1:
                continue
            new_sales.append({"key": f"new:{x['spid']}:{x['grade']}", "kind": "신규 매수 후 매도", "unit_price": x["sell_expected"],
                              "qty": qty, "name": x["player_name"], "cls": x["class_name"], "grade": x["grade"],
                              "buy_price": x["current_price"], "spid": x["spid"]})
    plan = assign_coupons(hold_sales + new_sales, usable, base, other) if usable else None
    hold_only = assign_coupons(hold_sales, usable, base, other) if usable else None
    return {"base": base, "standing_discount": other, "fee_now": fee_now, "room": max(0.0, 1 - other),
            "preset": preset, "inventory": inv, "holdings_plan": hold_only, "overall_plan": plan, "by_tier": by_tier,
            "budget_per_card": budget, "unaffordable": unaffordable}


# --- 개요 · 검증 · 갱신 -------------------------------------------------------
def overview(con) -> dict:
    s = load_settings()
    lo, hi, preset = active_price_range(s)
    fee = float(s["fee"]["sellFeeRate"])
    al = s["allocation"]
    pf = portfolio(con)
    held_value = {}
    for l in pf["lots"]:
        held_value[(l["spid"], l["grade"])] = held_value.get((l["spid"], l["grade"]), 0) + (l["market_value"] or l["cost"])
    cs = cards(con, lo, hi)
    one = lambda q: con.execute(q).fetchone()[0]
    # 랭커 픽 추천 순서대로 배분 (매수가 = 현재가, 예상 매도가 = 지난 주말 평균, 수량은 2시간 거래량·쿠폰 장수로 제한)
    rp = rp_evaluate(con)
    coupons = coupon_count(con, rp.get("coupon"))
    cands = [dict(key=(x["spid"], x["grade"]), unit_price=x["current_price"], sell_expected=x["weekend_avg"],
                  transfers_2h=x["transfers_2h"], name=x["player_name"], cls=x["class_name"], grade=x["grade"],
                  score=x["rp_score"], lower=x["lower"]) for x in rp["rows"]]
    plan = allocate_rankerpick(cands, pf["total_bp"], pf["cash"], float(al["maxPerCardRatio"]), fee,
                               rp.get("fee_coupon"), coupons, float(s["buyPrice"]["minTargetRoi"]),
                               al.get("maxQtyPerCard"), int(s["rankerPick"].get("transfersPerUnit", 5)), held_value)
    plan["coupon"] = rp.get("coupon")
    for r in plan["rows"] + plan["skipped"]:
        r["key"] = list(r["key"])
    return {
        "cash": pf["cash"], "holdings_value": pf["totals"]["market_value"], "total_bp": pf["total_bp"],
        "total_bp_after_fee": pf["total_bp_after_fee"], "holdings_count": len(pf["lots"]),
        "preset": preset, "range": [lo, hi], "fee": fee,
        "min_target_roi": s["buyPrice"]["minTargetRoi"], "max_per_card": al["maxPerCardRatio"],
        "max_qty_per_card": al.get("maxQtyPerCard"),
        "cards_in_range": len(cs),
        "cards_total": one("SELECT COUNT(*) FROM price_status WHERE status='ok'"),
        "verdicts": {v: sum(1 for c in cs if c["verdict"] == v) for v in ("buy_now", "risky", "wait", "insufficient")},
        "updated": {
            "prices": one("SELECT MAX(observed_at) FROM price_current"),
            "daily_end": one("SELECT MAX(day) FROM price_daily"),
            "ranking": one("SELECT MAX(collected_at) FROM ranking_snapshot WHERE status='complete'"),
            "analysis": one("SELECT MAX(computed_at) FROM deal_stats"),
        },
        "refreshing": LOCK.exists(),
        "allocation": plan,
    }


def validation(con) -> dict:
    """랭커 픽 추천 검증: 추천된 날 현재가에 사서 다음 주말 평균에 판 것으로 채점 (수수료 / 쿠폰 두 가지)."""
    recs = [dict(r) for r in con.execute(
        """SELECT r.*, k.player_name, k.class_name FROM rp_rec_log r LEFT JOIN card k USING (spid, grade)
           ORDER BY r.day DESC, r.rank""")]
    today = date.fromisoformat(today_kst())
    daily_cache = {}
    for r in recs:
        key = (r["spid"], r["grade"])
        if key not in daily_cache:
            daily_cache[key] = {date.fromisoformat(x["day"]): x["price"] for x in con.execute(
                "SELECT day, price FROM price_daily WHERE spid=? AND grade=?", key)}
        r.update(evaluate_rankerpick(r, daily_cache[key], r["fee_rate"], r["fee_coupon"], today))
        if r["status"] == "pending":  # 채점 전: 지금 시세로 본 중간 손익 (fionhub, 없으면 데이터센터)
            mk = fionhub.latest(con, *key)
            now = (mk and mk["price"]) or (latest_prices(con, [key])[key]["current"])
            r["now_price"] = now
            r["now_change_pct"] = (now - r["current_price"]) / r["current_price"] * 100 if now and r["current_price"] else None
    ev = [r for r in recs if r["status"] == "evaluated"]

    def summary(rows):
        n = len(rows)
        coup = [r["roi_coupon_pct"] for r in rows if r["roi_coupon_pct"] is not None]
        return {"evaluated": n, "wins": sum(1 for r in rows if r["roi_pct"] > 0),
                "wins_coupon": sum(1 for x in coup if x > 0),
                "avg_roi_pct": sum(r["roi_pct"] for r in rows) / n if n else None,
                "avg_roi_coupon_pct": sum(coup) / len(coup) if coup else None}
    pend = [r for r in recs if r["status"] == "pending"]
    return {"total": len(recs), "pending": len(pend), "days": len({r["day"] for r in recs}),
            "first_day": min((r["day"] for r in recs), default=None),
            "next_grade": min((r["sell_weekend"] for r in pend), default=None),
            "all": summary(ev), "top10": summary([r for r in ev if (r["rank"] or 99) <= 10]),
            "recs": recs[:500]}


def refresh_status(con) -> dict:
    logs = [dict(r) for r in con.execute(
        "SELECT at, job, ok, message FROM collect_log WHERE job LIKE 'refresh_%' OR job LIKE 'collect_%' "
        "ORDER BY id DESC LIMIT 8")]
    return {"running": LOCK.exists(), "logs": logs}


def start_refresh(mode: str = "quick") -> dict:
    if mode not in ("quick", "daily"):
        raise BadRequest("갱신 종류는 quick 또는 daily 입니다")
    if LOCK.exists():
        return {"ok": True, "started": False, "message": "이미 갱신 중입니다"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_path = ROOT / "data" / "refresh_manual.log"
    with open(log_path, "ab") as lf:
        subprocess.Popen([sys.executable, "-m", "fco.refresh", mode], cwd=str(ROOT), stdout=lf, stderr=lf,
                         creationflags=flags)
    msg = {"quick": "빠른 갱신을 시작했습니다 (2~4분)", "daily": "전체 갱신을 시작했습니다 (40~50분)"}[mode]
    return {"ok": True, "started": True, "message": msg}


# --- HTTP ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "fco-dashboard"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    # --- 로그인 ---
    def _client_ip(self) -> str:
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):  # 터널(Cloudflare 등)을 거쳐 온 요청이면 원래 접속지
            fwd = self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return fwd or ip
        return ip

    def _cookie(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == auth.COOKIE:
                return v
        return None

    def _authed(self) -> bool:
        return not auth.enabled() or auth.valid_token(self._cookie())

    def _https(self) -> bool:
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/login":
                return self._send(200, (STATIC / "login.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/kakao/callback":
                # 카카오 로그인에서 돌아오는 주소. 다른 사이트에서 오는 이동이라 로그인 쿠키(SameSite=Strict)가 안 붙으므로
                # 이 서버가 방금 만든 1회용 state 로 확인한다.
                if not kakao.valid_state(q.get("state", [None])[0]):
                    return self._send(400, "잘못된 요청 (state 불일치). 대시보드 3탭에서 다시 '카카오 연결'을 누르세요.".encode(), "text/plain; charset=utf-8")
                if "error" in q:
                    msg = f"카카오 연결 취소/실패: {q.get('error_description', q['error'])[0]}"
                else:
                    try:
                        kakao.exchange_code(q["code"][0])
                        msg = "카카오 연결 완료! 대시보드 3탭에서 '테스트 보내기'로 확인하세요."
                    except Exception as e:
                        msg = f"카카오 연결 실패: {e}"
                page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                        f"<body style='font-family:system-ui;padding:24px'><p>{html_escape(msg)}</p><p><a href='/'>대시보드로</a></p>")
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if not self._authed():
                if u.path.startswith("/api/"):
                    return self._json({"error": "로그인이 필요합니다"}, 401)
                return self._send(302, b"", "text/plain", {"Location": "/login"})
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/kakao/login":
                try:
                    return self._send(302, b"", "text/plain", {"Location": kakao.login_url()})
                except RuntimeError as e:
                    return self._send(400, str(e).encode(), "text/plain; charset=utf-8")
            if u.path == "/api/kakao":
                return self._json(kakao.status())
            con = connect()
            try:
                if u.path == "/api/overview":
                    return self._json(overview(con))
                if u.path == "/api/cards":
                    lo, hi, _ = active_price_range(load_settings())
                    return self._json(cards(con, lo, hi))
                if u.path == "/api/card":
                    d = card_detail(con, int(q["spid"][0]), int(q["grade"][0]))
                    return self._json(d) if d else self._json({"error": "카드 없음"}, 404)
                if u.path == "/api/portfolio":
                    return self._json(portfolio(con))
                if u.path == "/api/search":
                    return self._json(search_players(con, q.get("q", [""])[0]))
                if u.path == "/api/validation":
                    return self._json(validation(con))
                if u.path == "/api/coupons":
                    return self._json(coupon_advice(con))
                if u.path == "/api/refresh":
                    return self._json(refresh_status(con))
                if u.path == "/api/rankerpick":
                    return self._json(rp_evaluate(con))
                if u.path == "/api/longterm":  # 탭3 고가 선수 장기투자
                    return self._json(longterm.analyze(con, load_settings()))
                if u.path == "/api/rising":
                    return self._json(rankerpick.rising_view(con))
                if u.path == "/api/cardview":  # 어떤 카드든 랭커 픽 창 형식 (+ 보유 정보)
                    spid, grade = int(q["spid"][0]), int(q["grade"][0])
                    ctx = rankerpick.context(con)
                    v = rankerpick.card_view(con, ctx, spid, grade)
                    if v["team_colors"] is None or v["lower"] is None:
                        # 아직 fionhub 에서 안 받은 카드 → 창을 열 때 바로 받아 옴 (팀컬러 · 상한가 · 2시간 거래, 1~2초)
                        res = fionhub.collect(con, [(spid, grade)], progress=False)
                        if res["ok"]:
                            v = rankerpick.card_view(con, ctx, spid, grade)
                        else:
                            v["fionhub_error"] = res["failed"][0][2] if res["failed"] else None
                    v["market_history"] = fionhub.history(con, spid, grade, 24)  # 2시간 거래 · 시세 기록 (최근 순)
                    v["held"] = [dict(r) for r in con.execute(
                        "SELECT qty, buy_price, bought_at FROM holding WHERE spid=? AND grade=?", (spid, grade))]
                    return self._json({"fee": ctx["fee"], "coupon": ctx["coupon"], "config": ctx["cfg"], "card": v})
                if u.path == "/api/cardsearch":
                    return self._json(card_search(con, q.get("q", [""])[0]))
                if u.path == "/api/m/search":
                    return self._json(m_search(con, q))
                if u.path == "/api/m/seasons":
                    return self._json(m_seasons(con))
                if u.path == "/api/tunnel":
                    from .tunnel import read_state
                    return self._json(read_state())
                if u.path == "/api/classes":
                    return self._json({r[0]: r[1] for r in con.execute("SELECT class_id, img FROM meta_class")})
                if u.path == "/api/prices":
                    return self._json(price_history(con, int(q["spid"][0]), int(q["grade"][0])))
                if u.path == "/api/settings":
                    return self._json(settings_view(load_settings()))
            finally:
                con.close()
            self._json({"error": "없는 경로"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        if self.headers.get(CSRF_HEADER) != "1":
            return self._json({"error": "허용되지 않은 요청"}, 403)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise BadRequest("요청 형식이 올바르지 않습니다")
            if path == "/api/login":
                ip = self._client_ip()
                wait = auth.locked(ip)
                if wait:
                    return self._json({"error": f"잠시 잠겼습니다. {wait // 60 + 1}분 뒤에 다시 시도하세요."}, 429)
                ok = auth.check_password(str(body.get("password", "")))
                auth.record(ip, ok)
                if not ok:
                    con = connect()
                    try:
                        log(con, "login", False, f"로그인 실패 {ip}")
                    finally:
                        con.close()
                    return self._json({"error": "비밀번호가 틀렸습니다"}, 401)
                return self._json({"ok": True}, 200, {"Set-Cookie": auth.cookie_header(auth.make_token(), self._https())})
            if not self._authed():
                return self._json({"error": "로그인이 필요합니다"}, 401)
            if path == "/api/logout":
                return self._json({"ok": True}, 200, {"Set-Cookie": auth.clear_cookie_header()})
            if path.startswith("/api/kakao/"):  # 카카오 설정 (사용자 기록이 아니라 백업 대상 아님)
                act = path.rsplit("/", 1)[1]
                if act == "keys":
                    try:
                        kakao.set_keys(str(body.get("rest_api_key", "")), str(body.get("client_secret", "")))
                    except ValueError as e:
                        raise BadRequest(str(e)) from None
                elif act == "option":
                    kakao.set_only_when_running(bool(body.get("only_when_running")))
                elif act == "test":
                    from .tunnel import read_state
                    url = read_state().get("url")
                    text = url or "외부 접속 주소 없음 (터널 꺼짐)"  # 실제 알림과 같은 모양: 링크만
                    try:
                        kakao.send_to_me(text, url)
                    except RuntimeError:
                        kakao.send_to_me(text, None)
                else:
                    return self._json({"error": "없는 경로"}, 404)
                return self._json({"ok": True, "status": kakao.status()})
            rp_cache_clear()  # 설정 · 쿠폰 · 시세가 바뀔 수 있으니 랭커 픽 계산을 다시 하게
            if path == "/api/settings":
                return self._json({"ok": True, "settings": settings_view(save_settings(body))})
            if path == "/api/refresh":
                return self._json(start_refresh(body.get("mode", "quick")))
            if path == "/api/market/refresh":  # 외부 시세 조회만 하고 사용자 기록은 바꾸지 않음
                con = connect()
                try:
                    return self._json(refresh_market(con, body))
                finally:
                    con.close()
            routes = {"/api/holding": add_holding, "/api/holding/update": update_holding,
                      "/api/holding/delete": delete_holding, "/api/holding/sell": sell_holding,
                      "/api/coupon": add_coupon, "/api/coupon/update": update_coupon,
                      "/api/coupon/delete": delete_coupon, "/api/coupon/use": use_coupon,
                      "/api/trade/update": update_trade, "/api/watch": set_watch}
            if path not in routes:
                return self._json({"error": "없는 경로"}, 404)
            con = connect()
            try:
                result = routes[path](con, body)
                backup_user_records(con, path)  # 기록이 바뀔 때마다 JSON 백업 (data/backups)
                return self._json(result)
            finally:
                con.close()
        except (BadRequest, json.JSONDecodeError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"처리 실패: {e}"}, 500)


def card_search(con, q: str, limit: int = 80) -> list[dict]:
    """DB 에 있는 카드 (가격을 받은 적 있는 시즌·강화) 를 선수명 · 시즌으로 찾는다. 이름·시즌 단어 모두 포함."""
    words = [w for w in (q or "").split() if w]
    if not words:
        return []
    where = " AND ".join(["(COALESCE(k.player_name,'') || ' ' || COALESCE(k.class_name,'')) LIKE ?"] * len(words))
    rows = con.execute(
        f"""SELECT k.spid, k.grade, k.player_name, k.class_name, s.current_price, s.fetched_at,
                   (SELECT m.price FROM market_snapshot m WHERE m.spid = k.spid AND m.grade = k.grade
                     ORDER BY m.fetched_at DESC LIMIT 1) AS mk_price,
                   (SELECT m.transfers_2h FROM market_snapshot m WHERE m.spid = k.spid AND m.grade = k.grade
                     ORDER BY m.fetched_at DESC LIMIT 1) AS transfers_2h
            FROM card k LEFT JOIN price_status s USING (spid, grade)
            WHERE {where}
            ORDER BY k.player_name, k.class_name, k.grade LIMIT ?""",
        [f"%{w}%" for w in words] + [limit]).fetchall()
    return [dict(r) for r in rows]


# --- 모바일 ② 선수 검색 · 매수 -------------------------------------------------
def _num_arg(q, k):
    try:
        v = q.get(k, [""])[0]
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def m_search(con, q: dict) -> dict:
    """모바일 검색: 선수명 · 시즌 · 강화 · 가격대 필터 / 관심 매물만 / (조건 없으면) 랭커 픽 추천.
    카드마다 매수·관망 판단 (fco/judge.py) 과 관심 여부를 붙인다."""
    text = (q.get("q", [""])[0] or "").strip()
    season = (q.get("season", [""])[0] or "").strip()
    grade = _num_arg(q, "grade")
    lo, hi = _num_arg(q, "lo"), _num_arg(q, "hi")
    watch_only = q.get("watch", ["0"])[0] == "1"
    rp = rp_evaluate(con)
    rec_keys = {(x["spid"], x["grade"]) for x in rp["rows"]}
    watched = {(r[0], r[1]) for r in con.execute("SELECT spid, grade FROM watch")}
    mode = "search"
    if watch_only:
        keys, mode = sorted(watched), "watch"
    elif not (text or season or grade or lo or hi):
        keys, mode = [(x["spid"], x["grade"]) for x in rp["rows"]], "recommend"
    else:
        where, args = ["s.status = 'ok'"], []
        for w in text.split():
            where.append("COALESCE(k.player_name, '') LIKE ?")
            args.append(f"%{w}%")
        if season:
            where.append("COALESCE(k.class_name, '') LIKE ?")
            args.append(f"{season}%")
        if grade:
            where.append("k.grade = ?")
            args.append(int(grade))
        if lo:
            where.append("s.current_price >= ?")
            args.append(lo)
        if hi:
            where.append("s.current_price <= ?")
            args.append(hi)
        keys = [(r[0], r[1]) for r in con.execute(
            f"""SELECT k.spid, k.grade FROM card k JOIN price_status s USING (spid, grade)
                WHERE {' AND '.join(where)} ORDER BY s.current_price DESC LIMIT 60""", args)]
    ctx = rankerpick.context(con)
    cand = rankerpick.candidate_keys(con, ctx["snap"]["id"], ctx["cfg"]) if ctx["snap"] else {}
    r = float(ctx["settings"]["buyPrice"]["minTargetRoi"])
    min_tr = int(ctx["cfg"]["minTransfers2h"])
    rows = []
    for spid, g in keys:
        v = rankerpick.card_view(con, ctx, spid, g)
        why = None
        if (spid, g) not in rec_keys:
            cur = v["current_price"]
            if (spid, g) not in cand:
                why = "인기 팀컬러 상위 카드 아님 (랭커 픽 후보 밖)"
            elif cur and not (ctx["lo"] <= cur <= ctx["hi"]):
                why = f"가격대({ctx['preset']}) 밖"
            elif v["drop_pct"] is not None and v["drop_pct"] <= 0:
                why = f"지난 주말보다 {-v['drop_pct']:.1f}% 비쌈"
            elif v["transfers_2h"] is None:
                why = "2시간 거래 아직 모름"
            elif v["transfers_2h"] < min_tr:
                why = f"2시간 거래 {v['transfers_2h']}건 ({min_tr}건 미만)"
            else:
                why = "랭커 픽 추천 조건 미충족"
        v["judge"] = judge_buy(v["current_price"], v["weekend_avg"], ctx["fee"], ctx["fee_coupon"], r,
                               (spid, g) in rec_keys, why)
        v["watched"] = (spid, g) in watched
        v["chg7_pct"] = rankerpick.price_change(con, spid, g, 7)
        v.pop("appearances", None)  # 목록에는 필요 없음 (선수 창에서 따로 받음)
        rows.append(v)
    return {"mode": mode, "rows": rows, "fee": ctx["fee"], "coupon": ctx["coupon"], "min_roi": r}


def m_seasons(con) -> list[str]:
    """필터용 시즌 목록 (가격을 받은 카드의 시즌 약칭, 카드 많은 순)."""
    rows = con.execute(
        """SELECT k.class_name, COUNT(*) n FROM card k JOIN price_status s USING (spid, grade)
           WHERE s.status = 'ok' AND k.class_name IS NOT NULL GROUP BY k.class_name ORDER BY n DESC""").fetchall()
    return [r[0].split(" (")[0] for r in rows]


def set_watch(con, b: dict) -> dict:
    spid, grade = _int(b.get("spid"), "spid"), _int(b.get("grade"), "강화")
    if b.get("on"):
        con.execute("INSERT OR IGNORE INTO watch (spid, grade, created_at) VALUES (?, ?, ?)", (spid, grade, now_kst()))
    else:
        con.execute("DELETE FROM watch WHERE spid=? AND grade=?", (spid, grade))
    con.commit()
    return {"ok": True, "on": bool(b.get("on"))}


def price_history(con, spid: int, grade: int) -> dict:
    """그래프용: 넥슨 일별 가격 + 현재가 관측 (데이터센터 + fionhub, 시각 순)."""
    daily = [[r[0], r[1]] for r in con.execute(
        "SELECT day, price FROM price_daily WHERE spid=? AND grade=? AND price > 0 ORDER BY day", (spid, grade))]
    cur = [[r[0], r[1]] for r in con.execute(
        "SELECT observed_at, price FROM price_current WHERE spid=? AND grade=? AND price > 0", (spid, grade))]
    cur += [[r[0], r[1]] for r in con.execute(
        "SELECT fetched_at, price FROM market_snapshot WHERE spid=? AND grade=? AND price > 0", (spid, grade))]
    return {"daily": daily, "current": sorted(cur)}


def card_detail(con, spid: int, grade: int) -> dict | None:
    row = con.execute(CARD_SQL + " WHERE k.spid = ? AND k.grade = ?", (spid, grade)).fetchone()
    if not row:
        return None
    daily = [[r["day"], r["price"]] for r in con.execute(
        "SELECT day, price FROM price_daily WHERE spid=? AND grade=? ORDER BY day", (spid, grade))]
    current = [[r["observed_at"], r["price"]] for r in con.execute(
        "SELECT observed_at, price FROM price_current WHERE spid=? AND grade=? ORDER BY observed_at", (spid, grade))]
    parts = json.loads(con.execute("SELECT parts_json FROM deal_stats WHERE spid=? AND grade=?",
                                   (spid, grade)).fetchone()[0])
    squads = [dict(r) for r in con.execute(
        """SELECT s.rank, s.nickname, s.is_sub, s.match_date FROM squad_entry s
           WHERE s.spid=? AND s.grade=? AND s.rank<=20
             AND s.snap_id=(SELECT MAX(snap_id) FROM ranking_snapshot WHERE status='complete')
           ORDER BY s.rank""", (spid, grade))]
    held = [dict(r) for r in con.execute("SELECT id, qty, buy_price, bought_at FROM holding WHERE spid=? AND grade=?",
                                         (spid, grade))]
    mk = fionhub.latest(con, spid, grade)
    if mk and mk["lower_limit"] and mk["upper_limit"]:
        s = load_settings()
        f = s["fee"]
        mk["flip_roi_pct"] = flip_roi(mk["lower_limit"], mk["upper_limit"], float(f["sellFeeRate"])) * 100
        best = best_coupon(con)
        mk["flip_roi_coupon_pct"] = flip_roi(mk["lower_limit"], mk["upper_limit"],
                                             effective_fee(float(f["baseRate"]), standing_discount(f), best)) * 100 if best else None
        mk["coupon"] = best
    return {"card": dict(row), "daily": daily, "current": current, "parts": parts, "squads": squads, "held": held,
            "market": mk, "market_history": fionhub.history(con, spid, grade)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 이면 다른 기기(공유기 포트 포워딩)에서도 접속. 비밀번호가 설정돼 있어야만 허용")
    args = ap.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1") and not auth.enabled():
        print("비밀번호 없이 외부에 열 수 없습니다. 먼저: python -m fco.auth set", file=sys.stderr)
        return 1
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    con = connect()
    backup_user_records(con, "서버 시작")
    print(f"대시보드: http://127.0.0.1:{args.port}  (끄려면 Ctrl+C)  DB: {DB_PATH}", flush=True)
    con.close()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
