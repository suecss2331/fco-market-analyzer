"""랭커 픽 추천 (사용자 기준).

데이터
  fc-info.com/ranker-pick/official : 공식경기 랭커가 선발로 쓴 카드를 팀컬러별 · 포지션별로 모은 표 (하루 1번 갱신)
    robots.txt 허용 (ai-train 만 금지, 이 프로그램은 해당 없음). 1초에 1번 이하로 요청.
  넥슨 데이터센터 : 현재가 · 일별 가격 (지난 주말 가격)
  fionhub          : 2시간 거래 건수 · 상한가 · 하한가 · 카드 팀컬러

후보 고르기 (모두 만족해야 함, 기준값은 settings.json rankerPick)
  1. 인기 팀컬러 topTeams(20)위 안의 팀에서, 포지션별 topRankPerPosition(10)위 안에 든 카드와 같은 시즌(spid)
  2. 강화는 minGrade(8) 이상이면 상관없음 — 랭커가 실제로 쓴 강화 중 8강 이상을 후보로
  3. 현재가 < 지난 주말(토·일) 평균  (주말보다 싸짐)
  4. fionhub 최근 2시간 거래 ≥ minTransfers2h(5)건
  + 활성 가격대(설정의 가격대) 안

점수 (0~100, 후보들 사이 최고값 대비 비율)
  거래량 점수  = log(1 + 2시간 거래) / log(1 + 후보 중 최대)        × weights.transfers (40)
  인기 점수    = 최고 픽률 / 후보 중 최고 픽률                      × weights.pickRate  (35)
  하락 점수    = 주말 대비 하락률 / 후보 중 최대 하락률              × weights.weekendDrop (25)
  같은 카드가 여러 팀·포지션에 나오면 가장 높은 픽률을 쓴다 (나온 곳은 모두 표시).

  python -m fco.rankerpick collect      fc-info 표 받기 (약 25초)
  python -m fco.rankerpick prices       후보 카드 가격 · fionhub 시세 받기
  python -m fco.rankerpick show         점수 표 출력
"""
import argparse
import json
import math
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta

from . import fionhub, http
from .collect_prices import ensure_card, fetch_cards
from .config import active_price_range, load_settings
from .db import KST, connect, log, now_kst

BASE = "https://fc-info.com/ranker-pick/official"
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def rsc_text(html: str) -> str:
    """Next.js 페이지에 실린 데이터 조각을 이어 붙인다."""
    parts = _PUSH.findall(html)
    if not parts:
        raise RuntimeError("fc-info 페이지에서 데이터를 찾지 못함 (구조 변경·차단 가능성) → 건너뜀")
    return "".join(json.loads(p) for p in parts)


def _array_after(text: str, key: str, need: str | None = None):
    """'"key":[' 뒤의 배열. need 가 있으면 원소에 그 키가 있는 배열만 (같은 이름의 다른 배열 건너뜀)."""
    dec, start = json.JSONDecoder(), 0
    while True:
        i = text.find(f'"{key}":[', start)
        if i < 0:
            return None
        try:
            arr = dec.raw_decode(text, i + len(key) + 3)[0]
        except ValueError:
            arr = None
        if arr is not None and (need is None or (arr and isinstance(arr[0], dict) and need in arr[0])):
            return arr
        start = i + 1


def parse_teams(html: str) -> dict:
    s = rsc_text(html)
    teams = _array_after(s, "teams", need="rankers")
    m = re.search(r'"totalRankers":(\d+)', s)
    if not teams or not m:
        raise RuntimeError("fc-info 팀컬러 순위를 찾지 못함")
    total = int(m.group(1))
    return {"total": total, "teams": [{"rank": t["rank"], "name": t["name"], "rankers": t["rankers"],
                                       "share": t["rankers"] / total if total else None} for t in teams]}


