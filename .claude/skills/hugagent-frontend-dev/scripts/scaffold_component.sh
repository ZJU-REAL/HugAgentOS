#!/usr/bin/env bash
#
# 前端组件脚手架生成器
#
# 用法:
#   bash .claude/skills/hugagent-frontend-dev/scripts/scaffold_component.sh <ComponentName> <group>
#
# 示例:
#   bash .claude/skills/hugagent-frontend-dev/scripts/scaffold_component.sh TaskBoard task
#
# 生成文件:
#   - src/frontend/src/components/<group>/<ComponentName>.tsx
#   - src/frontend/src/styles/<componentName>.css (如果 group 没有现成样式文件)
#
# 手动步骤:
#   - 在 components/<group>/index.ts 中添加 export
#   - 在 components/index.ts 中添加 re-export (如需要)

set -euo pipefail

COMPONENT="${1:?Usage: $0 <ComponentName> <group>}"
GROUP="${2:?Usage: $0 <ComponentName> <group>}"

# camelCase
COMPONENT_CAMEL="$(echo "${COMPONENT:0:1}" | tr '[:upper:]' '[:lower:]')${COMPONENT:1}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/../templates"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../../" && pwd)"
FRONTEND="$PROJECT_ROOT/src/frontend/src"

echo "=== HugAgentOS Frontend Scaffold ==="
echo "Component: $COMPONENT"
echo "Group:     $GROUP"
echo ""

# Component directory
COMP_DIR="$FRONTEND/components/$GROUP"
mkdir -p "$COMP_DIR"

# Component file
COMP_FILE="$COMP_DIR/${COMPONENT}.tsx"
if [ ! -f "$COMP_FILE" ]; then
    sed -e "s/\${ComponentName}/$COMPONENT/g" \
        -e "s/\${componentName}/$COMPONENT_CAMEL/g" \
        -e "s/\${group}/$GROUP/g" \
        "$TEMPLATE_DIR/component.tsx" > "$COMP_FILE"
    echo "✓ Created $COMP_FILE"
else
    echo "⊘ Skipped $COMP_FILE (exists)"
fi

# CSS file
CSS_FILE="$FRONTEND/styles/${COMPONENT_CAMEL}.css"
if [ ! -f "$CSS_FILE" ]; then
    sed -e "s/\${componentName}/$COMPONENT_CAMEL/g" \
        "$TEMPLATE_DIR/css-module.css" > "$CSS_FILE"
    echo "✓ Created $CSS_FILE"
else
    echo "⊘ Skipped $CSS_FILE (exists)"
fi

# Check index.ts
INDEX_FILE="$COMP_DIR/index.ts"
if [ -f "$INDEX_FILE" ]; then
    if ! grep -q "$COMPONENT" "$INDEX_FILE"; then
        echo ""
        echo "⚠ 请在 $INDEX_FILE 中添加:"
        echo "  export { $COMPONENT } from './$COMPONENT';"
    fi
else
    echo "export { $COMPONENT } from './$COMPONENT';" > "$INDEX_FILE"
    echo "✓ Created $INDEX_FILE"
fi

echo ""
echo "=== 手动步骤 ==="
echo "1. 在 styles/index.ts 中 import './${COMPONENT_CAMEL}.css'"
echo "2. 按需在父组件中引入 <$COMPONENT />"
echo ""
echo "Done!"
