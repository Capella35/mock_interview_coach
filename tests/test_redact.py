"""Step 2b tests: contact-detail masking and the send guard. All people/data fictional."""

from __future__ import annotations

import pytest

from interview_agent.ingest import RawDocument, load_document
from interview_agent.redact import (
    PIILeakError,
    Term,
    assert_no_pii,
    find_pii,
    parse_terms,
    redact_document,
    redact_text,
)

TERMS = parse_terms(
    """
    # fictional candidate
    name: Alex Rivera
    name: Rivera, Alex
    address: 42 Lantern Row
    """
)


def _masked(text: str, terms=()) -> str:
    return redact_text(text, terms)[0]


def _raw(pages, kind="resume", doc_id="resume") -> RawDocument:
    return RawDocument(doc_id=doc_id, kind=kind, filename="f.pdf", sha256="a" * 64, pages=tuple(pages))


# --------------------------------------------------------------------------- #
# Emails
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Email: alex.rivera@example.com", "Email: [EMAIL]"),
        ("contact alex+jobs@mail.example.co.uk.", "contact [EMAIL]."),
        ("a.b@x.example, c_d@y.example.dev", "[EMAIL], [EMAIL]"),
        ("Recruiter: Jordan (jordan.lee@talent-example.com)", "Recruiter: Jordan ([EMAIL])"),
    ],
)
def test_emails_masked(text, expected):
    assert _masked(text) == expected


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("www.linkedin.com/in/alex-rivera-123", "[URL]"),
        ("See https://github.com/alexr/etl-tool.", "See [URL]."),
        ("Portfolio (https://alex.example.dev/projects)", "Portfolio ([URL])"),
        ("github.com/alexr and linkedin.com/in/alexr", "[URL] and [URL]"),
    ],
)
def test_urls_masked(text, expected):
    assert _masked(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Built services in ASP.NET/C# and Node.js/React.",
        "Joined Databricks.com as a partner engineer.",
        "Worked on CI/CD and e.g./i.e. style docs.",
    ],
)
def test_tech_terms_and_bare_domains_not_masked_as_urls(text):
    assert _masked(text) == text


# --------------------------------------------------------------------------- #
# Phones
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "phone",
    [
        "+44 20 7946 0958",
        "(555) 123-4567",
        "555-123-4567",
        "555.123.4567",
        "+1.555.123.4567",
        "+1 (555) 123-4567",
        "0532 123 45 67",
        "+90 532 123 45 67",
        "+90 (532) 123 45 67",
        "5551234567",
    ],
)
def test_phones_masked(phone):
    assert _masked(f"Mobile: {phone} (evenings)") == "Mobile: [PHONE] (evenings)"


@pytest.mark.parametrize(
    "text",
    [
        "Senior Data Engineer (2022-2024)",
        "Acme Logistics, 2018 - 2022",
        "Python 2019 2020 2021 cohort",
        "Grew revenue to 1,000,000 users and 100.000.000 events",
        "Cut runtime from 3 hours to 3 minutes; 99.9% uptime; 40% cost cut",
        "Python 3.12, Spark 3.5.1, v1.2.3",
        "Released 2021-03-12 and 12.03.2021",
        "Raised $1.2M; served 10,000+ customers",
        "2015-2019 2019-2022",
    ],
)
def test_numbers_that_are_not_phones_kept(text):
    assert _masked(text) == text


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("221B Baker Street, London, UK", "[ADDRESS], London, UK"),
        ("10 Downing St. London", "[ADDRESS] London"),
        ("1600 Amphitheatre Parkway, Mountain View", "[ADDRESS], Mountain View"),
        ("500 Market Street, Suite 200, San Francisco", "[ADDRESS], San Francisco"),
        ("San Francisco, CA 94105", "San Francisco, CA [ADDRESS]"),
        ("London SW1A 1AA", "London [ADDRESS]"),
        ("Moda Mah. Şair Nefi Sok. No: 7, Kadıköy, İstanbul", "[ADDRESS], Kadıköy, İstanbul"),
        ("Bağdat Caddesi No:45/3 İstanbul", "[ADDRESS] İstanbul"),
    ],
)
def test_addresses_masked_city_kept(text, expected):
    assert _masked(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Led 3 Data Engineers across 2 regions",
        "Based in London, UK (open to remote)",
        "Experience\nAtatürk University, İstanbul",
        "Built a 2-way sync for 5 Enterprise Customers",
    ],
)
def test_non_addresses_kept(text):
    assert _masked(text) == text


def test_address_never_spans_lines():
    text = "Experience\nAtatürk Cad. No: 12"
    assert _masked(text) == "Experience\n[ADDRESS]"


# --------------------------------------------------------------------------- #
# Terms file
# --------------------------------------------------------------------------- #


def test_parse_terms_kinds_comments_and_default():
    terms = parse_terms("# c\n\nname: Alex Rivera\naddress:42 Lantern Row\nSecret Project Falcon\nnote: x: y z\n")
    assert [t.kind for t in terms] == ["name", "address", "other", "other"]
    assert terms[0].value == "Alex Rivera" and terms[1].value == "42 Lantern Row"
    assert terms[3].value == "note: x: y z"  # unknown prefix -> whole line


def test_parse_terms_rejects_too_short_values():
    with pytest.raises(ValueError, match="line 1"):
        parse_terms("name: Al")


def test_term_repr_hides_value():
    assert "Alex" not in repr(TERMS[0])


