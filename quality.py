"""托育医育质量闭环的领域核心。

边界划分：
- 保育员：只能形成日常观察与“建议观察”意见；
- 健康管理员：具备筛查资质，组织筛查并提出“调整饮食”等非医疗处置意见；
- 授权医生：作出“需要就医”等医疗判断，签署用药与医疗处置。

异常以事件为主线：观察可连续追加，重复上报合并为同一事件；
升级、复核、转介、交接、回访、关闭全程留痕，换班或服务重启后
未完成事项仍然可查；家长只看本人孩子的信息，监管部门只接收
脱敏质量信息。
"""

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

# ---- 角色与可签署意见 ----

CAREGIVER = "caregiver"        # 保育员
HEALTH_ADMIN = "health_admin"  # 健康管理员（具备筛查资质）
DOCTOR = "doctor"              # 获授权医生

ROLE_NAMES = {
    CAREGIVER: "保育员",
    HEALTH_ADMIN: "健康管理员",
    DOCTOR: "授权医生",
}

# 三类健康意见及其唯一合法来源角色：角色边界即责任边界。
ADVICE_OBSERVE = "observe"     # 建议观察
ADVICE_REFER = "refer"         # 需要就医
ADVICE_DIET = "diet"           # 调整饮食

ADVICE_SOURCES = {
    ADVICE_OBSERVE: CAREGIVER,
    ADVICE_REFER: DOCTOR,
    ADVICE_DIET: HEALTH_ADMIN,
}

ADVICE_LABELS = {
    ADVICE_OBSERVE: "建议观察",
    ADVICE_REFER: "需要就医",
    ADVICE_DIET: "调整饮食",
}

# 升级理由
ESCALATION_REASONS = ("screening_abnormal", "observation_worsening", "doctor_requested")

# 事件状态
OPEN = "open"
REFERRED = "referred"
CLOSED = "closed"

# 资料用途
PURPOSE_CARE = "care"
PURPOSE_SCREENING = "screening"
PURPOSE_MEDICAL = "medical"
PURPOSE_QUALITY = "quality"
PURPOSE_NOTIFY_GUARDIAN = "notify_guardian"

ALL_PURPOSES = (
    PURPOSE_CARE,
    PURPOSE_SCREENING,
    PURPOSE_MEDICAL,
    PURPOSE_QUALITY,
    PURPOSE_NOTIFY_GUARDIAN,
)

READ_PURPOSES = (
    PURPOSE_CARE,
    PURPOSE_SCREENING,
    PURPOSE_MEDICAL,
)

# 不同动作所需的资料用途授权
ACTION_PURPOSE = {
    "observation": PURPOSE_CARE,
    "screening": PURPOSE_SCREENING,
    "medical": PURPOSE_MEDICAL,
}

# 各状态默认复核时限（小时）：异常出现后必须有人在期限内复核
DEFAULT_REVIEW_LIMIT_HOURS = 24

MEDICAL_KINDS = ("medical_judgement", "medication", "medical_disposition")


def _now():
    return datetime.now(timezone.utc)


def _ts(dt):
    return dt.isoformat()


def _parse(value):
    return datetime.fromisoformat(value)


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class QualityError(Exception):
    """领域规则冲突。"""

    status = 422


class AuthError(QualityError):
    """身份、资质或授权不满足。"""

    status = 403


class ConsentError(QualityError):
    """监护人同意的用途范围不覆盖本次使用。"""

    status = 403


class NotFoundError(QualityError):
    status = 404


