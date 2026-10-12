"""Versioned message templates with locale fallback (FR-008/FR-042; TASK-063).

Templates are keyed by channel, purpose and locale. Resolution follows an
explicit fallback chain — exact locale, bare language, en-CA default — so a
French-ready salon degrades gracefully instead of failing when a single
translation is missing. Only active templates resolve; drafts never send.
"""

from __future__ import annotations

from sqlalchemy import text

__all__ = ["resolve_template"]


def resolve_template(connection, *, tenant_id: str, channel: str,
                     purpose: str, locale: str = "en-CA") -> dict | None:
    """Resolve the newest active template, falling back across locales.

    Chain: requested locale, its bare language (fr-CA to fr), then en-CA.
    Returns the row (id, version, subject, body, locale) or None when no
    active template exists anywhere in the chain.
    """
    locales = [locale]
    if "_" in locale or "-" in locale:
        bare = locale.replace("-", "_").split("_")[0]
        if bare != locale:
            locales.append(bare)
    if "en-CA" not in locales:
        locales.append("en-CA")
    for choice in locales:
        row = connection.execute(
            text("""SELECT id::text AS id, version, subject, body, locale
                    FROM message_templates
                    WHERE tenant_id=:tenant AND channel=:channel
                      AND purpose=:purpose AND locale=:locale AND is_active
                    ORDER BY version DESC LIMIT 1"""),
            {"tenant": tenant_id, "channel": channel,
             "purpose": purpose, "locale": choice}).mappings().one_or_none()
        if row is not None:
            return {**dict(row), "resolved_locale": choice}
    return None
