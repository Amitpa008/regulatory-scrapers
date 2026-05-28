from extraction.metadata_cleaner import clean_reference_no, normalize_text, parse_indian_date


def test_normalize_text_collapses_whitespace() -> None:
    assert normalize_text("  SEBI   Circular \n  Update  ") == "SEBI Circular Update"


def test_clean_reference_no_preserves_allowed_chars() -> None:
    assert clean_reference_no(" SEBI/HO/CFD /PoD-2/P/CIR/2024/ 12 ") == "SEBI/HO/CFD/PoD-2/P/CIR/2024/12"


def test_parse_indian_date_day_first() -> None:
    assert parse_indian_date("16/05/2026").isoformat() == "2026-05-16"

