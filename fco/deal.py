"""8단계: Deal Score (순수 계산).

지표 (높을수록 좋게 정렬)
  popularity  5단계 인기 스쿼드 점수 (1~20위). popularityBlendTop200 > 0 이면 1~200위 사용자 수 백분위를 그 비율만큼 섞는다.
  discount    4단계 할인율 = (평일 평균 - 현재가) / 평일 평균
  weekend     7단계 '사용 주말 프리미엄' (보수적 값, 일시적이면 0). 반복성 판단 보류 카드는 데이터 부족.
  stability   6단계 30일 CV 의 역순 (CV 낮을수록 높음). 가격 정체 카드는 CV 를 믿을 수 없어 데이터 부족.

정규화
  같은 가격대 그룹(현재가 기준, priceBandEdges 로 나눈 구간) 안에서 백분위.
  단 인기 지표는 popularityNormalization = "ratio_to_max" 이면 '그룹 최고 인기 점수 대비 비율'
  (점수 / 그룹 최고점, 그룹 최고점이 0 이면 0). 대부분 0 점이라 백분위로는 1점과 26점이 거의 같아지기 때문.
  백분위 = (그 값보다 엄격히 작은 카드 수) / (그룹 내 지표 보유 카드 수 - 1)   → 최저 0, 최고 1, 동점은 같은 값
  그룹이 minGroupSize 보다 작으면 전체 카드로 비교 ('전체' 그룹).

점수
  사용 가능한 지표만으로: base = Σ(배점_i × 백분위_i) / Σ(사용 가능한 배점_i) × 100
  데이터 신뢰도 = Σ(사용 가능한 배점_i)  (100 이면 4개 지표 모두 있음)
  위험 감점 = riskPenalty 에 있는 위험 표시(급락·지속 하락·당일 급락) 합
  Deal Score = max(0, base - 위험 감점)
"""
from bisect import bisect_left, bisect_right


def percentile(values: dict) -> dict:
    n = len(values)
    if n == 1:
        return {k: 1.0 for k in values}
    srt = sorted(values.values())
    return {k: bisect_left(srt, v) / (n - 1) for k, v in values.items()}


def _fmt(v: float) -> str:
    return f"{v / 1e8:g}억" if v >= 1e8 else f"{v:g}"


def _band_label(i: int, edges: list) -> str:
    lo = "0" if i == 0 else _fmt(edges[i - 1])
    hi = "" if i >= len(edges) else _fmt(edges[i])
    return f"{lo}~{hi}"


def _metrics(c: dict) -> dict:
    stable = c.get("l_cv") is not None and not c.get("stale")
    weekend_ok = c.get("weekly_status") == "ok" and c.get("transient") is not None \
        and c.get("premium_used_pct") is not None
    return {
        "popularity": c.get("pop_score"),
        "discount": c.get("discount_pct") if c.get("weekly_status") == "ok" else None,
        "weekend": c["premium_used_pct"] if weekend_ok else None,
        "stability": -c["l_cv"] if stable else None,
    }


def compute_deal_scores(cards: list[dict], cfg: dict, fee: float) -> dict:
    edges = [float(e) for e in cfg["priceBandEdges"]]
    weights = {k: float(v) for k, v in cfg["weights"].items()}
    blend = float(cfg.get("popularityBlendTop200", 0))
    penalties = {k: float(v) for k, v in cfg["riskPenalty"].items()}

    band_of = {c["key"]: bisect_right(edges, c["current_price"]) for c in cards if c.get("current_price")}
    sizes = {}
    for b in band_of.values():
        sizes[b] = sizes.get(b, 0) + 1
    group = {}
    for c in cards:
        b = band_of.get(c["key"])
        group[c["key"]] = "전체" if b is None or sizes[b] < int(cfg["minGroupSize"]) else _band_label(b, edges)

    mets = {c["key"]: _metrics(c) for c in cards}
    users_of = {c["key"]: c.get("users_top_universe", 0) for c in cards}
    pct = {k: {} for k in weights}
    for g in set(group.values()):
        members = [c["key"] for c in cards if group[c["key"]] == g]
        for m in weights:
            vals = {k: mets[k][m] for k in members if mets[k][m] is not None}
            if not vals:
                continue
            if m == "popularity" and cfg.get("popularityNormalization") == "ratio_to_max":
                top = max(vals.values())
                pct[m].update({k: (v / top if top > 0 else 0.0) for k, v in vals.items()})
            else:
                pct[m].update(percentile(vals))
        if blend > 0:
            users = percentile({k: users_of[k] for k in members})
            for k in members:
                if k in pct["popularity"]:
                    pct["popularity"][k] = (1 - blend) * pct["popularity"][k] + blend * users[k]

    out = {}
    for c in cards:
        k = c["key"]
        avail = {m: w for m, w in weights.items() if k in pct[m]}
        total_w = sum(avail.values())
        parts = {}
        for m, w in weights.items():
            if m in avail:
                p = pct[m][k]
                parts[m] = {"raw": mets[k][m], "percentile": p, "weight": w,
                            "points": w * p * 100 / total_w}
            else:
                parts[m] = {"raw": None, "percentile": None, "weight": w, "points": None}
        base = sum(p["points"] for p in parts.values() if p["points"] is not None) if total_w else None
        flags = [f for f in (c.get("risk_flags") or "").split(",") if f]
        penalty = sum(penalties.get(f, 0) for f in flags)
        warnings = []
        names = {"sharp_drop": "최근 급락", "persistent_decline": "지속 하락", "current_gap": "당일 급락",
                 "stale": "가격 정체"}
        warnings += [names[f] for f in flags if f in names]
        if c.get("stale") and "가격 정체" not in warnings:
            warnings.append("가격 정체")
        if total_w < sum(weights.values()) or (c.get("price_days") or 0) < 60:
            warnings.append("가격 기록 부족")
        if c.get("transient") == 1:
            warnings.append("일시적 주말 차이")
        if c.get("drop_alert") == 1:
            warnings.append("인기 순위 급락")
        if c.get("buy_fair") is not None and c.get("current_price") and c["current_price"] > c["buy_fair"]:
            warnings.append("현재가 > 매수 적정가")
        if c.get("roi_at_current_pct") is not None and c["roi_at_current_pct"] < fee * 100:
            warnings.append("예상 수익률 < 수수료율")
        out[k] = {
            "group": group[k], "parts": parts, "base": base, "penalty": penalty,
            "final": None if base is None else max(0.0, base - penalty),
            "reliability_pct": total_w,
            "reliability": "높음" if total_w >= 100 else "보통" if total_w >= 75 else "낮음",
            "warnings": warnings,
        }
    return out
