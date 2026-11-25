"""5단계: 인기 스쿼드 점수 (순수 계산).

공식경기 랭킹 1~topN 위 구단주가 최근 공식경기에 쓴 카드(spid, 강화)에 대해
  순위 가중치 w(rank) = topN + 1 - rank      (1위 20점, 2위 19점, ..., 20위 1점)
  교체 명단이면 w × subWeight
  카드 점수 = 그 카드를 쓴 스쿼드들의 가중치 합 (한 스쿼드에 두 번 나오면 큰 쪽 한 번만)
  출현 횟수 = 그 카드를 쓴 스쿼드 수 (선발 출현 수는 따로)
만점은 20명 모두가 선발로 쓸 때 1+2+...+20 = 210점.

top 20 밖 카드는 점수 0 이다 (데이터가 없는 것이 아니라 '쓰지 않음'이 관측된 것).
"""


def compute_popularity(entries: list[dict], cfg: dict) -> dict:
    top_n = int(cfg["topN"])
    sub_w = float(cfg["subWeight"])
    per_squad: dict[tuple, dict[int, tuple[float, bool]]] = {}
    for x in entries:
        rank = int(x["rank"])
        if rank < 1 or rank > top_n:
            continue
        key = (x["spid"], x["grade"])
        starter = not x["is_sub"]
        w = (top_n + 1 - rank) * (1.0 if starter else sub_w)
        prev = per_squad.setdefault(key, {}).get(rank)
        if prev is None or w > prev[0]:
            per_squad[key][rank] = (w, starter or (prev[1] if prev else False))
    out = {}
    for key, squads in per_squad.items():
        ranks = sorted(squads)
        out[key] = {
            "score": sum(w for w, _ in squads.values()),
            "appearances": len(ranks),
            "starter_appearances": sum(1 for _, s in squads.values() if s),
            "best_rank": ranks[0],
            "ranks": ranks,
        }
    return out


def compare_snapshots(now: dict, prev: dict | None, cfg: dict) -> dict:
    """직전 스냅샷 대비 점수 변화. 점수가 dropAlertRatio 이상 줄면(0 이 된 경우 포함) 경고."""
    ratio = float(cfg["dropAlertRatio"])
    keys = set(now) | (set(prev) if prev else set())
    out = {}
    for k in keys:
        cur = now.get(k, {}).get("score", 0)
        if prev is None:
            out[k] = {"score": cur, "prev_score": None, "change_pct": None, "drop_alert": None}
            continue
        p = prev.get(k, {}).get("score", 0)
        change = None if p == 0 else (cur - p) / p * 100
        out[k] = {"score": cur, "prev_score": p, "change_pct": change,
                  "drop_alert": p > 0 and cur <= p * (1 - ratio)}
    return out
