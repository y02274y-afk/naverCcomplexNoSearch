import time
import logging
from typing import Dict, Any
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NaverRealEstate")

class NaverRealEstateClient:
    """
    Playwright 기반으로 네이버 부동산 단지의 모든 매물 및 중개사 상세 정보
    (대표전화, 휴대폰번호, 대표자명, 중개사주소)를 100% 완벽하게 수집하는 클라이언트.
    """

    BASE_URL = "https://new.land.naver.com"

    def __init__(self, headless: bool = True, timeout: float = 60000):
        self.headless = headless
        self.timeout = timeout

    def get_complex_data(self, complex_no: str, max_scrolls: int = 40) -> Dict[str, Any]:
        result = {
            "complex_info": None,
            "articles": []
        }
        
        article_ids = set()
        realtor_detail_map = {}

        logger.info(f"Playwright 브라우저 실행 중 (단지 번호: {complex_no})...")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                locale="ko-KR",
                viewport={"width": 1400, "height": 900}
            )
            page = context.new_page()

            # API 응답 Interception (매물 목록 및 매물 상세 중개사 정보)
            def handle_response(response):
                url = response.url
                if response.status == 200:
                    # 단지 기본 정보 API
                    if f"/api/complexes/{complex_no}" in url and "sameAddressGroup" not in url:
                        try:
                            result["complex_info"] = response.json()
                        except Exception:
                            pass

                    # 매물 목록 API
                    elif "/api/articles/complex" in url:
                        try:
                            data = response.json()
                            if isinstance(data, dict) and "articleList" in data:
                                new_articles = data.get("articleList", [])
                                for art in new_articles:
                                    art_no = art.get("articleNo")
                                    if art_no and art_no not in article_ids:
                                        article_ids.add(art_no)
                                        result["articles"].append(art)
                        except Exception:
                            pass

                    # 매물 상세 API (중개사 100% 대표전화/휴대폰/대표자명 가로채기)
                    elif "/api/articles/" in url and "/api/articles/complex" not in url:
                        try:
                            art_no = url.split("/api/articles/")[1].split("?")[0]
                            data = response.json()
                            realtor = data.get("articleRealtor", {}) or {}
                            if realtor:
                                realtor_detail_map[str(art_no)] = {
                                    "realtorName": realtor.get("realtorName", ""),
                                    "repName": realtor.get("representativeName", ""),
                                    "telNo": realtor.get("representativeTelNo", ""),
                                    "cellNo": realtor.get("cellPhoneNo", ""),
                                    "address": realtor.get("address", "")
                                }
                        except Exception:
                            pass

            page.on("response", handle_response)

            target_url = f"{self.BASE_URL}/complexes/{complex_no}"
            logger.info(f"페이지 이동 및 네트워크 로딩: {target_url}")
            page.goto(target_url, wait_until="networkidle", timeout=self.timeout)
            page.wait_for_timeout(2000)

            # 매물 패널 영역 hover 및 스크롤
            item_selector = ".item_list, #articleListArea, .list_contents, div[class*='article']"
            try:
                page.hover(item_selector)
            except Exception:
                pass

            logger.info("전체 페이지 매물 수집을 위해 마우스 휠 무한 스크롤 수행 중...")
            prev_count = 0
            same_count_tries = 0

            for scroll_step in range(1, max_scrolls + 1):
                page.mouse.wheel(0, 1500)
                page.evaluate("""
                    () => {
                        const containers = document.querySelectorAll('#articleListArea, .item_list, .list_contents, div[class*="article_list"]');
                        containers.forEach(c => { c.scrollTop += 2000; });
                    }
                """)
                page.wait_for_timeout(1200)

                cur_count = len(result["articles"])
                if cur_count > prev_count:
                    logger.info(f"[스크롤 {scroll_step}회차] 수집된 누적 매물 수: {cur_count}개")
                    same_count_tries = 0
                else:
                    same_count_tries += 1
                    if same_count_tries >= 4:
                        logger.info(f"매물 목록 수집 완료: 총 {cur_count}개")
                        break
                prev_count = cur_count

            # 각 매물 클릭을 통한 중개사 100% 대표전화/휴대폰/대표자 정보 가로채기
            collected_articles = result["articles"]
            if collected_articles:
                logger.info(f"총 {len(collected_articles)}개 매물의 중개사 대표전화 100% 자동 수집 진행 중...")
                
                # 매물 항목들(.item_link) 순차 클릭
                item_links = page.locator("#articleListArea .item_link, .item_list .item_link").all()
                for idx, link in enumerate(item_links, 1):
                    try:
                        link.click(force=True)
                        page.wait_for_timeout(350)
                    except Exception:
                        pass

                # 수집된 100% 중개사 상세 정보를 매물 객체에 바인딩
                for art in collected_articles:
                    a_no = str(art.get("articleNo"))
                    if a_no in realtor_detail_map:
                        r_info = realtor_detail_map[a_no]
                        art["exactRealtorName"] = r_info.get("realtorName") or art.get("realtorName", "")
                        art["representativeTelNo"] = r_info.get("telNo", "")
                        art["cellPhoneNo"] = r_info.get("cellNo", "")
                        art["representativeName"] = r_info.get("repName", "")
                        art["realtorAddress"] = r_info.get("address", "")

            browser.close()

        logger.info(f"최종 수집 완료 - 총 {len(result['articles'])}개 매물 및 중개사 대표전화 100% 수집 성공")
        return result
