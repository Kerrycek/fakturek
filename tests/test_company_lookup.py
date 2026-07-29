from fakturek.company_lookup import (
    ares_payload_to_contact_prefill,
    is_valid_ico,
    is_valid_sk_ico,
    normalize_ico,
    orsr_html_to_contact_prefill,
    rpo_payload_to_contact_prefill,
)


def test_normalize_ico_pads_and_strips():
    assert normalize_ico("6947") == "00006947"
    assert normalize_ico("  6947  ") == "00006947"
    assert normalize_ico("CZ-00006947") == "00006947"
    assert normalize_ico("") == ""


def test_is_valid_ico_checksum():
    assert is_valid_ico("00006947")
    assert is_valid_ico("6947")  # leading zeros allowed
    assert is_valid_ico("27604977")

    # Simple invalid examples
    assert not is_valid_ico("00000000")
    assert not is_valid_ico("123")
    assert not is_valid_ico("abcdefgh")


def test_is_valid_sk_ico_is_loose():
    assert is_valid_sk_ico("12345678")
    assert is_valid_sk_ico(" 12 34 56 78 ")
    assert is_valid_sk_ico("6947")  # pads to 8 digits
    assert not is_valid_sk_ico("00000000")
    # Very short inputs are still accepted and padded (lookup will typically just return "not found").
    assert is_valid_sk_ico("123")


def test_ares_payload_to_contact_prefill_formats_address():
    payload = {
        "obchodniJmeno": "Ministerstvo financí",
        "ico": "00006947",
        "dic": "CZ00006947",
        "sidlo": {
            "nazevUlice": "Letenská",
            "cisloDomovni": 525,
            "cisloOrientacni": 15,
            "nazevObce": "Praha 1",
            "psc": 11800,
            "kodStatu": "CZ",
        },
    }

    prefill = ares_payload_to_contact_prefill(payload)
    assert prefill.name == "Ministerstvo financí"
    assert prefill.ico == "00006947"
    assert prefill.dic == "CZ00006947"
    assert prefill.country == "CZ"
    assert prefill.city == "Praha 1"
    assert prefill.zip == "11800"
    assert prefill.street == "Letenská 525/15"


def test_ares_payload_to_contact_prefill_fallback_text_address():
    payload = {
        "obchodniJmeno": "Test s.r.o.",
        "ico": "27074358",
        "dic": "CZ27074358",
        "sidlo": {
            "textovaAdresa": "Nějaká 1, 110 00 Praha 1",
            "kodStatu": "CZ",
        },
    }

    prefill = ares_payload_to_contact_prefill(payload)
    assert prefill.street == "Nějaká 1, 110 00 Praha 1"


def test_rpo_payload_to_contact_prefill_extracts_basic_fields():
    payload = {
        "results": [
            {
                "id": 9363105,
                "identifiers": [{"value": "51207664", "validFrom": "2017-11-14"}],
                "fullNames": [
                    {"value": "A.B.C.\n system engineering s.r.o.", "validFrom": "2017-11-14"}
                ],
                "addresses": [
                    {
                        "validFrom": "2017-11-14",
                        "street": "Gusevova",
                        "regNumber": 0,
                        "buildingNumber": "26",
                        "postalCodes": ["82109"],
                        "municipality": {"value": "Bratislava - mestská časť Ružinov"},
                        "country": {"value": "Slovenská republika", "code": "703"},
                    }
                ],
            }
        ]
    }

    prefill = rpo_payload_to_contact_prefill(payload)
    assert prefill.name == "A.B.C. system engineering s.r.o."
    assert prefill.ico == "51207664"
    assert prefill.country == "SK"
    assert prefill.street == "Gusevova 26"
    assert prefill.city.startswith("Bratislava")
    assert prefill.zip == "82109"


def test_rpo_payload_to_contact_prefill_uses_current_historical_record():
    payload = {
        "results": [
            {
                "identifiers": [{"value": "46716998", "validFrom": "2012-06-13"}],
                "fullNames": [
                    {"value": "CoolStranky s. r. o.", "validFrom": "2012-06-13", "validTo": "2020-08-24"},
                    {"value": "MADE It Digital s. r. o.", "validFrom": "2020-08-25"},
                ],
                "addresses": [
                    {
                        "validFrom": "2020-08-25",
                        "street": "Pekná cesta",
                        "regNumber": 0,
                        "buildingNumber": "2457/15",
                        "postalCodes": ["831 52"],
                        "municipality": {"value": "Bratislava - mestská časť Rača"},
                    },
                    {
                        "validFrom": "2012-06-13",
                        "validTo": "2020-08-24",
                        "street": "Nobelova",
                        "regNumber": 0,
                        "buildingNumber": "6",
                        "postalCodes": ["83102"],
                        "municipality": {"value": "Bratislava"},
                    },
                ],
            }
        ]
    }

    prefill = rpo_payload_to_contact_prefill(payload)
    assert prefill.name == "MADE It Digital s. r. o."
    assert prefill.street == "Pekná cesta 2457/15"
    assert prefill.city == "Bratislava - mestská časť Rača"
    assert prefill.zip == "831 52"


def test_orsr_html_to_contact_prefill_parses_name_and_seat():
    # Minimal synthetic HTML inspired by ORSR page text.
    html = """
    <html><body>
      <div>Obchodné meno: ASP - Asistance, s. r. o. ; Sídlo: Mierová 83. Bratislava 821 05 ; IČO: 43 847 633 ;</div>
    </body></html>
    """
    prefill = orsr_html_to_contact_prefill(html)
    assert prefill.name == "ASP - Asistance, s. r. o."
    assert prefill.country == "SK"
    assert prefill.ico == "43847633"
    assert prefill.street == "Mierová 83"
    assert prefill.city == "Bratislava"
    assert prefill.zip == "82105"
