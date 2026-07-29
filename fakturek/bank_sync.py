from __future__ import annotations

import hashlib
import imaplib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fakturek.banking import digits_only, normalize_spaces
from fakturek.money import parse_money_to_signed_cents


class BankSyncError(RuntimeError):
    """Raised when bank sync communication or payload parsing fails."""


@dataclass(frozen=True)
class ImportedBankTransaction:
    provider: str
    external_id: str
    booked_on: date
    amount_cents: int
    currency: str
    direction: str
    variable_symbol: str | None
    constant_symbol: str | None
    specific_symbol: str | None
    counterparty_account: str | None
    counterparty_name: str | None
    message: str | None
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class ImportedBankEmail:
    provider: str
    imap_uid: str
    external_message_id: str | None
    received_at: datetime | None
    from_email: str | None
    subject: str | None
    body_text: str | None
    raw_headers: dict[str, str]


def extract_bank_email_recipients(imported: ImportedBankEmail) -> list[str]:
    header_values: list[str] = []
    for key, value in (imported.raw_headers or {}).items():
        if str(key or "").strip().lower() in {
            "delivered-to",
            "x-original-to",
            "envelope-to",
            "to",
            "cc",
            "resent-to",
        }:
            header_values.append(str(value or ""))
    recipients: list[str] = []
    for _display_name, address in getaddresses(header_values):
        clean = str(address or "").strip().lower()
        if clean and clean not in recipients:
            recipients.append(clean)
    return recipients


EMAIL_BANK_PARSER_OPTIONS: list[tuple[str, str]] = [
    ("pending", "Bez parseru"),
    ("csas_cz", "Česká spořitelna"),
    ("csob_cz", "ČSOB"),
    ("fio_email_cz", "Fio e-mail"),
    ("raiffeisenbank_cz", "Raiffeisenbank CZ"),
]


