# 배치 1 · 네이버 매물 수집 → 구글시트 『매물장』 적재

관심 단지의 매물을 수집해 구글시트 『매물장』에 전량 교체 적재하는 배치.
(분석·알림은 배치 2가 담당하며, 두 배치는 구글시트로만 이어진다.)

## 설치

```bash
pip install -r requirements.txt
playwright install chromium     # ★ 이 단계를 빠뜨리면 수집이 시작되지 않는다
```

> `pip install`만으로는 동작하지 않는다. 브라우저 바이너리를 받는 `playwright install chromium`이 반드시 필요하다.

## 설정

`.env` 파일에 구글시트 연동 정보를 둔다 (저장소에 커밋하지 않는다).

```
GSPREAD_KEY_FILE=navercomplexnosearch-273fa9c1af10.json   # 서비스 계정 키파일 경로
SPREADSHEET_ID=...                                        # 대상 스프레드시트 ID (배치 2와 동일 문서)
CONFIG_SHEET=설정
MAEMULJANG_SHEET=매물장
WATCH_LABEL=감시단지
```

- 서비스 계정 이메일을 대상 시트에 **편집자로 공유**해야 한다.
- 수집 대상은 『설정』 시트의 `감시단지` 값으로 정한다. 형식: `단지명|단지코드` (줄바꿈 또는 쉼표로 복수 등록).

## 실행

```bash
python main.py            # 수집 → 『매물장』 전량 교체 적재
python main.py --dry-run  # 시트에 쓰지 않고 변환 결과만 출력 (개발용)
```

윈도우 작업 스케줄러에 등록한다 (기본 08:00). 배치 2보다 **먼저** 실행되어야 한다.

## 사용자 주의사항

- 본 배치가 **먼저** 실행되어야 한다. 수집이 돌지 않은 날은 알림이 나가지 않는다.
- 로컬 실행이므로 **PC가 켜져 있어야 한다.**
- 『매물장』 시트를 직접 편집하지 않는다. 매 실행 시 전량 교체된다.
- 주기를 바꿀 때는 **알림 배치와 함께 조정한다.** 수집만 늘려도 알림은 하루 한 번이다.
- 인증 키 파일(`*.json`)과 `.env`가 저장소에 포함되지 않도록 주의한다.
- 설치 시 `playwright install chromium`을 반드시 실행한다.
- 수집에 걸리는 시간을 한 번 재보고 알림 배치 시각을 조정한다. (기본 간격 30분)

## 파일 구조

```
main.py             진입점 — 수집 호출 유지, 출력부를 시트 적재로 교체
naver_client.py     🔒 네이버 매물 수집 (수정 금지)
field_mapper.py     수집 결과 → 『매물장』 스키마 변환·정규화
sheets_loader.py    구글시트 인증·감시단지 읽기·전량 교체 적재
.env                인증/시트 설정 (gitignore)
requirements.txt
```
