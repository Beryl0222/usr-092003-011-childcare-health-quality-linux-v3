"""验证医育闭环的领域规则：角色边界、同意用途、事件生命周期与三类视图。"""

import unittest
from datetime import datetime, timedelta

import careloop
from careloop import (
    ALL_PURPOSES,
    CareLoop,
    ConflictError,
    ConsentMissingError,
    JsonStore,
    NotFoundError,
    PermissionDeniedError,
    PURPOSE_MEDICAL_CARE,
    PURPOSE_PARENT_NOTICE,
    PURPOSE_QUALITY_STATS,
    PURPOSE_SCREENING,
)


class FixedClock:
    def __init__(self, start="2026-09-27T08:00:00"):
        self.moment = datetime.fromisoformat(start)

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


class CareLoopTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock()
        self.app = CareLoop(clock=self.clock)
        self.caregiver = self.app.register_staff("王保育", "caregiver")
        self.manager = self.app.register_staff("李健康", "health_manager", qualified=True)
        self.doctor = self.app.register_staff("赵医生", "doctor", authorized=True)
        self.child = self.app.enroll_child("小明", ["guardian-1"])


class RoleBoundaryTest(CareLoopTestBase):
    def test_health_manager_must_be_qualified(self):
        with self.assertRaises(PermissionDeniedError):
            self.app.register_staff("无资质", "health_manager", qualified=False)

    def test_doctor_must_be_authorized(self):
        with self.assertRaises(PermissionDeniedError):
            self.app.register_staff("未授权", "doctor", authorized=False)

    def test_caregiver_can_observe_but_not_screen(self):
        obs = self.app.record_observation(
            self.caregiver["id"], self.child["id"], "咳嗽", "午后轻微咳嗽")
        self.assertFalse(obs["abnormal"])
        with self.assertRaises(PermissionDeniedError):
            self.app.organize_screening(
                self.caregiver["id"], self.child["id"], "视力", "normal", "未见异常")

    def test_unqualified_manager_cannot_screen(self):
        manager = self.app.register_staff("实习健康", "caregiver")
        with self.assertRaises(PermissionDeniedError):
            self.app.organize_screening(
                manager["id"], self.child["id"], "听力", "normal", "未见异常")

    def test_opinion_kind_matches_role(self):
        # 保育员可以“建议观察”
        opinion = self.app.add_opinion(
            self.caregiver["id"], self.child["id"], "observe", "建议继续观察体温")
        self.assertEqual(opinion["author_role"], "caregiver")
        # 保育员不能“调整饮食”
        with self.assertRaises(PermissionDeniedError):
            self.app.add_opinion(self.caregiver["id"], self.child["id"], "diet", "减少甜食")
        # 健康管理员可以“调整饮食”
        diet = self.app.add_opinion(self.manager["id"], self.child["id"], "diet", "减少甜食")
        self.assertEqual(diet["author_role"], "health_manager")
        # 健康管理员不能出具医疗意见
        with self.assertRaises(PermissionDeniedError):
            self.app.add_opinion(self.manager["id"], self.child["id"], "medical", "建议就诊")

    def test_medical_judgement_requires_authorized_doctor_signature(self):
        obs = self.app.record_observation(
            self.caregiver["id"], self.child["id"], "皮疹", "手臂红疹", abnormal=True)
        event_id = obs["event_id"]
        # 保育员不能出具医疗结论
        with self.assertRaises(PermissionDeniedError):
            self.app.conclude_medical(self.caregiver["id"], event_id, "过敏性皮疹")
        # 医生也必须签署
        with self.assertRaises(PermissionDeniedError):
            self.app.conclude_medical(self.doctor["id"], event_id, "过敏性皮疹")
        event = self.app.conclude_medical(
            self.doctor["id"], event_id, "过敏性皮疹", signature="赵医生")
        self.assertEqual(event["medical"]["signed_by"], "赵医生")

    def test_medication_requires_authorized_doctor_signature(self):
        with self.assertRaises(PermissionDeniedError):
            self.app.record_medication(self.manager["id"], self.child["id"], "炉甘石洗剂外用")
        with self.assertRaises(PermissionDeniedError):
            self.app.record_medication(self.doctor["id"], self.child["id"], "炉甘石洗剂外用")
        record = self.app.record_medication(
            self.doctor["id"], self.child["id"], "炉甘石洗剂外用", signature="赵医生")
        self.assertEqual(record["signed_by"], "赵医生")


