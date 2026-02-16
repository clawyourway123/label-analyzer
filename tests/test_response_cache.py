"""Tests for ResponseCache."""

import json
import time
import pytest
from pathlib import Path

# Add parent to path so we can import the module
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from label_analyzer_production import ResponseCache


@pytest.fixture
def cache(tmp_path):
    return ResponseCache(cache_dir=str(tmp_path), ttl_seconds=60)


SAMPLE_IMAGE = {"inline_data": {"mime_type": "image/jpeg", "data": "abc123"}}
SAMPLE_PROMPT = "Identify regions"
SAMPLE_SCHEMA = {"type": "object", "properties": {"regions": {"type": "array"}}}


class TestResponseCache:

    def test_miss_then_hit(self, cache):
        assert cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT) is None
        cache.put(SAMPLE_IMAGE, SAMPLE_PROMPT, '{"regions": []}')
        assert cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT) == '{"regions": []}'
        assert cache.hits == 1
        assert cache.misses == 1

    def test_schema_varies_key(self, cache):
        cache.put(SAMPLE_IMAGE, SAMPLE_PROMPT, "no_schema")
        cache.put(SAMPLE_IMAGE, SAMPLE_PROMPT, "with_schema", schema=SAMPLE_SCHEMA)
        assert cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT) == "no_schema"
        assert cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT, schema=SAMPLE_SCHEMA) == "with_schema"

    def test_ttl_expiry(self, tmp_path):
        cache = ResponseCache(cache_dir=str(tmp_path), ttl_seconds=1)
        cache.put(SAMPLE_IMAGE, SAMPLE_PROMPT, "old_data")
        assert cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT) == "old_data"
        time.sleep(1.1)
        assert cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT) is None

    def test_disabled_cache(self, cache):
        cache.disable()
        cache.put(SAMPLE_IMAGE, SAMPLE_PROMPT, "data")
        assert cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT) is None

    def test_clear(self, cache):
        cache.put(SAMPLE_IMAGE, SAMPLE_PROMPT, "a")
        cache.put(SAMPLE_IMAGE, "other prompt", "b")
        removed = cache.clear()
        assert removed == 2
        assert cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT) is None

    def test_stats(self, cache):
        cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT)  # miss
        cache.put(SAMPLE_IMAGE, SAMPLE_PROMPT, "data")
        cache.get(SAMPLE_IMAGE, SAMPLE_PROMPT)  # hit
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5

    def test_deterministic_key(self, cache):
        k1 = ResponseCache._make_key(SAMPLE_IMAGE, SAMPLE_PROMPT)
        k2 = ResponseCache._make_key(SAMPLE_IMAGE, SAMPLE_PROMPT)
        assert k1 == k2
        k3 = ResponseCache._make_key(SAMPLE_IMAGE, "different prompt")
        assert k1 != k3
