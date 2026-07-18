
# 平台权限管理

## 严格要求 (MUST DO)

- 修改前先查询当前状态：人员先 `search-user`，角色先 `list`，通讯录开关先 `address-book get`。
- 增删改管理员会影响真实组织权限，执行前向用户确认目标人员、角色、部门范围和管理场景。
- 同名人员必须用 `search-user` 的 `departmentNamePath` 区分，不得只凭姓名操作。
- 平台子管理员必须指定 `--dept-ids`，场景默认 `appManage,bulletinBoard`。

## 常用命令

搜索人员，确认 userId：

```bash
openyida corp-manager search-user "余浩" --dept "宜搭,钉钉官方同学"
```

查询管理员：

```bash
openyida corp-manager list app --user <userId>
openyida corp-manager list platform --user <userId>
openyida corp-manager list sub --user <userId>
```

添加或更新管理员：

```bash
openyida corp-manager add app --user <userId>
openyida corp-manager add platform --user <userId>
openyida corp-manager add sub --user <userId> --dept-ids 848712658 --scenes appManage,bulletinBoard
```

移除管理员：

```bash
openyida corp-manager remove app --user <userId>
openyida corp-manager remove platform --user <userId>
openyida corp-manager remove sub --user <userId>
```

通讯录权限：

```bash
openyida corp-manager address-book get
openyida corp-manager address-book set --all-visible n --admin-visible y
```

## 角色映射

| CLI 角色 | 页面含义 | 接口 roleType |
|---------|----------|---------------|
| `app` | 应用管理员 | `applicationCreateRole` |
| `platform` | 平台管理员 | `corpAdminRole` |
| `sub` | 平台子管理员 | `subCorpAdminRole` |

## 子管理员参数

`--dept-ids` 传部门 ID，多个用逗号分隔。可先通过人员搜索结果里的 `departmentIds` 获取常用部门 ID。`--scenes` 支持：

| scene | 含义 |
|-------|------|
| `appManage` | 应用管理 |
| `bulletinBoard` | 公告栏定制 |

## 安全检查清单

1. `search-user` 确认目标人员 userId 和部门路径。
2. `list <role> --user <userId>` 确认当前角色状态。
3. 展示将要执行的 add/remove/address-book set 命令。
4. 用户确认后执行。
5. 再次 `list` 或 `address-book get` 验证结果。