class ConsentBoundaryTest(CareLoopTestBase):
    def test_daily_care_consent_required(self):
        self.app.revoke_consent(self.child["id"], "daily_care")
        with self.assertRaises(ConsentMissingError):
            self.app.record_observation(self.caregiver["id"], self.child["id"], "咳嗽", "轻微")

    def test_screening_consent_required(self):
        self.app.revoke_consent(self.child["id"], PURPOSE_SCREENING)
        with self.assertRaises(ConsentMissingError):
            self.app.organize_screening(
                self.manager["id"], self.child["id"], "视力", "normal", "未见异常")

    def test_medical_consent_required(self):
        self.app.revoke_consent(self.child["id"], PURPOSE_MEDICAL_CARE)
        obs = self.app.record_observation(
            self.caregiver["id"], self.child["id"], "发热", "38.2℃", abnormal=True)
        with self.assertRaises(ConsentMissingError):
            self.app.conclude_medical(
                self.doctor["id"], obs["event_id"], "上呼吸道感染", signature="赵医生")

    def test_notice_skipped_without_notice_consent(self):
        self.app.revoke_consent(self.child["id"], PURPOSE_PARENT_NOTICE)
        self.app.add_opinion(self.caregiver["id"], self.child["id"], "observe", "多喝水")
        view = self.app.parent_view("guardian-1", self.child["id"])
        self.assertEqual(view["notices"], [])
        self.assertEqual(len(view["opinions"]), 1)

    def test_quality_stats_require_consent(self):
        app = CareLoop()
        app.register_staff("员", "caregiver")
        app.enroll_child("童", ["g"], consents=["daily_care"])
        with self.assertRaises(ConsentMissingError):
            app.regulator_view()


