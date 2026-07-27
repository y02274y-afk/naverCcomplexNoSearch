"""
네이버 부동산 단지별 매물 수집 → 구글시트 『매물장』 적재 진입점 (배치 1)

기존 수집 로직(naver_client)은 그대로 사용하고, 출력을 엑셀 → 구글시트 적재로 교체했다.
  main → 『설정』 감시단지 읽기 → naver_client 수집 → field_mapper 변환 → sheets_loader 적재
"""

import sys
import io
import argparse
import logging
from datetime import datetime

# 윈도우 콘솔 인코딩 방지
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding="utf-8", errors="replace")

from naver_client import NaverRealEstateClient
import field_mapper
import sheets_loader
from sheets_loader import SheetError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Batch1")


def collect_all(watch_list):
    """감시단지를 순회 수집한다.

    반환: (collected, failures)
      collected: [{"complex_no","complex_name","articles"}, ...]  (매물 1건 이상 성공한 단지)
      failures:  [단지코드, ...]  (수집 실패 또는 예외 단지)
    단지 1곳 실패는 로그 남기고 계속한다 (§6).
    """
    client = NaverRealEstateClient(headless=True)  # 헤드리스 — 브라우저 창을 띄우지 않는다 (§8.2)
    collected, failures = [], []

    for target in watch_list:
        cno = target["complex_no"]
        cname = target["complex_name"]
        print(f"\n▶ 수집 시작: {cname or '(이름미상)'} [{cno}]")
        try:
            data = client.get_complex_data(cno, max_scrolls=40)
            articles = data.get("articles", [])
            info = data.get("complex_info") or {}
            resolved_name = info.get("complexName") or cname
            if articles:
                collected.append({
                    "complex_no": cno,
                    "complex_name": resolved_name,
                    "articles": articles,
                })
                print(f"  수집 완료: {len(articles)}개 매물")
            else:
                print(f"  등록된 매물 없음 (단지 {cno})")
                collected.append({
                    "complex_no": cno,
                    "complex_name": resolved_name,
                    "articles": [],
                })
        except Exception as e:
            logger.error("단지 %s 수집 실패: %s", cno, e)
            failures.append(cno)

    return collected, failures


def main():
    parser = argparse.ArgumentParser(description="네이버 매물 수집 → 구글시트 적재 (배치 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="시트에 적재하지 않고 변환 결과만 출력 (개발용)")
    args = parser.parse_args()

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
        sys.exit(1)

    if not watch_list:
        logger.error("감시단지가 비어 있습니다. 수집할 대상이 없어 중단합니다.")
        sys.exit(1)

    print(f"\n감시단지 {len(watch_list)}곳:")
    for w in watch_list:
        print(f"  - {w['complex_name']} | {w['complex_no']}")

    # 2) 수집
    collected, failures = collect_all(watch_list)

    # 3) 원자성 판정 (§3.3)
    total_articles = sum(len(c["articles"]) for c in collected)
    succeeded = len(collected)
    if succeeded == 0:
        # 전 단지 수집 실패 → 시트 미변경, 종료
        logger.error("전 단지 수집 실패. 시트를 변경하지 않고 종료합니다.")
        sys.exit(1)
    if failures:
        logger.warning("일부 단지 수집 실패(성공 단지만 적재): %s", ", ".join(failures))

    # 4) 변환 — 모든 행에 동일한 수집일시 (§3.2)
    run_at = datetime.now().isoformat(timespec="seconds")
    rows = field_mapper.to_sheet_rows(collected, run_at)

    print(f"\n[변환] 총 {len(rows)}행 (수집일시: {run_at})")
    if not rows:
        logger.error("변환 결과가 0행입니다. 시트를 변경하지 않고 종료합니다.")
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
        sys.exit(1)

    print("\n=======================================================")
    print(f" 적재 완료: 『매물장』 {n}행 (성공 단지 {succeeded}곳 / 총 {total_articles}매물)")
    print("=======================================================\n")


if __name__ == "__main__":
    main()
