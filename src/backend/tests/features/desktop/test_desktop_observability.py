"""Collect desktop observability scenarios with stable pytest names."""
from tests.features.desktop.observability_support import store, local_store

from tests.features.desktop.observability_cases_1 import (
    test_duplicate_and_stale_delivery_keep_newest_chat,
    test_capture_upload_and_conditional_ack_survive_retry,
    test_cloud_gateway_and_desktop_receipt_are_one_call,
    test_gateway_stream_arrives_before_log_writer_and_extracts_usage,
    test_real_upload_endpoint_authorization_and_size_limit,
    test_offline_restart_and_http_delivery_to_cloud,
    test_unicode_large_records_and_secrets_are_safe,
)

from tests.features.desktop.observability_cases_2 import (
    test_gateway_unknown_usage_keeps_desktop_usage_and_parent,
    test_bulk_updates_are_uploaded_and_rollback_is_not,
    test_account_switch_during_upload_does_not_ack_old_account,
    test_blocked_upload_does_not_block_task_progress,
    test_capture_identity_is_stable_within_transaction_and_refreshes_on_reuse,
)
