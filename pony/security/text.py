"""Terminal-safe text projection shared by runtime and presentation."""


def sanitize_terminal_text(text) -> str:
    """Remove terminal control characters while retaining text layout."""
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        character
        for character in normalized
        if character in "\n\t"
        or (ord(character) >= 32 and not 127 <= ord(character) <= 159)
    )


def sanitize_terminal_line(text) -> str:
    """Project untrusted text to one printable terminal line."""
    return " ".join(sanitize_terminal_text(text).split())
