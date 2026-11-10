# 감정평가서 구현 보고서

## 1. 실행 시 원본파일 형태 변화가 보이는 워크플로우

`python test.py`는 하드코딩된 원본 PDF
`/Users/yujin/Desktop/성현회계법인/담보물배분표/datas/감정평가서 휴먼중공업.pdf`를 읽고,
하드코딩된 출력 루트 `/Users/yujin/Desktop/성현회계법인/담보물배분표/output/appraisal` 아래에 다음 산출물을 만든다.

1. `gpt-5.4-mini`가 원본 감정평가서의 목차·페이지 이미지·문서 제목을 보고 명세표 물리 PDF 페이지를 판정한다.
2. 판정된 페이지 범위만 `pypdf`로 잘라 원본 104쪽 PDF가 아래 2개 파일로 분리된다.
   - `output/appraisal/감정평가서 휴먼중공업/토지·건물 감정평가명세표.pdf`
   - `output/appraisal/감정평가서 휴먼중공업/기계기구 감정평가명세표.pdf`
3. 같은 폴더에 `section_plan.json`이 저장되어 실제 절단 범위를 확인할 수 있다.
   - 검증 실행 결과: 토지·건물 29-31쪽, 기계기구 32-35쪽.
4. `gpt-5.6-sol`이 분리된 두 PDF와 고해상도 페이지 이미지를 읽고 최종 구조화 JSON을 만든다.
   - `output/appraisal/감정평가서 휴먼중공업.json`

따라서 실행 후에는 원본 단일 PDF가 “명세표 구간 PDF 2개 + 최종 JSON 1개”로 변환된 흐름을 파일 시스템에서 직접 볼 수 있다.

## 2. 구현 로직 및 아키텍처

- `parser.py`
  - `AppraisalSectionSplitter`: 1단계 LLM 페이지 판정기다. `gpt-5.4-mini`의 LangChain `with_structured_output(AppraisalSectionPlan, method="json_schema")`를 사용한다.
  - `AppraisalContentExtractor`: 2단계 LLM 내용 추출기다. `gpt-5.6-sol`의 LangChain-Pydantic 구조화 출력을 사용해 `LandBuildingExtraction`, `MachineryExtraction`을 생성한다.
  - `AppraisalParser`: 위 두 단계를 묶는 공개 API다. `invoke(source_path, output_root)` 한 번으로 PDF 분리와 JSON 저장을 수행한다.
  - PDF 페이지 이미지는 `PyMuPDF(fitz)`로 렌더링해 LLM 입력에 `detail=high` 이미지로 함께 제공한다. 이미지 기반 PDF에서도 LLM이 표를 직접 읽게 하기 위한 핵심 처리다.
  - fixture, LLM 없는 fallback, 샘플 데이터 대체 경로는 제거했다.

- `test.py`
  - 사용자가 요구한 단일 실행 진입점이다.
  - 경로와 모델명은 코드에 하드코딩했다.
  - 모델은 1단계 `gpt-5.4-mini`, 2단계 `gpt-5.6-sol`로 고정했다.
  - `report.md`는 실행 산출물이 아니므로 `test.py`에서 생성하지 않는다.

## 검증 결과

실행 명령:

```bash
python test.py
```

확인된 출력:

- 토지 그룹: 2건
- 건물 그룹: 2건
- 기계기구 품목: 9건
- `section_plan.json`: 토지·건물 29-31쪽, 기계기구 32-35쪽
- 토지·건물 그룹별 `items[].appraised_value` 합계와 `total_appraised_value` 일치 확인

