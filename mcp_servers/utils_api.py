#!/usr/bin/env python3
"""
工具集 API - 时间、日期、星期、农历、节假日

纯 Python 实现，无需外部 API：
- datetime（标准库）：时间、日期、星期
- zhdate：农历
- chinese_calendar：中国法定节假日
"""

from datetime import datetime, date, timezone, timedelta
from typing import Optional

# 星期中文
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 默认时区：东八区
DEFAULT_TZ = timezone(timedelta(hours=8))


def _now(tz: Optional[str] = None) -> datetime:
    """获取当前时间，支持时区名（如 Asia/Shanghai），默认东八区"""
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz))
        except Exception:
            pass
    return datetime.now(DEFAULT_TZ)


def _to_local_naive(dt: datetime) -> datetime:
    """将带时区时间转为同一本地时钟语义的 naive datetime。"""
    if dt.tzinfo is None:
        return dt
    return dt.replace(tzinfo=None)


def get_current_time(tz: Optional[str] = None) -> str:
    """当前时间（时分秒）"""
    dt = _now(tz)
    return dt.strftime("%H:%M:%S")


def get_current_date(tz: Optional[str] = None) -> str:
    """当前日期（年月日）"""
    dt = _now(tz)
    return dt.strftime("%Y年%m月%d日")


def get_weekday(tz: Optional[str] = None) -> str:
    """今天星期几（中文）"""
    dt = _now(tz)
    # weekday(): 0=周一, 6=周日
    return WEEKDAY_CN[dt.weekday()]


def get_datetime_full(tz: Optional[str] = None) -> str:
    """当前完整日期时间（日期 + 时间 + 星期）"""
    dt = _now(tz)
    return f"{dt.strftime('%Y年%m月%d日 %H:%M:%S')} {WEEKDAY_CN[dt.weekday()]}"


def get_now_context(tz: Optional[str] = None) -> str:
    """一次性返回当前时间上下文：日期、时间、星期、农历、节假日。"""
    dt = _now(tz)
    parts = [
        f"现在是 {dt.strftime('%Y年%m月%d日 %H:%M:%S')}，{WEEKDAY_CN[dt.weekday()]}。",
    ]

    lunar_text = get_lunar_date(tz)
    if lunar_text and not lunar_text.startswith(("未安装", "查询失败")):
        parts.append(f"农历是 {lunar_text}。")

    holiday_text = is_holiday_today(tz)
    if holiday_text and not holiday_text.startswith(("未安装", "查询失败")):
        parts.append(f"今天{holiday_text}。")

    return " ".join(parts)


def get_lunar_date(tz: Optional[str] = None) -> str:
    """当前日期对应的农历"""
    try:
        from zhdate import ZhDate
    except ImportError:
        return "未安装 zhdate 库，无法查询农历。请执行: pip install zhdate"

    try:
        dt = _to_local_naive(_now(tz))
        z = ZhDate.from_datetime(dt)
        return z.chinese()  # 如 "二零二五年正月初四 乙巳年(蛇年)"
    except Exception as e:
        return f"查询失败：{e}"


def is_holiday_today(tz: Optional[str] = None) -> str:
    """今天是否为中国法定节假日/调休，并返回节日名称（如有）"""
    try:
        import chinese_calendar as cc
    except ImportError:
        return "未安装 chinesecalendar 库，无法查询节假日。请执行: pip install chinesecalendar"

    dt = _now(tz)
    d = date(dt.year, dt.month, dt.day)
    try:
        is_holiday = cc.is_holiday(d)
        detail = cc.get_holiday_detail(d)
        if detail[0]:  # 是节假日
            name = detail[1] or "法定节假日"
            return f"是，今天为：{name}"
        if cc.is_in_lieu(d):  # 调休补班
            return "否，今天为调休工作日（补班）"
        return "否，今天为工作日"
    except Exception as e:
        return f"查询失败（可能超出支持年份）：{e}"


def format_timestamp(ts: float, tz: Optional[str] = None) -> str:
    """将 Unix 时间戳转为可读的日期时间"""
    try:
        utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        if tz:
            from zoneinfo import ZoneInfo
            utc = utc.astimezone(ZoneInfo(tz))
        else:
            utc = utc.astimezone(DEFAULT_TZ)
        return utc.strftime("%Y年%m月%d日 %H:%M:%S")
    except Exception as e:
        return f"无效时间戳或转换失败: {e}"
