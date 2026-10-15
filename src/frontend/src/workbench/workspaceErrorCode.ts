import {ApiResponseError} from '../utils/apiError';
import {WorkbenchSourceError} from './sourceRequest';

// Public Workbench failures only. Arbitrary HTTP messages/payloads stay in the host.
const HTTP_CODES=new Set([
 'activity_grant_quota_exhausted', 'activity_rate_limited', 'app_disabled', 'app_not_found',
 'app_view_not_found', 'application_exists', 'approval_invalid', 'audience_required',
 'auth_configuration_required', 'background_expired', 'background_not_found',
 'background_quota_exhausted', 'candidate_storage_quota_exceeded', 'capability_denied',
 'capability_limit', 'capability_not_declared', 'capability_not_granted',
 'capability_not_requested', 'capability_timeout', 'client_secret_required',
 'configuration_declaration_invalid', 'configuration_encryption_unavailable',
 'configuration_field_unknown', 'configuration_fields_conflict', 'configuration_not_declared',
 'configuration_session_required', 'configuration_too_large', 'configuration_values_required',
 'conflicting_policy', 'data_source_denied', 'data_source_not_found', 'diagnostic_not_found',
 'file_not_found', 'file_not_selected', 'file_selection_limit', 'file_size_invalid',
 'file_too_large', 'host_cookie_scope_overlap', 'host_incompatible', 'host_origin_required',
 'host_session_required', 'identity_provider_required', 'immutable_application_id',
 'installation_not_ready', 'instance_binding_mismatch', 'instance_closed', 'instance_expired',
 'instance_not_found', 'instance_quota_exceeded', 'instance_quota_exhausted', 'invalid_application',
 'invalid_auth_destination', 'invalid_auth_request', 'invalid_config_shape', 'invalid_declaration',
 'invalid_external_url', 'invalid_filename', 'invalid_instance_id', 'invalid_json',
 'invalid_launch_intent', 'invalid_launch_ticket', 'invalid_legacy_app_config',
 'invalid_legacy_navigation', 'invalid_list', 'invalid_login_challenge',
 'invalid_navigation_preferences', 'invalid_notification_content', 'invalid_origin',
 'invalid_policy_application', 'invalid_request', 'invalid_state', 'invalid_target_binding',
 'invalid_target_credentials', 'invalid_url', 'json_required', 'launch_failed',
 'launch_identity_mismatch', 'legacy_application_id', 'module_changed', 'module_not_declared',
 'module_unavailable', 'notification_quota_exhausted', 'origin_identity_invalid', 'origin_mismatch',
 'origin_unavailable', 'package_asset_invalid', 'package_asset_missing', 'package_css_not_bundled',
 'package_revision_invalid', 'package_script_not_bundled', 'page_too_large', 'permission_denied',
 'permission_revoked', 'plugin_upgrade_not_found', 'probe_busy', 'probe_failed',
 'probe_target_denied', 'request_too_complex', 'request_too_large', 'resource_binding_mismatch',
 'resource_module_mismatch', 'resource_owner_mismatch', 'response_too_large', 'revision_conflict',
 'revision_expired', 'session_revoked', 'state_contains_credentials', 'state_quota_exceeded',
 'state_reset_confirmation_required', 'state_reset_required', 'state_schema_mismatch',
 'unknown_or_duplicate_reference', 'unmapped_permission_preserved', 'unsupported_capability',
 'unsupported_external_field', 'unsupported_external_target', 'unsupported_policy_placement',
 'unsupported_reference_origin', 'upgrade_not_pending', 'user_not_found',
 'workbench_application_id',
]);
const SOURCE_CODES=new Set(['origin_mismatch','execution_plane_not_ready','source_not_supported','origin_unavailable']);
export function workspaceErrorCode(error:unknown):string {
 if(error instanceof WorkbenchSourceError)return SOURCE_CODES.has(error.code)?error.code:'bridge_error';
 if(error instanceof ApiResponseError){
  const payload=error.payload;
  const detail=payload && typeof payload==='object' && !Array.isArray(payload)
   ? (payload as {detail?:unknown}).detail : undefined;
  const code=detail && typeof detail==='object' && !Array.isArray(detail)
   ? (detail as {code?:unknown}).code : undefined;
  return typeof code==='string' && HTTP_CODES.has(code)?code:'bridge_error';
 }
 return error instanceof Error && /^[a-z_]{1,64}$/.test(error.message)?error.message:'bridge_error';
}
