

# 목표

GT 담보물배분표와 자동 생성된 담보물배분표를 비교해, 셀 위치·문자열·서식이 아닌 **업무 의미 정확도(Domain Semantic F1)** 를 계산하는 재사용 가능한 Python 평가기를 구현하라.

최종 사용법은 다음처럼 간단해야 한다.

```bash
python validate.py --gt 담보물배분표GT.xlsx --target 결과.xlsx
````

GT는 다음 두 방식 중 하나를 선택할 수 있어야 한다.

```bash
# GT Excel도 이번 실행에서 의미 JSON으로 파싱
python validate.py \
  --gt 담보물배분표GT.xlsx \
  --target 결과.xlsx \
  --gt-format excel

# 이미 검수·고정된 GT JSON 사용
python validate.py \
  --gt 담보물배분표GT.json \
  --target 결과.xlsx \
  --gt-format json
```

`--gt-format`을 생략하면 확장자로 자동 판별한다.

* `.xlsx`, `.xlsm` → `excel`
* `.json` → `json`

파일명은 반드시 `validate.py`로 작성하라.

---

# 핵심 원칙

이 평가기는 다음을 평가하지 않는다.

* 셀 좌표 일치 여부
* 행·열 배치
* 병합 셀 모양
* 글꼴, 색상, 테두리, 열 너비
* 동일 정보의 반복 기재 여부
* 단순 문자열 유사도

두 Excel을 동일한 도메인 JSON으로 변환한 뒤, **객체·속성·관계 단위로 비교**해야 한다.

전체 파일을 LLM에게 주고 임의로 0~100점을 생성하게 해서는 안 된다.

LLM의 역할은 다음 두 가지다.

1. 비정형 Excel을 정해진 의미 JSON 스키마로 파싱
2. GT Domain JSON과 Target Domain JSON을 객체·속성·관계 단위로 양방향 의미 평가해 영역별 Precision/Recall/F1, 객체 매칭, 오류 근거를 structured output으로 반환

Python 코드는 LLM 결과를 Pydantic으로 검증하고 설정 가중치에 따른 최종 가중합과 리포트 저장만 수행한다. 로컬 문자열 유사도, 숫자 유사도, no-LLM fallback 점수체계는 사용하지 않는다.

---

# 처리 파이프라인

```text
GT Excel 또는 GT JSON
        ↓
정규화된 GT Domain JSON
        ↓
Target Excel
        ↓
정규화된 Target Domain JSON
        ↓
객체 정렬 및 원자 사실 비교
        ↓
애매한 자유서술만 LLM 동치 판정
        ↓
영역별 Precision / Recall / Semantic F1
        ↓
