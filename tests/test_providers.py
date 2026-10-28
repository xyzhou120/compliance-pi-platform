import tempfile
import threading
import time
import unittest

from apps.control_api.providers import ProviderPool, recognize_pages


class ProviderTests(unittest.TestCase):
    def test_external_provider_calls_are_bounded_and_candidates_remain_candidates(self):
        active = 0
        peak = 0
        def transport(_url, payload, _timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            active -= 1
            return {"text": f"page {payload['page']}", "blocks": []}
        result = recognize_pages([{"file_id": "f1", "page": 1, "image_bytes": b"a"}, {"file_id": "f1", "page": 2, "image_bytes": b"b"}], settings={"endpoint": "http://test", "max_concurrency": 1}, transport=transport)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["candidates"][0]["quality"], "candidate")
        self.assertLessEqual(peak, 1)

    def test_shared_pool_bounds_simultaneous_batches_and_cache_reuses_success(self):
        lock = threading.Lock()
        active, peak, calls = 0, 0, 0
        def transport(_url, _payload, _timeout):
            nonlocal active, peak, calls
            with lock:
                active += 1
                peak = max(peak, active)
                calls += 1
            time.sleep(0.01)
            with lock:
                active -= 1
            return {"text": "可见原文", "blocks": []}
        pool = ProviderPool(max_concurrency=2)
        with tempfile.TemporaryDirectory() as tmp:
            settings = {"endpoint": "http://test", "cache_dir": tmp}
            pages = [{"file_id": "f1", "page": n, "image_bytes": str(n).encode()} for n in range(4)]
            threads = [threading.Thread(target=recognize_pages, args=(pages[:2],), kwargs={"settings": settings, "pool": pool, "transport": transport}),
                       threading.Thread(target=recognize_pages, args=(pages[2:],), kwargs={"settings": settings, "pool": pool, "transport": transport})]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(calls, 4)
            recognize_pages(pages, settings=settings, pool=pool, transport=transport)
            self.assertEqual(calls, 4)
            self.assertEqual(peak, 2)

    def test_paddle_lines_are_normalized_as_candidates(self):
        result = recognize_pages([{"file_id": "f", "page": 1, "image_bytes": b"png"}], settings={"endpoint": "http://test"},
                                 transport=lambda *_: {"rec_texts": ["工作面401", "已撤人"], "rec_polys": [[[0, 0], [1, 0], [1, 1], [0, 1]]]})
        self.assertEqual(result["candidates"][0]["text"], "工作面401\n已撤人")
        self.assertEqual(result["candidates"][0]["quality"], "candidate")

    def test_vision_json_is_not_a_verdict(self):
        result = recognize_pages([{"file_id": "f", "page": 1, "image_bytes": b"png"}], settings={"endpoint": "http://test", "provider": "openai-compatible", "model": "local-vision"},
                                 transport=lambda *_: {"choices": [{"message": {"content": '{"text":"可见原文","blocks":[]}'}}]})
        self.assertEqual(result["candidates"][0]["text"], "可见原文")
        self.assertEqual(result["candidates"][0]["quality"], "candidate")


if __name__ == "__main__":
    unittest.main()
