"""Private KB hybrid retrieval must survive Milvus's combined requery failure."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from pymilvus.exceptions import MilvusException

from features.knowledge import kb_vector, vector_client


def hit(key):
    return {'id': key, 'distance': 0.9, 'entity': {
        'chunk_id': key, 'parent_chunk_id': key, 'row_type': 'chunk',
        'kb_id': 'private', 'document_id': 'doc', 'title': 'fixture',
        'content': 'fixture text', 'chunk_index': 0,
    }}


class SearchClient:
    def __init__(self, dense, sparse):
        self.dense, self.sparse = dense, sparse
        self.calls = []
        self.error_field = None
        self.delay = 0

    def hybrid_search(self, **kwargs):
        raise MilvusException(2200, 'incomplete query result: inconsistent requery result')

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs['anns_field'] == self.error_field:
            raise RuntimeError('search unavailable')
        time.sleep(self.delay)
        return [self.dense if kwargs['anns_field'] == 'dense_embedding' else self.sparse]


class HybridRetrievalTests(unittest.TestCase):
    def run_search(self, client, **kwargs):
        with patch.object(vector_client, 'get_or_create_collection'), \
             patch.object(vector_client, 'get_client', return_value=client), \
             patch.object(vector_client, 'is_lite', return_value=kwargs.pop('lite', False)):
            return kb_vector.hybrid_search('owner', kwargs.pop('private', ['private']),
                'union expenses', [1., 0.], top_k=kwargs.pop('top_k', 2), **kwargs)

    def test_production_combined_requery_failure_does_not_lose_dense_hits(self):
        rows = self.run_search(SearchClient([hit('a'), hit('b')], []))
        self.assertEqual([r['chunk_id'] for r in rows], ['a', 'b'])
        self.assertEqual(rows[0]['content'], 'fixture text')

    def test_rrf_combines_both_rankings_and_deduplicates(self):
        rows = self.run_search(SearchClient([hit('a'), hit('b')], [hit('b'), hit('c')]))
        self.assertEqual([r['chunk_id'] for r in rows], ['b', 'a', 'c'])
        self.assertAlmostEqual(rows[0]['score'], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(rows[1]['score'], 1 / 61)

    def test_sparse_only_and_empty_results(self):
        self.assertEqual(self.run_search(SearchClient([], [])), [])
        rows = self.run_search(SearchClient([], [hit('c')]))
        self.assertEqual([r['chunk_id'] for r in rows], ['c'])

    def test_identical_permission_filter_on_both_searches(self):
        client = SearchClient([hit('a')], [])
        self.run_search(client, public_kb_ids=['shared'])
        self.assertEqual(len(client.calls), 2)
        for call in client.calls:
            self.assertEqual(call['filter'],
                '(user_id == "owner" and kb_id in ["private"]) or kb_id in ["shared"]')
            self.assertEqual(call['limit'], 6)
            self.assertIn('content', call['output_fields'])
        self.assertEqual(client.calls[0]['filter'], client.calls[1]['filter'])

    def test_no_scope_never_searches(self):
        client = SearchClient([hit('a')], [])
        self.assertEqual(self.run_search(client, private=[]), [])
        self.assertEqual(client.calls, [])

    def test_errors_are_not_reported_as_empty_success(self):
        for field in ('dense_embedding', 'sparse_embedding'):
            client = SearchClient([hit('a')], [])
            client.error_field = field
            with self.assertRaisesRegex(RuntimeError, 'search unavailable'):
                self.run_search(client)

    def test_rankings_run_concurrently_under_the_callers_timeout(self):
        client = SearchClient([hit('a')], [hit('b')])
        client.delay = 0.3
        started = time.monotonic()
        rows = self.run_search(client, timeout=1)
        elapsed = time.monotonic() - started
        self.assertEqual({r['chunk_id'] for r in rows}, {'a', 'b'})
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(c['timeout'] == 1 for c in client.calls))
        self.assertLess(elapsed, 0.55, 'dense and sparse searches must overlap')

    def test_lite_stays_dense_only(self):
        client = SearchClient([hit('a')], [])
        self.assertEqual(len(self.run_search(client, lite=True, timeout=6)), 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]['timeout'], 6)

    def test_result_limit_and_no_implicit_timeout(self):
        client = SearchClient([hit(str(i)) for i in range(9)], [])
        self.assertEqual(len(self.run_search(client, top_k=2)), 4)
        self.assertTrue(all(c['timeout'] is None for c in client.calls))


if __name__ == '__main__':
    unittest.main()