가중 Domain Semantic F1 및 오류 보고서
```

---

# 필수 CLI

다음 인자를 구현하라.

```text
--gt PATH
--target PATH
--gt-format {auto,excel,json}   기본값 auto
--output PATH                   기본값 validation_report.json
--save-gt-json PATH             Excel로 파싱한 GT JSON을 별도 저장
--save-target-json PATH         Target 파싱 결과 저장
--model MODEL_NAME              LLM 모델 지정
--config PATH                   가중치 및 판정 설정 JSON
--verbose
```

다음 호출은 반드시 작동해야 한다.

```bash
python validate.py --gt GT.xlsx --target target.xlsx
```

---

# Excel 전처리

`openpyxl`을 사용해 다음 정보를 읽는다.

* 시트명
* 셀 좌표와 표시값
* 수식과 계산된 값
* 병합 셀 범위
* 숨겨진 행·열 여부
* 표 내부의 빈 셀
* 행과 열의 상대적 구조

LLM에는 바이너리 Excel을 그대로 전달하지 말고, 시트별 셀 내용을 구조화한 입력으로 전달한다.

빈 행·열을 제거하되 표의 의미 구조가 훼손되지 않도록 한다. 병합 셀은 좌상단 값을 병합 범위에 전파한 형태도 함께 제공한다.

서식 정보는 데이터 영역 추정에 참고할 수 있지만 점수에는 포함하지 않는다.

---

# 공통 Domain JSON 스키마

GT와 Target을 반드시 동일한 스키마로 파싱하라.

```json
{
  "schema_version": "1.0",
  "collateral_groups": [
    {
      "group_id": "stable_internal_id",
      "group_type": "real_estate|machine|mixed|unknown",
      "properties": ["property_id"],
      "valuation": 5681855200,
      "description": null,
      "source_refs": []
    }
  ],
  "properties": [
    {
      "property_id": "stable_internal_id",
      "property_type": "land|building|unknown",
      "address": {
        "province": null,
        "city_county": "함안군",
        "town": "칠서면",
        "village": "계내리",
        "lot_number": "12"
      },
      "land_category": null,
      "area_m2": null,
      "building_structure": null,
      "usage": null,
      "description": null,
      "source_refs": []
    }
  ],
  "liens": [
    {
      "group_id": "stable_internal_id",
      "rank": 1,
      "registration_date": "2012-08-20",
      "creditor": "중소기업은행",
      "debtor": null,
      "maximum_claim": 5040000000,
      "joint_collateral": [],
      "source_refs": []
    }
  ],
  "machines": [
    {
      "machine_id": "stable_internal_id",
      "serial_no": "1",
      "name": "Over Head Crane",
      "maker": "경남HC",
      "manufactured_date": null,
      "specification": null,
      "quantity": 1,
      "unit_price": 17570000,
      "amount": 17570000,
      "source_refs": []
    }
  ],
  "list_memberships": [
    {
      "machine_id": "stable_internal_id",
      "list_no": "2012-399",
      "state": "included|not_included|unknown",
      "source_refs": []
    }
  ],
  "notes": []
}
```

`group_id`, `property_id`, `machine_id`는 Excel에 존재하지 않는 내부 식별자이므로 두 파일에서 문자열이 같아야 할 필요는 없다. 실제 속성을 기반으로 객체를 정렬한 뒤 대응 관계를 찾아야 한다.

---

# 정규화 규칙

다음 정규화는 코드로 처리한다.

## 숫자

* 쉼표, `원`, 공백 제거
* 괄호 음수 처리
* 정수와 실수 통일
* 면적 단위 통일
* 금액은 기본적으로 정확 일치
* 설정값으로 허용 오차 지정 가능

## 날짜

다음 표현을 ISO 형식으로 통일한다.

```text
2012. 8. 20.
2012-08-20
2012/08/20
2012-08-20 00:00:00
```

결과:

```text
2012-08-20
```

## 텍스트

* 앞뒤 공백 제거
* 연속 공백 통합
* 유니코드 정규화
* 명백한 표기 변형 사전 적용
* 단, 행정구역·고유번호·채권자명처럼 의미가 바뀔 수 있는 문자열에는 퍼지 매칭을 남용하지 않는다.

## 파생정보 복원

정보가 논리적으로 유일하게 복원되면 누락으로 처리하지 않는다.

```python
unit_price = amount / quantity
amount = unit_price * quantity
```

예를 들어 수량과 총금액이 정확하다면 단가 셀이 비어 있더라도 단가를 복원해 정답 처리한다.

복원된 값은 보고서에 다음처럼 표시한다.

```json
{
  "value": 17570000,
  "derived": true,
  "derivation": "amount / quantity"
}
```

---

# O/X 및 공란 처리

Target의 공란을 `not_included`로 간주하면 안 된다.

```text
O, 포함, Y, Yes → included
X, 미포함, N, No → not_included
Target 공란 → unknown
```

GT 공란의 의미는 설정으로 지정한다.

```json
{
  "gt_blank_membership": "not_included"
}
```

기본값은 `not_included`로 하되 설정 파일에서 `unknown`으로 변경할 수 있어야 한다.

`unknown`은 정답으로 추측해서는 안 되며 Recall 누락으로 처리한다. 명시적인 오답과 미확정 누락은 보고서에서 구분한다.

---

# 객체 매칭

행 번호나 내부 ID가 아니라 의미 속성을 사용해 GT 객체와 Target 객체를 정렬한다.

## 부동산

우선순위:

1. 주소의 행정구역·리·번지
2. 토지/건물 유형
3. 면적
4. 구조 및 용도

## 기계기구

우선순위:

1. 일련번호
2. 명칭
3. 제작자
4. 규격
5. 수량과 금액

## 근저당권

우선순위:

1. 담보군 귀속
2. 순위
3. 설정일
4. 채권자
5. 채권최고액

Hungarian matching 또는 이에 준하는 최대 유사도 일대일 매칭을 사용한다. 매칭 근거와 confidence를 보고서에 남긴다.

---

# 사실 판정

각 필드는 원자 사실로 평가한다.

```text
(property_01, city_county, 함안군)
(property_01, town, 칠서면)
(lien_01, creditor, 중소기업은행)
(lien_01, maximum_claim, 5040000000)
(machine_01, quantity, 1)
(machine_01, member_of, 2012-399)
```

판정 상태:

```text
match
partial
contradiction
missing
extra
unknown
```

점수 credit:

```text
match         = 1.0
partial       = 0.5
contradiction = 0.0
missing       = 0.0
extra         = 0.0
unknown       = 0.0
```

`partial`은 자유서술처럼 GT의 핵심 내용 일부만 보존한 경우에만 사용한다. 주소, 금액, 날짜, 목록번호 등 명확한 필드에는 원칙적으로 사용하지 않는다.

---

# LLM 의미 판정

다음과 같은 자유서술에만 LLM 판정을 사용한다.

```text
GT: 일반철골구조 판넬지붕 단층 공장
Target: 판넬 지붕의 일반 철골조 1층 공장
```

LLM에게 전체 점수를 요청하지 말고 다음 JSON만 반환하도록 한다.

```json
{
  "gt_entails_target": true,
  "target_entails_gt": true,
  "contradiction": false,
  "credit": 1.0,
  "reason": "구조, 지붕 재료, 층수, 용도가 모두 동일함"
}
```

판정 규칙:

* 양방향 entailment, 모순 없음 → `1.0`
* Target이 GT 일부만 보존 → `0.5`
* 의미 모순 또는 무관 → `0.0`

같은 입력에 대한 판정은 캐시하여 재실행 비용과 변동성을 줄인다. temperature는 `0`으로 설정하고 structured output을 사용한다.

---

# 평가 영역과 기본 가중치

```json
{
  "weights": {
    "collateral_structure": 0.15,
    "lien_relations": 0.20,
    "financial_values": 0.20,
    "property_details": 0.15,
    "machine_details": 0.15,
    "list_memberships": 0.15
  }
}
```

가중치 합은 반드시 `1.0`인지 검증한다.

각 영역 내부에서 weighted Precision, Recall, F1을 계산한다.

```text
matched_credit = match credit의 합
target_total   = Target이 주장한 평가 대상 사실 수
gt_total       = GT의 평가 대상 사실 수