def _normalize_label(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_only = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", ascii_only).strip()


def _normalize_value(value: Any) -> str:
    return str(value or "").strip()


def _extract_email_body_text(message) -> str:
    if message.is_multipart():
        plain_parts: list[str] = []
        fallback_parts: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            try:
                payload = part.get_content()
            except Exception:
                payload = ""
            text = str(payload or "").strip()
            if not text:
                continue
            content_type = str(part.get_content_type() or "")
            if content_type == "text/plain":
                plain_parts.append(text)
            elif content_type == "text/html":
                fallback_parts.append(re.sub(r"<[^>]+>", " ", text))
        chosen = plain_parts or fallback_parts
        return "\n\n".join(part for part in chosen if part).strip()
    try:
        return str(message.get_content() or "").strip()
    except Exception:
        return ""


def _decode_email_header(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw


def _normalize_email_body(value: str | None) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_label_value_blocks(text: str, labels: list[str]) -> dict[str, str]:
    clean_text = _normalize_email_body(text)
    if not clean_text:
        return {}
    escaped_labels = [re.escape(label) for label in sorted(labels, key=len, reverse=True)]
    matches = list(re.finditer(r"(?:" + "|".join(escaped_labels) + r")", clean_text, flags=re.IGNORECASE))
    if not matches:
        return {}

    extracted: dict[str, str] = {}
    for index, match in enumerate(matches):
        raw_label = match.group(0)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean_text)
        value = clean_text[start:end].strip(" :|-")
        if value:
            extracted[_normalize_label(raw_label)] = value
    return extracted


def _parse_cz_datetime_to_date(value: str | None) -> date:
    raw = str(value or "").strip()
    if not raw:
        raise BankSyncError("Bankovní e-mail neobsahuje datum a čas transakce.")
    for fmt in ("%d. %m. %Y %H:%M", "%d.%m.%Y %H:%M", "%d. %m. %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise BankSyncError(f"Neplatné datum transakce v bankovním e-mailu: {raw}")


def _extract_first_account_number(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"(\d{1,10}(?:-\d{1,10})?/\d{4})", raw)
    if match:
        return match.group(1)
    return None


def _extract_account_name(value: str | None) -> str | None:
    raw = normalize_spaces(str(value or ""))
    if not raw:
        return None
    account = _extract_first_account_number(raw)
    if account:
        raw = normalize_spaces(raw.replace(account, " "))
    return raw or None


def _trim_after_markers(value: str | None, markers: list[str]) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    best: int | None = None
    for marker in markers:
        match = re.search(re.escape(marker), raw, flags=re.IGNORECASE)
        if match:
            pos = match.start()
            if best is None or pos < best:
                best = pos
    trimmed = raw[:best] if best is not None else raw
    return normalize_spaces(trimmed)


def _extract_label_value_regex(text: str | None, label: str) -> str | None:
    raw = str(text or "")
    if not raw:
        return None
    pattern = re.compile(re.escape(label) + r"\s*[:\-]?\s*(.+)", flags=re.IGNORECASE)
    match = pattern.search(raw)
    if not match:
        return None
    return normalize_spaces(match.group(1)) or None


def _build_email_external_id(imported: ImportedBankEmail) -> str:
    parts = {
        "imap_uid": imported.imap_uid,
        "message_id": imported.external_message_id,
        "received_at": imported.received_at.isoformat() if imported.received_at else None,
        "from_email": imported.from_email,
        "subject": imported.subject,
        "body_text": imported.body_text,
    }
    digest = hashlib.sha1(
        json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")
    ).hexdigest()
    return f"email-{digest}"


def _parse_amount_and_currency(value: str | None, *, default_currency: str = "CZK") -> tuple[int, str]:
    raw = str(value or "")
    if not raw:
        raise BankSyncError("Bankovní e-mail neobsahuje částku transakce.")
    normalized_raw = unicodedata.normalize("NFKC", raw)
    normalized_raw = normalized_raw.replace("\xa0", " ").replace("\u202f", " ")
    normalized_raw = normalize_spaces(normalized_raw)
    normalized_raw = re.sub(r"\bK[ČC]\b", "CZK", normalized_raw.upper(), flags=re.IGNORECASE)
    currency_match = re.search(r"\b([A-Z]{3})\b", normalized_raw)
    currency = currency_match.group(1) if currency_match else default_currency
    amount_region = re.sub(r"\s*[A-Z]{3}\b", "", normalized_raw).strip()
    amount_match = re.search(r"[+\-−]?\s*\d(?:[\d\s.,]*\d)?", amount_region)
    amount_numeric = amount_match.group(0) if amount_match else amount_region
    amount_numeric = normalize_spaces(amount_numeric).replace(" ", "").replace("−", "-")
    if "," in amount_numeric and "." in amount_numeric:
        if amount_numeric.rfind(",") > amount_numeric.rfind("."):
            amount_numeric = amount_numeric.replace(".", "")
        else:
            amount_numeric = amount_numeric.replace(",", "")
    return parse_money_to_signed_cents(amount_numeric or normalized_raw), currency


def parse_raiffeisenbank_cz_email(imported: ImportedBankEmail) -> ImportedBankTransaction:
    labels = [
        "Datum a čas",
        "Na účet",
        "Částka v měně účtu",
        "Kategorie pohybu",
        "Typ pohybu",
        "Z účtu",
        "Variabilní symbol",
        "Konstantní symbol",
        "Specifický symbol",
        "Zpráva pro příjemce",
    ]
    fields = _extract_label_value_blocks(imported.body_text or "", labels)
    if not fields:
        raise BankSyncError("Nepodařilo se z bankovního e-mailu vyčíst strukturovaná data.")

    booked_on = _parse_cz_datetime_to_date(fields.get(_normalize_label("Datum a čas")))
    amount_cents, currency = _parse_amount_and_currency(fields.get(_normalize_label("Částka v měně účtu")))
    movement_type = normalize_spaces(fields.get(_normalize_label("Typ pohybu")) or "")
    direction = "incoming" if "příchozí" in movement_type.lower() else "outgoing"

    body_text = imported.body_text or ""
    counterparty_block = _trim_after_markers(
        fields.get(_normalize_label("Z účtu")) or "",
        [
            "Variabilní symbol",
            "Konstantní symbol",
            "Specifický symbol",
            "Zpráva pro příjemce",
            "Tento e-mail byl vygenerován",
            "V případě dotazů",
            "Vaše Raiffeisenbank",
        ],
    )
    message = normalize_spaces(fields.get(_normalize_label("Zpráva pro příjemce")) or "") or None
    if not message:
        message = _extract_label_value_regex(body_text, "Zpráva pro příjemce")
        if message:
            message = _trim_after_markers(
                message,
                [
                    "Tento e-mail byl vygenerován",
                    "V případě dotazů",
                    "Vaše Raiffeisenbank",
                ],
            ) or None

    variable_symbol = digits_only(
        _trim_after_markers(
            fields.get(_normalize_label("Variabilní symbol")),
            [
                "Konstantní symbol",
                "Specifický symbol",
                "Zpráva pro příjemce",
                "Tento e-mail byl vygenerován",
                "V případě dotazů",
                "Vaše Raiffeisenbank",
            ],
        )
    )[:10] or None
    if not variable_symbol:
        variable_symbol_raw = _trim_after_markers(
            _extract_label_value_regex(body_text, "Variabilní symbol"),
            [
                "Konstantní symbol",
                "Specifický symbol",
                "Zpráva pro příjemce",
                "Tento e-mail byl vygenerován",
                "V případě dotazů",
                "Vaše Raiffeisenbank",
            ],
        )
        variable_symbol = digits_only(variable_symbol_raw)[:10] or None

    constant_symbol = digits_only(
        _trim_after_markers(
            fields.get(_normalize_label("Konstantní symbol")),
            [
                "Specifický symbol",
                "Zpráva pro příjemce",
                "Tento e-mail byl vygenerován",
                "V případě dotazů",
                "Vaše Raiffeisenbank",
            ],
        )
    )[:4] or None
    if not constant_symbol:
        constant_symbol_raw = _trim_after_markers(
            _extract_label_value_regex(body_text, "Konstantní symbol"),
            [
                "Specifický symbol",
                "Zpráva pro příjemce",
                "Tento e-mail byl vygenerován",
                "V případě dotazů",
                "Vaše Raiffeisenbank",
            ],
        )
        constant_symbol = digits_only(constant_symbol_raw)[:4] or None

    specific_symbol = digits_only(
        _trim_after_markers(
            fields.get(_normalize_label("Specifický symbol")),
            [
                "Zpráva pro příjemce",
                "Tento e-mail byl vygenerován",
                "V případě dotazů",
                "Vaše Raiffeisenbank",
            ],
        )
    )[:10] or None
    if not specific_symbol:
        specific_symbol_raw = _trim_after_markers(
            _extract_label_value_regex(body_text, "Specifický symbol"),
            [
                "Zpráva pro příjemce",
                "Tento e-mail byl vygenerován",
                "V případě dotazů",
                "Vaše Raiffeisenbank",
            ],
        )
        specific_symbol = digits_only(specific_symbol_raw)[:10] or None

    return ImportedBankTransaction(
        provider="email_bank_raiffeisenbank_cz",
        external_id=_build_email_external_id(imported),
        booked_on=booked_on,
        amount_cents=amount_cents,
        currency=currency,
        direction=direction,
        variable_symbol=variable_symbol,
        constant_symbol=constant_symbol,
        specific_symbol=specific_symbol,
        counterparty_account=_extract_first_account_number(counterparty_block),
        counterparty_name=_extract_account_name(counterparty_block),
        message=message,
        raw_payload={
            "email_provider": imported.provider,
            "external_message_id": imported.external_message_id,
            "received_at": imported.received_at.isoformat() if imported.received_at else None,
            "from_email": imported.from_email,
            "subject": imported.subject,
            "fields": fields,
        },
    )


def parse_csob_cz_email(imported: ImportedBankEmail) -> ImportedBankTransaction:
    labels = [
        "Účet",
        "Účet protistrany",
        "Název protistrany",
        "Datum účtování",
        "Částka",
        "Variabilní symbol",
        "Konstantní symbol",
        "Specifický symbol",
        "Zpráva pro příjemce",
    ]
    fields = _extract_label_value_blocks(imported.body_text or "", labels)
    if not fields:
        raise BankSyncError("Nepodařilo se z e-mailu ČSOB vyčíst parametry platby.")

    booked_on = _parse_cz_datetime_to_date(fields.get(_normalize_label("Datum účtování")))
    amount_cents, currency = _parse_amount_and_currency(fields.get(_normalize_label("Částka")))
    body = _normalize_email_body(imported.body_text or "")
    direction = "incoming" if "příchozí" in body.lower() else ("incoming" if amount_cents >= 0 else "outgoing")
    counterparty_account = _extract_first_account_number(fields.get(_normalize_label("Účet protistrany")))
    counterparty_name = normalize_spaces(fields.get(_normalize_label("Název protistrany")) or "") or None
    message = normalize_spaces(fields.get(_normalize_label("Zpráva pro příjemce")) or "") or None

    return ImportedBankTransaction(
        provider="email_bank_csob_cz",
        external_id=_build_email_external_id(imported),
        booked_on=booked_on,
        amount_cents=amount_cents,
        currency=currency,
        direction=direction,
        variable_symbol=digits_only(fields.get(_normalize_label("Variabilní symbol")))[:10] or None,
        constant_symbol=digits_only(fields.get(_normalize_label("Konstantní symbol")))[:4] or None,
        specific_symbol=digits_only(fields.get(_normalize_label("Specifický symbol")))[:10] or None,
        counterparty_account=counterparty_account,
        counterparty_name=counterparty_name,
        message=message,
        raw_payload={
            "email_provider": imported.provider,
            "external_message_id": imported.external_message_id,
            "received_at": imported.received_at.isoformat() if imported.received_at else None,
            "from_email": imported.from_email,
            "subject": imported.subject,
            "fields": fields,
        },
    )


def parse_csas_cz_email(imported: ImportedBankEmail) -> ImportedBankTransaction:
    labels = [
        "Směr platby",
        "Číslo účtu",
        "Číslo účtu protistrany",
        "Částka v měně transakce",
        "Částka v měně účtu",
        "Variabilní symbol",
        "Konstantní symbol",
        "Specifický symbol",
    ]
    fields = _extract_label_value_blocks(imported.body_text or "", labels)
    if not fields:
        raise BankSyncError("Nepodařilo se z e-mailu České spořitelny vyčíst informace o transakci.")

    body = _normalize_email_body(imported.body_text or "")
    amount_raw = (
        fields.get(_normalize_label("Částka v měně účtu"))
        or fields.get(_normalize_label("Částka v měně transakce"))
        or ""
    )
    amount_cents, currency = _parse_amount_and_currency(amount_raw, default_currency="CZK")

    booked_on = imported.received_at.date() if imported.received_at else date.today()
    direction_raw = normalize_spaces(fields.get(_normalize_label("Směr platby")) or "")
    direction = "incoming" if "příchozí" in direction_raw.lower() else ("incoming" if amount_cents >= 0 else "outgoing")

    counterparty_account = _extract_first_account_number(fields.get(_normalize_label("Číslo účtu protistrany")))

    return ImportedBankTransaction(
        provider="email_bank_csas_cz",
        external_id=_build_email_external_id(imported),
        booked_on=booked_on,
        amount_cents=amount_cents,
        currency=currency,
        direction=direction,
        variable_symbol=digits_only(fields.get(_normalize_label("Variabilní symbol")))[:10] or None,
        constant_symbol=digits_only(fields.get(_normalize_label("Konstantní symbol")))[:4] or None,
        specific_symbol=digits_only(fields.get(_normalize_label("Specifický symbol")))[:10] or None,
        counterparty_account=counterparty_account,
        counterparty_name=None,
        message=None,
        raw_payload={
            "email_provider": imported.provider,
            "external_message_id": imported.external_message_id,
            "received_at": imported.received_at.isoformat() if imported.received_at else None,
            "from_email": imported.from_email,
            "subject": imported.subject,
            "fields": fields,
        },
    )


def parse_fio_email_cz(imported: ImportedBankEmail) -> ImportedBankTransaction:
    body = str(imported.body_text or "").replace("\xa0", " ")
    if not body.strip():
        raise BankSyncError("E-mail od Fio neobsahuje žádné čitelné tělo zprávy.")

    def _pick(pattern: str) -> str:
        match = re.search(pattern, body, flags=re.IGNORECASE)
        return normalize_spaces(match.group(1)) if match else ""

    booked_on_dt = imported.received_at.date() if imported.received_at else date.today()
    amount_raw = _pick(r"Částka:\s*([^\n\r]+)")
    amount_cents, currency = _parse_amount_and_currency(amount_raw or "", default_currency="CZK")
    counterparty_account = _extract_first_account_number(_pick(r"Protiúčet:\s*([^\n\r]+)"))
    message = _pick(r"Zpráva příjemci:\s*([^\n\r]+)") or None
    variable_symbol = digits_only(_pick(r"VS:\s*([^\n\r]+)"))[:10] or None
    specific_symbol = digits_only(_pick(r"SS:\s*([^\n\r]+)"))[:10] or None
    constant_symbol = digits_only(_pick(r"KS:\s*([^\n\r]+)"))[:4] or None

    return ImportedBankTransaction(
        provider="email_bank_fio_email_cz",
        external_id=_build_email_external_id(imported),
        booked_on=booked_on_dt,
        amount_cents=amount_cents,
        currency=currency,
        direction="incoming" if amount_cents >= 0 else "outgoing",
        variable_symbol=variable_symbol,
        constant_symbol=constant_symbol,
        specific_symbol=specific_symbol,
        counterparty_account=counterparty_account,
        counterparty_name=None,
        message=message,
        raw_payload={
            "email_provider": imported.provider,
            "external_message_id": imported.external_message_id,
            "received_at": imported.received_at.isoformat() if imported.received_at else None,
            "from_email": imported.from_email,
            "subject": imported.subject,
            "body_text": _normalize_email_body(imported.body_text),
        },
    )


def _parse_fio_date(value: str | None) -> date:
    raw = str(value or "").strip()
    if not raw:
        raise BankSyncError("Fio API neposlalo datum transakce.")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError as exc:
        raise BankSyncError(f"Neplatné datum transakce z Fio API: {raw}") from exc


def _fio_label_values(transaction: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_value in transaction.values():
        if not isinstance(raw_value, dict):
            continue
        label = _normalize_label(raw_value.get("name"))
        value = _normalize_value(raw_value.get("value"))
        if label and value:
            values[label] = value
    return values


def _pick_label(values: dict[str, str], *labels: str) -> str:
    for label in labels:
        normalized = _normalize_label(label)
        if normalized in values and values[normalized]:
            return values[normalized]
    return ""


def _build_external_id(values: dict[str, str], raw_payload: dict[str, Any]) -> str:
    external_id = _pick_label(values, "ID pohybu", "ID transakce", "ID pokynu")
    if external_id:
        return external_id
    digest = hashlib.sha1(
        json.dumps(raw_payload, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")
    ).hexdigest()
    return f"fio-{digest}"


def parse_fio_transactions_payload(payload: dict[str, Any]) -> list[ImportedBankTransaction]:
    if not isinstance(payload, dict):
        raise BankSyncError("Fio API vrátilo neočekávaný formát.")
    statement = payload.get("accountStatement")
    if not isinstance(statement, dict):
        raise BankSyncError("Fio API neobsahuje accountStatement.")
    tx_container = statement.get("transactionList")
    if not isinstance(tx_container, dict):
        return []
    raw_transactions = tx_container.get("transaction") or []
    if isinstance(raw_transactions, dict):
        raw_transactions = [raw_transactions]
    if not isinstance(raw_transactions, list):
        return []

    parsed: list[ImportedBankTransaction] = []
    for item in raw_transactions:
        if not isinstance(item, dict):
            continue
        values = _fio_label_values(item)
        amount_cents = parse_money_to_signed_cents(_pick_label(values, "Objem", "Amount"))
        currency = (_pick_label(values, "Měna", "Currency") or "CZK").strip().upper()[:3] or "CZK"
        direction = "incoming" if amount_cents >= 0 else "outgoing"

        account = _pick_label(values, "Protiúčet", "Protiucet")
        bank_code = _pick_label(values, "Kód banky", "Kod banky")
        if account and bank_code and "/" not in account:
            account = f"{account}/{bank_code}"

        message_parts = [
            _pick_label(values, "Zpráva pro příjemce", "Zprava pro prijemce"),
            _pick_label(values, "Uživatelská identifikace", "Uzivatelska identifikace"),
            _pick_label(values, "Komentář", "Komentar"),
        ]
        message = " | ".join(part for part in message_parts if part) or None

        parsed.append(
            ImportedBankTransaction(
                provider="fio_api",
                external_id=_build_external_id(values, item),
                booked_on=_parse_fio_date(_pick_label(values, "Datum", "Datum zaúčtování", "Datum zauctovani")),
                amount_cents=amount_cents,
                currency=currency,
                direction=direction,
                variable_symbol=digits_only(_pick_label(values, "Variabilní symbol", "Variabilni symbol"))[:10] or None,
                constant_symbol=digits_only(_pick_label(values, "Konstantní symbol", "Konstantni symbol"))[:4] or None,
                specific_symbol=digits_only(_pick_label(values, "Specifický symbol", "Specificky symbol"))[:10] or None,
                counterparty_account=account or None,
                counterparty_name=(
                    _pick_label(values, "Název protiúčtu", "Nazev protiuctu", "Název účtu", "Nazev uctu")
                    or None
                ),
                message=message,
                raw_payload=item,
            )
        )
    return parsed


def fetch_fio_transactions(
    token: str,
    *,
    date_from: date,
    date_to: date,
    base_url: str,
    timeout_seconds: float = 10.0,
) -> list[ImportedBankTransaction]:
    clean_token = str(token or "").strip()
    if not clean_token:
        raise BankSyncError("Chybí Fio API token.")
    if not (base_url or "").strip():
        raise BankSyncError("Chybí FIO_API_BASE_URL.")

    url = (
        f"{base_url.rstrip('/')}/periods/{quote(clean_token, safe='')}/"
        f"{date_from.isoformat()}/{date_to.isoformat()}/transactions.json"
    )
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "fakturek bank sync",
        },
        method="GET",
    )

    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read()
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            detail = ""
        msg = f"Fio API vrátilo HTTP {getattr(exc, 'code', '?')}"
        if detail:
            msg = f"{msg}: {detail[:300]}"
        raise BankSyncError(msg) from exc
    except URLError as exc:
        raise BankSyncError(f"Nelze se připojit k Fio API: {exc}") from exc

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise BankSyncError("Fio API vrátilo neplatný JSON.") from exc

    return parse_fio_transactions_payload(payload)


def fetch_imap_bank_emails(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str = "INBOX",
    use_ssl: bool = True,
    since_uid: str | None = None,
) -> list[ImportedBankEmail]:
    clean_host = str(host or "").strip()
    clean_username = str(username or "").strip()
    clean_password = str(password or "")
    clean_mailbox = str(mailbox or "INBOX").strip() or "INBOX"
    if not clean_host or not clean_username or not clean_password:
        raise BankSyncError("IMAP schránka pro bankovní notifikace není nastavená.")

    client = imaplib.IMAP4_SSL(clean_host, port) if use_ssl else imaplib.IMAP4(clean_host, port)
    try:
        login_status, _login_data = client.login(clean_username, clean_password)
        if login_status != "OK":
            raise BankSyncError("Nelze se přihlásit do IMAP schránky.")
        select_status, _select_data = client.select(clean_mailbox, readonly=True)
        if select_status != "OK":
            raise BankSyncError(f"Nelze otevřít IMAP schránku {clean_mailbox}.")

        search_criteria = "ALL"
        if str(since_uid or "").strip().isdigit():
            search_criteria = f"{int(str(since_uid).strip()) + 1}:*"

        search_status, search_data = client.uid("search", None, search_criteria)
        if search_status != "OK":
            raise BankSyncError("Nepodařilo se vyhledat e-maily v IMAP schránce.")
        raw_uids = str((search_data[0] or b"").decode("utf-8", errors="ignore")).strip().split()

        emails: list[ImportedBankEmail] = []
        for uid in raw_uids:
            fetch_status, fetch_data = client.uid("fetch", uid, "(RFC822)")
            if fetch_status != "OK" or not fetch_data:
                continue
            raw_message = b""
            for part in fetch_data:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                    raw_message = bytes(part[1])
                    break
            if not raw_message:
                continue

            message = BytesParser(policy=policy.default).parsebytes(raw_message)
            received_at = None
            try:
                date_header = str(message.get("Date") or "").strip()
                if date_header:
                    received_at = parsedate_to_datetime(date_header)
                    if received_at is not None and received_at.tzinfo is not None:
                        received_at = received_at.astimezone().replace(tzinfo=None)
            except Exception:
                received_at = None

            header_map: dict[str, str] = {}
            for key, value in message.items():
                header_map[str(key)] = _decode_email_header(str(value))

            emails.append(
                ImportedBankEmail(
                    provider="email_bank",
                    imap_uid=str(uid),
                    external_message_id=str(message.get("Message-ID") or "").strip() or None,
                    received_at=received_at,
                    from_email=(parseaddr(str(message.get("From") or ""))[1].strip().lower() or None),
                    subject=_decode_email_header(str(message.get("Subject") or "")) or None,
                    body_text=_extract_email_body_text(message) or None,
                    raw_headers=header_map,
                )
            )
        return emails
    except imaplib.IMAP4.error as exc:
        raise BankSyncError(f"IMAP chyba: {exc}") from exc
    finally:
        try:
            client.logout()
        except Exception:
            pass
