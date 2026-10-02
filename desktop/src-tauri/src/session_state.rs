//! Owns the in-memory account and bridge state. Callers never receive write locks.
use crate::{auth::SessionEpoch, hybrid::BridgeSync};
use tokio::sync::RwLock;

#[derive(Default, Clone)]
pub(crate) struct Snapshot {
    pub token: Option<String>,
    pub bridge_user: Option<String>,
    pub bridge: BridgeSync,
}

pub(crate) struct SessionState {
    pub epoch: SessionEpoch,
    state: RwLock<Snapshot>,
}

impl SessionState {
    pub fn new(token: Option<String>) -> Self {
        Self {
            epoch: SessionEpoch::default(),
            state: RwLock::new(Snapshot {
                token,
                ..Default::default()
            }),
        }
    }

    pub async fn snapshot(&self) -> Snapshot {
        self.state.read().await.clone()
    }
    pub async fn token(&self) -> Option<String> {
        self.state.read().await.token.clone()
    }
    pub fn has_token(&self) -> bool {
        self.state
            .try_read()
            .map(|s| s.token.is_some())
            .unwrap_or(false)
    }
    pub async fn is_current(&self, expected: u64, token: &str) -> bool {
        let state = self.state.read().await;
        self.epoch.matches(expected)
            && self.epoch.is_active()
            && state.token.as_deref() == Some(token)
    }
    pub async fn accept(&self, expected: u64, token: String) -> bool {
        let mut state = self.state.write().await;
        if !self.epoch.matches(expected) {
            return false;
        }
        state.token = Some(token);
        true
    }
    // local_write serializes persistent/remote writes around these memory changes.
    pub async fn clear(&self, expected: u64) -> Option<String> {
        let mut state = self.state.write().await;
        if !self.epoch.matches(expected) {
            return None;
        }
        std::mem::take(&mut *state).token
    }
    pub async fn set_bridge_user(&self, expected: u64, user: String) -> bool {
        let mut state = self.state.write().await;
        if !self.epoch.matches(expected) || !self.epoch.is_active() {
            return false;
        }
        state.bridge_user = Some(user);
        true
    }
    pub async fn update_bridge(&self, expected: u64, change: impl FnOnce(&mut BridgeSync)) -> bool {
        let mut state = self.state.write().await;
        if !self.epoch.matches(expected) || !self.epoch.is_active() {
            return false;
        }
        change(&mut state.bridge);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn old_account_cannot_overwrite_new_identity_or_readiness() {
        let session = SessionState::new(Some("alice".into()));
        let old = session.epoch.current();
        let new = session.epoch.advance();
        session.clear(new).await;
        assert!(session.accept(new, "bob".into()).await);
        session.epoch.activate(new);
        session
            .update_bridge(new, |s| s.capabilities_ready = true)
            .await;
        assert!(
            !session
                .update_bridge(old, |s| *s = BridgeSync::default())
                .await
        );
        assert!(!session.set_bridge_user(old, "alice".into()).await);
        assert!(!session.accept(old, "alice".into()).await);
        session.clear(old).await;
        assert!(session.is_current(new, "bob").await);
        assert!(session.snapshot().await.bridge.capabilities_ready);
        assert!(session.snapshot().await.bridge_user.is_none());
    }
}
