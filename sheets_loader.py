"""
sheets_loader.py — 구글시트 적재 계층 (신규)

- 『설정』 시트의 `감시단지` 목록을 읽는다 (§4)
- 『매물장』 시트를 전량 교체 적재한다 (§3.1 · §3.3)

인증: 구글 서비스 계정 키파일 (.env / 환경변수로 경로 지정, 소스 하드코딩 금지 — §5)
"""

import os
import logging

import gspread
from gspread.utils import rowcol_to_a1

from field_mapper import SHEET_HEADER

logger = logging.getLogger("SheetsLoader")


# ── 설정 로드 (.env → 환경변수) ────────────────────────────────────────────
def _load_dotenv(path=".env"):
    """의존성 없이 .env 를 환경변수로 로드 (이미 설정된 값은 덮어쓰지 않음)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()

KEY_FILE = os.environ.get("GSPREAD_KEY_FILE", "navercomplexnosearch-273fa9c1af10.json")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "네이버_단지별_부동단_매물")
CONFIG_SHEET = os.environ.get("CONFIG_SHEET", "설정")
MAEMULJANG_SHEET = os.environ.get("MAEMULJANG_SHEET", "매물장")
WATCH_LABEL = os.environ.get("WATCH_LABEL", "감시단지")


class SheetError(Exception):
    """시트 인증/읽기/쓰기 실패. 조용히 넘어가지 않고 상위에서 명시적으로 처리한다 (§6)."""


# ── 인증 & 문서 열기 ───────────────────────────────────────────────────────
def _open_spreadsheet():
    if not os.path.exists(KEY_FILE):
        raise SheetError(f"서비스 계정 키파일을 찾을 수 없습니다: {KEY_FILE}")
    try:
        gc = gspread.service_account(filename=KEY_FILE)
    except Exception as e:
        raise SheetError(f"구글 인증 실패: {e}") from e
    try:
        if SPREADSHEET_ID:
            return gc.open_by_key(SPREADSHEET_ID)
        return gc.open(SPREADSHEET_NAME)
    except Exception as e:
        raise SheetError(f"스프레드시트 열기 실패: {e}") from e


# ── 감시단지 읽기 (§4) ─────────────────────────────────────────────────────
def read_watch_list():
    """『설정』 시트의 `감시단지` 값을 파싱해 [{"complex_name","complex_no"}, ...] 반환.

    형식: `단지명|단지코드`, 줄바꿈 또는 쉼표로 복수 등록.
    형식이 잘못된 행은 건너뛰고 로그를 남긴다.
    시트 읽기 실패 시 SheetError (호출부에서 즉시 중단 — §6).
    """
    sh = _open_spreadsheet()
    try:
        ws = sh.worksheet(CONFIG_SHEET)
        grid = ws.get_all_values()
    except Exception as e:
        raise SheetError(f"『{CONFIG_SHEET}』 시트 읽기 실패: {e}") from e

    # "감시단지" 라벨 셀을 찾아 우측 셀의 값을 읽는다.
    raw_value = ""
    for row in grid:
        for i, cell in enumerate(row):
            if cell.strip() == WATCH_LABEL:
                if i + 1 < len(row):
                    raw_value = row[i + 1]
                break
        if raw_value:
            break

    if not raw_value.strip():
        raise SheetError(f"『{CONFIG_SHEET}』 시트에서 `{WATCH_LABEL}` 값을 찾지 못했습니다.")

    # 줄바꿈·쉼표로 분리
    tokens = [t.strip() for t in raw_value.replace("\r", "\n").replace(",", "\n").split("\n")]
    watch = []
    for token in tokens:
        if not token:
            continue
        if "|" not in token:
            logger.warning("감시단지 형식 오류(| 없음), 건너뜀: %r", token)
            continue
        name, _, code = token.partition("|")
        name, code = name.strip(), code.strip()
        if not code:
            logger.warning("감시단지 단지코드 누락, 건너뜀: %r", token)
            continue
        watch.append({"complex_name": name, "complex_no": code})

    return watch


# ── 전량 교체 적재 (§3.1 · §3.3) ───────────────────────────────────────────
def replace_all(rows):
    """『매물장』 데이터 행을 전량 교체한다. 헤더 행은 유지·검증한다.

    rows: field_mapper.to_sheet_rows() 결과 (2차원 리스트, 헤더 제외)
    """
    if not rows:
        # 빈 적재로 시트를 비우지 않는다. 호출부(main)에서 전 단지 실패를 이미 걸렀어야 한다.
        raise SheetError("적재할 행이 없습니다. 시트를 변경하지 않습니다.")

    sh = _open_spreadsheet()
    try:
        ws = sh.worksheet(MAEMULJANG_SHEET)
    except Exception as e:
        raise SheetError(f"『{MAEMULJANG_SHEET}』 시트 열기 실패: {e}") from e

    ncols = len(SHEET_HEADER)
    end_col = rowcol_to_a1(1, ncols).rstrip("1")  # 예: 16 → "P"

    try:
        # 1) 헤더 보증 (없거나 다르면 재기록)
        current_header = ws.row_values(1)
        if current_header != SHEET_HEADER:
            ws.update(range_name=f"A1:{end_col}1", values=[SHEET_HEADER],
                      value_input_option="RAW")

        # 2) 기존 데이터 행 삭제 (헤더 유지)
        last_row = len(ws.get_all_values())
        if last_row > 1:
            ws.batch_clear([f"A2:{end_col}{last_row}"])

        # 3) 신규 데이터 단일 배치 삽입 (부분 적재 창을 최소화 — §3.3)
        ws.update(range_name="A2", values=rows, value_input_option="RAW")
    except SheetError:
        raise
    except Exception as e:
        raise SheetError(f"『{MAEMULJANG_SHEET}』 시트 쓰기 실패: {e}") from e

    logger.info("『%s』 전량 교체 완료: %d행 적재", MAEMULJANG_SHEET, len(rows))
    return len(rows)
