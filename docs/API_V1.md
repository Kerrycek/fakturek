# API v1

Veřejný integrační layer běží pod:

```text
/api/v1
```

## Stav po fázi 8

Hotové je teď read/write minimum pro kontakty, faktury, delivery workflow hotových dokladů, podpůrná master data pro klienty API, recurring workflow, payments/bulk/bank matching layer a nově i aktivní bank sync / ingest workflow:

- Bearer token autentizace přes personal access token scoped na jedno konkrétní IČO / subject
- `GET /api/v1/healthz`
- `GET /api/v1/openapi.json`
- `GET /api/v1/openapi.yaml`
- `GET /api/v1/docs`
- `GET /api/v1/me`
- `GET /api/v1/subjects`
- `GET /api/v1/subjects/{subject_id}`
- `GET /api/v1/subjects/{subject_id}/invoice-series`
- `GET /api/v1/subjects/{subject_id}/invoice-series/{series_id}`
- `GET /api/v1/subjects/{subject_id}/bank-accounts`
- `GET /api/v1/subjects/{subject_id}/bank-accounts/{bank_account_id}`
- `GET /api/v1/subjects/{subject_id}/catalog-items`
- `GET /api/v1/subjects/{subject_id}/catalog-items/{item_id}`
- `POST /api/v1/subjects/{subject_id}/catalog-items`
- `PATCH /api/v1/subjects/{subject_id}/catalog-items/{item_id}`
- `DELETE /api/v1/subjects/{subject_id}/catalog-items/{item_id}`
- `GET /api/v1/subjects/{subject_id}/recurring-plans`
- `GET /api/v1/subjects/{subject_id}/recurring-plans/{plan_id}`
- `POST /api/v1/subjects/{subject_id}/recurring-plans`
- `PATCH /api/v1/subjects/{subject_id}/recurring-plans/{plan_id}`
- `DELETE /api/v1/subjects/{subject_id}/recurring-plans/{plan_id}`
- `POST /api/v1/subjects/{subject_id}/recurring-plans/{plan_id}/run`
- `GET /api/v1/subjects/{subject_id}/contacts`
- `GET /api/v1/subjects/{subject_id}/contacts/{contact_id}`
- `POST /api/v1/subjects/{subject_id}/contacts`
- `PATCH /api/v1/subjects/{subject_id}/contacts/{contact_id}`
- `GET /api/v1/subjects/{subject_id}/invoices`
- `GET /api/v1/subjects/{subject_id}/invoices/{invoice_id}`
- `POST /api/v1/subjects/{subject_id}/invoices`
- `PATCH /api/v1/subjects/{subject_id}/invoices/{invoice_id}`
- `POST /api/v1/subjects/{subject_id}/invoices/{invoice_id}/issue`
- `GET /api/v1/subjects/{subject_id}/invoices/{invoice_id}/pdf`
- `GET /api/v1/subjects/{subject_id}/invoices/{invoice_id}/emails`
- `POST /api/v1/subjects/{subject_id}/invoices/{invoice_id}/send-email`
- `POST /api/v1/subjects/{subject_id}/invoices/{invoice_id}/public-link`
- `DELETE /api/v1/subjects/{subject_id}/invoices/{invoice_id}/public-link`
- `POST /api/v1/subjects/{subject_id}/invoices/bulk-action`
- `GET /api/v1/subjects/{subject_id}/invoices/{invoice_id}/payments`
- `POST /api/v1/subjects/{subject_id}/invoices/{invoice_id}/payments`
- `GET /api/v1/subjects/{subject_id}/invoices/{invoice_id}/payments/{payment_id}`
- `PATCH /api/v1/subjects/{subject_id}/invoices/{invoice_id}/payments/{payment_id}`
- `DELETE /api/v1/subjects/{subject_id}/invoices/{invoice_id}/payments/{payment_id}`
- `GET /api/v1/subjects/{subject_id}/bank-transactions`
- `GET /api/v1/subjects/{subject_id}/bank-transactions/{transaction_id}`
- `POST /api/v1/subjects/{subject_id}/bank-transactions/{transaction_id}/match`
- `POST /api/v1/subjects/{subject_id}/bank-transactions/{transaction_id}/unmatch`
- `POST /api/v1/subjects/{subject_id}/bank-accounts/{bank_account_id}/retry-matching`
- `POST /api/v1/subjects/{subject_id}/bank-sync/run`
- `POST /api/v1/subjects/{subject_id}/bank-accounts/{bank_account_id}/sync`
- `POST /api/v1/subjects/{subject_id}/bank-accounts/{bank_account_id}/import-transactions`
- `GET /api/v1/subjects/{subject_id}/bank-incoming-emails`
- `GET /api/v1/subjects/{subject_id}/bank-incoming-emails/{email_id}`
- `POST /api/v1/subjects/{subject_id}/bank-accounts/{bank_account_id}/import-email`
- `POST /api/v1/subjects/{subject_id}/bank-incoming-emails/{email_id}/reprocess`