def parse_team(html: str) -> list[dict]:
    s = rsc_text(html)
    pos = _array_after(s, "positions", need="items")
    if pos is None:
        raise RuntimeError("fc-info 포지션별 카드를 찾지 못함")
    out = []
    for p in pos:
        for it in p.get("items") or []:
            out.append({"position": p["position"], "rank": it["rank"], "spid": it["spId"], "grade": it.get("grade"),
                        "grades": [[g["grade"], g["picks"]] for g in it.get("grades") or []],
                        "picks": it.get("picks"), "sample": it.get("sample"), "pick_rate": it.get("pickRate"),
                        "name": it.get("name"), "class_name": it.get("className"), "price": it.get("price")})
    return out


def parse_team_trends(html: str) -> list[dict]:
    """팀 페이지의 '인기 급등 선수'(rising) · '새로운 선수'(newCards). 없으면 빈 목록."""
    s = rsc_text(html)
    base = re.search(r'"baseDate":"([\d-]+)"', s)
    comp = re.search(r'"compareDate":"([\d-]+)"', s)
    out = []
    for key, kind in (("rising", "rising"), ("newCards", "new")):
        for it in _array_after(s, key, need="spId") or []:
            out.append({"kind": kind, "position": it.get("position"), "rank": it.get("rank"), "spid": it["spId"],
                        "grades": [[g["grade"], g["picks"]] for g in it.get("grades") or []],
                        "picks": it.get("picks"), "prev_picks": it.get("prevPicks"), "sample": it.get("sample"),
                        "prev_sample": it.get("prevSample"), "pick_rate": it.get("pickRate"),
                        "rate_change": it.get("rateChange"), "name": it.get("name"), "class_name": it.get("className"),
                        "base_date": base and base.group(1), "compare_date": comp and comp.group(1)})
    return out


def collect(con, top_teams: int, throttle: http.Throttle | None = None) -> int:
    throttle = throttle or http.Throttle(1.0)
    throttle.wait()
    info = parse_teams(http.request(BASE))
    cur = con.execute("INSERT INTO rp_snapshot (collected_at, total_rankers, status) VALUES (?, ?, 'running')",
                      (now_kst(), info["total"]))
    snap = cur.lastrowid
    con.executemany("INSERT INTO rp_team (snap_id, rank, name, rankers, share) VALUES (?,?,?,?,?)",
                    [(snap, t["rank"], t["name"], t["rankers"], t["share"]) for t in info["teams"]])
    con.commit()
    done = 0
    try:
        for t in [t for t in info["teams"] if t["rank"] <= top_teams]:
            throttle.wait()
            html = http.request(f"{BASE}/{urllib.parse.quote(t['name'])}")
            rows = parse_team(html)
            con.executemany(
                """INSERT OR REPLACE INTO rp_rising (snap_id, team, kind, position, rank, spid, grades, picks, prev_picks,
                       sample, prev_sample, pick_rate, rate_change, name, class_name, base_date, compare_date)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(snap, t["name"], r["kind"], r["position"], r["rank"], r["spid"], json.dumps(r["grades"]), r["picks"],
                  r["prev_picks"], r["sample"], r["prev_sample"], r["pick_rate"], r["rate_change"], r["name"],
                  r["class_name"], r["base_date"], r["compare_date"]) for r in parse_team_trends(html)])
            con.executemany(
                """INSERT OR REPLACE INTO rp_pick (snap_id, team, position, rank, spid, grade, grades, picks, sample,
                       pick_rate, name, class_name, price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(snap, t["name"], r["position"], r["rank"], r["spid"], r["grade"], json.dumps(r["grades"]), r["picks"],
                  r["sample"], r["pick_rate"], r["name"], r["class_name"], r["price"]) for r in rows])
            done += 1
            con.execute("UPDATE rp_snapshot SET teams_done=? WHERE id=?", (done, snap))
            con.commit()
        con.execute("UPDATE rp_snapshot SET status='complete' WHERE id=?", (snap,))
    except Exception:
        con.execute("UPDATE rp_snapshot SET status='failed' WHERE id=?", (snap,))
        con.commit()
        raise
    con.commit()
    return snap


