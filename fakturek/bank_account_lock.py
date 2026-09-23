"""Subject-scoped serialization for bank-account mutations.

The caller must hold the surrounding database transaction until its bank
account changes are committed or rolled back. Reads must not call this helper.
"""

from __future__ import annotations


def lock_subject_bank_account_mutations(db, *, subject_id: int) -> None:
    """Lock one subject before changing any of its bank accounts.

    SQLite ignores ``FOR UPDATE``. Its narrow raw no-op update acquires the
    writer lock without triggering SQLAlchemy's TimestampMixin ``onupdate``.
    Server databases use the explicit row-level lock instead.
    """
    from sqlalchemy import select, text

    from fakturek.models import Subject

    if db.get_bind().dialect.name == "sqlite":
        result = db.execute(
            text("UPDATE subjects SET id = id WHERE id = :subject_id"),
            {"subject_id": int(subject_id)},
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise ValueError("Subject does not exist")
        return

    subject = db.scalar(
        select(Subject.id)
        .where(Subject.id == int(subject_id))
        .with_for_update()
        .limit(1)
    )
    if subject is None:
        raise ValueError("Subject does not exist")
