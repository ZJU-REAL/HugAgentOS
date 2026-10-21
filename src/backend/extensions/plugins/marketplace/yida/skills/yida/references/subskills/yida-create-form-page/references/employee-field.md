## EmployeeField 设置默认值为当前登录人

宜搭 EmployeeField 设置默认值为当前登录人，需要添加以下三个属性：

```json
{
  "valueType": "variable",
  "complexValue": {
    "complexType": "formula",
    "formula": "USER()",
    "value": []
  },
  "variable": {
    "type": "user"
  }
}
```