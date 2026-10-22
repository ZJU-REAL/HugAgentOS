
# 应用导航分组

## 严格要求

- 操作前必须已知 `appType`；不要编造。
- 移动页面前先执行 `openyida nav-group list <appType>` 确认 `navUuid` / `formUuid` 和目标分组。
- 完整应用首次生成后，必须基于业务信息架构决定根导航顺序，不能保留创建时的默认顺序（默认常是表单在前、看板在后）。
- **默认原则：面向决策者的总览/驾驶舱看板作为应用门面靠前，数据录入/明细表单在其后。** 判断某个看板是否靠前，看它是不是主要「查看/决策」入口：
  - 有独立总览首页看板时：`总览首页看板 → 专题看板 → 核心业务表单 → 明细/配置表单`。
  - 没有独立首页、专题看板本身就是主要决策视图时（如「双11看板/618看板」面向运营主管只读查看）：这些**专题看板靠前**，数据录入/明细表单在后。
- "不要无脑把看板全置顶"针对的是：当存在明确操作旅程（先录入后查看）、且看板只是次要报表时，按旅程排；不要把与主流程无关的次要看板也塞最前。多数带看板的应用，决策看板应先于录入表单。
- 删除分组默认只删除空分组；非空分组必须先移动子项，除非用户明确要求 `--force`。
- 分组节点是 `navType: "NAV"`，普通页面是 `navType: "PAGE"`，外链是 `navType: "LINK"`，系统节点不要移动或删除。

## 命令

### 查询导航树

```bash
openyida nav-group list <appType>
openyida nav-group list <appType> --flat
```

输出为 JSON。树形结果中 `type=group` 的节点即分组；`navUuid` 是后续重命名、删除、移动的稳定标识。

### 创建分组

```bash
openyida nav-group create <appType> "分组名"
openyida nav-group create <appType> "子分组名" --parent <groupNavUuid>
```

分组只能创建在根目录或一级分组下。

### 重命名分组

```bash
openyida nav-group rename <appType> <groupNavUuid|groupName> "新分组名"
```

同名分组可能歧义，优先使用 `navUuid`。

### 移动页面或分组

```bash
openyida nav-group move <appType> <formUuid|navUuid|name> --to <groupNavUuid|groupName|root>
openyida nav-group move <appType> <formUuid> --to <groupNavUuid> --before <siblingNavUuid>
openyida nav-group move <appType> <formUuid> --to root --after <siblingNavUuid>
```

常见场景：把新建表单放入已有分组：

```bash
openyida nav-group list APP_XXX --flat
openyida nav-group move APP_XXX FORM_XXX --to NAV_XXX
```

### 按业务顺序整理根导航

```bash
openyida nav-group order <appType> <formUuid|navUuid|name> [more items...]
```

`order` 会把列出的导航项按给定顺序移动到根导航靠前位置，未列出的系统导航、表单、页面、分组保持相对顺序并跟在后面。适合完整应用创建完成后一次性整理入口。

示例 A：电商销售系统只有「销售数据表单 + 双11看板 + 618看板」，看板是运营主管的主要决策视图，把看板作为门面靠前、录入表单在后：

```bash
openyida nav-group order APP_XXX 双11看板 618看板 销售数据
```

示例 B：应用另有独立「首页总览看板」，且双11/618 只是次要专题、主流程是先在订单/商品表录入再看板，则按旅程排、不要把每个专题看板都塞最前：

```bash
openyida nav-group order APP_XXX 首页总览看板 订单表 商品表 客户表 双11看板 618看板
```

### 删除分组

```bash
openyida nav-group delete <appType> <groupNavUuid>
```

非空分组会报错，先把子页面移动到其他分组或 `root`。

### 隐藏 / 显示导航项

```bash
openyida nav-group hide <appType> <navUuid|formUuid|name>
openyida nav-group show <appType> <navUuid|formUuid|name>
```

隐藏会同时设置 PC 与移动端导航隐藏。

## 接口事实

- 查询：`/dingtalk/web/{appType}/query/formnav/getFormNavigationListByOrder.json`
- 创建：`/dingtalk/web/{appType}/query/formnav/saveFormNavigation.json`
- 重命名：`/dingtalk/web/{appType}/query/formnav/updateNavigationTitle.json`
- 移动排序：`/dingtalk/web/{appType}/query/formnav/updateFormNavigationOrderNew.json`
- 删除：`/dingtalk/web/{appType}/query/formnav/deleteFormNavigation.json`

`ROOT` 分组的后端标识是 `NAV-SYSTEM-PARENT-UUID`，命令中可用 `root` 代替。