Po této fázi už klient integrace umí:

- najít dostupné číselné řady včetně `next_number_preview`,
- vypsat bankovní účty subjektu a vybrat správný `bank_account_id`,
- spravovat katalog oblíbených položek, které může znovu použít při vytváření draftů,
- spravovat recurring plány a ručně je spouštět bez obcházení UI,
- provádět explicitní bulk workflow akce nad více doklady,
- zapisovat a upravovat manuální platby,
- ručně párovat importované bankovní transakce a znovu pustit retry matching nad účtem,
- spustit aktivní bank sync pro celý subject nebo konkrétní účet,
- importovat normalizované bankovní transakce přes API bez spoofování provideru,
- ukládat bankovní notifikační e-maily, bezpečně je reprocessnout a dohledat jejich stav.

Mimo scope teď dál zůstává import/export workflow a případný finální integrační polish kolem webhooků a správy tokenů.

## Autentizace

API nepoužívá browser session ani CSRF. Každý request musí mít:

```http
Authorization: Bearer ftk_pat_xxxxx
```

Token je navázaný na uživatele **a zároveň na jeden konkrétní subject / IČO**. Token tedy automaticky nedědí přístup ke všem subjectům daného uživatele. Oprávnění uvnitř vybraného subjectu se dál vyhodnocují přes `user_subjects`:

- `can_view` pro čtení,
- `can_edit` pro vytváření a úpravy kontaktů, katalogu a draftů dokladů,
- `can_issue` pro vystavení draftu, delivery akce nad dokladem (`issue`, `send-email`), recurring workflow (`recurring-plans` create/update/delete/run), bulk workflow, payments, bank matching a bank sync / ingest mutace.

## Vytvoření tokenu

Zatím je k dispozici CLI helper:

```bash
python tools/create_api_token.py owner --subject-id 1 --name "ERP sync"
```

Nebo podle e-mailu:

```bash
python tools/create_api_token.py owner@example.test --subject-id 1 --name "Make scenario"
```

Volitelná expirace:

```bash
python tools/create_api_token.py owner --subject-id 1 --name "Temporary" --expires-in-days 30
```

Když má uživatel přístup jen k jedinému subjectu, helper umí subject dopočítat automaticky. Pokud má subjectů víc, bez `--subject-id` skončí chybou a vypíše dostupné varianty.

Skript token vypíše jen jednou. Do databáze se ukládá pouze jeho hash.

## Rate limiting

Autentizované API requesty mají základní in-memory rate limiting per Bearer token.

- default: `240` requestů / `60` sekund,
- konfigurace přes `API_RATE_LIMIT_MAX` a `API_RATE_LIMIT_WINDOW_SECONDS`,
- při překročení vrací API `429` a hlavičku `Retry-After`.

## Idempotence pro mutace

