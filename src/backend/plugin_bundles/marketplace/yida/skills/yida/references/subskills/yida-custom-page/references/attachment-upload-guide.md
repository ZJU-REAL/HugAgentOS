# 自定义页面实现 AttachmentField 上传

> 本文档说明如何在宜搭自定义页面中实现附件上传，并将结果写入 `AttachmentField`。

## 适用场景

- 自定义提交页中需要上传图片、文档、压缩包等附件
- 页面不是原生表单，而是自定义 JSX 页面
- 最终仍需将附件写入宜搭表单字段

## 核心结论

`AttachmentField` 不能直接写浏览器里的 `File` 对象。

正确链路是：

1. 用户选择本地文件
2. 调用 `/ossSign` 获取临时上传签名
3. 使用返回的 `host / policy / signature / accessid / objectName` 直传 OSS
4. 组装附件对象数组
5. 调用 `this.utils.yida.saveFormData`，将附件数组写入 `AttachmentField`

说明：

- 这套链路同样适用于 `ImageField`
- `Content-Disposition` 不要自行按文件名拼接
- 上传时应优先复用 `policy` 中签名要求的 `Content-Disposition`

## 第一步：调用 `/ossSign`

请求方式：`GET`

示例参数：

- `scene=AttachmentField`
- `_csrf_token=window.g_config._csrf_token`
- `appType=APP_XXX`
- `fileName=image.png`
- `fileSize=117611`
- `contentType=image/png`
- `isOpen=n`
- `newContext=y`
- `objectName=APP_XXX/2026/4-1/UUID.png`
- `procInstId=`
- `businessType=`
- `accelerate=y`

示例代码：

```javascript
export function requestAttachmentSign(file) {
  var csrfToken = window.g_config && window.g_config._csrf_token || '';
  var stamp = Date.now();
  var objectName = 'APP_XXX/2026/4-1/' + stamp + '-' + file.name;
  var query = [
    'scene=AttachmentField',
    '_api=nattyFetch',
    '_mock=false',
    '_csrf_token=' + encodeURIComponent(csrfToken),
    'appType=' + encodeURIComponent('APP_XXX'),
    'fileName=' + encodeURIComponent(file.name),
    'fileSize=' + encodeURIComponent(file.size),
    'contentType=' + encodeURIComponent(file.type || 'application/octet-stream'),
    'isOpen=n',
    'newContext=y',
    'objectName=' + encodeURIComponent(objectName),
    'procInstId=',
    'businessType=',
    'accelerate=y',
    '_stamp=' + stamp,
  ].join('&');

  return fetch(window.location.origin + '/ossSign?' + query, {
    method: 'GET',
    credentials: 'include',
    headers: {
      accept: 'application/json, text/json',
      'x-requested-with': 'XMLHttpRequest',
    },
  }).then(function(res) {
    return res.json();
  }).then(function(json) {
    if (!json || json.success === false || !json.content) {
      throw new Error(json && json.errorMsg ? json.errorMsg : '附件签名失败');
    }
    return json.content;
  });
}
```

## 第二步：直传 OSS

`ossSign` 返回后，需使用浏览器 `FormData` 直传到 `content.host`。

必填字段：

- `key`
- `policy`
- `OSSAccessKeyId`
- `signature`
- `success_action_status=200`
- `Content-Disposition`
- `file`

其中 `Content-Disposition` 不能简单写成 `attachment; filename=` 加文件名。更稳妥的方式是：

1. 先把 `policy` 按 UTF-8 解码
2. 从签名内容中提取真正要求的 `Content-Disposition`
3. 上传时原样回传给 OSS

辅助函数：

```javascript
export function decodeBase64Utf8(base64Text) {
  try {
    var binary = atob(base64Text || '');
    var bytes = [];
    for (var i = 0; i < binary.length; i += 1) {
      bytes.push(binary.charCodeAt(i));
    }
    if (typeof TextDecoder !== 'undefined') {
      return new TextDecoder('utf-8').decode(new Uint8Array(bytes));
    }
    return decodeURIComponent(bytes.map(function(byte) {
      return '%' + ('00' + byte.toString(16)).slice(-2);
    }).join(''));
  } catch (error) {
    return atob(base64Text || '');
  }
}

export function resolveSignedContentDisposition(signInfo, file) {
  try {
    var policyText = this.decodeBase64Utf8(signInfo.policy || '');
    var matchedText = policyText.match(/"Content-Disposition":"([^"]+)"/);
    if (matchedText && matchedText[1]) {
      return matchedText[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\');
    }
    var normalizedPolicyText = policyText.replace(/\\\$/g, '$');
    var policy = JSON.parse(normalizedPolicyText);
    var conditions = policy && policy.conditions || [];
    var matched = '';
    conditions.forEach(function(item) {
      if (item && typeof item === 'object' && item['Content-Disposition']) {
        matched = item['Content-Disposition'];
      }
    });
    if (matched) {
      return matched;
    }
  } catch (error) {}
  return 'attachment; filename=' + encodeURIComponent(file.name);
}
```

