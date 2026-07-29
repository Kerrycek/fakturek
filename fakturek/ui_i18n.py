from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import re
from typing import Iterable

UI_LANGUAGE_OPTIONS: list[tuple[str, str]] = [
    ("cs", "Čeština"),
    ("en", "English"),
]

_VALID_UI_LANGUAGES = {value for value, _label in UI_LANGUAGE_OPTIONS}


def normalize_ui_language(value: object | None) -> str:
    normalized = str(value or "cs").strip().lower() or "cs"
    if normalized.startswith("en"):
        return "en"
    if normalized.startswith("cs") or normalized.startswith("cz"):
        return "cs"
    return normalized if normalized in _VALID_UI_LANGUAGES else "cs"


# Runtime translations for the application shell and ordinary app pages.
# Invoice PDFs/public invoice printouts keep using the separate invoice_language field.
UI_TRANSLATIONS_EN: dict[str, str] = {
    # Global / navigation
    "fakturek": "fakturek",
    "Fakturek.cz": "Fakturek.cz",
    "Menu": "Menu",
    "Nástěnka": "Dashboard",
    "Faktury": "Invoices",
    "Kontakty": "Contacts",
    "Automatické faktury": "Recurring invoices",
    "Statistiky": "Statistics",
    "Export/Import": "Export/Import",
    "Nastavení": "Settings",
    "Nastavení – fakturek": "Settings – fakturek",
    "Nastavení účtu a fakturace": "Account and billing settings",
    "Účet": "Account",
    "profil a heslo": "profile and password",
    "Fakturace": "Billing",
    "vystavovatel, daně, styly": "issuer, taxes, styles",
    "Banky": "Banks",
    "účty a párování plateb": "accounts and payment matching",
    "Subjekty": "Subjects",
    "další IČO, lidé a role": "more company IDs, people and roles",
    "API": "API",
    "klíče a dokumentace": "keys and documentation",
    "Zrušení účtu": "Account deletion",
    "bezpečné ukončení": "safe termination",
    "Vzhled aplikace": "Appearance",
    "Motiv aplikace": "Appearance",
    "Jazyk aplikace": "Language",
    "Jazyk prostředí": "Interface language",
    "Vzhled a jazyk": "Appearance and language",
    "Uložit vzhled": "Save appearance",
    "Nastavení platí pro celý přihlášený účet napříč všemi subjekty. Jazyk vystavených faktur a PDF se nastavuje zvlášť na konkrétní faktuře.": "These settings apply to the whole signed-in account across all organizations. The language of issued invoices and PDFs is set separately on each invoice.",
    "Stejnou volbu najdeš i v horní liště pod ikonou motivu.": "The same option is available in the top bar under the theme icon.",
    "Čeština": "Czech",
    "Angličtina": "English",
    "Podle systému": "Use system setting",
    "Podle denní doby": "By time of day",
    "Světlý motiv": "Light theme",
    "Tmavý motiv": "Dark theme",
    "Aktivní organizace": "Active organization",
    "Bez aktivní organizace": "No active organization",
    "Správa organizací": "Manage organizations",
    "Zatím nemáš přiřazenou žádnou organizaci.": "You do not have any organization assigned yet.",
    "Bez IČO": "No company ID",
    "Aktivní": "Active",
    "Neaktivní": "Inactive",
    "Owner": "Owner",
    "Manager": "Manager",
    "Účetní": "Accountant",
    "Pouze čtení": "Read-only",
    "Odhlásit": "Log out",
    "Přihlásit": "Log in",
    "Přihlášení": "Login",
    "Registrovat": "Sign up",
    "Vytvořil": "Created by",
    "v roce": "in",
    "Fakturek pro klidnější fakturaci.": "Fakturek for calmer invoicing.",
    "Podmínky": "Terms",
    "GDPR": "Privacy",
    "Kontakt": "Contact",
    "VIEW-AS READ-ONLY": "VIEW-AS READ-ONLY",
    "Ukončit view-as": "Stop view-as",

    # Common actions / states
    "Uloženo.": "Saved.",
    "Nastavení bylo aktualizováno.": "Settings have been updated.",
    "Chyba:": "Error:",
    "Chyba": "Error",
    "Zpět": "Back",
    "Zpět na faktury": "Back to invoices",
    "Zpět na kontakty": "Back to contacts",
    "Zavřít": "Close",
    "Zrušit": "Cancel",
    "Uložit": "Save",
    "Uložit změny": "Save changes",
    "Uložit účet": "Save account",
    "Vyčistit": "Clear",
    "Vyhledat": "Search",
    "Filtrovat": "Filter",
    "Kopírovat": "Copy",
    "Zkopírováno": "Copied",
    "Upravit": "Edit",
    "Smazat": "Delete",
    "Vytvořit": "Create",
    "Přidat": "Add",
    "Otevřít": "Open",
    "Stáhnout": "Download",
    "Stáhnout PDF": "Download PDF",
    "Otevřít PDF": "Open PDF",
    "Vytisknout": "Print",
    "Náhled": "Preview",
    "Další": "Next",
    "Předchozí": "Previous",
    "← Předchozí": "← Previous",
    "Další →": "Next →",
    "Vše": "All",
    "Ano": "Yes",
    "Ne": "No",
    "Volitelné": "Optional",
    "Povinné": "Required",
    "výchozí": "default",
    "Výchozí": "Default",
    "Bez expirace": "No expiration",
    "30 dní": "30 days",
    "90 dní": "90 days",
    "1 rok": "1 year",
    "7 dní": "7 days",
    "14 dní": "14 days",
    "Na stránku": "Per page",
    "Výsledky": "Results",
    "Žádné výsledky.": "No results.",
    "Zatím nic k zobrazení.": "Nothing to show yet.",
    "Zatím bez dokladů.": "No documents yet.",
    "Zatím bez položek.": "No line items yet.",
    "Založeno": "Created",
    "Vytvořeno": "Created",
    "Upraveno": "Updated",
    "Poslední změna": "Last change",
    "Poslední kontrola": "Last check",
    "Poslední login": "Last login",
    "Naposledy použito": "Last used",
    "Čas": "Time",
    "Stav": "Status",
    "Důvod": "Reason",
    "Poznámka": "Note",
    "Poznámky": "Notes",
    "Interní poznámka": "Internal note",
    "Přehled": "Overview",
    "Zobrazení": "Viewing",
    "Úpravy": "Editing",
    "Vystavování": "Issuing",
    "Zapisovatelné": "Writable",
    "Oprávnění": "Permissions",
    "Nebezpečná akce": "Dangerous action",
    "Teď důležité": "Important now",
    "Co si pohlídat": "What to watch",
    "Otevřené doklady": "Open documents",
    "Po splatnosti": "Overdue",
    "Objem otevřených": "Open amount",
    "Limit DPH": "VAT limit",
    "LIMIT DPH": "VAT LIMIT",
    "Pod limitem": "Under limit",
    "Nad 1. limitem": "Over first limit",
    "Nad 2. limitem": "Over second limit",
    "Orientační obrat započtený z vystavených faktur v CZK za aktuální kalendářní rok.": "Indicative turnover counted from issued invoices in CZK for the current calendar year.",
    "Započteno faktur:": "Counted invoices:",
    "Od 1. 1. 2025 se v ČR pro povinnou registraci k DPH sleduje obrat za kalendářní rok. Tady jde o orientační přehled podle vystavených faktur.": "Since 1 Jan 2025, Czech mandatory VAT registration is based on turnover for the calendar year. This is an indicative overview based on issued invoices.",
    "Faktury v jiné měně než CZK nejsou do součtu zahrnuté.": "Invoices in currencies other than CZK are not included in the total.",
    "Hledat kontakt": "Search contact",
    "Zatím žádné kontakty.": "No contacts yet.",
    "FAKTURY CELKEM": "TOTAL INVOICES",
    "Faktury celkem": "Total invoices",
    "Exportovat faktury": "Export invoices",
    "Kontakty CSV": "Contacts CSV",
    "Faktury CSV": "Invoices CSV",
    "Faktury XML": "Invoices XML",
    "Vystav fakturu rovnou, nebo si ji ulož jako koncept. Koncept má jen interní DRAFT-ID a finální číslo dostane až při vystavení.": "Issue the invoice right away, or save it as a draft. A draft only has an internal DRAFT ID and gets its final number when issued.",
    "Pro vystavení faktury je potřeba založit aspoň jednoho odběratele.": "To issue an invoice, create at least one customer first.",

    # Account/settings
    "Můj profil": "My profile",
    "Uživatelské jméno": "Username",
    "E-mail": "Email",
    "E-mail účtu": "Account email",
    "Stav účtu": "Account status",
    "Poslední přihlášení": "Last login",
    "Platformová role": "Platform role",
    "Běžný účet": "Regular account",
    "Zabezpečení": "Security",
    "Změna hesla": "Change password",
    "Současné heslo": "Current password",
    "Nové heslo": "New password",
    "Nové heslo znovu": "New password again",
    "Uložit nové heslo": "Save new password",
    "Alespoň 8 znaků.": "At least 8 characters.",
    "Relace": "Session",
    "Platnost přihlášení": "Login validity",
    "Nechat přihlášeného nejdéle": "Keep me signed in for at most",
    "Uložit platnost": "Save validity",
    "Ukončit používání Fakturku": "Stop using Fakturek",
    "Smazat účet": "Delete account",
    "Zrušit účet": "Cancel account",
    "Potvrzení": "Confirmation",
    "Heslo": "Password",
    "Důvod / poznámka": "Reason / note",
    "Opiš přesně:": "Type exactly:",
    "SMAZAT ÚČET": "DELETE ACCOUNT",
    "Subjekty s přístupem": "Subjects with access",
    "Subjekty bez jiného vlastníka": "Subjects without another owner",
    "Faktury a doklady": "Invoices and documents",
    "Aktivní API klíče": "Active API keys",
    "Uživatelské rozhraní": "User interface",
    "Jazyk celého prostředí Fakturku. Nemění jazyk už vystavených faktur ani PDF; ten se nastavuje zvlášť u faktury.": "Language of the whole Fakturek interface. It does not change already issued invoices or PDFs; invoice language is set separately on each invoice.",
    "Uložit jazyk": "Save language",
    "Vybraný jazyk se použije pro navigaci, nastavení, seznamy, formuláře a administraci.": "The selected language is used for navigation, settings, lists, forms and administration.",

    # API/settings cards
    "API přístup": "API access",
    "Base URL": "Base URL",
    "Kontrola API": "API check",
    "Krátkodobý limit": "Short-term limit",
    "Měsíční quota": "Monthly quota",
    "Otevřít API dokumentaci": "Open API documentation",
    "Nový klíč": "New key",
    "Vytvořit API klíč": "Create API key",
    "Název klíče": "Key name",
    "Platnost": "Validity",
    "Subjekt / IČO": "Subject / company ID",
    "Vyber subjekt": "Select subject",
    "Zkušební prostředí": "Sandbox environment",
    "API může zapisovat": "API can write",
    "API může vystavovat": "API can issue",
    "API může exportovat": "API can export",
    "Vytvořené klíče": "Created keys",
    "Režim:": "Mode:",
    "zkušební": "sandbox",
    "ostrý": "live",
    "čtení": "read",
    "zápis": "write",
    "vystavení": "issue",
    "export": "export",

    # Subject / issuer / billing settings
    "Fakturační údaje": "Billing details",
    "Základní údaje": "Basic details",
    "Název": "Name",
    "Název subjektu": "Subject name",
    "Jméno / firma": "Name / company",
    "Ulice": "Street",
    "Město": "City",
    "PSČ": "ZIP code",
    "Země": "Country",
    "Země (2 znaky)": "Country (2 letters)",
    "IČO": "Company ID",
    "DIČ": "VAT ID",
    "IČO / DIČ": "Company ID / VAT ID",
    "IČO / subjekt": "Company ID / subject",
    "Subject / IČO": "Subject / company ID",
    "Telefon": "Phone",
    "Typ subjektu": "Subject type",
    "Podnikatel / OSVČ": "Sole trader",
    "Firma": "Company",
    "Spolek / nezisková organizace": "Association / non-profit",
    "Jiný subjekt": "Other subject",
    "Daňový režim": "Tax regime",
    "Klasické přiznání a přehledy": "Standard tax return and reports",
    "Paušální daň": "Flat tax",
    "Pásmo paušální daně": "Flat-tax band",
    "Příjmový profil": "Income profile",
    "Plátce DPH": "VAT payer",
    "Identifikovaná osoba k DPH": "VAT identified person",
    "Výchozí měna": "Default currency",
    "Výchozí styl faktury": "Default invoice style",
    "Vzhled PDF faktury": "Invoice PDF appearance",
    "Standard": "Standard",
    "Klasický": "Classic",
    "Minimal": "Minimal",
    "Patička faktury": "Invoice footer",
    "Vlastní text": "Custom text",
    "Bez patičky": "No footer",
    "Načíst z registru": "Load from registry",
    "Uložit fakturační údaje": "Save billing details",
    "Už vystavené faktury": "Already issued invoices",
    "Přegenerovat uložená PDF": "Regenerate stored PDFs",
    "Přegenerovat 1 PDF": "Regenerate 1 PDF",
    "Bankovní účty": "Bank accounts",
    "Bankovní účet": "Bank account",
    "Přidat účet": "Add account",
    "Upravit účet": "Edit account",
    "Nový samostatný účet": "New separate account",
    "Název / štítek": "Name / label",
    "Číslo účtu": "Account number",
    "Země účtu": "Account country",
    "Měna účtu": "Account currency",
    "Výchozí účet": "Default account",
    "Automatické párování plateb": "Automatic payment matching",
    "Kopírovat účet": "Copy account",
    "Kopírovat IBAN": "Copy IBAN",
    "Kopírovat BIC": "Copy BIC",
    "Kopírovat VS": "Copy reference",
    "Banka pro e-mailové notifikace": "Bank for email notifications",
    "Zatím tu není žádný bankovní účet.": "There is no bank account yet.",

    # Invoices / documents
    "Faktura": "Invoice",
    "Doklad": "Document",
    "Doklady": "Documents",
    "Nabídka": "Quote",
    "Dobropis": "Credit note",
    "Zálohová faktura": "Proforma invoice",
    "Nová faktura": "New invoice",
    "+ Nová faktura": "+ New invoice",
    "+ Nový doklad": "+ New document",
    "Vystavené doklady": "Issued documents",
    "Číslo": "Number",
    "Číslo dokladu": "Document number",
    "Odběratel": "Customer",
    "Dodavatel": "Supplier",
    "Kupující": "Buyer",
    "Položky": "Line items",
    "Položky dokladu": "Line items",
    "Popis": "Description",
    "Množství": "Quantity",
    "Jedn. cena": "Unit price",
    "Cena": "Price",
    "DPH": "VAT",
    "Včetně DPH": "Incl. VAT",
    "Celkem": "Total",
    "Mezisoučet": "Subtotal",
    "Sleva": "Discount",
    "Zaokrouhlení": "Rounding",
    "Částka": "Amount",
    "Měna": "Currency",
    "Období": "Period",
    "Datum vystavení": "Issue date",
    "Datum splatnosti": "Due date",
    "DUZP": "Taxable supply date",
    "Datum úhrady": "Payment date",
    "Celkem k úhradě": "Amount due",
    "Uhrazeno": "Paid",
    "Zaplaceno": "Paid",
    "Koncept": "Draft",
    "Vystavená": "Issued",
    "Odeslaná": "Sent",
    "Stornovaná": "Cancelled",
    "draft": "draft",
    "vystavená": "issued",
    "odeslaná": "sent",
    "zaplacená": "paid",
    "stornovaná": "cancelled",
    "Způsob platby": "Payment method",
    "Bankovní převod": "Bank transfer",
    "Převodem": "Bank transfer",
    "Hotově": "Cash",
    "Kartou": "Card",
    "Dobírkou": "Cash on delivery",
    "Variabilní symbol": "Reference number",
    "BIC / SWIFT": "BIC / SWIFT",
    "Jazyk faktury": "Invoice language",
    "Styl faktury": "Invoice style",
    "Přidat položku": "Add line item",
    "+ Přidat položku": "+ Add line item",
    "Označit jako zaplacenou": "Mark as paid",
    "Smazat doklad": "Delete document",
    "Duplikovat doklad": "Duplicate document",
    "Poslat e-mail": "Send email",
    "Poslat upomínku": "Send reminder",
    "Připomenout": "Remind",
    "Veřejný odkaz": "Public link",
    "Přidat veřejný odkaz": "Add public link",
    "Přiložit PDF": "Attach PDF",
    "Předmět": "Subject",
    "Odeslat": "Send",
    "Odesílatel": "Sender",
    "Kopie mně": "Copy me",
    "Všechny kontaktní e-maily": "All contact emails",
    "Lze zadat více adres oddělených čárkou nebo středníkem.": "You can enter multiple addresses separated by commas or semicolons.",
    "SMTP není nastavené. Doplň": "SMTP is not configured. Fill in",
    "a případně": "and optionally",
    "Splatnost": "Due date",
    "Splatnost za dní": "Due in days",
    "Název opakování": "Recurring name",
    "Opakování": "Recurrence",
    "Každý kolikátý interval": "Every N intervals",
    "Další vystavení": "Next issue date",
    "V den běhu vystavit jako ostrý doklad": "Issue as a final document on run day",
    "Po vystavení rovnou poslat e-mailem": "Send by email after issuing",
    "+ Nová automatická faktura": "+ New recurring invoice",
    "Zpět na automatické faktury": "Back to recurring invoices",

    # Contacts
    "Seznam kontaktů": "Contact list",
    "Nový kontakt": "New contact",
    "+ Nový kontakt": "+ New contact",
    "Upravit kontakt": "Edit contact",
    "Detail kontaktu": "Contact detail",
    "Rychlé doplnění": "Quick fill",
    "Pevný VS": "Fixed reference",
    "Zkontrolovat teď": "Check now",

    "Platba": "Payment",
    "Platby": "Payments",
    "Platba převodem": "Bank transfer payment",
    "Instrukce k úhradě": "Payment instructions",
    "Pokračovat v platbě": "Continue payment",
    "Ověřit stav": "Check status",
    "Objednávka služby": "Service order",
    "Souhlasím s": "I agree with",
    "obchodními podmínkami": "the terms and conditions",
    "ochranu osobních údajů": "the privacy policy",
    "a beru na vědomí": "and acknowledge",
    "Aktuální období od": "Current period from",
    "Aktuální období do": "Current period until",
    "Poslední platba": "Last payment",
    "Poslední výzva k úhradě": "Last payment request",
    "Bezplatný režim": "Free mode",

    # Setup/warnings/access
    "Na tuhle akci nemáš práva": "You do not have permission for this action",
    "Vystavování dokladů je vypnuté": "Document issuing is disabled",
    "Úpravy jsou zamčené": "Editing is locked",
    "Přístup k téhle části není povolený": "Access to this section is not allowed",
    "Prohlížet faktury": "View invoices",
    "Přejít na export/import": "Go to export/import",
    "Chybí fakturační údaje": "Billing details are missing",
    "Chybí bankovní účet": "Bank account is missing",
    "Chybí:": "Missing:",
}

