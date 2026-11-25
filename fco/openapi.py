"""NEXON Open API 클라이언트. 초당·일일 호출 한도를 지킨다 (개발 키: 5건/초, 1,000건/일)."""
import urllib.parse

from . import http
from .db import today_kst

BASE = "https://open.api.nexon.com"
MATCHTYPE_OFFICIAL = 50  # 공식경기
SUB_POSITION = 28        # spposition 28 = SUB(교체 명단)


class BudgetExceeded(Exception):
    pass


class OpenApi:
    def __init__(self, con, api_key: str, max_per_second: float, daily_cap: int):
        self.con = con
        self.key = api_key
        self.throttle = http.Throttle(1.0 / max_per_second)
        self.daily_cap = daily_cap

    # --- 호출 수 관리 -------------------------------------------------
    def used_today(self) -> int:
        row = self.con.execute("SELECT calls FROM api_usage WHERE day = ?", (today_kst(),)).fetchone()
        return row["calls"] if row else 0

    def _count(self) -> None:
        self.con.execute(
            "INSERT INTO api_usage (day, calls) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
            (today_kst(),),
        )
        self.con.commit()

    def _get(self, path: str, params: dict | None = None):
        if self.used_today() >= self.daily_cap:
            raise BudgetExceeded(f"오늘 Open API 호출 한도({self.daily_cap}건) 도달")
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        self.throttle.wait()
        self._count()
        return http.get_json(url, headers={"x-nxopen-api-key": self.key})

    # --- 엔드포인트 ----------------------------------------------------
    def ouid(self, nickname: str) -> str:
        return self._get("/fconline/v1/id", {"nickname": nickname})["ouid"]

    def match_ids(self, ouid: str, matchtype: int = MATCHTYPE_OFFICIAL,
                  offset: int = 0, limit: int = 1) -> list[str]:
        return self._get("/fconline/v1/user/match",
                         {"ouid": ouid, "matchtype": matchtype, "offset": offset, "limit": limit})

    def match_detail(self, match_id: str) -> dict:
        return self._get("/fconline/v1/match-detail", {"matchid": match_id})


def static_meta(name: str):
    """메타데이터(JSON)는 호출 한도와 무관한 정적 파일이다."""
    return http.get_json(f"{BASE}/static/fconline/meta/{name}.json")