Mutující endpointy podporují bezpečné opakování přes hlavičku:

```http
Idempotency-Key: invoice-create-2026-0001
```

Aktuálně je zapojená na:

- `POST /subjects/{subject_id}/catalog-items`
- `PATCH /subjects/{subject_id}/catalog-items/{item_id}`
- `DELETE /subjects/{subject_id}/catalog-items/{item_id}`
- `POST /subjects/{subject_id}/recurring-plans`
- `PATCH /subjects/{subject_id}/recurring-plans/{plan_id}`
- `DELETE /subjects/{subject_id}/recurring-plans/{plan_id}`
- `POST /subjects/{subject_id}/recurring-plans/{plan_id}/run`
- `POST /subjects/{subject_id}/contacts`
- `PATCH /subjects/{subject_id}/contacts/{contact_id}`
- `POST /subjects/{subject_id}/invoices`
- `PATCH /subjects/{subject_id}/invoices/{invoice_id}`
- `POST /subjects/{subject_id}/invoices/{invoice_id}/issue`
- `POST /subjects/{subject_id}/invoices/{invoice_id}/send-email`
- `POST /subjects/{subject_id}/invoices/{invoice_id}/public-link`
- `DELETE /subjects/{subject_id}/invoices/{invoice_id}/public-link`
- `POST /subjects/{subject_id}/invoices/bulk-action`
- `POST /subjects/{subject_id}/invoices/{invoice_id}/payments`
- `PATCH /subjects/{subject_id}/invoices/{invoice_id}/payments/{payment_id}`
- `DELETE /subjects/{subject_id}/invoices/{invoice_id}/payments/{payment_id}`
- `POST /subjects/{subject_id}/bank-transactions/{transaction_id}/match`
- `POST /subjects/{subject_id}/bank-transactions/{transaction_id}/unmatch`
- `POST /subjects/{subject_id}/bank-accounts/{bank_account_id}/retry-matching`
- `POST /subjects/{subject_id}/bank-sync/run`
- `POST /subjects/{subject_id}/bank-accounts/{bank_account_id}/sync`
- `POST /subjects/{subject_id}/bank-accounts/{bank_account_id}/import-transactions`
- `POST /subjects/{subject_id}/bank-accounts/{bank_account_id}/import-email`
- `POST /subjects/{subject_id}/bank-incoming-emails/{email_id}/reprocess`

Pravidla:

- stejný klíč + stejný request body vrátí uloženou původní response,
- stejný klíč + jiný body vrátí `409 idempotency_key_reused`,
- scope je na uživatele, HTTP metodu a request path.

## Příklady použití

```bash
curl -H "Authorization: Bearer ftk_pat_xxx" \
  http://127.0.0.1:8000/api/v1/me
```

```bash
curl -H "Authorization: Bearer ftk_pat_xxx" \
  "http://127.0.0.1:8000/api/v1/subjects/1/invoice-series?year=2026"
```

```bash
curl -H "Authorization: Bearer ftk_pat_xxx" \
  "http://127.0.0.1:8000/api/v1/subjects/1/bank-accounts?currency=EUR"
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: recurring-create-1" \
  -H "Content-Type: application/json" \
  -d '{
    "template_invoice_id": 123,
    "name": "Měsíční support",
    "interval_unit": "month",
    "interval_count": 1,
    "next_issue_date": "2026-04-01",
    "due_in_days": 14,
    "auto_issue": true,
    "auto_send": false
  }' \
  http://127.0.0.1:8000/api/v1/subjects/1/recurring-plans
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: recurring-run-1" \
  -H "Content-Type: application/json" \
  -d '{"force": true}' \
  http://127.0.0.1:8000/api/v1/subjects/1/recurring-plans/12/run
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: catalog-create-1" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Monitoring SLA",
    "quantity": "1",
    "unit": "měs",
    "unit_price": "990.00",
    "vat_rate": "21",
    "currency": "CZK"
  }' \
  http://127.0.0.1:8000/api/v1/subjects/1/catalog-items
```

