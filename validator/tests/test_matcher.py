from pathlib import Path


def test_rule_similarity_matcher_file_is_removed():
    assert not Path("validator/matcher.py").exists()
