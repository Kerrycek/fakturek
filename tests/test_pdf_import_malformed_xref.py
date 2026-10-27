"""Bounded importer regression for CVE-2026-113794 / pypdf PR #4170."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import fakturek

CHILD = r'''
from fakturek.fakturoid_import import _extract_pdf_text_lines
body = (
    b"%PDF-1.0\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
)
tail = b"\nstartxref\n%d\n%%%%EOF" % len(body)
data = body + b"xref\n0 3\n0000000000 65535 f \n" + tail
try:
    _extract_pdf_text_lines(data)
except ValueError as exc:
    assert "Unexpected EOF in Xref table." in str(exc), str(exc)
    print("malformed xref rejected safely")
else:
    raise AssertionError("malformed PDF was accepted")
'''


def test_pdf_import_rejects_truncated_xref_without_hanging():
    # A vulnerable parser hangs on this tiny synthetic input. Keep it out of
    # the test runner process and kill it if the reader stops making progress.
    result = subprocess.run(
        [sys.executable, "-c", CHILD],
        cwd=Path(fakturek.__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=5,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "malformed xref rejected safely"