class EventLifecycleTest(CareLoopTestBase):
    def _open_event(self):
        obs = self.app.record_observation(
            self.caregiver["id"], self.child["id"], "发热", "38.5℃", abnormal=True)
        return obs["event_id"]

    def test_duplicate_reports_merge_into_same_event(self):
        first = self.app.record_observation(
            self.caregiver["id"], self.child["id"], "发热", "38.5℃", abnormal=True)
        second = self.app.record_observation(
            self.caregiver["id"], self.child["id"], "发热", "38.7℃", abnormal=True)
        self.assertEqual(first["event_id"], second["event_id"])
        event = self.app.trace_event(first["event_id"])["event"]
        self.assertEqual(len(event["observation_ids"]), 2)
        # 合并后只保留一个复核待办
        review_tasks = [t for t in self.app.pending_items()
                        if t["kind"] == "review" and t["event_id"] == event["id"]]
        self.assertEqual(len(review_tasks), 1)

    def test_closed_event_does_not_merge(self):
        event_id = self._open_event()
        self.app.claim_event(self.manager["id"], event_id)
        self.app.complete_review(self.manager["id"], event_id, "体温已恢复正常")
        self.app.close_event(self.manager["id"], event_id, "体温恢复正常")
        obs = self.app.record_observation(
            self.caregiver["id"], self.child["id"], "发热", "再次发热", abnormal=True)
        self.assertNotEqual(obs["event_id"], event_id)

    def test_review_task_has_deadline(self):
        event_id = self._open_event()
        tasks = [t for t in self.app.pending_items() if t["event_id"] == event_id]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["due_at"], "2026-09-28T08:00:00")

    def test_escalation_records_reason_referral_and_handover(self):
        event_id = self._open_event()
        event = self.app.escalate_event(
            self.manager["id"], event_id, "持续高热不退", "hospital",
            handover_to=self.doctor["id"])
        self.assertEqual(event["status"], "escalated")
        self.assertEqual(event["escalation"]["reason"], "持续高热不退")
        self.assertEqual(event["escalation"]["refer_to"], "hospital")
        self.assertEqual(event["escalation"]["handover_to"], self.doctor["id"])
        self.assertEqual(event["owner_id"], self.doctor["id"])
        # 升级后复核待办完成，出现转介待办
        kinds = {t["kind"] for t in self.app.pending_items() if t["event_id"] == event_id}
        self.assertEqual(kinds, {"referral"})

    def test_double_escalation_rejected(self):
        event_id = self._open_event()
        self.app.escalate_event(self.manager["id"], event_id, "高热", "doctor")
        with self.assertRaises(ConflictError):
            self.app.escalate_event(self.manager["id"], event_id, "仍然高热", "hospital")

    def test_close_blocked_by_open_review_task(self):
        event_id = self._open_event()
        with self.assertRaises(ConflictError):
            self.app.close_event(self.manager["id"], event_id, "提前关闭")

    def test_caregiver_cannot_close(self):
        event_id = self._open_event()
        self.app.claim_event(self.manager["id"], event_id)
        with self.assertRaises(PermissionDeniedError):
            self.app.close_event(self.caregiver["id"], event_id, "恢复")

    def test_medical_stage_event_closed_only_by_doctor(self):
        event_id = self._open_event()
        self.app.conclude_medical(self.doctor["id"], event_id, "上呼吸道感染", signature="赵医生")
        with self.assertRaises(PermissionDeniedError):
            self.app.close_event(self.manager["id"], event_id, "已好转")
        event = self.app.close_event(self.doctor["id"], event_id, "症状消退，医嘱完成")
        self.assertEqual(event["status"], "closed")
        self.assertEqual(event["closure"]["basis"], "症状消退，医嘱完成")

    def test_trace_recovers_full_chain(self):
        event_id = self._open_event()
        self.app.escalate_event(self.manager["id"], event_id, "持续高热", "doctor")
        self.app.conclude_medical(self.doctor["id"], event_id, "上呼吸道感染", signature="赵医生")
        self.app.record_medication(
            self.doctor["id"], self.child["id"], "布洛芬混悬液 5ml",
            signature="赵医生", event_id=event_id)
        self.app.close_event(self.doctor["id"], event_id, "体温正常 48 小时")
        trace = self.app.trace_event(event_id)
        # 最初观察
        self.assertEqual(len(trace["observations"]), 1)
        self.assertEqual(trace["observations"][0]["symptom"], "发热")
        # 升级理由
        self.assertEqual(trace["event"]["escalation"]["reason"], "持续高热")
        # 专业结论与授权签署
        self.assertEqual(trace["event"]["medical"]["signed_by"], "赵医生")
        self.assertEqual(trace["medications"][0]["signed_by"], "赵医生")
        # 关闭依据
        self.assertEqual(trace["event"]["closure"]["basis"], "体温正常 48 小时")
        # 时间线覆盖关键节点
        types = [entry["type"] for entry in trace["timeline"]]
        self.assertEqual(
            types,
            ["opened", "escalated", "medical_conclusion", "medication", "closed"],
        )


