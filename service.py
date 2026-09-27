"""托育医育质量闭环的服务入口。

提供健康检查与医育闭环业务接口；业务规则见 careloop.py。
"""

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from careloop import CareLoop, CareLoopError, JsonStore

SERVICE_ID = "childcare-health-quality"
SERVICE_NAME = "托育医育质量闭环"

DEFAULT_DATA_PATH = os.environ.get("CARELOOP_DATA", "careloop-data.json")

_APP = None
_APP_LOCK = threading.Lock()


def get_app():
    """进程内唯一的领域服务实例（懒加载，便于测试注入）。"""
    global _APP
    if _APP is None:
        with _APP_LOCK:
            if _APP is None:
                _APP = CareLoop(store=JsonStore(DEFAULT_DATA_PATH))
    return _APP


def set_app(app):
    """测试或启动脚本可注入自定义实例。"""
    global _APP
    _APP = app


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# POST 路由：路径 -> (CareLoop 方法名, 需要从 JSON body 提取的参数)
POST_ROUTES = {
    "/staff": ("register_staff", ("name", "role", "qualified", "authorized", "staff_id")),
    "/children": ("enroll_child", ("name", "guardian_ids", "consents", "child_id")),
    "/consents/grant": ("grant_consent", ("child_id", "purpose")),
    "/consents/revoke": ("revoke_consent", ("child_id", "purpose")),
    "/observations": ("record_observation",
                      ("staff_id", "child_id", "symptom", "detail", "abnormal", "observed_at")),
    "/screenings": ("organize_screening", ("staff_id", "child_id", "item", "result", "summary")),
    "/opinions": ("add_opinion", ("staff_id", "child_id", "kind", "content", "event_id", "signature")),
    "/escalations": ("escalate_event",
                     ("staff_id", "event_id", "reason", "refer_to", "review_due_at", "handover_to")),
    "/medical-conclusions": ("conclude_medical", ("staff_id", "event_id", "conclusion", "signature")),
    "/medications": ("record_medication",
                     ("staff_id", "child_id", "description", "signature", "event_id")),
    "/followups": ("schedule_followup", ("staff_id", "child_id", "content", "due_at", "event_id")),
    "/followups/complete": ("complete_followup", ("staff_id", "followup_id", "result")),
    "/improvements": ("add_improvement", ("staff_id", "child_id", "content", "event_id", "due_at")),
    "/improvements/complete": ("complete_improvement", ("staff_id", "improvement_id", "note")),
    "/handovers": ("shift_handover", ("from_staff_id", "to_staff_id", "note")),
    "/events/claim": ("claim_event", ("staff_id", "event_id")),
    "/reviews/complete": ("complete_review", ("staff_id", "event_id", "note")),
    "/referrals/complete": ("complete_referral", ("staff_id", "event_id", "note")),
    "/events/close": ("close_event", ("staff_id", "event_id", "basis")),
}


def _query_params(path):
    if "?" not in path:
        return path, {}
    clean, _, raw = path.partition("?")
    params = {}
    for pair in raw.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        params[key] = value
    return clean, params


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 医育闭环业务接口。"""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message):
        self._send_json(status, {"error": message})

    def do_GET(self):
        path, params = _query_params(self.path)
        try:
            if path == "/health":
                self._send_json(200, health_payload())
            elif path == "/parent/child":
                self._send_json(200, get_app().parent_view(
                    params.get("guardian_id", ""), params.get("child_id", "")))
            elif path == "/regulator/quality":
                self._send_json(200, get_app().regulator_view())
            elif path == "/manager/event":
                self._send_json(200, get_app().trace_event(params.get("event_id", "")))
            elif path == "/manager/pending":
                self._send_json(200, {"items": get_app().pending_items(params.get("owner_id") or None)})
            elif path == "/manager/improvements/overdue":
                min_days = int(params.get("min_days", "30"))
                self._send_json(200, {"items": get_app().overdue_improvements(min_days)})
            elif path == "/journal":
                self._send_json(200, {"items": get_app().journal(params.get("child_id", ""))})
            else:
                self._send_error_json(404, "接口不存在")
        except CareLoopError as error:
            self._send_error_json(error.status, str(error))
        except (ValueError, KeyError) as error:
            self._send_error_json(400, f"请求参数有误: {error}")

    def do_POST(self):
        path, _ = _query_params(self.path)
        route = POST_ROUTES.get(path)
        if route is None:
            self._send_error_json(404, "接口不存在")
            return
        method_name, fields = route
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            kwargs = {name: payload[name] for name in fields if name in payload}
            result = getattr(get_app(), method_name)(**kwargs)
            self._send_json(200, result)
        except CareLoopError as error:
            self._send_error_json(error.status, str(error))
        except TypeError as error:
            self._send_error_json(400, f"请求参数有误: {error}")
        except (ValueError, KeyError) as error:
            self._send_error_json(400, f"请求参数有误: {error}")

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=DEFAULT_DATA_PATH, help="数据落盘文件，重启后未完成事项不丢失")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        app = CareLoop()
        member = app.register_staff("自检员", "caregiver")
        child = app.enroll_child("自检儿童", ["guardian-selfcheck"])
        app.record_observation(member["id"], child["id"], "自检症状", "自检观察")
        print("基础检查通过")
        return
    set_app(CareLoop(store=JsonStore(args.data)))
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
