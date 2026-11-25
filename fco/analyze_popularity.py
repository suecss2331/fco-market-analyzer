"""5단계 실행: 최신 랭킹 스냅샷으로 인기 스쿼드 점수를 계산해 popularity_stats 에 저장한다.

직전 스냅샷이 있으면 점수 변화와 급락 경고를 함께 저장한다 (스냅샷이 1개뿐이면 '비교 불가').
1~20위 스쿼드만 새로 받으려면:  python -m fco.collect_squads --top 20   (API 약 40건)

실행:  python -m fco.analyze_popularity [--preset 10억-100억]
"""
import argparse
import sys

from .config import load_settings, active_price_range
from .db import connect, log, now_kst
from .popularity import compute_popularity, compare_snapshots


def snapshots(con, top_n: int):
    return con.execute(
        "SELECT snap_id, collected_at, top_n FROM ranking_snapshot WHERE status = 'complete' AND top_n >= ? "
        "ORDER BY snap_id DESC LIMIT 2", (top_n,)).fetchall()


def entries(con, snap_id: int):
    return [dict(r) for r in con.execute(
        "SELECT rank, spid, grade, is_sub FROM squad_entry WHERE snap_id = ? AND grade >= 1", (snap_id,))]


def eok(v) -> str:
    return "-" if v is None else f"{v / 1e8:,.2f}억"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset")
    args = ap.parse_args(argv)
    settings = load_settings()
    if args.preset:
        settings["priceRange"]["activePreset"] = args.preset
    cfg = settings["popularSquad"]
    top_n = int(cfg["topN"])
    lo, hi, preset = active_price_range(settings)
    con = connect()

    snaps = snapshots(con, top_n)
    if not snaps:
        raise SystemExit("완료된 랭킹 스냅샷이 없습니다. 먼저 python -m fco.collect_squads 를 실행하세요.")
    cur_snap = snaps[0]
    prev_snap = snaps[1] if len(snaps) > 1 else None
    now = compute_popularity(entries(con, cur_snap["snap_id"]), cfg)
    prev = compute_popularity(entries(con, prev_snap["snap_id"]), cfg) if prev_snap else None
    diff = compare_snapshots(now, prev, cfg)

    # 참고 지표: 후보군 전체(1~200위) 사용자 수 — 점수에는 반영하지 않는다
    universe = {(r["spid"], r["grade"]): r["n"] for r in con.execute(
        """SELECT spid, grade, COUNT(DISTINCT rank) n FROM squad_entry
           WHERE snap_id = (SELECT MAX(snap_id) FROM ranking_snapshot WHERE status='complete')
           GROUP BY spid, grade""")}

    ts = now_kst()
    rows = []
    for r in con.execute("SELECT spid, grade FROM card"):
        k = (r["spid"], r["grade"])
        p = now.get(k)
        d = diff.get(k, {"prev_score": None if prev is None else 0, "change_pct": None,
                         "drop_alert": None if prev is None else 0})
        rows.append((k[0], k[1], ts, cur_snap["snap_id"], cur_snap["collected_at"],
                     p["score"] if p else 0, p["appearances"] if p else 0, p["starter_appearances"] if p else 0,
                     p["best_rank"] if p else None, ",".join(map(str, p["ranks"])) if p else None,
                     universe.get(k, 0), prev_snap["snap_id"] if prev_snap else None,
                     d["prev_score"], d["change_pct"],
                     None if d["drop_alert"] is None else int(d["drop_alert"])))
    con.execute("DELETE FROM popularity_stats")
    con.executemany(f"INSERT INTO popularity_stats VALUES ({','.join('?' * 15)})", rows)
    con.commit()
    alerts = sum(1 for x in rows if x[14] == 1)
    log(con, "analyze_popularity", True, f"스냅샷 #{cur_snap['snap_id']} 점수 계산, 1~{top_n}위 카드 {len(now)}종")

    print(f"[popularity] 스냅샷 #{cur_snap['snap_id']} ({cur_snap['collected_at']}) 1~{top_n}위 스쿼드 기준")
    print(f"  점수 > 0 카드 {len(now)}종 / 만점 {sum(range(1, top_n + 1))}점")
    print("  직전 비교: " + (f"스냅샷 #{prev_snap['snap_id']} 대비 급락 경고 {alerts}장" if prev_snap
                           else "비교 불가 (완료된 스냅샷이 1개뿐 — 다음 수집부터 계산)"))

    q = """SELECT p.*, k.player_name, k.class_name, k.ovr, k.main_position, s.current_price
           FROM popularity_stats p JOIN card k USING (spid, grade)
           LEFT JOIN price_status s USING (spid, grade)"""
    print("\n전체 상위 10장")
    for r in con.execute(q + " ORDER BY p.score DESC LIMIT 10"):
        print(f"  {r['score']:5.1f}점 | {r['player_name']} {r['class_name'].split(' (')[0]} {r['grade']}강"
              f" | 스쿼드 {r['appearances']}개(선발 {r['starter_appearances']}) 순위 {r['ranks']} | 현재 {eok(r['current_price'])}")

    sel = con.execute(q + " WHERE s.status='ok' AND s.current_price BETWEEN ? AND ? ORDER BY p.score DESC",
                      (lo, hi)).fetchall()
    scored = [r for r in sel if r["score"] > 0]
    print(f"\n[{preset}] {len(sel)}장 중 1~{top_n}위 스쿼드에 등장 {len(scored)}장, 미등장 {len(sel) - len(scored)}장")
    for r in scored[:15]:
        print(f"  {r['score']:5.1f}점 | {r['player_name']} {r['class_name'].split(' (')[0]} {r['grade']}강 OVR{r['ovr']}"
              f" {r['main_position']} | 스쿼드 {r['appearances']}개(선발 {r['starter_appearances']}) 순위 {r['ranks']}"
              f" | 1~200위 {r['users_top_universe']}명 | 현재 {eok(r['current_price'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