def prune_snapshots(con, keep_days: int = 7) -> int:
    """갱신마다 쌓이는 fc-info 표 정리: 최근 keep_days 일은 전부, 그 전은 하루 마지막 것만 남긴다."""
    cutoff = (datetime.now(KST) - timedelta(days=keep_days)).isoformat(timespec="seconds")
    keep = {r[0] for r in con.execute(
        "SELECT MAX(id) FROM rp_snapshot WHERE status='complete' GROUP BY substr(collected_at, 1, 10)")}
    old = [r[0] for r in con.execute("SELECT id FROM rp_snapshot WHERE collected_at < ?", (cutoff,)) if r[0] not in keep]
    for sid in old:
        for t in ("rp_pick", "rp_rising", "rp_team"):
            con.execute(f"DELETE FROM {t} WHERE snap_id=?", (sid,))
        con.execute("DELETE FROM rp_snapshot WHERE id=?", (sid,))
    con.commit()
    return len(old)


def latest_snapshot(con):
    return con.execute("SELECT * FROM rp_snapshot WHERE status='complete' ORDER BY id DESC LIMIT 1").fetchone()


# --- 후보 ---------------------------------------------------------------------
def candidate_keys(con, snap_id: int, cfg: dict) -> dict:
    """(spid, grade) → 나온 곳 목록. 기준 1·2."""
    top_teams, top_rank, min_grade = int(cfg["topTeams"]), int(cfg["topRankPerPosition"]), int(cfg["minGrade"])
    min_picks = int(cfg.get("minGradePicks", 3))
    rows = con.execute(
        """SELECT p.*, t.rank AS team_rank, t.share AS team_share FROM rp_pick p
           JOIN rp_team t ON t.snap_id = p.snap_id AND t.name = p.team
           WHERE p.snap_id = ? AND t.rank <= ? AND p.rank <= ?""", (snap_id, top_teams, top_rank)).fetchall()
    out = {}
    for r in rows:
        # 랭커가 실제로 쓴 강화 중 minGrade 이상, minGradePicks 명 이상이 쓴 것 (fc-info 도 2명 이하는 '참고만')
        grades = [g for g, n in json.loads(r["grades"] or "[]") if g >= min_grade and n >= min_picks]
        for g in grades:
            out.setdefault((r["spid"], g), []).append({
                "team": r["team"], "team_rank": r["team_rank"], "position": r["position"], "rank": r["rank"],
                "pick_rate": r["pick_rate"], "name": r["name"], "class_name": r["class_name"],
                "grade_picks": dict((a, b) for a, b in json.loads(r["grades"] or "[]")).get(g, 0)})
    return out


def last_weekend(today: date) -> tuple[str, str]:
    """오늘보다 앞선 가장 최근 토·일."""
    d = today - timedelta(days=1)
    while d.weekday() != 6:  # 일요일
        d -= timedelta(days=1)
    return (d - timedelta(days=1)).isoformat(), d.isoformat()


def weekend_avg(con, spid: int, grade: int, sat: str, sun: str):
    r = con.execute("SELECT AVG(price), COUNT(*) FROM price_daily WHERE spid=? AND grade=? AND day IN (?, ?) AND price > 0",
                    (spid, grade, sat, sun)).fetchone()
    return (r[0], r[1]) if r and r[1] else (None, 0)


def score(rows: list[dict], weights: dict) -> None:
    """후보들 사이 최고값 대비 비율로 점수를 매긴다 (rows 를 직접 고침)."""
    if not rows:
        return
    mt = max(r["transfers_2h"] for r in rows)
    mp = max(r["best_pick_rate"] for r in rows)
    md = max(r["drop_pct"] for r in rows)
    for r in rows:
        pt = weights["transfers"] * (math.log1p(r["transfers_2h"]) / math.log1p(mt) if mt > 0 else 0)
        pp = weights["pickRate"] * (r["best_pick_rate"] / mp if mp > 0 else 0)
        pd = weights["weekendDrop"] * (r["drop_pct"] / md if md > 0 else 0)
        r.update(points_transfers=pt, points_pick=pp, points_drop=pd, rp_score=pt + pp + pd)
    rows.sort(key=lambda r: -r["rp_score"])


