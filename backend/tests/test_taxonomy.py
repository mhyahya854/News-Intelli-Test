from newsintel.taxonomy import category_seed_rows, keyword_seed_rows, normalize_term


def test_taxonomy_is_large_bilingual_and_unique() -> None:
    categories = category_seed_rows()
    keywords = list(keyword_seed_rows())
    assert len(categories) == 16
    assert len(keywords) >= 1000
    identities = {(k["category_id"], k["language"], k["normalized_term"]) for k in keywords}
    assert len(identities) == len(keywords)
    assert {k["language"] for k in keywords} == {"en", "ur"}


def test_urdu_normalization_handles_common_codepoint_variants() -> None:
    assert normalize_term("مہنگاٸی", "ur") == normalize_term("مہنگائی", "ur")
    assert normalize_term("وزير اعظم", "ur") == normalize_term("وزیر اعظم", "ur")


def test_ambiguous_keywords_require_context() -> None:
    keywords = list(keyword_seed_rows())
    by_key = {(k["category_id"], k["language"], k["normalized_term"]): k for k in keywords}
    assert by_key[("judiciary", "en", "case")]["requires_context"] is True
    assert by_key[("energy", "en", "power")]["requires_context"] is True
    assert by_key[("judiciary", "en", "supreme court")]["requires_context"] is False


def test_keyword_seed_disables_misspelling_and_fuzzy_logic() -> None:
    keywords = list(keyword_seed_rows())
    assert all(keyword["match_mode"] == "phrase" for keyword in keywords)
    assert all("min_fuzzy_score" not in keyword for keyword in keywords)
    assert all("ocr_variants" not in keyword for keyword in keywords)


def test_cross_category_duplicate_terms_require_context() -> None:
    keywords = list(keyword_seed_rows())
    budget_rows = [item for item in keywords if item["language"] == "en" and item["normalized_term"] == "budget"]
    assert {item["category_id"] for item in budget_rows} == {"economy", "politics"}
    assert all(item["requires_context"] is True for item in budget_rows)


def test_city_names_do_not_classify_sports_without_context() -> None:
    keywords = list(keyword_seed_rows())
    lahore = next(item for item in keywords if item["category_id"] == "sports" and item["normalized_term"] == "lahore")
    assert lahore["requires_context"] is True
