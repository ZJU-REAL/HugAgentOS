# UI mockup variants and design controls

## Variant carousel

- Show one complete design at a time. Make the named designs meaningfully different in layout and interaction, beyond recoloring one design. Keep side-by-side layouts when the user explicitly asks for simultaneous comparison.
- Use one `.viz-carousel` root with an `aria-label` and one direct child per design, each labeled with a unique, short `data-variant` name. Start with only the first design visible and mark the others `hidden`.
- The runtime supplies previous/next buttons, a count, and a named picker at the bottom center. It switches designs without replacing their DOM, so local interactions and state survive. Do not generate carousel JavaScript, navigation markup, or CSS.
- Keep the carousel outside product-scoped mockup styles. The carousel is an exception to the mockup utility rule; use product-specific styles inside each variant. The carousel reserves bottom space for its controls. Give every variant the same responsive stage height, sized for the tallest design, so navigation stays in place when switching. At narrow widths, increase the shared stage height or reflow the product content so nothing clips.
- Use stable, descriptive names that can be referenced in feedback. Do not submit feedback on navigation or start animation or audio when a variant becomes visible.
- For another language, set `data-previous-label` and `data-next-label` on the root. The count and names update automatically. Navigation stays local; it does not persist a selection or submit it to the model.

```html
<div class="viz-carousel" aria-label="Music player designs">
  <section data-variant="Minimal" aria-label="Minimal music player">
    <!-- Complete interactive player with product-specific styles -->
  </section>
  <section data-variant="Editorial" aria-label="Editorial music player" hidden>
    <!-- A distinct design, with its own controls and state -->
  </section>
  <section data-variant="Studio" aria-label="Studio music player" hidden>
    <!-- A third design -->
  </section>
</div>
```

## Design controls

Modified for HugAgentOS; see ../../NOTICE.

Use native labeled input, select and button elements with local render callbacks.
Keep the first render useful without input. Use the bundled carousel to compare
variants; it keeps each variant's DOM and local state intact. There is no host
control panel, annotation service or Agent-message submission in the preview.