Precision = matched_credit / target_total
Recall    = matched_credit / gt_total
F1        = 2 × Precision × Recall / (Precision + Recall)
```

Target의 잘못된 추가 사실은 Precision을 낮추고, GT 사실의 누락은 Recall을 낮춰야 한다.

최종 점수:

```python
domain_semantic_f1 = 100 * sum(
    category_f1[name] * category_weights[name]
    for name in category_weights
)
```

목록관계 54개처럼 사실 수가 많은 한 영역이 전체 점수를 지배하지 않도록 반드시 **영역별 F1을 먼저 계산한 뒤 가중 평균**한다.

---

# 담보군 구조 평가 주의사항

다음은 단순 금액 일치로 정답 처리하면 안 된다.

```text
GT:
- 담보군 A 평가액: 5,681,855,200원
- 담보군 B 평가액:   145,500,000원

Target:
- A와 B를 합친 담보군: 5,827,355,200원
```

총합은 같지만 담보군 분리와 금액 귀속이 틀렸으므로 다음을 각각 감점한다.

* 담보군 구조
* 부동산 귀속
* 평가액 귀속
* 해당 담보군에 연결된 근저당권 관계

동일 오류에서 파생된 필드가 과도하게 중복 감점되지 않도록 보고서에는 `root_cause_id`를 부여한다. 다만 각 영역의 실제 업무 영향은 영역별 점수에 반영한다.

---

# 출력 결과

터미널에 다음 요약을 출력한다.

```text
Domain Semantic F1: 97.45