示例代码：

```javascript
export function uploadSingleAttachment(file) {
  return this.requestAttachmentSign(file).then(function(signInfo) {
    var form = new FormData();
    form.append('key', signInfo.objectName);
    form.append('policy', signInfo.policy);
    form.append('OSSAccessKeyId', signInfo.accessid);
    form.append('signature', signInfo.signature);
    form.append('success_action_status', '200');
    form.append('Content-Disposition', this.resolveSignedContentDisposition(signInfo, file));
    form.append('file', file, file.name);

    return fetch(signInfo.host, {
      method: 'POST',
      body: form,
    }).then(function(uploadRes) {
      if (!uploadRes.ok) {
        throw new Error('附件上传失败');
      }
      return {
        name: file.name,
        size: file.size,
        fileUuid: signInfo.objectName,
        url: signInfo.url,
        downloadUrl: signInfo.downloadUrl,
        previewUrl: signInfo.previewUrl,
      };
    });
  }.bind(this));
}
```

## 第三步：写入 `AttachmentField`

上传成功后，不能写单个对象，也不能写纯文本。

`AttachmentField` 需要的是附件对象数组：

```javascript
[
  {
    name: 'image.png',
    size: 117611,
    fileUuid: 'APP_XXX_...png',
    url: '/ossFileHandle?...',
    downloadUrl: '/ossFileHandle?...type=download',
    previewUrl: '/inst/preview?...'
  }
]
```

示例：

```javascript
export function buildPayload(state) {
  var payload = {};
  payload.attachmentField_xxx = state.attachments;
  return payload;
}

export function submitForm() {
  var payload = this.buildPayload(this.getCustomState());
  this.utils.yida.saveFormData({
    appType: 'APP_XXX',
    formUuid: 'FORM-XXX',
    formDataJson: JSON.stringify(payload),
  }).catch(function(error) {
    var message = error && error.message ? error.message : '提交失败';
    this.utils.toast({ title: message, type: 'error' });
  }.bind(this));
}
```

## 最小可复制片段

如果你只想快速把附件上传能力接进现有自定义页面，下面这段是最小闭环：

