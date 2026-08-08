# -*- coding: utf-8 -*-
"""注册资料生成工具。"""

from __future__ import annotations

import random
from datetime import date, timedelta


# 注册年龄要求：年龄必须严格大于 18 岁，因此最低有效年龄为 19 岁。
MIN_REGISTRATION_AGE = 19


def _shift_year_safe(day: date, years: int) -> date:
    """按年偏移日期；遇到 2 月 29 日且目标年非闰年时回退到 2 月 28 日。"""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, month=2, day=28)


def calculate_age(birthday: str, today: date | None = None) -> int:
    """按指定日期计算周岁，统一处理生日当天和闰年边界。"""
    try:
        born = date.fromisoformat(str(birthday).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"生日格式应为 YYYY-MM-DD: {birthday!r}") from exc
    current = today or date.today()
    if born > current:
        raise ValueError(f"生日不能晚于今天: {birthday}")
    return current.year - born.year - ((current.month, current.day) < (born.month, born.day))


def validate_registration_birthday(
    birthday: str,
    *,
    min_age: int = MIN_REGISTRATION_AGE,
    today: date | None = None,
) -> str:
    """校验注册生日满足最低年龄，并返回标准化 YYYY-MM-DD 字符串。"""
    normalized = date.fromisoformat(str(birthday).strip()).isoformat()
    age = calculate_age(normalized, today=today)
    if age < min_age:
        raise ValueError(f"注册年龄必须大于18岁，当前计算年龄为 {age} 岁")
    return normalized


def generate_random_birthday(min_age: int = MIN_REGISTRATION_AGE, max_age: int = 65) -> str:
    """
    生成年龄在 [min_age, max_age] 闭区间内的随机生日，格式 YYYY-MM-DD。

    例如默认会在“今天满 65 岁”到“今天满 19 岁”之间随机取一天。
    """
    if min_age < MIN_REGISTRATION_AGE or max_age < min_age:
        raise ValueError(f"年龄范围无效: min_age={min_age}, max_age={max_age}")

    today = date.today()
    oldest = _shift_year_safe(today, -max_age)
    youngest = _shift_year_safe(today, -min_age)
    span_days = (youngest - oldest).days
    birthday = oldest + timedelta(days=random.randint(0, span_days))
    return birthday.isoformat()