def test_name_masked_case_insensitive_across_lines_and_possessive():
    text = "ALEX RIVERA\nAlex\nRivera's team; Rivera, Alex"
    assert _masked(text, TERMS) == "[NAME]\n[NAME]'s team; [NAME]"


def test_name_not_masked_inside_other_words():
    terms = [Term("name", "Alexa")]
    assert _masked("Built an Alexander-scale Alexa skill", terms) == "Built an Alexander-scale [NAME] skill"


def test_terms_win_over_patterns():
    assert _masked("Home: 42 Lantern Row", TERMS) == "Home: [ADDRESS]"


# --------------------------------------------------------------------------- #
# redact_document
# --------------------------------------------------------------------------- #

CONTACT_HEAVY_P1 = (
    "Alex Rivera\n"
    "42 Lantern Row, Springfield | +1 (555) 123-4567 | alex.rivera@example.com\n"
    "www.linkedin.com/in/alex-rivera-123 | github.com/alexr\n"
    "Senior Data Engineer, Acme Logistics (2022-2024)\n"
    "- Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes."
)
CONTACT_HEAVY_P2 = "References: Rivera, Alex - call 0532 123 45 67 or alex.r@mail.example.org"
ORIGINALS = [
    "Alex Rivera", "42 Lantern Row", "(555) 123-4567", "alex.rivera@example.com",
    "linkedin.com/in/alex-rivera-123", "github.com/alexr", "0532 123 45 67", "alex.r@mail.example.org",
]


def test_redact_document_masks_everything_and_keeps_content():
    doc, rmap = redact_document(_raw([CONTACT_HEAVY_P1, CONTACT_HEAVY_P2]), owner_id="u1", terms=TERMS)
    for original in ORIGINALS:
        assert original not in doc.text
    assert find_pii(doc.text) == []
    assert "Springfield" in doc.text  # city kept
    assert "Senior Data Engineer, Acme Logistics (2022-2024)" in doc.text
    assert "cutting runtime from 3 hours to 3 minutes." in doc.text
    assert rmap.counts() == {"name": 2, "address": 1, "phone": 2, "email": 2, "url": 2}
    assert rmap.warnings == []


def test_map_points_at_placeholders_and_holds_originals():
    doc, rmap = redact_document(_raw([CONTACT_HEAVY_P1, CONTACT_HEAVY_P2]), owner_id="u1", terms=TERMS)
    for e in rmap.entries:
        assert doc.text[e.char_start : e.char_end] == e.placeholder
        assert doc.page_of(e.char_start) == e.page
    assert {e.original for e in rmap.entries} >= {"Alex Rivera", "0532 123 45 67", "alex.r@mail.example.org"}
    assert [e.page for e in rmap.entries if e.kind == "phone"] == [1, 2]


def test_source_document_fields_and_pages():
    raw = _raw([CONTACT_HEAVY_P1, CONTACT_HEAVY_P2])
    doc, rmap = redact_document(raw, owner_id="u1", terms=TERMS)
    assert (doc.doc_id, doc.kind, doc.filename, doc.sha256, doc.owner_id) == ("resume", "resume", "f.pdf", "a" * 64, "u1")
    assert len(doc.page_starts) == 2
    assert doc.text[doc.page_starts[1] :].startswith("References: [NAME]")
    assert rmap.source_sha256 == raw.sha256


def test_jd_recruiter_contact_masked_without_name_warning():
    jd = "Forward Deployed Engineer - London, UK\nQuestions? jordan.lee@talent-example.com or +44 20 7946 0958"
    doc, rmap = redact_document(_raw([jd], kind="job_description", doc_id="jd"), owner_id="u1")
    assert doc.text == "Forward Deployed Engineer - London, UK\nQuestions? [EMAIL] or [PHONE]"
    assert rmap.warnings == []


def test_resume_without_name_terms_warns():
    _, rmap = redact_document(_raw(["Senior Data Engineer at Acme Logistics, building pipelines."]), owner_id="u1")
    assert any("name" in w for w in rmap.warnings)


def test_map_repr_never_shows_originals():
    _, rmap = redact_document(_raw([CONTACT_HEAVY_P1]), owner_id="u1", terms=TERMS)
    assert "Alex" not in repr(rmap) and "555" not in repr(rmap) and "@" not in str(rmap)


def test_end_to_end_from_pdf(tmp_path, make_pdf):
    """Fictional PDF full of contact details -> ingest -> redact: nothing leaks."""
    pdf = make_pdf([CONTACT_HEAVY_P1.split("\n"), [CONTACT_HEAVY_P2]])
    path = tmp_path / "resume.pdf"
    path.write_bytes(pdf)
    doc, _ = redact_document(load_document(path, "resume"), owner_id="u1", terms=TERMS)
    for original in ORIGINALS:
        assert original not in doc.text
    assert_no_pii(doc.text)


# --------------------------------------------------------------------------- #
# Send guard
# --------------------------------------------------------------------------- #


def test_guard_blocks_email_and_phone_without_echoing_them():
    with pytest.raises(PIILeakError) as exc:
        assert_no_pii("prompt ... alex.rivera@example.com ... +44 20 7946 0958")
    msg = str(exc.value)
    assert "1 email" in msg and "1 phone" in msg
    assert "alex" not in msg and "7946" not in msg


def test_guard_allows_redacted_text_and_placeholders():
    assert_no_pii("Candidate [NAME], [EMAIL], [PHONE]; cut runtime from 3 hours to 3 minutes (2022-2024).")
