"""验证业务接口的 HTTP 行为（在内存实例上注入测试应用）。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from careloop import CareLoop


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service.set_app(CareLoop())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        service.set_app(None)

    def post(self, path, payload):
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def post_error(self, path, payload):
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        body = json.loads(error.exception.read())
        error.exception.close()
        return error.exception.code, body

    def test_full_loop_over_http(self):
        status, caregiver = self.post("/staff", {"name": "王保育", "role": "caregiver"})
        self.assertEqual(status, 200)
        _, doctor = self.post("/staff", {"name": "赵医生", "role": "doctor", "authorized": True})
        _, child = self.post("/children", {"name": "小明", "guardian_ids": ["g-1"]})

        _, obs = self.post("/observations", {
            "staff_id": caregiver["id"], "child_id": child["id"],
            "symptom": "发热", "detail": "38.5℃", "abnormal": True,
        })
        event_id = obs["event_id"]
        self.assertTrue(event_id)

        _, event = self.post("/medical-conclusions", {
            "staff_id": doctor["id"], "event_id": event_id,
            "conclusion": "上呼吸道感染", "signature": "赵医生",
        })
        self.assertEqual(event["status"], "medical")

        _, event = self.post("/events/close", {
            "staff_id": doctor["id"], "event_id": event_id, "basis": "体温恢复正常",
        })
        self.assertEqual(event["status"], "closed")

        with urlopen(f"{self.base_url}/parent/child?guardian_id=g-1&child_id={child['id']}",
                     timeout=2) as response:
            view = json.load(response)
        self.assertTrue(any(n["kind"] == "closure" for n in view["notices"]))

        with urlopen(f"{self.base_url}/manager/event?event_id={event_id}", timeout=2) as response:
            trace = json.load(response)
        self.assertEqual(trace["event"]["medical"]["signed_by"], "赵医生")

        with urlopen(f"{self.base_url}/regulator/quality", timeout=2) as response:
            stats = json.load(response)
        self.assertEqual(stats["events"]["closed"], 1)

    def test_permission_error_maps_to_403(self):
        _, caregiver = self.post("/staff", {"name": "保育", "role": "caregiver"})
        _, child = self.post("/children", {"name": "童", "guardian_ids": ["g-9"]})
        code, body = self.post_error("/medications", {
            "staff_id": caregiver["id"], "child_id": child["id"],
            "description": "退烧药", "signature": "x",
        })
        self.assertEqual(code, 403)
        self.assertIn("获授权医生", body["error"])

    def test_missing_field_maps_to_400(self):
        code, body = self.post_error("/staff", {"role": "caregiver"})
        self.assertEqual(code, 400)
        self.assertIn("name", body["error"])

    def test_unknown_child_maps_to_404(self):
        _, caregiver = self.post("/staff", {"name": "保育甲", "role": "caregiver"})
        code, _ = self.post_error("/observations", {
            "staff_id": caregiver["id"], "child_id": "child-404",
            "symptom": "咳嗽", "detail": "轻微",
        })
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
