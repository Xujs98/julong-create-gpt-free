from datetime import date

import pytest

from core.profile_utils import (
    MIN_REGISTRATION_AGE,
    calculate_age,
    generate_random_birthday,
    validate_registration_birthday,
)


def test_generated_birthday_is_strictly_over_eighteen():
    today = date.today()
    birthday = generate_random_birthday()
    assert calculate_age(birthday, today=today) >= MIN_REGISTRATION_AGE


def test_exactly_eighteen_is_rejected():
    with pytest.raises(ValueError, match="大于18岁"):
        validate_registration_birthday("2008-08-08", today=date(2026, 8, 8))


def test_nineteen_on_birthday_is_accepted():
    assert validate_registration_birthday("2007-08-08", today=date(2026, 8, 8)) == "2007-08-08"


def test_generator_rejects_minimum_age_eighteen():
    with pytest.raises(ValueError):
        generate_random_birthday(min_age=18)
