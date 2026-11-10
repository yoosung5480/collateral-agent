from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from .schemas import DomainJSON, ValidatorConfig
from .table_extractor import StructuredDocument, extract_document_tables

MODEL_NAME = "gpt-5.4"


class ParseState(TypedDict, total=False):
    path: str
    is_gt: bool
    config: ValidatorConfig
    structured_document: StructuredDocument
    domain: DomainJSON
    derived_values: list[dict[str, Any]]


def parse_excel(path: str | Path, *, is_gt: bool, config: ValidatorConfig) -> tuple[DomainJSON, list[dict[str, Any]]]:
    """Parse Excel into Domain JSON using structure-first tables and mandatory LLM parsing."""
    graph = StateGraph(ParseState)
    graph.add_node("extract_document_tables", _extract_tables_node)
    graph.add_node("llm_parse_structured_tables", _llm_parse_node)
    graph.set_entry_point("extract_document_tables")
    graph.add_edge("extract_document_tables", "llm_parse_structured_tables")
    graph.add_edge("llm_parse_structured_tables", END)
    result = graph.compile().invoke({"path": str(path), "is_gt": is_gt, "config": config})
    return result["domain"], result.get("derived_values", [])


def _extract_tables_node(state: ParseState) -> ParseState:
    return {**state, "structured_document": extract_document_tables(state["path"])}


def _llm_parse_node(state: ParseState) -> ParseState:
    load_dotenv(".env")
    structured = state["structured_document"]
    config = state["config"]
    role = "GT 기준 정답" if state["is_gt"] else "검증 대상 결과"
    table_text = structured.to_prompt_text()
    prompt = f"""
너는 담보물배분표 문서를 업무 의미 Domain JSON으로 파싱하는 검수자다.
입력은 원본 셀 좌표 덤프가 아니라, 문서 구조에 따라 미리 분리된 업무 테이블이다.
최상위 구획은 반드시 다음 세 종류다: 부동산 담보물 배분표, 기계기구 담보물 배분표, 기계기구 대조표.

역할: {role}
GT 공란 목록관계 정책: {config.gt_blank_membership}

절대 원칙:
1. 각 real_estate_collateral 테이블은 별도 부동산 담보군이다. 테이블 간 담보군을 합치지 마라.
2. machine_collateral 테이블은 별도 기계기구 담보군이다.
3. machine_register 테이블은 기계기구 대조표이며, 기계 객체와 목록관계의 주 근거다.
4. document_tables 배열에 입력 테이블 각각을 반드시 기록하고, 그 테이블에서 추출한 object id들을 연결하라.
5. source_refs에는 셀 좌표 기반 점수 근거가 아니라 테이블 id/행 범위/원문 요약을 남겨라.
6. 부동산 주소 서술에서 “12 외 5필지”와 “12, 13-3, 13-4, 14-2, 18, 22”처럼 같은 필지 집합을 달리 표현한 것은 의미상 같은 후보로 보존하라. 파싱 단계에서 오류로 만들지 마라.
7. 다만 “함안군→합천군”, “칠서면→철서면”처럼 행정구역 자체가 바뀐 값은 그대로 파싱해야 평가기가 오류를 잡을 수 있다.
8. 건물이 “제에이동호/제비동호”로 상세 열거되어도 GT의 “12 외 5필지” 건물 소재지와 의미상 같은 후보일 수 있으므로 description/raw 표현을 보존하라.
9. <건물> 아래에 1., 2.처럼 번호가 붙은 건물 항목이 있으면 원칙적으로 건물별 Property 객체를 각각 만든다. 예: 제에이동호와 제비동호/제비이동호는 별도 building property로 둔다.
10. 다만 주소 필지 표현은 raw description에 반드시 보존한다. “12외 5필지”와 “12, 13-3, 13-4, 14-2, 18, 22”는 같은 집합 후보임을 description에 남긴다.
11. 금액/수량/면적/날짜는 가능한 한 정규화하되, 애매하면 원문을 description/source_refs에 보존한다.
12. O/포함/Y/Yes는 included, X/미포함/N/No는 not_included, Target 공란은 unknown이다. GT 공란은 설정 정책을 따른다.
13. 단가가 비어 있고 수량과 금액으로 유일하게 복원되면 unit_price를 {{"value": ..., "derived": true, "derivation": "amount / quantity"}} 형태로 둔다.
14. 테이블에 명시된 담보군, 부동산, 근저당권, 기계, 목록관계를 누락하지 마라.

구조화된 문서 테이블:
{table_text}
"""
    llm = ChatOpenAI(model=MODEL_NAME, temperature=0).with_structured_output(DomainJSON, method="function_calling")
    domain = llm.invoke(prompt)
    if not isinstance(domain, DomainJSON):
        domain = DomainJSON.model_validate(domain)
    derived = _collect_derived_values(domain)
    return {**state, "domain": domain, "derived_values": derived}


def _collect_derived_values(domain: DomainJSON) -> list[dict[str, Any]]:
    derived: list[dict[str, Any]] = []
    for machine in domain.machines:
        for field in ("unit_price", "amount"):
            value = getattr(machine, field)
            if isinstance(value, dict) and value.get("derived"):
                derived.append({"object": machine.machine_id, "field": field, **value})
    return derived