```javascript
var APP_TYPE = 'APP_XXX';
var FORM_UUID = 'FORM-XXX';
var FIELDS = {
  evidence: 'attachmentField_xxx',
};

var _customState = {
  attachments: [],
  uploading: false,
};

export function requestAttachmentSign(file) {
  var csrfToken = window.g_config && window.g_config._csrf_token || '';
  var stamp = Date.now();
  var objectName = APP_TYPE + '/' + stamp + '-' + file.name;
  var query = [
    'scene=AttachmentField',
    '_api=nattyFetch',
    '_mock=false',
    '_csrf_token=' + encodeURIComponent(csrfToken),
    'appType=' + encodeURIComponent(APP_TYPE),
    'fileName=' + encodeURIComponent(file.name),
    'fileSize=' + encodeURIComponent(file.size),
    'contentType=' + encodeURIComponent(file.type || 'application/octet-stream'),
    'isOpen=n',
    'newContext=y',
    'objectName=' + encodeURIComponent(objectName),
    'procInstId=',
    'businessType=',
    'accelerate=y',
    '_stamp=' + stamp,
  ].join('&');

  return fetch(window.location.origin + '/ossSign?' + query, {
    method: 'GET',
    credentials: 'include',
    headers: {
      accept: 'application/json, text/json',
      'x-requested-with': 'XMLHttpRequest',
    },
  }).then(function(res) {
    return res.json();
  }).then(function(json) {
    return json.content;
  });
}

export function decodeBase64Utf8(base64Text) {
  try {
    var binary = atob(base64Text || '');
    var bytes = [];
    for (var i = 0; i < binary.length; i += 1) {
      bytes.push(binary.charCodeAt(i));
    }
    if (typeof TextDecoder !== 'undefined') {
      return new TextDecoder('utf-8').decode(new Uint8Array(bytes));
    }
    return decodeURIComponent(bytes.map(function(byte) {
      return '%' + ('00' + byte.toString(16)).slice(-2);
    }).join(''));
  } catch (error) {
    return atob(base64Text || '');
  }
}

export function resolveSignedContentDisposition(signInfo, file) {
  try {
    var policyText = this.decodeBase64Utf8(signInfo.policy || '');
    var matchedText = policyText.match(/"Content-Disposition":"([^"]+)"/);
    if (matchedText && matchedText[1]) {
      return matchedText[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\');
    }
    var normalizedPolicyText = policyText.replace(/\\\$/g, '$');
    var policy = JSON.parse(normalizedPolicyText);
    var conditions = policy && policy.conditions || [];
    var matched = '';
    conditions.forEach(function(item) {
      if (item && typeof item === 'object' && item['Content-Disposition']) {
        matched = item['Content-Disposition'];
      }
    });
    if (matched) {
      return matched;
    }
  } catch (error) {}
  return 'attachment; filename=' + encodeURIComponent(file.name);
}

export function uploadSingleAttachment(file) {
  return this.requestAttachmentSign(file).then(function(signInfo) {
    var form = new FormData();
    form.append('key', signInfo.objectName);
    form.append('policy', signInfo.policy);
    form.append('OSSAccessKeyId', signInfo.accessid);
    form.append('signature', signInfo.signature);
    form.append('success_action_status', '200');
    form.append('Content-Disposition', this.resolveSignedContentDisposition(signInfo, file));
    form.append('file', file, file.name);

    return fetch(signInfo.host, {
      method: 'POST',
      body: form,
    }).then(function() {
      return {
        name: file.name,
        size: file.size,
        fileUuid: signInfo.objectName,
        url: signInfo.url,
        downloadUrl: signInfo.downloadUrl,
        previewUrl: signInfo.previewUrl,
      };
    });
  }.bind(this));
}

export function handleAttachmentChange(e) {
  var files = Array.prototype.slice.call(e.target.files || []);
  if (!files.length) {
    return;
  }

  this.setCustomState({ uploading: true });

  Promise.all(files.map(function(file) {
    return this.uploadSingleAttachment(file);
  }.bind(this))).then(function(uploaded) {
    this.setCustomState({
      attachments: (_customState.attachments || []).concat(uploaded),
      uploading: false,
    });
  }.bind(this));
}

export function submitForm() {
  this.utils.yida.saveFormData({
    appType: APP_TYPE,
    formUuid: FORM_UUID,
    formDataJson: JSON.stringify({
      attachmentField_xxx: _customState.attachments,
    }),
  });
}
```

配套渲染片段：

```jsx
var self = this;

<label>
  选择附件
  <input
    type="file"
    multiple={true}
    style={{ display: 'none' }}
    onChange={(e) => { self.handleAttachmentChange(e); }}
  />
</label>
```

如果你要完整可运行版本，直接看：

- [附件上传示例](../examples/attachment-upload.js)

## 完整交互建议

- 页面状态中维护 `attachments`
- 选择文件后立即上传
- 上传成功后将结果追加到 `attachments`
- 支持删除已选附件
- 最终提交时，将 `attachments` 整体写入表单

## 常见坑

### 1. 不能直接写 `File`

错误：

```javascript
payload.attachmentField_xxx = file;
```

正确：

```javascript
payload.attachmentField_xxx = [attachmentObject];
```

### 2. 不能写纯文本

错误：

```javascript
payload.attachmentField_xxx = '附件说明';
```

这会导致类似 `syntax error, expect [` 的报错。

### 3. 必须带 `_csrf_token`

`/ossSign` 依赖 `window.g_config._csrf_token`。

### 4. 上传成功不等于落表成功

OSS 上传成功后，只是文件已存在临时附件地址。
仍需在 `saveFormData` 时把附件对象写入 `AttachmentField`。

### 5. 中文文件名上传可能触发 OSS `403`

如果上传中文文件名时遇到 `403`，优先排查两件事：

- 是否自己重拼了 `Content-Disposition`
- 是否把 `policy` 错当普通字符串解码，而没有按 UTF-8 还原

推荐直接使用本文前面的统一实现：

- `decodeBase64Utf8`
- `resolveSignedContentDisposition`

这样英文文件名和中文文件名都走同一套上传逻辑，不需要再额外分支处理。

## 推荐搭配阅读

- [编码指南](./coding-guide.md)
- 完整模板：执行 `openyida sample yida-custom-page custom-page-template`
- [附件上传示例](../examples/attachment-upload.js)
