"""Collect MCP marketplace lifecycle cases with stable pytest node IDs."""
from tests.features.marketplace.mcp_marketplace_support import db

from tests.features.marketplace.mcp_marketplace_cases_1 import (
    test_http_probe_releases_its_discovery_resource,
    test_http_probe_surfaces_401_instead_of_cancel_scope_noise,
    test_ce_metadata_contains_mcp_market_auth_without_ee_dependency,
    test_header_credentials_are_encrypted_and_masked,
    test_runtime_query_credentials_are_encrypted_and_never_sent_as_headers,
    test_oauth_bundle_is_provider_neutral_and_never_sent_as_header,
    test_oauth_callback_and_status_are_shared_without_plaintext_code,
    test_oauth_start_hands_back_before_discovery_finishes,
    test_oauth_cancel_marks_flow_failed_and_wakes_worker,
    test_oauth_callback_uses_browser_origin_not_untrusted_host,
    test_oauth_callback_allows_configured_http_in_production,
    test_legacy_plaintext_headers_are_encrypted_once,
    test_remote_url_security_blocks_private_and_accepts_public_dns,
    test_remote_url_security_allows_public_http_when_requested,
    test_remote_url_security_allows_trusted_private_networks_when_enabled,
    test_remote_url_security_keeps_high_risk_ranges_blocked_when_private_enabled,
    test_self_service_mcp_uses_private_network_deployment_setting,
)

from tests.features.marketplace.mcp_marketplace_cases_2 import (
    test_remote_url_security_rechecks_tun_fake_ip_dns,
    test_remote_url_security_rechecks_ipv6_tun_fake_ip_dns,
    test_remote_url_security_does_not_treat_arbitrary_ula_as_fake_ip,
    test_remote_url_security_rejects_fake_ip_when_public_dns_is_private,
    test_tool_hash_is_order_independent,
    test_submit_snapshots_without_credentials_then_approve,
    test_public_http_marketplace_submit_review_and_install,
    test_market_installed_private_server_cannot_submit_again,
    test_submission_strips_query_secret_into_install_schema,
    test_admin_publish_links_existing_global_instance_without_duplication,
    test_marketplace_first_admin_source_is_reused_on_global_install,
    test_user_install_reuses_admin_managed_credentials_without_exposing_them,
    test_user_install_reuses_admin_managed_query_token,
)

from tests.features.marketplace.mcp_marketplace_cases_3 import (
    test_removed_admin_credential_falls_back_to_installer_input,
    test_market_edit_can_require_installer_credentials_without_reusing_source,
    test_market_edit_sets_admin_token_and_keeps_it_out_of_responses,
    test_create_market_mcp_with_blank_token_requires_each_installer,
    test_create_market_mcp_with_admin_token_makes_user_install_tokenless,
    test_create_market_mcp_with_oauth_defers_authorization_to_installer,
    test_move_existing_published_server_removes_global_installation,
    test_edit_market_metadata_propagates_to_existing_installations,
    test_install_uses_installers_own_encrypted_credentials,
)

from tests.features.marketplace.mcp_marketplace_cases_4 import (
    test_curated_templates_seed_once_and_materialize_user_query_key,
    test_curated_bearer_token_gets_prefix_and_can_be_rotated,
    test_tool_drift_pauses_new_installation,
    test_failed_update_probe_never_commits_plaintext_credentials,
    test_security_suspension_disables_all_derived_servers,
    test_review_rechecks_live_snapshot,
    test_missing_source_revalidates_from_reviewed_version,
    test_missing_source_still_detects_live_snapshot_drift,
    test_manual_admin_revalidation_accepts_drift_as_new_version,
    test_periodic_revalidation_never_auto_accepts_drift,
    test_manual_revalidation_rejects_empty_tool_snapshot,
    test_mcp_visibility_uses_shared_marketplace_scope,
    test_delisted_item_blocks_user_install_but_not_admin_install,
    test_user_installs_admin_published_private_network_mcp,
    test_installer_cannot_redirect_an_admin_item_to_another_private_host,
    test_installer_may_keep_the_admin_host_when_overriding_the_endpoint,
)
