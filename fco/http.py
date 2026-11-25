"""요청 간격을 지키는 HTTP 도우미 (표준 라이브러리만 사용)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "fco-deal-tool/0.1 (personal, low-rate)"


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} {url} :: {body[:300]}")
        self.status = status
        self.body = body


class Throttle:
    """연속 요청 사이 최소 간격(초)을 보장한다."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


def request(url: str, *, method: str = "GET", headers: dict | None = None,
            form: dict | None = None, timeout: float = 20.0,
            retries: int = 2) -> str:
    data = None
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            # 429/5xx 만 재시도. 4xx(파라미터 오류 등)는 즉시 실패.
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            raise HttpError(e.code, body, url) from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            raise HttpError(0, str(e), url) from None


def get_json(url: str, **kw):
    return json.loads(request(url, **kw))
