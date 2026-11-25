"""탭3: 10억 BP 이상 고가 선수 장기투자 (상승 추세형 Momentum / 저평가 선점형 Value — 서로 독립 점수 · 독립 순위).

데이터 (모두 기존 수집분 재사용, 없는 값은 만들지 않음)
  일별 가격 · 현재가      : 넥슨 데이터센터 (price_daily 최대 365일, price_status.current_price)
  인기 순위               : fc-info 랭커 픽 — 인기 팀컬러 topTeams(20)위 팀의 포지션별 순위 중 가장 좋은 순위 (시즌 카드 기준).
                            fc-info 는 포지션별 15위까지만 보여 주므로 16위 이하는 '순위권 밖'으로만 안다.
  인기 순위 변화          : 20시간 이상 전 fc-info 표와 비교 (기록이 쌓이기 전에는 없음 → 그 몫만 빼고 계산)
  거래량 · 매물           : fionhub 2시간 거래 · 구매/판매 대기 (실제로 받은 카드만. 못 받은 카드는 이 항목을 빼고 나머지로 100점 환산)
  급등 · 급락 · 지속 하락  : 기존 6단계 가격 안정성 (stability_stats)

점수: 항목마다 0~1 비율 f 를 구해 '가중치 × f' 를 더하고, 데이터가 있는 항목 가중치 합으로 나눠 100점 환산.
가중치는 settings.json longTerm.momentumWeights / valueWeights.
"""
import json
import math
import statistics
from datetime import date, timedelta

from . import fionhub


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


# --- 가격 지표 (순수 계산) -------------------------------------------------------
def price_at_or_before(series: list, d: date):
    """series = [(date, price)] 오름차순. d 이전(포함) 가장 가까운 값."""
    best = None
    for day, p in series:
        if day <= d:
            best = p
        else:
            break
    return best


def linfit(ys: list) -> tuple[float, float]:
    """log 가격 직선 회귀: (하루 기울기, R²). 점이 3개 미만이면 (0, 0)."""
    n = len(ys)
    if n < 3:
        return 0.0, 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx if sxx else 0.0
    r2 = (sxy * sxy) / (sxx * syy) if sxx and syy else 0.0
    return slope, r2


def metrics(series: list, current: float, today: date) -> dict:
    """일별 가격 + 현재가로 계산한 지표. 데이터가 모자라면 해당 값은 None."""
    s = [(d, p) for d, p in series if p and p > 0]
    out = {"days": len(s)}
    if not s or not current:
        return out
    last = s[-1][0]

    def chg(days):
        base = price_at_or_before(s, last - timedelta(days=days))
        return current / base - 1 if base and (last - s[0][0]).days >= days else None
    out["r7"], out["r30"], out["r3"] = chg(7), chg(30), chg(3)
    ath_day, ath = max(s, key=lambda x: x[1])
    out["ath"], out["ath_day"] = max(ath, current), (ath_day.isoformat() if ath >= current else today.isoformat())
    out["disc"] = 1 - current / out["ath"]
    last30 = [p for d, p in s if d > last - timedelta(days=30)]
    if len(last30) >= 10:
        slope30, r2 = linfit([math.log(p) for p in last30])
        slope10, _ = linfit([math.log(p) for p in last30[-10:]])
        weekly = [price_at_or_before(s, last - timedelta(days=k)) for k in (28, 21, 14, 7, 0)]  # 1주 간격 5개 시점
        ups = sum(1 for a, b in zip(weekly, weekly[1:]) if a and b and b > a)
        out.update(slope30=slope30, r2_30=r2, slope10=slope10, up_weeks=ups)
    last14 = [p for d, p in s if d > last - timedelta(days=14)]
    if len(last14) >= 7:
        m = statistics.mean(last14)
        out["cv14"] = statistics.pstdev(last14) / m if m else None
        out["chg14"] = current / last14[0] - 1
        out["min14"], out["med14"] = min(last14), statistics.median(last14)
    last7 = [p for d, p in s if d > last - timedelta(days=7)]
    if last7:
        out["min7"], out["ma7"] = min(last7), statistics.mean(last7)
    return out