```bash
curl -H "Authorization: Bearer ftk_pat_xxx" \
  "http://127.0.0.1:8000/api/v1/subjects/1/invoices?status=issued&document_type=invoice"
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: invoice-issue-1" \
  http://127.0.0.1:8000/api/v1/subjects/1/invoices/123/issue
```

```bash
curl -H "Authorization: Bearer ftk_pat_xxx" \
  "http://127.0.0.1:8000/api/v1/subjects/1/invoices/123/pdf?download=1" \
  --output faktura-123.pdf
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: bulk-paid-1" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "paid",
    "invoice_ids": [101, 102],
    "paid_on": "2026-03-20"
  }' \
  http://127.0.0.1:8000/api/v1/subjects/1/invoices/bulk-action
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: payment-create-1" \
  -H "Content-Type: application/json" \
  -d '{
    "paid_on": "2026-03-20",
    "amount": "1250.00",
    "note": "Ruční platba z pokladny"
  }' \
  http://127.0.0.1:8000/api/v1/subjects/1/invoices/123/payments
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: bank-match-1" \
  -H "Content-Type: application/json" \
  -d '{
    "invoice_id": 123
  }' \
  http://127.0.0.1:8000/api/v1/subjects/1/bank-transactions/987/match
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: bank-sync-account-1" \
  http://127.0.0.1:8000/api/v1/subjects/1/bank-accounts/1/sync
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: bank-import-1" \
  -H "Content-Type: application/json" \
  -d '{
    "auto_pair": true,
    "items": [
      {
        "external_id": "api-001",
        "booked_on": "2026-03-20",
        "amount": "1250.00",
        "currency": "CZK",
        "direction": "incoming",
        "variable_symbol": "20260009",
        "message": "API import úhrady"
      }
    ]
  }' \
  http://127.0.0.1:8000/api/v1/subjects/1/bank-accounts/1/import-transactions
```

```bash
curl -X POST \
  -H "Authorization: Bearer ftk_pat_xxx" \
  -H "Idempotency-Key: bank-email-import-1" \
  -H "Content-Type: application/json" \
  -d '{
    "external_message_id": "<rb-1@example.test>",
    "received_at": "2026-03-25T20:57:00",
    "from_email": "info@rb.cz",
    "subject": "Pohyb na účtě",
    "body_text": "Pohyb na účtě Datum a čas 25. 03. 2026 20:56 ..."
  }' \
  http://127.0.0.1:8000/api/v1/subjects/1/bank-accounts/2/import-email
```

## Bezpečnostní poznámky

- API záměrně **nevrací** `fio_api_token`, `raw_payload_json` bankovních transakcí ani `raw_headers_json` bankovních e-mailů.
- Detail bankovního e-mailu vrací jen krátký `body_preview`, ne celé uložené raw tělo.
- Ruční import transakcí ukládá provider `api_manual`; klient API tím nemůže předstírat, že záznam přišel z Fio API nebo IMAP feedu.
- Sync endpointy respektují stejnou matching logiku jako aplikace: měna, přesná částka, správný účet, VS / číslo dokladu a whitelist stavů faktury.

## Stránkování

List endpointy používají:

```text
?page=1&per_page=50
```

Response má tvar:

```json
{
  "items": [],
  "page": 1,
  "per_page": 50,
  "total_items": 0,
  "total_pages": 1
}
```

## Filtrování

### Faktury

Podporované query parametry:

- `q`
- `status`
- `document_type`
- `contact_id`
- `overdue`
- `issue_date_from`
- `issue_date_to`

### Katalog položek

Podporované query parametry:

- `q`
- `currency`

### Bankovní účty

Podporované query parametry:

- `currency`

### Číselné řady

Podporované query parametry:

- `year`
- `document_type`

### Recurring plány

Podporované query parametry:

- `active`
- `template_invoice_id`

### Bankovní transakce

Podporované query parametry:

- `bank_account_id`
- `matched`
- `direction`
- `provider`

## Workflow dokladu

Aktuální workflow v API je záměrně řízené, ne přes generické přepisování `status`:

- `POST /invoices` vždy vytvoří `draft`,
- `PATCH /invoices/{invoice_id}` je povolený jen pro `draft`,
- `POST /invoices/{invoice_id}/issue` změní draft na vystavený doklad a přidělí finální číslo,
- `POST /invoices/{invoice_id}/send-email` je povolený až nad stavem mimo draft a při prvním odeslání posune `issued -> sent`,
- `GET /invoices/{invoice_id}/pdf` vygeneruje PDF na vyžádání; u dokladu mimo draft ho zároveň persistuje do `pdf_path`,
- `POST /invoices/{invoice_id}/public-link` umí link zajistit nebo otočit token (`rotate=true`),
- `DELETE /invoices/{invoice_id}/public-link` link vypne,
- `POST /invoices/bulk-action` umí jen explicitně povolené přechody `issue`, `sent`, `paid`, `revert`, `cancelled`, `delete_draft`.

Podporované typy dokladů v této fázi:

- `invoice`
- `quote`
- `proforma`
- `credit_note`

API respektuje číselné řady, defaultní měnu subjektu, bankovní účet subjektu, footer preset a fixní variabilní symbol kontaktu.

## Recurring workflow

Recurring API pracuje nad existující vystavenou šablonou dokladu (`template_invoice`).

Pravidla:

- šablona musí patřit do stejného subjektu,
- šablona musí mít přiřazený kontakt,
- kreditní nota (`credit_note`) není jako šablona povolená,
- podporované intervaly jsou `week` a `month`,
- `auto_send=true` vyžaduje `auto_issue=true`.

`POST /recurring-plans/{plan_id}/run`:

- defaultně běží s body `{"force": true}` a vytvoří doklad okamžitě,
- u neaktivního plánu vrátí `409 recurring_plan_inactive`,
- novou fakturu vytvoří klonováním položek a textů ze šablony,
- při `auto_issue=true` ji rovnou vystaví,
- při `auto_send=true` ji po vystavení zkusí odeslat e-mailem.

Tokeny v textových polích šablony (`notes`, `footer_text`, `invoice items.description`) se při běhu nahrazují hodnotami:

- `{{year}}`
- `{{month}}`
- `{{month_name}}`
- `{{period_label}}`
- `{{issue_date}}`

Response plánu vrací i stav potřebný pro polling / audit:

- `last_run_at`,
- `last_generated_invoice`,
- další `next_issue_date` po přepočtu intervalu.

## Bulk workflow

`POST /invoices/bulk-action` přijímá JSON:

```json
{
  "action": "paid",
  "invoice_ids": [101, 102, 103],
  "paid_on": "2026-03-20"
}
```

Pravidla:

- API nepřijímá generické přepsání stavu; vždy se volí jedna explicitní akce,
- na každou položku vrací `result = changed/skipped/deleted`,
- `revert` vrací doklad jen o jeden krok zpět,
- u `paid` se volitelné `paid_on` propíše jen na skutečně změněné doklady,
- `delete_draft` smaže jen koncepty,
- request zpracuje maximálně prvních 200 unikátních `invoice_ids`.

## Platby a bankovní párování

`POST /payments` podporuje dvě cesty:

- manuální platbu (`paid_on`, `amount`, `note`),
- spárování importované bankovní transakce přes `bank_transaction_id`.

Pravidla:

- platbu lze přidat jen k dokladu ve stavu `issued`, `sent` nebo `paid`,
- když u manuální platby chybí `amount`, použije se celková částka dokladu,
- při přidání první platby se doklad označí jako `paid`,
- při smazání poslední platby se doklad vrátí na `sent` nebo `issued` podle předchozí historie,
- u platby navázané na bankovní transakci nejde měnit `amount` ani `paid_on`,
- `note` je volitelná, ale má limit 255 znaků.