# A few common option labels that appear outside the templates too.
UI_TRANSLATIONS_EN.update(
    {
        "Týdně": "Weekly",
        "Měsíčně": "Monthly",
        "Živnostenský rejstřík": "Trade Register",
        "Obchodní rejstřík": "Commercial Register",
        "Spolkový rejstřík": "Association Register",
        "Fyzická osoba zapsaná v živnostenském rejstříku.": "Sole trader registered in the Trade Register.",
        "Společnost zapsaná v obchodním rejstříku.": "Company registered in the Commercial Register.",
        "Spolek zapsaný ve spolkovém rejstříku.": "Association registered in the Association Register.",
    }
)


# Page-level copy that is rendered directly by the current templates. Keeping it
# here lets the language switch cover the whole app shell without touching the
# per-invoice document language used for PDFs and public print views.
UI_TRANSLATIONS_EN.update(
    {
        # Dashboard
        "Přehled | fakturek": "Dashboard | fakturek",
        "Teď důležité": "Important now",
        "Co si pohlídat": "What to watch",
        "Poslední doklady": "Recent documents",
        "Naposledy vystavené": "Recently issued",
        "Otevřené doklady": "Open documents",
        "Objem otevřených": "Open amount",
        "Roční výkon": "Yearly performance",
        "Nejsilnější letos": "Strongest this year",
        "Rychlé srovnání roků": "Quick year comparison",
        "Ve statistikách": "In statistics",
        "Všechny doklady": "All documents",
        "Otevřít statistiky": "Open statistics",
        "Otevřít vystavené": "Open issued documents",
        "První limit": "First limit",
        "Druhý limit": "Second limit",
        "Započteno faktur:": "Invoices counted:",
        "Orientační obrat započtený z vystavených faktur v CZK za aktuální kalendářní rok.": "Approximate turnover counted from issued invoices in CZK for the current calendar year.",
        "Od 1. 1. 2025 se v ČR pro povinnou registraci k DPH sleduje obrat za kalendářní rok. Tady jde o orientační přehled podle vystavených faktur.": "Since 1 Jan 2025, VAT registration thresholds in the Czech Republic are tracked by calendar-year turnover. This is an approximate overview based on issued invoices.",

        # Setup warnings
        "Chybí fakturační údaje vystavovatele": "Issuer billing details are missing",
        "Chybí: ulice, PSČ": "Missing: street, postal code",
        "Chybí: bankovní účet / IBAN": "Missing: bank account / IBAN",
        "Doplnit údaje": "Fill in details",
        "Doplň fakturační údaje v nastavení. Na vystavené faktuře se pak nebudou lámat údaje napůl ani mizet povinné informace.": "Fill in billing details in settings. Issued invoices will then stop splitting details awkwardly or omitting required information.",
        "Přidej účet v nastavení. Nové faktury ho předvyberou rovnou v hlavní části formuláře, ne schovaný v dalších možnostech.": "Add an account in settings. New invoices will preselect it directly in the main form, not hide it under advanced options.",

        # Settings / appearance / billing
        "Aktuální subjekt": "Current subject",
        "Subjekty a přístupy": "Subjects and access",
        "Přidání dalšího IČO · Uživatelé a role pro aktuální subjekt": "Add another company ID · Users and roles for the current subject",
        "Přidat IČO": "Add company ID",
        "Přidat bankovní účet": "Add bank account",
        "Nastavit přístupy": "Set access",
        "Self-service přehled pro aktuálně vybraný subjekt": "Self-service overview for the currently selected subject",
        "Už vystavené doklady": "Already issued documents",
        "Po doplnění fakturačních údajů nebo bankovního účtu tím obnovíš PDF všech vystavených, odeslaných a zaplacených faktur z aktuálního nastavení.": "After filling in billing details or a bank account, this regenerates PDFs for all issued, sent and paid invoices from the current settings.",
        "Výchozí vzhled PDF faktury": "Default invoice PDF style",
        "Čistý výchozí vzhled pro běžné faktury.": "Clean default style for regular invoices.",
        "Konzervativnější papírový styl pro účetní a instituce.": "A more conservative paper style for accountants and institutions.",
        "Úsporná černobílá varianta s minimem barev.": "Compact black-and-white variant with minimal color.",
        "Výchozí patička faktury": "Default invoice footer",
        "Vlastní text patičky": "Custom footer text",
        "Vyplň jen pokud chceš používat volbu „Vlastní text“.": "Fill this in only if you want to use the \"Custom text\" option.",
        "Použije se jako výchozí text pro nové faktury tohoto subjektu.": "Used as the default text for new invoices of this subject.",
        "Použije se pro nové faktury tohoto subjektu a propíše se do interního náhledu, veřejného odkazu i PDF.": "Used for new invoices of this subject and shown in the internal preview, public link and PDF.",
        "Zdroj: databáze (subject)": "Source: database (subject)",
        "Zdanění": "Taxation",
        "Ovlivní jen orientační daňové hlídání a výchozí viditelnost polí. Faktury tím neměníme.": "Only affects approximate tax monitoring and default field visibility. It does not change invoices.",
        "U spolku nebo nekomerčního subjektu se na přehledu schová orientační limit DPH i paušální pásma.": "For associations or non-commercial subjects, the dashboard hides the approximate VAT limit and flat-tax bands.",
        "Klasické přiznání hlídá jen DPH limit neplátce. Paušální pásma se řeší pouze v paušálním režimu.": "The standard tax return only tracks the non-payer VAT limit. Flat-tax bands are handled only in flat-tax mode.",
        "Profil příjmů pro paušál": "Income profile for flat tax",
        "První pásmo není vždy 1 500 000 Kč. Hranice závisí na tom, jaká část příjmů spadá do činností s 80% / 60% výdajovým paušálem.": "The first band is not always CZK 1,500,000. The threshold depends on how much income falls under activities with 80% / 60% expense rates.",
        "Vyber pásmo, které chceš hlídat v paušálním režimu.": "Select the band you want to monitor in flat-tax mode.",
        "I. pásmo": "Band I",
        "II. pásmo": "Band II",
        "III. pásmo": "Band III",
        "Alespoň 75 % příjmů z činností s 80% výdaji": "At least 75% of income from activities with 80% expenses",
        "Alespoň 75 % příjmů z činností s 80% nebo 60% výdaji": "At least 75% of income from activities with 80% or 60% expenses",
        "Bez převahy činností s 80% / 60% výdaji": "No majority of activities with 80% / 60% expenses",
        "Pro přeshraniční EU povinnosti neplátce. Nezapíná DPH na běžných tuzemských fakturách jako plátce DPH.": "For cross-border EU obligations of a non-payer. It does not enable VAT on ordinary domestic invoices as a VAT payer.",
        "Posílat daňová upozornění e-mailem": "Send tax alerts by email",
        "E-mail pro upozornění": "Alert email",
        "Volitelné. Když pole necháš prázdné, použije se e-mail účtu (demo@example.test). Kontrola probíhá při otevření přehledu a po dosažení nového pásma pošle jen jeden e-mail.": "Optional. If you leave this blank, the account email (demo@example.test) is used. Checks run when opening the dashboard and send only one email after reaching a new threshold.",
        "Přehled účtů pro faktury a párování plateb. Podrobnosti a technické nastavení jsou schované u konkrétního účtu.": "Overview of accounts for invoices and payment matching. Details and technical settings are stored on each specific account.",
        "Bez automatického párování": "No automatic matching",
        "Jakmile vybereš Fio API nebo e-mail banky, Fakturek bude příchozí platby kontrolovat automaticky a při shodě označí fakturu jako zaplacenou.": "Once you select Fio API or bank email, Fakturek will check incoming payments automatically and mark invoices as paid on match.",
        "Token při uložení rovnou otestujeme proti Fio API. Po prvním zapnutí začne párování od této chvíle.": "The token is tested against the Fio API immediately when saved. After first enabling it, matching starts from that moment.",
        "Tajný klíč": "Secret key",
        "Tajný klíč ukládáme šifrovaně a nikdy ho nevypisujeme zpět.": "We store the secret key encrypted and never display it again.",
        "Testovací režim": "Test mode",
        "Země platební metody": "Payment method country",
        "Uložit údaje vystavovatele": "Save issuer details",
        "CZ – Česká republika": "CZ - Czech Republic",
        "CZK – Česká koruna": "CZK - Czech koruna",
        "USD – Americký dolar": "USD - US dollar",
        "GBP – Britská libra": "GBP - British pound",
        "HUF – Maďarský forint": "HUF - Hungarian forint",
        "PLN – Polský zlotý": "PLN - Polish zloty",
        "ČSOB": "CSOB",
        "Česká spořitelna": "Ceska sporitelna",
        "Bez záznamu": "No record",
        "aktuální": "current",
        "2500 / měsíc": "2500 / month",
        "Možnosti": "Options",
        "Všechny": "All",
        "Vystavené": "Issued",
        "1 týden": "1 week",
        "2 týdny": "2 weeks",
        "Po uplynutí zvolené doby tě aplikace automaticky odhlásí i v případě, že cookie v prohlížeči ještě existuje.": "After the selected time, the app will log you out automatically even if the browser cookie still exists.",
        "Pro změnu hesla je potřeba zadat současné heslo. Nové heslo musí mít alespoň 8 znaků.": "To change your password, enter the current password. The new password must be at least 8 characters.",
        "Účet se nejdřív deaktivuje, zneplatní se API klíče a vypnou se bankovní synchronizace u subjektů, kde jsi jediný vlastník. Vystavené doklady se nemažou okamžitě bez bezpečné retenční lhůty.": "The account is first deactivated, API keys are invalidated, and bank syncs are disabled for subjects where you are the only owner. Issued documents are not deleted immediately without a safe retention period.",
        "Tady si vytvoříš osobní API klíč pro integrace, skripty nebo jiný server. Každý klíč je navázaný jen na jedno konkrétní IČO / subject a po vytvoření se ukáže jen jednou.": "Create a personal API key for integrations, scripts or another server. Each key is tied to one specific company ID / subject and is shown only once after creation.",
        "Povinné. Název uvidíš v seznamu klíčů, samotný token se ukáže jen jednou.": "Required. The name appears in the key list; the token itself is shown only once.",
        "Nejdřív pojmenuj klíč a vyber oprávnění. Tlačítko níže patří k tomuhle formuláři.": "First name the key and choose permissions. The button below belongs to this form.",
        "Umožní vytvářet a upravovat data přes API.": "Allows creating and editing data through the API.",
        "Umožní přes API vystavit nebo odeslat doklady.": "Allows issuing or sending documents through the API.",
        "Umožní stahovat exporty a soubory pro účetnictví.": "Allows downloading exports and accounting files.",
        "Klíč může volat sandbox endpointy bez uložení reálných dokladů. Ostré změny mu API odmítne.": "The key can call sandbox endpoints without storing real documents. Production changes will be rejected by the API.",
        "Zatím tu nemáš žádný API klíč.": "You do not have any API keys yet.",

        # Invoices list / bulk actions
        "Faktury, nabídky, dobropisy a zálohovky": "Invoices, quotes, credit notes and proforma invoices",
        "Jeden seznam pro běžné faktury, nabídky, dobropisy i zálohové faktury. Typ dokladu poznáš hned v řádku a můžeš ho i filtrovat.": "One list for regular invoices, quotes, credit notes and proforma invoices. The document type is visible directly in the row and can be filtered.",
        "+ Nabídka": "+ Quote",
        "+ Zálohová faktura": "+ Proforma invoice",
        "Nabídky": "Quotes",
        "Zálohové faktury": "Proforma invoices",
        "Všichni": "Everyone",
        "Hromadná úprava": "Bulk edit",
        "Vyber si faktury na aktuální stránce a jedním krokem je uprav.": "Select invoices on the current page and update them in one step.",
        "Vybráno": "Selected",
        "dokladů": "documents",
        "Vybrat vše na stránce": "Select all on page",
        "Označit jako zaplacené": "Mark as paid",
        "Označit jako odeslané": "Mark as sent",
        "Vrátit na vystavenou": "Return to issued",
        "Vrátit o krok zpět": "Move one step back",
        "Provést hromadně": "Apply in bulk",
        "Po úpravě fakturačních údajů nebo banky můžeš přegenerovat uložená PDF z aktuálního nastavení.": "After changing billing details or bank settings, you can regenerate stored PDFs from the current settings.",
        "· CSV export respektuje aktuální filtry": "· CSV export respects the current filters",

        # Contacts
        "Jméno": "Name",
        "Vyhledávání, stránkování a rychlá navigace do detailu nebo nové faktury bez zbytečného hluku kolem.": "Search, pagination and quick navigation to detail or a new invoice without unnecessary noise.",
        "kontaktů": "contacts",

        # Statistics
        "Přehled fakturace po rocích a měsících. Měna grafů je": "Billing overview by years and months. Chart currency is",
        "Aktivní rok": "Active year",
        "Vyfakturováno celkem": "Total invoiced",
        "Vyfakturováno celkem v CZK": "Total invoiced in CZK",
        "Po splatnosti teď": "Overdue now",
        "Posledních 12 měsíců": "Last 12 months",
        "Rolling přehled a cashflow": "Rolling overview and cash flow",
        "Jen skutečné nezrušené faktury. Vyfakturováno se počítá podle data vystavení, zaplaceno podle data úhrady.": "Only real, non-cancelled invoices. Invoiced is counted by issue date, paid by payment date.",
        "Platné faktury / uhrazené": "Valid invoices / paid",
        "Vystavené faktury": "Issued invoices",
        "Vývoj po letech": "Yearly trend",
        "Rychlý pohled na to, v jakých letech jsi fakturoval nejvíc.": "A quick view of which years had the highest invoicing.",
        "Měsíce": "Months",
        "Měny": "Currencies",
        "Součty za rok 2026": "Totals for 2026",
        "Každý řádek ukazuje vyfakturovanou částku celkem v měně CZK.": "Each row shows the total invoiced amount in CZK.",
        "2026 po měsících": "2026 by month",
        "Únor": "February",
        "Březen": "March",
        "Květen": "May",
        "Červen": "June",
        "Červenec": "July",
        "Září": "September",
        "Říjen": "October",
        "Úno. 26": "Feb 26",
        "Bře. 26": "Mar 26",
        "Kvě. 26": "May 26",
        "Čer. 26": "Jun 26",
        "Zář. 25": "Sep 25",
        "Říj. 25": "Oct 25",

        # Import/export
        "Jedno místo pro zálohy, CSV exporty i nahrávání dat z Fakturoidu bez lovení funkcí po celé aplikaci.": "One place for backups, CSV exports and uploading data from Fakturoid without hunting for features across the app.",
        "Export faktur na míru": "Custom invoice export",
        "Vyfiltruj si období, klienty i typ dokladu a vyber si CSV, XML, ISDOC ZIP, jedno PDF nebo ZIP s jednotlivými PDF.": "Filter by period, clients and document type, then choose CSV, XML, ISDOC ZIP, one PDF or a ZIP with separate PDFs.",
        "Co chceš nahrát": "What do you want to upload",
        "Už to není jen “import z Fakturoidu”. Vyber si, co zrovna nahráváš, a stránka ti napoví, co od toho čekat.": "It is no longer just a \"Fakturoid import\". Choose what you are uploading and the page will tell you what to expect.",
        "Tohle hledání filtruje samotné faktury. Pro pohodlný výběr konkrétních klientů použij pole níže.": "This search filters the invoices themselves. To select specific clients comfortably, use the field below.",
        "Není vybraný žádný klient, takže se vezmou všichni.": "No client is selected, so all clients will be included.",
        "Žádný klient neodpovídá zadanému hledání.": "No client matches the search.",
        "Když nic nevybereš, vezmou se všichni klienti. Filtrování nahoře funguje okamžitě při psaní.": "If you do not select anything, all clients are included. Filtering above works immediately while typing.",
        "Výstup": "Output",
        "`CSV + položky v ZIPu` je dobré pro další zpracování, `XML` pro strukturovaný export a PDF varianty pro předání nebo archiv.": "`CSV + items in ZIP` is good for further processing, `XML` for structured export, and PDF variants for handoff or archiving.",
        "CSV + položky v ZIPu": "CSV + items in ZIP",
        "CSV přehled faktur": "Invoice CSV overview",
        "Jeden sloučený PDF": "One merged PDF",
        "Jednotlivé PDF v ZIPu": "Separate PDFs in ZIP",
        "Všechny stavy": "All statuses",
        "Všechny typy": "All types",
        "Odeslané": "Sent",
        "Zaplacené": "Paid",
        "Stornované": "Cancelled",
        "Rychlé exporty": "Quick exports",
        "Hotové zkratky pro kontakty nebo kompletní zálohu celé aplikace.": "Ready-made shortcuts for contacts or a complete backup of the whole app.",
        "Lehký export kontaktů pro účetní, CRM nebo kontrolu dat.": "Lightweight contact export for accountants, CRM or data checks.",
        "Rychlý přehled faktur bez dalšího nastavování.": "Quick invoice overview without additional setup.",
        "Kompletní ZIP": "Complete ZIP",
        "Všechno podstatné najednou včetně položek, účtů, auditů a e-mailového logu.": "Everything important at once, including items, accounts, audits and the email log.",
        "Záloha a přenos dat": "Backup and data transfer",
        "Co dnes umíme zpracovat hned": "What we can process today",
        "Fakturoid, obecné CSV kontaktů, XML/ISDOC faktury, PDF/ZIP a cílené XML importy pro POHODA a Money S3.": "Fakturoid, generic contact CSV, XML/ISDOC invoices, PDF/ZIP and targeted XML imports for POHODA and Money S3.",
        "XML faktury, CSV kontakty nebo ZIP z Fakturoidu. Nejbezpečnější cesta pro kompletní migraci.": "XML invoices, contact CSV or a ZIP from Fakturoid. The safest path for a complete migration.",
        "Jednodušší import kontaktů z jiného systému. Hodí se pro CRM exporty nebo ručně upravené CSV.": "Simpler contact import from another system. Useful for CRM exports or manually adjusted CSV files.",
        "Samotné faktury v XML, ISDOC nebo ZIP s XML. Dobré pro strukturovaný přesun bez kontaktového CSV.": "Invoices only in XML, ISDOC or a ZIP with XML. Good for structured migration without contact CSV.",
        "Import ISDOC faktur ve formátu .isdoc, XML nebo ZIP s ISDOC soubory.": "Import ISDOC invoices in .isdoc, XML or ZIP with ISDOC files.",
        "Doplňkový import PDF faktur nebo ZIPu s PDF. Vhodné hlavně pro archiv a dohledání podkladů.": "Supplementary import of invoice PDFs or a ZIP with PDFs. Best for archive and document lookup.",
        "Skutečný import faktur z POHODA XML včetně partnera, položek, bankovního účtu a VS.": "Real invoice import from POHODA XML, including partner, items, bank account and variable symbol.",
        "Strukturovaný import vydaných faktur z Money S3 XML včetně partnera, položek a platebních údajů.": "Structured import of issued invoices from Money S3 XML, including partner, items and payment details.",
        "Nahrát soubor": "Upload file",
        "Poslední importy": "Recent imports",
        "Přehled toho, co už proběhlo, co čeká na zpracování a kam se vrátit pro detail běhu.": "Overview of what has already run, what is waiting for processing and where to return for run details.",
        "Zatím žádné importy.": "No imports yet.",
    }
)


