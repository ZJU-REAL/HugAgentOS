#!/usr/bin/env bash
# 对话建站 React 工程初始化 / 自愈脚本（幂等，可反复执行）。
#
# 用法: bash /opt/site-template/init-react-site.sh <目标目录>
#   新建站点: <目标目录> = /workspace/site-src/<英文短名>
#   编辑站点: <目标目录> = /workspace/myspace/<uid>/<项目文件夹名>
#   新增依赖: 编辑 <目标目录>/package.json 后重跑本脚本
#
# 设计约束: npm 永远不在项目目录内执行——npm install 会把 node_modules 符号链接
# 替换成真实目录，导致依赖树落进 myspace 网盘。依赖统一装在
# /workspace/.site-node/<名>/（package.json 副本 + 真实 node_modules），
# 项目目录里只留一个符号链接；本脚本负责 package.json/lock 双向同步。
set -euo pipefail

# 工作区根：Docker 沙箱内固定 /workspace；无 Docker 本地档由 SCRIPT_RUNNER_WORKSPACE
# 指向真实目录（如 ~/.hugagent/workspace）。模板/脚本路径全部相对它算，跟着环境走。
WS="${SCRIPT_RUNNER_WORKSPACE:-/workspace}"
TEMPLATE_DIR="${SITE_TEMPLATE_DIR:-${SITE_TEMPLATE_HOME:-/opt/site-template}/react-vite}"
NODE_HOME_BASE="${SITE_NODE_BASE:-$WS/.site-node}"
TARGET="${1:?用法: init-react-site.sh <目标目录>}"
# 把容器规范化的 /workspace 前缀别名到真实工作区根（Docker 下 WS=/workspace 即无操作）。
# 让模型可以直接传 SKILL.md 里写死的 /workspace/... 路径。
case "$TARGET" in
  /workspace)   TARGET="$WS" ;;
  /workspace/*) TARGET="$WS${TARGET#/workspace}" ;;
esac
mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd -P)"  # 物理绝对路径（兼容 '.'、相对路径、/myspace 符号链接）
NAME="$(basename "$TARGET")"
# 工程标识 = 工作区下的相对路径打平（避免 site-src/foo 与 myspace/<uid>/foo
# 同名冲突共享依赖树）。必须与 vite.config.mjs 的 projectKey 算法保持一致。
KEY="${TARGET#$WS/}"
[ "$KEY" = "$TARGET" ] && KEY="$NAME"   # 不在工作区下（本地调试）回退 basename
KEY="${KEY//\//_}"
NM_HOME="${NODE_HOME_BASE}/${KEY}"
DIST_DIR="$WS/.site-dist/${KEY}"

if [ ! -d "$TEMPLATE_DIR" ]; then
  echo "错误: 模板目录 $TEMPLATE_DIR 不存在（沙箱镜像过旧，缺 React 建站模板层）" >&2
  exit 1
fi

# Docker 沙箱镜像在构建期预装了模板 node_modules；无 Docker 本地档铺模板时不含它
# （避免拷贝几百 MB），此处首次按需安装一次（幂等，装过即跳过）。
if [ ! -d "$TEMPLATE_DIR/node_modules" ]; then
  echo "[init] 模板依赖未安装，首次安装（可能需要几分钟）..."
  ( cd "$TEMPLATE_DIR" && npm install --prefer-offline --no-audit --no-fund --loglevel=error )
fi

mkdir -p "$NM_HOME"

# 1. 铺源码：仅在目标还不是一个工程时进行，避免覆盖用户已有源码
if [ ! -f "$TARGET/package.json" ]; then
  echo "[init] 铺入模板源码 -> $TARGET"
  tar -C "$TEMPLATE_DIR" --exclude=./node_modules -cf - . | tar -C "$TARGET" -xpf -
else
  echo "[init] 已有 package.json，跳过铺源码（不覆盖现有工程）"
fi

# 2/3. 依赖解析。注意：沙箱是 overlayfs，对镜像层文件 cp -al 会触发逐文件
# copy-up（等于全量复制几百 MB，必超时）——所以：
#   快路径（绝大多数）：package.json 与模板一致 → 直接符号链接到 /opt 预装树，
#     零复制零 npm，秒级完成（vite 只读依赖；缓存已重定向，不会写它）。
#   慢路径（agent 改过 package.json 加依赖）：物化真实副本到 NM_HOME 再
#     npm install 收敛（可能需要几分钟，调用方请给足超时）。
NM_TARGET="$TEMPLATE_DIR/node_modules"
if ! cmp -s "$TARGET/package.json" "$TEMPLATE_DIR/package.json" \
   || [ -d "$NM_HOME/node_modules" ]; then
  if [ ! -d "$NM_HOME/node_modules" ]; then
    echo "[init] 依赖与模板不一致，物化独立副本（可能需要几分钟）..."
    rm -rf "$NM_HOME/node_modules.tmp"   # 清掉上次被中断留下的残留，防止 cp 嵌套复制
    cp -a "$TEMPLATE_DIR/node_modules" "$NM_HOME/node_modules.tmp" \
      && mv "$NM_HOME/node_modules.tmp" "$NM_HOME/node_modules"
  fi
  # package.json 自上次**成功收敛**后未变 → 跳过 npm install 的 no-op 扫描
  # （编辑会话每次开头都会重跑本脚本）。比对基准是安装成功后才写的 stamp，
  # 上次 install 被中断时不会误判已收敛。
  if cmp -s "$TARGET/package.json" "$NM_HOME/.package.json.ok"; then
    echo "[init] 依赖已收敛且未变化，跳过 npm install"
  else
    cp -f "$TARGET/package.json" "$NM_HOME/package.json"
    [ -f "$TARGET/package-lock.json" ] && cp -f "$TARGET/package-lock.json" "$NM_HOME/package-lock.json"
    [ -f "$TARGET/.npmrc" ] && cp -f "$TARGET/.npmrc" "$NM_HOME/.npmrc"
    echo "[init] npm install 收敛依赖（增量安装新依赖）"
    (cd "$NM_HOME" && npm install --prefer-offline --no-audit --no-fund --loglevel=error)
    cp -f "$NM_HOME/package-lock.json" "$TARGET/package-lock.json"
    cp -f "$NM_HOME/package.json" "$NM_HOME/.package.json.ok"
  fi
  NM_TARGET="$NM_HOME/node_modules"
else
  echo "[init] 依赖与模板一致，直接使用预装依赖（零安装）"
  cp -f "$TEMPLATE_DIR/package-lock.json" "$TARGET/package-lock.json" 2>/dev/null || true
fi

# 4. 项目目录里的 node_modules 只能是符号链接；真实目录属于污染，清掉重链
if [ -e "$TARGET/node_modules" ] && [ ! -L "$TARGET/node_modules" ]; then
  echo "[init] 清理项目目录内的真实 node_modules（依赖树应在临时区）"
  rm -rf "$TARGET/node_modules"
fi
ln -sfn "$NM_TARGET" "$TARGET/node_modules"

cat <<EOF
[init] 完成 ✔
  工程目录: $TARGET
  构建命令: cd $TARGET && npm run build
  构建产物: ${DIST_DIR}/
  发布参数: publish_site(title='...', src_dir='${DIST_DIR}', source_dir='$TARGET')
  新增依赖: 编辑 $TARGET/package.json 的 dependencies 后重跑本脚本
EOF
