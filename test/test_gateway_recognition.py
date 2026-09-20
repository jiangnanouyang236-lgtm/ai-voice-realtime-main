from gateway.recognition import is_valid_recognition


def test_is_valid_recognition_rejects_empty_text():
    assert is_valid_recognition("") is False
    assert is_valid_recognition(None) is False


def test_is_valid_recognition_rejects_only_punctuation_or_space():
    assert is_valid_recognition(" ，。！？ ") is False
    assert is_valid_recognition("...") is False


def test_is_valid_recognition_accepts_chinese_english_and_digits():
    assert is_valid_recognition("嗯好") is True
    assert is_valid_recognition("ok") is True
    assert is_valid_recognition("a1") is True


def test_is_valid_recognition_applies_min_length_after_cleanup():
    assert is_valid_recognition(" 嗯。", min_length=2) is False
    assert is_valid_recognition(" 嗯，好。", min_length=2) is True
    assert is_valid_recognition("abc", min_length=4) is False
