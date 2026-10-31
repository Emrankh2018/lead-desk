import pytest

from leadgen.services import importer
from leadgen.services.leads import LeadInput, create_lead

CSV = (
    "Business Name,Website,Instagram,Phone,City,Category\n"
    "Oud House,oudhouse.qa,@oudhouse,55551234,Doha,perfume\n"
    "Rose Atelier,,rose.atelier,,Lusail,skincare\n"
    "Oud House Again,https://www.oudhouse.qa/shop,,,Doha,perfume\n"
    ",,,,,\n"
    "Bad Phone Co,badphone.qa,,12,Doha,\n"
    "Existing Brand,existing.qa,,,Doha,jewelry\n"
).encode("utf-8-sig")


def test_preview_classifies_rows_and_commit_creates_only_new(session):
    create_lead(session, LeadInput(name="Existing Brand", website="existing.qa"), actor="t", source="manual",
                source_ref=None, default_region="QA")
    session.commit()
    mapping, rows = importer.parse(CSV)
    assert mapping["name"] == "Business Name" and mapping["industry"] == "Category"
    preview = importer.preview(session, rows, icp_id=None, default_region="QA")
    status = {r.line: r.status for r in preview}
    assert status == {2: "new", 3: "new", 4: "duplicate_in_file", 6: "invalid", 7: "duplicate"}
    result = importer.commit(session, preview, filename="list.csv", icp_id=None, actor="t", default_region="QA")
    session.commit()
    assert result == {"created": 2, "skipped": 3}


def test_parse_rejects_missing_identifier_columns_and_non_utf8():
    with pytest.raises(importer.ImportError_, match="no name, website or Instagram"):
        importer.parse(b"city,phone\nDoha,1\n")
    with pytest.raises(importer.ImportError_, match="UTF-8"):
        importer.parse("name\nCaf\xe9\n".encode("latin-1"))


def test_semicolon_delimited_file_is_supported():
    _, rows = importer.parse("name;website\nBrand;brand.qa\n".encode())
    assert rows == [{"name": "Brand", "website": "brand.qa"}]
