"""The booking rules. Pure functions only: no network, no side effects.

Everything that decides money lives here so it can be tested in isolation.
See the build spec, "Core rules".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

ZERO = Decimal("0")
PENNY = Decimal("0.01")

# HubSpot pipeline ids (verified against the portal, Oct 2026)
PIPELINE_B2C_SALES = "default"
PIPELINE_B2C_RETURNER = "2534212796"
PIPELINE_B2B_NEW = "2322519286"
PIPELINE_B2B_EXISTING = "2527046857"
PIPELINE_MINISTAY = "2568452285"

B2C_PIPELINES = {PIPELINE_B2C_SALES, PIPELINE_B2C_RETURNER}
B2B_PIPELINES = {PIPELINE_B2B_NEW, PIPELINE_B2B_EXISTING, PIPELINE_MINISTAY}


def money(value) -> Decimal:
    """HubSpot numbers arrive as strings, floats or None."""
    if value in (None, ""):
        return ZERO
    return Decimal(str(value)).quantize(PENNY, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Students and add-ons
# --------------------------------------------------------------------------
# (HubSpot contact property, label on documents, Xero line description)
ADDONS = [
    ("airport_transfer_fee", "Airport Transfers", "Airport transfers"),
    ("insurance_fee__", "Insurance Fee", "Insurance"),
    ("pre_post_online_course__", "Online Course Fee", "Pre/post online English course"),
    ("ensuite_supplement", "Ensuite Supplement", "Ensuite supplement"),
    ("unaccompanied_minor_fee", "Unaccompanied Minor Fee", "Unaccompanied minor service"),
]
ALWAYS_SHOWN = {"airport_transfer_fee", "insurance_fee__"}
TRANSFERS_BOOKED = {"Return Transfers", "Arrival Only", "Departure Only"}


@dataclass
class Student:
    contact_id: str
    props: dict

    def p(self, name):
        return self.props.get(name)

    @property
    def name(self) -> str:
        return f"{self.p('firstname') or ''} {self.p('lastname') or ''}".strip()

    def course_price(self, basis: "Basis") -> Decimal:
        return money(self.p("gross_price__" if basis is Basis.GROSS else "net_price__"))

    def addon(self, prop: str) -> Decimal:
        return money(self.p(prop))

    def stated_total(self, basis: "Basis") -> Decimal:
        return money(self.p("gross_fee__total_" if basis is Basis.GROSS else "total_net_fee"))

    def computed_total(self, basis: "Basis") -> Decimal:
        return self.course_price(basis) + sum((self.addon(p) for p, _, _ in ADDONS), ZERO)

    @property
    def transfers_booked(self) -> bool:
        return (self.p("airport_transfers") or "") in TRANSFERS_BOOKED


# --------------------------------------------------------------------------
# Rule 1: net or gross, never chosen by hand
# --------------------------------------------------------------------------
class Basis(Enum):
    NET = "Net"
    GROSS = "Gross"


class BlockedError(Exception):
    """A booking can't be invoiced until a person fixes something."""


def billing_basis(pipeline: str, agent_billing_basis: str | None) -> Basis:
    if pipeline in B2C_PIPELINES:
        return Basis.NET
    if pipeline in B2B_PIPELINES:
        value = (agent_billing_basis or "").strip().lower()
        if value == "net":
            return Basis.NET
        if value == "gross":
            return Basis.GROSS
        raise BlockedError("Agent has no billing basis (Net/Gross) set on the company record")
    raise BlockedError(f"Unknown pipeline {pipeline!r}")


# --------------------------------------------------------------------------
# What the booking should cost, as invoice lines
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Line:
    description: str
    amount: Decimal
    kind: str  # "course" or the add-on property name
    student_id: str


def booking_lines(students: list[Student], basis: Basis) -> list[Line]:
    """One course line per student plus one line per non-zero add-on.

    Raises BlockedError if a student's fee fields don't add up to the total
    HubSpot shows for them: better to stop than invoice a wrong amount.
    """
    if not students:
        raise BlockedError("No students are attached to this deal")
    lines: list[Line] = []
    for s in students:
        if s.computed_total(basis) != s.stated_total(basis):
            raise BlockedError(
                f"{s.name}: fees add up to £{s.computed_total(basis)} but the student's "
                f"total shows £{s.stated_total(basis)}"
            )
        course = s.p("course") or "Summer course"
        nights = s.p("_nights")
        desc = f"{s.name} - {course}, {s.p('campus') or ''}"
        if nights:
            desc += f", {nights} nights"
        lines.append(Line(desc, s.course_price(basis), "course", s.contact_id))
        for prop, _label, xero_desc in ADDONS:
            amt = s.addon(prop)
            if amt != ZERO:
                lines.append(Line(f"{s.name} - {xero_desc}", amt, prop, s.contact_id))
    return lines


def total_of(lines: list[Line]) -> Decimal:
    return sum((l.amount for l in lines), ZERO)


