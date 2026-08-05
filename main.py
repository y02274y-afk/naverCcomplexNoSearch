"""
네이버 부동산 단지별 매물 수집 → 구글시트 『매물장』 적재 진입점 (배치 1)

수집은 naver_api_client(httpx 직접 호출)가 맡고, 출력은 구글시트 『매물장』 적재다.
  main → 『설정』 감시단지 읽기 → naver_api_client 수집 → field_mapper 변환 → sheets_loader 적재

2026-08-05: 수집 경로를 Playwright(naver_client)에서 API 직접 호출로 교체했다.
브라우저를 띄우지 않아 실행이 빠르고 결정적이다. 옛 Playwright 판은 네이버가 이
API 를 닫을 때를 대비해 naver_client.py 에 남겨뒀다(현재 미사용).
"""

import sys
import io
import time
import argparse
import logging
from collections import Counter
from datetime import datetime

# 윈도우 콘솔 인코딩 방지
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding="utf-8", errors="replace")

# sheets_loader 를 먼저 import 한다 — 이 모듈이 .env 를 환경변수로 올린다.
# naver_api_client 의 NAVER_* 설정도 그 환경변수를 읽으므로 순서가 앞뒤로 바뀌면 안 된다.
import sheets_loader
from sheets_loader import SheetError

from naver_api_client import NaverApiClient
import field_mapper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Batch1")


def collect_all(watch_list):
    """감시단지를 순회 수집한다.

    반환: results — 감시단지 순서대로 단지 1곳당 dict 1개
      {"complex_no","complex_name","articles","status","elapsed","message"}
      status: 성공 / 매물없음 / 실패
    단지 1곳 실패는 로그 남기고 계속한다 (§6).
    to_sheet_rows 는 complex_no/complex_name/articles 만 보므로 부가 키는 무해하다.

    업소전화는 전 단지 목록을 받은 **뒤에** 한꺼번에 채운다. 상세 페이지를 업소마다
    한 번씩 두드리는 부가 수집이라, 단지 사이에 끼워 넣으면 그쪽에서 429 를 맞았을 때
    아직 못 받은 단지의 목록까지 말려든다 (naver_api_client 모듈 주석 참조).
    """
    results = []

    with NaverApiClient() as client:
        for target in watch_list:
            cno = target["complex_no"]
            cname = target["complex_name"]
            print(f"\n▶ 수집 시작: {cname or '(이름미상)'} [{cno}]")
            t0 = time.monotonic()
            try:
                data = client.get_complex_data(cno)
                articles = data.get("articles", [])
                info = data.get("complex_info") or {}
                resolved_name = info.get("complexName") or cname
                elapsed = round(time.monotonic() - t0, 1)
                if articles:
                    print(f"  수집 완료: {len(articles)}개 매물 ({elapsed}초)")
                    status, message = "성공", f"{len(articles)}개 매물 수집"
                else:
                    print(f"  등록된 매물 없음 (단지 {cno})")
                    status, message = "매물없음", "네이버에 등록된 매물 없음"
                results.append({
                    "complex_no": cno,
                    "complex_name": resolved_name,
                    "articles": articles,
                    "status": status,
                    "elapsed": elapsed,
                    "message": message,
                })
            except Exception as e:
                elapsed = round(time.monotonic() - t0, 1)
                logger.error("단지 %s 수집 실패: %s", cno, e)
                results.append({
                    "complex_no": cno,
                    "complex_name": cname,
                    "articles": [],
                    "status": "실패",
                    "elapsed": elapsed,
                    # 예외 첫 줄만 — 셀이 스택트레이스로 뒤덮이지 않게
                    "message": str(e).splitlines()[0][:200] if str(e).strip() else type(e).__name__,
                })

        # 부가 수집. 실패해도 목록은 이미 손에 있으므로 배치를 멈추지 않는다.
        try:
            client.fill_office_tels(r["articles"] for r in results)
        except Exception as e:
            logger.warning("업소전화 수집 중 예외(무시하고 계속): %s", e)

    return results


def write_log(rows):
    """실행로그를 『실행로그』 탭에 기록한다. 실패해도 배치를 중단시키지 않는다.

    로그는 부가 기능이다. 시트 인증이 깨진 날은 기록할 수 없으므로
    로컬 파일 로그(logs\\batch1.log)가 최후의 진단 수단으로 남는다.
    """
    if not rows:
        return
    try:
        sheets_loader.append_log(rows)
    except Exception as e:  # SheetError 포함 — 로그 실패로 수집 결과를 버리지 않는다
        logger.warning("실행로그 기록 실패(무시하고 계속): %s", e)


def log_run_row(stamp, status, message):
    """단지 단위가 아닌 실행 전체 사건(중단·설정 오류)을 한 행으로 남긴다."""
    write_log([[stamp, "배치1", "", "(전체)", status, "", "", "", message]])


