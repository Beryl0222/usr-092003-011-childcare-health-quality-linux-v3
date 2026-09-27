"""验证医育结合的角色边界、同意限制与事件闭环规则。"""

import os
import tempfile
import unittest

from quality import (
    ADVICE_DIET,
    ADVICE_OBSERVE,
    ADVICE_REFER,
    ALL_PURPOSES,
    AuthError,
    ConsentError,
    NotFoundError,
    QualityError,
    QualitySystem,
)


def make_system(with_consent=True):
    system = QualitySystem()
    system.register_child("child1", "小明", "guardian1", "小明妈妈")
    system.register_child("child2", "小红", "guardian2", "小红爸爸")
    system.register_staff("care1", "王保育", "caregiver")
    system.register_staff("care2", "李保育", "caregiver")
    system.register_staff("admin1", "赵健管", "health_admin", qualified=True)
    system.register_staff("doc1", "陈医生", "doctor")
    if with_consent:
        system.grant_consent("child1", "guardian1", list(ALL_PURPOSES))
    return system


class RoleBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.system = make_system()
        self.incident = self.system.report_or_merge(
            "child1", "care1", "午睡后咳嗽", "咳嗽两声")[0]

    def test_caregiver_cannot_screen_or_prescribe(self):
        with self.assertRaises(AuthError):
            self.system.record_screening(
                self.incident["id"], "care1", "体温计", "normal", "36.5")
        with self.assertRaises(AuthError):
            self.system.record_medical(
                self.incident["id"], "care1", "medication", "用药", medication="退烧药")

    def test_health_admin_must_be_qualified(self):
        with self.assertRaises(QualityError):
            self.system.register_staff("admin2", "无资质", "health_admin")

    def test_health_admin_cannot_sign_medical(self):
        with self.assertRaises(AuthError):
            self.system.record_medical(
                self.incident["id"], "admin1", "medical_judgement", "判断")

    def test_advice_source_boundaries(self):
        # 保育员只能提“建议观察”
        self.system.add_advice(self.incident["id"], "care1", ADVICE_OBSERVE, "继续观察")
        with self.assertRaises(AuthError):
            self.system.add_advice(self.incident["id"], "care1", ADVICE_REFER, "去就医")
        with self.assertRaises(AuthError):
            self.system.add_advice(self.incident["id"], "care1", ADVICE_DIET, "少油")
        # 健康管理员只能提“调整饮食”
        self.system.add_advice(self.incident["id"], "admin1", ADVICE_DIET, "清淡饮食")
        with self.assertRaises(AuthError):
            self.system.add_advice(self.incident["id"], "admin1", ADVICE_REFER, "去就医")
        # 医生提“需要就医”
        self.system.add_advice(self.incident["id"], "doc1", ADVICE_REFER, "建议就诊")

    def test_advice_carries_source_title(self):
        self.system.add_advice(self.incident["id"], "care1", ADVICE_OBSERVE, "继续观察")
        self.system.add_advice(self.incident["id"], "admin1", ADVICE_DIET, "清淡饮食")
        self.system.add_advice(self.incident["id"], "doc1", ADVICE_REFER, "建议就诊")
        view = self.system.parent_view("guardian1", "child1")
        sources = {a["label"]: a["source_title"]
                   for a in view["incidents"][0]["advice"]}
        self.assertEqual(sources, {
            "建议观察": "保育员",
            "调整饮食": "健康管理员",
            "需要就医": "授权医生",
        })


class ConsentBoundaryTest(unittest.TestCase):
    def test_no_consent_blocks_opening(self):
        system = make_system(with_consent=False)
        with self.assertRaises(ConsentError):
            system.report_or_merge("child1", "care1", "皮疹", "手臂红点")

    def test_purpose_limited_consent(self):
        system = QualitySystem()
        system.register_child("c1", "小宝", "g1", "监护人")
        system.register_staff("care", "保育", "caregiver")
        system.register_staff("admin", "健管", "health_admin", qualified=True)
        system.grant_consent("c1", "g1", ["care"])
        incident = system.report_or_merge("c1", "care", "咳嗽", "干咳")[0]
        # 照护用途已授权，筛查用途未授权
        with self.assertRaises(ConsentError):
            system.record_screening(incident["id"], "admin", "量表", "normal", "无异常")

    def test_revoked_consent_blocks_use(self):
        system = make_system(with_consent=False)
        consent = system.grant_consent("child1", "guardian1", ["care"])
        incident = system.report_or_merge("child1", "care1", "腹泻", "一次")[0]
        system.revoke_consent(consent["id"])
        with self.assertRaises(ConsentError):
            system.add_observation(incident["id"], "care1", "再次腹泻")


