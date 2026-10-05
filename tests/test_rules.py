"""Rules tests built from real Samiad cases (figures from the Oct 2026 audit)."""
from decimal import Decimal as D

import pytest

from samiad import rules as r
from samiad.rules import Action, Basis, Chain, ChainDoc, Student

LIMIT = D("1000")


def student(cid="1", net="1350", gross="1900", insurance="12", transfer="0", ensuite="0",
            minor="0", online="0", total_net=None, total_gross=None, transfers="Return Transfers"):
    addons = sum(D(x) for x in (insurance, transfer, ensuite, minor, online))
    return Student(cid, {
        "firstname": "Test", "lastname": f"Student{cid}", "course": "General English",
        "campus": "The Leys School",
        "net_price__": net, "gross_price__": gross,
        "insurance_fee__": insurance, "airport_transfer_fee": transfer,
        "ensuite_supplement": ensuite, "unaccompanied_minor_fee": minor,
        "pre_post_online_course__": online,
        "total_net_fee": total_net if total_net is not None else str(D(net) + addons),
        "gross_fee__total_": total_gross if total_gross is not None else str(D(gross) + addons),
        "airport_transfers": transfers,
    })


# ---- rule 1 ---------------------------------------------------------------
def test_b2c_is_always_net_even_if_agent_says_gross():
    assert r.billing_basis(r.PIPELINE_B2C_SALES, "Gross") is Basis.NET
    assert r.billing_basis(r.PIPELINE_B2C_RETURNER, None) is Basis.NET


def test_b2b_follows_agent_basis():
    assert r.billing_basis(r.PIPELINE_B2B_NEW, "Gross") is Basis.GROSS
    assert r.billing_basis(r.PIPELINE_B2B_EXISTING, "net") is Basis.NET


def test_ministay_always_net_even_for_gross_or_unset_agents():
    assert r.billing_basis(r.PIPELINE_MINISTAY, "Gross") is Basis.NET
    assert r.billing_basis(r.PIPELINE_MINISTAY, None) is Basis.NET


def test_b2b_without_basis_is_blocked():
    with pytest.raises(r.BlockedError):
        r.billing_basis(r.PIPELINE_B2B_NEW, None)


# ---- lines ----------------------------------------------------------------
def test_tom_pasqual_net_lines_match_hubspot_total():
    # contact 876237513956: net 4434.75 + insurance 42 = 4476.75
    s = student(net="4434.75", gross="5475", insurance="42")
    lines = r.booking_lines([s], Basis.NET)
    assert r.total_of(lines) == D("4476.75")
    assert [l.kind for l in lines] == ["course", "insurance_fee__"]


def test_group_sums_students():
    group = [student("1"), student("2"), student("3", net="650", gross="650")]
    assert r.total_of(r.booking_lines(group, Basis.GROSS)) == D("1912") * 2 + D("662")


def test_fees_not_adding_up_blocks_invoicing():
    s = student(total_net="9999")
    with pytest.raises(r.BlockedError):
        r.booking_lines([s], Basis.NET)


def test_no_students_blocks():
    with pytest.raises(r.BlockedError):
        r.booking_lines([], Basis.NET)


# ---- rules 2 + 3 ----------------------------------------------------------
def lines_for(total):
    return [r.Line("x", D(total), "course", "1")]


def test_new_booking_creates_base():
    d = r.decide(Chain("513409122543"), lines_for("2684"), LIMIT)
    assert d.action is Action.CREATE_BASE and d.amount == D("2684")


def test_unpaid_price_change_edits_base():
    chain = Chain("1", [ChainDoc("1", D("2684"))])
    d = r.decide(chain, lines_for("3000"), LIMIT)
    assert d.action is Action.EDIT_BASE and d.amount == D("3000") and not d.needs_approval


def test_unpaid_change_folds_unpaid_topups_back_in():
    chain = Chain("1", [ChainDoc("1", D("1000")), ChainDoc("1-1", D("200"))])
    d = r.decide(chain, lines_for("1300"), LIMIT)
    assert d.action is Action.EDIT_BASE and d.void_topups == ["1-1"]


def test_price_rise_after_part_payment_raises_topup():
    chain = Chain("1", [ChainDoc("1", D("2684"), paid=D("1342"))])
    d = r.decide(chain, lines_for("2900"), LIMIT)
    assert d.action is Action.TOP_UP and d.amount == D("216")
    assert chain.next_number(credit=False) == "1-1"


def test_price_drop_after_part_payment_raises_credit():
    chain = Chain("1", [ChainDoc("1", D("2684"), paid=D("1342"))])
    d = r.decide(chain, lines_for("2500"), LIMIT)
    assert d.action is Action.CREDIT and d.amount == D("184") and not d.needs_approval
    assert chain.next_number(credit=True) == "1-CN1"


def test_credit_bigger_than_balance_needs_approval_refund():
    chain = Chain("1", [ChainDoc("1", D("2684"), paid=D("2684"))])
    d = r.decide(chain, lines_for("2000"), LIMIT)
    assert d.action is Action.CREDIT and d.needs_approval
    assert any("refund" in reason for reason in d.reasons)


def test_big_change_needs_approval():
    chain = Chain("1", [ChainDoc("1", D("2000"), paid=D("1000"))])
    d = r.decide(chain, lines_for("3500"), LIMIT)
    assert d.action is Action.TOP_UP and d.needs_approval


def test_pascaline_case_never_creates_second_base():
    # Gross £3,334 paid in full; a re-run must not raise anything new.
    chain = Chain("513409122543", [ChainDoc("513409122543", D("3334"), paid=D("3334"))])
    d = r.decide(chain, lines_for("3334"), LIMIT)
    assert d.action is Action.NONE


def test_voided_docs_ignored_but_numbers_not_reused():
    chain = Chain("1", [ChainDoc("1", D("1000"), paid=D("500")),
                        ChainDoc("1-1", D("50"), status="VOIDED")])
    assert chain.invoiced == D("1000")
    assert chain.next_number(credit=False) == "1-2"


def test_same_total_but_lines_changed_and_unpaid_edits():
    chain = Chain("1", [ChainDoc("1", D("1000"))])
    d = r.decide(chain, lines_for("1000"), LIMIT, base_lines_match=False)
    assert d.action is Action.EDIT_BASE


# ---- rule 5 / status ------------------------------------------------------
def test_status_and_half_paid():
    chain = Chain("1", [ChainDoc("1", D("2000"), paid=D("999.99"))])
    assert r.payment_status(chain) is r.PaymentStatus.PART_PAID and not r.half_paid(chain)
    chain.docs[0].paid = D("1000")
    assert r.half_paid(chain)
    chain.docs[0].paid = D("2000")
    assert r.payment_status(chain) is r.PaymentStatus.PAID


def test_credit_note_counts_towards_paid_off():
    chain = Chain("1", [ChainDoc("1", D("2684"), paid=D("2500"), credited=D("184")),
                        ChainDoc("1-CN1", D("184"), is_credit_note=True)])
    assert chain.invoiced == D("2500")
    assert r.payment_status(chain) is r.PaymentStatus.PAID


def test_transfers_booked_flag():
    assert student(transfers="Return Transfers").transfers_booked
    assert not student(transfers="Not Required").transfers_booked
