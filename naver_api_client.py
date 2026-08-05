"""
naver_api_client.py — 네이버 부동산 매물 수집 (httpx 직접 호출판, Playwright 대체)

기존 naver_client.py 는 브라우저를 띄워 XHR 응답을 가로챘다. 이 모듈은 같은 데이터를
fin.land.naver.com 의 front-api 로 직접 받는다. 브라우저·크로미움이 필요 없어
실행 시간과 메모리가 크게 줄고, 스크롤/클릭 타이밍에 의존하지 않아 결과가 결정적이다.

    GET  /complexes/{단지번호}                     ← 세션 쿠키 발급 (부트스트랩)
    POST /front-api/v1/complex/article/list        ← 매물 목록 (lastInfo 로 다음 장)
    GET  /articles/{매물번호}                       ← 중개업소 대표전화 (목록에 없음)

세션 쿠키
    단지 페이지를 한 번 열면 응답의 Set-Cookie 로 PROP_TEST_KEY / PROP_TEST_ID 가
    내려온다. 목록 API 는 이 쿠키를 요구한다. 네이버 로그인 쿠키(NID_AUT/NID_SES)는
    필요 없다 — 공개 매물 목록이라 계정 없이 받는다. 쿠키는 파일로 남기지 않고
    httpx.Client 의 쿠키 항아리(메모리)에만 둔다.

    쿠키는 만료된다. 그래서 모든 요청은 _request() 를 거치고, 만료·차단으로 보이는
    응답(401/403/419/429/5xx, 또는 isSuccess=false)을 받으면 세션을 통째로 버리고
    부트스트랩부터 다시 한 뒤 재시도한다.

요청량 (⚠️ 2026-08-05 실측)
    ~50요청/30초를 넣었더니 HTTP 429 TOO_MANY_REQUESTS 가 걸렸고 **7분 넘게** 풀리지
    않았다. IP 기준으로 보이며, 차단 중 재시도는 창을 오히려 늘리는 듯했다.
    그래서 이 클라이언트는 요청 수 자체를 줄이는 쪽으로 설계했다.

      · 목록(단지당 1~2회)은 반드시 지켜야 할 본체다.
      · 업소전화는 상세 페이지 1건당 1요청이라 전체 요청의 90%를 차지한다.
        전화는 중개업소 단위로 거의 불변이므로 파일 캐시(office_tel_cache.json)에
        모아두고 **처음 보는 업소만** 받는다. 첫날만 수십 회, 이튿날부터 0에 수렴한다.
      · 회당 상한(NAVER_TEL_MAX_PER_RUN)을 두어 첫날에도 몰아치지 않는다.
        못 받은 업소는 다음 실행에서 이어받는다.
      · 429 를 만나면 그 실행의 전화 수집은 즉시 접는다(회로차단). 목록은 이미
        받아둔 뒤이므로 『매물장』 적재는 그대로 진행된다.

    이 순서를 지키려고 main.py 는 (1) 전 단지 목록을 먼저 받고 (2) 마지막에
    fill_office_tels() 로 전화를 채운다. 전화 수집이 막혀도 목록이 손상되지 않는다.

반환 형식
    get_complex_data() 는 naver_client.NaverRealEstateClient 와 **같은 모양**이다.
      {"complex_info": {...}, "articles": [평탄화된 dict, ...]}
    field_mapper 가 기대하는 옛 new.land 필드명(articleNo, buildingName, floorInfo,
    dealOrWarrantPrc ...)으로 _to_legacy_article() 이 옮겨 담으므로 field_mapper 와
    『매물장』 스키마는 손대지 않는다.
"""

import os
import re
import json
import time
import random
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NaverRealEstate")

BASE_URL = "https://fin.land.naver.com"
LIST_API = f"{BASE_URL}/front-api/v1/complex/article/list"

# 실제 브라우저와 동일한 UA. 이 front-api 는 모바일 웹(userChannelType=MOBILE)이
# 쓰는 엔드포인트라 UA·sec-ch-ua 를 모바일로 맞춰 헤더 조합을 일관되게 둔다.
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36"
)
SEC_CH_UA = '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'