## Bankovní transakce

`GET /bank-transactions` vrací bezpečný detail importovaných transakcí bez interního `raw_payload_json`.

`POST /bank-transactions/{transaction_id}/match` ručně vytvoří platbu a transakci naváže na doklad. API přitom vyžaduje:

- příchozí transakci,
- kladnou částku,
- stejnou měnu jako na dokladu,
- přesnou shodu částky s celkem dokladu,
- shodu bankovního účtu, pokud je na dokladu explicitně vybraný.

`POST /bank-transactions/{transaction_id}/unmatch` vazbu zruší a případnou vygenerovanou platbu odstraní.

`POST /bank-accounts/{bank_account_id}/retry-matching` znovu projde existující nespárované příchozí transakce daného účtu. Matching používá stejný klíč jako aplikace:

- nejdřív přes variabilní symbol,
- pokud chybí nebo nic nenajde, tak fallback přes číslo faktury nalezené ve zprávě,
- automaticky se spáruje jen případ s jediným jednoznačným kandidátem.

## Master data chování

### Číselné řady

Response vrací:

- `name`, `prefix`, `pad_length`,
- poslední známý čítač,
- `next_number_preview` pro zvolený rok.

Preview zohledňuje nejen uložený counter v řadě, ale i už existující doklady, takže po importech nebo ručních zásazích dává klientovi bezpečnější odhad dalšího čísla.

### Bankovní účty

Response vrací pouze bezpečná metadata potřebná pro výběr účtu při vystavení dokladu:

- `id`, `label`, `currency`, `country`,
- `account_number`, `iban`, `iban_display`, `bic`,
- `is_default`, `sort_order`,
- základní sync stav (`payment_sync_provider`, `payment_sync_enabled`, čas poslední kontroly / úspěchu, případně poslední error).

Citlivé secret hodnoty jako Fio token se přes API nevystavují.

### Katalog položek

Katalog používá stejné datové formáty jako položky faktury:

- peníze jako string (`"175.50"`),
- množství a sazby jako string (`"2.00"`, `"21.00"`).

U neplátce DPH se `vat_rate` ukládá automaticky jako `0.00`, i kdyby klient poslal jinou hodnotu.

## Delivery chování

### PDF

- endpoint vrací `application/pdf`,
- query `download=true` přepne `Content-Disposition` na attachment,
- pokud už existuje persisted PDF vystaveného dokladu, vrací se cache z disku,
- pokud neexistuje, API PDF vygeneruje a u dokladu mimo draft persistuje metadata (`pdf_path`, `pdf_hash`, `pdf_generated_at`).

### Veřejný link

`public_link` v invoice detailu vrací:

- `url` – legacy / plná veřejná view URL,
- `short_url` – krátká canonical URL,
- `pdf_url`,
- `pdf_download_url`.

`POST /public-link`:

- bez `rotate` jen zajistí existenci linku,
- s `rotate=true` vytvoří nový token a tím zneplatní starý link.

`DELETE /public-link`:

- vymaže `public_token`,
- vrátí disabled model se všemi URL jako `null`.

### E-mail

`POST /send-email` přijímá JSON:

```json
{
  "to": "optional override",
  "cc": "optional cc list",
  "subject": "optional custom subject",
  "body": "optional custom body",
  "attach_pdf": true,
  "include_public_link": true
}
```

Pravidla:

- `to` je volitelné; když chybí, použije se e-mail kontaktu,
- `cc` je volitelné,
- `subject` a `body` se dopočítají defaultně, když nejsou poslané,
- při `include_public_link=true` se link podle potřeby automaticky založí a doplní do body,
- při `attach_pdf=true` se použije persisted PDF nebo se PDF vygeneruje před odesláním,
- log každého pokusu se ukládá do `invoice_emails`.