UI_TRANSLATIONS_EN.update(
    {
        # Extra coverage for the signed-in application shell and common empty states.
        "Volitelné. Když pole necháš prázdné, použije se e-mail účtu (smoke-owner@example.test).": "Optional. If you leave the field empty, the account email will be used (smoke-owner@example.test).",
        "Kontrola probíhá při otevření přehledu a po dosažení nového pásma pošle jen jeden e-mail.": "The check runs when the overview is opened and sends only one email after a new threshold is reached.",
        "Volitelné. Když pole necháš prázdné, použije se e-mail účtu (smoke-owner@example.test). Kontrola probíhá při otevření přehledu a po dosažení nového pásma pošle jen jeden e-mail.": "Optional. If you leave the field empty, the account email will be used (smoke-owner@example.test). The check runs when the overview is opened and sends only one email after a new threshold is reached.",
        "Párování": "Matching",
        "Vybereš jen způsob párování a zbytek si fakturek dopočítá sám. U bankovních e-mailů stačí zvolit banku.": "Choose only the matching method and Fakturek will handle the rest. For bank emails, selecting the bank is enough.",
        "teď umíme přímo párovat Raiffeisenbank CZ, ČSOB i Fio e-mail. Starší maily po prvním zapnutí nenačítáme zpětně, párování začne až od tohoto okamžiku.": "we can now match Raiffeisenbank CZ, ČSOB and Fio emails directly. Older emails are not loaded retroactively after first enabling; matching starts from that moment.",
        "Uložit změny účtu": "Save account changes",
        "Zatím tu nejsou žádné vystavené doklady.": "There are no issued documents yet.",
        "Technický přehled": "Technical overview",
        "Prostředí": "Environment",
        "Zatím žádné faktury.": "No invoices yet.",
        "Doklady – fakturek": "Documents – fakturek",
        "Dobropis": "Credit note",
        "Dobropisy": "Credit notes",
        "Koncepty": "Drafts",
        "Hledat": "Search",
        "Typ": "Type",
        "Jen po splatnosti": "Overdue only",
        "Zobrazeno:": "Shown:",
        "z": "of",
        "Vypnuto": "Off",
        "0–0 z 0": "0–0 of 0",
        "Faktura / platba": "Invoice / payment",
        "Doklad —": "Document —",
        "Platba —": "Payment —",
        "1 den": "1 day",
        "3 dny": "3 days",
        "Vyfakturováno včetně DPH v CZK": "Invoiced incl. VAT in CZK",
        "Vyfakturováno včetně DPH": "Invoiced incl. VAT",
        "Zatím tu nejsou data pro roční přehled.": "There is no data for the yearly overview yet.",
        "Každý řádek ukazuje vyfakturovanou částku včetně DPH v měně CZK.": "Each row shows the invoiced amount including VAT in CZK.",
        "Pro vybraný rok tu zatím nejsou žádné částky.": "There are no amounts for the selected year yet.",
        "Pro vybraný rok tu zatím nejsou žádné faktury.": "There are no invoices for the selected year yet.",
        "Automatické faktury – fakturek": "Recurring invoices - fakturek",
        "Správa všech opakovaných dokladů na jednom místě. Nové opakování můžeš založit z detailu konkrétní faktury nebo rovnou jako úplně novou automatickou fakturu.": "Manage all recurring documents in one place. You can create a new recurrence from a specific invoice detail or directly as a completely new recurring invoice.",
        "Aktivní a pozastavená opakování": "Active and paused recurrences",
        "Zatím tu není žádná automatická faktura.": "There is no recurring invoice yet.",
        "Můžeš vybrat existující fakturu jako šablonu, nebo si rovnou založit úplně novou automatickou fakturu včetně opakování.": "You can select an existing invoice as a template, or create a completely new recurring invoice including the recurrence.",
        "Vybrat existující fakturu": "Select an existing invoice",
        "Banky a párování plateb": "Banks and payment matching",
        "Rychlý přehled synchronizace účtů a plateb, které ještě čekají na spárování.": "A quick overview of account synchronization and payments still waiting to be matched.",
        "Poslední úspěch": "Last success",
        "Zaúčtované": "Recorded",
        "Spárované a ručně zadané platby": "Matched and manually entered payments",
        "Zatím tu není žádná zaúčtovaná platba.": "There is no recorded payment yet.",
        "Nespárované": "Unmatched",
        "Příchozí platby bez faktury": "Incoming payments without an invoice",
        "Tady jsou importované příchozí transakce, které ještě nejsou navázané na fakturu. Spárovat jde jen kandidáty se stejnou částkou a měnou.": "Imported incoming transactions that are not linked to an invoice yet are shown here. Only candidates with the same amount and currency can be matched.",
        "Žádná nespárovaná příchozí platba. Takhle to má vypadat.": "No unmatched incoming payment. This is how it should look.",
        "Vytvořil": "Created by",
        "v roce": "in",
        "Fakturek pro klidnější fakturaci.": "Fakturek for calmer invoicing.",
        "Podmínky": "Terms",
        "Ochrana osobních údajů": "Privacy policy",
    }
)


