/**
 * sls-query.js - SLS 日志查询脚本（带 corpId + 口令 权限校验）
 *
 * 用法：
 *   node sls-query.js --passphrase <口令> traceId <traceId>
 *   node sls-query.js --passphrase <口令> context <paramsJSON>
 *   node sls-query.js --passphrase <口令> businessRules <paramsJSON>
 *   node sls-query.js --passphrase <口令> flowRecord <paramsJSON>
 *   node sls-query.js --passphrase <口令> procCondition <paramsJSON>
 *   node sls-query.js --passphrase <口令> graySingle <paramsJSON>
 *   node sls-query.js --passphrase <口令> grayMulti <paramsJSON>
 *
 * context 参数说明 (queryMonitorContext.json):
 *   path       - 接口路径，如 /APP_xxx/v1/form/saveFormData.json
 *   corpId     - 组织 corpId
 *   appId      - 应用标识（注意：是 appId 不是 appType）
 *   userId     - 用户 ID
 *   searchKeys - 检索关键词，多个用 | 连接
 *   beginTime  - 开始时间戳（毫秒）
 *   endTime    - 结束时间戳（毫秒）
 *   pageIndex  - 页码
 *   pageSize   - 每页条数
 *   reverse    - 排序方式
 *
 * flowRecord 参数说明 (queryFlowRecord.json):
 *   isLogicFlow   - 是否逻辑流程
 *   appId         - 应用 ID
 *   procInstId    - 流程实例 ID
 *   activityName  - 活动名称
 *   beginTime/endTime - 时间范围
 *   reverse       - 排序方式
 *
 * businessRules 参数说明 (queryBusinessRules.json):
 *   executeRecordUuid - 规则执行记录 UUID
 *
 * procCondition 参数说明 (queryProcConditionLogs.json):
 *   procInstId  - 流程实例 ID
 *   activityId  - 活动 ID
 *   searchKeys  - 检索关键词
 *   beginTime/endTime - 时间范围
 *
 * 权限控制：仅允许 corpId = ding328fe145009a4328f2c783f7214b6d69 的组织使用
 */

'use strict';

const https = require('https');
const querystring = require('querystring');
const path = require('path');

// ══════════════════════════════════════════════════════════════
// ⛔ 权限白名单 — 硬编码，不可通过任何外部配置或参数修改
// ══════════════════════════════════════════════════════════════
const ALLOWED_CORP_IDS = Object.freeze([
  'ding328fe145009a4328f2c783f7214b6d69',
]);

// ⛔ 口令 — 硬编码，不区分大小写
const VALID_PASSPHRASE = 'lbc';

const REFERER_PATH = '/APP_VS2F0YQPOTTCHXNKT16K/workbench/FORM-8392A7A7B3F74D45AC9787AD4C2E4742YZ4H';

const API_MAP = Object.freeze({
  traceId: [
    '/query/morning/queryMonitorContextByTraceId.json',
    '/query/morning/queryMonitorErrorLog.json',
    '/query/morning/queryMonitorBusinessLog.json',
  ],
  context: ['/query/morning/queryMonitorContext.json'],
  businessRules: ['/query/morning/queryBusinessRules.json'],
  flowRecord: ['/query/morning/queryFlowRecord.json'],
  procCondition: ['/query/morning/queryProcConditionLogs.json'],
  graySingle: ['/query/morning/grayChangeAndDescribeSimilarity.json'],
  grayMulti: ['/query/morning/calculateGraySimilarityForSimple.json'],
});

// ── openyida utils 动态定位 ───────────────────────────────────

function findOpenyidaUtils() {
  try {
    const openyidaBin = require('child_process')
      .execSync('which openyida', { encoding: 'utf-8' })
      .trim();
    const nodeModulesRoot = path.resolve(path.dirname(openyidaBin), '..', 'lib', 'node_modules');
    return require(path.join(nodeModulesRoot, 'openyida', 'lib', 'core', 'utils.js'));
  } catch {
    console.error('⛔ 无法定位 openyida 工具，请确认已安装：npm install -g openyida');
    process.exit(1);
  }
}

