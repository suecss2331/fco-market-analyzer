"""10단계: 자동 갱신.

  python -m fco.refresh quick   (2시간마다 권장, 약 2~4분)
      보유 매물 + 활성 가격대 Deal Score 상위 quickTopN + '지금 매수'·'위험' 판정 카드의 현재가·일별 가격
      → 4~8단계 재계산 → 오늘 추천 기록
  python -m fco.refresh daily   (하루 1번 권장, 약 40~50분)
      공식경기 랭킹 1~200위 스쿼드 (Open API 약 400건) + 전체 카드 가격 → 재계산 → 추천 기록

데이터센터 현재가는 2시간마다 바뀌므로 2시간보다 자주 돌려도 새 값이 나오지 않는다.
한 번에 하나만 실행된다 (data/refresh.lock). 실패해도 기존 데이터는 지우지 않고 collect_log 에 남긴다.
"""
import argparse
import os
import sys
import time
import traceback

from . import collect_prices, collect_squads, fionhub, http, longterm, rankerpick
from .collect_prices import fetch_cards
from .config import DATA_DIR, active_price_range, load_settings
from .db import connect, log
from .pipeline import log_recommendations, rerun_analysis

LOCK = DATA_DIR / "refresh.lock"
STALE_LOCK_SEC = 3 * 3600


def acquire_lock() -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime > STALE_LOCK_SEC:
        LOCK.unlink(missing_ok=True)  # 비정상 종료로 남은 잠금
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def watchlist(con, settings) -> list[tuple[int, int]]:
    lo, hi, _ = active_price_range(settings)
    top_n = int(settings["refresh"]["quickTopN"])
    keys = [(r[0], r[1]) for r in con.execute("SELECT DISTINCT spid, grade FROM holding")]
    keys += [(r[0], r[1]) for r in con.execute(
        """SELECT p.spid, p.grade FROM pricing_stats p JOIN deal_stats d USING (spid, grade)
           WHERE p.current_price BETWEEN ? AND ? ORDER BY d.deal_score DESC LIMIT ?""", (lo, hi, top_n))]
    keys += [(r[0], r[1]) for r in con.execute(
        "SELECT spid, grade FROM pricing_stats WHERE verdict IN ('buy_now','risky') AND current_price BETWEEN ? AND ?",
        (lo, hi))]
    return list(dict.fromkeys(keys))  # 순서 유지 중복 제거 (보유 매물 먼저)


def run(mode: str) -> int:
    settings = load_settings()
    con = connect()
    t0 = time.time()
    try:
        if mode == "daily":
            collect_squads.main(["--skip-meta"] if con.execute("SELECT COUNT(*) FROM meta_player").fetchone()[0] else [])
            collect_prices.main(["--max-age-hours", "20"])
            # 보유 매물이 랭커 카드가 아니어도 갱신
            held = [(r[0], r[1]) for r in con.execute("SELECT DISTINCT spid, grade FROM holding")]
            fetch_cards(con, held, http.Throttle(float(settings["collection"]["datacenterMinIntervalSec"])), False)
        else:
            keys = watchlist(con, settings)
            print(f"[quick] 대상 {len(keys)}장", flush=True)
            fetch_cards(con, keys, http.Throttle(float(settings["collection"]["datacenterMinIntervalSec"])))
        mk_msg = ""
        try:  # fionhub 상한가·하한가 (보유 매물 + 추천 상위). 실패해도 나머지 갱신은 유지
            res = fionhub.collect(con, fionhub.watch_keys(con, settings), progress=False)
            mk_msg = f", 상한가·하한가 {res['ok']}장" + (f" (실패 {len(res['failed'])})" if res["failed"] else "")
        except Exception as e:
            mk_msg = f", 상한가·하한가 실패: {e}"
        try:  # 랭커 픽 추천 (daily: fc-info 표 + 후보 가격, quick: 거래량만)
            mk_msg += ", " + rankerpick.run(con, settings, mode, progress=False)
        except Exception as e:
            mk_msg += f", 랭커 픽 실패: {e}"
        try:  # 탭3 장기투자: 두 순위표 상위 카드의 fionhub 거래량 · 매물 (받은 카드만 점수에 씀)
            r = longterm.refresh_market(con, settings)
            mk_msg += f", 장기투자 거래량 {r['ok']}/{r['requested']}장"
        except Exception as e:
            mk_msg += f", 장기투자 거래량 실패: {e}"
        rerun_analysis()
        n = log_recommendations(con)
        msg = f"{mode} 완료 {time.time() - t0:.0f}초, 추천 기록 {n}건{mk_msg}"
        log(con, f"refresh_{mode}", True, msg)
        print("[refresh] " + msg)
        return 0
    except Exception as e:
        log(con, f"refresh_{mode}", False, f"{e}\n{traceback.format_exc()[-1500:]}")
        print(f"[refresh] 실패 (기존 데이터 유지): {e}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["quick", "daily"])
    args = ap.parse_args(argv)
    if not acquire_lock():
        print("[refresh] 다른 갱신이 실행 중이라 건너뜀")
        return 0
    try:
        return run(args.mode)
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
