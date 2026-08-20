/**
 * 引用命中定位自测（无浏览器依赖，node 直接跑）。
 *
 * 盯的是两类线上反馈：
 *  1. 「高亮只涂前面几百字」—— 老实现把前缀探针的长度当成了高亮长度，几千字的引用
 *     只圈住探针那一小截。现在终点要靠 extendMatch 量出来，整段圈住。
 *  2. 「高亮的内容跟实际引用的不相关」—— 整体型锚点的 snippet 就是正文开头的截断，
 *     命中必然落在 index 0，涂出来的是开头那几百字。这类退化命中一律不画高亮。
 */
import {
  findCitedRange,
  findCitedHighlight,
  isTrivialCitedHit,
  normalizeCitedFragment,
} from '../src/utils/highlight';

let failed = 0;
function check(name: string, cond: boolean, extra?: unknown) {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failed += 1;
    console.error(`  FAIL ${name}`, extra ?? '');
  }
}

const LEAD = '前言部分'.repeat(60); // 240 字无关正文，保证命中不在开头

// ① 整段命中：引用片段有 900 字，高亮必须把 900 字都圈进去，而不是只圈 200 字探针
{
  const body = '这是被引用的核心论述。'.repeat(90); // 990 字
  const text = `${LEAD}\n\n${body}\n\n${'后续无关内容。'.repeat(50)}`;
  const range = findCitedRange(text, body);
  check('整段引用被完整圈出（不再只圈 200 字探针）',
    !!range && range[1] - range[0] >= body.length * 0.98,
    range && { covered: range[1] - range[0], expect: body.length });
  check('起点落在引用段开头', !!range && range[0] === text.indexOf(body), range);
}

// ② 尾部带省略号 + 正文里有换行/缩进：仍要贴到片段末尾
{
  const body = '第一句话说明背景。\n    第二句给出数据。\n第三句下结论收尾。';
  const text = `${LEAD}\n${body}\n${'尾巴'.repeat(100)}`;
  const snippet = '第一句话说明背景。 第二句给出数据。 第三句下结论收尾。……';
  const range = findCitedRange(text, snippet);
  check('空白差异 + 尾部省略号仍能贴到片段末尾',
    !!range && range[1] === text.indexOf(body) + body.length, range);
}

// ③ 短探针撞上重复文本：应挑贴得最长的那个落点，而不是第一个
{
  const dup = '同样的开头';
  const text = `${LEAD}${dup}到此为止。${'中间'.repeat(80)}${dup}后面才是真正被引用的那一大段论述内容。`;
  const snippet = `${dup}后面才是真正被引用的那一大段论述内容。`;
  const range = findCitedRange(text, snippet);
  check('重复开头时选中真正匹配的那一处',
    !!range && range[0] === text.lastIndexOf(dup), range);
}

// ④ 退化命中之一：snippet 是正文开头的截断（整体型锚点）→ 不高亮
{
  const text = '结果开头这一段。'.repeat(400);
  const snippet = text.slice(0, 500); // 后端 _WHOLE_SNIPPET_MAX 的行为
  check('定位仍然成功（DataView 找命中记录要用）', !!findCitedRange(text, snippet));
  check('开头命中被判为退化', isTrivialCitedHit(text, findCitedRange(text, snippet)!));
  check('开头命中不画高亮', findCitedHighlight(text, snippet) === null);
}

// ⑤ 退化命中之二：snippet 约等于整条记录正文 → 不高亮
{
  const text = '这条记录的全部内容就这么多。'.repeat(20);
  check('几乎盖满全文的命中不画高亮', findCitedHighlight(text, text) === null);
}

// ⑥ 正常的中段命中要保留高亮
{
  const text = `${LEAD}\n${'真正被引用的中段内容。'.repeat(20)}\n${'后面还有很多别的内容。'.repeat(60)}`;
  const snippet = '真正被引用的中段内容。'.repeat(20);
  const hl = findCitedHighlight(text, snippet);
  check('中段命中保留高亮', !!hl && hl[0] > 0);
  check('中段命中长度等于引用片段', !!hl && hl[1] - hl[0] >= snippet.length * 0.98,
    hl && { covered: hl[1] - hl[0], expect: snippet.length });
}

// ⑦ 边界：空片段 / 过短片段 / 找不到
{
  check('空片段不命中', findCitedRange('随便一段正文', '') === null);
  check('过短片段不命中', findCitedRange('随便一段正文', '正文') === null);
  check('找不到时返回 null', findCitedRange(`${LEAD}`, '完全不存在的引用片段内容') === null);
  check('normalize 去掉尾部省略与多余空白',
    normalizeCitedFragment('  甲   乙\n丙 …  ') === '甲 乙 丙');
}

if (failed) {
  console.error(`\n${failed} 项未通过`);
  process.exit(1);
}
console.log('\n引用命中定位自测全部通过');
