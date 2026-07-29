from __future__ import annotations

from email.message import EmailMessage

import fakturek.emailing as emailing


def test_split_recipients_and_validation():
    assert emailing.split_recipients("a@example.com") == ["a@example.com"]
    assert emailing.split_recipients(" a@example.com ; b@example.com ") == [
        "a@example.com",
        "b@example.com",
    ]
    assert emailing.looks_like_email("a@example.com")
    assert not emailing.looks_like_email("nope")


def test_build_email_message_with_pdf_attachment():
    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    msg = emailing.build_email_message(
        from_email="sender@example.com",
        from_name="Sender",
        to_emails=["rcpt@example.com"],
        subject="Test",
        body="Hello",
        attachment_pdf=("invoice.pdf", pdf),
    )

    assert isinstance(msg, EmailMessage)
    assert "sender@example.com" in str(msg["From"])
    assert "rcpt@example.com" in str(msg["To"])
    assert str(msg["Subject"]) == "Test"

    # When an attachment exists, EmailMessage becomes multipart.
    assert msg.get_content_maintype() == "multipart"

    atts = list(msg.iter_attachments())
    assert len(atts) == 1
    att = atts[0]
    assert att.get_filename() == "invoice.pdf"
    assert att.get_content_type() == "application/pdf"


def test_build_email_message_adds_html_alternative_with_clickable_links():
    msg = emailing.build_email_message(
        from_email="sender@example.com",
        from_name="Sender",
        to_emails=["rcpt@example.com"],
        subject="Faktura",
        body="Dobrý den,\n\nFaktura je tady: https://fakturek.cz/i/test-token/2026-0001\n<script>alert(1)</script>",
    )

    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    assert plain is not None
    assert html is not None
    assert "https://fakturek.cz/i/test-token/2026-0001" in plain.get_content()
    html_body = html.get_content()
    assert '<a href="https://fakturek.cz/i/test-token/2026-0001">https://fakturek.cz/i/test-token/2026-0001</a>' in html_body
    assert "<script>" not in html_body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_body


def test_send_via_smtp_uses_starttls(monkeypatch):
    calls: list[str] = []

    class DummySMTP:
        def __init__(self, host: str, port: int, timeout: float):
            self.host = host
            self.port = port
            self.timeout = timeout

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self, context=None):
            calls.append("starttls")

        def login(self, username: str, password: str):
            calls.append("login")

        def send_message(self, msg: EmailMessage):
            calls.append("send_message")

        def quit(self):
            calls.append("quit")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(emailing.smtplib, "SMTP", DummySMTP)

    cfg = emailing.SMTPConfig(
        host="smtp.example.com",
        port=587,
        username="u",
        password="p",
        use_tls=False,
        use_starttls=True,
        timeout_seconds=5,
        from_email="sender@example.com",
        from_name="",
    )

    msg = emailing.build_email_message(
        from_email="sender@example.com",
        from_name=None,
        to_emails=["rcpt@example.com"],
        subject="Hello",
        body="Body",
    )

    message_id, debug = emailing.send_via_smtp(cfg, msg)
    assert debug == "sent"
    assert message_id

    assert "ehlo" in calls
    assert "starttls" in calls
    assert "login" in calls
    assert "send_message" in calls
    assert "quit" in calls


def test_send_via_smtp_uses_tls(monkeypatch):
    calls: list[str] = []

    class DummySMTPSSL:
        def __init__(self, host: str, port: int, timeout: float, context=None):
            self.host = host
            self.port = port
            self.timeout = timeout

        def ehlo(self):
            calls.append("ehlo")

        def login(self, username: str, password: str):
            calls.append("login")

        def send_message(self, msg: EmailMessage):
            calls.append("send_message")

        def quit(self):
            calls.append("quit")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(emailing.smtplib, "SMTP_SSL", DummySMTPSSL)

    cfg = emailing.SMTPConfig(
        host="smtp.example.com",
        port=465,
        username="u",
        password="p",
        use_tls=True,
        use_starttls=False,
        timeout_seconds=5,
        from_email="sender@example.com",
        from_name="",
    )

    msg = emailing.build_email_message(
        from_email="sender@example.com",
        from_name=None,
        to_emails=["rcpt@example.com"],
        subject="Hello",
        body="Body",
    )

    message_id, debug = emailing.send_via_smtp(cfg, msg)
    assert debug == "sent"
    assert message_id
    assert "send_message" in calls
