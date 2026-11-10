from validator.table_extractor import extract_document_tables


def test_extracts_business_tables_from_example_gt():
    doc = extract_document_tables("GT/담보물배분표GT.xlsx")
    assert [t.table_type for t in doc.tables] == [
        "real_estate_collateral",
        "real_estate_collateral",
        "machine_collateral",
        "machine_register",
    ]
    assert doc.tables[0].table_id == "real_estate_1"
    assert "12 외 5필지" in doc.tables[0].narrative
    assert "기계기구10식" in doc.tables[2].narrative
    assert "Over head Crane" in doc.tables[3].to_prompt_text()


def test_extracts_business_tables_from_target_without_cell_coordinate_dump():
    doc = extract_document_tables("우리시스템/휴먼중공업 담보물배분표.xlsx")
    text = doc.to_prompt_text()
    assert "real_estate_1" in text
    assert "machine_register_1" in text
    assert "합천군 철서면" in text
    assert "13-3, 13-4, 14-2, 18, 22" in text
