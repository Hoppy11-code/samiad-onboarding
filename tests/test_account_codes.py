"""Each add-on lands on its own Xero income account."""
from decimal import Decimal

from samiad.clients.xero import Xero
from samiad.config import DEFAULT_ACCOUNT_CODES
from samiad.rules import Line


def codes_for(lines, ministay=False):
    return [i["AccountCode"] for i in Xero._line_items(lines, DEFAULT_ACCOUNT_CODES, ministay)]


def test_addons_have_their_own_income_accounts():
    lines = [Line("Kid - General English", Decimal("1350"), "course", "s1"),
             Line("Kid - Insurance", Decimal("12"), "insurance_fee__", "s1"),
             Line("Kid - Pre/post online English course", Decimal("90"), "pre_post_online_course__", "s1"),
             Line("Kid - Airport transfers", Decimal("120"), "airport_transfer_fee", "s1"),
             Line("Kid - Ensuite supplement", Decimal("60"), "ensuite_supplement", "s1")]
    assert codes_for(lines) == ["203", "207", "208", "209", "203"]


def test_ministay_course_still_on_ministay_sales_but_addons_split_out():
    lines = [Line("Kid - Ministay", Decimal("900"), "course", "s1"),
             Line("Kid - Insurance", Decimal("12"), "insurance_fee__", "s1")]
    assert codes_for(lines, ministay=True) == ["205", "207"]
