"""Terminal-safe text projection shared by runtime and presentation."""

import unicodedata


def contains_isolated_surrogate(value) -> bool:
    text = str(value)
    index = 0
    while index < len(text):
        character = ord(text[index])
        if 0xD800 <= character <= 0xDBFF:
            if index + 1 < len(text) and 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
                index += 2
                continue
            return True
        if 0xDC00 <= character <= 0xDFFF:
            return True
        index += 1
    return False


def normalize_surrogate_pairs(value) -> str:
    """Merge valid UTF-16 surrogate pairs and replace isolated records."""
    text = str(value)
    if not any("\ud800" <= character <= "\udfff" for character in text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def sanitize_terminal_text(text) -> str:
    """Remove terminal control characters while retaining text layout."""
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        character
        for character in normalized
        if character in "\n\t"
        or (
            ord(character) >= 32
            and not 127 <= ord(character) <= 159
            and unicodedata.category(character) != "Cf"
        )
    )


def sanitize_terminal_line(text) -> str:
    """Project untrusted text to one printable terminal line."""
    return " ".join(sanitize_terminal_text(text).split())


def sanitize_terminal_security_text(text) -> str:
    """Remove zero-width marks after preserving their composed text form."""
    normalized = unicodedata.normalize("NFC", sanitize_terminal_text(text))
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Mn", "Me"}
    )


def sanitize_terminal_security_line(text) -> str:
    return " ".join(sanitize_terminal_security_text(text).split())


def project_terminal_text(text, redact_text) -> str:
    """Sanitize before and after redaction so projection cannot rebuild secrets."""
    projected = sanitize_terminal_security_text(text)
    return sanitize_terminal_security_text(redact_text(projected))
