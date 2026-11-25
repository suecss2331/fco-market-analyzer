"""3단계: 카드별 가격 기록 저장.

카드마다 데이터센터에서
  - 가격 그래프: 현재가 + 최근 365일 일별 가격  (매 실행)
  - 능력치: 강화 반영 OVR, 주 포지션            (카드당 처음 한 번)
을 받아 저장한다. 요청은 settings.collection.datacenterMinIntervalSec 간격으로 보낸다.

처리 순서: 최신 랭킹 스냅샷 1~20위가 쓰는 카드 -> 1~200위 사용자 수가 많은 카드 순.
--max-age-hours 안에 이미 받은 카드는 건너뛰므로 중단 후 다시 실행하면 이어서 진행된다.

실행:  python -m fco.collect_prices [--limit N] [--max-age-hours H] [--only-top20]
"""
import argparse
import sys
from datetime import datetime, timedelta

from . import datacenter, http
from .config import load_settings, active_price_range
from .db import connect, log, now_kst, KST


def card_queue(con, only_top20: bool):
    snap = con.execute(
        "SELECT snap_id FROM ranking_snapshot WHERE status IN ('complete','partial') "
        "ORDER BY snap_id DESC LIMIT 1").fetchone()
    if not snap:
        raise SystemExit("랭킹 스냅샷이 없습니다. 먼저 python -m fco.collect_squads 를 실행하세요.")
    sql = """
        SELECT k.spid, k.grade, k.player_name,
               MAX(CASE WHEN s.rank <= 20 THEN 1 ELSE 0 END) AS in_top20,
               COUNT(DISTINCT s.rank) AS users
        FROM card k
        LEFT JOIN squad_entry s ON s.spid = k.spid AND s.grade = k.grade AND s.snap_id = ?
        GROUP BY k.spid, k.grade
    """
    rows = con.execute(sql, (snap["snap_id"],)).fetchall()
    if only_top20:
        rows = [r for r in rows if r["in_top20"]]
    rows.sort(key=lambda r: (-r["in_top20"], -r["users"], r["spid"], r["grade"]))
    return rows


def save_price(con, spid, grade, res, fetched_at, today):
    days, date_check = datacenter.assign_dates(res["labels"], today)
    pts = [(d.isoformat(), v) for d, v in zip(days, res["values"]) if v > 0]
    con.executemany(
        "INSERT INTO price_daily (spid, grade, day, price, fetched_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(spid, grade, day) DO UPDATE SET price = excluded.price, fetched_at = excluded.fetched_at",
        [(spid, grade, d, v, fetched_at) for d, v in pts],
    )
    if res["current"]:
        con.execute(
            "INSERT OR IGNORE INTO price_current (spid, grade, observed_at, price) VALUES (?, ?, ?, ?)",
            (spid, grade, fetched_at, res["current"]),
        )
    status = "ok" if pts and res["current"] else "no_data"
    con.execute(
        "INSERT OR REPLACE INTO price_status (spid, grade, status, days, first_day, last_day, "
        "current_price, date_check, message, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (spid, grade, status, len(pts), pts[0][0] if pts else None, pts[-1][0] if pts else None,
         res["current"], date_check, None if status == "ok" else "데이터센터에 시세 없음", fetched_at),
    )
    return status


