from __future__ import annotations
from fakturek.time_utils import as_utc_aware, utc_now

import json
import html as _html
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fakturek.security import validate_outbound_base_url


class CompanyLookupError(RuntimeError):
    """Raised when company lookup fails (validation, network, provider error)."""


def _validated_provider_base_url(base_url: str, *, name: str) -> str:
    try:
        return validate_outbound_base_url(base_url, name=name)
    except ValueError as exc:
        raise CompanyLookupError(str(exc)) from exc


def normalize_ico(raw: str | None) -> str:
    """Normalize Czech IČO.

    - Keep only digits.
    - Left-pad with zeros to 8 digits when shorter.
    """

    digits = re.sub(r"\D", "", (raw or "").strip())
    if not digits:
        return ""
    if len(digits) < 8:
        digits = digits.zfill(8)
    return digits


def normalize_sk_ico(raw: str | None) -> str:
    """Normalize Slovak IČO.

    In practice we accept any digit sequence and left-pad to 8 digits.

    NOTE: Unlike Czech IČO, we intentionally do **not** apply the CZ checksum
    algorithm here. SK identifiers are validated loosely.
    """

    return normalize_ico(raw)


def is_valid_sk_ico(raw: str | None) -> bool:
    """Loose validation for Slovak IČO.

    The public RPO API accepts an 8‑digit identifier. We treat:
    - exactly 8 digits
    - not all zeros
    as valid.
    """

    ico = normalize_sk_ico(raw)
    return len(ico) == 8 and ico.isdigit() and ico != "00000000"


def is_valid_ico(raw: str | None) -> bool:
    """Validate Czech IČO using the official checksum algorithm."""

    ico = normalize_ico(raw)
    if len(ico) != 8 or not ico.isdigit():
        return False

    digits = [int(c) for c in ico]
    weights = [8, 7, 6, 5, 4, 3, 2]
    s = sum(digits[i] * weights[i] for i in range(7))
    m = s % 11
    c = 11 - m
    if c == 10:
        check = 0
    elif c == 11:
        check = 1
    else:
        check = c
    return check == digits[7]


def fetch_ares_economic_subject(
    ico: str,
    *,
    base_url: str,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Fetch an economic subject from ARES (CZ) by IČO.

    Uses the modern ARES REST endpoint:
    https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}
    """

    ico_norm = normalize_ico(ico)
    if not is_valid_ico(ico_norm):
        raise CompanyLookupError("Neplatné IČO.")

    if not (base_url or "").strip():
        raise CompanyLookupError("ARES_BASE_URL není nastaveno.")
    base_url = _validated_provider_base_url(base_url, name="ARES_BASE_URL")

    url = f"{base_url.rstrip('/')}/{ico_norm}"
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "fakturek (phase-15)",
        },
        method="GET",
    )

    try:
        # The administrator-configured base URL is restricted to HTTP(S) above.
        with urlopen(req, timeout=timeout_seconds) as resp:  # nosec B310
            body = resp.read()
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            detail = ""
        msg = f"ARES vrátil chybu HTTP {getattr(exc, 'code', '?')}"
        if detail:
            msg = f"{msg}: {detail[:300]}"
        raise CompanyLookupError(msg) from exc
    except URLError as exc:
        raise CompanyLookupError(f"Nelze se připojit na ARES: {exc}") from exc

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise CompanyLookupError("ARES vrátil neplatný JSON.") from exc

    # ARES may return an error object instead of the expected DTO.
    if isinstance(payload, dict) and payload.get("kod") and payload.get("popis"):
        raise CompanyLookupError(f"ARES: {payload.get('kod')} – {payload.get('popis')}")

    if not isinstance(payload, dict):
        raise CompanyLookupError("ARES vrátil neočekávaný formát odpovědi.")

    return payload


def fetch_rpo_search(
    identifier: str,
    *,
    base_url: str,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Fetch Slovak entity data from RPO (ŠÚ SR) by identifier (IČO).

    Public endpoint:
    GET {base_url}/search?identifier={ico}

    The response contains a list under the key "results".
    """

    ico_norm = normalize_sk_ico(identifier)
    if not is_valid_sk_ico(ico_norm):
        raise CompanyLookupError("Neplatné IČO.")

    if not (base_url or "").strip():
        raise CompanyLookupError("SK_RPO_BASE_URL není nastaveno.")
    base_url = _validated_provider_base_url(base_url, name="SK_RPO_BASE_URL")

    url = f"{base_url.rstrip('/')}/search?identifier={ico_norm}"
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "fakturek (phase-16)",
        },
        method="GET",
    )

    try:
        # The administrator-configured base URL is restricted to HTTP(S) above.
        with urlopen(req, timeout=timeout_seconds) as resp:  # nosec B310
            body = resp.read()
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            detail = ""
        msg = f"RPO vrátil chybu HTTP {getattr(exc, 'code', '?')}"
        if detail:
            msg = f"{msg}: {detail[:300]}"
        raise CompanyLookupError(msg) from exc
    except URLError as exc:
        raise CompanyLookupError(f"Nelze se připojit na RPO: {exc}") from exc

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise CompanyLookupError("RPO vrátil neplatný JSON.") from exc

    if not isinstance(payload, dict):
        raise CompanyLookupError("RPO vrátil neočekávaný formát odpovědi.")

    return payload