def context(con, settings: dict | None = None) -> dict:
    """카드 보기·평가에 공통으로 쓰는 값 (설정, 최근 랭커 픽, 지난 주말, 수수료·쿠폰)."""
    from .coupons import effective_fee
    s = settings or load_settings()
    cfg = s["rankerPick"]
    snap = latest_snapshot(con)
    lo, hi, preset = active_price_range(s)
    today = datetime.now(KST).date()
    sat, sun = last_weekend(today)
    f = s["fee"]
    best_c = con.execute("SELECT MAX(discount) FROM coupon WHERE qty > 0 AND (expires_at IS NULL OR expires_at = '' OR expires_at >= ?)",
                         (today.isoformat(),)).fetchone()[0]
    standing = (f["pcBangDiscount"] if f.get("premiumPcBang") else 0) + (f["topClassDiscount"] if f.get("topClass") else 0)
    team_rank = {r[0]: r[1] for r in con.execute("SELECT name, rank FROM rp_team WHERE snap_id=?", (snap["id"],))} if snap else {}
    return {"settings": s, "cfg": cfg, "snap": snap, "lo": lo, "hi": hi, "preset": preset, "sat": sat, "sun": sun,
            "fee": float(f["sellFeeRate"]), "coupon": best_c,
            "fee_coupon": effective_fee(float(f["baseRate"]), standing, best_c) if best_c else None, "team_rank": team_rank}


def appearances_for(con, ctx: dict, spid: int, grade: int) -> list[dict]:
    """이 시즌 카드가 fc-info 랭커 픽에 나온 곳 전부 (팀 순위 · 포지션 순위 · 픽률 · 이 강화 사용 수)."""
    if not ctx["snap"]:
        return []
    out = []
    for r in con.execute(
            """SELECT p.team, p.position, p.rank, p.pick_rate, p.grades, p.name, p.class_name, t.rank AS team_rank
               FROM rp_pick p JOIN rp_team t ON t.snap_id = p.snap_id AND t.name = p.team
               WHERE p.snap_id = ? AND p.spid = ? ORDER BY t.rank, p.rank""", (ctx["snap"]["id"], spid)):
        out.append({"team": r["team"], "team_rank": r["team_rank"], "position": r["position"], "rank": r["rank"],
                    "pick_rate": r["pick_rate"], "name": r["name"], "class_name": r["class_name"],
                    "grade_picks": dict((a, b) for a, b in json.loads(r["grades"] or "[]")).get(grade, 0)})
    return out


