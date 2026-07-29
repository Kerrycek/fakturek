from __future__ import annotations

from datetime import date

from fakturek.banking import BankAccountPayload, build_payment_qr_codes
from fakturek.pdf import InvoicePDFData, render_invoice_pdf_bytes


def test_country_filtered_qr_and_fallback_pdf_embed_image():
    account = BankAccountPayload(
        label="CZ účet",
        number="123456789/0100",
        iban="CZ6508000000192000145399",
        bic="GIBACZPX",
        country="CZ",
    )
    assert account.iban_display == "CZ65 0800 0000 1920 0014 5399"

    cz_qr = build_payment_qr_codes(
        account=account,
        amount_cents=12100,
        currency="CZK",
        beneficiary_name="Seller One s.r.o.",
        invoice_number="2026-0003",
        due_date=date(2026, 3, 15),
        subject_country="CZ",
    )
    assert [qr.kind for qr in cz_qr] == ["cz_spd"]

    pdf_bytes = render_invoice_pdf_bytes(
        InvoicePDFData(
            number="2026-0003",
            status="issued",
            issue_date=date(2026, 3, 1),
            taxable_supply_date=None,
            due_date=date(2026, 3, 15),
            currency="CZK",
            items_total_cents=12100,
            discount_cents=0,
            rounding_adjustment_cents=0,
            total_cents=12100,
            notes="Test PDF s QR",
            issuer={
                "name": "Seller One s.r.o.",
                "street": "Dlouhá 1",
                "city": "Praha",
                "zip": "11000",
                "country": "CZ",
                "ico": "12345678",
                "dic": "CZ12345678",
            },
            customer={
                "name": "Acme Client a.s.",
                "street": "Krátká 2",
                "city": "Praha",
                "zip": "12000",
                "country": "CZ",
                "ico": "87654321",
                "dic": "CZ87654321",
            },
            items=[
                {
                    "description": "Členský příspěvek",
                    "quantity": "1",
                    "unit_price_cents": 12100,
                    "vat_rate": "0",
                    "line_total_cents": 12100,
                }
            ],
            payment_account={
                "label": account.label,
                "number": account.number,
                "display": account.number,
                "iban": account.iban,
                "bic": account.bic,
                "country": account.country,
            },
            payment_qr_codes=[
                {
                    "kind": qr.kind,
                    "title": qr.title,
                    "payload": qr.payload,
                    "image_data_uri": qr.image_data_uri,
                }
                for qr in cz_qr
            ],
        )
    )
    assert pdf_bytes.startswith(b"%PDF")
    assert b"/Subtype /Image" in pdf_bytes