@dataclass(frozen=True)
class CompanyPrefill:
    name: str
    street: str
    city: str
    zip: str
    country: str
    ico: str
    dic: str


def _current_or_latest_registry_record(records: Any) -> dict[str, Any]:
    """Pick the currently valid registry record from historical RPO arrays."""

    if not isinstance(records, list):
        return {}
    candidates = [item for item in records if isinstance(item, dict)]
    if not candidates:
        return {}

    active = [item for item in candidates if not str(item.get("validTo") or "").strip()]
    pool = active or candidates

    def _valid_from(item: dict[str, Any]) -> str:
        return str(item.get("validFrom") or "")

    return sorted(pool, key=_valid_from, reverse=True)[0]


def ares_payload_to_contact_prefill(payload: dict[str, Any]) -> CompanyPrefill:
    """Extract fields usable for `Contact` from an ARES payload."""

    name = str(payload.get("obchodniJmeno") or "").strip()
    ico = str(payload.get("ico") or "").strip()
    dic = str(payload.get("dic") or "").strip()

    sidlo = payload.get("sidlo")
    if not isinstance(sidlo, dict):
        sidlo = {}

    country = str(sidlo.get("kodStatu") or "CZ").strip() or "CZ"
    city = str(sidlo.get("nazevObce") or "").strip()

    zip_raw = sidlo.get("psc")
    if zip_raw is None or zip_raw == "":
        zip_raw = sidlo.get("pscTxt")
    zip_str = str(zip_raw or "").strip()

    street = ""
    street_name = str(sidlo.get("nazevUlice") or "").strip()
    if street_name:
        street = street_name

        # Prefer structured house numbers when available.
        cislo_domovni = sidlo.get("cisloDomovni")
        if cislo_domovni is None or str(cislo_domovni).strip() == "":
            cislo_domovni = sidlo.get("cisloDoAdresy")
        cislo_domovni_s = str(cislo_domovni or "").strip()

        if cislo_domovni_s:
            street = f"{street} {cislo_domovni_s}"

        cislo_orientacni = str(sidlo.get("cisloOrientacni") or "").strip()
        cislo_orientacni_pismeno = str(sidlo.get("cisloOrientacniPismeno") or "").strip()
        if cislo_orientacni:
            street = f"{street}/{cislo_orientacni}{cislo_orientacni_pismeno}"

    else:
        # As a fallback, use the textual address (may contain city/zip too).
        street = str(sidlo.get("textovaAdresa") or "").strip()

    return CompanyPrefill(
        name=name,
        street=street,
        city=city,
        zip=zip_str,
        country=country,
        ico=normalize_ico(ico) or normalize_ico(payload.get("ico")),
        dic=dic,
    )


