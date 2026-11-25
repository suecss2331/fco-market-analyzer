"""6단계: 가격 안정성 (순수 계산).

기간별 (단기 shortDays=7일, 장기 longDays=30일, 모두 기준일 포함):
  n / coverage   기록된 날 수, 기록 비율 (= n / 기간 일수)  → 데이터 연속성
  max_gap_days   기간 안에서 연속으로 빠진 가장 긴 일수
  mean, std      평균, 모표준편차
  cv             변동계수 = std / mean  (낮을수록 안정)
  change_pct     변동률 = (기간 마지막 값 - 첫 값) / 첫 값 × 100
  range_pct      (최고 - 최저) / 평균 × 100
  trend_pct_per_day  ln(가격) 의 최소제곱 기울기를 하루 % 로 환산
  trend          '상승' / '하락' / '보합'  (±trendFlatPctPerDay 기준)

급변 (최근 sharpLookbackDays 일 안의 '전날 대비' 변화, 연속된 날끼리만 비교):
  max_daily_drop_pct / max_daily_rise_pct
  sharp_drop / sharp_rise  : ±sharpMovePct 이상
지속 하락:
  persistent_decline = 장기 변동률 ≤ persistentDeclinePct 이고 장기 추세 '하락' 이고 단기 추세가 '상승' 이 아님

가격 정체:
  flat_run_days = 기준일부터 거슬러 올라가며 같은 값이 이어진 날 수 (연속 기록 기준)
  stale = flat_run_days ≥ staleDays  → 거래가 없어 값이 멈췄을 가능성. CV 가 낮아도 '안정'으로 보지 않는다.

기록 수가 minShortPoints / minLongPoints 미만인 기간은 지표를 계산하지 않는다 (None).
"""
import math
from datetime import date, timedelta
from statistics import mean, pstdev


def _window(by_day: dict, end: date, days: int, min_points: int, flat: float) -> dict:
    start = end - timedelta(days=days - 1)
    ds = [start + timedelta(days=i) for i in range(days)]
    pts = [(d, by_day[d]) for d in ds if d in by_day]
    gap = cur = 0
    for d in ds:
        cur = 0 if d in by_day else cur + 1
        gap = max(gap, cur)
    w = {"days": days, "n": len(pts), "coverage": len(pts) / days, "max_gap_days": gap,
         "mean": None, "std": None, "cv": None, "change_pct": None, "range_pct": None,
         "trend_pct_per_day": None, "trend": None}
    if len(pts) < min_points:
        return w
    vals = [p for _, p in pts]
    m = mean(vals)
    s = pstdev(vals)
    w.update(mean=m, std=s, cv=s / m, change_pct=(vals[-1] - vals[0]) / vals[0] * 100,
             range_pct=(max(vals) - min(vals)) / m * 100)
    xs = [(d - start).days for d, _ in pts]
    ys = [math.log(v) for v in vals]
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    tpd = (math.exp(slope) - 1) * 100
    w["trend_pct_per_day"] = tpd
    w["trend"] = "상승" if tpd > flat else "하락" if tpd < -flat else "보합"
    return w


def compute_stability(points: list[tuple[date, int]], end: date, cfg: dict) -> dict:
    by_day = {d: p for d, p in points if p and p > 0 and d <= end}
    flat = float(cfg["trendFlatPctPerDay"])
    short = _window(by_day, end, int(cfg["shortDays"]), int(cfg["minShortPoints"]), flat)
    long = _window(by_day, end, int(cfg["longDays"]), int(cfg["minLongPoints"]), flat)

    look = int(cfg["sharpLookbackDays"])
    moves = []
    for i in range(look):
        d = end - timedelta(days=i)
        prev = d - timedelta(days=1)
        if d in by_day and prev in by_day:
            moves.append((by_day[d] - by_day[prev]) / by_day[prev] * 100)
    sharp = float(cfg["sharpMovePct"])
    max_drop = min(moves) if moves else None
    max_rise = max(moves) if moves else None

    run, d = 0, end
    while d in by_day and (run == 0 or by_day[d] == by_day[end]):
        run += 1
        d -= timedelta(days=1)

    out = {
        "short": short, "long": long,
        "flat_run_days": run, "stale": run >= int(cfg["staleDays"]),
        "max_daily_drop_pct": max_drop, "max_daily_rise_pct": max_rise,
        "sharp_drop": max_drop is not None and max_drop <= -sharp,
        "sharp_rise": max_rise is not None and max_rise >= sharp,
        "persistent_decline": False, "status": "ok", "note": "",
    }
    if long["cv"] is not None:
        out["persistent_decline"] = (long["change_pct"] <= float(cfg["persistentDeclinePct"])
                                     and long["trend"] == "하락" and short["trend"] != "상승")

    notes = []
    if short["cv"] is None:
        notes.append(f"최근 {short['days']}일 기록 {short['n']}일 (최소 {cfg['minShortPoints']}일)")
    if long["cv"] is None:
        notes.append(f"최근 {long['days']}일 기록 {long['n']}일 (최소 {cfg['minLongPoints']}일)")
    if notes:
        out["status"] = "insufficient" if long["cv"] is None and short["cv"] is None else "partial"
        out["note"] = "데이터 부족: " + ", ".join(notes)
    return out