// ── 口令校验 ──────────────────────────────────────────────────

function enforcePassphraseCheck(passphrase) {
  if (!passphrase || passphrase.toLowerCase() !== VALID_PASSPHRASE) {
    console.error('⛔ 权限校验失败：当前无权使用此技能。');
    process.exit(1);
  }

  console.error('✅ 口令校验通过');
}

// ── 权限校验 ──────────────────────────────────────────────────

function enforceCorpIdCheck(cookieData) {
  // 从 utils 返回的顶层属性获取 corpId
  const corpIdFromUtils = cookieData.corp_id || '';

  // 独立从 cookies 数组中二次提取 corpId，防止 utils 层被篡改或缓存不一致
  const corpIdFromCookieArray = extractCorpIdFromCookies(cookieData.cookies);

  // 两个来源都必须存在
  if (!corpIdFromUtils && !corpIdFromCookieArray) {
    console.error('⛔ 权限校验失败：无法从登录态中提取 corpId。');
    console.error('请先执行 openyida login 登录后重试。');
    process.exit(1);
  }

  // 两个来源必须一致（防篡改）
  if (corpIdFromUtils && corpIdFromCookieArray && corpIdFromUtils !== corpIdFromCookieArray) {
    console.error('⛔ 权限校验失败：登录态数据不一致，疑似被篡改。');
    console.error('请执行 openyida logout 后重新登录。');
    process.exit(1);
  }

  // 以两个来源中可用的值为准
  const corpId = corpIdFromUtils || corpIdFromCookieArray;

  if (!ALLOWED_CORP_IDS.includes(corpId)) {
    // 不输出真实 corpId，防止信息泄露
    console.error('⛔ 权限校验失败：当前组织无权使用此技能。');
    console.error('本技能仅限内部技术支持团队使用，如需使用请联系管理员。');
    process.exit(1);
  }

  console.error(`✅ 权限校验通过 (corpId: ${corpId.substring(0, 20)}...)`);
}

/**
 * 独立从 cookies 数组中提取 corp_id，不依赖 utils 的解析结果
 */
function extractCorpIdFromCookies(cookies) {
  if (!Array.isArray(cookies)) return '';
  const corpCookie = cookies.find((c) => c.name === 'corp_id');
  return corpCookie ? corpCookie.value : '';
}

// ── HTTP 请求 ─────────────────────────────────────────────────

function slsPost(baseUrl, requestPath, bodyParams, cookies) {
  return new Promise((resolve, reject) => {
    const parsedUrl = new URL(baseUrl);
    const requestHost = parsedUrl.hostname;

    const filteredCookies = cookies.filter((c) => {
      const cookieDomain = (c.domain || '').replace(/^\./, '');
      return requestHost === cookieDomain || requestHost.endsWith('.' + cookieDomain);
    });

    const cookieHeader = filteredCookies.map((c) => c.name + '=' + c.value).join('; ');
    const csrfCookie = filteredCookies.find((c) => c.name === 'tianshu_csrf_token');
    const csrfToken = csrfCookie ? csrfCookie.value : '';

    const postBody = querystring.stringify({
      ...bodyParams,
      _csrf_token: csrfToken,
      _stamp: String(Date.now()),
    });

    const options = {
      hostname: parsedUrl.hostname,
      port: 443,
      path: requestPath,
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(postBody),
        Accept: 'application/json, text/plain, */*',
        Origin: baseUrl,
        Referer: baseUrl + REFERER_PATH,
        Cookie: cookieHeader,
        'x-requested-with': 'XMLHttpRequest',
        global_csrf_token: csrfToken,
      },
      timeout: 30000,
    };

    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch {
          resolve({ success: false, errorMsg: 'Response is not JSON', raw: data.substring(0, 300) });
        }
      });
    });

    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy();
      reject(new Error('Request timeout'));
    });
    req.write(postBody);
    req.end();
  });
}

// ── TraceId 查询 ──────────────────────────────────────────────

