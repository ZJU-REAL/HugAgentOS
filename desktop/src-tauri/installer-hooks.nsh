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
  ; 默认保留 data；交互卸载只有在用户明确确认时才随运行环境一起删除。
  ; 静默更新不弹窗，也不会删除用户数据。软件分发系统可显式传入
  ; /HUGAGENT_DELETE_DATA 请求清理全部本机数据。
  StrCpy $R5 "0"
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/HUGAGENT_DELETE_DATA" $R1
  IfErrors hugagent_uninstall_check_interactive
  StrCpy $R5 "1"
  Goto hugagent_uninstall_choice_done

  hugagent_uninstall_check_interactive:
  IfSilent hugagent_uninstall_choice_done
  ClearErrors
  ${GetOptions} $R0 "/P" $R1
  IfErrors hugagent_uninstall_check_long_passive hugagent_uninstall_choice_done

  hugagent_uninstall_check_long_passive:
  ClearErrors
  ${GetOptions} $R0 "/passive" $R1
  IfErrors hugagent_uninstall_ask_delete_data hugagent_uninstall_choice_done

  hugagent_uninstall_ask_delete_data:
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "是否同时删除本机服务数据？选择“否”会保留账号、对话、上传文件和工作区，重新安装后可继续使用。" IDNO hugagent_uninstall_choice_done
  StrCpy $R5 "1"

  hugagent_uninstall_choice_done:
  ; 只结束 local-server 下、且 PID 与记录匹配的进程，避免陈旧 PID
  ; 误杀其它 Python。进程退出后再原子移走运行目录。
  nsExec::ExecToLog `powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$root=[IO.Path]::GetFullPath('$LOCALAPPDATA\com.hugagent.desktop\local-server').TrimEnd('\'); $$pidFile=Join-Path $$root 'server.pid'; if(Test-Path -LiteralPath $$pidFile){ $$pidText=(Get-Content -LiteralPath $$pidFile -Raw).Trim(); if($$pidText -match '^\d+$$'){ $$p=Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $$pidText) -ErrorAction SilentlyContinue; if($$p -and $$p.ExecutablePath -and [IO.Path]::GetFullPath($$p.ExecutablePath).StartsWith($$root + '\',[StringComparison]::OrdinalIgnoreCase)){ & taskkill.exe /PID $$pidText /T /F | Out-Null } } }"`
  Sleep 500

  StrCpy $R6 "$LOCALAPPDATA\com.hugagent.desktop\local-server"
  IfFileExists "$R6" 0 hugagent_uninstall_cleanup_done
  StrCpy $R7 ""

  ; 保留数据时先把 data 原子挪到同卷临时名，再把剩余 local-server
  ; 整体原子改名。这样卸载器不用同步枚举数万个 venv/Node 小文件。
  StrCmp $R5 "1" hugagent_uninstall_detach_runtime
  IfFileExists "$R6\data" 0 hugagent_uninstall_detach_runtime
  GetTempFileName $R7 "$LOCALAPPDATA\com.hugagent.desktop"
  Delete "$R7"
  ClearErrors
  Rename "$R6\data" "$R7"
  IfErrors hugagent_uninstall_preserve_failed

  hugagent_uninstall_detach_runtime:
  GetTempFileName $R8 "$LOCALAPPDATA\com.hugagent.desktop"
  Delete "$R8"
  ClearErrors
  Rename "$R6" "$R8"
  IfErrors hugagent_uninstall_detach_failed

  StrCmp $R7 "" hugagent_uninstall_start_cleanup
  CreateDirectory "$R6"
  ClearErrors
  Rename "$R7" "$R6\data"
  IfErrors hugagent_uninstall_restore_failed

  hugagent_uninstall_start_cleanup:
  ; ExecShell 不等待子进程；原生 rd 在隐藏后台清理已改名目录。
  ; 用户立即完成卸载，实际磁盘回收继续进行。
  ExecShell "" "$SYSDIR\cmd.exe" `/D /Q /C RD /S /Q "$R8"` SW_HIDE
  Goto hugagent_uninstall_cleanup_done

  hugagent_uninstall_preserve_failed:
  MessageBox MB_OK|MB_ICONEXCLAMATION "本机服务数据正在被占用，已跳过本机服务清理以保护数据。"
  Goto hugagent_uninstall_cleanup_done

  hugagent_uninstall_detach_failed:
  StrCmp $R7 "" hugagent_uninstall_detach_warning
  CreateDirectory "$R6"
  Rename "$R7" "$R6\data"
  hugagent_uninstall_detach_warning:
  MessageBox MB_OK|MB_ICONEXCLAMATION "本机服务文件正在被占用，已跳过后台清理；可稍后手动删除 local-server。"
  Goto hugagent_uninstall_cleanup_done

  hugagent_uninstall_restore_failed:
  MessageBox MB_OK|MB_ICONEXCLAMATION "运行环境已进入后台清理，但数据目录未能恢复。数据仍安全保存在：$R7"
  Goto hugagent_uninstall_start_cleanup

  hugagent_uninstall_cleanup_done:
!macroend
