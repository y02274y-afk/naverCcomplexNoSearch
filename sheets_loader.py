"""
sheets_loader.py — 구글시트 적재 계층 (신규)

- 『설정』 시트의 `감시단지` 목록을 읽는다 (§4)
- 『매물장』 시트를 전량 교체 적재한다 (§3.1 · §3.3)
- 실행 결과를 『실행로그』에 단지별로 append 한다 (PRD 범위 외 · 운영 편의)

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
# 실행로그 탭 — 원본 빈 탭(『시트1』)을 『실행로그』로 개명해 쓴다. 탭 이름을 바꾸면 .env 값도 함께 바꿔야 한다.
LOG_SHEET = os.environ.get("LOG_SHEET", "실행로그")

# 『실행로그』 헤더 — 실행 1회당 감시단지 1행씩 append
LOG_HEADER = [
    "실행시각", "배치", "단지코드", "단지", "결과", "수집매물", "적재행수", "소요초", "메시지",
]


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
def _cell(row, idx):
    """행 리스트의 idx 칸을 안전하게 읽는다 (구글시트는 뒤쪽 빈 칸을 잘라서 준다)."""
    return row[idx].strip() if idx < len(row) else ""


def _read_watch_cells(grid):
    """『설정』 그리드에서 감시단지 값 셀들을 순서대로 뽑는다.

    `감시단지` 라벨 우측 셀부터 시작해, **라벨 칸이 빈 아래 행들을 계속 이어 읽는다.**
    한 단지당 한 행으로 쓰는 게 기본이고, 목록은 다음 중 먼저 오는 곳에서 끝난다.
      - 라벨 칸에 다른 항목명이 나온 행 (다음 설정 항목 시작)
      - 값 칸이 빈 행 (목록 끝)
    라벨을 못 찾으면 빈 리스트를 반환한다.
    """
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if cell.strip() != WATCH_LABEL:
                continue
            values = [_cell(row, c + 1)]
            for below in grid[r + 1:]:
                if _cell(below, c):        # 다른 설정 항목 — 감시단지 목록은 여기서 끝
                    break
                value = _cell(below, c + 1)
                if not value:              # 값이 빈 행 — 목록 끝
                    break
                values.append(value)
            return [v for v in values if v]
    return []


def read_watch_list():
    """『설정』 시트의 `감시단지` 목록을 파싱해 [{"complex_name","complex_no"}, ...] 반환.

    형식: `단지명|단지코드`. 단지 추가는 라벨 아래 행에 한 줄씩 쓰는 것이 기본이며,
    한 셀 안에서 줄바꿈·쉼표로 여러 개를 쓰는 예전 형식도 그대로 인정한다.
    형식이 잘못된 항목은 건너뛰고 로그를 남긴다.
    시트 읽기 실패 시 SheetError (호출부에서 즉시 중단 — §6).
    """
    sh = _open_spreadsheet()
    try:
        ws = sh.worksheet(CONFIG_SHEET)
        grid = ws.get_all_values()
    except Exception as e:
        raise SheetError(f"『{CONFIG_SHEET}』 시트 읽기 실패: {e}") from e

    cells = _read_watch_cells(grid)
    if not cells:
        raise SheetError(f"『{CONFIG_SHEET}』 시트에서 `{WATCH_LABEL}` 값을 찾지 못했습니다.")

    # 셀 하나에 줄바꿈·쉼표로 여러 개를 넣은 경우까지 펼친다 (구형 형식 호환)
    tokens = []
    for raw in cells:
        tokens += [t.strip() for t in raw.replace("\r", "\n").replace(",", "\n").split("\n")]

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


# ── 실행로그 append (운영 편의 · PRD 범위 외) ──────────────────────────────
def append_log(rows):
    """『실행로그』에 행을 append 한다. 기존 행은 지우지 않는다 (누적).

    rows: LOG_HEADER 순서의 2차원 리스트
    로그는 부가 기능이므로 호출부(main)에서 예외를 삼켜 배치 본체를 죽이지 않는다.
    """
    if not rows:
        return 0

    sh = _open_spreadsheet()
    try:
        ws = sh.worksheet(LOG_SHEET)
    except Exception as e:
        raise SheetError(f"『{LOG_SHEET}』 시트 열기 실패: {e}") from e

    end_col = rowcol_to_a1(1, len(LOG_HEADER)).rstrip("1")  # 예: 9 → "I"

    try:
        # 헤더 보증 (빈 탭 첫 실행 또는 헤더 훼손 시 재기록)
        if ws.row_values(1) != LOG_HEADER:
            ws.update(range_name=f"A1:{end_col}1", values=[LOG_HEADER],
                      value_input_option="RAW")
        ws.append_rows(rows, value_input_option="RAW", table_range="A1")
    except Exception as e:
        raise SheetError(f"『{LOG_SHEET}』 시트 쓰기 실패: {e}") from e

    logger.info("『%s』 실행로그 %d행 기록", LOG_SHEET, len(rows))
    return len(rows)