def build_log_rows(stamp, results, rows):
    """단지별 실행로그 행을 만든다. 적재행수는 중복 제거 후 실제 적재된 행 기준."""
    loaded = Counter(r[1] for r in rows)  # r[1] = 단지코드 (SHEET_HEADER 순서)
    return [
        [
            stamp,
            "배치1",
            r["complex_no"],
            r["complex_name"],
            r["status"],
            len(r["articles"]),
            loaded.get(str(r["complex_no"]), 0),
            r["elapsed"],
            r["message"],
        ]
        for r in results
    ]


def main():
    parser = argparse.ArgumentParser(description="네이버 매물 수집 → 구글시트 적재 (배치 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="시트에 적재하지 않고 변환 결과만 출력 (개발용)")
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 실행로그 실행시각 (실행 시작 기준)
    log_enabled = not args.dry_run  # dry-run 은 시트를 일절 건드리지 않는다

    print("\n=======================================================")
    print(" 배치 1 · 네이버 매물 수집 → 『매물장』 적재")
    if args.dry_run:
        print(" (DRY-RUN: 시트에 쓰지 않음)")
    print("=======================================================")

    # 1) 감시단지 읽기 — 실패 시 즉시 중단 (§6)
    try:
        watch_list = sheets_loader.read_watch_list()
    except SheetError as e:
        logger.error("설정 시트 읽기 실패, 중단합니다: %s", e)
        if log_enabled:
            log_run_row(stamp, "중단", f"설정 시트 읽기 실패: {e}")
        sys.exit(1)

    if not watch_list:
        logger.error("감시단지가 비어 있습니다. 수집할 대상이 없어 중단합니다.")
        if log_enabled:
            log_run_row(stamp, "중단", "감시단지가 비어 있음")
        sys.exit(1)

    print(f"\n감시단지 {len(watch_list)}곳:")
    for w in watch_list:
        print(f"  - {w['complex_name']} | {w['complex_no']}")

    # 2) 수집
    results = collect_all(watch_list)
    collected = [r for r in results if r["status"] != "실패"]
    failures = [r["complex_no"] for r in results if r["status"] == "실패"]

    # 3) 원자성 판정 (§3.3)
    total_articles = sum(len(c["articles"]) for c in collected)
    succeeded = len(collected)
    if succeeded == 0:
        # 전 단지 수집 실패 → 시트 미변경, 종료
        logger.error("전 단지 수집 실패. 시트를 변경하지 않고 종료합니다.")
        if log_enabled:
            write_log(build_log_rows(stamp, results, [])
                      + [[stamp, "배치1", "", "(전체)", "중단", "", "", "",
                          "전 단지 수집 실패 — 『매물장』 미변경"]])
        sys.exit(1)
    if failures:
        logger.warning("일부 단지 수집 실패(성공 단지만 적재): %s", ", ".join(failures))

    # 4) 변환 — 모든 행에 동일한 수집일시 (§3.2)
    run_at = datetime.now().isoformat(timespec="seconds")
    rows = field_mapper.to_sheet_rows(collected, run_at)

    print(f"\n[변환] 총 {len(rows)}행 (수집일시: {run_at})")
    if not rows:
        logger.error("변환 결과가 0행입니다. 시트를 변경하지 않고 종료합니다.")
        if log_enabled:
            write_log(build_log_rows(stamp, results, [])
                      + [[stamp, "배치1", "", "(전체)", "중단", "", "", "",
                          "변환 결과 0행 — 『매물장』 미변경"]])
        sys.exit(1)

    # 5) 적재 또는 dry-run 출력
    if args.dry_run:
        print("\n[DRY-RUN] 헤더:")
        print("  " + " | ".join(field_mapper.SHEET_HEADER))
        print("[DRY-RUN] 샘플(최대 5행):")
        for r in rows[:5]:
            print("  " + " | ".join(str(c) for c in r))
        print(f"\n[DRY-RUN] 시트 미변경. 적재 예정 {len(rows)}행.")
        return

    try:
        n = sheets_loader.replace_all(rows)
    except SheetError as e:
        logger.error("시트 적재 실패, 중단합니다: %s", e)
        # 수집은 됐으나 적재가 깨진 경우 — 단지별 결과에 적재행수 0으로 남긴다
        write_log(build_log_rows(stamp, results, [])
                  + [[stamp, "배치1", "", "(전체)", "실패", "", "", "",
                      f"『매물장』 적재 실패: {e}"]])
        sys.exit(1)

    # 6) 실행로그 — 단지별 1행 append (성공 경로)
    write_log(build_log_rows(stamp, results, rows))

    print("\n=======================================================")
    print(f" 적재 완료: 『매물장』 {n}행 (성공 단지 {succeeded}곳 / 총 {total_articles}매물)")
    print("=======================================================\n")


if __name__ == "__main__":
    main()