UI_ATTRIBUTE_TRANSLATIONS_EN: dict[str, str] = {
    key: value
    for key, value in UI_TRANSLATIONS_EN.items()
    if key not in {"SMAZAT ÚČET", "DELETE ACCOUNT"}
}


def ui_translation_payload(language: object | None = None) -> dict[str, object]:
    lang = normalize_ui_language(language)
    if lang != "en":
        return {"language": lang, "translations": {}, "attributeTranslations": {}}
    return {
        "language": "en",
        "translations": UI_TRANSLATIONS_EN,
        "attributeTranslations": UI_ATTRIBUTE_TRANSLATIONS_EN,
    }

_TRANSLATABLE_ATTRS = {"title", "aria-label", "placeholder", "alt", "value"}
_RAW_TEXT_TAGS = {"script", "style"}


def _split_outer_whitespace(value: str) -> tuple[str, str, str]:
    match = re.match(r"^(\s*)(.*?)(\s*)$", value, flags=re.DOTALL)
    if not match:
        return "", value, ""
    return match.group(1), match.group(2), match.group(3)


def _normalized_space(value: str) -> str:
    return " ".join(str(value or "").split())


def _translate_dynamic_ui_text(normalized: str) -> str | None:
    # Company identifiers are often rendered together with a dynamic company
    # name/role, so exact dictionary keys cannot cover every combination.
    match = re.fullmatch(r"30 dní • do ([0-9.]+)", normalized)
    if match:
        return f"30 days • until {match.group(1)}"
    match = re.fullmatch(r"· Zaváděcí sleva (.+)", normalized)
    if match:
        return f"· Introductory discount {match.group(1)}"
    match = re.fullmatch(r"/ (\d+) měsíců / (\d+) IČO", normalized)
    if match:
        return f"/ {match.group(1)} months / {match.group(2)} company ID"
    match = re.fullmatch(r"IČO (.+?) · (\d+) dokladů", normalized)
    if match:
        count = match.group(2)
        noun = "document" if count == "1" else "documents"
        return f"Company ID {match.group(1)} · {count} {noun}"

    if normalized.startswith("IČO "):
        return "Company ID " + normalized[4:]
    if " · IČO " in normalized:
        return normalized.replace(" · IČO ", " · Company ID ")
    if " • IČO " in normalized:
        return normalized.replace(" • IČO ", " • Company ID ")
    if ", IČO " in normalized:
        return normalized.replace(", IČO ", ", company ID ")

    count_patterns = [
        (r"(\d+) dokladů", "document", "documents"),
        (r"(\d+) aktivních filtrů", "active filter", "active filters"),
        (r"(\d+) k prověření", "to review", "to review"),
        (r"(\d+) nespárováno", "unmatched", "unmatched"),
        (r"(\d+) nálezů", "result", "results"),
        (r"(\d+) refund návrhů", "refund proposal", "refund proposals"),
        (r"(\d+) záznamů se stacktrace", "record with stacktrace", "records with stacktrace"),
        (r"(\d+) řádků logu", "log line", "log lines"),
        (r"(\d+) API klíčů", "API key", "API keys"),
        (r"(\d+) evidovaných plateb", "recorded payment", "recorded payments"),
        (r"(\d+) použito za 30 dní", "use in 30 days", "uses in 30 days"),
        (r"(\d+) aktivních IČO", "active company ID", "active company IDs"),
        (r"(\d+) kontaktů", "contact", "contacts"),
        (r"(\d+) nových účtů", "new account", "new accounts"),
        (r"(\d+) odesláno/vystaveno", "sent/issued", "sent/issued"),
        (r"(\d+) včetně šablon", "including templates", "including templates"),
        (r"(\d+) migrací v repu", "migration in repo", "migrations in repo"),
        (r"(\d+) souborů", "file", "files"),
    ]
    for pattern, singular, plural in count_patterns:
        match = re.fullmatch(pattern, normalized)
        if match:
            count = match.group(1)
            noun = singular if count == "1" else plural
            return f"{count} {noun}"

    match = re.fullmatch(r"(\d+) faktur", normalized)
    if match:
        count = match.group(1)
        noun = "invoice" if count == "1" else "invoices"
        return f"{count} {noun}"
    month_abbr = {
        "Led.": "Jan", "Úno.": "Feb", "Bře.": "Mar", "Dub.": "Apr",
        "Kvě.": "May", "Čvn.": "Jun", "Čvc.": "Jul", "Srp.": "Aug",
        "Zář.": "Sep", "Říj.": "Oct", "Lis.": "Nov", "Pro.": "Dec",
    }
    for cs_month, en_month in month_abbr.items():
        if normalized.startswith(cs_month + " "):
            return en_month + normalized[len(cs_month):]

    match = re.fullmatch(r"(.+?) · (\d+) dokladů", normalized)
    if match:
        count = match.group(2)
        noun = "document" if count == "1" else "documents"
        return f"{match.group(1)} · {count} {noun}"
    match = re.fullmatch(r"(\d+) aktivní / (\d+) revokované", normalized)
    if match:
        return f"{match.group(1)} active / {match.group(2)} revoked"
    if normalized.startswith("+ "):
        suffix = normalized[2:].strip()
        if suffix in UI_TRANSLATIONS_EN:
            return "+ " + UI_TRANSLATIONS_EN[suffix]
    if normalized.startswith("Použit "):
        return "Used " + normalized[len("Použit "):]
    if normalized.startswith("Přidáno "):
        return "Added " + normalized[len("Přidáno "):]
    if normalized.startswith("Vytvořen "):
        return "Created " + normalized[len("Vytvořen "):]
    if normalized.endswith(" · Odesláno"):
        return normalized[:-len("Odesláno")] + "Sent"
    match = re.fullmatch(r"z (\d+) kontaktů", normalized)
    if match:
        return f"of {match.group(1)} contacts"
    if normalized == "účtů k prověření":
        return "accounts to review"
    if normalized == "účtů s loginem za 30 dní":
        return "accounts with login in 30 days"

    match = re.fullmatch(r"Aktuální placené období běží do (.+)\.", normalized)
    if match:
        return f"The current paid period runs until {match.group(1)}."
    return None