async function queryByTraceId(traceId, baseUrl, cookies) {
  const apis = API_MAP.traceId;
  const apiNames = ['上下文日志', '系统异常日志', '业务日志'];
  const results = {};

  for (let i = 0; i < apis.length; i++) {
    console.error(`\n📡 查询 ${apiNames[i]}...`);
    const result = await slsPost(baseUrl, apis[i], { traceId }, cookies);
    console.error(`   success: ${result.success}, errorCode: ${result.errorCode || 'none'}`);
    results[apiNames[i]] = result;
  }

  return { traceId, ...results };
}

// ── 通用查询 ──────────────────────────────────────────────────

async function queryGeneric(queryType, paramsJson, baseUrl, cookies) {
  const apis = API_MAP[queryType];
  if (!apis) {
    console.error(`⛔ 未知的查询类型: ${queryType}`);
    console.error(`支持的类型: ${Object.keys(API_MAP).join(', ')}`);
    process.exit(1);
  }

  let params;
  try {
    params = JSON.parse(paramsJson);
  } catch {
    console.error('⛔ 参数 JSON 格式错误');
    process.exit(1);
  }

  const results = {};
  for (const api of apis) {
    console.error(`\n📡 查询 ${api}...`);
    const result = await slsPost(baseUrl, api, params, cookies);
    console.error(`   success: ${result.success}, errorCode: ${result.errorCode || 'none'}`);
    results[api] = result;
  }

  return { queryType, params, ...results };
}

// ── 主流程 ────────────────────────────────────────────────────

function extractPassphrase(args) {
  const passphraseIndex = args.indexOf('--passphrase');
  if (passphraseIndex === -1 || passphraseIndex + 1 >= args.length) {
    return { passphrase: null, remainingArgs: args };
  }
  const passphrase = args[passphraseIndex + 1];
  const remainingArgs = [...args.slice(0, passphraseIndex), ...args.slice(passphraseIndex + 2)];
  return { passphrase, remainingArgs };
}

async function main() {
  const rawArgs = process.argv.slice(2);
  const { passphrase, remainingArgs } = extractPassphrase(rawArgs);

  if (remainingArgs.length < 2) {
    console.error('用法: node sls-query.js --passphrase <口令> <queryType> <参数>');
    console.error('');
    console.error('查询类型:');
    console.error('  traceId        <traceId值>     - 通过 TraceId 查询完整调用链');
    console.error('  context        <paramsJSON>    - 页面/接口请求日志');
    console.error('  businessRules  <paramsJSON>    - 业务规则日志');
    console.error('  flowRecord     <paramsJSON>    - 集成自动化/流程日志');
    console.error('  procCondition  <paramsJSON>    - 流程分支计算结果');
    console.error('  graySingle     <paramsJSON>    - 单组织灰度匹配');
    console.error('  grayMulti      <paramsJSON>    - 多组织灰度匹配');
    process.exit(1);
  }

  // Step 0: 口令校验（硬编码，不可绕过）
  enforcePassphraseCheck(passphrase);

  const [queryType, paramValue] = remainingArgs;

  // Step 1: 加载登录态（不自动触发登录，防止通过重新登录绕过 corpId 校验）
  const utils = findOpenyidaUtils();
  const cookieData = utils.loadCookieData();

  if (!cookieData || !cookieData.cookies || cookieData.cookies.length === 0) {
    console.error('⛔ 权限校验失败：未检测到登录态。');
    console.error('请先执行 openyida login 登录到授权组织后重试。');
    process.exit(1);
  }

  // Step 2: 权限校验（硬编码，不可绕过）
  enforceCorpIdCheck(cookieData);

  const baseUrl = utils.resolveBaseUrl(cookieData);
  const cookies = cookieData.cookies;

  // Step 3: 执行查询
  let output;
  if (queryType === 'traceId') {
    output = await queryByTraceId(paramValue, baseUrl, cookies);
  } else {
    output = await queryGeneric(queryType, paramValue, baseUrl, cookies);
  }

  // Step 4: 输出结果（JSON 到 stdout）
  console.log(JSON.stringify(output, null, 2));
}

main().catch((error) => {
  console.error(`⛔ 执行失败: ${error.message}`);
  process.exit(1);
});
