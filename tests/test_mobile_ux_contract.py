from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_mobile_tables_expose_card_labels():
    contacts = _read("templates/contacts/list.html")
    payments = _read("templates/payments/index.html")
    invoice_detail = _read("templates/invoices/detail.html")

    assert "responsive-card-table contacts-mobile-table" in contacts
    assert 'data-label="E-mail"' in contacts
    contact_detail = _read("templates/contacts/detail.html")
    assert "responsive-card-table contact-invoices-mobile-table" in contact_detail
    assert 'data-label="Splatnost"' in contact_detail
    assert "responsive-card-table payments-posted-table payments-posted-mobile-table" in payments
    assert 'data-label="Uhrazeno"' in payments
    assert "responsive-card-table invoice-detail-items-mobile-table" in invoice_detail
    assert 'data-label="Jedn. cena"' in invoice_detail


def test_invoice_editor_has_real_mobile_item_layout():
    editor = _read("templates/invoices/_editor.html")
    css = _read("static/tabler-direct.css")

    for label in ["Počet", "MJ", "Popis", "Cena za MJ", "Celkem", "Akce"]:
        assert f'data-label="{label}"' in editor
    assert "Mobile workspace: compact, touch-friendly layouts" in css
    assert ".invoice-items-table tbody" in css
    assert "min-width: 0 !important" in css
    assert ".invoice-item-cell-desc" in css


def test_mobile_controls_and_navigation_are_touch_friendly():
    css = _read("static/tabler-direct.css")
    base = _read("templates/base.html")

    assert 'input:not([type="checkbox"])' in css
    assert "font-size: 1rem" in css
    assert ".navbar-collapse.show" in css
    assert "body.tabler-direct a.btn" in css
    assert "body.tabler-direct button.btn" in css
    assert "min-height: 44px" in css
    assert ".invoice-detail-hero-actions > form" in css
    assert ".import-source-list" in css
    assert ".settings-section-nav" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert ".dashboard-client-list .dashboard-info-grid" in css
    assert ".table-wrap:has(> .responsive-card-table)" in css
    assert ".responsive-card-table .admin-json-preview" in css
    app_css = _read("static/app.css")
    assert ".settings-inline-form > select" in app_css
    assert "20260903-mobile-admin-recheck" in base


def test_public_invoice_toolbar_uses_mobile_action_grid():
    template = _read("templates/invoices/print.html")

    assert "body.public-mode .toolbar-actions form.inline-form" in template
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in template
    assert "min-height: 2.75rem" in template
    assert "body.public-mode .totals-row.total .public-copy-button" in template