# --------------------------------------------------------------------------
# Rules 2 + 3: one invoice chain, edit if unpaid else top-up / credit
# --------------------------------------------------------------------------
@dataclass
class ChainDoc:
    number: str
    total: Decimal
    paid: Decimal = ZERO
    credited: Decimal = ZERO
    status: str = "AUTHORISED"  # Xero status
    is_credit_note: bool = False
    xero_id: str = ""


@dataclass
class Chain:
    deal_id: str
    docs: list[ChainDoc] = field(default_factory=list)

    @property
    def live(self) -> list[ChainDoc]:
        return [d for d in self.docs if d.status not in ("VOIDED", "DELETED")]

    @property
    def base(self) -> ChainDoc | None:
        for d in self.live:
            if not d.is_credit_note and d.number == self.deal_id:
                return d
        return None

    @property
    def topups(self) -> list[ChainDoc]:
        return [d for d in self.live if not d.is_credit_note and d.number != self.deal_id]

    @property
    def credit_notes(self) -> list[ChainDoc]:
        return [d for d in self.live if d.is_credit_note]

    @property
    def invoiced(self) -> Decimal:
        inv = sum((d.total for d in self.live if not d.is_credit_note), ZERO)
        return inv - sum((d.total for d in self.credit_notes), ZERO)

    @property
    def paid(self) -> Decimal:
        return sum((d.paid for d in self.live if not d.is_credit_note), ZERO)

    @property
    def any_payment(self) -> bool:
        return self.paid > ZERO or any(d.credited > ZERO for d in self.live if not d.is_credit_note)

    @property
    def outstanding(self) -> Decimal:
        return self.invoiced - self.paid

    def next_number(self, credit: bool) -> str:
        prefix = f"{self.deal_id}-CN" if credit else f"{self.deal_id}-"
        used = []
        for d in self.docs:  # include voided numbers: Xero won't reuse them cleanly
            if d.number.startswith(prefix):
                tail = d.number[len(prefix):]
                if tail.isdigit():
                    used.append(int(tail))
        return f"{prefix}{max(used, default=0) + 1}"


class Action(Enum):
    CREATE_BASE = "create base invoice"
    EDIT_BASE = "edit base invoice"
    TOP_UP = "raise top-up invoice"
    CREDIT = "raise credit note"
    NONE = "no change"


@dataclass
class Decision:
    action: Action
    amount: Decimal = ZERO  # top-up / credit amount, or new base total
    void_topups: list[str] = field(default_factory=list)
    needs_approval: bool = False
    reasons: list[str] = field(default_factory=list)


def decide(chain: Chain, target_lines: list[Line], approval_threshold: Decimal,
           base_lines_match: bool = True) -> Decision:
    """Decide what to do so the chain totals what the booking should cost.

    base_lines_match: whether the base invoice's current lines already equal
    target_lines (lets an unpaid invoice be corrected when a fee moves
    between lines but the total stays the same).
    """
    target = total_of(target_lines)
    if chain.base is None:
        return Decision(Action.CREATE_BASE, target)

    diff = target - chain.invoiced
    if not chain.any_payment and not chain.credit_notes:
        # Nothing paid: keep one clean invoice. Fold any unpaid top-ups back in.
        if diff == ZERO and not chain.topups and base_lines_match:
            return Decision(Action.NONE)
        d = Decision(Action.EDIT_BASE, target, void_topups=[t.number for t in chain.topups])
        if abs(diff) > approval_threshold:
            d.needs_approval = True
            d.reasons.append(f"change of £{abs(diff)} is over the £{approval_threshold} limit")
        return d

    if diff == ZERO:
        return Decision(Action.NONE)
    if diff > ZERO:
        d = Decision(Action.TOP_UP, diff)
    else:
        d = Decision(Action.CREDIT, -diff)
        if -diff > chain.outstanding:
            d.needs_approval = True
            d.reasons.append(
                f"credit of £{-diff} is more than the £{chain.outstanding} still owed, "
                "so a refund would be due"
            )
    if abs(diff) > approval_threshold:
        d.needs_approval = True
        d.reasons.append(f"change of £{abs(diff)} is over the £{approval_threshold} limit")
    return d


# --------------------------------------------------------------------------
# Payment status and the 50% trigger (rule 5)
# --------------------------------------------------------------------------
class PaymentStatus(Enum):
    UNPAID = "Unpaid"
    PART_PAID = "Part paid"
    PAID = "Paid"


def payment_status(chain: Chain) -> PaymentStatus:
    if chain.invoiced > ZERO and chain.outstanding <= ZERO:
        return PaymentStatus.PAID
    if chain.paid > ZERO:
        return PaymentStatus.PART_PAID
    return PaymentStatus.UNPAID


def half_paid(chain: Chain) -> bool:
    return chain.invoiced > ZERO and chain.paid * 2 >= chain.invoiced