Category                 Precision  Recall   F1
collateral_structure       1.0000   1.0000  1.0000
lien_relations             1.0000   1.0000  1.0000
financial_values           1.0000   1.0000  1.0000
property_details           0.9412   0.9412  0.9412
machine_details            1.0000   1.0000  1.0000
list_memberships           0.8889   0.8889  0.8889
```

`validation_report.json`에는 다음을 저장한다.

```json
{
  "score": 97.45,
  "category_scores": {},
  "counts": {
    "match": 0,
    "partial": 0,
    "contradiction": 0,
    "missing": 0,
    "extra": 0,
    "unknown": 0
  },
  "errors": [
    {
      "category": "property_details",
      "object": "property_01",
      "field": "city_county",
      "gt": "함안군",
      "target": "합천군",
      "status": "contradiction",
      "credit": 0.0,
      "root_cause_id": "address_001"
    }
  ],
  "object_matches": [],
  "derived_values": [],
  "llm_judgements": [],
  "metadata": {
    "gt_path": "",
    "target_path": "",
    "gt_format": "",
    "model": "",
    "created_at": ""
  }
}
```

---

# GT 운영 방식

## GT Excel 파싱 모드

```bash
python validate.py \
  --gt GT.xlsx \
  --target target.xlsx \
  --gt-format excel \
  --save-gt-json GT.json
```

* GT Excel과 Target Excel을 모두 파싱한다.
* 파싱한 GT를 `GT.json`으로 저장한다.
* 최초 생성된 GT JSON은 사람이 검수할 수 있어야 한다.

## 고정 GT JSON 모드

```bash
python validate.py \
  --gt GT.json \
  --target target.xlsx \
  --gt-format json
```

* GT는 다시 LLM으로 파싱하지 않는다.
* Target만 파싱한다.
* 공식 벤치마크에서는 이 모드를 권장한다.
* 동일한 결과 파일은 반복 실행해도 같은 점수가 나와야 한다.

---

# 프로젝트 구조

최소한 다음처럼 분리하라.

```text
validate.py
validator/
    excel_reader.py
    schemas.py
    parser.py
    normalizer.py
    matcher.py
    semantic_judge.py
    scorer.py
    reporter.py
config/
    default.json
tests/
    test_normalizer.py
    test_matcher.py
    test_scorer.py
    test_cli.py
```

Pydantic 모델을 사용해 Domain JSON을 검증한다.

LLM API 키, 모델명 등은 코드에 하드코딩하지 말고 환경변수와 CLI 인자를 사용한다.

---

# 필수 테스트

다음 케이스를 테스트하라.

1. 같은 정보가 다른 셀에 있어도 정답
2. 날짜 표현만 다른 경우 정답
3. 쉼표와 `원`이 붙은 금액도 정답
4. 수량과 금액으로 단가가 복원되면 정답
5. 담보군별 금액을 합쳐 적으면 총합이 같아도 구조 오류
6. Target 공란 목록관계는 `unknown`
7. 잘못된 추가 기계나 근저당은 Precision 감점
8. 누락된 기계는 Recall 감점
9. 표현만 다른 건물구조는 LLM 동치 판정
10. `함안군 → 합천군`은 의미 모순
11. GT JSON 모드에서는 GT 파싱 LLM이 호출되지 않음
12. 동일 입력을 두 번 실행하면 같은 점수 산출

---

# 완료 조건

다음이 모두 충족되어야 한다.

* `python validate.py --gt GT/담보물배분표GT.xlsx --target 우리시스템/휴먼중공업 담보물배분표.xlsx` 실행 가능
* GT Excel 파싱과 기존 GT JSON 사용을 선택 가능
* 결과 Excel을 Domain JSON으로 변환
* 셀 위치와 서식을 점수에서 제외
* 파생 가능한 단가를 정답 처리
* 객체 누락과 환각을 Precision/Recall로 구분
* 자유서술 외 필드는 결정론적으로 비교
* 영역별 Semantic F1과 최종 점수 출력
* 모든 불일치의 GT 값, Target 값, 판정 이유 출력
* 단위 테스트 통과
* README에 설치법, 환경변수, CLI 예시 작성

구현 후 예시 파일로 실행하여 다음을 보고하라.

1. 생성·수정한 파일
2. 실행 명령어
3. 영역별 Precision/Recall/F1
4. 최종 Domain Semantic F1
5. 주요 감점 항목
6. 테스트 결과

핵심은 기본 실행은 짧게 유지하면서:

```bash
python validate.py --gt GT.xlsx --target target.xlsx
````

공식 평가 때는 검수된 GT를 고정해서:

```bash
python validate.py --gt GT.json --target target.xlsx
```