def translate_ui_text(text: object, language: object | None = None) -> str:
    raw = str(text or "")
    if normalize_ui_language(language) != "en":
        return raw
    if raw in UI_TRANSLATIONS_EN:
        return UI_TRANSLATIONS_EN[raw]

    leading, inner, trailing = _split_outer_whitespace(raw)
    normalized = _normalized_space(inner)
    if normalized in UI_TRANSLATIONS_EN:
        return f"{leading}{UI_TRANSLATIONS_EN[normalized]}{trailing}"

    # Handle a handful of very common tiny inflections around punctuation.
    if normalized.endswith(":") and normalized[:-1] in UI_TRANSLATIONS_EN:
        return f"{leading}{UI_TRANSLATIONS_EN[normalized[:-1]]}:{trailing}"
    if normalized.endswith(".") and normalized[:-1] in UI_TRANSLATIONS_EN:
        return f"{leading}{UI_TRANSLATIONS_EN[normalized[:-1]]}.{trailing}"

    dynamic = _translate_dynamic_ui_text(normalized)
    if dynamic is not None:
        return f"{leading}{dynamic}{trailing}"
    return raw

# Additional language coverage for statistics and export/import pages.
UI_TRANSLATIONS_EN.update(
    {
        "Vyber rok": "Choose year",
        "Včetně DPH": "Including VAT",
        "Bez DPH": "Excluding VAT",
        "Pouze DPH": "VAT only",
        "Vyfakturováno": "Invoiced",
        "Zaplaceno": "Paid",
        "faktur": "invoices",
        "uhrazeno": "paid",
        "Zatím tu nejsou data pro roční přehled.": "No yearly overview data yet.",
        "Pro vybraný rok tu zatím nejsou žádné vystavené faktury.": "No issued invoices for the selected year yet.",
        "Pro vybraný rok tu zatím nejsou žádné částky.": "No amounts for the selected year yet.",
        "Mimo hlavní měnu je v tomto roce ještě": "Outside the main currency, this year also has",
        "Workflow": "Workflow",
        "Stavy faktur za rok": "Invoice statuses for",
        "Měna": "Currency",
        "Celkem": "Total",
        "Stav": "Status",
        "Počet": "Count",
        "Hledat": "Search",
        "číslo faktury / klient / IČO": "invoice number / client / company ID",
        "Datum od": "Date from",
        "Datum do": "Date to",
        "Typ dokladu": "Document type",
        "Jen po splatnosti": "Overdue only",
        "Klienti": "Clients",
        "Začni psát název klienta, IČO nebo e-mail": "Start typing a client name, company ID or email",
        "Bez doplňujících údajů": "No extra details",
        "Exportovat faktury": "Export invoices",
        "Rychlé exporty": "Quick exports",
        "Záloha a přenos dat": "Backup and data transfer",
        "Hotové zkratky pro kontakty nebo kompletní zálohu celé aplikace.": "Ready-made shortcuts for contacts or a complete backup of the whole app.",
        "Kontakty CSV": "Contacts CSV",
        "Faktury CSV": "Invoices CSV",
        "Kompletní ZIP": "Complete ZIP",
        "Lehký export kontaktů pro účetní, CRM nebo kontrolu dat.": "Lightweight contact export for accountants, CRM or data checks.",
        "Rychlý přehled faktur bez dalšího nastavování.": "Quick invoice overview without additional setup.",
        "Všechno podstatné najednou včetně položek, účtů, auditů a e-mailového logu.": "Everything important at once, including items, accounts, audits and the email log.",
        "Exporty jsou dostupné jen uživatelům s oprávněním Exportovat.": "Exports are available only to users with export permission.",
        "Vystavit výzvu k úhradě": "Create payment request",
        "Cena": "Price",
        "měsíců / 1 IČO": "months / 1 company ID",
        "Doklad": "Document",
        "Číslo": "Number",
        "Částka": "Amount",
        "Vystaveno": "Issued on",
        "Veřejný odkaz": "Public link",
        "Poslední výzva k úhradě": "Last payment request",
        "Splatnost": "Due date",
        "Vystavil": "Issued by",
        "Výzva k úhradě se vystavuje v účtu": "The payment request is issued in the account",
        "Online platba": "Online payment",
        "Transakce": "Transaction",
        "Založeno": "Created",
        "Pokračovat v platbě": "Continue payment",
        "Ověřit stav": "Check status",
        "Roky": "Years",
        "Export a import dat": "Export and import data",
        "Součty za rok": "Year totals",
        "po měsících": "by month",
        "Placené období": "Paid period",
        "Aktuální období od": "Current period from",
        "Aktuální období do": "Current period until",
        "Poslední platba": "Last payment",
        "Poslední výzva k úhradě je uhrazená. Další platební instrukce se zobrazí až po vystavení nové výzvy.": "The last payment request is paid. New payment instructions will appear after a new request is issued.",
    }
)


