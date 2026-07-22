!include "FileFunc.nsh"

!macro NSIS_HOOK_POSTINSTALL
  ; SSH / 企业软件分发可显式指定首装模式；未传参数时仍沿用交互选择。
  ; 自动更新不会携带这两个参数，因此不会覆盖用户已有配置。
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/HUGAGENT_LOCAL" $R1
  IfErrors hugagent_install_mode_check_remote
  StrCpy $R2 "local"
  Goto hugagent_install_mode_write

  hugagent_install_mode_check_remote:
  ClearErrors
  ${GetOptions} $R0 "/HUGAGENT_REMOTE" $R1
  IfErrors hugagent_install_mode_check_silent
  StrCpy $R2 "remote"
  Goto hugagent_install_mode_write

  hugagent_install_mode_check_silent:
  ; Tauri 的 NSIS 自动更新使用 /P（passive）或 /S（silent），不能再次弹首装选择。
  IfSilent hugagent_install_mode_done
  ClearErrors
  ${GetOptions} $R0 "/P" $R1
  IfErrors hugagent_install_mode_check_long_passive hugagent_install_mode_done

  hugagent_install_mode_check_long_passive:
  ClearErrors
  ${GetOptions} $R0 "/passive" $R1
  IfErrors hugagent_install_mode_interactive hugagent_install_mode_done

  hugagent_install_mode_interactive:
  ; 已选择过部署方式时保持原值，升级安装不覆盖用户配置。
  IfFileExists "$APPDATA\com.hugagent.desktop\install-mode" hugagent_install_mode_done
  MessageBox MB_YESNO|MB_ICONQUESTION "是否同时安装无 Docker 的本机服务？选择“是”后，首次启动会联网下载 Python 依赖；选择“否”则连接已有服务器。" IDNO hugagent_install_mode_remote
  StrCpy $R2 "local"
  Goto hugagent_install_mode_write

  hugagent_install_mode_remote:
  StrCpy $R2 "remote"

  hugagent_install_mode_write:
  CreateDirectory "$APPDATA\com.hugagent.desktop"
  FileOpen $R3 "$APPDATA\com.hugagent.desktop\install-mode" w
  FileWrite $R3 "$R2"
  FileClose $R3
  ; 单独写一次性待处理标记：兼容已有 server.json 的旧版客户端升级。
  FileOpen $R3 "$APPDATA\com.hugagent.desktop\install-mode.pending" w
  FileWrite $R3 "$R2"
  FileClose $R3

  hugagent_install_mode_done:
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ; 本机模式把 venv、CE 源码、SQLite/上传文件、Node 工具与日志统一放在
  ; app_local_data_dir/local-server。卸载前只结束该目录下、且 PID 与记录
  ; 匹配的进程，避免陈旧 PID 误杀其它 Python；随后清理整套托管数据。
  nsExec::ExecToLog `powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$root=[IO.Path]::GetFullPath('$LOCALAPPDATA\com.hugagent.desktop\local-server').TrimEnd('\'); $$pidFile=Join-Path $$root 'server.pid'; if(Test-Path -LiteralPath $$pidFile){ $$pidText=(Get-Content -LiteralPath $$pidFile -Raw).Trim(); if($$pidText -match '^\d+$$'){ $$p=Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $$pidText) -ErrorAction SilentlyContinue; if($$p -and $$p.ExecutablePath -and [IO.Path]::GetFullPath($$p.ExecutablePath).StartsWith($$root + '\',[StringComparison]::OrdinalIgnoreCase)){ & taskkill.exe /PID $$pidText /T /F | Out-Null } } }"`
  Sleep 500
  RMDir /r "$LOCALAPPDATA\com.hugagent.desktop\local-server"
!macroend
