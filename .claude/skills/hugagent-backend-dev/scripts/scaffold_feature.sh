#!/usr/bin/env bash
#
# 后端功能模块脚手架：生成 features/<feature>/ 完整骨架
#
# 用法:
#   bash .claude/skills/hugagent-backend-dev/scripts/scaffold_feature.sh <feature> <Feature> <table_name>
#
# 示例:
#   bash .claude/skills/hugagent-backend-dev/scripts/scaffold_feature.sh bookmark Bookmark bookmarks
#
# 生成:
#   src/backend/features/<feature>/__init__.py
#   src/backend/features/<feature>/README.md          职责、公开入口、依赖方向
#   src/backend/features/<feature>/extension.py       NAME + setup()
#   src/backend/features/<feature>/models.py          ORM（UTCDateTime + utc_now + JSONType）
#   src/backend/features/<feature>/repository.py
#   src/backend/features/<feature>/service.py
#   src/backend/features/<feature>/api/__init__.py
#   src/backend/features/<feature>/api/<feature>s.py  路由（同步 def + run_in_session 示例）
#   src/backend/tests/features/<feature>/__init__.py
#   src/backend/tests/features/<feature>/test_<feature>.py
#
# 只建新文件，已存在的文件跳过；不改任何注册表（手动步骤见脚本末尾输出）。
# 不是所有新功能都该进 features/：先按 SKILL.md「放哪里」判断。

set -euo pipefail

FEATURE="${1:?Usage: $0 <feature> <Feature> <table_name>}"
FEATURE_CAP="${2:?Usage: $0 <feature> <Feature> <table_name>}"
TABLE="${3:?Usage: $0 <feature> <Feature> <table_name>}"
FEATURE_UPPER="$(echo "$FEATURE" | tr '[:lower:]' '[:upper:]')"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/../templates"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../../" && pwd)"
BACKEND="$PROJECT_ROOT/src/backend"
PKG="$BACKEND/features/$FEATURE"
TESTS="$BACKEND/tests/features/$FEATURE"

render() {  # render <template> <dest>
    local src="$1" dest="$2"
    if [ -f "$dest" ]; then
        echo "⊘ Skipped ${dest#$PROJECT_ROOT/} (exists)"
        return
    fi
    sed -e "s/\${feature}/$FEATURE/g" \
        -e "s/\${Feature}/$FEATURE_CAP/g" \
        -e "s/\${FEATURE}/$FEATURE_UPPER/g" \
        -e "s/\${table_name}/$TABLE/g" \
        "$src" > "$dest"
    echo "✓ Created ${dest#$PROJECT_ROOT/}"
}

touch_file() {  # touch_file <dest> <content>
    if [ -f "$1" ]; then
        echo "⊘ Skipped ${1#$PROJECT_ROOT/} (exists)"
    else
        printf '%s\n' "$2" > "$1"
        echo "✓ Created ${1#$PROJECT_ROOT/}"
    fi
}

echo "=== HugAgentOS backend feature scaffold ==="
echo "Feature: $FEATURE / $FEATURE_CAP   Table: $TABLE"
echo ""

mkdir -p "$PKG/api" "$TESTS"

touch_file "$PKG/__init__.py" "\"\"\"${FEATURE_CAP} feature module.\"\"\""
touch_file "$PKG/api/__init__.py" ""
touch_file "$TESTS/__init__.py" ""
touch_file "$PKG/README.md" "# ${FEATURE_CAP}

负责：（一句话写清本模块拥有的业务能力与数据）。

公开入口：service、repository、api/${FEATURE}s.py。调用方直接依赖实际模块，不保留旧路径转发。

依赖方向：本模块依赖 core（db / auth / infra / harness registry）；core 不导入本模块，只通过 extension.setup()
注册的钩子接入。模型由安装清单加载，不依赖运行开关。

版本差异：如 CE/EE 行为不同，统一放 edition.py，CE 只覆盖该入口。"

render "$TEMPLATE_DIR/extension.py"  "$PKG/extension.py"
render "$TEMPLATE_DIR/model.py"      "$PKG/models.py"
render "$TEMPLATE_DIR/repository.py" "$PKG/repository.py"
render "$TEMPLATE_DIR/service.py"    "$PKG/service.py"
render "$TEMPLATE_DIR/route.py"      "$PKG/api/${FEATURE}s.py"
render "$TEMPLATE_DIR/test.py"       "$TESTS/test_${FEATURE}.py"

cat <<EOF

=== 手动步骤 ===
1. 删掉模板里用不到的示例（如 route 里的 /sync 异步示例），把 README 第一行写实。
2. 安装清单：src/backend/features/installed.py 的 INSTALLED_FEATURES 追加 "features.$FEATURE"；
   CE 也要提供 → 同步 ce/overlay/src/backend/features/installed.py；EE 专属 → ce/manifest.yaml 排除
   src/backend/features/$FEATURE/** 与 src/backend/tests/features/$FEATURE/**。
3. 路由注册（二选一）：
   - 常驻：api/routes/v1/__init__.py 的 CE_ROUTERS 追加 ("features.$FEATURE.api.${FEATURE}s", "router")
     （EE：edition_ee/routes/registry.py 的 EE_ROUTERS 追加三元组并给能力位）——此时删掉 extension.setup() 里的 register_router；
   - 可关闭：保留 extension.setup() 里的 register_router(router)。
4. 迁移：make migrate-new msg="add $TABLE table"，人工审阅生成结果（SQLite / PostgreSQL 都要能跑）；
   EE 专属表加进 edition_ee/db/edition_tables.py::EE_ONLY_TABLES。
5. 验证：PYTHONPATH=src/backend pytest src/backend/tests/features/$FEATURE -q
   再跑守卫：pytest src/backend/tests/test_no_blocking_async_routes.py src/backend/tests/test_no_event_loop_in_sync_routes.py \\
             src/backend/tests/infra/test_no_session_on_event_loop.py src/backend/tests/test_utc_conventions.py \\
             src/backend/tests/test_route_import_order.py src/backend/tests/test_db_import_order.py \\
             src/backend/tests/features/test_installed_modules.py -q
EOF
