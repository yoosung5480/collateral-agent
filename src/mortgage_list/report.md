# 공장저당법목록 2단계 LLM 추출 프레임워크 보고서

## 실행 요약

- 입력 PDF: `/Users/yujin/Desktop/성현회계법인/담보물배분표/datas/공장저당법목록.pdf`
- 1단계 모델: `gpt-5.4-mini` — 문서 구조 OCR 및 목록번호별 물리 페이지 범위 판정
- 2단계 모델: `gpt-5.6-sol` — 분할 PDF별 `detail=high` 고성능 구조화 추출
- 분할 산출물 폴더: `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/mortgage_list/공장저당법목록`
- 분할 계획 JSON: `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/mortgage_list/공장저당법목록/split_plan.json`
- 최종 JSON: `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/mortgage_list/공장저당법목록.json`
- 목록번호 수: 6건
- 현재 유효 기계기구 품목 수: 41건
- 경고 수: 15건
- LLM 없는 대체 실행 사용: 없음. LLM 객체가 없으면 실행이 실패하도록 설계했다.

## 데이터 흐름도

```text
공장저당법목록.pdf
  │
  ├─[1단계 gpt-5.4-mini]
  │   PDF 원본 + 빨간 PDF PAGE contact sheet 확인
  │   → MortgageListSplitPlan(slices: 목록번호, start_page, end_page, evidence)
  │
  ├─[pypdf]
  │   원본 PDF를 slices 기준으로 목록번호별 소형 PDF로 분할
  │   → output/mortgage_list/공장저당법목록/*.pdf
  │
  ├─[2단계 gpt-5.6-sol 병렬]
  │   각 소형 PDF를 detail=high로 읽고 목록 변경내역표와 기계기구목록을 함께 판단
  │   → MortgageListGroup[]
  │
  └─[Pydantic 검증/직렬화]
      MortgageAssetsReport
      → output/mortgage_list/공장저당법목록.json
      → 호환용 output/mortgage_list/공장저당법목록/mortgage_assets.json
```

## 아키텍처

- `MortgageListSplitter`: 문서 전체를 다루는 전처리 계층이다. LLM에는 원본 PDF와 페이지 라벨이 찍힌 contact sheet를 함께 보내 물리 페이지 번호 혼동을 줄인다.
- `MortgageListSplitPlan`: Pydantic 스키마로 목록번호 중복, 페이지 양수, 시작/끝 역전, 원본 등장순 오름차순을 검증한다.
- `write_page_range`: `pypdf`로 원본 PDF에서 각 목록번호 묶음만 잘라낸다. 이 단계는 LLM 결과를 실행 가능한 파일 산출물로 바꾸는 순수 I/O 계층이다.
- `MortgageListContentExtractor`: 분할 PDF 하나를 하나의 목록번호 단위로 추출한다. `with_structured_output(..., method="json_schema")`를 사용해 LangChain 응답을 Pydantic 모델로 직접 검증한다.
- `MortgageParser`: split → async 병렬 extract → 최종 JSON 저장을 오케스트레이션한다. LLM 없는 대체 실행이나 환경변수 기반 모델 선택을 넣지 않았다.

## 구현 로직

1. `gpt-5.4-mini`가 전체 PDF에서 `목록 변경내역표 - 간지 - 목록 표지 - 기계기구목록` 반복 구조를 찾는다.
2. 각 반복 묶음은 `공장 및 광업재단 저당법 제6조 목록 제YYYY-N호`로 정규화한다.
3. 분할 계획의 페이지 범위가 원본 총 페이지 수를 넘으면 즉시 실패한다.
4. 목록번호별 PDF 파일명은 목록번호와 페이지 범위를 포함해 추적 가능하게 만든다.
5. `gpt-5.6-sol`은 각 분할 PDF에서 변경내역표와 본문을 함께 읽어 현재 유효 품목만 `machinery_items`에 둔다.
6. 말소·삭제·변경 단서는 `change_history` 또는 `warnings`로 분리해 후속 엑셀 매칭 데이터와 섞이지 않게 했다.
7. 최종 결과는 README 지정 경로의 `공장저당법목록.json`에 저장하고, 기존 병합 코드가 읽을 수 있도록 분할 폴더 안에 `mortgage_assets.json`도 같은 내용으로 저장한다.

## 분할 PDF

- `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/mortgage_list/공장저당법목록/01_목록_2012-399_p11-17.pdf`
- `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/mortgage_list/공장저당법목록/02_목록_2012-514_p18-29.pdf`
- `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/mortgage_list/공장저당법목록/03_목록_2012-515_p30-37.pdf`
- `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/mortgage_list/공장저당법목록/04_목록_2012-720_p38-44.pdf`
- `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/mortgage_list/공장저당법목록/05_목록_2017-309_p45-51.pdf`
- `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/mortgage_list/공장저당법목록/06_목록_2018-234_p52-58.pdf`

## 목록별 추출 결과

- 공장 및 광업재단 저당법 제6조 목록 제2012-399호: 품목 3건, 변경내역 2건, 경고 3건
- 공장 및 광업재단 저당법 제6조 목록 제2012-514호: 품목 6건, 변경내역 3건, 경고 2건
- 공장 및 광업재단 저당법 제6조 목록 제2012-515호: 품목 3건, 변경내역 2건, 경고 2건
- 공장 및 광업재단 저당법 제6조 목록 제2012-720호: 품목 11건, 변경내역 1건, 경고 2건
- 공장 및 광업재단 저당법 제6조 목록 제2017-309호: 품목 9건, 변경내역 1건, 경고 3건
- 공장 및 광업재단 저당법 제6조 목록 제2018-234호: 품목 9건, 변경내역 1건, 경고 3건

## 검증 관점

- `python -m py_compile parser.py test.py`로 문법 검증.
- `python test.py`는 실제 LLM 호출 없이 성공할 수 없다. API 키 또는 모델 호출 실패 시 전체 실행이 실패한다.
- 방어적 대체 실행 경로를 제거했다.
