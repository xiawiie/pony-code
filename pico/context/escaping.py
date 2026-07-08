"""Prompt-injection defense: break literal `<pico:*>` tag lookalikes.

Structural tags in prompts follow the `<pico:name>...</pico:name>` shape.
User- or tool-provided content that happens to contain that literal
substring must not be confusable with an actual system section — an
attacker (or an unlucky grep result) could otherwise close a section
early and forge a new one.

We take a minimal step: insert a zero-width space (U+200B) between
`pico` and `:` in any such occurrence. The rendered text looks identical
to a human reader but the token boundary is broken, so a language model
treating `pico:` as a namespace marker no longer matches.

Only the exact prefixes `<pico:` and `</pico:` are affected. Substrings
like `<picofoo:...>` are left untouched — the defense targets a narrow
namespace, not everything vaguely tag-shaped.
"""

ZWSP = "​"


def escape_pico_tags(text):
    """Return `text` with `<pico:` and `</pico:` occurrences neutralized.

    Falsy input (empty string, ``None``) is returned as-is so renderers
    can pipe optional content through without a guard.
    """
    if not text:
        return text
    return text.replace("<pico:", f"<pico{ZWSP}:").replace("</pico:", f"</pico{ZWSP}:")
