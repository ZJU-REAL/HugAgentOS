import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const scriptsDir = dirname(fileURLToPath(import.meta.url));
const stylesDir = join(scriptsDir, '..', 'src', 'styles');
const chatCss = readFileSync(join(stylesDir, 'chat.css'), 'utf8');
const mobileCss = readFileSync(join(stylesDir, 'mobile.css'), 'utf8');

function ruleBody(css, selectorPattern, message) {
  const match = css.match(new RegExp(`${selectorPattern}\\s*\\{([^}]*)\\}`, 's'));
  assert.ok(match, message);
  return match[1];
}

const desktopHomeComposer = ruleBody(
  chatCss,
  '\\.jx-homeInput \\.jx-composer',
  'The desktop home composer rule must exist.',
);
assert.match(
  desktopHomeComposer,
  /(?:^|\n)\s*height:\s*auto\s*!important;/,
  'The desktop home composer must keep its initial height while allowing content-driven growth.',
);
assert.match(
  desktopHomeComposer,
  /(?:^|\n)\s*min-height:\s*148px\s*!important;/,
  'The desktop home composer must preserve its 148px resting height.',
);

const mobileHomeWrapper = ruleBody(
  mobileCss,
  '\\.jx-emptyPage--main \\.jx-homeInput \\.jx-composerWrap',
  'The mobile home composer wrapper rule must exist.',
);
assert.match(
  mobileHomeWrapper,
  /(?:^|\n)\s*height:\s*auto;/,
  'The mobile home composer wrapper must grow beyond its initial height.',
);

const mobileHomeEditor = ruleBody(
  mobileCss,
  '\\.jx-emptyPage--main \\.jx-homeInput \\.jx-composer,\\s*'
    + '\\.jx-emptyPage--main \\.jx-homeInput \\.jx-composerEditor,\\s*'
    + '\\.jx-emptyPage--main \\.jx-homeInput \\.jx-composerPlaceholder',
  'The mobile home composer editor rule must exist.',
);
assert.match(
  mobileHomeEditor,
  /(?:^|\n)\s*height:\s*auto\s*!important;/,
  'The mobile home editor must use an automatic height.',
);

const compactMobileHome = ruleBody(
  mobileCss,
  '\\.jx-emptyPage--main \\.jx-homeInput \\.jx-composerWrap,\\s*'
    + '\\.jx-emptyPage--main \\.jx-homeInput \\.jx-composer,\\s*'
    + '\\.jx-emptyPage--main \\.jx-homeInput \\.jx-composerEditor,\\s*'
    + '\\.jx-emptyPage--main \\.jx-homeInput \\.jx-composerPlaceholder',
  'The compact mobile home composer rule must exist.',
);
assert.doesNotMatch(
  compactMobileHome,
  /(?:^|\n)\s*height:\s*82px\s*!important;/,
  'Mobile compact breakpoints must not restore a fixed composer height.',
);

console.log('composer autogrow CSS checks passed');
