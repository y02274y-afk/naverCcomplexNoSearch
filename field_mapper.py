"""
field_mapper.py — 수집 결과(naver_client 반환) → 『매물장』 시트 스키마 변환·정규화 (신규)

PRD 배치1 §2 스키마와의 계약을 지킨다. 컬럼명·순서를 임의로 바꾸지 않는다.
naver_client.py 가 돌려주는 원시 article dict 를 있는 그대로 받아 시트 행으로 변환한다.
"""

import re

# 『매물장』 헤더 — §2 스키마. 순서·이름 고정 (배치2와의 계약)
SHEET_HEADER = [
    "매물번호", "단지코드", "단지", "거래유형", "동", "층원문", "층대",
    "평형", "가격", "월세", "향", "중개업소", "업소전화", "묶음수",
    "매물링크", "수집일시",
]

# 층대 정규화 경계 (총층 대비 현재층 비율)
#   §2.2 프로세: "~1/3 저층 / ~2/3 중층 / 그 이상 고층"
#   §9 완료조건: "8/10(=0.8) 은 중층" — 이 예시를 충족하도록 중/고 경계를 0.8 로 둔다.
#   (프로세의 2/3 를 그대로 쓰면 8/10 이 고층이 되어 §9 검증에 실패하므로 §9 예시를 우선한다.)
_FLOOR_LOW = 1 / 3     # 이하 → 저층
_FLOOR_HIGH = 0.8      # 이하 → 중층, 초과 → 고층


def normalize_floor(floor_info):
    """원천 층 표기 → (층원문, 층대) 튜플.

    앞부분이 저/중/고 → 그대로 사용
    앞부분이 숫자      → 총층 대비 비율로 저층/중층/고층 환산
    판별 불가          → 층대 "" (원문은 그대로 보존)
    """
    raw = str(floor_info or "").strip()
    if not raw:
        return "", ""

    front = raw.split("/")[0].strip()

    # 층대 표기: "고/10", "저/12", "중/15"
    for key, label in (("저", "저층"), ("중", "중층"), ("고", "고층")):
        if front.startswith(key):
            return raw, label

    # 실층수 표기: "8/10", "11/11", "4/12"
    m = re.match(r"(-?\d+)\s*/\s*(\d+)", raw)
    if m:
        cur, total = int(m.group(1)), int(m.group(2))
        if total > 0:
            ratio = cur / total
            if ratio <= _FLOOR_LOW:
                return raw, "저층"
            elif ratio <= _FLOOR_HIGH:
                return raw, "중층"
            else:
                return raw, "고층"

    return raw, ""


def parse_price(text):
    """가격 문자열 → 만원 단위 정수.

    "17억 5,000" → 175000 · "7억" → 70000 · "1억" → 10000 · "5,000" → 5000
    빈 값/파싱 불가 → 0
    """
    if text is None:
        return 0
    s = str(text).strip().replace(",", "").replace(" ", "")
    if not s:
        return 0

    if "억" in s:
        eok_part, _, rest = s.partition("억")
        eok_digits = re.sub(r"[^0-9]", "", eok_part)
        rest_digits = re.sub(r"[^0-9]", "", rest)
        eok = int(eok_digits) if eok_digits else 0
        man = int(rest_digits) if rest_digits else 0
        return eok * 10000 + man

    digits = re.sub(r"[^0-9]", "", s)
    return int(digits) if digits else 0


def _clean_realtor_name(raw_name):
    """중개업소 상호명에서 전화번호 등 가공 텍스트 제거."""
    if not raw_name:
        return ""
    phone_pattern = r'(\d{2,3}[-)\s]?)?(\d{3,4}[-]\d{4})'
    return re.sub(phone_pattern, "", str(raw_name)).strip()


def _to_int(val, default=0):
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _map_article(art, complex_no, complex_name, run_at):
    """원시 article dict 한 건 → SHEET_HEADER 순서의 리스트 한 행."""
    art_no = art.get("articleNo", "")

    floor_raw, floor_band = normalize_floor(art.get("floorInfo", ""))

    # 평형: 공급면적 ㎡ 정수 (예: 112). 텍스트로 기록.
    area1 = art.get("area1", art.get("spc1", ""))
    pyeong = str(_to_int(area1, "")) if _to_int(area1, "") != "" else ""

    trade = art.get("tradeTypeName", "")

    # 가격: 매매가 또는 보증금
    price = parse_price(art.get("dealOrWarrantPrc", ""))

    # 월세: 매매·전세는 0 (§2 스키마, §9 완료조건)
    if trade in ("매매", "전세"):
        wolse = 0
    else:
        wolse = parse_price(art.get("rentPrc", ""))

    # 중개업소: 상호명만. 개인정보(휴대폰·대표자명·주소)는 §2.1 에 따라 제외.
    realtor = _clean_realtor_name(art.get("exactRealtorName") or art.get("realtorName", ""))
    # 업소전화: 업소 대표전화만. cellPhoneNo(개인 휴대폰) 폴백 금지 (§2.1)
    office_tel = art.get("representativeTelNo", "") or ""

    link = (
        f"https://new.land.naver.com/complexes/{complex_no}?articleNo={art_no}"
        if art_no else ""
    )

    return [
        str(art_no),                                        # 매물번호
        str(complex_no),                                    # 단지코드
        art.get("articleName") or complex_name or "",       # 단지
        trade,                                              # 거래유형
        art.get("buildingName", "") or "",                  # 동
        floor_raw,                                          # 층원문
        floor_band,                                         # 층대
        pyeong,                                             # 평형
        price,                                              # 가격 (number)
        wolse,                                              # 월세 (number)
        art.get("direction", "") or "",                     # 향
        realtor,                                            # 중개업소
        office_tel,                                         # 업소전화
        _to_int(art.get("sameAddrCnt", 1), 1),              # 묶음수 (number)
        link,                                               # 매물링크
        run_at,                                             # 수집일시
    ]


def to_sheet_rows(collected, run_at):
    """수집 결과를 『매물장』 데이터 행 리스트로 변환한다.

    collected: [{"complex_no": str, "complex_name": str, "articles": [dict, ...]}, ...]
    run_at:    모든 행에 동일하게 기록할 ISO 시각 문자열 (§3.2 완료 신호)
    반환:      각 행이 SHEET_HEADER 순서인 2차원 리스트 (헤더 제외)
    """
    rows = []
    seen = set()  # 매물번호 기준 중복 제거 (단지 간 중복 방어)
    for entry in collected:
        cno = entry.get("complex_no", "")
        cname = entry.get("complex_name", "")
        for art in entry.get("articles", []):
            art_no = str(art.get("articleNo", ""))
            if art_no and art_no in seen:
                continue
            if art_no:
                seen.add(art_no)
            rows.append(_map_article(art, cno, cname, run_at))
    return rows