def rpo_payload_to_contact_prefill(payload: dict[str, Any]) -> CompanyPrefill:
    """Extract fields usable for `Contact` from an RPO payload (SK)."""

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise CompanyLookupError("RPO: subjekt nenalezen.")

    first = results[0]
    if not isinstance(first, dict):
        raise CompanyLookupError("RPO vrátil neočekávaný formát výsledku.")

    # Name
    name = ""
    n0 = _current_or_latest_registry_record(first.get("fullNames"))
    if n0:
        name = str(n0.get("value") or "").replace("\n", " ")
        name = re.sub(r"\s+", " ", name).strip()

    # IČO
    ico = ""
    identifiers = first.get("identifiers")
    if isinstance(identifiers, list) and identifiers:
        i0 = identifiers[0]
        if isinstance(i0, dict):
            ico = str(i0.get("value") or "").strip()

    # Address
    street = ""
    city = ""
    zip_str = ""

    a0 = _current_or_latest_registry_record(first.get("addresses"))
    if a0:
        street_name = str(a0.get("street") or "").strip()
        building_number = str(a0.get("buildingNumber") or "").strip()
        reg_number = a0.get("regNumber")
        reg_number_s = ""
        try:
            if reg_number is not None and str(reg_number).strip() and str(reg_number).strip() != "0":
                reg_number_s = str(reg_number).strip()
        except Exception:
            reg_number_s = ""

        if street_name:
            street = street_name
            if reg_number_s:
                street = f"{street} {reg_number_s}"
            if building_number:
                street = f"{street} {building_number}" if street else building_number

        municipality = a0.get("municipality")
        if isinstance(municipality, dict):
            city = str(municipality.get("value") or "").strip()

        postal_codes = a0.get("postalCodes")
        if isinstance(postal_codes, list) and postal_codes:
            zip_str = str(postal_codes[0] or "").strip()

    return CompanyPrefill(
        name=name,
        street=street,
        city=city,
        zip=zip_str,
        country="SK",
        ico=normalize_sk_ico(ico),
        dic="",
    )


