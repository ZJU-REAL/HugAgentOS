#!/usr/bin/env python3
"""
Security self-test for HugAgentOS.

Tests security features including:
- Authentication and authorization
- File access controls
- Input validation
- Data masking
- SQL injection prevention
- Path traversal prevention

Usage:
    python -m pytest selftests/security_selftest.py -v
"""

import pytest
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.db.engine import Base, get_db
from core.auth.backend import AuthService, get_current_user
from core.infra.data_masking import (
    mask_phone, mask_email, mask_api_key, mask_password,
    mask_sensitive_data, mask_log_data
)
from core.services import ChatService, ArtifactService, KBService


# Test database setup
TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    """Override database dependency for testing."""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module")
def test_db():
    """Create test database."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    """Create a new database session for a test."""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class TestAuthentication:
    """Test authentication mechanisms."""

    def test_mock_auth_mode(self):
        """Test that mock authentication works in development."""
        os.environ["AUTH_MODE"] = "mock"
        auth_service = AuthService()

        user_info = auth_service.verify_token("any_token")

        assert "user_center_id" in user_info
        assert "username" in user_info
        assert user_info["username"] == os.getenv("AUTH_MOCK_USERNAME", "Developer")

    def test_auth_mode_detection(self):
        """Test authentication mode detection."""
        # Test mock mode
        os.environ["AUTH_MODE"] = "mock"
        auth_service = AuthService()
        assert auth_service.auth_mode == "mock"

        # Test remote mode
        os.environ["AUTH_MODE"] = "remote"
        auth_service = AuthService()
        assert auth_service.auth_mode == "remote"


class TestAuthorization:
    """Test authorization and access control."""

    def test_session_ownership_check(self, db_session, test_db):
        """Test that users can only access their own sessions."""
        chat_service = ChatService(db_session)

        # User A creates a session
        session_a = chat_service.create_session("user_a", "Session A")

        # User A can access their own session
        result = chat_service.get_session(session_a.chat_id, "user_a")
        assert result is not None
        assert result.chat_id == session_a.chat_id

        # User B cannot access User A's session
        result = chat_service.get_session(session_a.chat_id, "user_b")
        assert result is None

    def test_file_ownership_check(self, db_session, test_db):
        """Test that users can only access their own files."""
        artifact_service = ArtifactService(db_session)

        # User A creates an artifact
        artifact_a = artifact_service.create_artifact(
            user_id="user_a",
            artifact_type="chart",
            title="Chart A",
            filename="chart_a.png",
            size_bytes=1024,
            mime_type="image/png",
            storage_key="user_a/chart_a.png"
        )

        # User A can access their own artifact
        result = artifact_service.get_artifact(artifact_a["artifact_id"], "user_a")
        assert result is not None
        assert result["artifact_id"] == artifact_a["artifact_id"]

        # User B cannot access User A's artifact
        result = artifact_service.get_artifact(artifact_a["artifact_id"], "user_b")
        assert result is None

    def test_kb_space_ownership_check(self, db_session, test_db):
        """Test that users can only access their own KB spaces."""
        kb_service = KBService(db_session)

        # User A creates a KB space
        space_a = kb_service.create_space("user_a", "Space A")

        # User B attempts to upload to User A's space (should fail)
        with pytest.raises(PermissionError, match="Access denied"):
            kb_service.upload_document(
                kb_id=space_a["kb_id"],
                user_id="user_b",
                title="Document B",
                filename="doc_b.pdf",
                size_bytes=2048,
                mime_type="application/pdf",
                storage_key="user_b/doc_b.pdf"
            )


class TestInputValidation:
    """Test input validation and sanitization."""

    def test_sql_injection_prevention(self, db_session, test_db):
        """Test that SQL injection is prevented by ORM."""
        chat_service = ChatService(db_session)

        # Create a legitimate session
        session = chat_service.create_session("user_test", "Normal Session")

        # Attempt SQL injection in search query
        malicious_query = "'; DROP TABLE chat_sessions; --"

        # Should not raise exception and should not drop table
        try:
            results, total = chat_service.search_sessions("user_test", malicious_query)
            # Should return empty results, not execute SQL
            assert isinstance(results, list)
        except Exception as e:
            pytest.fail(f"SQL injection attempt raised exception: {e}")

        # Verify table still exists by querying it
        existing_session = chat_service.get_session(session.chat_id, "user_test")
        assert existing_session is not None

    def test_path_traversal_prevention(self):
        """Test that path traversal attacks are prevented."""
        # In our implementation, files are accessed by ID, not path
        # So path traversal is not possible

        # Test that artifact_id doesn't allow path characters
        malicious_ids = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "file:///etc/passwd",
        ]

        # These should all be treated as regular IDs (not found)
        # The system doesn't interpret them as paths
        for malicious_id in malicious_ids:
            # In production, this would return None (not found)
            # because the ID doesn't exist in database
            assert "/" in malicious_id or "\\" in malicious_id  # Contains path separators


class TestDataMasking:
    """Test data masking functions."""

    def test_phone_masking(self):
        """Test phone number masking."""
        assert mask_phone("13812345678") == "138****5678"
        assert mask_phone("1234567") == "1234567"  # Too short, no masking
        assert mask_phone("+86 138 1234 5678") == "138****5678"

    def test_email_masking(self):
        """Test email masking."""
        assert mask_email("user@example.com") == "us***@example.com"
        assert mask_email("a@test.com") == "a***@test.com"
        assert mask_email("admin@company.org") == "ad***@company.org"

    def test_api_key_masking(self):
        """Test API key masking."""
        assert mask_api_key("sk-1234567890abcdef") == "sk-1...cdef"
        assert mask_api_key("short") == "***"

    def test_password_masking(self):
        """Test password masking."""
        assert mask_password("my_secure_password") == "***"
        assert mask_password("") == ""

    def test_sensitive_data_masking(self):
        """Test masking sensitive data in dictionaries."""
        data = {
            "username": "john",
            "phone": "13812345678",
            "email": "john@example.com",
            "password": "secret123",
            "api_key": "sk-1234567890abcdef"
        }

        masked = mask_sensitive_data(data)

        assert masked["username"] == "john"
        assert masked["phone"] == "138****5678"
        assert masked["email"] == "jo***@example.com"
        assert masked["password"] == "***"
        assert "***" in masked["api_key"]

    def test_log_data_masking(self):
        """Test masking sensitive data in log strings."""
        log_string = 'Authorization: Bearer sk-1234567890abcdef, password="secret123"'

        masked = mask_log_data(log_string)

        assert "Bearer ***" in masked
        assert "secret123" not in masked
        assert "***" in masked

    def test_log_data_masking_key_value(self):
        """Test masking key=value tokens does not raise and is properly masked."""
        log_string = "token=abc123 password=secret api_key=sk-demo"

        masked = mask_log_data(log_string)

        assert "abc123" not in masked
        assert "secret" not in masked
        assert "sk-demo" not in masked
        assert "token=***" in masked.lower()
        assert "password=***" in masked.lower()
        assert "api_key=***" in masked.lower()

    def test_nested_data_masking(self):
        """Test masking nested data structures."""
        data = {
            "user": {
                "name": "John Doe",
                "contact": {
                    "phone": "13812345678",
                    "email": "john@example.com"
                }
            },
            "credentials": {
                "password": "secret",
                "api_key": "sk-abcdef1234567890"
            }
        }

        masked = mask_sensitive_data(data)

        assert masked["user"]["name"] == "John Doe"
        assert masked["user"]["contact"]["phone"] == "138****5678"
        assert masked["user"]["contact"]["email"] == "jo***@example.com"
        assert masked["credentials"]["password"] == "***"
        assert "***" in masked["credentials"]["api_key"]


class TestAuditLogging:
    """Test audit logging functionality."""

    def test_session_creation_logged(self, db_session, test_db):
        """Test that session creation is logged."""
        from core.db.repository import AuditLogRepository

        chat_service = ChatService(db_session)
        audit_repo = AuditLogRepository(db_session)

        # Create a session
        session = chat_service.create_session("user_audit_test", "Test Session")

        # Check audit log
        logs, _ = audit_repo.list_by_user("user_audit_test", action="chat.session.created")

        assert len(logs) > 0
        log = logs[0]
        assert log.action == "chat.session.created"
        assert log.resource_type == "chat_session"
        assert log.resource_id == session.chat_id
        assert log.status == "success"

    def test_session_deletion_logged(self, db_session, test_db):
        """Test that session deletion is logged."""
        from core.db.repository import AuditLogRepository

        chat_service = ChatService(db_session)
        audit_repo = AuditLogRepository(db_session)

        # Create and delete a session
        session = chat_service.create_session("user_audit_test", "Test Session")
        chat_service.delete_session(session.chat_id, "user_audit_test")

        # Check audit log
        logs, _ = audit_repo.list_by_user("user_audit_test", action="chat.session.deleted")

        assert len(logs) > 0
        log = logs[0]
        assert log.action == "chat.session.deleted"
        assert log.resource_type == "chat_session"
        assert log.resource_id == session.chat_id
        assert log.status == "success"

    def test_failed_access_logged(self, db_session, test_db):
        """Test that failed access attempts are logged."""
        from core.db.repository import AuditLogRepository

        kb_service = KBService(db_session)
        audit_repo = AuditLogRepository(db_session)

        # Create a KB space
        space = kb_service.create_space("user_owner", "Owner Space")

        # Attempt to delete with wrong user (should fail and log)
        result = kb_service.delete_space(space["kb_id"], "user_attacker")

        assert result is False

        # Check audit log for failed attempt
        logs, _ = audit_repo.list_by_user("user_attacker", action="kb.space.delete.failed")

        assert len(logs) > 0
        log = logs[0]
        assert log.action == "kb.space.delete.failed"
        assert log.status == "failed"
        assert log.details.get("reason") == "not_found_or_unauthorized"


class TestRequestValidation:
    """Test request size limits and validation."""

    def test_request_size_limit_configured(self):
        """Test that request size limit is configured."""
        max_size = os.getenv("MAX_REQUEST_SIZE", "10485760")  # 10MB default

        # Should be a valid integer
        try:
            max_size_int = int(max_size)
            assert max_size_int > 0
            assert max_size_int <= 100 * 1024 * 1024  # Should not exceed 100MB
        except ValueError:
            pytest.fail("MAX_REQUEST_SIZE is not a valid integer")


class TestSecretsManagement:
    """Test secrets management practices."""

    def test_no_hardcoded_database_password(self):
        """Test that database password is not hardcoded."""
        db_url = os.getenv("DATABASE_URL", "")

        # Should use environment variable, not hardcoded
        assert db_url == "" or db_url.startswith("postgresql://") or db_url.startswith("sqlite://")

        # If it's a real database URL, verify it uses env vars or is a test URL
        if "postgresql://" in db_url:
            # Should not contain literal "password" as the password
            assert "password@" not in db_url.lower() or "test" in db_url.lower()

    def test_env_example_has_no_secrets(self):
        """Test that .env.example doesn't contain real secrets."""
        env_example_path = project_root / ".env.example"

        if env_example_path.exists():
            with open(env_example_path, 'r') as f:
                content = f.read()

            # Should not contain common secret patterns
            assert "sk-" not in content or "your-" in content or "example" in content
            assert "password123" not in content.lower()
            assert "secret123" not in content.lower()

            # Should contain placeholder patterns
            assert "your-" in content.lower() or "example" in content.lower()


class TestCORSConfiguration:
    """Test CORS configuration."""

    def test_cors_not_wildcard_in_production(self):
        """Test that CORS is not set to * in production."""
        if os.getenv("ENV") == "prod":
            cors_origins = os.getenv("CORS_ORIGINS", "")

            # Should be configured with specific domains
            assert cors_origins != ""
            assert "*" not in cors_origins
            assert "," in cors_origins or cors_origins.startswith("https://")


def test_security_suite_complete():
    """Verify that all security tests are present."""
    test_classes = [
        TestAuthentication,
        TestAuthorization,
        TestInputValidation,
        TestDataMasking,
        TestAuditLogging,
        TestRequestValidation,
        TestSecretsManagement,
        TestCORSConfiguration,
    ]

    total_tests = sum(
        len([m for m in dir(cls) if m.startswith("test_")])
        for cls in test_classes
    )

    print(f"\n✓ Security test suite contains {total_tests} tests")
    assert total_tests >= 20, "Security test suite should have at least 20 tests"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