# --- 인기 ---------------------------------------------------------------------
def pop_fraction(rank: int | None) -> float:
    """인기 순위 → 0~1. 1~10위 최고 (1위 1.00 ~ 10위 0.82), 11~15위 높음 (0.72~0.60), 순위권 밖 낮음 (0.15)."""
    if rank is None:
        return 0.15
    if rank <= 10:
        return 1.0 - (rank - 1) * 0.02
    if rank <= 20:
        return 0.72 - (rank - 11) * 0.03
    if rank <= 50:
        return 0.35
    return 0.15


def pop_with_change(rank, rank_prev) -> tuple[float, str | None]:
    f = pop_fraction(rank)
    if rank_prev is None or rank is None:
        return f, None
    if rank < rank_prev:
        return clamp(f + 0.10), f"인기 순위 상승 ({rank_prev}→{rank}위)"
    if rank > rank_prev:
        return clamp(f - 0.15), f"인기 순위 하락 ({rank_prev}→{rank}위)"
    return f, None


def volume_fraction(mk: dict | None) -> float | None:
    """fionhub 2시간 거래 · 구매/판매 대기로 '매물 소화' 정도. 데이터 없으면 None (항목 제외)."""
    if not mk or mk.get("transfers_2h") is None:
        return None
    tr = mk["transfers_2h"]
    buy = sum(c or 0 for _p, c in mk.get("buy_levels") or [])
    sell = sum(c or 0 for _p, c in mk.get("sell_levels") or [])
    f_tr = clamp(math.log1p(tr) / math.log1p(20))
    f_bal = buy / (buy + sell) if buy + sell else 0.5
    return 0.6 * f_tr + 0.4 * f_bal


def weighted(parts: dict, weights: dict) -> tuple[float | None, dict]:
    """parts = {항목: f 또는 None}. 데이터 있는 항목만으로 100점 환산."""
    num = den = 0.0
    detail = {}
    for k, w in weights.items():
        f = parts.get(k)
        detail[k] = {"weight": w, "f": f, "points": None if f is None else w * f}
        if f is not None:
            num += w * f
            den += w
    return (num / den * 100 if den else None), detail


def grade(score: float | None) -> str:
    if score is None:
        return "-"
    return "S" if score >= 85 else "A" if score >= 75 else "B" if score >= 65 else "C" if score >= 50 else "D"


GRADE_TEXT = {"S": "적극적인 매수 검토", "A": "매수 후보", "B": "관찰 대상", "C": "매수 보류", "D": "투자 제외", "-": "분석 불가"}


