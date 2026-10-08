"""Reusing a Xero contact staff already made, instead of creating a duplicate."""
from samiad.clients.xero import Xero


class FakeResp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def xero_with(contacts):
    x = Xero.__new__(Xero)  # no network, no tokens
    calls = []

    def _req(method, path, params=None, json=None, idem=None):
        calls.append((method, path, params, json))
        if method == "GET" and path == "/Contacts":
            term = (params or {}).get("searchTerm", "").lower()
            where = (params or {}).get("where", "")
            if where.startswith("AccountNumber"):
                acct = where.split('"')[1]
                return FakeResp({"Contacts": [c for c in contacts if c.get("AccountNumber") == acct]})
            if where.startswith("Name"):
                nm = where.split('"')[1]
                return FakeResp({"Contacts": [c for c in contacts if c["Name"] == nm]})
            return FakeResp({"Contacts": [c for c in contacts if term in c["Name"].lower()
                                          or term in (c.get("EmailAddress") or "").lower()]})
        if method == "PUT" and path == "/Contacts":
            return FakeResp({"Contacts": [{"ContactID": "new"}]})
        return FakeResp({})
    x._req = _req
    return x, calls


def c(cid, name, email=None, acct=None, status="ACTIVE"):
    return {"ContactID": cid, "Name": name, "EmailAddress": email, "AccountNumber": acct, "ContactStatus": status}


def test_parent_matched_by_email_and_stamped_with_hubspot_id():
    x, calls = xero_with([c("e9", "Amelie Pascual Gracia", "amelie@yahoo.de")])
    assert x.find_or_create_contact("Amelie Pascual Gracia", "", "HS-P-1", emails=["amelie@yahoo.de"]) == "e9"
    assert ("POST", "/Contacts/e9", None, {"Contacts": [{"ContactID": "e9", "AccountNumber": "HS-P-1"}]}) in calls
    assert not any(m == "PUT" for m, *_ in calls)


def test_same_person_twice_in_xero_prefers_the_exact_name():
    x, _ = xero_with([c("e9", "Amelie Pascual Gracia", "amelie@yahoo.de"),
                      c("77", "Amelie Pascual", "amelie@yahoo.de")])
    assert x.find_or_create_contact("Amelie Pascual Gracia", "", "HS-P-1", emails=["amelie@yahoo.de"]) == "e9"


def test_agent_matched_by_staff_email_or_own_domain():
    x, _ = xero_with([c("eb", "Tugce Ozsahin", "tugce@gowest.com.tr")])
    assert x.find_or_create_contact("Gowest", "", "HS-C-2", emails=["tugce@gowest.com.tr"]) == "eb"
    assert x.find_or_create_contact("Gowest", "", "HS-C-2", domain="www.gowest.com.tr") == "eb"


def test_public_mail_domain_never_matches_an_agent():
    x, _ = xero_with([c("g1", "Someone Else", "someone@gmail.com")])
    assert x.find_or_create_contact("Agency Y", "", "HS-C-3", domain="gmail.com") == "new"


def test_contact_belonging_to_another_hubspot_record_is_not_reused():
    x, _ = xero_with([c("o1", "Maria Rossi", "maria@x.it", acct="HS-P-999")])
    assert x.find_or_create_contact("Maria Rossi", "", "HS-P-1", emails=["maria@x.it"]) == "new"


def test_archived_or_ambiguous_contacts_are_not_reused():
    x, _ = xero_with([c("a1", "Old Agency", "x@old.com", status="ARCHIVED")])
    assert x.find_or_create_contact("Old Agency", "", "HS-C-4", emails=["x@old.com"]) == "new"
    x, _ = xero_with([c("d1", "Twin", "t@t.com"), c("d2", "Twin", "t@t.com")])
    assert x.find_or_create_contact("Twin", "", "HS-P-5", emails=["t@t.com"]) == "new"
