"""Opt-in integration test against LOCAL Milvus; never targets production."""
from __future__ import annotations

import os
import unittest
import uuid
from urllib.parse import urlsplit
from unittest.mock import patch

from features.knowledge import kb_vector, vector_client


@unittest.skipUnless(os.getenv('TEST_LOCAL_MILVUS') == '1', 'requires local Milvus opt-in')
class LocalMilvusRetrievalTests(unittest.TestCase):
    def setUp(self):
        from pymilvus import DataType

        host = urlsplit(vector_client.milvus_uri()).hostname
        if host not in {'milvus', '127.0.0.1', 'localhost'}:
            self.fail('Refusing a non-local Milvus endpoint')
        self.client = vector_client.get_client()
        self.collection = 'kb_retrieval_test_' + uuid.uuid4().hex
        self.addCleanup(self.cleanup_collection)
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=True)
        for key in ('chunk_id', 'user_id', 'kb_id'):
            schema.add_field(key, DataType.VARCHAR, max_length=64, is_primary=key == 'chunk_id')
        schema.add_field('dense_embedding', DataType.FLOAT_VECTOR, dim=4)
        schema.add_field('sparse_embedding', DataType.SPARSE_FLOAT_VECTOR)
        indexes = self.client.prepare_index_params()
        indexes.add_index('dense_embedding', index_type='FLAT', metric_type='IP')
        indexes.add_index('sparse_embedding', index_type='SPARSE_INVERTED_INDEX', metric_type='IP')
        self.client.create_collection(self.collection, schema=schema, index_params=indexes,
                                      consistency_level='Strong')
        query_sparse = kb_vector.text_to_sparse('union expenses')
        unrelated_sparse = kb_vector.text_to_sparse('unrelated fixture')
        self.client.insert(self.collection, [
            {'chunk_id': str(i), 'user_id': 'owner' if i < 4 else 'other',
             'kb_id': 'shared' if i == 6 else 'private',
             'dense_embedding': [1., 0., 0., 0.],
             'sparse_embedding': query_sparse if i in (1, 5, 6) else unrelated_sparse,
             'title': 'fixture', 'content': 'fixture text', 'row_type': 'chunk',
             'parent_chunk_id': str(i), 'document_id': 'doc', 'chunk_index': i}
            for i in range(7)
        ])

    def cleanup_collection(self):
        if self.client.has_collection(self.collection):
            self.client.drop_collection(self.collection)

    def search(self, query, public=None):
        with patch.object(vector_client, 'get_or_create_collection'), \
             patch.object(vector_client, 'is_lite', return_value=False), \
             patch.object(kb_vector.kb_vector_state, 'COLLECTION_NAME', self.collection):
            return kb_vector.hybrid_search('owner', ['private'], query, [1., 0., 0., 0.],
                top_k=3, public_kb_ids=public, timeout=10)

    def test_no_sparse_hits_still_returns_authorized_dense_payload(self):
        rows = self.search('nevermatch20261110')
        self.assertEqual({r['chunk_id'] for r in rows}, {'0', '1', '2', '3'})
        self.assertTrue(all(r['content'] == 'fixture text' for r in rows))

    def test_both_rankings_include_shared_but_exclude_other_owner(self):
        rows = self.search('union expenses', public=['shared'])
        self.assertEqual({r['chunk_id'] for r in rows}, {'0', '1', '2', '3', '6'})
        self.assertEqual({r['chunk_id'] for r in rows[:2]}, {'1', '6'})
        self.assertTrue(all(r['score'] > 1 / 61 for r in rows[:2]))


if __name__ == '__main__':
    unittest.main()