class _HtmlUiTranslator(HTMLParser):
    def __init__(self, language: str):
        super().__init__(convert_charrefs=False)
        self.language = normalize_ui_language(language)
        self.parts: list[str] = []
        self._raw_stack: list[str] = []

    def _translate_attr(self, tag: str, name: str, value: str | None) -> str | None:
        if value is None:
            return None
        attr = str(name or "").lower()
        if tag.lower() == "html" and attr == "lang":
            return self.language
        if attr in _TRANSLATABLE_ATTRS:
            return translate_ui_text(value, self.language)
        return value

    def _format_attrs(self, tag: str, attrs: Iterable[tuple[str, str | None]]) -> str:
        rendered: list[str] = []
        for name, value in attrs:
            if value is None:
                rendered.append(str(name))
                continue
            translated = self._translate_attr(tag, name, value)
            rendered.append(f'{name}="{escape(str(translated), quote=True)}"')
        return (" " + " ".join(rendered)) if rendered else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(f"<{tag}{self._format_attrs(tag, attrs)}>")
        if tag.lower() in _RAW_TEXT_TAGS:
            self._raw_stack.append(tag.lower())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(f"<{tag}{self._format_attrs(tag, attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")
        if self._raw_stack and self._raw_stack[-1] == tag.lower():
            self._raw_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._raw_stack:
            self.parts.append(data)
        else:
            self.parts.append(translate_ui_text(data, self.language))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")

    def output(self) -> str:
        return "".join(self.parts)


def translate_html_document(html: str, language: object | None = None) -> str:
    lang = normalize_ui_language(language)
    if lang != "en" or not html:
        return html
    parser = _HtmlUiTranslator(lang)
    try:
        parser.feed(html)
        parser.close()
        return parser.output()
    except Exception:
        return html
