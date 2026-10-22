// 自定义页面附件上传最小示例
// 用法：将 APP_TYPE / FORM_UUID / FIELDS.evidence 替换成实际值

var APP_TYPE = 'APP_XXX';
var FORM_UUID = 'FORM-XXX';

var FIELDS = {
  evidence: 'attachmentField_xxx',
};

var _customState = {
  attachments: [],
  uploading: false,
};

export function getCustomState(key) {
  if (key) {
    return _customState[key];
  }
  return { ..._customState };
}

export function setCustomState(newState) {
  Object.keys(newState).forEach(function(key) {
    _customState[key] = newState[key];
  });
  this.forceUpdate();
}

export function forceUpdate() {
  this.setState({ timestamp: new Date().getTime() });
}

export function didMount() {}

export function didUnmount() {}

export function buildUploadObjectName(fileName) {
  var now = new Date();
  var monthDay = (now.getMonth() + 1) + '-' + now.getDate();
  return APP_TYPE + '/' + now.getFullYear() + '/' + monthDay + '/' + Date.now() + '-' + fileName;
}

export function requestAttachmentSign(file) {
  var csrfToken = window.g_config && window.g_config._csrf_token || '';
  var stamp = Date.now();
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
    'objectName=' + encodeURIComponent(this.buildUploadObjectName(file.name)),
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

export function uploadSingleAttachment(file) {
  return this.requestAttachmentSign(file).then(function(signInfo) {
    var form = new FormData();
    form.append('key', signInfo.objectName);
    form.append('policy', signInfo.policy);
    form.append('OSSAccessKeyId', signInfo.accessid);
    form.append('signature', signInfo.signature);
    form.append('success_action_status', '200');
    form.append('Content-Disposition', 'attachment; filename=' + encodeURIComponent(file.name));
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

export function handleAttachmentChange(e) {
  var self = this;
  var files = Array.prototype.slice.call(e.target.files || []);
  if (!files.length) {
    return;
  }

  this.setCustomState({ uploading: true });

  Promise.all(files.map((file) => {
    return self.uploadSingleAttachment(file);
  })).then(function(uploaded) {
    var next = (_customState.attachments || []).concat(uploaded);
    self.setCustomState({ attachments: next, uploading: false });
    self.utils.toast({ title: '附件上传成功', type: 'success' });
  }).catch(function(error) {
    var message = error && error.message ? error.message : '附件上传失败';
    self.setCustomState({ uploading: false });
    self.utils.toast({ title: message, type: 'error' });
  });
}

export function removeAttachment(fileUuid) {
  var next = (_customState.attachments || []).filter(function(item) {
    return item.fileUuid !== fileUuid;
  });
  this.setCustomState({ attachments: next });
}

export function submitForm() {
  this.utils.yida.saveFormData({
    appType: APP_TYPE,
    formUuid: FORM_UUID,
    formDataJson: JSON.stringify({
      attachmentField_xxx: _customState.attachments,
    }),
  }).then(function() {
    this.utils.toast({ title: '提交成功', type: 'success' });
  }.bind(this)).catch(function(error) {
    var message = error && error.message ? error.message : '提交失败';
    this.utils.toast({ title: message, type: 'error' });
  }.bind(this));
}

var styles = {
  page: {
    padding: '16px',
    minHeight: '100vh',
    background: '#f7f8fa',
    borderRadius: '0 !important',
  },
  btn: {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    minWidth: '108px',
    height: '36px',
    padding: '0 12px',
    borderRadius: '8px',
    background: 'var(--color-brand1-6, #1677FF)',
    color: '#fff',
    border: 'none',
    fontSize: '12px',
    fontWeight: 700,
    cursor: 'pointer',
  },
  item: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: '8px',
    minHeight: '36px',
    padding: '8px 10px',
    marginTop: '8px',
    background: '#fff',
    border: '1px solid #e5e6eb',
    borderRadius: '8px',
  },
};

export function renderJsx() {
  var state = this.getCustomState();
  var self = this;

  return (
    <div style={styles.page}>
      <div style={{ display: 'none' }}>{this.state.timestamp}</div>

      <label style={styles.btn}>
        {state.uploading ? '上传中...' : '选择附件'}
        <input
          type="file"
          multiple={true}
          style={{ display: 'none' }}
          disabled={state.uploading}
          onChange={(e) => { self.handleAttachmentChange(e); }}
        />
      </label>

      {(state.attachments || []).map((item) => {
        return (
          <div key={item.fileUuid} style={styles.item}>
            <span>{item.name}</span>
            <button style={styles.btn} onClick={(e) => { self.removeAttachment(item.fileUuid); }}>删除</button>
          </div>
        );
      })}

      <div style={{ marginTop: '16px' }}>
        <button style={styles.btn} onClick={(e) => { self.submitForm(); }}>提交</button>
      </div>
    </div>
  );
}