# --- 상승 추세형 --------------------------------------------------------------
def momentum(m: dict, pop: tuple, vol, flags: dict, weights: dict) -> dict:
    r7, r30 = m.get("r7"), m.get("r30")
    reasons, risks = [], []
    # 7일 +15%, 30일 +40% 에서 만점 (실측: 시장 전반이 강세일 때 더 낮은 기준이면 대부분 만점이 됨)
    parts = {"r7": None if r7 is None else clamp(r7 / 0.15),
             "r30": None if r30 is None else clamp(r30 / 0.40)}
    if "slope30" in m:
        if m["slope30"] <= 0:
            persist = 0.0
        else:
            persist = 0.5 * m["r2_30"] + 0.5 * m["up_weeks"] / 4
            if m["slope10"] <= 0:
                persist *= 0.6
                risks.append("최근 10일 상승 꺾임")
            elif m["slope10"] < 0.5 * m["slope30"]:
                persist *= 0.85
                reasons.append("상승 둔화")
            else:
                reasons.append("상승 유지/가속")
        parts["persist"] = persist
    else:
        parts["persist"] = None
    parts["pop"] = pop[0]
    parts["volume"] = vol
    # 급등 위험: 7일 +15% 넘으면 깎이기 시작 (+35% 면 0), 30일 +80% 넘게 오른 '과열'도 깎음, 하루 ±15% 급등 이력이면 최대 0.3
    risk_f = 1.0 if r7 is None else 1 - clamp((r7 - 0.15) / 0.20)
    if r30 is not None:
        risk_f = min(risk_f, 1 - clamp((r30 - 0.80) / 0.80))
    if flags.get("sharp_rise"):
        risk_f = min(risk_f, 0.3)
    parts["risk"] = risk_f
    score, detail = weighted(parts, weights)
    if r7 is not None and r7 > 0:
        reasons.insert(0, f"7일 +{r7 * 100:.1f}%")
    if r30 is not None and r30 > 0:
        reasons.insert(1 if reasons else 0, f"30일 +{r30 * 100:.1f}%")
    overheated = (r7 is not None and r7 > 0.20) or (r30 is not None and r30 > 0.80) or bool(flags.get("sharp_rise"))
    if overheated:
        risks.append("단기 급등 — 추격 매수 주의")
    if flags.get("sharp_drop"):
        risks.append("최근 급락 이력")
    if pop[1]:
        (reasons if "상승" in pop[1] else risks).append(pop[1])
    # 매수 적정가: 기본은 현재가보다 3~5% 낮은 대기가, 그 안에서 7일 평균을 따름. 급등 상태면 7일 평균까지 기다림.
    fair, wait = None, False
    if m.get("ma7") and m.get("days", 0) >= 14:
        cur = m["cur"]
        if overheated:
            fair, wait = min(m["ma7"], cur * 0.95), True
        else:
            fair = min(max(m["ma7"], cur * 0.95), cur * 0.97)
    return {"score": score, "grade": grade(score), "parts": detail, "reasons": reasons, "risks": risks,
            "fair": fair, "wait": wait, "overheated": overheated}


# --- 저평가 선점형 ------------------------------------------------------------
def value(m: dict, pop: tuple, vol, flags: dict, rising: bool, weights: dict) -> dict:
    r30, disc = m.get("r30"), m.get("disc")
    reasons, risks = [], []
    parts = {"pop": pop[0],
             "discount": None if disc is None else clamp((disc - 0.05) / 0.35)}
    # 가격 상승 미반영: 인기는 높은데 30일 가격이 덜 올랐을수록 (+ fc-info 인기 급등 선수면 가산)
    if r30 is None:
        parts["unpriced"] = None
    else:
        gap = pop[0] - clamp(r30 / 0.20)
        parts["unpriced"] = clamp(gap + (0.2 if rising else 0.0))
    if m.get("cv14") is not None:
        parts["stability"] = clamp(1 - m["cv14"] / 0.10) * (1.0 if m["chg14"] >= -0.05 else 0.5)
    else:
        parts["stability"] = None
    parts["volume"] = vol
    if m.get("r3") is not None and m.get("min7"):
        up3, off_low = m["r3"] > 0, m["cur"] >= m["min7"] * 1.02
        parts["rebound"] = 1.0 if (up3 and off_low) else 0.5 if up3 else 0.0
    else:
        parts["rebound"] = None
    score, detail = weighted(parts, weights)
    if disc is not None and disc > 0:
        reasons.append(f"고점 대비 −{disc * 100:.1f}%")
    if rising:
        reasons.append("fc-info 인기 급등 선수")
    if parts.get("rebound") == 1.0:
        reasons.append("반등 신호")
    if pop[1]:
        (reasons if "상승" in pop[1] else risks).append(pop[1])
    if flags.get("persistent_decline"):
        risks.append("지속 하락 중")
    if flags.get("sharp_drop"):
        risks.append("최근 급락")
    if pop[1] and "하락" in pop[1] and (m.get("chg14") or 0) < -0.05:
        risks.append("인기와 가격이 함께 하락 — 수요 감소 의심")
    # 매수 적정가: 최근 14일 지지선(최저)과 중앙값의 중간, 현재가 이하. 흔들림이 크거나 기록이 짧으면 계산하지 않음.
    fair = None
    if m.get("min14") and m.get("days", 0) >= 30 and m.get("cv14") is not None and m["cv14"] <= 0.15:
        fair = min(m["cur"], (m["min14"] + m["med14"]) / 2)
    return {"score": score, "grade": grade(score), "parts": detail, "reasons": reasons, "risks": risks, "fair": fair}


