"""4단계: 평일·주말 가격 분석 (순수 계산 — DB 와 무관하게 테스트 가능).

입력: 카드 한 장의 일별 가격 [(date, price)], 분석 기준 마지막 날(end), 설정, 현재가.

1) 기준 기간 (end 포함 최근 windowDays 일)
   평일 평균·중앙값 = 월~금 일별 가격,  주말 평균·중앙값 = 토·일 일별 가격
   Weekend Premium (%) = (주말 평균 - 평일 평균) / 평일 평균 × 100      ← 기획서 공식
   Discount (%)        = (평일 평균 - 현재가) / 평일 평균 × 100

2) 반복성 (최근 repetitionWeeks 번의 주말)
   주말마다 '그 주말 토·일 평균' 을 '바로 앞 평일 k일 + 바로 뒤 평일 k일 평균' 과 비교한다.
   (k = 앞뒤 중 적은 쪽 일수, 최대 5)  앞뒤를 같은 일수로 감싸므로 가격이 꾸준히 오르거나
   내리기만 하는 추세는 상쇄되고, 주말에만 생기는 차이만 남는다.
   뒤쪽 평일이 아직 없는 가장 최근 주말은 반복성 계산에서 빠진다.
   weeklyPositiveMinPct 보다 큰 주를 '주말이 비싼 주' 로 센다.
   기획서 공식 프리미엄은 양수인데 그런 주의 비율이 transientPositiveRatio 미만이면 '일시적' 으로 표시한다.
"""
from datetime import date, timedelta
from statistics import mean, median


def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100.0


def compute_weekly(points: list[tuple[date, int]], end: date, cfg: dict,
                   current_price: int | None) -> dict:
    by_day = {d: p for d, p in points if p and p > 0 and d <= end}
    window_start = end - timedelta(days=int(cfg["windowDays"]) - 1)
    wd = [p for d, p in by_day.items() if d >= window_start and d.weekday() < 5]
    we = [p for d, p in by_day.items() if d >= window_start and d.weekday() >= 5]

    out = {
        "window_start": window_start.isoformat(), "window_end": end.isoformat(),
        "wd_n": len(wd), "we_n": len(we),
        "wd_mean": mean(wd) if wd else None, "wd_median": median(wd) if wd else None,
        "we_mean": mean(we) if we else None, "we_median": median(we) if we else None,
        "premium_pct": None, "discount_pct": None,
        "weeks_total": 0, "weeks_positive": 0, "weekly_premium_median_pct": None,
        "transient": None, "status": "ok", "note": "",
    }

    notes = []
    if len(wd) < int(cfg["minWeekdayPoints"]):
        notes.append(f"평일 기록 {len(wd)}일 (최소 {cfg['minWeekdayPoints']}일)")
    if len(we) < int(cfg["minWeekendPoints"]):
        notes.append(f"주말 기록 {len(we)}일 (최소 {cfg['minWeekendPoints']}일)")
    if notes:
        out["status"] = "insufficient"
        out["note"] = "데이터 부족: " + ", ".join(notes)
        return out

    out["premium_pct"] = _pct(out["we_mean"], out["wd_mean"])
    if current_price:
        out["discount_pct"] = (out["wd_mean"] - current_price) / out["wd_mean"] * 100.0

    # --- 반복성 ---------------------------------------------------------
    weekly = []
    sat = end - timedelta(days=(end.weekday() - 5) % 7)  # end 이전(포함) 가장 가까운 토요일
    for _ in range(int(cfg["repetitionWeeks"]) + 1):  # 최근 주말은 뒤 평일이 없어 빠질 수 있으므로 +1
        sun = sat + timedelta(days=1)
        if sat in by_day and sun in by_day:
            before = [by_day[sat - timedelta(days=j)] for j in range(1, 6) if sat - timedelta(days=j) in by_day]
            after = [by_day[sun + timedelta(days=j)] for j in range(1, 6) if sun + timedelta(days=j) in by_day]
            k = min(len(before), len(after))
            if k >= 2:
                base = mean(before[:k] + after[:k])
                weekly.append(_pct((by_day[sat] + by_day[sun]) / 2, base))
        if len(weekly) >= int(cfg["repetitionWeeks"]):
            break
        sat -= timedelta(days=7)

    thr = float(cfg["weeklyPositiveMinPct"])
    out["weeks_total"] = len(weekly)
    out["weeks_positive"] = sum(1 for w in weekly if w > thr)
    if weekly:
        out["weekly_premium_median_pct"] = median(weekly)
    if len(weekly) >= int(cfg["minRepetitionWeeks"]):
        ratio = out["weeks_positive"] / len(weekly)
        out["transient"] = out["premium_pct"] > 0 and ratio < float(cfg["transientPositiveRatio"])
    else:
        out["note"] = f"반복성 판단 보류: 비교 가능한 주말 {len(weekly)}번 (최소 {cfg['minRepetitionWeeks']}번)"
    return out