## Formát dat

- datum: `YYYY-MM-DD`
- timestamp: `YYYY-MM-DDTHH:MM:SSZ`
- peníze: string, např. `"1250.00"`
- množství a sazby: string, např. `"2.00"`, `"21.00"`

## Chybové response

```json
{
  "error": {
    "code": "subject_access_denied",
    "message": "K tomuto subjektu nemáte přístup.",
    "details": {
      "subject_id": 2
    }
  },
  "request_id": "..."
}
```

Časté chybové kódy:

- `auth_missing_bearer`
- `auth_invalid_token`
- `subject_access_denied`
- `bank_account_not_found`
- `invoice_series_not_found`
- `catalog_item_not_found`
- `catalog_item_description_required`
- `catalog_item_quantity_invalid`
- `catalog_item_unit_price_invalid`
- `catalog_item_vat_rate_invalid`
- `recurring_plan_not_found`
- `recurring_template_not_found`
- `recurring_template_missing_contact`
- `recurring_template_credit_note_invalid`
- `recurring_interval_unit_invalid`
- `recurring_interval_count_invalid`
- `recurring_due_in_days_invalid`
- `recurring_email_override_invalid`
- `recurring_auto_send_requires_auto_issue`
- `recurring_plan_inactive`
- `contact_not_found`
- `invoice_not_found`
- `invoice_not_draft`
- `invoice_email_requires_issued_document`
- `invoice_email_recipient_invalid`
- `invoice_email_cc_invalid`
- `invoice_pdf_generation_failed`
- `smtp_not_configured`
- `smtp_missing_from_email`
- `invoice_email_send_failed`
- `bulk_action_invalid`
- `bulk_action_empty`
- `payment_not_found`
- `payment_amount_invalid`
- `payment_note_too_long`
- `payment_linked_to_bank_transaction`
- `invoice_payment_state_invalid`
- `invoice_payment_transition_invalid`
- `bank_transaction_not_found`
- `bank_transaction_already_matched`
- `bank_transaction_not_incoming`
- `bank_transaction_amount_invalid`
- `bank_transaction_currency_mismatch`
- `bank_transaction_amount_mismatch`
- `bank_transaction_bank_account_mismatch`
- `idempotency_key_reused`
- validační chyby `422` nad konkrétním polem.

## Co ještě zbývá

Rozumný zbytek roadmapy API se teď smrsknul na dva velké celky a případný finální polish:

1. **Fáze 8 – Aktivní bank sync / ingest API**
   - spuštění syncu konkrétního účtu přes API,
   - případně job-like status pro delší běhy,
   - audit importu nových transakcí a chyb syncu.

2. **Fáze 9 – Import / export workflow**
   - import runs, stav a výsledky importu,
   - export subjektových dat,
   - migrační / onboarding scénáře.

3. **Volitelný finální polish**
   - webhooky nebo polling-friendly job status endpointy,
   - správa API tokenů a jejich rotace,
   - případně jemnější rate limiting a observability pro veřejné integrace.

## Poznámky k implementaci

- Root middleware pro session auth a CSRF API cestu `/api/v1` obchází, aby se nemíchalo browser flow a integrační API s Bearer tokeny.
- OpenAPI je dostupné odděleně přímo z mountnuté subaplikace.
- Idempotentní response se persistují do tabulky `api_idempotency_keys`.
- Bulk workflow je záměrně whitelistovaný; API neumí libovolné přepsání `invoice.status`.
- Detail bankovní transakce schválně nevystavuje `raw_payload_json` ani bankovní secret hodnoty.
- Po výměně invoice items se response serializuje z čerstvě načtené ORM entity, aby API nevracelo zastaralý stav kolekcí.
- PDF endpoint commitne nově vygenerovaná metadata ještě před odesláním response, takže další detail/list už konzistentně vidí `pdf_available=true`.
