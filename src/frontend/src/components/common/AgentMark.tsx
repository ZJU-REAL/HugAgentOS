import { useEffect, useState } from 'react';

/**
 * What the agent is doing, told by the brand glyph itself:
 * - `work`  — a light runs along the outline (a tool or the model is busy)
 * - `think` — the light slows down and the spark breathes (waiting on the model)
 * - `rest`  — the settled grey glyph left on finished rows
 */
export type AgentMarkState = 'work' | 'think' | 'rest';

// assets/design-v2/logo/hugagentos-glyph-blue.svg
const GLYPH = 'M14.5888 7.77765C16.2401 7.14973 18.1314 7.84717 18.8512 9.33629L18.8813 9.40059L23.3166 19.1746L27.4553 17.3081C28.7126 16.7409 30.2298 17.1971 30.8786 18.3257L30.9103 18.3827C31.204 18.9283 31.256 19.5557 31.0576 20.1341L31.0315 20.2062L28.3231 27.2981C27.8528 28.5294 26.381 29.1787 25.0358 28.7482C24.3677 28.5344 23.826 28.0792 23.5357 27.4906L23.5045 27.4247L21.6269 23.288L8.74766 29.098C7.98987 29.4398 7.11299 29.4778 6.32588 29.2067L6.2419 29.1765L6.20584 29.1625L6.10859 29.1229C4.60563 28.4799 3.94394 26.8582 4.60616 25.4749L4.63717 25.4122L12.8735 9.28094C13.208 8.62579 13.7888 8.10382 14.5083 7.80942L14.5888 7.77765ZM16.1618 10.3778L8.30761 26.1502L20.6214 20.4547L16.1618 10.3778ZM27.6579 20.4883L25.0192 21.7538L26.2052 24.5325L27.6579 20.4883Z';
// The outer contour only: the running light travels round the silhouette, not the holes.
const OUTLINE = GLYPH.slice(0, GLYPH.indexOf('Z') + 1);

/** Brand intro: outline 0–600ms → fill 500–700ms → spark from 650ms (design spec §10). */
const INTRO_MS = 1000;

interface AgentMarkProps {
  state: AgentMarkState;
  /** Play the one-time brand intro before settling into `state`. */
  intro?: boolean;
  className?: string;
}

export function AgentMark({ state, intro = false, className }: AgentMarkProps) {
  const [introPlaying, setIntroPlaying] = useState(intro && state !== 'rest');

  useEffect(() => {
    if (!introPlaying) return;
    const id = window.setTimeout(() => setIntroPlaying(false), INTRO_MS);
    return () => window.clearTimeout(id);
  }, [introPlaying]);

  const phase = introPlaying ? 'intro' : state;
  return (
    <svg
      className={`jx-agentMark jx-agentMark--${phase}${className ? ` ${className}` : ''}`}
      viewBox="3.4 6.6 31.4 23.4"
      aria-hidden="true"
      focusable="false"
    >
      <path className="jx-agentMark-base" fillRule="evenodd" d={GLYPH} />
      <path className="jx-agentMark-trace" pathLength={100} d={OUTLINE} />
      <circle className="jx-agentMark-spark" cx="32.0186" cy="14.1187" r="1.921" />
    </svg>
  );
}
