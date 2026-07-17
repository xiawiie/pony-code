"""Prompt-injection defense: break literal `<pony:*>` tag lookalikes.

Structural tags in prompts follow the `<pony:name>...</pony:name>` shape.
User- or tool-provided content that happens to contain that literal
substring must not be confusable with an actual system section — an
attacker (or an unlucky grep result) could otherwise close a section
early and forge a new one.

We take a minimal step: insert a zero-width space (U+200B) between
`pony` and `:` in any such occurrence. The rendered text looks identical
to a human reader but the token boundary is broken, so a language model
treating `pony:` as a namespace marker no longer matches.

Only the exact prefixes `<pony:` and `</pony:` are affected. Substrings
like `<ponyfoo:...>` are left untouched — the defense targets a narrow
namespace, not everything vaguely tag-shaped.
"""

ZWSP = "​"


def escape_pony_tags(text):
    """Return `text` with `<pony:` and `</pony:` occurrences neutralized.

    Falsy input (empty string, ``None``) is returned as-is so renderers
    can pipe optional content through without a guard.
    """
    if not text:
        return text
    return text.replace("<pony:", f"<pony{ZWSP}:").replace("</pony:", f"</pony{ZWSP}:")