def sell_signals(m: dict, pop: tuple, mk: dict | None, overheated: bool) -> list[str]:
    """보유 중일 때 매도 검토 신호 (장기투자: 근거가 깨졌는지 위주)."""
    out = []
    if m.get("slope10") is not None and m["slope10"] < 0 and (m.get("r7") or 0) < -0.05:
        out.append("30일 상승 추세 꺾임")
    if pop[1] and "하락" in pop[1]:
        out.append(pop[1])
    if mk:
        sell = sum(c or 0 for _p, c in mk.get("sell_levels") or [])
        if sell >= 10:
            out.append(f"판매 대기 {sell}건 — 매물 증가")
    if overheated:
        out.append("단기 과열")
    return out


# --- 모아서 계산 ---------------------------------------------------------------
def rank_maps(con, snap_id: int, top_teams: int) -> dict:
    """spid → (가장 좋은 포지션 순위, 그 팀, 포지션, 픽률)."""
    best = {}
    for r in con.execute(
            """SELECT p.spid, p.rank, p.team, p.position, p.pick_rate FROM rp_pick p
               JOIN rp_team t ON t.snap_id = p.snap_id AND t.name = p.team
               WHERE p.snap_id = ? AND t.rank <= ?""", (snap_id, top_teams)):
        cur = best.get(r[0])
        if cur is None or r[1] < cur[0] or (r[1] == cur[0] and (r[4] or 0) > (cur[3] or 0)):
            best[r[0]] = (r[1], r[2], r[3], r[4])
    return best