# 이 쿠키가 있어야 목록 API 가 응답한다. 부트스트랩 성공 판정에 쓴다.
SESSION_COOKIES = ("PROP_TEST_KEY", "PROP_TEST_ID")

# 쿠키 만료·일시 차단으로 보고 세션을 갱신할 상태코드
REFRESH_STATUS = frozenset({401, 403, 407, 419, 429, 500, 502, 503, 504})

TRADE_TYPE_MAP = {"A1": "매매", "B1": "전세", "B2": "월세", "B3": "단기임대"}

# 향 코드 → 한글. 네이버는 두 글자 방위 코드를 쓴다(E동 W서 S남 N북).
# 조합 코드의 순서 표기가 갈릴 수 있어 양쪽을 모두 등록해 둔다.
DIRECTION_MAP = {
    "EE": "동향", "WW": "서향", "SS": "남향", "NN": "북향",
    "SE": "남동향", "ES": "남동향",
    "SW": "남서향", "WS": "남서향",
    "NE": "북동향", "EN": "북동향",
    "NW": "북서향", "WN": "북서향",
}

# 상세 페이지 중개사 영역의 『전화』 항목. 클래스 해시(__9aKEGa 등)는 배포마다 바뀌므로
# 해시에 기대지 않고 `__term">전화</div>` ~ `</ul>` 구간만 잡는다.
_TEL_BLOCK_RE = re.compile(r'__term">전화</div>.*?</ul>', re.S)
_TEL_LINK_RE = re.compile(r'href="tel:([0-9\-]+)"')

TEL_CACHE_FILE = Path(__file__).resolve().parent / "office_tel_cache.json"
TEL_CACHE_TTL_DAYS = 90  # 업소 전화는 잘 안 바뀌지만 영원히 믿지는 않는다


