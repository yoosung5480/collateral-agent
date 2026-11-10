from __future__ import annotations

import json
from typing import Any
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from .schemas import DomainJSON, LLMValidationResult, ValidationReport, ValidatorConfig

MODEL_NAME = "gpt-5.4"
CATEGORIES = ["collateral_structure", "lien_relations", "financial_values", "property_details", "machine_details", "list_memberships"]


class LLMScorer:
    """LLM-only table-aware semantic evaluator.

    No local equality, similarity, embedding, object matching, or fact credit is computed here.
    Python only validates structured LLM output and applies the category weight sum.
    """

    def __init__(self, config: ValidatorConfig):
        self.config = config

    def score(self, gt: DomainJSON, target: DomainJSON, *, metadata: dict[str, Any], derived_values: list[dict[str, Any]]) -> ValidationReport:
        load_dotenv(".env")
        llm_result = self._invoke_evaluator(gt, target)
        category_scores = {item.category: item.model_dump(exclude={"category"}) for item in llm_result.category_evaluations}
        weighted = sum(float(category_scores[name]["f1"]) * getattr(self.config.weights, name) for name in CATEGORIES)
        return ValidationReport(
            score=round(100 * weighted, 2),
            category_scores=category_scores,
            counts=llm_result.counts.model_dump(),
            errors=llm_result.errors,
            object_matches=llm_result.object_matches,
            derived_values=derived_values,
            llm_judgements=llm_result.llm_judgements,
            metadata={**metadata, "scoring_engine": "gpt_5_4_table_aware_semantic_evaluator"},
        )

    def _invoke_evaluator(self, gt: DomainJSON, target: DomainJSON) -> LLMValidationResult:
        weights = self.config.weights.model_dump()
        prompt = f"""
너는 담보물배분표 의미 정확도 평가자다. 반드시 LLM 의미판단으로 평가하고, 내부 문자열 유사도/행번호/셀좌표/서식 일치로 채점하지 마라.

평가의 최상위 단위:
1. document_tables에 있는 부동산 담보물 배분표별 테이블
2. 기계기구 담보물 배분표 테이블
3. 기계기구 대조표 테이블

평가 순서:
1. 먼저 GT 테이블과 Target 테이블을 table_type 및 표제부/담보군 의미로 매칭한다.
2. 테이블 분리나 병합이 잘못되면 collateral_structure를 크게 낮춘다.
3. 테이블이 맞으면 그 안의 담보군, 부동산, 근저당권, 금액, 기계, 목록관계를 객체/속성/관계 단위로 평가한다.

의미 동치 중요 규칙:
- “경상남도 함안군 칠서면 계내리 12 외 5필지”와 “경상남도 함안군 칠서면 계내리 12, 13-3, 13-4, 14-2, 18, 22”는 같은 6필지 집합을 가리키는 표현이면 감점하지 않는다.
- “외 5필지”를 상세 5개 필지로 풀어쓴 것은 더 정확한 문서 기반 표현일 수 있으므로 오류가 아니다.
- 건물이 “제에이동호/제비동호/제비이동호”로 상세 열거되어도 GT의 같은 건물군/필지군을 설명하는 것이라면 객체 경계 오류로 보지 않는다.
- 한쪽 Domain JSON이 제에이동호와 제비동호를 하나의 building 객체 description에 묶고, 다른 쪽이 두 개 building 객체로 분리했더라도 같은 real_estate 테이블 안에서 같은 건물 집합/구조/면적을 보존하면 감점하지 않는다. 이것은 더 상세한 구조화 차이일 뿐 hallucination이 아니다.
- “12 외 5필지”와 상세 여섯 필지 열거가 같은 집합이면 property_details/collateral_structure에서 match로 처리한다.
- 같은 real_estate 테이블, 같은 6필지 집합, 같은 건물 순서/구조/면적/용도라면 “제비동호”와 “제비이동호” 같은 동호 표기의 미세 표기차만으로 partial/contradiction을 주지 마라. 이는 문서 표현 상세도 차이로 보고 match 처리한다.
- 반대로 “함안군→합천군”, “칠서면→철서면”처럼 행정구역/읍면/리/번지가 실제로 바뀐 것은 의미 모순이다.
- 임베딩식 유사도가 아니라 업무상 동일 담보물인지, 잘못 생성한 담보물인지, 누락인지 판단하라.
- unit_price/amount가 한쪽은 숫자이고 다른 쪽은 {{"value": 숫자, "derived": true, "derivation": ...}} 형태여도 value가 같고 파생 근거가 수량/금액 관계로 타당하면 match다. 파생 객체형이라는 이유만으로 partial 감점하지 마라.

수치화 규칙:
- 원자 사실 status: match/partial/contradiction/missing/extra/unknown.
- credit: match=1.0, partial=0.5, contradiction/missing/extra/unknown=0.0.
- category precision = matched_credit / target_total.
- category recall = matched_credit / gt_total.
- f1 = 2PR/(P+R), P+R=0이면 0.
- category_evaluations 배열에 6개 영역을 모두 반환한다.
- 최종 가중합은 Python이 아래 weights로 계산하므로 너는 영역별 점수와 근거만 반환한다.

영역 가중치:
{json.dumps(weights, ensure_ascii=False, indent=2)}

오류 보고:
- 모든 감점은 errors에 남긴다. 어떤 category의 f1이 1.0 미만이면 그 category에 속한 ErrorItem이 최소 1개 이상 있어야 한다.
- matched_credit/target_total/gt_total을 낮춰 놓고 errors를 비워두지 마라. 점수와 오류 목록은 서로 감사 가능해야 한다.
- errors.reason에는 왜 업무상 오류인지 적는다.
- 표현 차이지만 동일 의미인 것은 errors에 넣지 않는다. 필요하면 llm_judgements에 동치 판단 근거로 남긴다.
- object_matches에는 table match와 object match를 모두 남긴다.

GT Domain JSON:
{gt.model_dump_json(indent=2)}

Target Domain JSON:
{target.model_dump_json(indent=2)}
"""
        llm = ChatOpenAI(model=MODEL_NAME, temperature=0).with_structured_output(LLMValidationResult, method="function_calling")
        result = llm.invoke(prompt)
        if not isinstance(result, LLMValidationResult):
            result = LLMValidationResult.model_validate(result)
        return result