def analyze(con, settings: dict) -> dict:
    lt = settings["longTerm"]
    min_price = float(lt["minPrice"])
    top_teams = int(settings["rankerPick"]["topTeams"])
    snap = con.execute("SELECT * FROM rp_snapshot WHERE status='complete' ORDER BY id DESC LIMIT 1").fetchone()
    ranks = rank_maps(con, snap["id"], top_teams) if snap else {}
    prev = None
    if snap:  # 20시간 이상 전 fc-info 표 (인기 순위 변화용)
        prev = con.execute("""SELECT id, collected_at FROM rp_snapshot WHERE status='complete'
                              AND julianday(?) - julianday(collected_at) >= ? ORDER BY id DESC LIMIT 1""",
                           (snap["collected_at"], 20 / 24)).fetchone()
    ranks_prev = rank_maps(con, prev["id"], top_teams) if prev else {}
    rising = {r[0] for r in con.execute("SELECT DISTINCT spid FROM rp_rising WHERE snap_id = ?", (snap["id"],))} if snap else set()
    held = {(r[0], r[1]) for r in con.execute("SELECT spid, grade FROM holding")}
    today = date.today()
    rows, held_rows = [], []
    for c in con.execute(
            """SELECT s.spid, s.grade, s.current_price, k.player_name, k.class_name,
                      t.sharp_drop, t.sharp_rise, t.persistent_decline, t.stale
               FROM price_status s JOIN card k USING (spid, grade) LEFT JOIN stability_stats t USING (spid, grade)
               WHERE s.status = 'ok' AND s.current_price >= ?""", (min_price,)):
        series = [(date.fromisoformat(d), p) for d, p in con.execute(
            "SELECT day, price FROM price_daily WHERE spid=? AND grade=? ORDER BY day", (c["spid"], c["grade"]))]
        m = metrics(series, c["current_price"], today)
        m["cur"] = c["current_price"]
        rk = ranks.get(c["spid"])
        rk_prev = ranks_prev.get(c["spid"])
        pop = pop_with_change(rk[0] if rk else None, rk_prev[0] if rk_prev else None)
        mk = fionhub.latest(con, c["spid"], c["grade"])
        vol = volume_fraction(mk)
        flags = {k: c[k] for k in ("sharp_drop", "sharp_rise", "persistent_decline", "stale")}
        mo = momentum(m, pop, vol, flags, lt["momentumWeights"])
        va = value(m, pop, vol, flags, c["spid"] in rising, lt["valueWeights"])
        data_short = m.get("days", 0) < 30
        if data_short:
            mo["risks"].append(f"가격 기록 {m.get('days', 0)}일 (30일 미만)")
            va["risks"].append(f"가격 기록 {m.get('days', 0)}일 (30일 미만)")
        if flags.get("stale"):
            for x in (mo, va):
                x["risks"].append("가격 정체 (같은 값 반복)")
        ranked = rk is not None and rk[0] <= int(lt["popularRankMax"])
        if (c["spid"], c["grade"]) in held:
            held_rows.append({"spid": c["spid"], "grade": c["grade"], "player_name": c["player_name"],
                              "class_name": c["class_name"], "current_price": c["current_price"],
                              "r7": m.get("r7"), "r30": m.get("r30"), "pop_rank": rk[0] if rk else None,
                              "momentum_score": mo["score"], "value_score": va["score"],
                              "sell_signals": sell_signals(m, pop, mk, mo["overheated"])})
        row = {"spid": c["spid"], "grade": c["grade"], "player_name": c["player_name"], "class_name": c["class_name"],
               "current_price": c["current_price"], "r7": m.get("r7"), "r30": m.get("r30"), "disc": m.get("disc"),
               "ath": m.get("ath"), "ath_day": m.get("ath_day"), "days": m.get("days"),
               "pop_rank": rk[0] if rk else None, "pop_team": rk[1] if rk else None, "pop_pos": rk[2] if rk else None,
               "pop_pick_rate": rk[3] if rk else None, "pop_rank_prev": rk_prev[0] if rk_prev else None,
               "ranked": ranked, "held": (c["spid"], c["grade"]) in held,
               "transfers_2h": mk and mk["transfers_2h"], "volume_known": vol is not None,
               "sell_signals": sell_signals(m, pop, mk, mo["overheated"]) if (c["spid"], c["grade"]) in held else None}
        # 상승 추세형 목록: 30일 · 7일이 모두 플러스인 카드 (예외: 순위권 밖이면 30일 +30% 이상 · 지속성 0.6 이상)
        if m.get("r30") is not None and m.get("r7") is not None and m["r30"] > 0 and m["r7"] > 0:
            persist = mo["parts"]["persist"]["f"] or 0
            if ranked or (m["r30"] >= 0.30 and persist >= 0.6):
                rows.append(dict(row, kind="momentum", exception=not ranked, **_verdict(mo, ranked, c["current_price"], lt)))
        # 저평가 선점형 목록: 고점 대비 10% 이상 할인 (예외: 순위권 밖이면 할인 30% 이상 + 14일 안정)
        if m.get("disc") is not None and m["disc"] >= 0.10:
            stab = va["parts"]["stability"]["f"]
            if ranked or (m["disc"] >= 0.30 and (stab or 0) >= 0.6):
                rows.append(dict(row, kind="value", exception=not ranked, **_verdict(va, ranked, c["current_price"], lt)))
    mom = sorted([r for r in rows if r["kind"] == "momentum"], key=lambda r: -(r["score"] or -1))
    val = sorted([r for r in rows if r["kind"] == "value"], key=lambda r: -(r["score"] or -1))
    for lst in (mom, val):
        for i, r in enumerate(lst, 1):
            r["rank"] = i
    return {"momentum": mom, "value": val, "held": held_rows,
            "trade_top": {"high": trade_top(con, min_price), "all": trade_top(con, 0)},
            "snapshot": snap and snap["collected_at"],
            "prev_snapshot": prev and prev["collected_at"], "min_price": min_price, "config": lt}