class HandoverAndRestartTest(CareLoopTestBase):
    def test_shift_handover_moves_open_items(self):
        obs = self.app.record_observation(
            self.caregiver["id"], self.child["id"], "腹泻", "每日三次", abnormal=True)
        event_id = obs["event_id"]
        self.app.claim_event(self.manager["id"], event_id)
        relief = self.app.register_staff("陈健康", "health_manager", qualified=True)
        record = self.app.shift_handover(self.manager["id"], relief["id"], note="晚班交接")
        self.assertIn(event_id, record["moved_events"])
        self.assertEqual(self.app.trace_event(event_id)["event"]["owner_id"], relief["id"])
        owners = {t["owner_id"] for t in self.app.pending_items()}
        self.assertEqual(owners, {relief["id"]})

    def test_unassigned_review_claimed_by_relieving_manager(self):
        self.app.record_observation(
            self.caregiver["id"], self.child["id"], "咳嗽", "夜间加重", abnormal=True)
        relief = self.app.register_staff("陈健康", "health_manager", qualified=True)
        record = self.app.shift_handover(self.caregiver["id"], relief["id"])
        self.assertEqual(len(record["claimed_tasks"]), 1)
        owners = {t["owner_id"] for t in self.app.pending_items()}
        self.assertEqual(owners, {relief["id"]})

    def test_pending_items_survive_restart(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.json")
            app1 = CareLoop(store=JsonStore(path), clock=self.clock)
            caregiver = app1.register_staff("王保育", "caregiver")
            app1.enroll_child("小明", ["guardian-1"], child_id="child-1")
            app1.record_observation(caregiver["id"], "child-1", "发热", "38.5℃", abnormal=True)
            # 模拟服务重启：用同一数据文件重建实例
            app2 = CareLoop(store=JsonStore(path), clock=self.clock)
            pending = app2.pending_items()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["kind"], "review")
            trace = app2.trace_event(pending[0]["event_id"])
            self.assertEqual(trace["observations"][0]["detail"], "38.5℃")


class ViewTest(CareLoopTestBase):
    def test_parent_view_scoped_to_own_child(self):
        other = self.app.enroll_child("小红", ["guardian-2"])
        self.app.add_opinion(self.caregiver["id"], self.child["id"], "observe", "观察体温")
        self.app.add_opinion(self.manager["id"], other["id"], "diet", "少量多餐")
        view = self.app.parent_view("guardian-1", self.child["id"])
        self.assertEqual(view["child"]["name"], "小明")
        self.assertEqual(len(view["opinions"]), 1)
        self.assertEqual(view["opinions"][0]["kind_label"], "建议观察")
        self.assertEqual(view["opinions"][0]["author"]["role_label"], "保育员")
        with self.assertRaises(PermissionDeniedError):
            self.app.parent_view("guardian-2", self.child["id"])

    def test_parent_view_shows_followups(self):
        followup = self.app.schedule_followup(
            self.manager["id"], self.child["id"], "电话回访体温", "2026-09-28T09:00:00")
        view = self.app.parent_view("guardian-1", self.child["id"])
        self.assertEqual(view["followups"][0]["id"], followup["id"])

    def test_regulator_view_is_deidentified(self):
        self.app.record_observation(
            self.caregiver["id"], self.child["id"], "发热", "38.5℃", abnormal=True)
        stats = self.app.regulator_view()
        self.assertEqual(stats["events"]["total"], 1)
        self.assertEqual(stats["events"]["open"], 1)
        # 输出中不得出现儿童或员工姓名
        import json
        blob = json.dumps(stats, ensure_ascii=False)
        for name in ("小明", "王保育", "guardian-1"):
            self.assertNotIn(name, blob)

    def test_overdue_improvements_surface_long_pending(self):
        improvement = self.app.add_improvement(
            self.manager["id"], self.child["id"], "增加户外活动时长")
        self.assertEqual(self.app.overdue_improvements(min_days=30), [])
        self.clock.advance(days=45)
        overdue = self.app.overdue_improvements(min_days=30)
        self.assertEqual([i["id"] for i in overdue], [improvement["id"]])
        self.assertEqual(overdue[0]["days_open"], 45)
        self.app.complete_improvement(self.manager["id"], improvement["id"], "已排入课表")
        self.assertEqual(self.app.overdue_improvements(min_days=30), [])

    def test_followup_completion_flow(self):
        obs = self.app.record_observation(
            self.caregiver["id"], self.child["id"], "发热", "38℃", abnormal=True)
        event_id = obs["event_id"]
        followup = self.app.schedule_followup(
            self.doctor["id"], self.child["id"], "三日后回访", "2026-09-30T08:00:00",
            event_id=event_id)
        self.app.complete_followup(self.doctor["id"], followup["id"], "体温已正常")
        kinds = {t["kind"] for t in self.app.pending_items() if t["event_id"] == event_id}
        self.assertEqual(kinds, {"review"})  # 回访待办已消除，仅剩复核待办


if __name__ == "__main__":
    unittest.main()
