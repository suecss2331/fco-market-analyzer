"""넥슨 공식 데이터센터 화면 데이터 파싱. 공개 API 가 없으므로 요청 간격을 두고 필요한 만큼만 요청한다."""
import html
import re
from datetime import date, timedelta

from . import http

BASE = "https://fconline.nexon.com"
RANK_PAGE_SIZE = 20

_TR_SPLIT = re.compile(r'<div class="tr"[^>]*>')
_RANK_NO = re.compile(r'class="td rank_no"[^>]*>\s*(\d+)\s*<')
_NAME = re.compile(r'class="name profile_pointer"\s+data-sn="(\d*)"[^>]*>(.*?)</span>', re.S)
_CLUB_VALUE = re.compile(r'class="price"\s+alt="([\d,]+)"')
_SCORE = re.compile(r'class="td rank_r_win_point"[^>]*>\s*([\d.]+)\s*<')


def parse_rank_page(page_html: str) -> list[dict]:
    rows = []
    for chunk in _TR_SPLIT.split(page_html)[1:]:
        m_rank, m_name = _RANK_NO.search(chunk), _NAME.search(chunk)
        if not (m_rank and m_name):
            continue  # 헤더 행
        m_val, m_score = _CLUB_VALUE.search(chunk), _SCORE.search(chunk)
        rows.append({
            "rank": int(m_rank.group(1)),
            "data_sn": m_name.group(1) or None,
            "nickname": html.unescape(re.sub(r"<[^>]+>", "", m_name.group(2))).strip(),
            "club_value": int(m_val.group(1).replace(",", "")) if m_val else None,
            "rank_score": float(m_score.group(1)) if m_score else None,
        })
    return rows


def fetch_official_ranking(top_n: int, throttle: http.Throttle) -> list[dict]:
    """공식경기(1vs1) 현재 시즌 랭킹 1~top_n 위."""
    out: list[dict] = []
    pages = (top_n + RANK_PAGE_SIZE - 1) // RANK_PAGE_SIZE
    for page in range(1, pages + 1):
        throttle.wait()
        body = http.request(f"{BASE}/datacenter/rank_inner?rt=1vs1&n4seasonno=0&n4pageno={page}")
        rows = parse_rank_page(body)
        if not rows:
            raise RuntimeError(f"랭킹 {page}페이지 파싱 결과가 비어 있음 (페이지 구조 변경 가능성)")
        out.extend(rows)
    out = [r for r in out if r["rank"] <= top_n]
    out.sort(key=lambda r: r["rank"])
    return out


# --- 선수 상세 -----------------------------------------------------------
_OVR = re.compile(r'class="ovr value"[^>]*>\s*(\d+)')
# 카드 앞면의 OVR 바로 다음 칸이 주 포지션. ('position st value' 는 포지션별 OVR 목록이라 쓰지 않는다)
_MAIN_POS = re.compile(r'class="ovr value"[^>]*>\s*\d+\s*</div>\s*<div class="position">\s*(\w+)')
_DC_NAME = re.compile(r'class="name"[^>]*>\s*([^<]{1,60}?)\s*<')


def fetch_card_ability(spid: int, grade: int, throttle: http.Throttle) -> dict:
    """강화 단계가 반영된 OVR, 주 포지션, 데이터센터 표기 선수명."""
    throttle.wait()
    body = http.request(f"{BASE}/datacenter/PlayerAbility", method="POST",
                        form={"spid": spid, "n1strong": grade})
    m_ovr, m_pos, m_name = _OVR.search(body), _MAIN_POS.search(body), _DC_NAME.search(body)
    return {
        "ovr": int(m_ovr.group(1)) if m_ovr else None,
        "main_position": m_pos.group(1).upper() if m_pos else None,
        "dc_name": html.unescape(m_name.group(1)).strip() if m_name else None,
    }


# --- 가격 ---------------------------------------------------------------
_CURRENT = re.compile(r'현재가</span>\s*<strong alt="([\d,]*)"')
_TIME = re.compile(r'"time"\s*:\s*\[(.*?)\]', re.S)
_VALUE = re.compile(r'"value"\s*:\s*\[(.*?)\]', re.S)
_QUOTED = re.compile(r'"([^"]*)"')


def fetch_price_graph(spid: int, grade: int, throttle: http.Throttle) -> dict:
    """현재가 + 일별 가격 원본 ('M.DD' 라벨, 문자열 값). 연도는 붙어 있지 않다."""
    throttle.wait()
    body = http.request(f"{BASE}/datacenter/PlayerPriceGraph", method="POST",
                        form={"spid": spid, "n1strong": grade})
    m_cur, m_t, m_v = _CURRENT.search(body), _TIME.search(body), _VALUE.search(body)
    if not (m_t and m_v):
        raise RuntimeError("가격 그래프 데이터를 찾지 못함 (페이지 구조 변경 가능성)")
    labels = _QUOTED.findall(m_t.group(1))
    values = _QUOTED.findall(m_v.group(1))
    if len(labels) != len(values):
        raise RuntimeError(f"날짜 {len(labels)}개 / 값 {len(values)}개 불일치")
    cur = m_cur.group(1).replace(",", "") if m_cur else ""
    return {
        "current": int(cur) if cur.isdigit() and int(cur) > 0 else None,
        "labels": labels,
        "values": [int(v) if v.isdigit() else 0 for v in values],
    }


def assign_dates(labels: list[str], today: date) -> tuple[list[date], str]:
    """연도 없는 'M.DD' 라벨에 날짜를 붙인다.

    마지막 라벨을 오늘 이전의 가장 가까운 같은 월·일로 두고, 하루씩 거슬러 올라가며
    각 라벨이 실제 날짜와 맞는지 검사한다. 하나라도 어긋나면 라벨별로 개별 추정하고 경고를 남긴다.
    """
    def parse(lbl):
        m, d = lbl.split(".")
        return int(m), int(d)

    def on_or_before(limit: date, m: int, d: int) -> date:
        y = limit.year
        while True:  # 2월 29일처럼 없는 해는 건너뛴다
            try:
                cand = date(y, m, d)
            except ValueError:
                y -= 1
                continue
            if cand <= limit:
                return cand
            y -= 1

    if not labels:
        return [], "ok"
    lm, ld = parse(labels[-1])
    last = on_or_before(today, lm, ld)
    seq = [last - timedelta(days=len(labels) - 1 - i) for i in range(len(labels))]
    if all((d.month, d.day) == parse(l) for d, l in zip(seq, labels)):
        return seq, "ok"
    # 연속이 아닌 경우: 뒤에서부터 직전 날짜보다 앞선 가장 가까운 같은 월·일
    out, nxt = [], last + timedelta(days=1)
    for lbl in reversed(labels):
        m, d = parse(lbl)
        cand = on_or_before(nxt - timedelta(days=1), m, d)
        out.append(cand)
        nxt = cand
    out.reverse()
    return out, "날짜가 연속되지 않음(빠진 날 있음) — 라벨별로 연도 추정"