def _strip_html_to_text(raw_html: str) -> str:
    """Best-effort HTML → text for simple regex parsing."""

    # Remove scripts/styles.
    s = re.sub(
        r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>",
        " ",
        raw_html,
        flags=re.I | re.S,
    )
    # Replace <br> and </p> with separators.
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.I)
    s = re.sub(r"</\s*p\s*>", "\n", s, flags=re.I)
    # Drop remaining tags.
    s = re.sub(r"<[^>]+>", " ", s)
    # Unescape entities.
    s = _html.unescape(s)
    # Collapse whitespace.
    s = re.sub(r"[\t\r ]+", " ", s)
    s = re.sub(r"\n\s+", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def orsr_html_to_contact_prefill(raw_html: str, *, ico: str | None = None) -> CompanyPrefill:
    """Extract basic company data from ORSR HTML (fallback)."""

    text = _strip_html_to_text(raw_html)

    # Name
    name = ""
    m = re.search(r"\bObchodn[eé]\s+meno\s*:\s*([^;\n]+)", text, flags=re.I)
    if m:
        name = m.group(1).strip()

    # Seat address line (often: "Street ... . City ZIP")
    seat = ""
    m = re.search(r"\bS[ií]dlo\s*:\s*([^;\n]+)", text, flags=re.I)
    if m:
        seat = m.group(1).strip()

    # IČO
    ico_found = ""
    m = re.search(r"\bI[ČC]O\s*:\s*([0-9 ]{6,})", text, flags=re.I)
    if m:
        ico_found = re.sub(r"\D", "", m.group(1) or "").strip()

    ico_norm = normalize_sk_ico(ico_found or ico)

    street = seat
    city = ""
    zip_str = ""

    # Try to split seat into street/city/zip.
    if seat:
        seat_norm = re.sub(r"\s+", " ", seat).strip()
        m_zip = re.search(r"(\d{3}\s?\d{2})\s*$", seat_norm)
        if m_zip:
            zip_str = m_zip.group(1).replace(" ", "")
            before = seat_norm[: m_zip.start()].strip()
        else:
            before = seat_norm

        # Split by the last dot which often separates street from city.
        if "." in before:
            parts = [p.strip() for p in before.rsplit(".", 1)]
            if len(parts) == 2:
                street = parts[0]
                city = parts[1]
            else:
                street = before
        else:
            street = before

    return CompanyPrefill(
        name=name,
        street=street,
        city=city,
        zip=zip_str,
        country="SK",
        ico=ico_norm,
        dic="",
    )


def fetch_orsr_company_by_ico(
    ico: str,
    *,
    base_url: str,
    timeout_seconds: float = 5.0,
) -> CompanyPrefill:
    """Fetch and parse company data from ORSR by IČO."""

    ico_norm = normalize_sk_ico(ico)
    if not is_valid_sk_ico(ico_norm):
        raise CompanyLookupError("Neplatné IČO.")
    if not (base_url or "").strip():
        raise CompanyLookupError("SK_ORSR_BASE_URL není nastaveno.")
    base_url = _validated_provider_base_url(base_url, name="SK_ORSR_BASE_URL")

    search_url = f"{base_url.rstrip('/')}/hladaj_ico.asp?ICO={ico_norm}&SID=0"
    req = Request(
        search_url,
        headers={
            "Accept": "text/html,*/*;q=0.8",
            "User-Agent": "fakturek (phase-16)",
        },
        method="GET",
    )

    try:
        # The administrator-configured base URL is restricted to HTTP(S) above.
        with urlopen(req, timeout=timeout_seconds) as resp:  # nosec B310
            body = resp.read()
    except HTTPError as exc:
        raise CompanyLookupError(f"ORSR vrátil chybu HTTP {getattr(exc, 'code', '?')}") from exc
    except URLError as exc:
        raise CompanyLookupError(f"Nelze se připojit na ORSR: {exc}") from exc

    # Decode legacy HTML.
    try:
        html1 = body.decode("cp1250", errors="ignore")
    except Exception:
        html1 = body.decode("utf-8", errors="ignore")

    m = re.search(r"vypis\.asp\?ID=(\d+)&P=0&SID=(\d+)", html1, flags=re.I)
    if m:
        vid, p, sid = m.group(1), "0", m.group(2)
    else:
        m2 = re.search(r"vypis\.asp\?ID=(\d+)&P=(\d+)&SID=(\d+)", html1, flags=re.I)
        if not m2:
            raise CompanyLookupError("ORSR: subjekt nenalezen.")
        vid, p, sid = m2.group(1), m2.group(2), m2.group(3)

    detail_url = f"{base_url.rstrip('/')}/vypis.asp?ID={vid}&P={p}&SID={sid}"
    req2 = Request(
        detail_url,
        headers={
            "Accept": "text/html,*/*;q=0.8",
            "User-Agent": "fakturek (phase-16)",
        },
        method="GET",
    )
    try:
        # The administrator-configured base URL is restricted to HTTP(S) above.
        with urlopen(req2, timeout=timeout_seconds) as resp:  # nosec B310
            body2 = resp.read()
    except HTTPError as exc:
        raise CompanyLookupError(f"ORSR vrátil chybu HTTP {getattr(exc, 'code', '?')}") from exc
    except URLError as exc:
        raise CompanyLookupError(f"Nelze se připojit na ORSR: {exc}") from exc

    try:
        html2 = body2.decode("cp1250", errors="ignore")
    except Exception:
        html2 = body2.decode("utf-8", errors="ignore")

    prefill = orsr_html_to_contact_prefill(html2, ico=ico_norm)
    if not prefill.name:
        raise CompanyLookupError("ORSR: nepodařilo se načíst obchodní jméno.")
    return prefill


def lookup_cz_company_prefill_with_cache(
    db: Any,
    ico: str,
    *,
    base_url: str,
    timeout_seconds: float,
    cache_ttl_days: int,
) -> tuple[CompanyPrefill, str]:
    """Lookup CZ company by IČO using ARES + DB cache.

    Returns (prefill, source) where source is "cache" or "live".

    The cache is stored in `company_lookup_cache`.
    """

    # Import SQLAlchemy bits lazily to keep this module importable even when
    # the DB stack is disabled.
    from sqlalchemy import select  # type: ignore
    from sqlalchemy.exc import SQLAlchemyError  # type: ignore

    from fakturek.models import CompanyLookupCache  # type: ignore

    now = utc_now()
    ico_norm = normalize_ico(ico)
    if not is_valid_ico(ico_norm):
        raise CompanyLookupError("Neplatné IČO.")

    cached = None
    try:
        cached = db.scalar(
            select(CompanyLookupCache).where(
                CompanyLookupCache.country == "CZ",
                CompanyLookupCache.registration_no == ico_norm,
            )
        )
    except Exception:
        cached = None

    if cached is not None:
        try:
            expires_at = getattr(cached, "expires_at", None)
            if expires_at is None or as_utc_aware(expires_at) > now:
                payload = json.loads(getattr(cached, "payload_json", "{}") or "{}")
                if isinstance(payload, dict):
                    return ares_payload_to_contact_prefill(payload), "cache"
        except Exception:
            pass

    payload = fetch_ares_economic_subject(
        ico_norm,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    prefill = ares_payload_to_contact_prefill(payload)

    expires_at = now + timedelta(days=max(int(cache_ttl_days), 1))
    payload_json = json.dumps(payload, ensure_ascii=False)
    try:
        if cached is None:
            cached = CompanyLookupCache(
                country="CZ",
                registration_no=ico_norm,
                payload_json=payload_json,
                fetched_at=now,
                expires_at=expires_at,
            )
            db.add(cached)
        else:
            cached.payload_json = payload_json
            cached.fetched_at = now
            cached.expires_at = expires_at
        db.commit()
    except SQLAlchemyError:
        try:
            db.rollback()
        except Exception:
            pass

    return prefill, "live"


def lookup_sk_company_prefill_with_cache(
    db: Any,
    ico: str,
    *,
    rpo_base_url: str,
    rpo_timeout_seconds: float,
    orsr_base_url: str,
    orsr_timeout_seconds: float,
    cache_ttl_days: int,
) -> tuple[CompanyPrefill, str, str]:
    """Lookup SK company by IČO using RPO + ORSR fallback + DB cache.

    Returns (prefill, source, provider) where:
    - source is "cache" or "live"
    - provider is "rpo" or "orsr"
    """

    from sqlalchemy import select  # type: ignore
    from sqlalchemy.exc import SQLAlchemyError  # type: ignore

    from fakturek.models import CompanyLookupCache  # type: ignore

    now = utc_now()
    ico_norm = normalize_sk_ico(ico)
    if not is_valid_sk_ico(ico_norm):
        raise CompanyLookupError("Neplatné IČO.")

    cached = None
    try:
        cached = db.scalar(
            select(CompanyLookupCache).where(
                CompanyLookupCache.country == "SK",
                CompanyLookupCache.registration_no == ico_norm,
            )
        )
    except Exception:
        cached = None

    def _decode_cached_prefill(payload: Any) -> tuple[CompanyPrefill, str] | None:
        if not isinstance(payload, dict):
            return None
        provider = str(payload.get("provider") or "").strip().lower()
        data = payload.get("data")
        if provider == "rpo" and isinstance(data, dict):
            return rpo_payload_to_contact_prefill(data), "rpo"
        if provider == "orsr" and isinstance(data, dict):
            # Stored as already normalized prefill dict.
            prefill = CompanyPrefill(
                name=str(data.get("name") or ""),
                street=str(data.get("street") or ""),
                city=str(data.get("city") or ""),
                zip=str(data.get("zip") or ""),
                country=str(data.get("country") or "SK") or "SK",
                ico=normalize_sk_ico(str(data.get("ico") or ico_norm)),
                dic=str(data.get("dic") or ""),
            )
            return prefill, "orsr"
        return None

    if cached is not None:
        try:
            expires_at = getattr(cached, "expires_at", None)
            if expires_at is None or as_utc_aware(expires_at) > now:
                payload = json.loads(getattr(cached, "payload_json", "{}") or "{}")
                decoded = _decode_cached_prefill(payload)
                if decoded is not None:
                    prefill, provider = decoded
                    return prefill, "cache", provider
        except Exception:
            pass

    provider = "rpo"
    payload_for_cache: dict[str, Any]
    try:
        rpo_payload = fetch_rpo_search(
            ico_norm,
            base_url=rpo_base_url,
            timeout_seconds=rpo_timeout_seconds,
        )
        prefill = rpo_payload_to_contact_prefill(rpo_payload)
        payload_for_cache = {"provider": "rpo", "data": rpo_payload}
    except CompanyLookupError:
        provider = "orsr"
        prefill = fetch_orsr_company_by_ico(
            ico_norm,
            base_url=orsr_base_url,
            timeout_seconds=orsr_timeout_seconds,
        )
        payload_for_cache = {
            "provider": "orsr",
            "data": {
                "name": prefill.name,
                "street": prefill.street,
                "city": prefill.city,
                "zip": prefill.zip,
                "country": prefill.country,
                "ico": prefill.ico,
                "dic": prefill.dic,
            },
        }

    expires_at = now + timedelta(days=max(int(cache_ttl_days), 1))
    payload_json = json.dumps(payload_for_cache, ensure_ascii=False)
    try:
        if cached is None:
            cached = CompanyLookupCache(
                country="SK",
                registration_no=ico_norm,
                payload_json=payload_json,
                fetched_at=now,
                expires_at=expires_at,
            )
            db.add(cached)
        else:
            cached.payload_json = payload_json
            cached.fetched_at = now
            cached.expires_at = expires_at
        db.commit()
    except SQLAlchemyError:
        try:
            db.rollback()
        except Exception:
            pass

    return prefill, "live", provider
