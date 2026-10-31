import pytest

from leadgen import normalize


@pytest.mark.parametrize("raw,expected", [
    ("https://www.Example.qa/shop?x=1", "example.qa"),
    ("example.qa", "example.qa"),
    ("http://m.store.example.com:8080/", "store.example.com"),
    ("https://www.instagram.com/brand/", None),
    ("not a url", None),
    ("", None),
    (None, None),
])
def test_domain_of(raw, expected):
    assert normalize.domain_of(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("@Oud_House.QA", "oud_house.qa"),
    ("https://www.instagram.com/oudhouse/?hl=en", "oudhouse"),
    ("instagram.com/p/Cxyz123/", None),  # a post URL is not a profile
    ("has spaces", None),
    ("", None),
])
def test_instagram_handle(raw, expected):
    assert normalize.instagram_handle(raw) == expected


def test_phone_defaults_to_qatar_and_rejects_invalid():
    assert normalize.phone_e164("5555 1234") == "+97455551234"
    assert normalize.phone_e164("+971 50 123 4567") == "+971501234567"
    assert normalize.phone_e164("12") is None


def test_email_requires_valid_address():
    assert normalize.email("mailto:Info@Brand.QA?subject=hi") == "info@brand.qa"
    assert normalize.email("info at brand") is None


def test_name_key_strips_legal_suffixes_and_keeps_arabic():
    assert normalize.name_key("Al Reem Perfumes W.L.L.") == normalize.name_key("al reem perfumes")
    assert normalize.name_key("عطور الريم") == "عطور الريم"
