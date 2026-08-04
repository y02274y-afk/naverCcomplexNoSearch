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

# 『설정』 감시단지 표의 헤더 — 『매물장』·『실행로그』와 같이 헤더 행을 쓴다.
# 열 순서를 바꿔도 헤더 이름으로 찾으므로 안전하다. 헤더가 없으면 구형(라벨) 방식으로 폴백한다.
WATCH_HEADER = ["감시단지", "네이버단지코드", "법정동코드", "관리여부"]
WATCH_COL_NAME, WATCH_COL_NO, WATCH_COL_CORTAR, WATCH_COL_STATUS = WATCH_HEADER

# 구형(헤더 없는 라벨) 배치의 칸 위치 — 라벨 칸을 0 으로 본 상대 오프셋.
#   A 라벨 | B 단지명|단지코드 | C 법정동코드 | D 상태
WATCH_OFF_VALUE = 1
WATCH_OFF_CORTAR = 2
WATCH_OFF_STATUS = 3
# 상태 칸이 이 값이면 그 단지를 수집에서 제외한다.
# 칸이 없거나 비어 있으면 `관리`로 간주 — 상태 열을 만들기 전 시트와도 그대로 호환된다.
WATCH_STATUS_EXCLUDE = os.environ.get("WATCH_STATUS_EXCLUDE", "삭제")
WATCH_STATUS_VALUES = ("관리", WATCH_STATUS_EXCLUDE)
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


def _parse_watch_row(row, c):
    """감시단지 한 행 → (값, 법정동코드, 상태).

    법정동코드 열을 만들기 전 시트(`... | 단지명|코드 | 상태`)와도 호환된다.
    법정동코드 자리에 상태값(`관리`/`삭제`)이 그대로 있으면 구형 배치로 보고 한 칸 당겨 읽는다.
    이 보정이 없으면 구형 시트에서 `삭제`가 법정동코드로 읽혀 **조용히 무시**된다.
    """
    value = _cell(row, c + WATCH_OFF_VALUE)
    cortar = _cell(row, c + WATCH_OFF_CORTAR)
    status = _cell(row, c + WATCH_OFF_STATUS)
    if cortar in WATCH_STATUS_VALUES and not status:
        logger.warning("『%s』이 구형 배치입니다(법정동코드 열 없음). 상태를 %r 로 읽습니다.",
                       CONFIG_SHEET, cortar)
        return value, "", cortar
    return value, cortar, status


def _find_header(grid):
    """헤더 행을 찾아 (행번호, {헤더명: 열번호}) 반환. 없으면 (None, {}).

    `감시단지` 와 `네이버단지코드` 가 같은 행에 있으면 헤더 행으로 본다.
    (`감시단지` 만으로 판단하면 구형 라벨 행을 헤더로 오인한다.)
    """
    for r, row in enumerate(grid):
        cells = [c.strip() for c in row]
        if WATCH_COL_NAME in cells and WATCH_COL_NO in cells:
            return r, {name: cells.index(name) for name in WATCH_HEADER if name in cells}
    return None, {}


def _read_watch_cells(grid):
    """『설정』에서 감시단지 (단지명, 단지코드, 법정동코드, 상태) 튜플을 순서대로 뽑는다.

    헤더 행이 있으면 헤더 이름으로 열을 찾고, 그 아래 행을 이름·코드가 모두 빌 때까지 읽는다.
    헤더가 없으면 구형(라벨 + `단지명|단지코드`) 배치로 폴백한다.
    """
    hr, cols = _find_header(grid)
    if hr is None:
        return _read_watch_cells_legacy(grid)

    ci_name, ci_no = cols.get(WATCH_COL_NAME), cols.get(WATCH_COL_NO)
    ci_cortar, ci_status = cols.get(WATCH_COL_CORTAR), cols.get(WATCH_COL_STATUS)
    out = []
    for row in grid[hr + 1:]:
        name = _cell(row, ci_name) if ci_name is not None else ""
        code = _cell(row, ci_no) if ci_no is not None else ""
        if not name and not code:
            break                      # 표 끝
        # 이름 칸에 `단지명|단지코드` 를 그대로 붙여 넣은 경우도 받아준다.
        # 단지코드는 항상 마지막 조각이므로 rpartition — 단지명에 `|` 가 있어도 안전하다.
        if not code and "|" in name:
            name, _, code = name.rpartition("|")
            name, code = name.strip(), code.strip()
        out.append((
            name, code,
            _cell(row, ci_cortar) if ci_cortar is not None else "",
            _cell(row, ci_status) if ci_status is not None else "",
        ))
    return out