def _env(name: str, default, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        logger.warning("환경변수 %s=%r 를 해석하지 못해 기본값 %r 을 씁니다.", name, raw, default)
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return default if not raw else raw not in ("0", "false", "no", "off")


class NaverApiError(RuntimeError):
    """수집 실패. main.py 가 단지 단위로 잡아 실행로그에 남긴다."""


class NaverRateLimited(NaverApiError):
    """HTTP 429. 재시도로 풀리지 않으므로 부가 수집은 즉시 접는다."""


# ── 값 변환 ────────────────────────────────────────────────────────────────

def _to_manwon(won: Any) -> str:
    """원 단위 정수 → 만원 단위 문자열.

    field_mapper.parse_price() 가 숫자 문자열을 그대로 만원으로 읽으므로
    "17억 5,000" 같은 한글 표기로 되돌릴 필요 없이 "175000" 을 넘긴다.
    """
    try:
        amount = int(won or 0)
    except (TypeError, ValueError):
        return ""
    return str(amount // 10000) if amount else ""


def _dong_name(raw: Any) -> str:
    """동 이름 정규화. API 는 "2", 『매물장』은 "2동" 을 써왔다."""
    name = str(raw or "").strip()
    if not name or name.endswith("동"):
        return name
    return f"{name}동"


def _direction(code: Any) -> str:
    """향 코드 → 한글. 모르는 코드는 원문을 남기고 경고한다(조용히 비우지 않는다)."""
    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    label = DIRECTION_MAP.get(raw)
    if label is None:
        logger.warning("알 수 없는 향 코드 %r — 원문을 그대로 기록합니다.", raw)
        return raw
    return label


def _broker_key(broker: Dict[str, Any]) -> str:
    """업소전화 캐시 키. 전화는 매물이 아니라 중개업소에 딸린 값이다."""
    return f"{broker.get('cpId') or ''}|{broker.get('brokerageName') or ''}"


def _to_legacy_article(article: Dict[str, Any], bundle_count: int) -> Dict[str, Any]:
    """front-api 매물 1건 → 옛 new.land 필드명의 평탄한 dict.

    field_mapper._map_article() 이 읽는 키만 채운다. 업소전화(representativeTelNo)는
    목록 응답에 없어 빈칸으로 두고 fill_office_tels() 가 나중에 채운다.
    """
    detail = article.get("articleDetail") or {}
    space = article.get("spaceInfo") or {}
    price = article.get("priceInfo") or {}
    broker = article.get("brokerInfo") or {}
    trade_code = str(article.get("tradeType") or "")

    # 매매는 dealPrice, 전세·월세는 warrantyPrice 에 값이 들어온다(나머지는 0).
    main_price = price.get("dealPrice") or price.get("warrantyPrice") or 0

    return {
        "articleNo": str(article.get("articleNumber") or ""),
        "articleName": article.get("complexName") or article.get("articleName") or "",
        "buildingName": _dong_name(article.get("dongName")),
        "tradeTypeName": TRADE_TYPE_MAP.get(trade_code, trade_code),
        "floorInfo": detail.get("floorInfo") or "",
        "area1": space.get("supplySpace") or 0,
        "dealOrWarrantPrc": _to_manwon(main_price),
        "rentPrc": _to_manwon(price.get("rentPrice")),
        "direction": _direction(detail.get("direction")),
        "realtorName": broker.get("brokerageName") or "",
        "representativeTelNo": "",
        "sameAddrCnt": bundle_count,
        # 아래는 시트에 나가지 않는 내부 필드 (field_mapper 가 무시한다)
        "_brokerKey": _broker_key(broker),
    }


# ── 업소전화 캐시 ───────────────────────────────────────────────────────────

class OfficeTelCache:
    """중개업소 대표전화 파일 캐시. 실행 간에 유지돼 상세 요청을 0 에 수렴시킨다.

    ⚠️ 같은 단지 안에서 상호가 겹치는 별개 업소는 없다고 보고 (cpId, 상호) 로 묶는다.
    요청 수를 매물 수 → 업소 수로 줄이는 대가다.
    """

    def __init__(self, path: Path = TEL_CACHE_FILE, ttl_days: int = TEL_CACHE_TTL_DAYS):
        self.path = path
        self.ttl = timedelta(days=ttl_days)
        self._data: Dict[str, Dict[str, str]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("업소전화 캐시를 읽지 못해 새로 만듭니다: %s", exc)
            return
        if isinstance(raw, dict):
            self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}
            logger.info("업소전화 캐시 %d건을 읽었습니다.", len(self._data))

    def get(self, key: str) -> Optional[str]:
        """유효한 캐시 값. 없거나 만료면 None (= 다시 받아야 함)."""
        entry = self._data.get(key)
        if not entry:
            return None
        try:
            at = datetime.fromisoformat(entry.get("at", ""))
        except ValueError:
            return None
        if datetime.now() - at > self.ttl:
            return None
        return entry.get("tel", "")

    def put(self, key: str, tel: str) -> None:
        self._data[key] = {"tel": tel, "at": datetime.now().isoformat(timespec="seconds")}
        self._dirty = True

    def save(self) -> None:
        """캐시는 부가 정보다. 저장 실패로 배치를 죽이지 않는다."""
        if not self._dirty:
            return
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1, sort_keys=True),
                encoding="utf-8",
            )
            self._dirty = False
        except OSError as exc:
            logger.warning("업소전화 캐시 저장 실패(무시하고 계속): %s", exc)


# ── 클라이언트 ──────────────────────────────────────────────────────────────

