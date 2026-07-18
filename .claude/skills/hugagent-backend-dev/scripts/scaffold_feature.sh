#!/usr/bin/env bash
#
# 后端新功能脚手架生成器
#
# 用法:
#   bash .claude/skills/hugagent-backend-dev/scripts/scaffold_feature.sh <feature_name> <FeatureName> <table_name>
#
# 示例:
#   bash .claude/skills/hugagent-backend-dev/scripts/scaffold_feature.sh bookmark Bookmark bookmarks
#
# 生成文件:
#   - src/backend/api/routes/v1/<feature>s.py       (路由)
#   - src/backend/core/services/<feature>_service.py (服务)
#   - src/backend/tests/test_<feature>.py            (测试)
#
# 注意: model 和 repository 需手动合并到对应领域文件中
#   - core/db/models/<领域>.py      ← 添加 ORM 类（并在包 __init__.py re-export）
#   - core/db/repository/<领域>.py  ← 添加 Repository 类
#   - api/routes/v1/__init__.py     ← 注册进 CE_ROUTERS / EE_ROUTERS 注册表
#   - 运行: alembic revision --autogenerate -m "add ${table_name} table"

set -euo pipefail

FEATURE="${1:?Usage: $0 <feature_name> <FeatureName> <table_name>}"
FEATURE_CAP="${2:?Usage: $0 <feature_name> <FeatureName> <table_name>}"
TABLE="${3:?Usage: $0 <feature_name> <FeatureName> <table_name>}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/../templates"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../../" && pwd)"
BACKEND="$PROJECT_ROOT/src/backend"

echo "=== HugAgentOS Backend Scaffold ==="
echo "Feature: $FEATURE / $FEATURE_CAP"
echo "Table:   $TABLE"
echo ""

# Route file
ROUTE_FILE="$BACKEND/api/routes/v1/${FEATURE}s.py"
if [ ! -f "$ROUTE_FILE" ]; then
    sed -e "s/\${feature}/$FEATURE/g" \
        -e "s/\${Feature}/$FEATURE_CAP/g" \
        -e "s/\${FEATURE_NAME}/$FEATURE_CAP/g" \
        "$TEMPLATE_DIR/route.py" > "$ROUTE_FILE"
    echo "✓ Created $ROUTE_FILE"
else
    echo "⊘ Skipped $ROUTE_FILE (exists)"
fi

# Service file
SERVICE_FILE="$BACKEND/core/services/${FEATURE}_service.py"
if [ ! -f "$SERVICE_FILE" ]; then
    sed -e "s/\${feature}/$FEATURE/g" \
        -e "s/\${Feature}/$FEATURE_CAP/g" \
        "$TEMPLATE_DIR/service.py" > "$SERVICE_FILE"
    echo "✓ Created $SERVICE_FILE"
else
    echo "⊘ Skipped $SERVICE_FILE (exists)"
fi

# Test file
TEST_FILE="$BACKEND/tests/test_${FEATURE}.py"
if [ ! -f "$TEST_FILE" ]; then
    sed -e "s/\${feature}/$FEATURE/g" \
        -e "s/\${Feature}/$FEATURE_CAP/g" \
        "$TEMPLATE_DIR/test.py" > "$TEST_FILE"
    echo "✓ Created $TEST_FILE"
else
    echo "⊘ Skipped $TEST_FILE (exists)"
fi

echo ""
echo "=== 手动步骤 ==="
echo "1. 将 model 模板合并到 core/db/models/<领域>.py（替换 \${Feature}=$FEATURE_CAP, \${table_name}=$TABLE），并在包 __init__.py re-export"
echo "2. 将 repository 模板合并到 core/db/repository/<领域>.py（替换 \${Feature}=$FEATURE_CAP）"
echo "3. 在 api/routes/v1/__init__.py 的注册表中添加:"
echo "   CE_ROUTERS += ((\"${FEATURE}s\", \"router\"),)  # 或 EE_ROUTERS（带 license 能力位）"
echo "4. 创建迁移:"
echo "   alembic revision --autogenerate -m \"add $TABLE table\""
echo "5. 应用迁移:"
echo "   alembic upgrade head"
echo ""
echo "Done!"
