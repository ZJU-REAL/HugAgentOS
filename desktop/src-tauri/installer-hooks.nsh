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
