"""Pre-built partner identities for cross-table consistency.

res_users.login must equal res_partner.email for the same person.
Building identities first ensures both fields get the same generated value.
"""
from __future__ import annotations

from dataclasses import dataclass

import generators as gen


@dataclass
class PartnerIdentity:
    id: int
    name: str
    email: str
    phone: str
    mobile: str
    street: str
    street2: str
    zip: str
    city: str
    vat: str | None
    website: str | None


def build_partner_identities(conn) -> dict[int, PartnerIdentity]:
    """Build a fake identity for every res_partner row."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, is_company FROM res_partner ORDER BY id")
        rows = cur.fetchall()
    result: dict[int, PartnerIdentity] = {}
    for row_id, is_company in rows:
        result[row_id] = PartnerIdentity(
            id=row_id,
            name=gen.company_name() if is_company else gen.full_name(),
            email=gen.email(row_id),
            phone=gen.phone(),
            mobile=gen.phone(),
            street=gen.street(),
            street2=gen.street2(),
            zip=gen.zip_code(),
            city=gen.city(),
            vat=gen.vat(row_id) if is_company else None,
            website=gen.website(row_id) if is_company else None,
        )
    return result
