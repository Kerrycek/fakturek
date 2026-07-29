from __future__ import annotations
from fakturek.time_utils import utc_now

import base64
import binascii
import io
import lzma
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

import qrcode


_IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}$")
_BIC_RE = re.compile(r"^[A-Z0-9]{8}([A-Z0-9]{3})?$")
_DOMESTIC_ACC_RE = re.compile(r"^(?:(?P<prefix>\d{1,6})-)?(?P<account>\d{1,10})/(?P<bank>\d{4})$")


@dataclass(frozen=True)
class BankAccountPayload:
    label: str
    number: str
    iban: str
    bic: str
    country: str

    @property
    def display(self) -> str:
        return self.number or self.iban_display

    @property
    def iban_display(self) -> str:
        return format_iban_for_display(self.iban)


@dataclass(frozen=True)
class PaymentQRCode:
    kind: str
    title: str
    payload: str
    image_data_uri: str


def normalize_spaces(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip()


def normalize_bic(value: str | None) -> str:
    bic = re.sub(r"\s+", "", str(value or "")).upper()
    if bic and not _BIC_RE.match(bic):
        raise ValueError("BIC/SWIFT musí mít 8 nebo 11 znaků.")
    return bic


def iban_is_valid(iban: str | None) -> bool:
    raw = re.sub(r"\s+", "", str(iban or "")).upper()
    if not _IBAN_RE.match(raw):
        return False
    rearranged = raw[4:] + raw[:4]
    numeric = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        else:
            numeric.append(str(ord(ch) - 55))
    try:
        return int("".join(numeric)) % 97 == 1
    except Exception:
        return False


def normalize_iban(value: str | None) -> str:
    iban = re.sub(r"\s+", "", str(value or "")).upper()
    if not iban:
        return ""
    if not _IBAN_RE.match(iban):
        raise ValueError("IBAN nemá platný formát.")
    if not iban_is_valid(iban):
        raise ValueError("IBAN není platný.")
    return iban


def format_iban_for_display(iban: str | None) -> str:
    try:
        raw = normalize_iban(iban) if iban else ""
    except ValueError:
        raw = re.sub(r"\s+", "", str(iban or "")).upper()
    if not raw:
        return ""
    return " ".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def parse_domestic_account(value: str | None) -> tuple[str, str, str] | None:
    raw = re.sub(r"\s+", "", str(value or ""))
    if not raw:
        return None
    match = _DOMESTIC_ACC_RE.match(raw)
    if not match:
        return None
    prefix = (match.group("prefix") or "").lstrip("0")
    account = (match.group("account") or "").lstrip("0")
    bank_code = match.group("bank") or ""
    return prefix or "", account or "0", bank_code


def format_domestic_account(prefix: str | None, account: str | None, bank_code: str | None) -> str:
    pref = str(prefix or "").strip()
    acc = str(account or "").strip()
    bank = str(bank_code or "").strip()
    if not acc or not bank:
        return ""
    if pref:
        return f"{pref}-{acc}/{bank}"
    return f"{acc}/{bank}"


def compute_cz_sk_iban(*, country: str, bank_code: str, account: str, prefix: str = "") -> str:
    country_code = str(country or "").strip().upper()
    if country_code not in {"CZ", "SK"}:
        raise ValueError("IBAN lze automaticky dopočítat jen pro CZ/SK účty.")

    bank = re.sub(r"\D+", "", str(bank_code or ""))
    acc = re.sub(r"\D+", "", str(account or ""))
    pref = re.sub(r"\D+", "", str(prefix or ""))
    if len(bank) != 4:
        raise ValueError("Kód banky musí mít 4 číslice.")
    if not acc:
        raise ValueError("Číslo účtu nesmí být prázdné.")
    if len(acc) > 10:
        raise ValueError("Číslo účtu je příliš dlouhé.")
    if len(pref) > 6:
        raise ValueError("Předčíslí účtu je příliš dlouhé.")

    bban = f"{bank}{pref.zfill(6)}{acc.zfill(10)}"
    country_digits = "".join(str(ord(ch) - 55) for ch in country_code)
    remainder = int(f"{bban}{country_digits}00") % 97
    check_digits = 98 - remainder
    return f"{country_code}{check_digits:02d}{bban}"


def resolve_bank_account(
    *,
    account_number: str | None = None,
    iban: str | None = None,
    bic: str | None = None,
    country: str | None = None,
    label: str | None = None,
) -> BankAccountPayload:
    raw_number = normalize_spaces(account_number)
    raw_country = (str(country or "") or "").strip().upper()
    raw_label = normalize_spaces(label)

    normalized_bic = normalize_bic(bic)
    normalized_iban = ""
    number_display = raw_number
    inferred_country = raw_country

    if raw_number and raw_number[:2].isalpha() and not iban:
        normalized_iban = normalize_iban(raw_number)
        inferred_country = normalized_iban[:2]
        number_display = ""
    elif iban:
        normalized_iban = normalize_iban(iban)
        inferred_country = normalized_iban[:2]

    domestic = parse_domestic_account(raw_number)
    if domestic and not normalized_iban:
        pref, account, bank_code = domestic
        inferred_country = inferred_country or "CZ"
        normalized_iban = compute_cz_sk_iban(
            country=inferred_country,
            bank_code=bank_code,
            account=account,
            prefix=pref,
        )
        number_display = format_domestic_account(pref, account, bank_code)

    if normalized_iban and not inferred_country:
        inferred_country = normalized_iban[:2]

    if not number_display and not normalized_iban:
        raise ValueError("Zadej číslo účtu nebo IBAN.")

    if not raw_label:
        raw_label = number_display or format_iban_for_display(normalized_iban) or "Bankovní účet"

    return BankAccountPayload(
        label=raw_label,
        number=number_display,
        iban=normalized_iban,
        bic=normalized_bic,
        country=(inferred_country or "CZ").upper(),
    )


def cents_to_decimal(amount_cents: int) -> Decimal:
    return (Decimal(int(amount_cents or 0)) / Decimal("100")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def compute_rounding_adjustment_cents(total_cents: int, *, increment_cents: int = 100) -> int:
    if increment_cents <= 0:
        return 0
    total = Decimal(int(total_cents or 0)) / Decimal(100)
    increment = Decimal(int(increment_cents)) / Decimal(100)
    rounded = (total / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment
    rounded_cents = int((rounded * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return rounded_cents - int(total_cents or 0)


def digits_only(value: str | None) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def variable_symbol_from_invoice_number(value: str | None) -> str:
    digits = digits_only(value)
    if not digits:
        return ""
    return digits[-10:]


def build_cz_qr_platba_payload(
    *,
    iban: str,
    bic: str = "",
    amount_cents: int,
    currency: str,
    variable_symbol: str = "",
    message: str = "",
    due_date: date | None = None,
) -> str:
    normalized_iban = normalize_iban(iban)
    cur = (str(currency or "") or "").strip().upper()
    if len(cur) != 3:
        raise ValueError("Měna pro QR Platbu musí mít 3 znaky.")

    parts = ["SPD", "1.0", f"ACC:{normalized_iban}{('+' + normalize_bic(bic)) if bic else ''}"]
    parts.append(f"AM:{cents_to_decimal(amount_cents)}")
    parts.append(f"CC:{cur}")

    vs = digits_only(variable_symbol)
    if vs:
        parts.append(f"X-VS:{vs[:10]}")
    if due_date is not None:
        parts.append(f"DT:{due_date.strftime('%Y%m%d')}")
    msg = normalize_spaces(message).replace("*", " ")
    if msg:
        parts.append(f"MSG:{msg[:60]}")

    return "*".join(parts)


def build_epc_qr_payload(
    *,
    iban: str,
    bic: str,
    beneficiary_name: str,
    amount_cents: int,
    currency: str,
    remittance_text: str = "",
    purpose: str = "",
) -> str:
    normalized_iban = normalize_iban(iban)
    cur = (str(currency or "") or "").strip().upper()
    if cur != "EUR":
        raise ValueError("SEPA/EPC QR vyžaduje měnu EUR.")

    name = normalize_spaces(beneficiary_name)[:70]
    if not name:
        raise ValueError("Pro SEPA/EPC QR je potřeba název příjemce.")

    purpose_value = re.sub(r"[^A-Z0-9]", "", str(purpose or "").upper())[:4]
    fields = [
        "BCD",
        "002",
        "1",
        "SCT",
        normalize_bic(bic),
        name,
        normalized_iban,
        f"EUR{cents_to_decimal(amount_cents)}",
        purpose_value,
        "",
        normalize_spaces(remittance_text)[:140],
    ]
    return "\n".join(fields).rstrip("\n")


def build_pay_by_square_payload(
    *,
    iban: str,
    bic: str = "",
    amount_cents: int,
    currency: str,
    beneficiary_name: str = "",
    variable_symbol: str = "",
    constant_symbol: str = "",
    specific_symbol: str = "",
    note: str = "",
    due_date: date | None = None,
    beneficiary_address_1: str = "",
    beneficiary_address_2: str = "",
) -> str:
    amount = cents_to_decimal(amount_cents)
    used_date = due_date or utc_now().date()
    data = "\t".join(
        [
            "",
            "1",
            "1",
            f"{amount:.2f}",
            (str(currency or "EUR") or "EUR").strip().upper(),
            used_date.strftime("%Y%m%d"),
            digits_only(variable_symbol)[:10],
            digits_only(constant_symbol)[:4],
            digits_only(specific_symbol)[:10],
            "",
            normalize_spaces(note)[:140],
            "1",
            normalize_iban(iban),
            normalize_bic(bic),
            "0",
            "0",
            normalize_spaces(beneficiary_name)[:140],
            normalize_spaces(beneficiary_address_1)[:70],
            normalize_spaces(beneficiary_address_2)[:70],
        ]
    )
    checksum = binascii.crc32(data.encode("utf-8")).to_bytes(4, "little")
    total = checksum + data.encode("utf-8")
    compressed = lzma.compress(
        total,
        format=lzma.FORMAT_RAW,
        filters=[
            {
                "id": lzma.FILTER_LZMA1,
                "lc": 3,
                "lp": 0,
                "pb": 2,
                "dict_size": 128 * 1024,
            }
        ],
    )
    compressed_with_length = b"\x00\x00" + len(total).to_bytes(2, "little") + compressed
    binary = "".join(bin(single_byte)[2:].zfill(8) for single_byte in compressed_with_length)
    remainder = len(binary) % 5
    if remainder:
        binary += "0" * (5 - remainder)
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUV"
    return "".join(alphabet[int(binary[idx : idx + 5], 2)] for idx in range(0, len(binary), 5))


def make_qr_png_data_uri(payload: str) -> str:
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(str(payload or ""))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def build_payment_qr_codes(
    *,
    account: BankAccountPayload,
    amount_cents: int,
    currency: str,
    beneficiary_name: str,
    invoice_number: str,
    variable_symbol: str | None = None,
    due_date: date | None = None,
    subject_country: str | None = None,
) -> list[PaymentQRCode]:
    qr_codes: list[PaymentQRCode] = []
    cur = (str(currency or "") or "").strip().upper()
    resolved_variable_symbol = digits_only(variable_symbol)[:10] or variable_symbol_from_invoice_number(invoice_number)
    note = f"Faktura {normalize_spaces(invoice_number)}".strip()

    if account.iban:
        try:
            payload = build_cz_qr_platba_payload(
                iban=account.iban,
                bic=account.bic,
                amount_cents=amount_cents,
                currency=cur,
                variable_symbol=resolved_variable_symbol,
                message=note,
                due_date=due_date,
            )
            qr_codes.append(
                PaymentQRCode(
                    kind="cz_spd",
                    title="QR platba (ČR)",
                    payload=payload,
                    image_data_uri=make_qr_png_data_uri(payload),
                )
            )
        except Exception:
            pass

    if account.iban:
        try:
            payload = build_pay_by_square_payload(
                iban=account.iban,
                bic=account.bic,
                amount_cents=amount_cents,
                currency=cur,
                beneficiary_name=beneficiary_name,
                variable_symbol=resolved_variable_symbol,
                note=note,
                due_date=due_date,
            )
            qr_codes.append(
                PaymentQRCode(
                    kind="sk_bysquare",
                    title="PAY by square (SK)",
                    payload=payload,
                    image_data_uri=make_qr_png_data_uri(payload),
                )
            )
        except Exception:
            pass

    if account.iban and cur == "EUR":
        try:
            payload = build_epc_qr_payload(
                iban=account.iban,
                bic=account.bic,
                beneficiary_name=beneficiary_name,
                amount_cents=amount_cents,
                currency=cur,
                remittance_text=invoice_number,
            )
            qr_codes.append(
                PaymentQRCode(
                    kind="epc",
                    title="SEPA QR (EU/SK)",
                    payload=payload,
                    image_data_uri=make_qr_png_data_uri(payload),
                )
            )
        except Exception:
            pass

    country = (str(subject_country or "") or "").strip().upper()
    if country == "CZ":
        preferred = [code for code in qr_codes if code.kind == "cz_spd"]
        return preferred or (qr_codes[:1] if qr_codes else [])
    if country == "SK":
        preferred = [code for code in qr_codes if code.kind == "sk_bysquare"]
        if preferred:
            return preferred
        fallback = [code for code in qr_codes if code.kind == "epc"]
        return fallback or (qr_codes[:1] if qr_codes else [])

    return qr_codes