class QualitySystem:
    """事件闭环系统；以事件（incident）为聚合根，全部状态可持久化。"""

    def __init__(self, path=None):
        self._lock = threading.RLock()
        self.path = path
        self.children = {}       # child_id -> {id, name, guardian_id, guardian_name}
        self.staff = {}          # staff_id -> {id, name, role, qualified}
        self.consents = {}       # consent_id -> 授权记录
        self.incidents = {}      # incident_id -> 事件
        self._seq = 0
        if path and os.path.exists(path):
            self._load()

    # ============ 人员与同意 ============

    def register_child(self, child_id, name, guardian_id, guardian_name):
        with self._lock:
            self.children[child_id] = {
                "id": child_id,
                "name": name,
                "guardian_id": guardian_id,
                "guardian_name": guardian_name,
            }
            self._save()
            return self.children[child_id]

    def register_staff(self, staff_id, name, role, qualified=False):
        if role not in ROLE_NAMES:
            raise QualityError(f"未知角色：{role}")
        if role == HEALTH_ADMIN and not qualified:
            raise QualityError("健康管理员必须具备筛查资质")
        with self._lock:
            self.staff[staff_id] = {
                "id": staff_id,
                "name": name,
                "role": role,
                "qualified": bool(qualified),
            }
            self._save()
            return self.staff[staff_id]

    def grant_consent(self, child_id, guardian_id, purposes, scope="all"):
        """监护人登记同意用途。scope=all 覆盖该儿童全部资料；
        scope=incident 时须带 incident_id，仅覆盖单一事件。"""
        with self._lock:
            child = self._require_child(child_id)
            if guardian_id != child["guardian_id"]:
                raise AuthError("只有监护人可以登记同意")
            purposes = list(purposes)
            for purpose in purposes:
                if purpose not in ALL_PURPOSES:
                    raise QualityError(f"未知资料用途：{purpose}")
            consent = {
                "id": _new_id("cse"),
                "child_id": child_id,
                "guardian_id": guardian_id,
                "purposes": purposes,
                "scope": scope,
                "incident_id": None,
                "granted_at": _ts(_now()),
                "revoked_at": None,
            }
            self.consents[consent["id"]] = consent
            self._save()
            return consent

    def revoke_consent(self, consent_id):
        with self._lock:
            consent = self.consents.get(consent_id)
            if consent is None:
                raise NotFoundError("同意记录不存在")
            consent["revoked_at"] = _ts(_now())
            self._save()
            return consent

    def _require_child(self, child_id):
        child = self.children.get(child_id)
        if child is None:
            raise NotFoundError("儿童不存在")
        return child

    def _staff(self, staff_id):
        member = self.staff.get(staff_id)
        if member is None:
            raise AuthError("人员未登记")
        return member

    def _consent_covers(self, child_id, purpose, incident_id):
        for consent in self.consents.values():
            if consent["child_id"] != child_id or consent["revoked_at"]:
                continue
            if purpose not in consent["purposes"]:
                continue
            if consent["scope"] == "all":
                return True
            if consent["incident_id"] == incident_id:
                return True
        return False

    def _require_consent(self, child_id, purpose, incident_id=None):
        if not self._consent_covers(child_id, purpose, incident_id):
            raise ConsentError(
                f"监护人未授权资料用于该用途（{purpose}）"
            )

    # ============ 观察与事件 ============

    def open_incident(self, child_id, staff_id, summary, review_limit_hours=None):
        """保育员发现异常，开启事件并写入首条连续观察。"""
        with self._lock:
            member = self._staff(staff_id)
            self._require_child(child_id)
            if member["role"] != CAREGIVER:
                raise AuthError("只有保育员可以从日常观察开启事件")
            self._require_consent(child_id, PURPOSE_CARE)
            now = _now()
            limit = (DEFAULT_REVIEW_LIMIT_HOURS if review_limit_hours is None
                     else review_limit_hours)
            incident = {
                "id": _new_id("inc"),
                "child_id": child_id,
                "status": OPEN,
                "summary": summary,
                "opened_by": staff_id,
                "opened_at": _ts(now),
                "review_due_at": _ts(now + timedelta(hours=limit)),
                "referral_target": None,
                "owner_id": staff_id,
                "merged_into": None,
                "fingerprint": self._fingerprint(child_id, now, summary),
                "observations": [],
                "screenings": [],
                "medical_records": [],
                "advice": [],
                "timeline": [],
                "improvements": [],
                "followups": [],
                "closure": None,
            }
            self.incidents[incident["id"]] = incident
            self._append(
                incident,
                "incident_opened",
                staff_id,
                {"summary": summary, "review_due_at": incident["review_due_at"]},
                now=now,
            )
            self.add_observation(incident["id"], staff_id, summary, now=now)
            self._save()
            return self.public_incident(incident)

    @staticmethod
    def _fingerprint(child_id, when, summary):
        day = when.date().isoformat()
        token = "".join(ch for ch in summary if ch.isalnum())[:24]
        return f"{child_id}|{day}|{token}"

    def add_observation(self, incident_id, staff_id, content, now=None):
        """连续观察：保育员在同一事件上追加，不另起事件。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            member = self._staff(staff_id)
            if member["role"] != CAREGIVER:
                raise AuthError("只有保育员可以记录日常观察")
            self._require_consent(incident["child_id"], PURPOSE_CARE, incident_id)
            if incident["status"] == CLOSED:
                raise QualityError("事件已关闭，不能继续记录观察")
            now = now or _now()
            record = {
                "id": _new_id("obs"),
                "staff_id": staff_id,
                "content": content,
                "at": _ts(now),
            }
            incident["observations"].append(record)
            self._append(incident, "observation_added", staff_id,
                         {"observation_id": record["id"], "content": content}, now=now)
            self._save()
            return record

    def find_duplicate_open_incident(self, child_id, summary, when=None):
        """同一天、同一儿童、摘要指纹相同的未关闭事件，视为重复上报。"""
        when = when or _now()
        fingerprint = self._fingerprint(child_id, when, summary)
        for incident in self.incidents.values():
            if (incident["child_id"] == child_id
                    and incident["status"] != CLOSED
                    and incident["merged_into"] is None
                    and incident["fingerprint"] == fingerprint):
                return incident
        return None

    def report_or_merge(self, child_id, staff_id, summary, content=None,
                        review_limit_hours=None):
        """重复上报合并为同一事件：命中当天未关闭事件则并入并留痕。"""
        with self._lock:
            duplicate = self.find_duplicate_open_incident(child_id, summary)
            if duplicate is not None:
                member = self._staff(staff_id)
                self._require_consent(child_id, PURPOSE_CARE, duplicate["id"])
                if member["role"] == CAREGIVER:
                    self.add_observation(
                        duplicate["id"], staff_id,
                        content or "重复上报：同症状再次发现",
                    )
                self._append(duplicate, "duplicate_merged", staff_id,
                             {"summary": summary})
                self._save()
                return self.public_incident(duplicate), True
            return self.open_incident(
                child_id, staff_id, summary,
                review_limit_hours=review_limit_hours,
            ), False

    # ============ 筛查与医疗 ============

    def record_screening(self, incident_id, staff_id, tool, result, findings,
                         advice_summary=None):
        """筛查由具备资质的健康管理员组织；可附“建议观察/调整饮食”意见。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            member = self._staff(staff_id)
            if member["role"] != HEALTH_ADMIN:
                raise AuthError("筛查只能由健康管理员组织")
            if not member["qualified"]:
                raise AuthError("该健康管理员不具备筛查资质")
            self._require_consent(incident["child_id"], PURPOSE_SCREENING, incident_id)
            record = {
                "id": _new_id("scr"),
                "staff_id": staff_id,
                "tool": tool,
                "result": result,
                "findings": findings,
                "at": _ts(_now()),
            }
            incident["screenings"].append(record)
            self._append(incident, "screening_recorded", staff_id,
                         {"screening_id": record["id"], "tool": tool, "result": result})
            if advice_summary:
                self._add_advice(incident, staff_id, ADVICE_DIET, advice_summary,
                                 record["id"], lock=False)
            self._save()
            return record

    def record_medical(self, incident_id, staff_id, kind, conclusion,
                       medication=None, advice_summary=None):
        """医疗判断与用药处置只能由获授权医生签署。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            member = self._staff(staff_id)
            if kind not in MEDICAL_KINDS:
                raise QualityError(f"未知医疗记录类型：{kind}")
            if member["role"] != DOCTOR:
                raise AuthError("医疗判断与用药处置只能由获授权医生签署")
            self._require_consent(incident["child_id"], PURPOSE_MEDICAL, incident_id)
            record = {
                "id": _new_id("med"),
                "staff_id": staff_id,
                "kind": kind,
                "conclusion": conclusion,
                "medication": medication,
                "signed_by": staff_id,
                "signed_at": _ts(_now()),
            }
            incident["medical_records"].append(record)
            self._append(incident, "medical_signed", staff_id,
                         {"medical_id": record["id"], "kind": kind})
            if advice_summary:
                self._add_advice(incident, staff_id, ADVICE_REFER, advice_summary,
                                 record["id"], lock=False)
            self._save()
            return record

    def add_advice(self, incident_id, staff_id, advice_code, summary,
                   linked_record_id=None):
        """登记健康意见；意见类型必须与签署人角色匹配。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            return self._add_advice(incident, staff_id, advice_code, summary,
                                    linked_record_id)

    def _add_advice(self, incident, staff_id, advice_code, summary,
                    linked_record_id=None, lock=True):
        member = self._staff(staff_id)
        expected = ADVICE_SOURCES.get(advice_code)
        if expected is None:
            raise QualityError(f"未知意见类型：{advice_code}")
        if member["role"] != expected:
            raise AuthError(
                f"“{ADVICE_LABELS[advice_code]}”只能由{ROLE_NAMES[expected]}提出"
            )
        purpose = ACTION_PURPOSE["medical" if advice_code == ADVICE_REFER
                                 else ("screening" if advice_code == ADVICE_DIET
                                       else "observation")]
        self._require_consent(incident["child_id"], purpose, incident["id"])
        record = {
            "id": _new_id("adv"),
            "code": advice_code,
            "label": ADVICE_LABELS[advice_code],
            "summary": summary,
            "staff_id": staff_id,
            "staff_role": member["role"],
            "staff_name": member["name"],
            "source_title": ROLE_NAMES[expected],
            "linked_record_id": linked_record_id,
            "at": _ts(_now()),
        }
        incident["advice"].append(record)
        self._append(incident, "advice_added", staff_id,
                     {"advice_id": record["id"], "code": advice_code})
        if lock:
            self._save()
        return record

    # ============ 升级、复核、转介、交接、回访、关闭 ============

    def escalate(self, incident_id, staff_id, reason, to_role=DOCTOR,
                 review_limit_hours=None):
        """异常升级：记录理由并设定下一次复核时限。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            if reason not in ESCALATION_REASONS:
                raise QualityError(f"未知升级理由：{reason}")
            member = self._staff(staff_id)
            if to_role == DOCTOR and member["role"] == CAREGIVER:
                # 保育员可请求升级，但须经健康管理员组织；这里仅记录请求
                pass
            now = _now()
            limit = (DEFAULT_REVIEW_LIMIT_HOURS if review_limit_hours is None
                     else review_limit_hours)
            incident["review_due_at"] = _ts(now + timedelta(hours=limit))
            self._append(incident, "escalated", staff_id,
                         {"reason": reason, "to_role": to_role,
                          "review_due_at": incident["review_due_at"]}, now=now)
            self._save()
            return {"review_due_at": incident["review_due_at"]}

    def review(self, incident_id, staff_id, note, new_review_limit_hours=DEFAULT_REVIEW_LIMIT_HOURS):
        """到点复核：换班后接手人完成复核并续定下一次时限。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            now = _now()
            record = {
                "id": _new_id("rev"),
                "staff_id": staff_id,
                "note": note,
                "at": _ts(now),
                "was_overdue": now > _parse(incident["review_due_at"]),
            }
            incident["timeline"].append(
                self._event("review_completed", staff_id,
                            {"review_id": record["id"], "note": note,
                             "was_overdue": record["was_overdue"]}, now)
            )
            incident["review_due_at"] = _ts(now + timedelta(hours=new_review_limit_hours))
            self._save()
            return record

    def refer(self, incident_id, staff_id, target, reason):
        """转介：指定转介对象（如指定医院/科室），事件进入转介态。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            member = self._staff(staff_id)
            if member["role"] not in (HEALTH_ADMIN, DOCTOR):
                raise AuthError("只有健康管理员或医生可以发起转介")
            incident["status"] = REFERRED
            incident["referral_target"] = target
            self._append(incident, "referred", staff_id,
                         {"target": target, "reason": reason})
            self._save()
            return {"status": REFERRED, "referral_target": target}

    def handover(self, incident_id, from_staff_id, to_staff_id, note):
        """交接责任：换班或服务重启时，未完成事项必须有明确责任人。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            if from_staff_id != incident["owner_id"]:
                raise QualityError("只有当前责任人可以交接")
            to_member = self._staff(to_staff_id)
            if incident["status"] == CLOSED:
                raise QualityError("事件已关闭，无需交接")
            incident["owner_id"] = to_staff_id
            self._append(incident, "handover", from_staff_id,
                         {"to_staff_id": to_staff_id,
                          "to_name": to_member["name"], "note": note})
            self._save()
            return {"owner_id": to_staff_id}

    def add_followup(self, incident_id, staff_id, channel, note):
        """家长回访记录。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            record = {
                "id": _new_id("fol"),
                "staff_id": staff_id,
                "channel": channel,
                "note": note,
                "at": _ts(_now()),
            }
            incident["followups"].append(record)
            self._append(incident, "followup_done", staff_id,
                         {"followup_id": record["id"], "channel": channel})
            self._save()
            return record

    def close_incident(self, incident_id, staff_id, basis):
        """关闭须有依据（专业结论/转介完成+回访），并核对无未完成改进项。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            member = self._staff(staff_id)
            if member["role"] == CAREGIVER:
                raise AuthError("保育员不能单独关闭健康事件")
            pending = [i for i in incident["improvements"] if i["status"] == "open"]
            if pending:
                raise QualityError("存在未完成的健康改进项，不能关闭")
            if not incident["medical_records"] and incident["status"] != REFERRED:
                raise QualityError("缺少专业结论或转介记录，不能关闭")
            incident["status"] = CLOSED
            incident["closure"] = {
                "by": staff_id,
                "at": _ts(_now()),
                "basis": basis,
            }
            self._append(incident, "closed", staff_id, {"basis": basis})
            self._save()
            return incident["closure"]

    # ============ 改进项 ============

    def add_improvement(self, incident_id, staff_id, title, due_hours=72):
        """从一次处置派生必须落实的健康改进，设定期限。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            now = _now()
            item = {
                "id": _new_id("imp"),
                "title": title,
                "owner_id": staff_id,
                "created_at": _ts(now),
                "due_at": _ts(now + timedelta(hours=due_hours)),
                "status": "open",
                "resolved_at": None,
            }
            incident["improvements"].append(item)
            self._append(incident, "improvement_created", staff_id,
                         {"improvement_id": item["id"], "due_at": item["due_at"]})
            self._save()
            return item

    def resolve_improvement(self, improvement_id, staff_id, note=""):
        with self._lock:
            for incident in self.incidents.values():
                for item in incident["improvements"]:
                    if item["id"] == improvement_id:
                        item["status"] = "resolved"
                        item["resolved_at"] = _ts(_now())
                        self._append(incident, "improvement_resolved", staff_id,
                                     {"improvement_id": improvement_id, "note": note})
                        self._save()
                        return item
            raise NotFoundError("改进项不存在")

    # ============ 视图 ============

    def parent_view(self, guardian_id, child_id):
        """家长端：仅本人孩子，呈现建议、通知与回访，给出每条意见的来源。"""
        with self._lock:
            child = self._require_child(child_id)
            if guardian_id != child["guardian_id"]:
                raise AuthError("只能查看本人孩子的信息")
            result = []
            for incident in self._incidents_of_child(child_id):
                result.append({
                    "incident_id": incident["id"],
                    "status": incident["status"],
                    "summary": incident["summary"],
                    "advice": [
                        {
                            "label": a["label"],
                            "summary": a["summary"],
                            "source_title": a["source_title"],
                            "staff_name": a["staff_name"],
                            "at": a["at"],
                        }
                        for a in incident["advice"]
                    ],
                    "notifications": [
                        {"type": e["type"], "at": e["at"], **{
                            k: v for k, v in e["detail"].items()
                            if k in ("target", "reason", "basis", "review_due_at")}}
                        for e in incident["timeline"]
                        if e["type"] in ("referred", "closed", "escalated")
                    ],
                    "followups": [
                        {"channel": f["channel"], "note": f["note"], "at": f["at"]}
                        for f in incident["followups"]
                    ],
                })
            return {"child_id": child_id, "incidents": result}

    def regulator_quality_view(self):
        """监管部门：只接收脱敏质量信息（无儿童/人员姓名与身份）。"""
        with self._lock:
            totals = {"open": 0, "referred": 0, "closed": 0}
            overdue_reviews = 0
            overdue_improvements = 0
            abnormal_screenings = 0
            advice_counts = dict.fromkeys(ADVICE_LABELS.values(), 0)
            now = _now()
            for incident in self.incidents.values():
                totals[incident["status"]] = totals.get(incident["status"], 0) + 1
                if (incident["status"] != CLOSED
                        and now > _parse(incident["review_due_at"])):
                    overdue_reviews += 1
                abnormal_screenings += sum(
                    1 for s in incident["screenings"] if s["result"] == "abnormal")
                for item in incident["improvements"]:
                    if item["status"] == "open" and now > _parse(item["due_at"]):
                        overdue_improvements += 1
                for advice in incident["advice"]:
                    advice_counts[advice["label"]] += 1
            return {
                "generated_at": _ts(now),
                "incident_counts": totals,
                "abnormal_screenings": abnormal_screenings,
                "overdue_reviews": overdue_reviews,
                "overdue_improvements": overdue_improvements,
                "advice_counts": advice_counts,
                "total_incidents": len(self.incidents),
            }

    def manager_trace(self, incident_id):
        """管理者：从一次处置还原最初观察、升级理由、专业结论、授权与关闭依据。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            return {
                "incident_id": incident["id"],
                "child_id": incident["child_id"],
                "status": incident["status"],
                "initial_observation": (incident["observations"][0]
                                        if incident["observations"] else None),
                "escalations": [
                    {"at": e["at"], "by": e["staff_id"], **e["detail"]}
                    for e in incident["timeline"] if e["type"] == "escalated"
                ],
                "professional_conclusions": [
                    {"id": m["id"], "kind": m["kind"], "conclusion": m["conclusion"],
                     "signed_by": m["signed_by"], "signed_at": m["signed_at"],
                     "medication": m["medication"]}
                    for m in incident["medical_records"]
                ],
                "screenings": list(incident["screenings"]),
                "referral_target": incident["referral_target"],
                "owner_id": incident["owner_id"],
                "review_due_at": incident["review_due_at"],
                "closure": incident["closure"],
                "timeline": incident["timeline"],
                "improvements": incident["improvements"],
            }

    def pending_work(self):
        """换班/服务重启后的未完成事项：逾期复核、转介未关闭、逾期改进。"""
        with self._lock:
            now = _now()
            items = []
            for incident in self.incidents.values():
                if incident["status"] == CLOSED:
                    continue
                overdue = now > _parse(incident["review_due_at"])
                items.append({
                    "kind": "overdue_review" if overdue else "awaiting_review",
                    "incident_id": incident["id"],
                    "owner_id": incident["owner_id"],
                    "review_due_at": incident["review_due_at"],
                })
                for improvement in incident["improvements"]:
                    if (improvement["status"] == "open"
                            and now > _parse(improvement["due_at"])):
                        items.append({
                            "kind": "overdue_improvement",
                            "incident_id": incident["id"],
                            "improvement_id": improvement["id"],
                            "title": improvement["title"],
                            "owner_id": improvement["owner_id"],
                            "due_at": improvement["due_at"],
                        })
            return {"generated_at": _ts(now), "items": items}

    # ============ 内部工具 ============

    def _require_incident(self, incident_id):
        incident = self.incidents.get(incident_id)
        if incident is None:
            raise NotFoundError("事件不存在")
        if incident.get("merged_into"):
            raise QualityError("该事件已合并，请使用主事件")
        return incident

    def _incidents_of_child(self, child_id):
        return [i for i in self.incidents.values() if i["child_id"] == child_id]

    def _event(self, event_type, staff_id, detail, now=None):
        self._seq += 1
        return {
            "seq": self._seq,
            "type": event_type,
            "staff_id": staff_id,
            "at": _ts(now or _now()),
            "detail": detail,
        }

    def _append(self, incident, event_type, staff_id, detail, now=None):
        incident["timeline"].append(self._event(event_type, staff_id, detail, now))

    def public_incident(self, incident):
        return {
            "id": incident["id"],
            "child_id": incident["child_id"],
            "status": incident["status"],
            "summary": incident["summary"],
            "review_due_at": incident["review_due_at"],
            "referral_target": incident["referral_target"],
            "owner_id": incident["owner_id"],
            "advice": [
                {"code": a["code"], "label": a["label"],
                 "source_title": a["source_title"], "staff_id": a["staff_id"]}
                for a in incident["advice"]
            ],
            "observation_count": len(incident["observations"]),
            "screening_count": len(incident["screenings"]),
            "medical_count": len(incident["medical_records"]),
            "improvement_count": len(incident["improvements"]),
        }

    # ============ 持久化（换班/重启不丢） ============

    def _snapshot(self):
        return {
            "children": self.children,
            "staff": self.staff,
            "consents": self.consents,
            "incidents": self.incidents,
            "seq": self._seq,
        }

    def _load(self):
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.children = data.get("children", {})
        self.staff = data.get("staff", {})
        self.consents = data.get("consents", {})
        self.incidents = data.get("incidents", {})
        self._seq = data.get("seq", 0)

    def _save(self):
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._snapshot(), handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
