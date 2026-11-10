from validator.normalizer import normalize_date, normalize_money, normalize_membership, normalize_text


def test_date_variants_are_iso():
    assert normalize_date("2012. 8. 20.") == "2012-08-20"
    assert normalize_date("2012/08/20") == "2012-08-20"
    assert normalize_date("2012-08-20 00:00:00") == "2012-08-20"


def test_money_variants_are_numbers():
    assert normalize_money("5,040,000,000원") == 5040000000
    assert normalize_money("(1,000원)") == -1000


def test_membership_states_are_normalized_for_llm_parser_guidance():
    assert normalize_membership("O") == "included"
    assert normalize_membership("X") == "not_included"
    assert normalize_membership("") == "unknown"
    assert normalize_membership("", blank_policy="not_included") == "not_included"


def test_text_variants_are_normalized_for_excel_snapshot_cleaning():
    assert normalize_text("  경남   HC ") == "경남HC"
