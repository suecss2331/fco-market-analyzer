"""2단계: 공식경기 랭커 스쿼드 수집 + 선수 카드 매칭.

흐름
  1) Open API 메타데이터(spid / class / spposition) 갱신
  2) 데이터센터 공식경기 랭킹 1~N위 스냅샷 저장
  3) 랭커별: 구단주명 -> ouid -> 최근 공식경기 1건 -> 경기 상세에서 본인 선수 목록
  4) (spid, 강화) 를 키로 카드 마스터에 등록. 선수명 매칭은 쓰지 않는다.

순위 오름차순으로 처리하므로 일일 한도에 걸려도 1~20위(인기 스쿼드 점수 대상)가 먼저 채워진다.
한도에 걸리면 스냅샷을 'partial' 로 남기고, 다음 실행 시 --resume 으로 이어서 수집한다.

실행:  python -m fco.collect_squads [--resume] [--top N]
"""
import argparse
import sys

from . import datacenter, http
from .config import load_api_key, load_settings
from .db import connect, log, now_kst
from .openapi import OpenApi, BudgetExceeded, SUB_POSITION, static_meta


def refresh_meta(con) -> None:
    ts = now_kst()
    players = static_meta("spid")
    con.executemany(
        "INSERT INTO meta_player (spid, name, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(spid) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
        [(p["id"], p["name"], ts) for p in players],
    )
    classes = static_meta("class")
    con.executemany(
        "INSERT INTO meta_class (class_id, name, img, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(class_id) DO UPDATE SET name = excluded.name, img = excluded.img, "
        "updated_at = excluded.updated_at",
        [(c["class"], c["className"], c.get("classImg"), ts) for c in classes],
    )
    positions = static_meta("spposition")
    con.executemany(
        "INSERT OR REPLACE INTO meta_position (sp_position, code) VALUES (?, ?)",
        [(p["spposition"], p["desc"]) for p in positions],
    )
    con.commit()
    print(f"[meta] 선수 {len(players):,} / 클래스 {len(classes)} / 포지션 {len(positions)}")


def new_snapshot(con, top_n: int, dc_throttle) -> int:
    ranking = datacenter.fetch_official_ranking(top_n, dc_throttle)
    cur = con.execute(
        "INSERT INTO ranking_snapshot (mode, top_n, collected_at, status) VALUES ('1vs1', ?, ?, 'ranking_ok')",
        (top_n, now_kst()),
    )
    snap_id = cur.lastrowid
    con.executemany(
        "INSERT INTO ranking_entry (snap_id, rank, nickname, data_sn, rank_score, club_value) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(snap_id, r["rank"], r["nickname"], r["data_sn"], r["rank_score"], r["club_value"]) for r in ranking],
    )
    con.commit()
    print(f"[ranking] 스냅샷 #{snap_id}: {len(ranking)}명 (1위 {ranking[0]['nickname']})")
    return snap_id


def get_ouid(con, api: OpenApi, nickname: str, force: bool = False) -> str:
    if not force:
        row = con.execute("SELECT ouid FROM ranker_ouid WHERE nickname = ?", (nickname,)).fetchone()
        if row:
            return row["ouid"]
    ouid = api.ouid(nickname)
    con.execute(
        "INSERT INTO ranker_ouid (nickname, ouid, checked_at) VALUES (?, ?, ?) "
        "ON CONFLICT(nickname) DO UPDATE SET ouid = excluded.ouid, checked_at = excluded.checked_at",
        (nickname, ouid, now_kst()),
    )
    con.commit()
    return ouid


def fetch_squad(con, api: OpenApi, nickname: str):
    """(match_id, match_date, players) 또는 공식경기 기록이 없으면 None."""
    ouid = get_ouid(con, api, nickname)
    try:
        ids = api.match_ids(ouid)
    except http.HttpError as e:
        if e.status in (400, 404):  # 캐시된 ouid 가 바뀌었을 수 있음 -> 한 번 다시 조회
            ouid = get_ouid(con, api, nickname, force=True)
            ids = api.match_ids(ouid)
        else:
            raise
    if not ids:
        return None
    detail = api.match_detail(ids[0])
    mine = next((m for m in detail.get("matchInfo", []) if m.get("ouid") == ouid), None)
    if mine is None:
        raise RuntimeError("경기 상세에 본인 ouid 가 없음")
    return ids[0], detail.get("matchDate"), mine.get("player", [])


