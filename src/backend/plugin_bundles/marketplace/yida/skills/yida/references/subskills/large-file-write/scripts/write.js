#!/usr/bin/env node
/**
 * large-file-write - Node.js 大文件可靠写入工具
 *
 * 解决 AI Agent 模式下 heredoc / shell 命令写入大文件时内容被截断的问题。
 *
 * 用法：
 *   node write.js --file <目标文件路径> [--mode write|append] [--encoding utf8]
 *
 * 内容来源（三选一）：
 *   1. stdin：交互式输入或由调用方传入标准输入
 *   2. --content-file <内容文件>：node write.js --file out.js --content-file .cache/openyida/task/content.txt
 *   3. --payload <JS文件>：node write.js --payload .cache/openyida/task/payload.js（payload 文件自行调用 fs 写入）
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');

// ─── 解析命令行参数 ───────────────────────────────────────────────────────────

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i++) {
    const key = argv[i];
    if (key.startsWith('--')) {
      const name = key.slice(2);
      const value = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
      args[name] = value;
    }
  }
  return args;
}

const args = parseArgs(process.argv);

// ─── 帮助信息 ─────────────────────────────────────────────────────────────────

if (args.help || args.h) {
  console.log(`
large-file-write - 大文件可靠写入工具

用法：
  node write.js --file <目标路径> [选项]

选项：
  --file <path>          目标文件路径（必填，除非使用 --payload）
  --mode <write|append>  写入模式，默认 write（覆盖）
  --encoding <enc>       文件编码，默认 utf8
  --content-file <path>  从指定文件读取内容后写入目标文件
  --payload <path>       执行一个自包含的 JS 脚本（脚本内自行调用 fs 写入）
  --verify               写入后输出行数和末尾5行进行验证
  --help                 显示帮助

示例：
	  # 从 stdin 写入
	  node write.js --file .cache/openyida/task/out.txt

	  # 从内容文件写入
	  node write.js --file .cache/openyida/task/out.js --content-file .cache/openyida/task/content.txt --verify

	  # 执行自包含 payload 脚本
	  node write.js --payload .cache/openyida/task/my-payload.js
	`);
  process.exit(0);
}

// ─── 执行 payload 模式 ────────────────────────────────────────────────────────

if (args.payload) {
  const payloadPath = path.resolve(args.payload);
  if (!fs.existsSync(payloadPath)) {
    console.error(`❌ payload 文件不存在：${payloadPath}`);
    process.exit(1);
  }
  console.log(`▶ 执行 payload：${payloadPath}`);
  require(payloadPath);
  process.exit(0);
}

// ─── 普通写入模式 ─────────────────────────────────────────────────────────────

if (!args.file) {
  console.error('❌ 缺少必填参数 --file <目标文件路径>');
  console.error('   运行 node write.js --help 查看帮助');
  process.exit(1);
}

const targetFile = path.resolve(args.file);
const writeMode = args.mode === 'append' ? 'a' : 'w';
const encoding = args.encoding || 'utf8';
const shouldVerify = args.verify === true || args.verify === 'true';

// 确保目标目录存在
const targetDir = path.dirname(targetFile);
if (!fs.existsSync(targetDir)) {
  fs.mkdirSync(targetDir, { recursive: true });
  console.log(`📁 已创建目录：${targetDir}`);
}

function writeContent(content) {
  const flag = writeMode;
  fs.writeFileSync(targetFile, content, { encoding, flag });

  const totalLines = content.split('\n').length;
  const actualLines = fs.readFileSync(targetFile, 'utf8').split('\n').length;
  const modeLabel = flag === 'a' ? '追加' : '写入';

  console.log(`✅ ${modeLabel}成功：${targetFile}`);
  console.log(`   本次内容：${totalLines} 行 | 文件总行数：${actualLines} 行`);

  if (shouldVerify) {
    const lines = fs.readFileSync(targetFile, 'utf8').split('\n');
    const tail = lines.slice(-5).join('\n');
    console.log('\n📋 文件末尾 5 行：');
    console.log('─'.repeat(50));
    console.log(tail);
    console.log('─'.repeat(50));
  }
}

// ─── 从 --content-file 读取内容 ───────────────────────────────────────────────

if (args['content-file']) {
  const contentFilePath = path.resolve(args['content-file']);
  if (!fs.existsSync(contentFilePath)) {
    console.error(`❌ 内容文件不存在：${contentFilePath}`);
    process.exit(1);
  }
  const content = fs.readFileSync(contentFilePath, encoding);
  writeContent(content);
  process.exit(0);
}

// ─── 从 stdin 读取内容 ────────────────────────────────────────────────────────

const isTTY = process.stdin.isTTY;

if (isTTY) {
  console.log('📝 请输入内容（输入完成后按 Ctrl+D 结束）：');
}

const chunks = [];
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on('line', (line) => {
  chunks.push(line);
});

rl.on('close', () => {
  const content = chunks.join('\n');
  if (!content.trim()) {
    console.error('❌ 未接收到任何内容，请通过 stdin 或 --content-file 提供内容');
    process.exit(1);
  }
  writeContent(content);
});
