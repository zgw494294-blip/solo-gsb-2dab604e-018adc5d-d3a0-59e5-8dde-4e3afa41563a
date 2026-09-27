"""Tests for POST /zones/import (DNS master-file subset)."""
import pytest
from fastapi.testclient import TestClient

from app import store as zone_store
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DNS_DB_PATH", str(tmp_path / "zones.db"))
    zone_store.reset_store()
    with TestClient(app) as c:
        yield c
    zone_store.reset_store()


def import_(client, zone, text):
    return client.post("/zones/import", json={"zone": zone, "text": text})


# ---------------------------------------------------------------------------
# Successful imports: expansion, directives, line folding
# ---------------------------------------------------------------------------


def test_basic_import_normalizes_dedupes_and_sorts(client):
    text = """
$ORIGIN example.com.
@       3600 IN A   192.0.2.9
www     IN A       192.0.2.1
www 360 A 192.0.2.1          ; duplicate after normalization
WWW             a 192.0.2.2
alias   cname   WWW
"""
    r = import_(client, "example.com", text)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 0
    assert body["zone"] == "example.com."
    assert body["records"] == [
        {"name": "alias.example.com.", "type": "CNAME", "value": "www.example.com."},
        {"name": "example.com.", "type": "A", "value": "192.0.2.9"},
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"},
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.2"},
    ]


def test_ttl_and_class_orders_are_accepted_but_not_stored(client):
    text = """
a 300 IN A 192.0.2.1
b IN 60 A 192.0.2.2
c 60 A 192.0.2.3
d A 192.0.2.4
$TTL 3600
e IN A 192.0.2.5
"""
    r = import_(client, "example.com", text)
    assert r.status_code == 200, r.text
    assert [
        (rec["name"], rec["type"], rec["value"]) for rec in r.json()["records"]
    ] == [
        ("a.example.com.", "A", "192.0.2.1"),
        ("b.example.com.", "A", "192.0.2.2"),
        ("c.example.com.", "A", "192.0.2.3"),
        ("d.example.com.", "A", "192.0.2.4"),
        ("e.example.com.", "A", "192.0.2.5"),
    ]


def test_leading_whitespace_reuses_previous_expanded_name(client):
    text = """
host    IN A 192.0.2.1
        IN A 192.0.2.2
        A 192.0.2.3
"""
    r = import_(client, "example.com", text)
    assert r.status_code == 200, r.text
    assert [rec["value"] for rec in r.json()["records"]] == [
        "192.0.2.1",
        "192.0.2.2",
        "192.0.2.3",
    ]
    assert {rec["name"] for rec in r.json()["records"]} == {"host.example.com."}


def test_origin_switch_does_not_change_reused_name(client):
    text = """
www     IN A 192.0.2.1
$ORIGIN sub.example.com.
        IN A 192.0.2.2
new     IN A 192.0.2.3
$ORIGIN sib
x       IN A 192.0.2.4
"""
    r = import_(client, "example.com", text)
    assert r.status_code == 200, r.text
    assert {
        (rec["name"], rec["value"]) for rec in r.json()["records"]
    } == {
        ("www.example.com.", "192.0.2.1"),
        ("www.example.com.", "192.0.2.2"),
        ("new.sub.example.com.", "192.0.2.3"),
        ("x.sib.sub.example.com.", "192.0.2.4"),
    }


def test_relative_origin_argument_and_at(client):
    text = """
$ORIGIN sub
@       IN A 192.0.2.1
deep    IN A 192.0.2.2
"""
    r = import_(client, "example.com", text)
    assert r.status_code == 200, r.text
    assert [rec["name"] for rec in r.json()["records"]] == [
        "deep.sub.example.com.",
        "sub.example.com.",
    ]


def test_parentheses_allow_records_to_span_lines(client):
    text = """
www  (
   300
   IN  ; class inside the parentheses
   A 192.0.2.1 )
del IN ( NS ns1.example.net. )
ns1.del IN A 198.51.100.1
"""
    r = import_(client, "example.com", text)
    assert r.status_code == 200, r.text
    assert {
        (rec["name"], rec["type"], rec["value"]) for rec in r.json()["records"]
    } == {
        ("www.example.com.", "A", "192.0.2.1"),
        ("del.example.com.", "NS", "ns1.example.net."),
        ("ns1.del.example.com.", "A", "198.51.100.1"),
    }


def test_relative_cname_and_ns_targets_expand_to_origin(client):
    text = """
alias IN CNAME www
del IN NS ns1
ns1.del IN A 198.51.100.1
"""
    r = import_(client, "example.com", text)
    assert r.status_code == 200, r.text
    values = {(rec["type"], rec["value"]) for rec in r.json()["records"]}
    assert ("CNAME", "www.example.com.") in values
    assert ("NS", "ns1.example.com.") in values