class NaverApiClient:
    """fin.land front-api 로 단지 매물을 수집한다."""

    def __init__(
        self,
        page_size: int = 30,
        request_delay: Optional[float] = None,
        max_retries: Optional[int] = None,
        timeout: Optional[float] = None,
        fetch_office_tel: Optional[bool] = None,
        tel_max_per_run: Optional[int] = None,
        max_pages: int = 200,
        tel_cache: Optional[OfficeTelCache] = None,
    ):
        self.page_size = page_size
        self.request_delay = _env("NAVER_REQUEST_DELAY", 1.5, float) if request_delay is None else request_delay
        self.max_retries = _env("NAVER_MAX_RETRIES", 3, int) if max_retries is None else max_retries
        self.timeout = _env("NAVER_TIMEOUT", 20.0, float) if timeout is None else timeout
        self.fetch_office_tel = _env_flag("NAVER_FETCH_OFFICE_TEL", True) if fetch_office_tel is None else fetch_office_tel
        self.tel_max_per_run = _env("NAVER_TEL_MAX_PER_RUN", 20, int) if tel_max_per_run is None else tel_max_per_run
        self.max_pages = max_pages
        self.tel_cache = OfficeTelCache() if tel_cache is None else tel_cache

        self._client: Optional[httpx.Client] = None
        self._boot_complex_no: str = ""
        self._rate_limited = False  # 429 를 한 번 맞으면 이 실행에서는 전화 수집을 접는다

    # ── 세션 ────────────────────────────────────────────────────────────────

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "user-agent": USER_AGENT,
                "accept-language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "sec-ch-ua": SEC_CH_UA,
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
            },
        )

    def _bootstrap(self) -> None:
        """단지 페이지를 한 번 열어 세션 쿠키를 받는다.

        쿠키가 안 내려와도 여기서 끊지 않는다 — 실제로 통과하는 경우가 있고,
        정말 못 받았다면 뒤이은 API 호출이 _request() 의 재시도에서 걸러낸다.
        """
        self.close()
        self._client = self._new_client()
        try:
            resp = self._client.get(f"{BASE_URL}/complexes/{self._boot_complex_no}", headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "image/avif,image/webp,image/apng,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            })
        except httpx.HTTPError as exc:
            logger.warning("세션 부트스트랩 요청 실패: %s", exc)
            return

        got = [name for name in SESSION_COOKIES if name in self._client.cookies]
        if got:
            logger.info("세션 쿠키 발급됨 (%s)", ", ".join(got))
        else:
            logger.warning("세션 쿠키를 받지 못했습니다 (HTTP %s). 그대로 진행합니다.", resp.status_code)

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._bootstrap()
        if self._client is None:  # 부트스트랩이 클라이언트조차 못 만든 경우
            self._client = self._new_client()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.tel_cache.save()
        self.close()

    # ── 요청 ────────────────────────────────────────────────────────────────

    def _sleep(self, seconds: Optional[float] = None) -> None:
        base = self.request_delay if seconds is None else seconds
        if base > 0:
            time.sleep(base * random.uniform(0.85, 1.25))

    def _request(self, method: str, url: str, *, expect_json: bool, **kwargs) -> httpx.Response:
        """세션 자동 갱신·재시도를 얹은 요청.

        차단·만료 신호를 받으면 쿠키 항아리를 버리고 부트스트랩부터 다시 한 뒤
        재시도한다. 429 는 창이 길어(실측 7분+) 재시도 간격을 따로 크게 잡고,
        끝내 실패하면 NaverRateLimited 로 구분해 던진다.
        """
        last_reason = ""
        rate_limited = False

        for attempt in range(1, self.max_retries + 1):
            client = self._ensure_client()
            try:
                resp = client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    if not expect_json:
                        return resp
                    try:
                        body = resp.json()
                    except ValueError:
                        last_reason = "JSON 파싱 실패 (차단 페이지 응답 가능성)"
                    else:
                        if body.get("isSuccess") is False:
                            last_reason = ("isSuccess=false "
                                           f"{body.get('detailCode') or ''} {body.get('message') or ''}").strip()
                        else:
                            return resp
                elif resp.status_code == 429:
                    rate_limited = True
                    self._rate_limited = True
                    last_reason = "HTTP 429 TOO_MANY_REQUESTS"
                elif resp.status_code in REFRESH_STATUS:
                    last_reason = f"HTTP {resp.status_code}"
                else:
                    # 400/404 등은 세션이 아니라 요청 자체가 틀린 것 — 재시도해도 같다.
                    raise NaverApiError(
                        f"{method} {url} 실패: HTTP {resp.status_code} {resp.text[:200]}")

            if attempt >= self.max_retries:
                break
            # 429 는 초 단위 백오프로 안 풀린다. 분 단위로 벌린다.
            backoff = (60 * attempt) if rate_limited else min(2 ** attempt, 10)
            logger.warning("요청 실패(%s) — 세션을 갱신하고 %.0f초 뒤 재시도 (%d/%d)",
                           last_reason, backoff, attempt, self.max_retries)
            time.sleep(backoff * random.uniform(0.9, 1.2))
            self._bootstrap()

        message = f"{method} {url} 실패 ({self.max_retries}회 시도): {last_reason}"
        raise NaverRateLimited(message) if rate_limited else NaverApiError(message)

    # ── 매물 목록 ───────────────────────────────────────────────────────────

    def _list_headers(self, complex_no: str) -> Dict[str, str]:
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": BASE_URL,
            "referer": f"{BASE_URL}/complexes/{complex_no}?tab=article",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "priority": "u=1, i",
        }

    def _fetch_pages(self, complex_no: str) -> List[Dict[str, Any]]:
        """목록 API 를 lastInfo 로 끝까지 넘기며 매물을 모은다(묶음 매물 펼침 포함)."""
        payload = {
            "size": self.page_size,
            "complexNumber": str(complex_no),
            "tradeTypes": [],
            "pyeongTypes": [],
            "dongNumbers": [],
            "userChannelType": "MOBILE",
            "articleSortType": "RANKING_DESC",
            "lastInfo": [],
        }
        headers = self._list_headers(complex_no)
        articles: List[Dict[str, Any]] = []
        seen = set()

        for page in range(1, self.max_pages + 1):
            resp = self._request("POST", LIST_API, expect_json=True, json=payload, headers=headers)
            result = resp.json().get("result") or {}
            groups = result.get("list") or []

            for group in groups:
                # 같은 매물을 여러 중개사가 올린 묶음. 펼쳐서 전건을 담되
                # 묶음수(sameAddrCnt)는 그룹의 중개사 수로 채운다.
                dup = group.get("duplicatedArticleInfo") or {}
                members = dup.get("articleInfoList") or []
                if members:
                    bundle = dup.get("realtorCount") or len(members)
                else:
                    rep = group.get("representativeArticleInfo")
                    members, bundle = ([rep] if rep else []), 1

                for raw in members:
                    art = _to_legacy_article(raw, bundle)
                    if art["articleNo"] and art["articleNo"] in seen:
                        continue
                    seen.add(art["articleNo"])
                    # 캐시에 있으면 요청 없이 지금 채운다.
                    art["representativeTelNo"] = self.tel_cache.get(art["_brokerKey"]) or ""
                    articles.append(art)

            logger.info("[%s] 페이지 %d: 그룹 %d개 (누적 매물 %d개)",
                        complex_no, page, len(groups), len(articles))

            if not result.get("hasNextPage") or not (result.get("lastInfo") or []):
                break
            payload["lastInfo"] = result["lastInfo"]
            self._sleep()
        else:
            logger.warning("[%s] 페이지 상한 %d 에 도달해 중단합니다.", complex_no, self.max_pages)

        return articles

    def get_complex_data(self, complex_no: str, max_scrolls: int = 0) -> Dict[str, Any]:
        """단지 1곳의 매물 목록을 수집한다.

        naver_client.NaverRealEstateClient.get_complex_data 와 같은 형태를 돌려준다.
        업소전화는 여기서 채우지 않는다 — 전 단지 목록을 먼저 확보한 뒤
        fill_office_tels() 로 한꺼번에 채워야 부가 수집이 본체를 위협하지 않는다.
        max_scrolls 는 Playwright 판과의 호출부 호환을 위해 받기만 하고 쓰지 않는다.
        """
        complex_no = str(complex_no)
        if self._boot_complex_no != complex_no:
            self._boot_complex_no = complex_no
            self._bootstrap()  # referer·쿠키를 이번 단지로 맞춘다

        logger.info("단지 %s 매물 수집 시작 (API)", complex_no)
        articles = self._fetch_pages(complex_no)
        logger.info("단지 %s 수집 완료: 매물 %d건", complex_no, len(articles))
        return {
            "complex_info": {
                "complexName": articles[0]["articleName"] if articles else "",
                "complexNumber": complex_no,
            },
            "articles": articles,
        }

    # ── 업소전화 ────────────────────────────────────────────────────────────

    def _fetch_office_tel(self, article_no: str) -> Optional[str]:
        """상세 페이지에서 중개업소 대표전화를 뽑는다.

        『전화』 항목에는 대표전화(ret.call1)와 휴대폰(ret.call2)이 순서대로 들어있다.
        PRD §2.1 에 따라 개인 휴대폰은 쓰지 않으므로 **첫 번째만** 취한다.
        옛 경로의 representativeTelNo 와 같은 값이다.

        반환값을 두 가지로 나눈다 — 캐시에 무엇을 남길지가 달라서다.
          ""   전화 항목은 있는데 번호가 없다 → 정당한 결과. 캐시해도 된다.
          None 전화 항목 자체를 못 찾았다 → 페이지 구조가 바뀌었을 수 있다.
               이걸 ""로 캐시하면 90일간 업소전화가 조용히 비어버리므로 캐시하지 않는다.
        """
        resp = self._request("GET", f"{BASE_URL}/articles/{article_no}", expect_json=False,
                             headers={
                                 "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                 "referer": f"{BASE_URL}/complexes/{self._boot_complex_no}?tab=article",
                                 "sec-fetch-dest": "document",
                                 "sec-fetch-mode": "navigate",
                                 "sec-fetch-site": "same-origin",
                             })
        block = _TEL_BLOCK_RE.search(resp.text)
        if not block:
            return None
        tels = _TEL_LINK_RE.findall(block.group(0))
        return tels[0] if tels else ""

    def fill_office_tels(self, article_lists: Iterable[List[Dict[str, Any]]]) -> None:
        """수집이 끝난 뒤 업소전화를 채운다. 캐시에 없는 업소만, 회당 상한까지.

        전화는 부가 정보다. 여기서 무슨 일이 나도 예외를 밖으로 내보내지 않는다.
        못 채운 업소는 빈칸으로 두고 다음 실행에서 이어받는다.
        """
        articles = [a for lst in article_lists for a in lst]
        if not articles:
            return
        if not self.fetch_office_tel:
            logger.info("NAVER_FETCH_OFFICE_TEL=0 — 업소전화 수집을 건너뜁니다.")
            return

        # 캐시에 없는 업소 → 대표로 조회할 매물 1건
        todo: Dict[str, str] = {}
        for art in articles:
            key = art["_brokerKey"]
            if art["articleNo"] and key not in todo and self.tel_cache.get(key) is None:
                todo[key] = art["articleNo"]

        if not todo:
            logger.info("업소전화: 캐시로 전부 해결 (요청 0회)")
        elif self._rate_limited:
            logger.warning("이미 429 를 만나 업소전화 %d곳 수집을 건너뜁니다.", len(todo))
        else:
            targets = list(todo.items())[:self.tel_max_per_run]
            if len(todo) > self.tel_max_per_run:
                logger.info("업소전화 미확보 %d곳 중 %d곳만 받습니다 (회당 상한). "
                            "나머지는 다음 실행에서 이어받습니다.", len(todo), self.tel_max_per_run)
            else:
                logger.info("업소전화 %d곳 수집", len(targets))

            unparsed = 0
            for key, article_no in targets:
                self._sleep()
                try:
                    tel = self._fetch_office_tel(article_no)
                except NaverRateLimited:
                    # 회로차단: 창이 길어 계속 두드려봐야 손해다. 목록은 이미 확보됨.
                    logger.warning("429 — 업소전화 수집을 이 실행에서 중단합니다 "
                                   "(『매물장』 적재는 그대로 진행).")
                    break
                except NaverApiError as exc:
                    # 일시적 실패일 수 있으니 캐시에 남기지 않는다. 다음 실행에서 다시 시도.
                    logger.warning("업소전화 수집 실패(%s): %s", key.split("|")[-1] or "?", exc)
                    continue

                if tel is None:
                    unparsed += 1
                    continue
                self.tel_cache.put(key, tel)

            if unparsed:
                logger.warning("상세 페이지에서 『전화』 항목을 찾지 못한 매물 %d건 — "
                               "네이버가 페이지 구조를 바꿨는지 확인이 필요합니다.", unparsed)

        self.tel_cache.save()
        for art in articles:
            art["representativeTelNo"] = self.tel_cache.get(art["_brokerKey"]) or ""

        filled = sum(1 for a in articles if a["representativeTelNo"])
        logger.info("업소전화 채움: %d/%d건", filled, len(articles))
