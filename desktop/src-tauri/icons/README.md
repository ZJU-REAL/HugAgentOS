# 应用图标

`tauri.conf.json` 引用了这些图标文件，构建前必须先生成：

```
32x32.png  128x128.png  128x128@2x.png  icon.icns  icon.ico
```

最简单的办法：准备一张 ≥ 1024×1024 的品牌 PNG（ / HugAgentOS 各一套），用 Tauri 自带工具一键生成全套：

```bash
cd desktop
npm run tauri icon /path/to/logo-1024.png
# 产物自动写入 src-tauri/icons/
```

多品牌：分别用各自 logo 跑一次 `tauri icon`，构建对应品牌包前替换本目录即可。
