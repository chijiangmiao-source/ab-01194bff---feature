"""HTTP API 冒烟与语义测试（启动真实端口）。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import Counter

from app.server import create_server, serve_forever


def request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "api.db")
        self.httpd, self.store = create_server(
            host="127.0.0.1", port=0, db_path=self.db, partition_count=6
        )
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=serve_forever, args=(self.httpd,))
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.thread.join()
        self.httpd.server_close()
        self.store.close()
        self.tmp.cleanup()

    def test_health(self) -> None:
        code, body = request("GET", f"{self.base}/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_handover_flow_over_http(self) -> None:
        code, body = request(
            "POST", f"{self.base}/v1/snapshots",
            {"request_id": "r1", "members": ["a", "b"]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "stable")

        code, body = request(
            "POST", f"{self.base}/v1/snapshots",
            {"request_id": "r2", "members": ["b", "c"]},
        )
        self.assertEqual(code, 202)
        self.assertEqual(len(body["revocations"]), 6)

        code, body = request(
            "POST", f"{self.base}/v1/confirms",
            {"request_id": "c1", "member": "a", "parts": ["0", "2", "4"]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "partially_released")

        # 重传返回首次结果
        code, replay = request(
            "POST", f"{self.base}/v1/confirms",
            {"request_id": "c1", "member": "a", "parts": ["0", "2", "4"]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(replay, body)

        code, body = request(
            "POST", f"{self.base}/v1/confirms",
            {"request_id": "c2", "member": "b", "parts": ["1", "3", "5"]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "completed")

        code, body = request("GET", f"{self.base}/v1/assignments")
        self.assertEqual(code, 200)
        self.assertEqual(body["epoch"], 2)
        owners = Counter(v["owner"] for v in body["assignments"].values())
        self.assertEqual(owners, Counter({"b": 3, "c": 3}))

        code, body = request("GET", f"{self.base}/v1/assignments?member=c")
        self.assertEqual(code, 200)
        self.assertEqual(set(body["assignments"]), {"1", "3", "5"})

    def test_invalid_confirmations_are_rejected(self) -> None:
        request("POST", f"{self.base}/v1/snapshots",
                {"request_id": "r1", "members": ["a", "b"]})
        request("POST", f"{self.base}/v1/snapshots",
                {"request_id": "r2", "members": ["b", "c"]})

        code, body = request("POST", f"{self.base}/v1/confirms",
                             {"request_id": "x1", "member": "c", "parts": ["0"]})
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "not_owner")

        code, body = request("POST", f"{self.base}/v1/confirms",
                             {"request_id": "x2", "member": "a", "parts": ["9"]})
        self.assertEqual(code, 400)

        code, body = request("POST", f"{self.base}/v1/snapshots",
                             {"request_id": "r1", "members": ["a", "c"]})
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "idempotency_conflict")

    def test_ownership_chain_over_http(self) -> None:
        # 完成一次真实交接
        request("POST", f"{self.base}/v1/snapshots",
                {"request_id": "r1", "members": ["a", "b"]})
        request("POST", f"{self.base}/v1/snapshots",
                {"request_id": "r2", "members": ["b", "c"]})
        code, body = request(
            "POST", f"{self.base}/v1/confirms",
            {"request_id": "c1", "member": "a", "parts": ["0", "2", "4"]},
        )
        self.assertEqual(code, 200)

        code, body = request("GET", f"{self.base}/v1/ownership/chain?part=0")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["part"], "0")
        self.assertTrue(body["history"])
        self.assertEqual(
            [(e["seq"], e["from_owner"], e["to_owner"], e["epoch"], e["releaser"])
             for e in body["chain"]],
            [(0, None, "a", 0, "baseline"), (1, "a", "b", 1, "a")],
        )
        # 末项与当前分配读模型一致
        self.assertEqual(body["current_owner"], "b")
        self.assertTrue(body["matches"])

        # 拒绝/重放不增加记录：重传 c1 后链路不变
        request("POST", f"{self.base}/v1/confirms",
                {"request_id": "c1", "member": "a", "parts": ["0", "2", "4"]})
        code, replay = request("GET", f"{self.base}/v1/ownership/chain?part=0")
        self.assertEqual(code, 200)
        self.assertEqual(replay["chain"], body["chain"])

        # 未知分区 -> 404 明确结果
        code, body = request("GET", f"{self.base}/v1/ownership/chain?part=99")
        self.assertEqual(code, 404)
        self.assertEqual(body["error"], "unknown_partition")

        # 缺少 part 参数 -> 400
        code, body = request("GET", f"{self.base}/v1/ownership/chain")
        self.assertEqual(code, 400)

    def test_ownership_chain_completes_second_handover(self) -> None:
        request("POST", f"{self.base}/v1/snapshots",
                {"request_id": "r1", "members": ["a", "b"]})
        request("POST", f"{self.base}/v1/snapshots",
                {"request_id": "r2", "members": ["b", "c"]})
        request("POST", f"{self.base}/v1/confirms",
                {"request_id": "c1", "member": "a", "parts": ["0", "2", "4"]})
        request("POST", f"{self.base}/v1/confirms",
                {"request_id": "c2", "member": "b", "parts": ["1", "3", "5"]})
        request("POST", f"{self.base}/v1/snapshots",
                {"request_id": "r3", "members": ["c", "d"]})
        request("POST", f"{self.base}/v1/confirms",
                {"request_id": "c3", "member": "b", "parts": ["0", "2", "4"]})
        request("POST", f"{self.base}/v1/confirms",
                {"request_id": "c4", "member": "c", "parts": ["1", "3", "5"]})
        code, body = request("GET", f"{self.base}/v1/ownership/chain?part=5")
        self.assertEqual(code, 200)
        self.assertEqual(
            [(e["from_owner"], e["to_owner"], e["releaser"])
             for e in body["chain"]],
            [(None, "b", "baseline"), ("b", "c", "b"), ("c", "d", "c")],
        )
        self.assertEqual(body["current_owner"], "d")

    def test_bad_json_and_missing_fields(self) -> None:
        req = urllib.request.Request(
            f"{self.base}/v1/snapshots",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

        code, body = request("POST", f"{self.base}/v1/snapshots",
                             {"request_id": "", "members": []})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
