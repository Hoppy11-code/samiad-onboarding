"""Render sample documents from a made-up two-student booking into out/ (for checking templates)."""
from decimal import Decimal as D
from pathlib import Path

from samiad import documents as docs
from samiad.rules import Basis, Student

OUT = Path(__file__).resolve().parent.parent / "out"


def st(cid, first, last, net, gross, ins="12", transfer="0", transfers="Return Transfers",
       ensuite="0", course="General English", spec="Multi-Activity"):
    add = D(ins) + D(transfer) + D(ensuite)
    return Student(cid, {
        "firstname": first, "lastname": last, "date_of_birth": "9/21/2009", "nationality": "Algerian",
        "passport_number": "X1234567", "course": course, "specialism": spec, "campus": "The Leys School",
        "arrival_dats": "2027-07-22", "departure_date": "2027-07-29", "net_price__": net,
        "gross_price__": gross, "insurance_fee__": ins, "airport_transfer_fee": transfer,
        "airport_transfers": transfers, "ensuite_supplement": ensuite, "unaccompanied_minor_fee": "0",
        "pre_post_online_course__": "0", "total_net_fee": str(D(net) + add),
        "gross_fee__total_": str(D(gross) + add)})


def main():
    OUT.mkdir(exist_ok=True)
    studs = [st("1", "Sample", "Student One", "1350", "1900"),
             st("2", "Sample", "Student Two", "1350", "1900", ins="0", transfers="Not Required",
                ensuite="150", spec="")]
    for basis in (Basis.NET, Basis.GROSS):
        tot = sum((s.stated_total(basis) for s in studs), D(0))
        b = docs.BookingDocs("123456789012", "Sample group", basis, studs, "Sample Agency Ltd",
                             "Sample Salesperson", "sales@samiad.com", tot, D("1000"), tot - D("1000"))
        sfx = basis.value.lower()
        for name, data in (("confirmation", docs.confirmation_docx(b)), ("receipt", docs.receipt_docx(b)),
                           ("visa1", docs.visa_docx(b, 0)), ("visa2", docs.visa_docx(b, 1))):
            (OUT / f"{name}_{sfx}.docx").write_bytes(data)
            (OUT / f"{name}_{sfx}.pdf").write_bytes(docs.to_pdf(data))
    print("written to", OUT)


if __name__ == "__main__":
    main()