class IncidentFlowTest(unittest.TestCase):
    def setUp(self):
        self.system = make_system()

    def test_duplicate_reports_merge_into_one(self):
        first, merged1 = self.system.report_or_merge("child1", "care1", "发热", "38度")
        second, merged2 = self.system.report_or_merge("child1", "care2", "发热", "仍发热")
        self.assertFalse(merged1)
        self.assertTrue(merged2)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["observation_count"], 2)

    def test_review_overdue_shows_in_pending(self):
        incident, _ = self.system.report_or_merge(
            "child1", "care1", "呕吐", "一次", review_limit_hours=0)
        pending = self.system.pending_work()
        kinds = {(i["incident_id"], i["kind"]) for i in pending["items"]}
        self.assertIn((incident["id"], "overdue_review"), kinds)

    def test_handover_requires_current_owner(self):
        incident, _ = self.system.report_or_merge("child1", "care1", "咳嗽", "干咳")
        with self.assertRaises(QualityError):
            self.system.handover(incident["id"], "care2", "care1", "换班")
        self.system.handover(incident["id"], "care1", "care2", "换班交接")
        trace = self.system.manager_trace(incident["id"])
        self.assertEqual(trace["owner_id"], "care2")

    def test_close_requires_basis_and_no_pending_improvement(self):
        incident, _ = self.system.report_or_merge("child1", "care1", "皮疹", "红点")
        improvement = self.system.add_improvement(
            incident["id"], "admin1", "更换床单并消毒")
        with self.assertRaises(QualityError):
            self.system.close_incident(incident["id"], "admin1", "观察结束")
        self.system.resolve_improvement(improvement["id"], "admin1", "已完成")
        # 保育员不能关闭
        with self.assertRaises(AuthError):
            self.system.close_incident(incident["id"], "care1", "观察结束")
        # 无专业结论且未转介，不能关闭
        with self.assertRaises(QualityError):
            self.system.close_incident(incident["id"], "admin1", "观察结束")
        self.system.record_medical(
            incident["id"], "doc1", "medical_judgement", "普通过敏，无需用药")
        closure = self.system.close_incident(
            incident["id"], "doc1", "医生结论：普通过敏，已回访")
        self.assertEqual(closure["by"], "doc1")

    def test_full_flow_traceable(self):
        system = self.system
        incident, _ = system.report_or_merge("child1", "care1", "持续低热", "37.8")
        iid = incident["id"]
        system.add_observation(iid, "care1", "精神稍差")
        system.record_screening(iid, "admin1", "体温复测", "abnormal", "38.1")
        system.escalate(iid, "admin1", "screening_abnormal")
        system.refer(iid, "admin1", "市儿童医院发热门诊", "筛查异常")
        system.record_medical(iid, "doc1", "medical_judgement", "病毒感染",
                              medication="布洛芬混悬液")
        system.add_followup(iid, "care1", "电话", "家长反馈已退热")
        system.close_incident(iid, "doc1", "医生结论+回访正常")

        trace = system.manager_trace(iid)
        self.assertEqual(trace["initial_observation"]["content"], "持续低热")
        self.assertEqual(trace["escalations"][0]["reason"], "screening_abnormal")
        self.assertEqual(trace["professional_conclusions"][0]["signed_by"], "doc1")
        self.assertEqual(trace["referral_target"], "市儿童医院发热门诊")
        self.assertEqual(trace["closure"]["by"], "doc1")
        self.assertEqual(trace["status"], "closed")


class ViewSeparationTest(unittest.TestCase):
    def setUp(self):
        self.system = make_system()
        incident, _ = self.system.report_or_merge("child1", "care1", "咳嗽", "干咳")
        self.iid = incident["id"]
        self.system.add_advice(self.iid, "doc1", ADVICE_REFER, "建议就诊")
        self.system.add_followup(self.iid, "care1", "微信", "已告知家长")

    def test_parent_view_scoped_to_own_child(self):
        view = self.system.parent_view("guardian1", "child1")
        self.assertEqual(len(view["incidents"]), 1)
        self.assertEqual(view["incidents"][0]["advice"][0]["source_title"], "授权医生")
        self.assertEqual(view["incidents"][0]["followups"][0]["channel"], "微信")
        with self.assertRaises(AuthError):
            self.system.parent_view("guardian2", "child1")

    def test_regulator_view_is_deidentified(self):
        view = self.system.regulator_quality_view()
        text = str(view)
        for leaked in ("小明", "王保育", "陈医生", "child1", "guardian1"):
            self.assertNotIn(leaked, text)
        self.assertEqual(view["incident_counts"]["open"], 1)
        self.assertEqual(view["advice_counts"]["需要就医"], 1)


class PersistenceTest(unittest.TestCase):
    def test_restart_keeps_pending_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.json")
            system = QualitySystem(path)
            system.register_child("c1", "小宝", "g1", "监护人")
            system.register_staff("care", "保育", "caregiver")
            system.grant_consent("c1", "g1", list(ALL_PURPOSES))
            incident, _ = system.report_or_merge(
                "c1", "care", "皮疹", "红点", review_limit_hours=0)
            system.add_improvement(incident["id"], "care", "消毒玩具", due_hours=0)

            reloaded = QualitySystem(path)
            pending = reloaded.pending_work()
            kinds = {i["kind"] for i in pending["items"]}
            self.assertIn("overdue_review", kinds)
            self.assertIn("overdue_improvement", kinds)
            trace = reloaded.manager_trace(incident["id"])
            self.assertEqual(trace["initial_observation"]["content"], "皮疹")


if __name__ == "__main__":
    unittest.main()