def _read_watch_cells_legacy(grid):
    """구형 배치(헤더 없음) — `감시단지` 라벨 우측 셀부터 아래로 이어 읽는다.

    한 단지당 한 행이 기본이고, 목록은 다음 중 먼저 오는 곳에서 끝난다.
      - 라벨 칸에 다른 항목명이 나온 행 (다음 설정 항목 시작)
      - 값 칸이 빈 행 (목록 끝)
    값 칸은 `단지명|단지코드` 형식이므로 여기서 분리한다.
    """
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if cell.strip() != WATCH_LABEL:
                continue
            rows = [_parse_watch_row(row, c)]
            for below in grid[r + 1:]:
                if _cell(below, c):        # 다른 설정 항목 — 감시단지 목록은 여기서 끝
                    break
                if not _cell(below, c + WATCH_OFF_VALUE):   # 값이 빈 행 — 목록 끝
                    break
                rows.append(_parse_watch_row(below, c))
            out = []
            for value, cortar, status in rows:
                if not value:
                    continue
                # 구형은 한 셀에 줄바꿈·쉼표로 여러 건을 넣기도 한다 — 여기서 펼친다
                for token in value.replace("\r", "\n").replace(",", "\n").split("\n"):
                    token = token.strip()
                    if not token:
                        continue
                    # 단지코드는 마지막 조각 — 단지명에 `|` 가 있어도 안전하다.
                    # 파이프가 없으면 rpartition 이 값을 코드 자리로 넣어버리므로 반드시 가른다.
                    if "|" in token:
                        name, _, code = token.rpartition("|")
                    else:
                        name, code = token, ""
                    out.append((name.strip(), code.strip(), cortar, status))
            return out
    return []


def read_watch_list():
    """『설정』 시트의 `감시단지` 목록을 파싱해
    [{"complex_name","complex_no","cortar_no"}, ...] 반환.

    표는 헤더 행(`감시단지`/`네이버단지코드`/`법정동코드`/`관리여부`)을 기준으로 읽는다.
    헤더가 없는 구형 시트(라벨 + `단지명|단지코드`, 한 셀 다건 포함)도 그대로 인정한다.
    `관리여부` 가 `삭제` 인 행은 목록에서 제외한다 (행을 지우지 않고 감시만 끈다).
    `cortar_no` 는 법정동코드 10자리(빈 문자열 가능) — 공공데이터 연계용 캐시값이며
    수집 자체에는 쓰이지 않는다. 형식이 잘못된 항목은 건너뛰고 로그를 남긴다.
    시트 읽기 실패 시 SheetError (호출부에서 즉시 중단 — §6).
    """
    sh = _open_spreadsheet()
    try:
        ws = sh.worksheet(CONFIG_SHEET)
        grid = ws.get_all_values()
    except Exception as e:
        raise SheetError(f"『{CONFIG_SHEET}』 시트 읽기 실패: {e}") from e

    rows = _read_watch_cells(grid)
    if not rows:
        raise SheetError(f"『{CONFIG_SHEET}』 시트에서 `{WATCH_LABEL}` 목록을 찾지 못했습니다.")

    watch = []
    excluded = 0
    for name, code, cortar, status in rows:
        if status.strip() == WATCH_STATUS_EXCLUDE:
            excluded += 1
            continue
        if not code:
            logger.warning("감시단지 네이버단지코드 누락, 건너뜀: %r", name or "(이름없음)")
            continue
        # 네이버 단지코드는 숫자다. 숫자가 아니면 이름/코드 분리가 어긋났을 가능성이 크다.
        if not code.isdigit():
            logger.warning("네이버단지코드가 숫자가 아닙니다(이름·코드 분리 확인 필요): "
                           "단지명=%r 코드=%r", name, code)
        cortar = cortar.strip()
        if cortar and not (cortar.isdigit() and len(cortar) == 10):
            logger.warning("법정동코드 형식이 이상합니다(10자리 숫자 아님): %r", cortar)
        watch.append({"complex_name": name, "complex_no": code, "cortar_no": cortar})

    if excluded:
        logger.info("감시단지 %d행이 `%s` 상태로 제외되었습니다.", excluded, WATCH_STATUS_EXCLUDE)

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