사용하는 구조다. 이렇게 해야 **GT 자체가 실행할 때마다 다르게 파싱되어 점수가 흔들리는 문제**를 막을 수 있다.

# 주의사항
- 반드시 LangChain, LangGraph 및 pydantic 프레임워크를 사용해서 구현한다.
- 지나친 try-except및 방어적 코딩 금지이다.
- llm api호출없이 fall-back 호출은 금지다. 
- llm api를 통한 계산없이 내부 fallback으로 실행성공은 성공으로 간주하지 않는다.
---

# 구현/실행 안내

## 설치

Python 3.10 이상 환경에서 다음 패키지가 필요하다.

```bash
pip install openpyxl pydantic langchain langgraph langchain-openai python-dotenv pytest
```

## 환경변수

LLM 자유서술 의미 동치 판정을 사용할 때는 `.env` 또는 셸 환경에 OpenAI API 키를 둔다.

```bash
OPENAI_API_KEY=...
```

모델은 CLI에서 지정할 수 있다.

```bash
python validate.py --gt GT.xlsx --target target.xlsx --model gpt-5.4
```

## 기본 실행

```bash
python validate.py --gt GT/담보물배분표GT.xlsx --target "우리시스템/휴먼중공업 담보물배분표.xlsx"
```

결과는 기본적으로 `validation_report.json`에 저장된다.

## GT JSON 고정 실행

최초 1회 GT Excel을 Domain JSON으로 저장한다.

```bash
python validate.py \
  --gt GT/담보물배분표GT.xlsx \
  --target "우리시스템/휴먼중공업 담보물배분표.xlsx" \
  --save-gt-json GT/담보물배분표GT.json
```

검수된 GT JSON을 고정해 반복 평가한다.

```bash
python validate.py \
  --gt GT/담보물배분표GT.json \
  --target "우리시스템/휴먼중공업 담보물배분표.xlsx" \
  --gt-format json
```

## 부가 출력

```bash
python validate.py \
  --gt GT/담보물배분표GT.xlsx \
  --target "우리시스템/휴먼중공업 담보물배분표.xlsx" \
  --output validation_report.json \
  --save-gt-json GT/담보물배분표GT.json \
  --save-target-json target_domain.json \
  --verbose
```

## 테스트

```bash
pytest -q
```

## 구현 구조

```text
validate.py                 # CLI 엔트리포인트
validator/excel_reader.py   # openpyxl 기반 엑셀 구조화 입력 생성
validator/schemas.py        # Pydantic Domain JSON/리포트 스키마
validator/parser.py         # LangGraph 파싱 파이프라인과 도메인 추출
validator/normalizer.py     # 숫자/날짜/텍스트/OX 정규화
validator/matcher.py        # 의미 속성 기반 일대일 객체 매칭
validator/semantic_judge.py # LangChain 기반 자유서술 LLM 동치 판정 및 캐시
validator/scorer.py         # 원자 사실 비교와 영역별 Precision/Recall/F1
validator/reporter.py       # JSON 리포트 저장 및 터미널 요약 출력
config/default.json         # 기본 가중치/판정 설정
tests/                      # 정규화, 매칭, 스코어러, CLI 회귀 테스트
```


## 2026-08-04 구현 변경: LLM 필수 평가체계

현재 구현은 `--no-llm` 모드를 제공하지 않는다. 다음 경로는 모두 제거되었다.

- 로컬 `text_similarity` / `number_similarity` 기반 객체 매칭
- 규칙 기반 Precision/Recall/F1 산출
- 자유서술 필드만 따로 처리하는 no-LLM fallback

현재 평가 흐름은 다음과 같다.

```text
Excel
  -> openpyxl 구조화 스냅샷
  -> LangGraph + LangChain structured-output LLM 파싱
  -> Pydantic DomainJSON 검증
  -> GT/Target DomainJSON 전체를 LangChain structured-output LLM 평가기에 전달
  -> LLM이 object_matches, errors, category Precision/Recall/F1을 반환
  -> Python은 Pydantic 검증 및 영역별 F1 가중합만 계산
```

따라서 API 키와 LLM 모델이 없으면 검증은 성공하지 않는다. 기본 모델은 `VALIDATOR_MODEL` 환경변수 또는 `--model`로 지정하며, 생략 시 `gpt-5.4`를 사용한다.
