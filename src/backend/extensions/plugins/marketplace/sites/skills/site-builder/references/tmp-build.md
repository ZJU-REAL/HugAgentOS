# 云端 Linux 沙箱：在 /tmp 构建，从工作区入口发布

适用于云端个人或团队项目。桌面本机 / 混合模式的路径校验不同，仍须遵循
SKILL.md 中的本机授权目录约定；不要用链接绕过本机真实路径校验。

## 目录职责

| 目录 | 内容 |
|---|---|
| list_sites 返回的 source_dir，或新站当前项目源码目录 | 持久源码，保存所有正式修改 |
| /tmp/site-build.<随机批次>/source | 真实源码副本，执行 init 与 npm run build |
| 同批次的 node、cache、npm-cache、dist | 临时依赖、构建缓存、npm 缓存、产物 |
| /workspace/.site-dist/<本批次> | 指向临时 dist 的发布根链接；不放进团队项目 |

把依赖放在临时区、但仍在挂载项目目录执行构建，只解决了一部分慢 I/O。
必须复制真实源码；不能将临时源码或 src 链接回项目。预装 node_modules 可以只读复用。
不假定所有 /workspace 子目录都是同一种挂载；本流程把高频构建 I/O 统一移至 /tmp。
不要把整个项目根目录替换成软链接，不在团队目录删除、重命名或搬运历史 node_modules。

## 保存源码，再创建构建副本

1. 编辑老站前先 list_sites，确认原 site_id 与 source_dir。读取该项目规则。
   新站先在当前项目的站点子目录保存模板源码和正式修改；没有项目时保存在会话源码目录，
   发布时传该 source_dir，平台会保存到项目。此处只铺模板源码，不在挂载目录执行 init。
2. 源码、静态资源和依赖声明都先写回持久目录。只复制站点工程，不复制整个团队文件夹、
   凭据、业务导出备份或无关截图。复制前后及发布前核对源码文件清单与内容哈希；
   发现并发修改，重新取副本并构建，不用旧产物覆盖新源码。
3. 在同一次 Bash 调用中运行以下准备和构建命令。将 SOURCE 替换为已确认的真实源码路径。
   后续调用不假定变量仍存在，使用打印出的绝对路径。已有本批次目录且来源一致时可以复用，
   不要每个命令都重新初始化、重复安装。

```bash
set -euo pipefail
umask 077
SOURCE='<已确认的持久源码绝对路径>'
test -f "$SOURCE/package.json"
TEMPLATE="${SITE_TEMPLATE_HOME:-/opt/site-template}"
# 云端模板依赖应在镜像构建时就位；缺失即停止，不能运行时改共享模板。
test -d "$TEMPLATE/react-vite/node_modules"
RUN=$(mktemp -d /tmp/site-build.XXXXXXXX)
mkdir -p "$RUN/source"
# 不解引用链接；若站点源码本身还有其他链接，先核实真实资源并物化，不能链接回挂载目录。
tar -C "$SOURCE" --exclude='./node_modules' --exclude='./.git' \
  --exclude='./dist' --exclude='./.vite' --exclude='./.site-node' \
  --exclude='./.site-dist' --exclude='./.vite-cache' -cf - . \
  | tar -C "$RUN/source" -xf -
export SITE_NODE_BASE="$RUN/node"
export SITE_CACHE="$RUN/cache"
export SITE_DIST="$RUN/dist"
export npm_config_cache="$RUN/npm-cache"
bash "$TEMPLATE/init-react-site.sh" "$RUN/source"
cd "$RUN/source"
npm run build
test -s "$RUN/dist/index.html"
printf 'SOURCE=%s\nRUN=%s\nDIST=%s\n' "$SOURCE" "$RUN" "$RUN/dist"
```

- init 在 /tmp 的工程里执行；它只读复用 /opt 模板依赖，或在 SITE_NODE_BASE 内安装独立依赖。
  不修改共享模板的依赖。模板未预装依赖时停止并报告镜像资产缺失，不能回退到团队目录安装。
- 同一会话内再次构建同一站点，保留并复用本批次的 node / npm-cache；先更新真实源码副本，
  再核对依赖。不要为每次小改动重新 mktemp 后全量安装；有并发或来源不一致则使用独立批次。
- 旧工程的 vite.config 若没有读取 SITE_DIST / SITE_CACHE，先把配置改成读取这两个环境变量，
  保留 base: './' 与 HashRouter，并保存到持久源码。不能只在临时副本中修改配置。
- 新增依赖须写入持久 package.json。init 若改变 package.json / package-lock.json，先核对差异，
  只保存确认过的清单文件到持久目录，再核对两边源码完全一致；若有其他源码变化则重新构建。
  不把 /tmp 整目录反向覆盖到团队项目，不使用 --no-save 留下不可复现的依赖。
- 缺 vite 或链接失效时在当前临时副本重跑 init；安装失败先处理原错误。
  构建日志需要 tail 时使用 pipefail，不能把构建失败当作退出码 0。

## 建立发布入口

云端 publish_site 当前要求 src_dir 在工作区内，不能直接传 /tmp。
可在工作区临时区建一个指向本批次真实 dist 的根链接；只链接根，dist 内部均为普通文件。
源码目录仍传持久项目路径，尤其团队项目要求与项目保存的源码一致。

```bash
# 新 Bash 调用时，把 RUN 换成上一步打印的实际绝对路径。
test -s "$RUN/dist/index.html"
PUBLISH="/workspace/.site-dist/$(basename "$RUN")"
mkdir -p /workspace/.site-dist
test ! -e "$PUBLISH" && test ! -L "$PUBLISH"
ln -s "$RUN/dist" "$PUBLISH"
test -s "$PUBLISH/index.html"
printf 'src_dir=%s\nsource_dir=%s\n' "$PUBLISH" "$SOURCE"
```

若执行环境不支持该根链接，或发布工具拒绝链接，不放宽权限、不更改发布校验。
改用同一工作区临时位置的真实目录，只复制 dist 中少量文件后发布。不要复制 node_modules。
目录已存在时先核实本批次归属，不用 ln -sfn、rm -rf 覆盖未知路径。

调用 publish_site(title=..., site_id='<老站原编号>', src_dir='<打印的发布入口>',
source_dir='<持久源码目录>')；新站省略 site_id。不要把 /tmp/source 当成团队 source_dir。
发布前确认这份产物来自最新持久源码；发布后核对 site_id、URL、版本和实际首页。

## 生命周期

同一站点串行构建，不在同一临时副本上并行安装和构建。
只在发布与线上核验成功后移除本批次链接及临时目录；失败时保留以便诊断，禁止重发旧 dist。
本地临时路径不是源码关联或可长期复用的 publish_dir；后续编辑重新查询站点并从持久源码准备。
