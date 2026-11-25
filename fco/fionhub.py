"""fionhub.com 이적시장 시세: 상한가 · 하한가 · 호가 · 최근 2시간 거래 건수.

넥슨 데이터센터에는 상한가·하한가가 없어서 fionhub 공개 페이지 데이터(__data.json)를 읽는다.
  - robots.txt 가 모든 수집을 허용 (2026-11-24 확인). 차단·확인 페이지가 나오면 우회하지 않고 그 카드를 건너뛴다.
  - 1초에 1번 이하로만 요청한다 (Throttle).
  - fionhub 카드 ID 는 선수 이름 검색으로 찾고 fionhub_card 에 저장해 다시 찾지 않는다.

  python -m fco.fionhub            보유 매물 시세 갱신
  python -m fco.fionhub --watch    보유 매물 + 추천 상위 카드 (market.topN)
"""
import argparse
import json
import sys
import urllib.parse

from . import http
from .db import connect, now_kst

BASE = "https://fionhub.com"
HEADERS = {"Accept": "application/json"}


class NotFound(LookupError):
    pass


# --- SvelteKit devalue 형식 풀기 --------------------------------------------
_SPECIAL = {-1: None, -2: None, -3: float("nan"), -4: float("inf"), -5: float("-inf"), -6: 0}


