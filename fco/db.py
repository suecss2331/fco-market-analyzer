"""SQLite 저장소. 수집 실패 시 기존 데이터는 그대로 두고, 모든 행에 수집 시각을 남긴다."""
import sqlite3
from datetime import datetime, timezone, timedelta

from .config import DB_PATH, DATA_DIR

KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_player (
    spid        INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_class (
    class_id    INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    img         TEXT,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_position (
    sp_position INTEGER PRIMARY KEY,
    code        TEXT NOT NULL
);

-- 공식경기 랭킹 스냅샷 (수집할 때마다 1행)
CREATE TABLE IF NOT EXISTS ranking_snapshot (
    snap_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    mode         TEXT NOT NULL,           -- '1vs1'
    top_n        INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    status       TEXT NOT NULL            -- 'ranking_ok' / 'complete' / 'partial'
);
CREATE TABLE IF NOT EXISTS ranking_entry (
    snap_id      INTEGER NOT NULL REFERENCES ranking_snapshot(snap_id),
    rank         INTEGER NOT NULL,
    nickname     TEXT NOT NULL,
    data_sn      TEXT,
    rank_score   REAL,
    club_value   INTEGER,
    PRIMARY KEY (snap_id, rank)
);

-- 구단주명 -> ouid 캐시 (ouid 는 게임 측 사정으로 바뀔 수 있어 확인 시각을 남긴다)
CREATE TABLE IF NOT EXISTS ranker_ouid (
    nickname     TEXT PRIMARY KEY,
    ouid         TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);

-- 랭커가 최근 공식경기에서 쓴 선수 카드
CREATE TABLE IF NOT EXISTS squad_entry (
    snap_id      INTEGER NOT NULL REFERENCES ranking_snapshot(snap_id),
    rank         INTEGER NOT NULL,
    nickname     TEXT NOT NULL,
    match_id     TEXT NOT NULL,
    match_date   TEXT,
    spid         INTEGER NOT NULL,
    grade        INTEGER NOT NULL,
    sp_position  INTEGER NOT NULL,
    is_sub       INTEGER NOT NULL,
    PRIMARY KEY (snap_id, rank, spid, sp_position)
);
CREATE TABLE IF NOT EXISTS squad_fetch (
    snap_id      INTEGER NOT NULL,
    rank         INTEGER NOT NULL,
    nickname     TEXT NOT NULL,
    status       TEXT NOT NULL,           -- 'ok' / 'no_ouid' / 'no_match' / 'error' / 'budget'
    message      TEXT,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (snap_id, rank)
);

-- 카드 마스터: (spid, 강화) 가 카드의 고유 키. 선수명으로 매칭하지 않는다.
CREATE TABLE IF NOT EXISTS card (
    spid         INTEGER NOT NULL,
    grade        INTEGER NOT NULL,
    player_id    INTEGER NOT NULL,        -- spid % 1,000,000
    class_id     INTEGER NOT NULL,        -- spid // 1,000,000
    player_name  TEXT,
    class_name   TEXT,
    ovr          INTEGER,                 -- 강화 반영 OVR (데이터센터, 3단계에서 채움)
    main_position TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (spid, grade)
);

-- 데이터센터 일별 가격 (하루 1개 값, 최근 365일). 0 원은 '값 없음'으로 보고 저장하지 않는다.
CREATE TABLE IF NOT EXISTS price_daily (
    spid         INTEGER NOT NULL,
    grade        INTEGER NOT NULL,
    day          TEXT NOT NULL,           -- YYYY-MM-DD (KST)
    price        INTEGER NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (spid, grade, day)
);
-- 데이터센터 '현재가' 관측 기록 (2시간 단위 갱신 값을 관측할 때마다 1행)
CREATE TABLE IF NOT EXISTS price_current (
    spid         INTEGER NOT NULL,
    grade        INTEGER NOT NULL,
    observed_at  TEXT NOT NULL,
    price        INTEGER NOT NULL,
    PRIMARY KEY (spid, grade, observed_at)
);
-- 카드별 가격 수집 상태 (마지막 갱신 시각 표시용)
CREATE TABLE IF NOT EXISTS price_status (
    spid          INTEGER NOT NULL,
    grade         INTEGER NOT NULL,
    status        TEXT NOT NULL,          -- 'ok' / 'no_data' / 'error'
    days          INTEGER,
    first_day     TEXT,
    last_day      TEXT,
    current_price INTEGER,
    date_check    TEXT,                   -- 'ok' 또는 날짜 역산 경고
    message       TEXT,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (spid, grade)
);
CREATE TABLE IF NOT EXISTS card_ability (
    spid          INTEGER NOT NULL,
    grade         INTEGER NOT NULL,
    ovr           INTEGER,
    main_position TEXT,
    dc_name       TEXT,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (spid, grade)
);

-- 4단계 평일·주말 분석 결과 (분석할 때마다 카드별로 덮어쓴다; 계산 시각·기간을 함께 저장)
CREATE TABLE IF NOT EXISTS weekly_stats (
    spid          INTEGER NOT NULL,
    grade         INTEGER NOT NULL,
    computed_at   TEXT NOT NULL,
    window_start  TEXT, window_end TEXT,
    current_price INTEGER, current_at TEXT,
    wd_n INTEGER, wd_mean REAL, wd_median REAL,
    we_n INTEGER, we_mean REAL, we_median REAL,
    premium_pct REAL, discount_pct REAL,
    weeks_total INTEGER, weeks_positive INTEGER, weekly_premium_median_pct REAL,
    transient INTEGER,
    status TEXT NOT NULL, note TEXT,
    PRIMARY KEY (spid, grade)
);

-- 5단계 인기 스쿼드 점수 (카드 마스터 전체에 대해 계산; 1~20위 미사용 카드는 score 0)
CREATE TABLE IF NOT EXISTS popularity_stats (
    spid          INTEGER NOT NULL,
    grade         INTEGER NOT NULL,
    computed_at   TEXT NOT NULL,
    snap_id       INTEGER NOT NULL,
    snap_at       TEXT NOT NULL,
    score         REAL NOT NULL,
    appearances   INTEGER NOT NULL,
    starter_appearances INTEGER NOT NULL,
    best_rank     INTEGER,
    ranks         TEXT,                   -- 예: "1,3,17"
    users_top_universe INTEGER NOT NULL,  -- 참고: 1~200위 중 사용자 수 (점수에는 반영 안 함)
    prev_snap_id  INTEGER,
    prev_score    REAL,
    change_pct    REAL,
    drop_alert    INTEGER,
    PRIMARY KEY (spid, grade)
);

-- 6단계 가격 안정성 (s_ = 단기 7일, l_ = 장기 30일)
CREATE TABLE IF NOT EXISTS stability_stats (
    spid INTEGER NOT NULL, grade INTEGER NOT NULL,
    computed_at TEXT NOT NULL, end_day TEXT NOT NULL,
    s_n INTEGER, s_coverage REAL, s_max_gap INTEGER, s_mean REAL, s_std REAL, s_cv REAL,
    s_change_pct REAL, s_range_pct REAL, s_trend_pct_per_day REAL, s_trend TEXT,
    l_n INTEGER, l_coverage REAL, l_max_gap INTEGER, l_mean REAL, l_std REAL, l_cv REAL,
    l_change_pct REAL, l_range_pct REAL, l_trend_pct_per_day REAL, l_trend TEXT,
    max_daily_drop_pct REAL, max_daily_rise_pct REAL,
    sharp_drop INTEGER, sharp_rise INTEGER, persistent_decline INTEGER,
    flat_run_days INTEGER, stale INTEGER,
    status TEXT NOT NULL, note TEXT,
    PRIMARY KEY (spid, grade)
);

-- 7단계 매수 적정가 · 수익률 (계산식은 fco/pricing.py)
CREATE TABLE IF NOT EXISTS pricing_stats (
    spid INTEGER NOT NULL, grade INTEGER NOT NULL, computed_at TEXT NOT NULL,
    fee_rate REAL NOT NULL, min_target_roi REAL NOT NULL,
    current_price INTEGER, basis_price REAL, factor REAL, buy_fair REAL,
    premium_used_pct REAL, sell_expected REAL, fee_amount REAL,
    net_at_current REAL, roi_at_current_pct REAL, break_even_sell_at_current REAL,
    max_buy_for_target REAL, target_buy REAL, net_at_target REAL, roi_at_target_pct REAL,
    required_discount_pct REAL,
    reach_days INTEGER, reach_lookback INTEGER, min_daily_lookback REAL,
    current_vs_last_daily_pct REAL, risk_flags TEXT,
    verdict TEXT NOT NULL,
    PRIMARY KEY (spid, grade)
);

-- 8단계 Deal Score (parts_json 에 지표별 원값·백분위·배점·점수 = 산출 근거)
CREATE TABLE IF NOT EXISTS deal_stats (
    spid INTEGER NOT NULL, grade INTEGER NOT NULL, computed_at TEXT NOT NULL,
    price_group TEXT NOT NULL,
    pop_points REAL, discount_points REAL, weekend_points REAL, stability_points REAL,
    base_score REAL, penalty REAL, deal_score REAL,
    reliability_pct REAL, reliability TEXT,
    warnings TEXT, parts_json TEXT NOT NULL,
    PRIMARY KEY (spid, grade)
);

-- 10단계 내 보유 매물 (같은 카드를 같은 가격에 산 묶음 1건 = 1행). 사용자가 대시보드에서 입력.
CREATE TABLE IF NOT EXISTS holding (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    spid       INTEGER NOT NULL,
    grade      INTEGER NOT NULL,
    qty        INTEGER NOT NULL CHECK (qty > 0),
    buy_price  INTEGER NOT NULL CHECK (buy_price > 0),
    bought_at  TEXT NOT NULL,
    memo       TEXT,
    created_at TEXT NOT NULL
);
-- 매도 기록 (실현 손익). 매도 당시 수수료율을 함께 남긴다.
CREATE TABLE IF NOT EXISTS trade (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    spid        INTEGER NOT NULL,
    grade       INTEGER NOT NULL,
    qty         INTEGER NOT NULL,
    buy_price   INTEGER NOT NULL,
    sell_price  INTEGER NOT NULL,
    fee_rate    REAL NOT NULL,
    net_profit  REAL NOT NULL,
    bought_at   TEXT,
    sold_at     TEXT NOT NULL,
    memo        TEXT,
    created_at  TEXT NOT NULL
);
-- 보유한 수수료 할인 쿠폰 (할인율별 묶음). 사용자가 대시보드에서 입력.
CREATE TABLE IF NOT EXISTS coupon (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    discount    REAL NOT NULL CHECK (discount > 0 AND discount <= 1),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    expires_at  TEXT,
    memo        TEXT,
    created_at  TEXT NOT NULL
);
-- 관심 매물 (모바일 ② 탭 즐겨찾기). 사용자 기록이라 자동 백업 대상.
CREATE TABLE IF NOT EXISTS watch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    spid        INTEGER NOT NULL,
    grade       INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (spid, grade)
);
-- fionhub 카드 ID (spid → fionhub uuid). uuid 가 '' 이면 fionhub 에서 찾지 못한 카드.
CREATE TABLE IF NOT EXISTS fionhub_card (
    spid        INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- fionhub 이적시장 시세 기록 (상한가·하한가·호가). 갱신할 때마다 쌓인다.
CREATE TABLE IF NOT EXISTS market_snapshot (
    spid          INTEGER NOT NULL,
    grade         INTEGER NOT NULL,
    observed_at   TEXT NOT NULL,     -- fionhub 이 시장을 관측한 시각 (UTC ISO)
    fetched_at    TEXT NOT NULL,
    price         INTEGER,
    lower_limit   INTEGER,
    upper_limit   INTEGER,
    available     INTEGER,
    transfers_2h  INTEGER,           -- 최근 2시간 거래 완료 건수
    buy_levels    TEXT,              -- JSON [[가격, 건수], ...]  구매 대기 (가격 높은 순)
    sell_levels   TEXT,              -- JSON [[가격, 건수], ...]  판매 대기
    PRIMARY KEY (spid, grade, observed_at)
);
-- fc-info.com 랭커 픽 (공식경기 랭커가 선발로 쓴 카드, 팀컬러별 · 포지션별)
CREATE TABLE IF NOT EXISTS rp_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at  TEXT NOT NULL,
    total_rankers INTEGER,
    teams_done    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL            -- 'running' / 'complete' / 'failed'
);
CREATE TABLE IF NOT EXISTS rp_team (
    snap_id   INTEGER NOT NULL,
    rank      INTEGER NOT NULL,
    name      TEXT NOT NULL,
    rankers   INTEGER,
    share     REAL,                        -- 랭커 수 / 전체 랭커
    PRIMARY KEY (snap_id, name)
);
CREATE TABLE IF NOT EXISTS rp_pick (
    snap_id    INTEGER NOT NULL,
    team       TEXT NOT NULL,
    position   TEXT NOT NULL,              -- ST, CAM, L·RM, CM, CDM, LB, CB, RB, GK
    rank       INTEGER NOT NULL,
    spid       INTEGER NOT NULL,
    grade      INTEGER,                    -- 가장 많이 쓴 강화
    grades     TEXT,                       -- JSON [[강화, 사용 수], ...]
    picks      INTEGER,
    sample     INTEGER,
    pick_rate  REAL,
    name       TEXT,
    class_name TEXT,
    price      INTEGER,
    PRIMARY KEY (snap_id, team, position, spid)
);
-- fc-info 팀별 '인기 급등 선수'(rising) · '새로운 선수'(new). 비교 기간은 compare_date → base_date.
CREATE TABLE IF NOT EXISTS rp_rising (
    snap_id      INTEGER NOT NULL,
    team         TEXT NOT NULL,
    kind         TEXT NOT NULL,     -- 'rising' / 'new'
    position     TEXT,
    rank         INTEGER,           -- 지금 포지션 순위
    spid         INTEGER NOT NULL,
    grades       TEXT,              -- JSON [[강화, 사용 수], ...]
    picks        INTEGER,
    prev_picks   INTEGER,
    sample       INTEGER,
    prev_sample  INTEGER,
    pick_rate    REAL,
    rate_change  REAL,              -- 픽률 증가율 (지금 픽률 ÷ 이전 픽률 − 1). 새로운 선수는 NULL
    name         TEXT,
    class_name   TEXT,
    base_date    TEXT,
    compare_date TEXT,
    PRIMARY KEY (snap_id, team, kind, position, spid)
);
-- 랭커 픽 추천 기록 (그날 처음 추천됐을 때 값, 카드별 하루 1행). 다음 주말 가격으로 채점한다.
CREATE TABLE IF NOT EXISTS rp_rec_log (
    day            TEXT NOT NULL,
    spid           INTEGER NOT NULL,
    grade          INTEGER NOT NULL,
    rank           INTEGER,          -- 그때 점수 순위
    rp_score       REAL,
    current_price  INTEGER,
    weekend_avg    REAL,             -- 추천 당시 '지난 주말 평균'
    drop_pct       REAL,
    transfers_2h   INTEGER,
    best_pick_rate REAL,
    lower_limit    INTEGER,
    upper_limit    INTEGER,
    fee_rate       REAL NOT NULL,
    fee_coupon     REAL,             -- 추천 당시 가장 큰 쿠폰을 쓰면 수수료율 (없으면 NULL)
    logged_at      TEXT NOT NULL,
    PRIMARY KEY (day, spid, grade)
);
-- 추천 기록 (하루 1번, 카드별). 나중에 실제 가격으로 채점한다.
CREATE TABLE IF NOT EXISTS rec_log (
    day           TEXT NOT NULL,
    spid          INTEGER NOT NULL,
    grade         INTEGER NOT NULL,
    verdict       TEXT NOT NULL,
    deal_score    REAL,
    current_price INTEGER,
    target_buy    REAL,
    sell_expected REAL,
    fee_rate      REAL NOT NULL,
    logged_at     TEXT NOT NULL,
    PRIMARY KEY (day, spid, grade)
);

-- Open API 일일 호출 수 (KST 기준)
CREATE TABLE IF NOT EXISTS api_usage (
    day          TEXT PRIMARY KEY,
    calls        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS collect_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    job          TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    message      TEXT
);
"""


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)  # 대시보드와 자동 갱신이 동시에 써도 잠금을 기다린다
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """기존 DB 에 새 열을 더한다 (데이터는 그대로)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(trade)")}
    if "coupon_discount" not in cols:
        con.execute("ALTER TABLE trade ADD COLUMN coupon_discount REAL NOT NULL DEFAULT 0")
        con.commit()
    cols = {r[1] for r in con.execute("PRAGMA table_info(fionhub_card)")}
    if "team_colors" not in cols:
        con.execute("ALTER TABLE fionhub_card ADD COLUMN team_colors TEXT")  # JSON, fionhub 카드 팀컬러
        con.commit()


def log(con: sqlite3.Connection, job: str, ok: bool, message: str = "") -> None:
    con.execute(
        "INSERT INTO collect_log (at, job, ok, message) VALUES (?, ?, ?, ?)",
        (now_kst(), job, 1 if ok else 0, message[:2000]),
    )
    con.commit()