def upsert_cards(con, snap_id: int) -> int:
    ts = now_kst()
    rows = con.execute(
        "SELECT DISTINCT s.spid, s.grade, p.name AS pname, c.name AS cname "
        "FROM squad_entry s "
        "LEFT JOIN meta_player p ON p.spid = s.spid "
        "LEFT JOIN meta_class c ON c.class_id = s.spid / 1000000 "
        # 강화 0 은 교체 명단 LIVE 카드에서만 관측되는 값(거래 단계 아님) -> 카드 마스터에서 제외
        "WHERE s.snap_id = ? AND s.grade >= 1",
        (snap_id,),
    ).fetchall()
    con.execute("DELETE FROM card WHERE grade < 1")
    con.executemany(
        "INSERT INTO card (spid, grade, player_id, class_id, player_name, class_name, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(spid, grade) DO UPDATE SET player_name = excluded.player_name, "
        "class_name = excluded.class_name, last_seen = excluded.last_seen",
        [(r["spid"], r["grade"], r["spid"] % 1_000_000, r["spid"] // 1_000_000,
          r["pname"], r["cname"], ts, ts) for r in rows],
    )
    con.commit()
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", action="store_true", help="마지막 partial 스냅샷 이어서 수집")
    ap.add_argument("--top", type=int, help="랭커 범위 (기본: settings.collection.universeTopN)")
    ap.add_argument("--skip-meta", action="store_true")
    args = ap.parse_args(argv)

    settings = load_settings()
    col = settings["collection"]
    top_n = args.top or int(col["universeTopN"])
    include_subs = bool(col.get("includeSubs", True))

    con = connect()
    api = OpenApi(con, load_api_key(), float(col["openApiMaxPerSecond"]), int(col["openApiDailyCap"]))
    dc_throttle = http.Throttle(float(col["datacenterMinIntervalSec"]))

    try:
        if not args.skip_meta:
            refresh_meta(con)

        snap_id = None
        if args.resume:
            row = con.execute(
                "SELECT snap_id FROM ranking_snapshot WHERE status IN ('ranking_ok','partial') "
                "ORDER BY snap_id DESC LIMIT 1").fetchone()
            snap_id = row["snap_id"] if row else None
            if snap_id:
                print(f"[resume] 스냅샷 #{snap_id} 이어서 수집")
        if snap_id is None:
            snap_id = new_snapshot(con, top_n, dc_throttle)
    except Exception as e:  # 수집 실패 시 기존 데이터는 그대로 둔다
        log(con, "collect_squads", False, f"랭킹/메타 수집 실패: {e}")
        print(f"[실패] {e}", file=sys.stderr)
        return 1

    done = {r["rank"] for r in con.execute(
        "SELECT rank FROM squad_fetch WHERE snap_id = ? AND status IN ('ok','no_match')", (snap_id,))}
    rankers = con.execute(
        "SELECT rank, nickname FROM ranking_entry WHERE snap_id = ? ORDER BY rank", (snap_id,)).fetchall()

    counts = {"ok": 0, "no_match": 0, "error": 0}
    budget_hit = False
    for r in rankers:
        rank, nick = r["rank"], r["nickname"]
        if rank in done:
            continue
        status, msg = "ok", None
        try:
            res = fetch_squad(con, api, nick)
            if res is None:
                status = "no_match"
            else:
                match_id, match_date, players = res
                con.execute("DELETE FROM squad_entry WHERE snap_id = ? AND rank = ?", (snap_id, rank))
                for p in players:
                    is_sub = 1 if p["spPosition"] == SUB_POSITION else 0
                    if is_sub and not include_subs:
                        continue
                    con.execute(
                        "INSERT OR REPLACE INTO squad_entry (snap_id, rank, nickname, match_id, match_date, "
                        "spid, grade, sp_position, is_sub) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (snap_id, rank, nick, match_id, match_date, p["spId"], p["spGrade"], p["spPosition"], is_sub),
                    )
        except BudgetExceeded as e:
            budget_hit = True
            print(f"[중단] {e} — {rank}위부터 남음. 내일 --resume 으로 이어서 수집하세요.")
            break
        except Exception as e:
            status, msg = "error", str(e)
        con.execute(
            "INSERT OR REPLACE INTO squad_fetch (snap_id, rank, nickname, status, message, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (snap_id, rank, nick, status, msg, now_kst()))
        con.commit()
        counts[status] += 1
        if rank % 20 == 0:
            print(f"  ..{rank}위까지 처리 (오늘 API {api.used_today()}건)")

    remaining = con.execute(
        "SELECT COUNT(*) FROM ranking_entry e WHERE e.snap_id = ? AND NOT EXISTS ("
        "SELECT 1 FROM squad_fetch f WHERE f.snap_id = e.snap_id AND f.rank = e.rank "
        "AND f.status IN ('ok','no_match'))", (snap_id,)).fetchone()[0]
    status = "complete" if remaining == 0 else "partial"
    con.execute("UPDATE ranking_snapshot SET status = ? WHERE snap_id = ?", (status, snap_id))
    con.commit()

    n_cards = upsert_cards(con, snap_id)
    summary = (f"스냅샷 #{snap_id} {status}: 성공 {counts['ok']}, 공식경기 없음 {counts['no_match']}, "
               f"오류 {counts['error']}, 미수집 {remaining}명 / 카드 {n_cards}종 / 오늘 API {api.used_today()}건")
    log(con, "collect_squads", counts["error"] == 0 and not budget_hit, summary)
    print("[완료] " + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
