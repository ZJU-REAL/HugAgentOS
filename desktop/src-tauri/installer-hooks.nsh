!include "FileFunc.nsh"
!define HUGAGENT_UNINSTALL_CLEANUP_SOURCE "${__FILEDIR__}\uninstall-cleanup.ps1"

!macro NSIS_HOOK_POSTINSTALL
  ; SSH / 企业软件分发可显式指定首装模式；普通交互安装不弹任何窗口——
  ; 运行模式在应用首启的初始化页选择。自动更新不携带这两个参数，不会覆盖用户配置。
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
  ; 安装器不做任何交互弹窗——运行模式在应用首启的初始化页里选（本机 / 云端 /
  ; 本机+云端三选一）。这里只处理软件分发系统显式传入的 /HUGAGENT_LOCAL、
  ; /HUGAGENT_REMOTE 参数；未传参数时什么都不写，首启必出初始化页。
  Goto hugagent_install_mode_done

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
  ; 清理助手嵌入卸载器，先验证根路径；不访问 junction 重定向的运行目录。
  InitPluginsDir
  File /oname=$PLUGINSDIR\hugagent-uninstall-cleanup.ps1 "${HUGAGENT_UNINSTALL_CLEANUP_SOURCE}"
  nsExec::ExecToStack `powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\hugagent-uninstall-cleanup.ps1" -AppRoot "$LOCALAPPDATA\com.hugagent.desktop" -ValidateOnly`
  Pop $R4
  Pop $R3
  StrCmp $R4 "0" hugagent_uninstall_root_valid
  MessageBox MB_OK|MB_ICONEXCLAMATION "本机数据路径已重定向或无法验证，已跳过清理以保护数据。"
  Goto hugagent_uninstall_cleanup_done
  hugagent_uninstall_root_valid:
  ; 默认保留业务数据与四类能力；只有明确确认时才删除。
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
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "是否同时删除本机能力和服务数据？选择“否”会保留技能、插件、智能体、MCP 配置、账号、对话、上传文件和工作区，重新安装后可继续使用。" IDNO hugagent_uninstall_choice_done
  StrCpy $R5 "1"

  hugagent_uninstall_choice_done:
  ; 结束所有从 local-server 运行目录里启动的进程：服务本体、脚本执行、内置 MCP，
  ; 以及上次异常退出留下的孤儿。只看可执行文件所在位置，不会碰其它产品或系统 Python。
  ; 进程退出后再原子移走运行目录。
  nsExec::ExecToLog `powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$root=[IO.Path]::GetFullPath('$LOCALAPPDATA\com.hugagent.desktop\local-server').TrimEnd('\') + '\'; Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $$_.ExecutablePath -and [IO.Path]::GetFullPath($$_.ExecutablePath).StartsWith($$root,[StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { & taskkill.exe /PID $$_.ProcessId /T /F | Out-Null }"`
  Sleep 500

  StrCpy $R6 "$LOCALAPPDATA\com.hugagent.desktop\local-server"
  ; 仅有新版顶层能力而运行目录缺失时，仍允许用户明确清理。
  CreateDirectory "$R6"
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
  ${GetFileName} $R8 $R9
  StrCpy $R8 "$LOCALAPPDATA\com.hugagent.desktop\remove-$R9"
  ClearErrors
  Rename "$R6" "$R8"
  IfErrors hugagent_uninstall_detach_failed

  StrCmp $R7 "" hugagent_uninstall_start_cleanup
  CreateDirectory "$R6"
  ClearErrors
  Rename "$R7" "$R6\data"
  IfErrors hugagent_uninstall_restore_failed

  hugagent_uninstall_start_cleanup:
  ; 后台助手只枚举普通目录，遇到联接只删除链接本身。
  ; 助手先复制到已脱离的目录，避免卸载器退出后 PLUGINSDIR 消失。
  ClearErrors
  CopyFiles /SILENT "$PLUGINSDIR\hugagent-uninstall-cleanup.ps1" "$R8\hugagent-uninstall-cleanup.ps1"
  IfErrors hugagent_uninstall_cleanup_copy_failed
  ExecShell "" "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" `-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$R8\hugagent-uninstall-cleanup.ps1" -AppRoot "$LOCALAPPDATA\com.hugagent.desktop" -DetachedRuntime "$R8" -DeleteData $R5` SW_HIDE
  Goto hugagent_uninstall_cleanup_done
  hugagent_uninstall_cleanup_copy_failed:
  MessageBox MB_OK|MB_ICONEXCLAMATION "无法启动安全清理，文件已保留在：$R8"
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
