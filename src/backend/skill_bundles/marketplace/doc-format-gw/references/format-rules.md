references/format-rules.md

# Format Rules

## Core Rules

- Title: `方正小标宋简体`, second-size (`22pt`), centered, not bold.
- Body: `仿宋_GB2312`, third-size (`16pt`), justified, first-line indent `2` Chinese characters, fixed line spacing `28pt`, spacing before/after `0`.
- Heading 1: `黑体`, third-size (`16pt`), pattern like `一、`.
- Heading 2: `楷体_GB2312`, third-size (`16pt`), pattern like `（一）`.
- Heading 3: `仿宋_GB2312`, third-size (`16pt`), bold, pattern like `1.` or `1、`.
- Heading 4: `仿宋_GB2312`, third-size (`16pt`), not bold, pattern like `（1）`.
- Arabic digits inside text: keep `Times New Roman`.
- Margins: top `37mm`, bottom `35mm`, left `28mm`, right `26mm`.
- Footer/page number target: centered `-1-` style in `宋体`, fourth-size.
- The line spacing between paragraphs is fixed at 28 points, with 0 lines before and after each paragraph.

## Table Rules

- Table title: centered, `方正小标宋简体`, small second-size.
- Header row: centered, `黑体`, fourth-size (`14pt`).
- Body cells: `仿宋_GB2312`, fourth-size (`14pt`).
- Keep borders complete and visible.

## Heuristics

- The first short non-empty lines before the main body are treated as title lines.
- `附件` or `附件1` starts attachment-label formatting.
- `（2025年4月19日）`-style lines are treated as centered date lines.
- `一、` / `（一）` / `1.` / `（1）` patterns are treated as heading levels 1-4.
- Other paragraphs default to body text.

## Failure Rule

- If any required font is missing, stop and return the missing-font list.
