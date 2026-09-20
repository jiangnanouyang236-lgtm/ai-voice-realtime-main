import re


def is_valid_recognition(text: str, min_length: int = 2) -> bool:
    """
    检查识别结果是否有效（过滤误触发）

    Args:
        text: 识别文本
        min_length: 最小有效字符数

    Returns:
        是否有效
    """
    if not text:
        return False
    # 去除标点和空格，只保留中英文和数字
    clean_text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', text)
    return len(clean_text) >= min_length
