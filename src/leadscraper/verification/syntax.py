"""Step 1 — syntax & normalisation: ``email-validator`` with ``check_deliverability=False`` (no DNS
here; DNS is step 4)."""

from __future__ import annotations

from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email


@dataclass(slots=True, frozen=True)
class SyntaxResult:
    valid: bool
    normalized: str | None = None      # local part as given, domain lower-case ASCII (IDNA)
    local: str | None = None
    domain: str | None = None          # ASCII domain used for DNS
    error: str | None = None


def check_syntax(email: str) -> SyntaxResult:
    try:
        info = validate_email(email.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        return SyntaxResult(False, error=str(exc))
    domain = (info.ascii_domain or info.domain).lower()
    return SyntaxResult(True, f"{info.local_part}@{domain}", info.local_part, domain)
