from validator.parser import MODEL_NAME, parse_excel
from validator.schemas import CollateralGroup, DomainJSON, Machine, ParsedBusinessTable, ValidatorConfig


class FakeStructuredParser:
    def invoke(self, prompt):
        assert "문서 구조에 따라 미리 분리된 업무 테이블" in prompt
        assert "real_estate_1" in prompt
        assert "machine_register_1" in prompt
        assert "12 외 5필지" in prompt
        assert "13-3, 13-4, 14-2, 18, 22" in prompt
        return DomainJSON(
            collateral_groups=[CollateralGroup(group_id="cg_machine", group_type="machine", valuation=10)],
            machines=[Machine(machine_id="machine_01", serial_no="1", name="Over Head Crane", quantity=1, unit_price={"value": 10, "derived": True, "derivation": "amount / quantity"}, amount=10)],
            document_tables=[ParsedBusinessTable(table_id="machine_register_1", table_type="machine_register", title="기계기구 대조표", summary="ok", machine_ids=["machine_01"], membership_count=1)],
        )


class FakeChatOpenAI:
    calls = []
    def __init__(self, **kwargs):
        FakeChatOpenAI.calls.append(kwargs)
    def with_structured_output(self, schema, **kwargs):
        assert schema is DomainJSON
        assert kwargs.get("method") == "function_calling"
        return FakeStructuredParser()


def test_parse_excel_is_table_structured_llm(monkeypatch):
    import validator.parser as parser_module
    monkeypatch.setattr(parser_module, "ChatOpenAI", FakeChatOpenAI)
    domain, derived = parse_excel("우리시스템/휴먼중공업 담보물배분표.xlsx", is_gt=False, config=ValidatorConfig())
    assert MODEL_NAME == "gpt-5.4"
    assert domain.document_tables[0].table_id == "machine_register_1"
    assert derived[0]["derivation"] == "amount / quantity"
    assert FakeChatOpenAI.calls[-1]["model"] == "gpt-5.4"