def unflatten(values: list):
    """devalue 로 납작하게 만든 배열을 원래 값으로 되돌린다 (Date 는 ISO 문자열로)."""
    memo = {}

    def h(i):
        if isinstance(i, int) and i < 0:
            return _SPECIAL.get(i)
        if i in memo:
            return memo[i]
        v = values[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str):
                tag = v[0]
                if tag == "Date":
                    out = v[1]
                elif tag in ("Set",):
                    out = [h(x) for x in v[1:]]
                elif tag == "Map":
                    out = {str(h(v[k])): h(v[k + 1]) for k in range(1, len(v), 2)}
                elif tag == "BigInt":
                    out = int(v[1])
                else:
                    out = None
                memo[i] = out
                return out
            out = []
            memo[i] = out
            out.extend(h(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            memo[i] = out
            for k, x in v.items():
                out[k] = h(x)
            return out
        memo[i] = v
        return v

    return h(0)


def _num(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_market(body: str) -> dict:
    """선수 페이지 __data.json 에서 marketPrice (+ 있으면 카드 팀컬러) 를 꺼낸다."""
    try:
        doc = json.loads(body)
    except ValueError:
        raise RuntimeError("fionhub 응답이 JSON 이 아님 (차단·확인 페이지 가능성) → 건너뜀") from None
    team_colors = None
    for node in doc.get("nodes") or []:
        if not node or node.get("type") != "data":
            continue
        root = unflatten(node["data"])
        if isinstance(root, dict) and isinstance(root.get("teamColors"), list):
            team_colors = [{"id": t.get("id"), "label": t.get("label"), "category": t.get("category"),
                            "minGrade": t.get("minGrade"), "maxGrade": t.get("maxGrade")}
                           for t in root["teamColors"] if isinstance(t, dict)]
        if isinstance(root, dict) and "marketPrice" in root:
            m = root["marketPrice"] or {}
            lv = lambda xs: [[_num(x.get("price")), _num(x.get("count"))] for x in (xs or []) if isinstance(x, dict)]
            return {
                "price": _num(m.get("price")), "lower_limit": _num(m.get("lowerLimit")),
                "upper_limit": _num(m.get("upperLimit")), "available": 1 if m.get("available") else 0,
                "transfers_2h": _num(m.get("completedTransfers")) if not isinstance(m.get("completedTransfers"), list)
                else len(m.get("completedTransfers")),
                "buy_levels": lv(m.get("buyLevels")), "sell_levels": lv(m.get("sellLevels")),
                "observed_at": m.get("observedAt"), "team_colors": team_colors,
            }
    errors = [n.get("error", {}).get("message") for n in doc.get("nodes") or [] if n and n.get("type") == "error"]
    raise NotFound("fionhub 에 시세 없음" + (f" ({errors[0]})" if errors else ""))


# --- 카드 ID 찾기 ------------------------------------------------------------
def _get_json(url: str, throttle: http.Throttle):
    throttle.wait()
    body = http.request(url, headers=HEADERS)
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError("fionhub 응답이 JSON 이 아님 (차단·확인 페이지 가능성) → 건너뜀") from None


def resolve_uuid(con, spid: int, throttle: http.Throttle) -> str:
    row = con.execute("SELECT uuid FROM fionhub_card WHERE spid=?", (spid,)).fetchone()
    if row:
        if not row[0]:
            raise NotFound("fionhub 에서 카드를 찾지 못함 (이전 검색)")
        return row[0]
    name = con.execute("SELECT name FROM meta_player WHERE spid=?", (spid,)).fetchone()
    name = name[0] if name else (con.execute("SELECT player_name FROM card WHERE spid=? LIMIT 1", (spid,)).fetchone() or [None])[0]
    uuid = ""
    if name:
        q = urllib.parse.quote(name)
        hits = _get_json(f"{BASE}/player/quick-search?q={q}&region=ko&exclude_unlicensed=0", throttle) or []
        season = spid // 1_000_000
        same = [h for h in hits if h.get("seasonId") == season]
        if len(same) == 1:
            uuid = same[0]["uuid"]
        elif hits:  # 같은 시즌 이름이 여럿이거나 검색 결과에 없으면 같은 선수 전 시즌 목록에서 spid 로 찾는다
            for h in (same or hits)[:3]:
                lst = _get_json(f"{BASE}/player/{h['uuid']}/same-pid?region=ko&exclude_unlicensed=0", throttle) or []
                hit = [x for x in lst if x.get("spid") == spid]
                if hit:
                    uuid = hit[0]["uuid"]
                    break
    con.execute("INSERT OR REPLACE INTO fionhub_card (spid, uuid, updated_at) VALUES (?, ?, ?)", (spid, uuid, now_kst()))
    con.commit()
    if not uuid:
        raise NotFound("fionhub 에서 카드를 찾지 못함")
    return uuid


def fetch_market(uuid: str, grade: int, throttle: http.Throttle) -> dict:
    throttle.wait()
    body = http.request(f"{BASE}/player/{uuid}/{grade}/x/__data.json", headers=HEADERS)
    return parse_market(body)


def save(con, spid: int, grade: int, m: dict) -> None:
    con.execute(
        """INSERT OR REPLACE INTO market_snapshot (spid, grade, observed_at, fetched_at, price, lower_limit, upper_limit,
               available, transfers_2h, buy_levels, sell_levels) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (spid, grade, m["observed_at"] or now_kst(), now_kst(), m["price"], m["lower_limit"], m["upper_limit"],
         m["available"], m["transfers_2h"], json.dumps(m["buy_levels"]), json.dumps(m["sell_levels"])))
    if m.get("team_colors") is not None:
        con.execute("UPDATE fionhub_card SET team_colors = ? WHERE spid = ?",
                    (json.dumps(m["team_colors"], ensure_ascii=False), spid))


TC_CATEGORY = {"affiliation": "소속·국가", "enhance": "강화", "trait": "특성", "feature": "특성"}


def card_team_colors(con, spid: int, grade: int) -> list[dict] | None:
    """이 카드(이 강화)가 받는 팀컬러. 강화 팀컬러는 강화 단계 조건에 맞는 것만."""
    r = con.execute("SELECT team_colors FROM fionhub_card WHERE spid=?", (spid,)).fetchone()
    if not r or not r[0]:
        return None
    out, enhance = [], None
    for t in json.loads(r[0]):
        lo, hi = t.get("minGrade") or 0, t.get("maxGrade")
        if grade < lo or (hi is not None and grade > hi):
            continue
        if t.get("category") == "enhance":  # 강화 팀컬러는 조건을 만족하는 가장 높은 단계 하나만 표시
            if enhance is None or lo > (enhance.get("minGrade") or 0):
                enhance = t
            continue
        out.append({"label": t["label"], "category": TC_CATEGORY.get(t.get("category"), t.get("category") or "기타")})
    if enhance:
        out.append({"label": enhance["label"], "category": "강화"})
    return out


def collect(con, keys, throttle: http.Throttle | None = None, progress: bool = True) -> dict:
    throttle = throttle or http.Throttle(1.0)
    ok, fail = 0, []
    for i, (spid, grade) in enumerate(keys, 1):
        try:
            m = fetch_market(resolve_uuid(con, spid, throttle), grade, throttle)
            save(con, spid, grade, m)
            con.commit()
            ok += 1
        except Exception as e:  # 한 카드 실패가 전체를 멈추지 않게
            fail.append((spid, grade, str(e)[:200]))
        if progress:
            print(f"[fionhub] {i}/{len(keys)}", flush=True)
    return {"ok": ok, "failed": fail}


# --- 조회 --------------------------------------------------------------------
def latest(con, spid: int, grade: int) -> dict | None:
    r = con.execute("SELECT * FROM market_snapshot WHERE spid=? AND grade=? ORDER BY fetched_at DESC LIMIT 1",
                    (spid, grade)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["buy_levels"] = json.loads(d["buy_levels"] or "[]")
    d["sell_levels"] = json.loads(d["sell_levels"] or "[]")
    d["buy_at_upper"] = sum(c or 0 for p, c in d["buy_levels"] if p == d["upper_limit"])
    d["sell_at_lower"] = sum(c or 0 for p, c in d["sell_levels"] if p == d["lower_limit"])
    return d


def history(con, spid: int, grade: int, limit: int = 50) -> list[dict]:
    return [dict(r) for r in con.execute(
        """SELECT observed_at, fetched_at, price, lower_limit, upper_limit, transfers_2h FROM market_snapshot
           WHERE spid=? AND grade=? ORDER BY fetched_at DESC LIMIT ?""", (spid, grade, limit))]


def watch_keys(con, settings) -> list[tuple[int, int]]:
    from .refresh import watchlist
    held = [(r[0], r[1]) for r in con.execute("SELECT DISTINCT spid, grade FROM holding")]
    n = int(settings.get("market", {}).get("topN", 30))
    return list(dict.fromkeys(held + watchlist(con, settings)[:len(held) + n]))


def main(argv=None) -> int:
    from .config import load_settings
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args(argv)
    con = connect()
    keys = watch_keys(con, load_settings()) if args.watch else [(r[0], r[1]) for r in con.execute("SELECT DISTINCT spid, grade FROM holding")]
    res = collect(con, keys)
    print(f"[fionhub] 성공 {res['ok']} / 실패 {len(res['failed'])}")
    for f in res["failed"]:
        print("  ", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
