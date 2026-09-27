"""托育医育质量闭环的基础服务入口。"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from quality import (
    AuthError,
    ConsentError,
    NotFoundError,
    QualityError,
    QualitySystem,
)

SERVICE_ID = "childcare-health-quality"
SERVICE_NAME = "托育医育质量闭环"

# 默认内存系统；使用 --data 可落盘，换班/重启后未完成事项不丢失。
default_system = QualitySystem()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def make_handler(system):
    """构造绑定指定领域系统（如持久化实例）的 Handler 类。"""

    class Handler(HandlerBase):
        pass

    Handler.system = system
    return Handler


class HandlerBase(BaseHTTPRequestHandler):
    """提供健康检查与医育闭环接口。"""

    system = None

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, health_payload())
            return
        try:
            self._route_get()
        except QualityError as error:
            self._write_json(error.status, {"error": str(error)})
        except TypeError as error:
            self._write_json(400, {"error": f"请求参数有误：{error}"})

    def do_POST(self):
        try:
            self._route_post()
        except QualityError as error:
            self._write_json(error.status, {"error": str(error)})
        except TypeError as error:
            self._write_json(400, {"error": f"请求参数有误：{error}"})

    # ---- GET 路由（分角色视图）----

    def _not_found(self):
        self._write_json(404, {"error": "接口不存在"})

    def _route_get(self):
        system = self._system()
        match = re.fullmatch(r"/v1/parents/([^/]+)/children/([^/]+)", self.path)
        if match:
            self._write_json(200, system.parent_view(match.group(1), match.group(2)))
            return
        if self.path == "/v1/regulator/quality":
            self._write_json(200, system.regulator_quality_view())
            return
        if self.path == "/v1/pending":
            self._write_json(200, system.pending_work())
            return
        match = re.fullmatch(r"/v1/incidents/([^/]+)/trace", self.path)
        if match:
            self._write_json(200, system.manager_trace(match.group(1)))
            return
        self._not_found()

    # ---- POST 路由 ----

    def _route_post(self):
        system = self._system()
        payload = self._read_json()
        path = self.path

        if path == "/v1/children":
            self._write_json(201, system.register_child(**payload))
            return
        if path == "/v1/staff":
            self._write_json(201, system.register_staff(**payload))
            return
        if path == "/v1/consents":
            self._write_json(201, system.grant_consent(**payload))
            return
        if path == "/v1/incidents/report":
            incident, merged = system.report_or_merge(**payload)
            self._write_json(201 if not merged else 200,
                             {"incident": incident, "merged": merged})
            return

        match = re.fullmatch(r"/v1/incidents/([^/]+)/(.+)", path)
        if match:
            incident_id, action = match.group(1), match.group(2)
            self._route_incident_action(system, incident_id, action, payload)
            return

        match = re.fullmatch(r"/v1/improvements/([^/]+)/resolve", path)
        if match:
            self._write_json(200, system.resolve_improvement(match.group(1), **payload))
            return

        self._not_found()

    def _route_incident_action(self, system, incident_id, action, payload):
        routes = {
            "observations": lambda: system.add_observation(incident_id, **payload),
            "screenings": lambda: system.record_screening(incident_id, **payload),
            "medical": lambda: system.record_medical(incident_id, **payload),
            "advice": lambda: system.add_advice(incident_id, **payload),
            "escalate": lambda: system.escalate(incident_id, **payload),
            "reviews": lambda: system.review(incident_id, **payload),
            "referral": lambda: system.refer(incident_id, **payload),
            "handover": lambda: system.handover(incident_id, **payload),
            "followups": lambda: system.add_followup(incident_id, **payload),
            "close": lambda: system.close_incident(incident_id, **payload),
            "improvements": lambda: system.add_improvement(incident_id, **payload),
        }
        handler = routes.get(action)
        if handler is None:
            self._not_found()
            return
        self._write_json(201, handler())

    # ---- 工具 ----

    def _system(self):
        return self.system if self.system is not None else default_system

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as error:
            raise QualityError(f"请求体不是合法 JSON：{error}")

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


# 向后兼容：既有测试直接引用 Handler。
Handler = make_handler(default_system)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", help="事件数据落盘路径（JSON），省略则仅内存运行")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    system = QualitySystem(args.data) if args.data else default_system
    handler = make_handler(system)
    ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
