use super::backup::backup_local_data;
use super::backup::prune_data_backups;
use super::backup::restore_local_data;
use crate::local_payload;
use crate::local_payload::PayloadPaths;
use crate::local_server::LocalServerManager;
use std::fs::File;
use std::sync::atomic::Ordering;
use std::sync::Arc;
impl LocalServerManager {
    /// 从桌面安装包携带的 CE 派生树安装或升级本机服务。
    pub fn install_in_background(self: &Arc<Self>) -> bool {
        if self.shutting_down.load(Ordering::SeqCst) {
            return false;
        }
        if self
            .install_running
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return false;
        }
        let manager = self.clone();
        tauri::async_runtime::spawn(async move {
            let result = manager.run_install().await;
            manager.install_running.store(false, Ordering::SeqCst);
            if let Err(error) = result {
                manager.append_log(format!("安装失败：{error}")).await;
                manager.update("error", 0, error).await;
            }
        });
        true
    }

    /// 用户点击「一键安装并启动」时，已安装同版本则只启动，否则执行安装/升级。
    pub fn prepare_in_background(self: &Arc<Self>) -> bool {
        if self.shutting_down.load(Ordering::SeqCst) {
            return false;
        }
        if self.needs_install() {
            self.install_in_background()
        } else {
            self.start_in_background();
            true
        }
    }

    pub(super) async fn run_install(self: &Arc<Self>) -> Result<(), String> {
        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("桌面端正在退出，已取消本机服务安装".to_string());
        }
        if local_payload::current_target() == "unsupported" {
            return Err("当前 CPU 架构没有对应的离线本机运行时".to_string());
        }
        if !self.bundle_archive.is_file()
            || !self.bundle_manifest.is_file()
            || !self.runtime_archive.is_file()
            || !self.runtime_manifest.is_file()
        {
            return Err("安装包未携带本机服务资源，请重新下载完整安装包".to_string());
        }

        let upgrading = self.is_installed();
        self.stop_server()?;
        let data_backup = if upgrading {
            self.update("installing", 3, "正在备份本机数据…").await;
            backup_local_data(&self.data_root, &self.root.join("backups"))?
        } else {
            None
        };
        let installer_log_path = self.installer_log_path();
        if let Some(parent) = installer_log_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| format!("创建安装日志目录失败：{error}"))?;
        }
        File::create(installer_log_path).map_err(|error| format!("重置安装日志失败：{error}"))?;
        self.status.write().await.logs.clear();
        self.update("installing", 2, "正在准备本机服务…").await;
        self.append_log("开始离线安装本机服务；不会下载 Python 或项目依赖。")
            .await;

        let root = self.root.clone();
        let source_archive = self.bundle_archive.clone();
        let source_manifest = self.bundle_manifest.clone();
        let runtime_archive = self.runtime_archive.clone();
        let runtime_manifest = self.runtime_manifest.clone();
        let (progress_tx, mut progress_rx) = tokio::sync::mpsc::unbounded_channel();
        let cancelled = self.shutting_down.clone();
        let install_task = tauri::async_runtime::spawn_blocking(move || {
            let paths = PayloadPaths {
                root: &root,
                source_archive: &source_archive,
                source_manifest: &source_manifest,
                runtime_archive: &runtime_archive,
                runtime_manifest: &runtime_manifest,
            };
            local_payload::install_payloads(
                &paths,
                |progress, message| {
                    let _ = progress_tx.send((progress, message.to_string()));
                },
                &cancelled,
            )
        });
        while let Some((progress, message)) = progress_rx.recv().await {
            self.update("installing", progress, &message).await;
            self.append_log(format!("HUGAGENT_PROGRESS|{progress}|{message}"))
                .await;
        }
        if let Err(error) = install_task
            .await
            .map_err(|error| format!("本机服务安装任务异常退出：{error}"))?
        {
            if self.is_installed() {
                self.append_log("新版本安装失败，正在恢复原有本机服务…")
                    .await;
                if let Err(restart_error) = self.start_server().await {
                    self.append_log(format!("原有本机服务恢复失败：{restart_error}"))
                        .await;
                }
            }
            return Err(error);
        }

        self.status.write().await.installed = self.is_installed();
        self.update("starting", 92, "离线运行环境已就绪，正在启动服务…")
            .await;
        match self.start_server().await {
            Ok(()) => {
                local_payload::prune_old_releases(&self.root);
                prune_data_backups(&self.root.join("backups"));
                Ok(())
            }
            Err(start_error) => {
                if local_payload::restore_previous(&self.root)? {
                    if let Some(backup) = data_backup.as_deref() {
                        restore_local_data(&self.data_root, backup)?;
                    }
                    self.append_log(format!("新版本启动失败，已回滚原有版本：{start_error}"))
                        .await;
                    self.start_server().await.map_err(|rollback_error| {
                        format!(
                            "新版本启动失败（{start_error}），回滚后原有版本也无法启动（{rollback_error}）"
                        )
                    })?;
                    return Err(format!("新版本启动失败，已自动恢复原有版本：{start_error}"));
                }
                Err(start_error)
            }
        }
    }
}
