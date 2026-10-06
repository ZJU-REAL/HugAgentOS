# 工具参数

所有 resource_id 都来自 browser_open 返回的 resource.resource_id。不要把 install_id、chat_id 或 checkpoint_id 当作资源编号。

## browser_open

| 参数 | 含义 |
| --- | --- |
| resource_id | 可选。继续当前对话已存在的浏览器。 |
| checkpoint_id | 可选。恢复用户明确保存的登录到新浏览器。 |

返回 resource，包括 resource_id、execution_scope、chat_id、install_id、revision 和 status。
恢复失败时说明会话已过期或插件已更新，不静默创建另一个登录会话。

## browser_observe

必填 resource_id。action 默认为 snapshot。selector 默认为 body，只用于 text。

| action | 返回 |
| --- | --- |
| snapshot | url、title、snapshot：当前活动页的可访问结构。 |
| text | text：selector 所匹配元素的文字。 |
| screenshot | mime_type=image/png、data：Base64 图像。按平台附件能力展示，避免在对话粘贴整段数据。 |
| state | tabs、active_tab、controller、private、dialogs、downloads 等。 |

观察的是当前活动标签页。先通过 browser_action 的 select_tab 切换目标。

## browser_action

必填 resource_id 和 action。params 是对象。部分动作支持 tab_id，默认活动标签页。

| action | params |
| --- | --- |
| navigate | url：完整 http/https 地址。 |
| click | selector，或 role 与 name。role/name 精确匹配。 |
| fill | text，加 selector，或 role 与 name。替换输入框内容。 |
| select | value，加 selector，或 role 与 name。选择原生选项。 |
| key | key：如 Enter、Tab、Control+A。作用于当前焦点。 |
| back / forward / reload | 可选 tab_id。 |
| new_tab | 可选 url。返回 tab_id 并激活新页。 |
| select_tab / close_tab | tab_id：先从 state 获取。 |
| dialog | accept：布尔值；可选 text、tab_id。 |
| upload | files：文件数组；加 selector，或处理当前文件选择器。 |
| resize | width：320–1920；height：240–1440。 |
| retain_download | download_id：从 state.downloads 获取。返回对话附件信息。 |

upload 文件结构为 {"name":"文件名","mime_type":"text/plain","data":"Base64 文件内容"}。
只上传用户授权的文件，总内容上限 8 MiB。浏览器无法直接读取用户电脑文件路径。

示例：填写已观察确认的输入框，不提交：
~~~json
{"resource_id":"从打开结果读取","action":"fill","params":{"selector":"#search","text":"用户指定的关键词"}}
~~~

用户鼠标、键盘、自动取得控制权和自动释放由 Canvas 通道处理，不属于 browser_action 的动作。
不要调用 checkpoint、take_control、release_control、input 或任意脚本执行动作来替代用户操作。

## browser_close

必填 resource_id。返回 closed。会话关闭后不能继续导航或观看。
已保留的对话附件继续有效；未保留的浏览器下载不保证继续可用。