def card_view(con, ctx: dict, spid: int, grade: int) -> dict:
    """어떤 카드든 랭커 픽 창에 보여 줄 정보. 값이 없으면 None (거르지 않음)."""
    cfg, fee, fee_c = ctx["cfg"], ctx["fee"], ctx["fee_coupon"]
    ps = con.execute("SELECT status, current_price FROM price_status WHERE spid=? AND grade=?", (spid, grade)).fetchone()
    mk = fionhub.latest(con, spid, grade)
    current = (mk and mk["price"]) or (ps and ps["status"] == "ok" and ps["current_price"]) or None
    we, n_we = weekend_avg(con, spid, grade, ctx["sat"], ctx["sun"])
    apps = appearances_for(con, ctx, spid, grade)
    k = con.execute("SELECT player_name, class_name, ovr, main_position FROM card WHERE spid=? AND grade=?", (spid, grade)).fetchone()
    if not k:
        p = con.execute("SELECT name FROM meta_player WHERE spid=?", (spid,)).fetchone()
        c = con.execute("SELECT name FROM meta_class WHERE class_id=?", (spid // 1_000_000,)).fetchone()
        k = {"player_name": p and p[0], "class_name": c and c[0], "ovr": None, "main_position": None}
    row = {"spid": spid, "grade": grade, "player_name": k["player_name"] or (apps[0]["name"] if apps else None),
           "class_name": k["class_name"] or (apps[0]["class_name"] if apps else None), "ovr": k["ovr"],
           "main_position": k["main_position"], "current_price": current,
           "price_source": "fionhub" if mk and mk["price"] else "데이터센터",
           "weekend_avg": we, "weekend_days": n_we,
           "drop_pct": (we - current) / we * 100 if we and current else None,
           "transfers_2h": mk and mk["transfers_2h"], "lower": mk and mk["lower_limit"], "upper": mk and mk["upper_limit"],
           "buy_at_upper": mk and mk["buy_at_upper"], "market_at": mk and mk["fetched_at"],
           "best_pick_rate": max((a["pick_rate"] or 0 for a in apps), default=0), "appearances": apps,
           "net_if_weekend": we * (1 - fee) - current if we and current else None,
           "team_colors": fionhub.card_team_colors(con, spid, grade)}
    row["roi_if_weekend_pct"] = row["net_if_weekend"] / current * 100 if row["net_if_weekend"] is not None else None
    row["net_if_weekend_coupon"] = we * (1 - fee_c) - current if fee_c is not None and we and current else None
    picks_by_team = {}
    for a in apps:
        picks_by_team.setdefault(a["team"], []).append({"position": a["position"], "rank": a["rank"], "pick_rate": a["pick_rate"]})
    for t in row["team_colors"] or []:
        if t["label"] in ctx["team_rank"]:
            t["popular_rank"] = ctx["team_rank"][t["label"]]
            # 이 팀컬러를 쓰는 랭커들 사이에서 이 카드(시즌)의 포지션별 순위 · 픽률 (fc-info 15위까지 저장)
            if t["popular_rank"] <= int(cfg["topTeams"]):  # 선수 표는 topTeams 위 팀만 받음
                t["player_picks"] = picks_by_team.get(t["label"], [])
    return row


def evaluate(con, settings: dict | None = None) -> dict:
    """기준 1~4 를 적용해 점수 표를 만든다. 탈락 이유별 개수도 돌려준다."""
    ctx = context(con, settings)
    cfg, snap = ctx["cfg"], ctx["snap"]
    if not snap:
        return {"snapshot": None, "rows": [], "near": [], "excluded": {}, "weekend": None, "config": cfg}
    lo, hi = ctx["lo"], ctx["hi"]
    keys = candidate_keys(con, snap["id"], cfg)
    min_tr = int(cfg["minTransfers2h"])
    excluded = {"no_price": 0, "out_of_range": 0, "no_weekend": 0, "not_cheaper": 0, "no_market": 0, "low_volume": 0}
    rows, near = [], []
    for (spid, grade), apps in keys.items():
        row = card_view(con, ctx, spid, grade)
        cur = row["current_price"]
        if not cur:
            excluded["no_price"] += 1
        elif not (lo <= cur <= hi):
            excluded["out_of_range"] += 1
        elif not row["weekend_avg"]:
            excluded["no_weekend"] += 1
        elif row["drop_pct"] <= 0:
            excluded["not_cheaper"] += 1
        elif row["transfers_2h"] is None:
            excluded["no_market"] += 1
        else:
            # 점수용 픽률은 후보 조건(팀 topTeams·포지션 topRank)에 든 곳 기준
            row["best_pick_rate"] = max(a["pick_rate"] or 0 for a in apps)
            if row["transfers_2h"] < min_tr:
                excluded["low_volume"] += 1
                near.append(row)
            else:
                rows.append(row)
    score(rows, cfg["weights"])
    return {"snapshot": dict(snap), "weekend": [ctx["sat"], ctx["sun"]], "range": [lo, hi], "preset": ctx["preset"],
            "fee": ctx["fee"], "coupon": ctx["coupon"], "fee_coupon": ctx["fee_coupon"],
            "rows": rows, "near": sorted(near, key=lambda r: -r["transfers_2h"])[:20], "excluded": excluded,
            "candidates": len(keys), "config": cfg}


# --- 인기 급등 선수 -------------------------------------------------------------
def rising_keys(con, snap_id: int, cfg: dict) -> dict:
    """인기 topTeams 위 팀의 '인기 급등' · '새로운 선수' 카드 → (spid, 강화) 별 나온 곳. 강화는 minGrade 이상 쓰인 것."""
    min_grade = int(cfg["minGrade"])
    out = {}
    for r in con.execute(
            """SELECT x.*, t.rank AS team_rank FROM rp_rising x JOIN rp_team t ON t.snap_id = x.snap_id AND t.name = x.team
               WHERE x.snap_id = ? AND t.rank <= ?""", (snap_id, int(cfg["topTeams"]))):
        for g, n in json.loads(r["grades"] or "[]"):
            if g >= min_grade:
                out.setdefault((r["spid"], g), []).append({
                    "team": r["team"], "team_rank": r["team_rank"], "kind": r["kind"], "position": r["position"],
                    "rank": r["rank"], "picks": r["picks"], "prev_picks": r["prev_picks"], "grade_picks": n,
                    "pick_rate": r["pick_rate"], "rate_change": r["rate_change"],
                    "base_date": r["base_date"], "compare_date": r["compare_date"]})
    return out


def price_change(con, spid: int, grade: int, days: int = 7):
    """최근 일별 가격 대비 days 일 전 일별 가격 변화율 (%)."""
    rows = con.execute("SELECT day, price FROM price_daily WHERE spid=? AND grade=? AND price > 0 ORDER BY day DESC LIMIT ?",
                       (spid, grade, days + 1)).fetchall()
    if len(rows) < days + 1:
        return None
    return (rows[0][1] - rows[-1][1]) / rows[-1][1] * 100


def rising_view(con, settings: dict | None = None) -> dict:
    """인기 급등 선수 분석: 랭커 사용이 늘어난 카드가 가격에는 얼마나 반영됐는지.

    인기 변화 = 같은 팀컬러 랭커 중 이 카드(시즌)를 쓴 수 이전 → 지금, 픽률 증가율
    가격 반영 = 7일 일별 가격 변화 · 지난 주말 대비 · 2시간 거래
    판단 (참고용):
      '인기↑ 가격 아직'  : 7일 가격 변화 ≤ +3%   (인기만 먼저 오른 상태)
      '이미 가격 반영'   : 7일 가격 변화 > +10%
      그 사이            : '가격 오르는 중'
    """
    ctx = context(con, settings)
    cfg, snap = ctx["cfg"], ctx["snap"]
    if not snap:
        return {"rows": [], "out_of_range": 0}
    keys = rising_keys(con, snap["id"], cfg)
    rows, out_of_range = [], 0
    for (spid, grade), apps in keys.items():
        v = card_view(con, ctx, spid, grade)
        cur = v["current_price"]
        if not cur or not (ctx["lo"] <= cur <= ctx["hi"]):
            out_of_range += 1
            continue
        best = max(apps, key=lambda a: (a["rate_change"] if a["rate_change"] is not None else 1e9))
        v["rising"] = sorted(apps, key=lambda a: a["team_rank"])
        v["is_new"] = any(a["kind"] == "new" for a in apps)
        v["max_rate_change"] = max((a["rate_change"] for a in apps if a["rate_change"] is not None), default=None)
        v["picks_now"] = best["picks"]
        v["picks_prev"] = best["prev_picks"]
        v["rising_teams"] = len({a["team"] for a in apps})
        # 2시간 거래 추적: fionhub 에서 받을 때마다 쌓인 기록 (최근 → 과거)
        hist = [h["transfers_2h"] for h in fionhub.history(con, spid, grade, 12) if h["transfers_2h"] is not None]
        v["transfers_hist"] = hist
        v["transfers_prev"] = hist[1] if len(hist) > 1 else None
        v["transfers_avg"] = sum(hist) / len(hist) if hist else None
        v["chg7_pct"] = price_change(con, spid, grade, 7)
        c7 = v["chg7_pct"]
        v["signal"] = None if c7 is None else ("인기↑ 가격 아직" if c7 <= 3 else ("이미 가격 반영" if c7 > 10 else "가격 오르는 중"))
        v["compare"] = [best["compare_date"], best["base_date"]]
        rows.append(v)
    rows.sort(key=lambda r: (-(r["max_rate_change"] if r["max_rate_change"] is not None else 99), -(r["transfers_2h"] or 0)))
    return {"rows": rows, "out_of_range": out_of_range, "fee": ctx["fee"], "coupon": ctx["coupon"],
            "range": [ctx["lo"], ctx["hi"]], "preset": ctx["preset"], "config": cfg,
            "compare": rows[0]["compare"] if rows else None}


def log_recommendations(con, ev: dict) -> int:
    """지금 랭커 픽 추천 카드를 오늘 날짜로 기록. 같은 날 이미 기록된 카드는 처음 값을 유지 (INSERT OR IGNORE)."""
    day = datetime.now(KST).date().isoformat()
    ts = now_kst()
    before = con.execute("SELECT COUNT(*) FROM rp_rec_log WHERE day=?", (day,)).fetchone()[0]
    con.executemany(
        """INSERT OR IGNORE INTO rp_rec_log (day, spid, grade, rank, rp_score, current_price, weekend_avg, drop_pct,
               transfers_2h, best_pick_rate, lower_limit, upper_limit, fee_rate, fee_coupon, logged_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(day, r["spid"], r["grade"], i, r["rp_score"], r["current_price"], r["weekend_avg"], r["drop_pct"],
          r["transfers_2h"], r["best_pick_rate"], r["lower"], r["upper"], ev["fee"], ev.get("fee_coupon"), ts)
         for i, r in enumerate(ev["rows"], 1)])
    con.commit()
    return con.execute("SELECT COUNT(*) FROM rp_rec_log WHERE day=?", (day,)).fetchone()[0] - before


# --- 가격 받기 -----------------------------------------------------------------
def refresh_prices(con, settings: dict, progress: bool = True, datacenter: bool = True, limit: int | None = None) -> dict:
    """후보 카드의 데이터센터 가격 (priceMaxAgeHours 보다 오래된 것만, datacenter=True 일 때)
    → 가격대 안 · 주말보다 싼 카드만 fionhub 시세 (2시간 거래 · 상한가 · 팀컬러)."""
    cfg = settings["rankerPick"]
    snap = latest_snapshot(con)
    if not snap:
        return {"prices": 0, "market": 0, "market_failed": 0}
    cand = candidate_keys(con, snap["id"], cfg)
    # 픽률 높은 카드부터 (fionhub 조회 수 상한에 걸려도 인기 카드가 먼저 들어가게)
    keys = sorted(cand, key=lambda k: -max(a["pick_rate"] or 0 for a in cand[k]))
    rising = [k for k in rising_keys(con, snap["id"], cfg) if k not in cand]  # 인기 급등 · 새로운 선수 카드
    keys += rising
    stale = []
    if datacenter:
        cutoff = (datetime.now(KST) - timedelta(hours=float(cfg["priceMaxAgeHours"]))).isoformat(timespec="seconds")
        fresh = {(r[0], r[1]) for r in con.execute(
            "SELECT spid, grade FROM price_status WHERE status IN ('ok','no_data') AND fetched_at >= ?", (cutoff,))}
        stale = [k for k in keys if k not in fresh][:limit]
        for spid, grade in stale:
            ensure_card(con, spid, grade)
        con.commit()
        if stale:
            if progress:
                print(f"[rankerpick] 데이터센터 가격 {len(stale)}장", flush=True)
            fetch_cards(con, stale, http.Throttle(float(settings["collection"]["datacenterMinIntervalSec"])),
                        progress, ability=False)  # OVR 은 건너뜀 (요청 수 절반)
    # fionhub 는 가격대 안 + 주말보다 싼 카드만 (요청 수 줄이기)
    lo, hi, _ = active_price_range(settings)
    sat, sun = last_weekend(datetime.now(KST).date())
    shortlist = []
    for spid, grade in keys:
        ps = con.execute("SELECT current_price FROM price_status WHERE spid=? AND grade=? AND status='ok'", (spid, grade)).fetchone()
        if not ps or not ps[0] or not (lo * 0.9 <= ps[0] <= hi * 1.1):
            continue
        we, _n = weekend_avg(con, spid, grade, sat, sun)
        if we and ps[0] < we * 1.05:  # 데이터센터 값이 조금 늦을 수 있어 5% 여유
            shortlist.append((spid, grade))
    shortlist = [k for k in shortlist if k not in set(rising)][: int(cfg["maxMarketFetch"])]
    # 인기 급등 카드는 주말보다 싼지와 상관없이 가격대 안이면 거래량을 받는다 (가격 반영 여부를 보려고)
    for spid, grade in rising_keys(con, snap["id"], cfg):
        ps = con.execute("SELECT current_price FROM price_status WHERE spid=? AND grade=? AND status='ok'", (spid, grade)).fetchone()
        if ps and ps[0] and lo * 0.9 <= ps[0] <= hi * 1.1 and (spid, grade) not in shortlist:
            shortlist.append((spid, grade))
            if len(shortlist) >= int(cfg["maxMarketFetch"]) + int(cfg.get("maxRisingMarketFetch", 60)):
                break
    if progress:
        print(f"[rankerpick] fionhub 시세 {len(shortlist)}장", flush=True)
    res = fionhub.collect(con, shortlist, http.Throttle(1.0), progress)
    return {"prices": len(stale), "market": res["ok"], "market_failed": len(res["failed"])}


def run(con, settings: dict, mode: str = "daily", progress: bool = True, limit: int | None = None) -> str:
    """daily: fc-info 표(오래됐으면) + 후보 데이터센터 가격 + fionhub.  quick: fionhub 만 (2시간 거래가 자주 바뀜)."""
    cfg = settings["rankerPick"]
    snap = latest_snapshot(con)
    age_h = None
    if snap:
        age_h = (datetime.now(KST) - datetime.fromisoformat(snap["collected_at"])).total_seconds() / 3600
    # fc-info 랭커 픽 (인기 팀컬러 topTeams 위 × 포지션별 선수 · 인기 급등 · 새로운 선수) 은 갱신할 때마다 받는다
    if snap is None or age_h >= float(cfg["collectEveryHours"]):
        collect(con, int(cfg["topTeams"]))
        prune_snapshots(con)
    r = refresh_prices(con, settings, progress, datacenter=(mode == "daily"), limit=limit)
    ev = evaluate(con, settings)
    n_log = log_recommendations(con, ev)  # 검증용 기록 (오늘 처음 추천된 카드만 새로 쌓임)
    msg = f"랭커 픽 후보 {ev['candidates']}장 → 추천 {len(ev['rows'])}장 (가격 {r['prices']} / fionhub {r['market']}, 새 기록 {n_log})"
    log(con, "rankerpick", True, msg)
    return msg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "prices", "quick", "show"])
    ap.add_argument("--limit", type=int, help="데이터센터 가격을 받을 최대 카드 수 (시험용)")
    args = ap.parse_args(argv)
    s = load_settings()
    con = connect()
    if args.cmd == "collect":
        print("snapshot", collect(con, int(s["rankerPick"]["topTeams"])))
    elif args.cmd == "prices":
        print(run(con, s, "daily", limit=args.limit))
    elif args.cmd == "quick":
        print(run(con, s, "quick"))
    ev = evaluate(con, s)
    print(f"후보 {ev.get('candidates')}장, 추천 {len(ev['rows'])}장, 제외 {ev['excluded']}")
    for r in ev["rows"][:20]:
        print(f"{r['rp_score']:5.1f}  {r['player_name']} {r['class_name']} {r['grade']}강  {r['current_price']/1e8:.2f}억 "
              f"주말 {r['weekend_avg']/1e8:.2f}억 (−{r['drop_pct']:.1f}%)  2h {r['transfers_2h']}건  픽률 {r['best_pick_rate']*100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