def _verdict(x: dict, ranked: bool, cur: float, lt: dict) -> dict:
    """매수 후보 = 점수 75 이상 + 인기 순위권 + 위험 없음 + 적정가 이하/근접(+2%)."""
    fair = x["fair"]
    near = fair is not None and cur <= fair * (1 + float(lt["nearFairPct"]))
    if x["score"] is None:
        verdict = "분석 불가"
    elif x["score"] >= float(lt["buyScore"]) and ranked and not x["risks"] and near and not x.get("wait"):
        verdict = "매수 후보"
    elif x["score"] >= float(lt["buyScore"]) and ranked and not x["risks"]:
        verdict = "적정가 대기"
    elif x["risks"] and x["score"] is not None and x["score"] >= float(lt["buyScore"]):
        verdict = "위험 — 매수 제한"
    else:
        verdict = GRADE_TEXT[x["grade"]]
    # inv_grade = 투자 등급 S~D ('grade' 는 카드 강화 단계라 이름을 따로 씀)
    return {"score": x["score"], "inv_grade": x["grade"], "verdict": verdict,
            "fair": fair, "fair_note": None if fair else "분석 데이터 부족",
            "reasons": x["reasons"], "risks": x["risks"], "parts": x["parts"], "wait": x.get("wait", False)}


def trade_top(con, min_price: float = 0, limit: int = 15, max_age_hours: float = 3) -> list[dict]:
    """거래 많은 순 (게임 '최다 조회수' 대신 쓰는 수요 지표). fionhub 2시간 거래 건수, 최근 max_age_hours 안에 받은 값만.
    가격은 fionhub 현재가 (없으면 데이터센터). 받은 적 없는 카드는 순위에 들어갈 수 없다."""
    rows = con.execute(
        """SELECT m.spid, m.grade, m.price, m.transfers_2h, m.lower_limit, m.upper_limit, m.buy_levels, m.sell_levels,
                  m.fetched_at, k.player_name, k.class_name, s.current_price
           FROM market_snapshot m
           JOIN (SELECT spid, grade, MAX(fetched_at) f FROM market_snapshot GROUP BY spid, grade) x
             ON m.spid = x.spid AND m.grade = x.grade AND m.fetched_at = x.f
           LEFT JOIN card k ON k.spid = m.spid AND k.grade = m.grade
           LEFT JOIN price_status s ON s.spid = m.spid AND s.grade = m.grade
           WHERE m.transfers_2h IS NOT NULL
             AND julianday('now', 'localtime') - julianday(substr(m.fetched_at, 1, 19)) <= ?""",
        (max_age_hours / 24,)).fetchall()
    out = []
    for r in rows:
        price = r["price"] or r["current_price"]
        if not price or price < min_price:
            continue
        buy = json.loads(r["buy_levels"] or "[]")
        sell = json.loads(r["sell_levels"] or "[]")
        prev = con.execute("""SELECT transfers_2h FROM market_snapshot WHERE spid=? AND grade=? AND fetched_at < ?
                              AND transfers_2h IS NOT NULL ORDER BY fetched_at DESC LIMIT 1""",
                           (r["spid"], r["grade"], r["fetched_at"])).fetchone()
        out.append({"spid": r["spid"], "grade": r["grade"], "player_name": r["player_name"], "class_name": r["class_name"],
                    "price": price, "transfers_2h": r["transfers_2h"], "transfers_prev": prev[0] if prev else None,
                    "lower": r["lower_limit"], "upper": r["upper_limit"],
                    "buy_at_upper": sum(c or 0 for p, c in buy if p == r["upper_limit"]),
                    "buy_total": sum(c or 0 for _p, c in buy), "sell_total": sum(c or 0 for _p, c in sell),
                    "fetched_at": r["fetched_at"]})
    out.sort(key=lambda x: (-x["transfers_2h"], -x["price"]))
    return out[:limit]


def refresh_market(con, settings: dict, progress: bool = False) -> dict:
    """두 순위표 상위 카드의 fionhub 거래량 · 매물 (실제로 받은 것만 점수에 씀)."""
    from . import http
    res = analyze(con, settings)
    n = int(settings["longTerm"]["marketFetchPerList"])
    keys = list(dict.fromkeys([(r["spid"], r["grade"]) for r in res["momentum"][:n] + res["value"][:n]]))
    out = fionhub.collect(con, keys, http.Throttle(1.0), progress)
    return {"requested": len(keys), "ok": out["ok"], "failed": len(out["failed"])}
