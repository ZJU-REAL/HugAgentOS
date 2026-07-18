---
name: translate
description: "Translate text accurately — preserve formatting, handle plurals, and adapt tone per locale."
---

# Translate

Master accurate translation that preserves meaning, formatting, and cultural context.

## Formatting Preservation
- Never translate content inside code blocks, HTML tags, or markdown syntax
- Preserve placeholders like `{name}`, `{{variable}}`, `%s`, `$1` exactly as-is
- Keep markdown structure intact: headers, links, bold/italic formatting
- Maintain JSON/XML structure and keys — translate only values where appropriate

## Content Rules
- Don't translate: proper nouns, brand names, technical terms, URLs, email addresses
- Don't translate: code snippets, CSS classes, API endpoints, file extensions
- Preserve numbers, dates, and IDs in their original format unless locale conversion needed
- Keep consistent terminology throughout — create a glossary for repeated terms

## Language-Specific Handling
- **Plurals**: Use correct plural forms per target language rules (not English patterns)
- **Gender**: Ensure noun-adjective agreement in gendered languages (Spanish/French/German)
- **Formality**: Choose appropriate register (tu/vous, tú/usted, du/Sie) based on context
- **RTL languages**: Consider text direction for Arabic/Hebrew but keep LTR elements (URLs, numbers)

## Cultural Adaptation
- Adapt idioms and expressions rather than literal translation
- Convert units when culturally appropriate (miles↔km, Fahrenheit↔Celsius)
- Adjust date formats to locale standards (MM/DD vs DD/MM vs DD.MM)
- Use local currency symbols and number formatting (, vs . for decimals)

## Context Awareness
- Disambiguate based on context: "bank" (financial vs river), "mouse" (animal vs computer)
- Maintain document tone: formal business vs casual blog vs technical manual
- Consider target audience: children's content vs academic paper vs marketing copy
- Preserve original intent and emotional nuance, not just literal meaning

## Quality Control
- Read full context before translating to understand meaning
- Check that translated text flows naturally in target language
- Verify all formatting elements remain functional after translation
- Ensure consistent voice and terminology across the entire document