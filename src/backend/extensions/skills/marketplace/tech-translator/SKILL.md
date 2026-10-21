---
name: tech-translator
description: Professional technical documentation translation expert, proficient in internet industry terminology. Translates user-provided files, preserves original formatting, and performs professional accuracy and format validation.
---

# Technical Documentation Translation Expert

## Applicable Scenarios

Suitable for Chinese-English translation scenarios of technical documentation, API docs, READMEs, technical blogs, specification documents, etc.

## Usage

Provide translation content (file/folder/pasted text/URL). The skill intelligently executes the translation. Results are written to the workspace root directory; only the path and a validation summary are reported.

Input is automatically detected: File path → translates file; Folder path → traverses and translates; URL → fetches then translates; Plain text → translates directly.

Language direction is automatically determined, or you can manually specify `{source}→{target}` (e.g., `zh→en`).

## Output Rules

All output is written to the workspace root directory:

| Input Type | Output |
|------------|--------|
| Folder | `{original_name}_{lang}/`, preserving internal structure |
| Single file | `{original_name}_{lang}.{ext}` |
| Pasted text / URL | `.translations/translated_output_{lang}/output.{ext}` |

Language codes: `zh` `en` `ja`, etc.

## Format Handling (Automatically Recognized by File Extension)

| Format | Extensions | Handling Method |
|--------|------------|------------------|
| Markdown | `.md` `.mdx` `.markdown` | Translate body text; preserve heading levels, code blocks, links, tables, lists, etc. |
| JSON | `.json` | Translate values only; skip non-text fields like `version`/`keywords`/`config`/`scripts`/`dependencies`/`license`/`main` |
| YAML/TOML | `.yaml` `.yml` `.toml` | Translate string values only; preserve keys and structure. For front matter, translate `name`/`description`/`title` but keep field names. |
| HTML | `.html` `.htm` `.xhtml` | Translate text inside `<body>`/`<p>`/`<h1~6>`/`<li>`/`<td>`/`<th>`/`<a>`/`<title>`/`<meta description>`; preserve tag attributes. Do not touch `<code>`, `<pre>`, `<script>`, `<style>`. |
| XML/SVG | `.xml` `.svg` `.plist` | Translate element text content and comments; preserve tags, attributes, namespaces, CDATA. For SVG, translate `<text>` content but preserve font/Aria attributes. |
| RST | `.rst` | Translate body text; preserve heading adornments (`===` `---`), directives (e.g., `.. code-block::`), roles (e.g., `:ref:`), and reference links. |
| AsciiDoc | `.adoc` `.asciidoc` `.ad` | Translate body text; preserve heading markers, block delimiters (`----`), macros (e.g., `include::`), and attributes (e.g., `{name}`). |
| CSV/TSV | `.csv` `.tsv` | Detect header row, translate data cells; preserve delimiters and quotes. |
| Properties | `.properties` `.env` | Translate values after `=` only; preserve keys and comments (`#` `!`). |
| Strings | `.strings` `.stringsdict` | iOS format: translate string values after `=` only; preserve keys. |
| PO/POT | `.po` `.pot` | gettext format: translate `msgstr` values; preserve `msgid` and all metadata. |
| LaTeX | `.tex` `.ltx` | Translate body paragraphs; preserve commands (e.g., `\section`), environments (e.g., `\begin{}`), math mode, and references (`\ref` `\cite`). |
| Plain Text | Others / no extension | Translate paragraph by paragraph; preserve paragraph structure. |

## Smart Strategies

### Directory-Level Consistency

When translating a folder, a temporary glossary is created to ensure consistent translation of the same term across multiple files:
- Determine the translation of a technical term when first encountered; subsequent files use the same translation.
- Brand names, product names (npm, Docker, Kubernetes, etc.), API names, CLI commands are **not translated**.
- Placeholders `{var}` `{{var}}` `%s` `<param>` are protected and left untouched.

### Intelligent Code Block Handling

- The code/commands themselves are not translated.
- Comments/docstrings within code blocks (following `//` `/** */`) can be translated, and marked as "Comments translated".
- Inline code `` `code` `` is not translated.

### Incremental Translation

- **Folder mode**: The output file's mtime is checked against the source file's; if the output is newer, the file is skipped.
- An overview is provided on first run: `Total N files, M require translation`.

### Quality Validation

Automatically checks and reports the following:
1.  Whether any code blocks, inline code, URLs, or paths were mistranslated.
2.  Whether terminology is consistent throughout the file.
3.  Whether paragraph structure / heading levels correspond correctly.
4.  Whether placeholders and protected content remain intact.
5.  Whether professional terms retained their original form where appropriate.

If issues are found, they are noted in the validation summary without retranslating (to be confirmed by a human).

## Terminology Reference

General principles:
- Industry-standard translations take priority.
- Write the full term with its abbreviation in parentheses on first use; use only the abbreviation afterward.
- Brand/product/library names remain in their original form (Node.js, React, TypeScript).
- For domain-specific terms, decide whether to translate based on context.

## Output Report

```
═══════════════════════
Translation Complete

File: {full path}
Format Validation: ✅ | Terminology Consistency: ✅ | Professional Accuracy: ✅
{Any noted issues}
═══════════════════════
```
