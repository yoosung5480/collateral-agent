# 담보물배분표 엑셀 자동 반환기 구현 보고서

## 1. 업무 워크플로우

`python main.py`는 README의 세 입력 PDF를 하드코딩 경로에서 읽어 다음 순서로 처리한다.

1. `registry` 프레임워크로 `부동산등기부등본.pdf`를 등기부별 표제부/을구 PDF로 분할하고 `title.json`, `encumbrance.json`을 LLM 추출한다.
2. `appraisal` 프레임워크로 `감정평가서 휴먼중공업.pdf`를 토지·건물/기계기구 명세표로 분할하고 통합 감정평가 JSON을 LLM 추출한다.
3. `mortgage_list` 프레임워크로 `공장저당법목록.pdf`를 목록번호별로 분할하고 저당목록 JSON을 LLM 추출한다.
4. `pipeline.py`가 `run_manifest.json`을 만들고, 명시된 JSON만 사용해 단계별 산출물을 생성한다.
5. 부동산 의미 대조와 기계기구-저당목록 대조는 LLM이 ID 관계만 판단하고, 금액 합계·순위·O/X 변환·Excel 좌표는 코드가 결정한다.
6. 검증된 `workbook_plan.json`만 `excel.write_collateral_workbook()`에 전달하여 `감정평가서 휴먼중공업/담보물배분표.xlsx`를 저장한다.

## 2. 아키텍처

- `main.py`: 실행 진입점. 모델명, 루트 경로, 입력 PDF, 출력 경로를 최상위 전역 상수로 하드코딩했다. argparse 또는 외부 인자 전달은 사용하지 않는다.
- 기존 패키지: `mortgage_list`, `registry`, `appraisal`, `test.py`는 수정하지 않고 그대로 호출한다.
- `pipeline.py`: 단계별 JSON 산출물 생성 모듈이다.
  - `01_registry_merge`: 등기부 표제부와 을구를 `registry_id`로 연결하고 동일 담보설정현황 그룹을 병합한다.
  - `02_real_estate`: 감정평가 부동산 그룹에 안정 ID를 부여하고 LLM 1차 매칭 및 독립 검증을 수행한다.
  - `03_machinery_base`: 저당목록 번호 집합으로 기계기구 기준 담보 그룹을 결정한다.
  - `04_machinery_match`: 감정평가 기계기구 행과 목록별 항목을 LLM으로 대조한다.
  - `05_excel`: 최종 Excel 입력 계획과 검증 결과를 만든다.
- `excel.py`: 고수준 Excel tool-call 역할을 하는 `write_collateral_workbook(workbook_plan, output_path)`를 추가했다. 이 함수가 행 커서, 병합, 서식, 수식, 숫자 형식, 저장을 전담한다.

## 3. 주요 로직

- 등기부 병합은 날짜, 금액, 공담, 목록번호를 정규화하고 `주식회사` 법인 접두어 차이를 제거해 동일 담보설정현황을 판단한다.
- LLM에는 금액 합산을 맡기지 않는다. 부동산 가치평가액은 승인된 `appraisal_group_id`를 원본 감정평가 JSON에서 다시 조회해 합산한다.
- 기계기구 대조표는 감정평가서의 기계기구 배열 순서를 기준으로 작성한다. 수량 합계는 행 수가 아니라 `quantity` 합계이며, 금액 합계는 `amount` 합계다.
- `unit_price = null`인 행은 단가를 역산하지 않고 Excel 단가 셀을 공란으로 둔다.
- 배분액과 배분잔액은 원천 데이터가 없으므로 계속 공란으로 유지한다.
- 계획의 `status`, `review_required`, `blocking_errors`, 부동산/기계기구 합계가 검증을 통과해야 Excel 파일이 확정 저장된다.

## 4. 산출물

- 실행 파일: `main.py`
- 파이프라인 모듈: `pipeline.py`
- Excel tool-call 함수: `excel.write_collateral_workbook`
- 최종 결과 경로: `감정평가서 휴먼중공업/담보물배분표.xlsx`
- 중간 산출물 경로: `감정평가서 휴먼중공업/pipeline_artifacts/`

## 5. 검증

- `python -m py_compile main.py pipeline.py excel.py`로 문법 검증을 완료했다.
- 기존 추출 JSON을 이용한 로컬 smoke plan으로 `output/smoke.xlsx` 생성 및 주요 셀 저장을 확인했다.
- `python main.py` 전체 실행을 완료했다. 실행 결과 `감정평가서 휴먼중공업/담보물배분표.xlsx`가 생성되었고, `workbook_plan.status=ok`, `review_required=0`, `workbook_validation.status=ok`를 확인했다.
- 산출 XLSX 핵심값 검증: 부동산 평가 합계 5,827,355,200원, 기계기구 행 수 9, 수량 합계 10, 금액 합계 179,890,000원, 저당목록 열 수 6.
