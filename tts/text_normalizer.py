from __future__ import annotations

import re


_RE_CODE_BLOCK = re.compile(r'```[\s\S]*?```')
_RE_INLINE_CODE = re.compile(r'`[^`]+`')
_RE_LINK = re.compile(r'\[([^\]]+)\]\([^\)]+\)')
_RE_HEADING = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_RE_BOLD1 = re.compile(r'\*\*([^\*]+)\*\*')
_RE_ITALIC1 = re.compile(r'\*([^\*]+)\*')
_RE_BOLD2 = re.compile(r'__([^_]+)__')
_RE_ITALIC2 = re.compile(r'_([^_]+)_')
_RE_STRIKETHROUGH = re.compile(r'~~([^~]+)~~')
_RE_LIST_DASH = re.compile(r'^\s*[-+*]\s+', re.MULTILINE)
_RE_LIST_NUM = re.compile(r'^\s*\d+\.\s+', re.MULTILINE)
_RE_EMOTICON = re.compile(r'\([^\u4e00-\u9fa5a-zA-Z0-9\s,，.。!！?？;；:：\)]{1,10}\)')
_RE_EMOJI = re.compile(r'[^\u4e00-\u9fa5a-zA-Z0-9\s,，.。!！?？;；:：""''（）()《》【】、~\-]')
_RE_SPECIAL_CHARS = re.compile(r'[#$%^&<>[\]\\|@]')
_RE_MULTI_NEWLINE = re.compile(r'\n\s*\n')
_RE_MULTI_SPACE = re.compile(r' +')
_RE_DECIMAL_NUMBER = re.compile(r'(?<![A-Za-z0-9.])(\d+)\.(\d+)(?![A-Za-z0-9.])')
_RE_PERCENT = re.compile(r'(?<![A-Za-z0-9])(\d+(?:点\d+)?)%')
_RE_CELSIUS = re.compile(r'(?<=\d)\s*(?:℃|°C|°c)')
_RE_CHINESE_UNIT_SLASH = re.compile(r'(?<=[\u4e00-\u9fa5])/(?=[\u4e00-\u9fa5])')


def clean_text_for_tts(text: str) -> str:
    """
    Clean model text and normalize formats that are awkward for speech.
    """
    if not text:
        return text

    text = _RE_CODE_BLOCK.sub('', text)
    text = _RE_INLINE_CODE.sub('', text)
    text = _RE_LINK.sub(r'\1', text)
    text = _RE_HEADING.sub('', text)
    text = _RE_BOLD1.sub(r'\1', text)
    text = _RE_ITALIC1.sub(r'\1', text)
    text = _RE_BOLD2.sub(r'\1', text)
    text = _RE_ITALIC2.sub(r'\1', text)
    text = _RE_STRIKETHROUGH.sub(r'\1', text)
    text = _RE_LIST_DASH.sub('', text)
    text = _RE_LIST_NUM.sub('', text)

    text = _RE_CELSIUS.sub('摄氏度', text)
    text = _RE_DECIMAL_NUMBER.sub(r'\1点\2', text)
    text = _RE_PERCENT.sub(r'百分之\1', text)
    text = _RE_CHINESE_UNIT_SLASH.sub('每', text)

    text = _RE_EMOTICON.sub('', text)
    text = _RE_EMOJI.sub('', text)
    text = _RE_SPECIAL_CHARS.sub('', text)
    text = _RE_MULTI_NEWLINE.sub('\n', text)
    text = _RE_MULTI_SPACE.sub(' ', text)
    return text.strip()
