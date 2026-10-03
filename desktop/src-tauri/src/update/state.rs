use std::sync::OnceLock;
use tokio::sync::watch;
#[derive(Clone, Default, PartialEq, serde::Serialize)]
pub struct UpdateStatus {
    pub current_version: String,
    pub available_version: Option<String>,
    pub busy: bool,
}

static STATUS: OnceLock<watch::Sender<UpdateStatus>> = OnceLock::new();

fn state() -> &'static watch::Sender<UpdateStatus> {
    STATUS.get_or_init(|| {
        watch::channel(UpdateStatus {
            current_version: env!("CARGO_PKG_VERSION").into(),
            ..Default::default()
        })
        .0
    })
}

pub fn status() -> UpdateStatus {
    state().borrow().clone()
}

pub fn subscribe() -> watch::Receiver<UpdateStatus> {
    state().subscribe()
}

pub(super) struct UpdateGuard;
impl Drop for UpdateGuard {
    fn drop(&mut self) {
        state().send_if_modified(|status| {
            if !status.busy {
                return false;
            }
            status.busy = false;
            true
        });
    }
}

pub(super) fn begin() -> Option<UpdateGuard> {
    state()
        .send_if_modified(|status| {
            if status.busy {
                return false;
            }
            status.busy = true;
            true
        })
        .then(|| UpdateGuard)
}

pub(super) fn set_available(version: Option<String>) {
    state().send_if_modified(|status| {
        if status.available_version == version {
            return false;
        }
        status.available_version = version;
        true
    });
}