def test_comments_and_blank_lines(client):
    text = """
; full line comment
$ORIGIN example.com. ; harmless trailing comment

www IN A 192.0.2.1 ; address
; quote " and backslash \\ inside a comment are fine
  IN A 192.0.2.2
"""
    r = import_(client, "example.com", text)
    assert r.status_code == 200, r.text
    assert [rec["value"] for rec in r.json()["records"]] == [
        "192.0.2.1",
        "192.0.2.2",
    ]


def test_wildcard_name_is_accepted(client):
    r = import_(client, "example.com", "*.wild IN A 203.0.113.9\n")
    assert r.status_code == 200, r.text
    assert r.json()["records"] == [
        {"name": "*.wild.example.com.", "type": "A", "value": "203.0.113.9"}
    ]


def test_empty_text_creates_empty_zone(client):
    r = import_(client, "example.com", "")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 0
    assert body["zone"] == "example.com."
    assert body["records"] == []


def test_ttl_has_no_upper_bound_and_is_not_stored(client):
    r = import_(client, "example.com", "www 999999999999 IN A 192.0.2.1\n")
    assert r.status_code == 200, r.text
    assert r.json()["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"}
    ]


def test_import_response_matches_post_zones_and_is_persisted(client):
    text = "www IN A 192.0.2.1\n"
    imported = import_(client, "example.com", text).json()

    direct = client.post(
        "/zones",
        json={
            "zone": "example.com",
            "records": [
                {"name": "www.example.com", "type": "A", "value": "192.0.2.1"}
            ],
        },
    ).json()
    assert set(imported) == set(direct)
    assert imported["records"] == direct["records"]

    fetched = client.get(f"/zones/{imported['id']}").json()
    assert fetched == imported


def test_imported_snapshot_is_resolvable(client):
    text = """
www IN A 192.0.2.1
alias IN CNAME www
"""
    z = import_(client, "example.com", text).json()
    r = client.post(
        "/resolve",
        json={"zone": z["zone"], "records": z["records"], "query": "alias.example.com"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "NOERROR"
    assert r.json()["addresses"] == ["192.0.2.1"]


# ---------------------------------------------------------------------------
# Syntax errors: 422 with the logical record start line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,line",
    [
        ("   IN A 192.0.2.1\n", 1),
        ("www IN A 192.0.2.1\nx IN TXT hello\n", 2),
        ("www IN A 999.1.1.1\n", 1),
        ("$INCLUDE other.zone\n", 1),
        ('www IN TXT "hello"\n', 1),
        ("www IN A 192\\.0.2.1\n", 1),
        ("www IN ( ( A 192.0.2.1 ) )\n", 1),
        ("www IN A 192.0.2.1 )\n", 1),
        ("www (\nIN A 192.0.2.1\n", 1),
        ("www -1 IN A 192.0.2.1\n", 1),
        ("$TTL 12x\n", 1),
        ("www IN 300\n", 1),
        ("www IN A 192.0.2.1 192.0.2.2\n", 1),
        ("@ IN SOA ns admin 1 2 3 4 5\n", 1),
        ("$ORIGIN\n", 1),
        ("$ORIGIN a.com. b.com.\n", 1),
        ("www CH A 192.0.2.1\n", 1),
        ("  $ORIGIN x.com.\n", 1),
    ],
)
def test_syntax_errors_return_422_with_start_line(client, text, line):
    r = import_(client, "example.com", text)
    assert r.status_code == 422, text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["line"] == line
    assert "error" in detail


def test_error_in_folded_record_reports_first_line(client):
    text = "www (\nIN A 999.1.1.1\n)\n"
    r = import_(client, "example.com", text)
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 1


def test_error_after_valid_records_reports_its_own_line(client):
    text = "www IN A 192.0.2.1\n\ndel IN NS ns1.example.net.\nbad IN MX 10 mail\n"
    r = import_(client, "example.com", text)
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 4


# ---------------------------------------------------------------------------
# Whole-zone validation is reused and failure leaves no snapshot
# ---------------------------------------------------------------------------


def test_zone_level_failure_returns_422(client):
    # NS at the apex is rejected by the existing zone rules.
    r = import_(client, "example.com", "@ IN NS ns1.example.net.\n")
    assert r.status_code == 422


def test_out_of_zone_record_returns_422(client):
    r = import_(client, "example.com", "www.other.org. IN A 192.0.2.1\n")
    assert r.status_code == 422


def test_failed_import_creates_no_snapshot(client):
    assert import_(client, "example.com", "www IN TXT hi\n").status_code == 422
    # a later successful import still starts fresh; nothing was persisted
    r = import_(client, "example.com", "www IN A 192.0.2.1\n")
    assert r.status_code == 200
    assert r.json()["version"] == 0


def test_missing_fields_return_422(client):
    assert client.post("/zones/import", json={"zone": "example.com"}).status_code == 422
    assert client.post("/zones/import", json={"text": "www IN A 192.0.2.1\n"}).status_code == 422
