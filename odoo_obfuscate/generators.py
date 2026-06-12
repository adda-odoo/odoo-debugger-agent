"""Value generators for anonymization.

No seeded Faker — in-memory identity maps only. MT19937 (Mersenne Twister)
is invertible after 624 observed outputs; seeded runs are explicitly forbidden.
"""
from __future__ import annotations

import json
import random
import string
from datetime import date, timedelta

try:
    from faker import Faker  # type: ignore[import]
except ImportError:
    class Faker:
        def __init__(self, locale: str | None = None):
            self.locale = locale

        def name(self) -> str:
            return "John Doe"

        def company(self) -> str:
            return "Acme Corporation"

        def first_name(self) -> str:
            return "John"

        def last_name(self) -> str:
            return "Doe"

        def phone_number(self) -> str:
            return "000-000-0000"

        def street_address(self) -> str:
            return "123 Main St"

        def secondary_address(self) -> str:
            return "Apt 1"

        def postcode(self) -> str:
            return "00000"

        def city(self) -> str:
            return "Anytown"

        def iban(self) -> str:
            return "XX00TEST000000000000"

        def ssn(self) -> str:
            return "000-00-0000"

_faker = Faker()


def configure_locale(locale: str) -> None:
    """Reinitialize the shared Faker instance with a different locale."""
    global _faker
    _faker = Faker(locale)


# ---------------------------------------------------------------------------
# Primitive generators
# ---------------------------------------------------------------------------

def full_name() -> str:
    return _faker.name()


def company_name() -> str:
    return _faker.company()


def product_name(row_id: int) -> str:
    return f"Test Product {row_id}"


def email(row_id: int) -> str:
    first = "".join(c for c in _faker.first_name().lower() if c.isalpha()) or "user"
    last = "".join(c for c in _faker.last_name().lower() if c.isalpha()) or "anon"
    return f"{first}.{last}.{row_id}@anon.example.com"


def phone() -> str:
    return _faker.phone_number()


def street() -> str:
    return _faker.street_address()


def street2() -> str:
    return _faker.secondary_address()


def zip_code() -> str:
    return _faker.postcode()


def city() -> str:
    return _faker.city()


def vat(row_id: int) -> str:
    digits = "".join(random.choices(string.digits, k=8))
    return f"XX{digits}{row_id}"


def website(row_id: int) -> str:
    return f"https://www.anon-{row_id}.example.com"


def iban() -> str:
    return _faker.iban()


def ssn() -> str:
    return _faker.ssn()


def passport(row_id: int) -> str:
    chars = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{chars}{row_id}"


def barcode(row_id: int) -> str:
    digits = "".join(random.choices(string.digits, k=8))
    return f"{digits}{row_id}"


def alphanum(length: int = 12, row_id: int | None = None) -> str:
    chars = "".join(random.choices(string.ascii_letters + string.digits, k=length))
    if row_id is not None:
        return f"{chars}{row_id}"
    return chars


def date_shift(original: date | None, max_days: int = 180) -> date | None:
    if original is None:
        return None
    return original + timedelta(days=random.randint(-max_days, max_days))


# ---------------------------------------------------------------------------
# JSONB helper
# ---------------------------------------------------------------------------

def wrap_jsonb(value: str | None, keys: list[str]) -> str:
    """Wrap a plain string as a JSONB object, replicating it across all locale keys."""
    return json.dumps({k: value for k in keys})


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def generate(
    props: dict,
    row_id: int,
    original=None,
    identity=None,
) -> object:
    """Return a generated value for props. None means SQL NULL (type: clear)."""
    gtype = props.get("type", "clear")

    if gtype == "clear":
        return None
    if gtype == "literal":
        return props["value"]
    if gtype == "full_name":
        return full_name()
    if gtype == "company_name":
        return company_name()
    if gtype == "product_name":
        return product_name(row_id)
    if gtype == "email":
        return email(row_id)
    if gtype == "phone":
        return phone()
    if gtype == "street":
        return street()
    if gtype == "street2":
        return street2()
    if gtype == "zip":
        return zip_code()
    if gtype == "city":
        return city()
    if gtype == "vat":
        return vat(row_id)
    if gtype == "website":
        return website(row_id)
    if gtype == "iban":
        return iban()
    if gtype == "ssn":
        return ssn()
    if gtype == "passport":
        return passport(row_id)
    if gtype == "barcode":
        return barcode(row_id)
    if gtype == "alphanum":
        uid = row_id if props.get("unique_suffix") else None
        return alphanum(length=props.get("length", 12), row_id=uid)
    if gtype == "date_shift":
        return date_shift(original, max_days=props.get("max_days", 180))
    if gtype == "from_partner":
        if identity is not None:
            return identity.email
        return f"user.{row_id}@anon.example.com"

    raise ValueError(f"Unknown generator type: {gtype!r}")
