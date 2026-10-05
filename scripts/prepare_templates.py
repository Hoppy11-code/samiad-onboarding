"""Turn Samiad's Word templates into tagged templates the service can fill.

Reads the originals in templates/source/ (left untouched) and writes tagged
copies to templates/. Run it again whenever a source template changes:

    python scripts/prepare_templates.py

What it does to each template:
  * swaps every <placeholder> for a template tag ({{ ... }}), even when Word
    has split the placeholder across several formatting runs;
  * adds loops so group bookings get one block / row per student;
  * fixes the problems found in review (hard-coded Box Hill School, the
    "Net Fee" label on the gross confirmation, the misaligned receipt,
    hard-coded names), keeping the original styling.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import docx
from docx.oxml.ns import qn

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "templates" / "source"
OUT = ROOT / "templates"

PLACEHOLDER = re.compile(r"<[^<>]{1,60}>")

# Placeholder text (lower-cased, spaces collapsed) -> template expression.
# "s." = the current student inside a per-student loop.
COMMON = {
    "guardian/agent name": "customer_name",
    "deal id": "deal_id",
    "record id": "deal_id",
    "today date": "today",
    "sales person": "owner_name",
    "sales person email": "owner_email",
    "first name": "s.first_name",
    "surname": "s.last_name",
    "last name": "s.last_name",
    "date of birth": "s.date_of_birth",
    "nationality": "s.nationality",
    "passport number": "s.passport_number",
    "arrival date": "s.arrival_date",
    "date of arrival": "s.arrival_date",
    "departure date": "s.departure_date",
    "campus": "s.campus",
    "campus address": "s.campus_address",
    "course": "s.course",
    "specialism": "s.specialism",
    "number of nights": "s.nights",
    "no of nights": "s.nights",
}
TOTALS = {
    "total net fee": "total",
    "total gross fee": "total",
    "total net price": "s.total",
    "total gross price": "s.total",
    "total received": "received",
    "outstanding net fees": "outstanding",
    "outstanding gross fees": "outstanding",
}


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip("<>").strip().lower())


def expression_for(placeholder: str) -> str | None:
    k = _key(placeholder)
    return COMMON.get(k) or TOTALS.get(k)


def replace_in_paragraph(p, func) -> None:
    """Replace <placeholders> in a paragraph even when split across runs.

    The replacement text goes into the run where the placeholder starts, so
    that run's formatting is kept; the other runs keep their own text.
    """
    runs = p.runs
    if not runs:
        return
    full = "".join(r.text for r in runs)
    matches = list(PLACEHOLDER.finditer(full))
    if not matches:
        return
    # character offset -> run index
    bounds, pos = [], 0
    for r in runs:
        bounds.append((pos, pos + len(r.text)))
        pos += len(r.text)

    def run_at(offset):
        for i, (a, b) in enumerate(bounds):
            if a <= offset < b:
                return i
        return len(runs) - 1

    texts = [r.text for r in runs]
    # work backwards so earlier offsets stay valid
    for m in reversed(matches):
        repl = func(m.group(0))
        if repl is None:
            continue
        s_run, e_run = run_at(m.start()), run_at(m.end() - 1)
        s_off = m.start() - bounds[s_run][0]
        e_off = m.end() - bounds[e_run][0]
        if s_run == e_run:
            t = texts[s_run]
            texts[s_run] = t[:s_off] + repl + t[e_off:]
        else:
            texts[s_run] = texts[s_run][:s_off] + repl
            for i in range(s_run + 1, e_run):
                texts[i] = ""
            texts[e_run] = texts[e_run][e_off:]
    for r, t in zip(runs, texts):
        if r.text != t:
            r.text = t


def tag(placeholder: str) -> str | None:
    expr = expression_for(placeholder)
    return "{{ " + expr + " }}" if expr else None


def all_paragraphs(doc):
    """Every paragraph in body, tables (each cell once) and headers/footers."""
    seen = []

    def cell_pars(table):
        for row in table.rows:
            for cell in row.cells:
                if any(cell._tc is x for x in seen):
                    continue
                seen.append(cell._tc)
                yield from cell.paragraphs
                for t in cell.tables:
                    yield from cell_pars(t)

    yield from doc.paragraphs
    for t in doc.tables:
        yield from cell_pars(t)
    for s in doc.sections:
        for part in (s.header, s.footer):
            yield from part.paragraphs
            for t in part.tables:
                yield from cell_pars(t)


def set_text(p, text: str) -> None:
    """Replace a paragraph's text, keeping the first run's formatting."""
    runs = p.runs
    if not runs:
        p.add_run(text)
        return
    runs[0].text = text
    for r in runs[1:]:
        r._r.getparent().remove(r._r)


def new_paragraph_like(p, text: str, before: bool = True):
    """Insert a copy of paragraph p (same style) holding only `text`."""
    el = copy.deepcopy(p._p)
    if before:
        p._p.addprevious(el)
    else:
        p._p.addnext(el)
    new = docx.text.paragraph.Paragraph(el, p._parent)
    set_text(new, text)
    return new


def unique_cells(row):
    out = []
    for c in row.cells:
        if not any(c._tc is x._tc for x in out):
            out.append(c)
    return out


def fill_cell_with_loop(cell, field: str, list_expr: str = "s.lines") -> None:
    """Make a cell hold: {%p for l in <list> %} / {{ l.<field> }} / {%p endfor %}."""
    pars = cell.paragraphs
    keep = pars[0]
    for extra in pars[1:]:
        extra._p.getparent().remove(extra._p)
    set_text(keep, "{{ l." + field + " }}")
    new_paragraph_like(keep, "{%p for l in " + list_expr + " %}", before=True)
    new_paragraph_like(keep, "{%p endfor %}", before=False)


def insert_row_like(row, text: str, before: bool):
    el = copy.deepcopy(row._tr)
    (row._tr.addprevious if before else row._tr.addnext)(el)
    new_row = docx.table._Row(el, row._parent)
    for i, c in enumerate(unique_cells(new_row)):
        for extra in c.paragraphs[1:]:
            extra._p.getparent().remove(extra._p)
        set_text(c.paragraphs[0], text if i == 0 else "")
    return new_row


# --------------------------------------------------------------------------
def prepare_confirmation(src: Path, dst: Path, gross: bool) -> None:
    d = docx.Document(src)
    for p in all_paragraphs(d):
        replace_in_paragraph(p, tag)

    pars = d.paragraphs
    heading = next(p for p in pars if p.text.strip().startswith("Student Details"))
    details, fees = d.tables[0], d.tables[1]

    # fees table: header row, one looping line row, total row
    rows = fees.rows
    header, line_row, total_row = rows[0], rows[1], rows[-1]
    for r in rows[2:-1]:
        r._tr.getparent().remove(r._tr)
    label_cell, amount_cell = unique_cells(line_row)[:2]
    set_text(label_cell.paragraphs[0], "{{ l.label }}")
    set_text(amount_cell.paragraphs[0], "{{ l.amount }}")
    insert_row_like(line_row, "{%tr for l in s.lines %}", before=True)
    insert_row_like(line_row, "{%tr endfor %}", before=False)
    # per-student total
    for p in unique_cells(total_row)[1].paragraphs:
        if "{{" in p.text or "£" in p.text:
            set_text(p, "£{{ s.total }}")

    # loop the heading + both tables once per student
    new_paragraph_like(heading, "{%p for s in students %}", before=True)
    after_fees = fees._tbl.getnext()
    end = docx.text.paragraph.Paragraph(copy.deepcopy(heading._p), heading._parent)
    set_text(end, "{%p endfor %}")
    fees._tbl.addnext(end._p)

    d.save(dst)


def prepare_receipt(src: Path, dst: Path) -> None:
    d = docx.Document(src)
    for p in all_paragraphs(d):
        replace_in_paragraph(p, tag)

    # header table: "Invoice Date" -> "Receipt Date"
    for p in all_paragraphs(d):
        if p.text.strip() == "Invoice Date:":
            set_text(p, "Receipt Date:")
        elif p.text.strip() == "Group Reference:":
            set_text(p, "Booking Reference:")
        elif p.text.strip() == "Parent/Guardian Name":
            set_text(p, "{{ customer_name }}")
        elif p.text.strip().startswith("Name: Sam Allen") or p.text.strip() == "Name:":
            pass

    # items table: rebuild the student row so labels and amounts line up
    items = d.tables[2]
    header, student_row, totals_row = items.rows[0], items.rows[1], items.rows[2]
    cells = unique_cells(student_row)
    name_cell, desc_cell, amount_cell = cells[0], cells[1], cells[2]
    for extra in name_cell.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)
    set_text(name_cell.paragraphs[0], "{{ s.first_name }} {{ s.last_name }}")
    fill_cell_with_loop(desc_cell, "label")
    fill_cell_with_loop(amount_cell, "amount")
    insert_row_like(student_row, "{%tr for s in students %}", before=True)
    insert_row_like(student_row, "{%tr endfor %}", before=False)

    tcells = unique_cells(totals_row)
    labels, amounts = tcells[1], tcells[2]
    for extra in labels.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)
    for extra in amounts.paragraphs[1:]:
        extra._p.getparent().remove(extra._p)
    lp, ap = labels.paragraphs[0], amounts.paragraphs[0]
    set_text(lp, "Total Fees:")
    set_text(ap, "£{{ total }}")
    for label, val in (("Received:", "£{{ received }}"), ("Outstanding:", "£{{ outstanding }}")):
        lp = new_paragraph_like(lp, label, before=False)
        ap = new_paragraph_like(ap, val, before=False)

    # enquiries box: deal owner instead of Sam Allen
    for p in all_paragraphs(d):
        t = p.text
        if "Sam Allen" in t:
            set_text(p, t.replace("Sam Allen", "{{ owner_name }}"))
        elif "sam@samiad.com" in t:
            set_text(p, t.replace("sam@samiad.com", "{{ owner_email }}"))
    d.save(dst)


def prepare_visa(src: Path, dst: Path) -> None:
    d = docx.Document(src)
    for p in all_paragraphs(d):
        replace_in_paragraph(p, tag)
    for p in all_paragraphs(d):
        t = p.text
        if "Box Hill School" in t:
            for r in p.runs:
                if "Box Hill School" in r.text:
                    r.text = r.text.replace("Box Hill School", "{{ s.campus }}")
        if t.strip().lower().startswith("return airport transfers"):
            new_paragraph_like(p, "{%p if s.transfers_booked %}", before=True)
            new_paragraph_like(p, "{%p endif %}", before=False)
        if t.startswith("Sales Person"):
            for r in p.runs:
                if "Sales Person" in r.text:
                    r.text = r.text.replace("Sales Person", "{{ owner_name }}")
        # tidy the leading spaces before the course name
        if "{{ s.course }}" in t and t.startswith(" "):
            for r in p.runs:
                if r.text.strip() == "":
                    r.text = ""
    d.save(dst)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    prepare_confirmation(SRC / "Net_Confirmation.docx", OUT / "confirmation_net.docx", gross=False)
    prepare_confirmation(SRC / "Gross_Confirmation.docx", OUT / "confirmation_gross.docx", gross=True)
    prepare_receipt(SRC / "net_receipt.docx", OUT / "receipt_net.docx")
    prepare_receipt(SRC / "gross_receipt.docx", OUT / "receipt_gross.docx")
    prepare_visa(SRC / "Net_Visa_Invitation_Letter.docx", OUT / "visa_net.docx")
    prepare_visa(SRC / "Gross_Visa_Invitation_Letter.docx", OUT / "visa_gross.docx")

    # report anything left unconverted
    for f in sorted(OUT.glob("*.docx")):
        d = docx.Document(f)
        left = sorted({m.group(0) for p in all_paragraphs(d) for m in PLACEHOLDER.finditer(p.text)})
        print(f"{f.name}: {'OK' if not left else 'UNCONVERTED ' + ', '.join(left)}")


if __name__ == "__main__":
    main()
