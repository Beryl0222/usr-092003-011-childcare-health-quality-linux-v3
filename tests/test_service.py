"""验证基础服务身份、路由行为与医育闭环 HTTP 契约。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from quality import (
    ALL_PURPOSES,
    QualitySystem,
)
from service import Handler, SERVICE_ID, health_payload, make_handler


def post(base_url, path, payload):
    request = Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        body = json.load(error)
        error.close()
        return error.code, body


def get(base_url, path):
    try:
        with urlopen(f"{base_url}{path}", timeout=2) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        body = json.load(error)
        error.close()
        return error.code, body


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_payload(self):
        self.assertEqual(health_payload(), {"status": "ok", "service": SERVICE_ID, "name": "托育医育质量闭环"})

    def test_health_route(self):
        status, body = get(self.base_url, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_unknown_route(self):
        status, _ = get(self.base_url, "/unknown")
        self.assertEqual(status, 404)


class ClosedLoopHttpTest(unittest.TestCase):
    def setUp(self):
        self.system = QualitySystem()
        handler = make_handler(self.system)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _seed(self):
        post(self.base_url, "/v1/children",
             {"child_id": "c1", "name": "小明",
              "guardian_id": "g1", "guardian_name": "妈妈"})
        post(self.base_url, "/v1/staff",
             {"staff_id": "care", "name": "王保育", "role": "caregiver"})
        post(self.base_url, "/v1/staff",
             {"staff_id": "doc", "name": "陈医生", "role": "doctor"})
        post(self.base_url, "/v1/consents",
             {"child_id": "c1", "guardian_id": "g1", "purposes": list(ALL_PURPOSES)})

    def test_full_loop_over_http(self):
        self._seed()
        status, body = post(self.base_url, "/v1/incidents/report",
                            {"child_id": "c1", "staff_id": "care",
                             "summary": "发热", "content": "38度"})
        self.assertEqual(status, 201)
        iid = body["incident"]["id"]

        # 重复上报合并
        status, body = post(self.base_url, "/v1/incidents/report",
                            {"child_id": "c1", "staff_id": "care",
                             "summary": "发热", "content": "仍发热"})
        self.assertEqual(status, 200)
        self.assertTrue(body["merged"])
        self.assertEqual(body["incident"]["id"], iid)

        # 保育身份越权开医疗判断 -> 403
        status, body = post(self.base_url, f"/v1/incidents/{iid}/medical",
                            {"staff_id": "care", "kind": "medication",
                             "conclusion": "用药", "medication": "退烧药"})
        self.assertEqual(status, 403)

        # 医生签署
        status, body = post(self.base_url, f"/v1/incidents/{iid}/medical",
                            {"staff_id": "doc", "kind": "medical_judgement",
                             "conclusion": "普通感冒", "advice_summary": "建议就诊"})
        self.assertEqual(status, 201)
        self.assertEqual(body["signed_by"], "doc")

        # 关闭
        status, body = post(self.base_url, f"/v1/incidents/{iid}/close",
                            {"staff_id": "doc", "basis": "医生结论+回访"})
        self.assertEqual(status, 201)
        self.assertEqual(body["by"], "doc")

        # 家长视图标注意见来源
        status, view = get(self.base_url, "/v1/parents/g1/children/c1")
        self.assertEqual(status, 200)
        advice = view["incidents"][0]["advice"][0]
        self.assertEqual(advice["label"], "需要就医")
        self.assertEqual(advice["source_title"], "授权医生")

        # 监管视图脱敏
        status, quality = get(self.base_url, "/v1/regulator/quality")
        self.assertEqual(status, 200)
        self.assertNotIn("小明", json.dumps(quality, ensure_ascii=False))
        self.assertEqual(quality["incident_counts"]["closed"], 1)

    def test_consent_missing_returns_403(self):
        post(self.base_url, "/v1/children",
             {"child_id": "c2", "name": "小红",
              "guardian_id": "g2", "guardian_name": "爸爸"})
        post(self.base_url, "/v1/staff",
             {"staff_id": "care", "name": "王保育", "role": "caregiver"})
        status, body = post(self.base_url, "/v1/incidents/report",
                            {"child_id": "c2", "staff_id": "care",
                             "summary": "咳嗽", "content": "干咳"})
        self.assertEqual(status, 403)
        self.assertIn("用途", body["error"])


if __name__ == "__main__":
    unittest.main()
