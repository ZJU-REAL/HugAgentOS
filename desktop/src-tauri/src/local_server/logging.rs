use std::collections::VecDeque;
use std::fs::File;
use std::fs::OpenOptions;
use std::io::BufRead;
use std::io::BufReader;
use std::io::Write;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;
const MAX_LOG_BYTES: u64 = 32 * 1024 * 1024;

/// 服务日志的滚存写入器。
///
/// 服务日志是追加写的，不滚存就会一直涨。此前只在**启动那一刻**查一次大小，而托盘常驻
/// 正是这个产品的用法——一次连续运行攒到 450 MB 是实测过的。上限要在运行中生效，就必须
/// 由壳自己持有文件句柄：子进程那边拿到的是管道，句柄换一份它无感，Windows 上也不会出现
/// 「文件正被占用、改不了名」。
pub(super) struct RotatingLog {
    path: PathBuf,
    /// ``None`` 只出现在滚存过程中间：改名前必须真正关掉句柄，Windows 上改名一个
    /// 仍被打开的文件会失败——那正是"轮转看起来没生效"的样子。
    file: Option<File>,
    written: u64,
    limit: u64,
}

impl RotatingLog {
    pub(super) fn open(path: &Path) -> Result<Self, String> {
        Self::with_limit(path, MAX_LOG_BYTES)
    }

    pub(super) fn with_limit(path: &Path, limit: u64) -> Result<Self, String> {
        let mut log = Self {
            path: path.to_path_buf(),
            file: None,
            written: 0,
            limit,
        };
        log.reopen().map_err(|e| format!("打开服务日志失败：{e}"))?;
        log.rotate_if_oversized();
        Ok(log)
    }

    pub(super) fn reopen(&mut self) -> std::io::Result<()> {
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        self.written = file.metadata().map(|meta| meta.len()).unwrap_or(0);
        self.file = Some(file);
        Ok(())
    }

    pub(super) fn rotate_if_oversized(&mut self) {
        if self.written < self.limit {
            return;
        }
        let mut previous = self.path.as_os_str().to_os_string();
        previous.push(".1");
        self.file = None;
        let _ = std::fs::rename(&self.path, PathBuf::from(previous));
        let _ = self.reopen();
    }

    pub(super) fn append(&mut self, bytes: &[u8]) {
        let Some(file) = self.file.as_mut() else {
            return;
        };
        if file.write_all(bytes).is_ok() {
            self.written += bytes.len() as u64;
            self.rotate_if_oversized();
        }
    }
}

/// 把子进程的一路输出抽到滚存日志里。管道关闭（子进程退出）时线程自然结束。
pub(super) fn pump_output<R: std::io::Read + Send + 'static>(
    mut source: R,
    log: Arc<Mutex<RotatingLog>>,
) {
    std::thread::spawn(move || {
        let mut buffer = [0u8; 8192];
        loop {
            match source.read(&mut buffer) {
                Ok(0) | Err(_) => return,
                Ok(count) => {
                    if let Ok(mut log) = log.lock() {
                        log.append(&buffer[..count]);
                    }
                }
            }
        }
    });
}

pub(super) fn tail_file(path: &Path, max_lines: usize) -> Vec<String> {
    let Ok(file) = File::open(path) else {
        return Vec::new();
    };
    let mut lines: VecDeque<String> = BufReader::new(file).lines().map_while(Result::ok).collect();
    while lines.len() > max_lines {
        lines.pop_front();
    }
    lines.into_iter().collect()
}
