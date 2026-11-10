from validator.scorer import LLMScorer, MODEL_NAME
from validator.schemas import CategoryEvaluation, CollateralGroup, DomainJSON, ErrorItem, LLMValidationResult, ObjectMatch, ParsedBusinessTable, StatusCounts, ValidatorConfig


class FakeStructuredLLM:
    def __init__(self, result):
        self.result = result
    def invoke(self, prompt):
        assert "document_tables" in prompt
        assert "외 5필지" in prompt
        assert "감점하지 않는다" in prompt
        return self.result


class FakeChatOpenAI:
    result = None
    calls = []
    def __init__(self, **kwargs):
        FakeChatOpenAI.calls.append(kwargs)
    def with_structured_output(self, schema, **kwargs):
        assert schema is LLMValidationResult
        assert kwargs.get("method") == "function_calling"
        return FakeStructuredLLM(FakeChatOpenAI.result)


def llm_result():
    cats = [
        CategoryEvaluation(category="collateral_structure", precision=1, recall=1, f1=1, matched_credit=1, target_total=1, gt_total=1, rationale="테이블 분리 일치"),
        CategoryEvaluation(category="lien_relations", precision=1, recall=1, f1=1, matched_credit=1, target_total=1, gt_total=1, rationale="ok"),
        CategoryEvaluation(category="financial_values", precision=1, recall=1, f1=1, matched_credit=1, target_total=1, gt_total=1, rationale="ok"),
        CategoryEvaluation(category="property_details", precision=0.5, recall=0.5, f1=0.5, matched_credit=1, target_total=2, gt_total=2, rationale="행정구역 오류만 감점"),
        CategoryEvaluation(category="machine_details", precision=1, recall=1, f1=1, matched_credit=1, target_total=1, gt_total=1, rationale="ok"),
        CategoryEvaluation(category="list_memberships", precision=0, recall=0, f1=0, matched_credit=0, target_total=1, gt_total=1, rationale="목록 오류"),
    ]
    return LLMValidationResult(
        category_evaluations=cats,
        counts=StatusCounts(match=4, contradiction=2),
        errors=[ErrorItem(category="property_details", object="property_01", field="address.city_county", gt="함안군", target="합천군", status="contradiction", credit=0, reason="행정구역이 다름")],
        object_matches=[ObjectMatch(category="tables", gt_id="real_estate_1", target_id="real_estate_1", confidence=1.0, evidence={"basis": "table structure"})],
        llm_judgements=[{"gt": "12 외 5필지", "target": "12, 13-3, 13-4, 14-2, 18, 22", "credit": 1.0}],
    )


def test_llm_scorer_is_table_aware_and_gpt54(monkeypatch):
    import validator.scorer as scorer_module
    FakeChatOpenAI.result = llm_result()
    FakeChatOpenAI.calls = []
    monkeypatch.setattr(scorer_module, "ChatOpenAI", FakeChatOpenAI)
    gt = DomainJSON(collateral_groups=[CollateralGroup(group_id="g1")], document_tables=[ParsedBusinessTable(table_id="real_estate_1", table_type="real_estate_collateral", title="부동산", summary="12 외 5필지")])
    target = DomainJSON(collateral_groups=[CollateralGroup(group_id="t1")], document_tables=[ParsedBusinessTable(table_id="real_estate_1", table_type="real_estate_collateral", title="부동산", summary="12, 13-3, 13-4, 14-2, 18, 22")])
    report = LLMScorer(ValidatorConfig()).score(gt, target, metadata={}, derived_values=[])
    assert MODEL_NAME == "gpt-5.4"
    assert report.score == 77.5
    assert report.llm_judgements[0]["credit"] == 1.0
    assert FakeChatOpenAI.calls[0]["model"] == "gpt-5.4"


def test_scorer_source_has_no_local_similarity_logic():
    import pathlib
    source = pathlib.Path("validator/scorer.py").read_text(encoding="utf-8")
    forbidden = ["almost_equal", "canonical_text", "normalize_date", "build_object_matches", "SequenceMatcher"]
    assert not any(token in source for token in forbidden)
