"""Collect knowledge cases under their original node names."""

from tests.features.knowledge.kb_multimodal_assets_support import fake_storage

from tests.features.knowledge.kb_multimodal_assets_cases_1 import (
    test_plain_parse_does_not_request_media,
    test_rich_parse_requests_media_and_decodes_data_uri,
    test_rich_parse_survives_undecodable_image,
    test_uploaded_image_no_longer_indexed_as_binary_garbage,
    test_pdf_assets_carry_caption_and_locator,
    test_rich_parse_failure_degrades_to_text,
    test_with_assets_false_keeps_legacy_path,
    test_asset_lands_on_the_chunk_that_contains_it,
    test_placeholder_rewritten_in_both_parent_and_child,
    test_tiny_assets_are_dropped,
    test_storage_failure_skips_one_asset_not_the_document,
    test_no_vision_model_is_a_degradation_not_a_failure,
    test_evidence_lands_in_caption_and_ocr_merges_into_text,
    test_ocr_identical_to_caption_is_not_duplicated,
    test_text_content_is_capped,
    test_single_image_failure_keeps_asset_indexable,
    test_whole_batch_exception_does_not_fail_the_document,
    test_kb_never_saturates_the_shared_vision_lane,
    test_unreadable_asset_is_skipped_without_calling_vision,
    test_per_document_cap_marks_the_rest_explicitly,
    test_index_text_prefers_caption_then_caption_text,
    test_asset_rows_point_at_their_parent_chunk,
)

from tests.features.knowledge.kb_multimodal_assets_cases_2 import (
    test_orphan_asset_falls_back_to_itself,
    test_multimodal_toggle_precedence,
    test_describe_batch_runs_on_the_host_loop_when_there_is_one,
    test_describe_batch_without_host_loop_disables_the_evidence_cache,
    test_explicit_loop_wins_over_anyio,
    test_media_only_parent_falls_back_to_the_asset_row_content,
)
