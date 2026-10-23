"""Collect knowledge cases under their original node names."""

from tests.features.knowledge.kb_wiki_end_to_end_support import wiki_db, stub_llm

from tests.features.knowledge.kb_wiki_end_to_end_cases_1 import (
    test_ingest_creates_pages_with_lineage,
    test_ingest_links_pages_to_each_other,
    test_finalize_builds_index_and_backlinks,
    test_finalize_strips_dead_links,
    test_retract_removes_document_lineage,
    test_read_surface_matches_expected_shape,
    test_source_chunks_resolve_by_id,
    test_source_chunks_fall_back_when_refs_are_stale,
    test_search_ranks_title_hits_above_body,
    test_search_supports_alternation,
    test_folders_report_recursive_page_counts,
    test_graph_ego_mode_centres_on_node,
    test_graph_ego_on_missing_node_is_empty,
    test_ingest_is_idempotent_on_rerun,
    test_concurrent_stages_never_touch_the_session,
    test_quota_stops_generation_without_failing,
)

from tests.features.knowledge.kb_wiki_end_to_end_cases_2 import (
    test_custom_instructions_reach_every_stage,
    test_worker_batches_sibling_ingest_jobs,
    test_batching_never_crosses_knowledge_bases,
    test_one_knowledge_base_runs_one_batch_at_a_time,
    test_a_busy_knowledge_base_does_not_block_other_ones,
    test_a_dead_workers_knowledge_base_is_taken_over,
)