def ensure_card(con, spid: int, grade: int) -> None:
    """랭커 스쿼드에 없던 카드(예: 내가 산 카드)를 카드 마스터에 등록한다. 이름·시즌은 공식 메타데이터에서."""
    ts = now_kst()
    p = con.execute("SELECT name FROM meta_player WHERE spid = ?", (spid,)).fetchone()
    c = con.execute("SELECT name FROM meta_class WHERE class_id = ?", (spid // 1_000_000,)).fetchone()
    con.execute(
        "INSERT INTO card (spid, grade, player_id, class_id, player_name, class_name, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(spid, grade) DO NOTHING",
        (spid, grade, spid % 1_000_000, spid // 1_000_000, p["name"] if p else None, c["name"] if c else None, ts, ts))
    con.commit()


def fetch_cards(con, keys: list[tuple[int, int]], throttle, progress: bool = True, ability: bool = True) -> dict:
    """카드별 가격 그래프(현재가 + 일별 가격)를 받고, 처음 보는 카드는 OVR·포지션도 받는다.
    실패해도 기존 가격 기록은 지우지 않는다."""
    have_ability = {(r["spid"], r["grade"]) for r in con.execute("SELECT spid, grade FROM card_ability")}
    counts = {"ok": 0, "no_data": 0, "error": 0}
    today = datetime.now(KST).date()
    for i, (spid, grade) in enumerate(keys, 1):
        ts = now_kst()
        try:
            res = datacenter.fetch_price_graph(spid, grade, throttle)
            status = save_price(con, spid, grade, res, ts, today)
            if ability and (spid, grade) not in have_ability and status == "ok":
                ab = datacenter.fetch_card_ability(spid, grade, throttle)
                con.execute(
                    "INSERT OR REPLACE INTO card_ability (spid, grade, ovr, main_position, dc_name, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (spid, grade, ab["ovr"], ab["main_position"], ab["dc_name"], ts))
                con.execute(
                    "UPDATE card SET ovr = ?, main_position = ?, player_name = COALESCE(player_name, ?) "
                    "WHERE spid = ? AND grade = ?",
                    (ab["ovr"], ab["main_position"], ab["dc_name"], spid, grade))
        except Exception as e:
            status = "error"
            con.execute(
                "INSERT INTO price_status (spid, grade, status, message, fetched_at) VALUES (?, ?, 'error', ?, ?) "
                "ON CONFLICT(spid, grade) DO UPDATE SET status = 'error', message = excluded.message, "
                "fetched_at = excluded.fetched_at", (spid, grade, str(e)[:500], ts))
        con.commit()
        counts[status] += 1
        if progress and (i % 50 == 0 or i == len(keys)):
            print(f"  ..{i}/{len(keys)}  ok {counts['ok']} / 시세없음 {counts['no_data']} / 오류 {counts['error']}",
                  flush=True)
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="이번 실행에서 처리할 최대 카드 수")
    ap.add_argument("--max-age-hours", type=float, default=20.0,
                    help="이 시간 안에 받은 카드는 건너뜀 (기본 20)")
    ap.add_argument("--only-top20", action="store_true", help="1~20위 스쿼드 카드만")
    args = ap.parse_args(argv)

    settings = load_settings()
    throttle = http.Throttle(float(settings["collection"]["datacenterMinIntervalSec"]))
    con = connect()

    cutoff = (datetime.now(KST) - timedelta(hours=args.max_age_hours)).isoformat(timespec="seconds")
    fresh = {(r["spid"], r["grade"]) for r in con.execute(
        "SELECT spid, grade FROM price_status WHERE status IN ('ok','no_data') AND fetched_at >= ?", (cutoff,))}
    queue = [(r["spid"], r["grade"]) for r in card_queue(con, args.only_top20) if (r["spid"], r["grade"]) not in fresh]
    if args.limit:
        queue = queue[: args.limit]
    print(f"[prices] 대상 {len(queue)}장 (최근 {args.max_age_hours:g}시간 내 수집분 {len(fresh)}장 건너뜀)")
    counts = fetch_cards(con, queue, throttle)

    lo, hi, preset = active_price_range(settings)
    in_range = con.execute(
        "SELECT COUNT(*) FROM price_status WHERE status = 'ok' AND current_price BETWEEN ? AND ?", (lo, hi)
    ).fetchone()[0]
    summary = (f"처리 {len(queue)}장: ok {counts['ok']}, 시세없음 {counts['no_data']}, 오류 {counts['error']} / "
               f"현재가 {preset} 구간 카드 {in_range}장")
    log(con, "collect_prices", counts["error"] == 0, summary)
    print("[완료] " + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
