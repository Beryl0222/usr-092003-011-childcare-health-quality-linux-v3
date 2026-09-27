"""托育医育质量闭环的领域规则。

边界约定（医育结合的分工）：

- 保育员：记录日常照护观察，可提出“建议观察”，不具备筛查与医疗权限。
- 健康管理员：须持有筛查资质，组织健康筛查，可提出“调整饮食”等保健建议。
- 医生：须获医疗授权，医疗判断与用药处置只能由获授权医生签署。

数据使用受监护人同意的用途限制；家长端只看到与自己孩子有关的建议、
通知和回访；监管部门只接收脱敏后的质量统计；管理者可以从一次处置还原
最初观察、升级理由、专业结论、授权及关闭依据，并发现长期未落实的改进。
"""

from datetime import datetime, timedelta

# 角色
ROLE_CAREGIVER = "caregiver"        # 保育员
ROLE_HEALTH_MANAGER = "health_manager"  # 健康管理员
ROLE_DOCTOR = "doctor"              # 医生

ROLE_LABELS = {
    ROLE_CAREGIVER: "保育员",
    ROLE_HEALTH_MANAGER: "健康管理员",
    ROLE_DOCTOR: "医生",
}

# 意见种类（家长最想知道：这句话是谁说的、谁来跟进）
OPINION_OBSERVE = "observe"   # 建议观察（保育员及以上）
OPINION_DIET = "diet"         # 调整饮食（健康管理员及以上）
OPINION_MEDICAL = "medical"   # 就医建议（获授权医生签署）

OPINION_LABELS = {
    OPINION_OBSERVE: "建议观察",
    OPINION_DIET: "调整饮食",
    OPINION_MEDICAL: "就医建议",
}

# 监护人同意的用途
PURPOSE_DAILY_CARE = "daily_care"        # 日常照护记录
PURPOSE_SCREENING = "screening"          # 健康筛查
PURPOSE_MEDICAL_CARE = "medical_care"    # 医疗处置
PURPOSE_PARENT_NOTICE = "parent_notice"  # 家长告知
PURPOSE_QUALITY_STATS = "quality_stats"  # 脱敏质量统计

ALL_PURPOSES = (
    PURPOSE_DAILY_CARE,
    PURPOSE_SCREENING,
    PURPOSE_MEDICAL_CARE,
    PURPOSE_PARENT_NOTICE,
    PURPOSE_QUALITY_STATS,
)

# 事件状态
EVENT_OPEN = "open"                # 待复核
EVENT_ESCALATED = "escalated"      # 已升级转介
EVENT_MEDICAL = "medical"          # 已有医疗结论
EVENT_CLOSED = "closed"            # 已关闭

# 待办类型（统一台账，换班与重启都不能让事项消失）
TASK_REVIEW = "review"            # 复核
TASK_REFERRAL = "referral"        # 转介跟进
TASK_FOLLOWUP = "followup"        # 回访
TASK_IMPROVEMENT = "improvement"  # 健康改进

# 家长通知类型
NOTICE_OPINION = "opinion"
NOTICE_ESCALATION = "escalation"
NOTICE_MEDICAL = "medical"
NOTICE_FOLLOWUP = "followup"
NOTICE_CLOSURE = "closure"

# 转介对象
REFERRAL_HEALTH_MANAGER = "health_manager"
REFERRAL_DOCTOR = "doctor"
REFERRAL_HOSPITAL = "hospital"

DEFAULT_REVIEW_HOURS = 24

_OPINION_ROLE_RULES = {
    OPINION_OBSERVE: {ROLE_CAREGIVER, ROLE_HEALTH_MANAGER, ROLE_DOCTOR},
    OPINION_DIET: {ROLE_HEALTH_MANAGER, ROLE_DOCTOR},
    OPINION_MEDICAL: {ROLE_DOCTOR},
}

_OPINION_PURPOSES = {
    OPINION_OBSERVE: PURPOSE_DAILY_CARE,
    OPINION_DIET: PURPOSE_SCREENING,
    OPINION_MEDICAL: PURPOSE_MEDICAL_CARE,
}

_ESCALATION_ROLES = {ROLE_CAREGIVER, ROLE_HEALTH_MANAGER, ROLE_DOCTOR}


class CareLoopError(Exception):
    """领域错误基类，status 供 HTTP 层映射。"""

    status = 400


class NotFoundError(CareLoopError):
    status = 404


class PermissionDeniedError(CareLoopError):
    status = 403


class ConsentMissingError(CareLoopError):
    status = 403


class ConflictError(CareLoopError):
    status = 409


def new_store():
    """返回一份空的数据存储结构。"""
    return {
        "staff": {},
        "children": {},
        "observations": [],
        "events": {},
        "opinions": [],
        "notices": [],
        "followups": [],
        "improvements": [],
        "pending_tasks": [],
        "handovers": [],
        "counters": {},
    }


class JsonStore:
    """把数据落盘为 JSON 文件，服务重启后未完成事项不消失。"""

    def __init__(self, path):
        self.path = path

    def load(self):
        import json
        import os

        if not os.path.exists(self.path):
            return new_store()
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, data):
        import json
        import os

        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp_path, self.path)


def _parse_time(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


class CareLoop:
    """托育医育质量闭环的领域服务。"""

    def __init__(self, store=None, review_hours=DEFAULT_REVIEW_HOURS, clock=None):
        self._store = store
        self._review_hours = review_hours
        self._clock = clock or datetime.now
        if store is None:
            self.data = new_store()
        else:
            self.data = store.load()

    # ---------- 基础工具 ----------

    def _now(self):
        return self._clock().isoformat(timespec="seconds")

    def _save(self):
        if self._store is not None:
            self._store.save(self.data)

    def _next_id(self, prefix):
        counters = self.data["counters"]
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]:04d}"

    @staticmethod
    def _require(payload, *fields):
        missing = [f for f in fields if payload.get(f) in (None, "")]
        if missing:
            raise CareLoopError("缺少必填字段: " + ", ".join(missing))

    def _staff(self, staff_id):
        member = self.data["staff"].get(staff_id)
        if member is None:
            raise NotFoundError(f"员工不存在: {staff_id}")
        return member

    def _child(self, child_id):
        child = self.data["children"].get(child_id)
        if child is None:
            raise NotFoundError(f"儿童不存在: {child_id}")
        return child

    def _event(self, event_id):
        event = self.data["events"].get(event_id)
        if event is None:
            raise NotFoundError(f"事件不存在: {event_id}")
        return event

    def _require_role(self, staff_id, roles, action):
        member = self._staff(staff_id)
        if member["role"] not in roles:
            role_labels = {
                frozenset({ROLE_DOCTOR}): "获授权医生",
                frozenset({ROLE_HEALTH_MANAGER}): "具备筛查资质的健康管理员",
            }
            wanted = role_labels.get(frozenset(roles), "、".join(
                ROLE_LABELS[r] for r in sorted(roles)))
            raise PermissionDeniedError(f"{action}仅限{wanted}，当前为{ROLE_LABELS[member['role']]}")
        return member

    def _require_consent(self, child_id, purpose):
        child = self._child(child_id)
        if purpose not in child["consents"]:
            raise ConsentMissingError(f"监护人未同意用途 {purpose}，相关操作被阻止")

    def _add_task(self, kind, event_id, owner_id, due_at, note):
        task = {
            "id": self._next_id("task"),
            "kind": kind,
            "event_id": event_id,
            "owner_id": owner_id,
            "created_at": self._now(),
            "due_at": due_at,
            "status": "open",
            "note": note,
        }
        self.data["pending_tasks"].append(task)
        return task

    def _resolve_tasks(self, event_id, kinds, note):
        now = self._now()
        resolved = []
        for task in self.data["pending_tasks"]:
            if task["event_id"] == event_id and task["kind"] in kinds and task["status"] == "open":
                task["status"] = "done"
                task["resolved_at"] = now
                task["resolution"] = note
                resolved.append(task)
        return resolved

    def _open_tasks(self, event_id, kinds=None):
        return [
            t for t in self.data["pending_tasks"]
            if t["event_id"] == event_id and t["status"] == "open"
            and (kinds is None or t["kind"] in kinds)
        ]

    def _timeline(self, event, entry_type, at, actor_id, summary, **extra):
        entry = {
            "type": entry_type,
            "at": at,
            "actor_id": actor_id,
            "summary": summary,
        }
        entry.update(extra)
        event["timeline"].append(entry)

    def _notify_parents(self, child_id, kind, content, event_id=None):
        """生成家长通知；未同意告知用途时跳过并返回 None。"""
        child = self._child(child_id)
        if PURPOSE_PARENT_NOTICE not in child["consents"]:
            return None
        notice = {
            "id": self._next_id("notice"),
            "child_id": child_id,
            "event_id": event_id,
            "kind": kind,
            "content": content,
            "created_at": self._now(),
        }
        self.data["notices"].append(notice)
        return notice

    def _staff_ref(self, staff_id):
        member = self.data["staff"].get(staff_id)
        if member is None:
            return {"id": staff_id, "name": None, "role": None, "role_label": None}
        return {
            "id": member["id"],
            "name": member["name"],
            "role": member["role"],
            "role_label": ROLE_LABELS[member["role"]],
        }

    # ---------- 人员与儿童 ----------

    def register_staff(self, name, role, qualified=False, authorized=False, staff_id=None):
        """登记员工。筛查资质与医疗授权分开记录，互不代表。"""
        self._require({"name": name, "role": role}, "name", "role")
        if role not in ROLE_LABELS:
            raise CareLoopError(f"未知角色: {role}")
        if role == ROLE_HEALTH_MANAGER and not qualified:
            raise PermissionDeniedError("健康管理员须具备筛查资质才能登记上岗")
        if role == ROLE_DOCTOR and not authorized:
            raise PermissionDeniedError("医生须获医疗授权才能登记上岗")
        member = {
            "id": staff_id or self._next_id("staff"),
            "name": name,
            "role": role,
            "qualified": bool(qualified),
            "authorized": bool(authorized),
        }
        self.data["staff"][member["id"]] = member
        self._save()
        return dict(member)

    def enroll_child(self, name, guardian_ids, consents=None, child_id=None):
        """登记儿童及监护人，并记录监护人同意的用途范围。"""
        self._require({"name": name}, "name")
        if not guardian_ids:
            raise CareLoopError("至少需要一位监护人")
        unknown = [p for p in (consents or ALL_PURPOSES) if p not in ALL_PURPOSES]
        if unknown:
            raise CareLoopError("未知用途: " + ", ".join(unknown))
        child = {
            "id": child_id or self._next_id("child"),
            "name": name,
            "guardian_ids": list(guardian_ids),
            "consents": sorted(consents) if consents is not None else list(ALL_PURPOSES),
        }
        self.data["children"][child["id"]] = child
        self._save()
        return dict(child)

    def grant_consent(self, child_id, purpose):
        child = self._child(child_id)
        if purpose not in ALL_PURPOSES:
            raise CareLoopError(f"未知用途: {purpose}")
        if purpose not in child["consents"]:
            child["consents"].append(purpose)
            child["consents"].sort()
            self._save()
        return dict(child)

    def revoke_consent(self, child_id, purpose):
        child = self._child(child_id)
        if purpose in child["consents"]:
            child["consents"].remove(purpose)
            self._save()
        return dict(child)

    # ---------- 日常观察（连续记录） ----------

    def _open_event_for(self, child_id, symptom):
        for event in self.data["events"].values():
            if (
                event["child_id"] == child_id
                and event["symptom"] == symptom
                and event["status"] != EVENT_CLOSED
            ):
                return event
        return None

    def record_observation(self, staff_id, child_id, symptom, detail,
                           abnormal=False, observed_at=None):
        """保育员记录日常观察，形成连续记录。

        异常观察同一儿童、同一症状的重复上报会合并进同一未关闭事件，
        不重复生成复核待办。
        """
        member = self._require_role(
            staff_id, {ROLE_CAREGIVER, ROLE_HEALTH_MANAGER, ROLE_DOCTOR}, "记录观察"
        )
        self._require({"symptom": symptom, "detail": detail}, "symptom", "detail")
        self._require_consent(child_id, PURPOSE_DAILY_CARE)
        now = self._now()
        observation = {
            "id": self._next_id("obs"),
            "child_id": child_id,
            "staff_id": member["id"],
            "staff_role": member["role"],
            "symptom": symptom,
            "detail": detail,
            "abnormal": bool(abnormal),
            "observed_at": observed_at or now,
            "recorded_at": now,
            "event_id": None,
        }
        if abnormal:
            event = self._open_event_for(child_id, symptom)
            if event is None:
                event = {
                    "id": self._next_id("evt"),
                    "child_id": child_id,
                    "symptom": symptom,
                    "status": EVENT_OPEN,
                    "owner_id": None,
                    "opened_at": now,
                    "observation_ids": [],
                    "timeline": [],
                }
                self.data["events"][event["id"]] = event
                due = (_parse_time(now) + timedelta(hours=self._review_hours)).isoformat(timespec="seconds")
                self._add_task(TASK_REVIEW, event["id"], None, due, "异常观察待复核")
                self._timeline(event, "opened", now, member["id"],
                               f"异常观察开启事件，复核时限 {self._review_hours} 小时")
            else:
                self._timeline(event, "observation_merged", now, member["id"],
                               "重复上报已合并为同一事件")
            event["observation_ids"].append(observation["id"])
            observation["event_id"] = event["id"]
        self.data["observations"].append(observation)
        self._save()
        return dict(observation)

    def journal(self, child_id):
        """儿童的连续日常观察记录。"""
        self._child(child_id)
        return [dict(o) for o in self.data["observations"] if o["child_id"] == child_id]

    # ---------- 筛查（须具备资质） ----------

    def organize_screening(self, staff_id, child_id, item, result, summary):
        """组织健康筛查，仅限具备筛查资质的健康管理员。"""
        member = self._require_role(staff_id, {ROLE_HEALTH_MANAGER}, "组织筛查")
        if not member["qualified"]:
            raise PermissionDeniedError("该健康管理员不具备筛查资质")
        self._require({"item": item, "result": result, "summary": summary},
                      "item", "result", "summary")
        self._require_consent(child_id, PURPOSE_SCREENING)
        now = self._now()
        screening = {
            "id": self._next_id("scr"),
            "child_id": child_id,
            "item": item,
            "result": result,
            "summary": summary,
            "organized_by": member["id"],
            "at": now,
        }
        if result == "abnormal":
            event = {
                "id": self._next_id("evt"),
                "child_id": child_id,
                "symptom": f"筛查异常:{item}",
                "status": EVENT_OPEN,
                "owner_id": member["id"],
                "opened_at": now,
                "observation_ids": [],
                "timeline": [],
            }
            self.data["events"][event["id"]] = event
            due = (_parse_time(now) + timedelta(hours=self._review_hours)).isoformat(timespec="seconds")
            self._add_task(TASK_REVIEW, event["id"], member["id"], due, "筛查异常待复核")
            self._timeline(event, "opened", now, member["id"], "筛查异常开启事件")
            screening["event_id"] = event["id"]
        else:
            screening["event_id"] = None
        self.data.setdefault("screenings", []).append(screening)
        self._save()
        return dict(screening)

    # ---------- 专业意见 ----------

    def add_opinion(self, staff_id, child_id, kind, content, event_id=None, signature=None):
        """出具意见。种类决定所需角色，医疗意见必须由获授权医生签署。"""
        self._require({"content": content}, "content")
        if kind not in OPINION_LABELS:
            raise CareLoopError(f"未知意见种类: {kind}")
        member = self._require_role(staff_id, _OPINION_ROLE_RULES[kind],
                                    f"出具“{OPINION_LABELS[kind]}”")
        if kind == OPINION_MEDICAL:
            if not member["authorized"]:
                raise PermissionDeniedError("医疗判断只能由获授权医生作出")
            if not signature:
                raise PermissionDeniedError("医疗意见必须由医生本人签署")
        self._require_consent(child_id, _OPINION_PURPOSES[kind])
        if event_id is not None:
            event = self._event(event_id)
            if event["status"] == EVENT_CLOSED:
                raise ConflictError("事件已关闭，不能再补充意见")
        opinion = {
            "id": self._next_id("op"),
            "child_id": child_id,
            "event_id": event_id,
            "kind": kind,
            "kind_label": OPINION_LABELS[kind],
            "content": content,
            "author_id": member["id"],
            "author_role": member["role"],
            "signed_by": signature if kind == OPINION_MEDICAL else None,
            "created_at": self._now(),
        }
        self.data["opinions"].append(opinion)
        if event_id is not None:
            self._timeline(self.data["events"][event_id], "opinion", opinion["created_at"],
                           member["id"], f"出具{OPINION_LABELS[kind]}")
        self._notify_parents(child_id, NOTICE_OPINION,
                             f"{ROLE_LABELS[member['role']]}出具{OPINION_LABELS[kind]}：{content}",
                             event_id=event_id)
        self._save()
        return dict(opinion)

    # ---------- 升级与转介 ----------

    def escalate_event(self, staff_id, event_id, reason, refer_to,
                       review_due_at=None, handover_to=None):
        """升级事件：记录升级理由、复核时限、转介对象与交接责任。"""
        member = self._require_role(staff_id, _ESCALATION_ROLES, "升级事件")
        self._require({"reason": reason, "refer_to": refer_to}, "reason", "refer_to")
        if refer_to not in (REFERRAL_HEALTH_MANAGER, REFERRAL_DOCTOR, REFERRAL_HOSPITAL):
            raise CareLoopError(f"未知转介对象: {refer_to}")
        event = self._event(event_id)
        if event["status"] == EVENT_CLOSED:
            raise ConflictError("事件已关闭，不能升级")
        if event["status"] in (EVENT_ESCALATED, EVENT_MEDICAL):
            raise ConflictError("事件已升级，请勿重复升级")
        now = self._now()
        due = review_due_at
        if due is None:
            due = (_parse_time(now) + timedelta(hours=self._review_hours)).isoformat(timespec="seconds")
        handover_owner = None
        if handover_to is not None:
            handover_owner = self._staff(handover_to)["id"]
        event["status"] = EVENT_ESCALATED
        event["escalation"] = {
            "by": member["id"],
            "reason": reason,
            "refer_to": refer_to,
            "review_due_at": due,
            "handover_to": handover_owner,
            "at": now,
        }
        if handover_owner is not None:
            event["owner_id"] = handover_owner
        self._resolve_tasks(event["id"], {TASK_REVIEW}, "升级复核完成")
        referral_owner = handover_owner or (event["owner_id"] if refer_to == REFERRAL_HEALTH_MANAGER else None)
        self._add_task(TASK_REFERRAL, event["id"], referral_owner, due, f"转介至{refer_to}")
        self._timeline(event, "escalated", now, member["id"],
                       f"升级转介至{refer_to}：{reason}",
                       refer_to=refer_to, review_due_at=due, handover_to=handover_owner)
        self._notify_parents(event["child_id"], NOTICE_ESCALATION,
                             f"孩子的情况已升级，转介至{refer_to}，请在复核时限 {due} 前关注进展",
                             event_id=event["id"])
        self._save()
        return dict(event)

    def conclude_medical(self, staff_id, event_id, conclusion, signature=None):
        """医疗判断：只能由获授权医生签署。"""
        member = self._require_role(staff_id, {ROLE_DOCTOR}, "出具医疗结论")
        if not member["authorized"]:
            raise PermissionDeniedError("医疗判断只能由获授权医生作出")
        self._require({"conclusion": conclusion}, "conclusion")
        if not signature:
            raise PermissionDeniedError("医疗结论必须由医生本人签署")
        event = self._event(event_id)
        if event["status"] == EVENT_CLOSED:
            raise ConflictError("事件已关闭")
        self._require_consent(event["child_id"], PURPOSE_MEDICAL_CARE)
        now = self._now()
        event["status"] = EVENT_MEDICAL
        event["medical"] = {
            "conclusion": conclusion,
            "by": member["id"],
            "signed_by": signature,
            "at": now,
        }
        self._resolve_tasks(event["id"], {TASK_REVIEW, TASK_REFERRAL}, "医疗结论已出具")
        self._timeline(event, "medical_conclusion", now, member["id"],
                       f"医疗结论：{conclusion}", signed_by=signature)
        self._notify_parents(event["child_id"], NOTICE_MEDICAL,
                             f"医生已出具医疗结论：{conclusion}", event_id=event["id"])
        self._save()
        return dict(event)

    def record_medication(self, staff_id, child_id, description, signature=None, event_id=None):
        """用药处置：只能由获授权医生签署。"""
        member = self._require_role(staff_id, {ROLE_DOCTOR}, "用药处置")
        if not member["authorized"]:
            raise PermissionDeniedError("用药处置只能由获授权医生作出")
        self._require({"description": description}, "description")
        if not signature:
            raise PermissionDeniedError("用药处置必须由医生本人签署")
        self._require_consent(child_id, PURPOSE_MEDICAL_CARE)
        if event_id is not None:
            event = self._event(event_id)
            if event["status"] == EVENT_CLOSED:
                raise ConflictError("事件已关闭，不能再记录用药")
        now = self._now()
        record = {
            "id": self._next_id("med"),
            "child_id": child_id,
            "event_id": event_id,
            "description": description,
            "by": member["id"],
            "signed_by": signature,
            "at": now,
        }
        self.data.setdefault("medications", []).append(record)
        if event_id is not None:
            self._timeline(self.data["events"][event_id], "medication", now,
                           member["id"], f"用药处置：{description}", signed_by=signature)
        self._notify_parents(child_id, NOTICE_MEDICAL,
                             f"医生已签署用药处置：{description}", event_id=event_id)
        self._save()
        return dict(record)

    # ---------- 回访与改进 ----------

    def schedule_followup(self, staff_id, child_id, content, due_at, event_id=None):
        """安排回访，形成回访待办。"""
        member = self._require_role(staff_id, {ROLE_HEALTH_MANAGER, ROLE_DOCTOR}, "安排回访")
        self._require({"content": content, "due_at": due_at}, "content", "due_at")
        self._child(child_id)
        if event_id is not None:
            self._event(event_id)
        followup = {
            "id": self._next_id("fu"),
            "child_id": child_id,
            "event_id": event_id,
            "content": content,
            "owner_id": member["id"],
            "due_at": due_at,
            "status": "pending",
            "created_at": self._now(),
            "completed_at": None,
            "result": None,
        }
        self.data["followups"].append(followup)
        if event_id is not None:
            self._add_task(TASK_FOLLOWUP, event_id, member["id"], due_at, content)
            self._timeline(self.data["events"][event_id], "followup_scheduled",
                           followup["created_at"], member["id"], f"安排回访：{content}")
        self._save()
        return dict(followup)

    def complete_followup(self, staff_id, followup_id, result):
        """完成回访并记录结果。"""
        self._require_role(staff_id, {ROLE_HEALTH_MANAGER, ROLE_DOCTOR}, "完成回访")
        self._require({"result": result}, "result")
        followup = next((f for f in self.data["followups"] if f["id"] == followup_id), None)
        if followup is None:
            raise NotFoundError(f"回访不存在: {followup_id}")
        if followup["status"] == "done":
            raise ConflictError("回访已完成")
        now = self._now()
        followup["status"] = "done"
        followup["completed_at"] = now
        followup["result"] = result
        if followup["event_id"] is not None:
            self._resolve_tasks(followup["event_id"], {TASK_FOLLOWUP}, "回访已完成")
            self._timeline(self.data["events"][followup["event_id"]], "followup_done",
                           now, staff_id, f"回访完成：{result}")
        self._notify_parents(followup["child_id"], NOTICE_FOLLOWUP,
                             f"回访已完成：{result}", event_id=followup["event_id"])
        self._save()
        return dict(followup)

    def add_improvement(self, staff_id, child_id, content, event_id=None, due_at=None):
        """登记健康改进事项，用于发现长期未落实的改进。"""
        member = self._require_role(staff_id, {ROLE_HEALTH_MANAGER, ROLE_DOCTOR}, "登记健康改进")
        self._require({"content": content}, "content")
        self._child(child_id)
        if event_id is not None:
            self._event(event_id)
        improvement = {
            "id": self._next_id("imp"),
            "child_id": child_id,
            "event_id": event_id,
            "content": content,
            "owner_id": member["id"],
            "created_at": self._now(),
            "due_at": due_at,
            "status": "open",
            "implemented_at": None,
            "note": None,
        }
        self.data["improvements"].append(improvement)
        if event_id is not None:
            self._add_task(TASK_IMPROVEMENT, event_id, member["id"], due_at, content)
            self._timeline(self.data["events"][event_id], "improvement_added",
                           improvement["created_at"], member["id"], f"健康改进：{content}")
        self._save()
        return dict(improvement)

    def complete_improvement(self, staff_id, improvement_id, note=None):
        """确认健康改进已落实。"""
        self._require_role(staff_id, {ROLE_HEALTH_MANAGER, ROLE_DOCTOR}, "落实健康改进")
        improvement = next((i for i in self.data["improvements"] if i["id"] == improvement_id), None)
        if improvement is None:
            raise NotFoundError(f"改进事项不存在: {improvement_id}")
        if improvement["status"] == "done":
            raise ConflictError("改进事项已落实")
        now = self._now()
        improvement["status"] = "done"
        improvement["implemented_at"] = now
        improvement["note"] = note
        if improvement["event_id"] is not None:
            self._resolve_tasks(improvement["event_id"], {TASK_IMPROVEMENT}, "改进已落实")
            self._timeline(self.data["events"][improvement["event_id"]], "improvement_done",
                           now, staff_id, "健康改进已落实")
        self._save()
        return dict(improvement)

    def overdue_improvements(self, min_days=30):
        """长期未落实的健康改进（默认超过 30 天）。"""
        now = _parse_time(self._now())
        overdue = []
        for improvement in self.data["improvements"]:
            if improvement["status"] != "open":
                continue
            age_days = (now - _parse_time(improvement["created_at"])).days
            if age_days >= min_days:
                item = dict(improvement)
                item["days_open"] = age_days
                overdue.append(item)
        overdue.sort(key=lambda i: i["days_open"], reverse=True)
        return overdue

    # ---------- 待办台账、换班与关闭 ----------

    def claim_event(self, staff_id, event_id):
        """认领事件及其未分配的复核待办（明确跟进人）。"""
        member = self._require_role(staff_id, {ROLE_HEALTH_MANAGER, ROLE_DOCTOR}, "认领事件")
        event = self._event(event_id)
        if event["status"] == EVENT_CLOSED:
            raise ConflictError("事件已关闭")
        event["owner_id"] = member["id"]
        for task in self._open_tasks(event["id"]):
            if task["owner_id"] is None:
                task["owner_id"] = member["id"]
        self._timeline(event, "claimed", self._now(), member["id"], "认领事件，负责跟进")
        self._save()
        return dict(event)

    def complete_review(self, staff_id, event_id, note):
        """完成复核：出具复核结论，复核待办随之关闭。"""
        member = self._require_role(staff_id, {ROLE_HEALTH_MANAGER, ROLE_DOCTOR}, "完成复核")
        self._require({"note": note}, "note")
        event = self._event(event_id)
        if event["status"] == EVENT_CLOSED:
            raise ConflictError("事件已关闭")
        resolved = self._resolve_tasks(event["id"], {TASK_REVIEW}, note)
        if not resolved:
            raise ConflictError("没有待完成的复核")
        self._timeline(event, "review_done", self._now(), member["id"], f"复核完成：{note}")
        self._save()
        return dict(event)

    def complete_referral(self, staff_id, event_id, note):
        """完成转介跟进：记录转介结果，转介待办随之关闭。"""
        member = self._require_role(staff_id, {ROLE_HEALTH_MANAGER, ROLE_DOCTOR}, "完成转介")
        self._require({"note": note}, "note")
        event = self._event(event_id)
        if event["status"] == EVENT_CLOSED:
            raise ConflictError("事件已关闭")
        resolved = self._resolve_tasks(event["id"], {TASK_REFERRAL}, note)
        if not resolved:
            raise ConflictError("没有待完成的转介")
        self._timeline(event, "referral_done", self._now(), member["id"], f"转介完成：{note}")
        self._save()
        return dict(event)

    def shift_handover(self, from_staff_id, to_staff_id, note=None):
        """换班交接：把交班人名下未完成事项整体移交给接班人。"""
        from_member = self._staff(from_staff_id)
        to_member = self._staff(to_staff_id)
        now = self._now()
        moved_tasks = []
        for task in self.data["pending_tasks"]:
            if task["status"] == "open" and task["owner_id"] == from_member["id"]:
                task["owner_id"] = to_member["id"]
                moved_tasks.append(task["id"])
        # 接班人若是健康管理员/医生，可同时认领未分配的复核待办
        claimed_tasks = []
        if to_member["role"] in (ROLE_HEALTH_MANAGER, ROLE_DOCTOR):
            for task in self.data["pending_tasks"]:
                if task["status"] == "open" and task["owner_id"] is None and task["kind"] == TASK_REVIEW:
                    task["owner_id"] = to_member["id"]
                    claimed_tasks.append(task["id"])
        moved_events = []
        for event in self.data["events"].values():
            if event["status"] != EVENT_CLOSED and event["owner_id"] == from_member["id"]:
                event["owner_id"] = to_member["id"]
                moved_events.append(event["id"])
                self._timeline(event, "handover", now, from_member["id"],
                               f"换班交接给{to_member['name']}", to_staff_id=to_member["id"])
        record = {
            "id": self._next_id("ho"),
            "from_staff_id": from_member["id"],
            "to_staff_id": to_member["id"],
            "at": now,
            "note": note,
            "moved_tasks": moved_tasks,
            "claimed_tasks": claimed_tasks,
            "moved_events": moved_events,
        }
        self.data["handovers"].append(record)
        self._save()
        return dict(record)

    def pending_items(self, owner_id=None):
        """统一待办台账：重启、换班之后未完成事项都在这里。"""
        tasks = [dict(t) for t in self.data["pending_tasks"] if t["status"] == "open"]
        followups = [
            {"id": f["id"], "kind": TASK_FOLLOWUP, "event_id": f["event_id"],
             "owner_id": f["owner_id"], "due_at": f["due_at"], "note": f["content"],
             "created_at": f["created_at"], "status": "open"}
            for f in self.data["followups"] if f["status"] == "pending"
        ]
        items = tasks + followups
        if owner_id is not None:
            items = [i for i in items if i["owner_id"] == owner_id]
        items.sort(key=lambda i: (i["due_at"] or "9999", i["id"]))
        return items

    def close_event(self, staff_id, event_id, basis):
        """关闭事件：复核与转介待办必须完成，医疗阶段事件只能由授权医生关闭。"""
        member = self._staff(staff_id)
        self._require({"basis": basis}, "basis")
        event = self._event(event_id)
        if event["status"] == EVENT_CLOSED:
            raise ConflictError("事件已关闭")
        if event["status"] == EVENT_MEDICAL:
            if member["role"] != ROLE_DOCTOR or not member["authorized"]:
                raise PermissionDeniedError("进入医疗阶段的事件只能由获授权医生关闭")
        elif member["role"] not in (ROLE_HEALTH_MANAGER, ROLE_DOCTOR):
            raise PermissionDeniedError("关闭事件仅限健康管理员或医生")
        blocking = self._open_tasks(event["id"], {TASK_REVIEW, TASK_REFERRAL})
        if blocking:
            kinds = "、".join(sorted({t["kind"] for t in blocking}))
            raise ConflictError(f"存在未完成的{kinds}待办，不能关闭")
        now = self._now()
        event["status"] = EVENT_CLOSED
        event["closure"] = {"by": member["id"], "basis": basis, "at": now}
        self._timeline(event, "closed", now, member["id"], f"关闭事件：{basis}")
        self._notify_parents(event["child_id"], NOTICE_CLOSURE,
                             f"孩子的事件已关闭，依据：{basis}", event_id=event["id"])
        self._save()
        return dict(event)

    # ---------- 三类视图 ----------

    def parent_view(self, guardian_id, child_id):
        """家长端：只呈现与监护人自己孩子有关的建议、通知和回访。"""
        child = self._child(child_id)
        if guardian_id not in child["guardian_ids"]:
            raise PermissionDeniedError("只能查看自己孩子的信息")
        opinions = []
        for opinion in self.data["opinions"]:
            if opinion["child_id"] != child_id:
                continue
            item = dict(opinion)
            item["author"] = self._staff_ref(opinion["author_id"])
            opinions.append(item)
        notices = [dict(n) for n in self.data["notices"] if n["child_id"] == child_id]
        followups = [dict(f) for f in self.data["followups"] if f["child_id"] == child_id]
        return {
            "child": {"id": child["id"], "name": child["name"]},
            "opinions": opinions,
            "notices": notices,
            "followups": followups,
        }

    def regulator_view(self):
        """监管端：只输出脱敏后的质量统计，不含任何儿童或员工身份信息。"""
        self._require_any_consent(PURPOSE_QUALITY_STATS)
        events = list(self.data["events"].values())
        now = _parse_time(self._now())
        on_time = 0
        for event in events:
            review_tasks = [t for t in self.data["pending_tasks"]
                            if t["event_id"] == event["id"] and t["kind"] == TASK_REVIEW]
            if not review_tasks:
                continue
            task = review_tasks[0]
            if task["status"] == "done" and _parse_time(task["resolved_at"]) <= _parse_time(task["due_at"]):
                on_time += 1
        open_followups = [f for f in self.data["followups"] if f["status"] == "pending"]
        overdue_followups = [f for f in open_followups if _parse_time(f["due_at"]) < now]
        open_improvements = [i for i in self.data["improvements"] if i["status"] == "open"]
        overdue_improvements = self.overdue_improvements()
        return {
            "children_total": len(self.data["children"]),
            "observations_total": len(self.data["observations"]),
            "events": {
                "total": len(events),
                "open": sum(1 for e in events if e["status"] == EVENT_OPEN),
                "escalated": sum(1 for e in events if e["status"] == EVENT_ESCALATED),
                "medical": sum(1 for e in events if e["status"] == EVENT_MEDICAL),
                "closed": sum(1 for e in events if e["status"] == EVENT_CLOSED),
            },
            "review": {
                "tasks_total": len([t for t in self.data["pending_tasks"] if t["kind"] == TASK_REVIEW]),
                "on_time": on_time,
            },
            "followups": {"pending": len(open_followups), "overdue": len(overdue_followups)},
            "improvements": {"open": len(open_improvements), "overdue": len(overdue_improvements)},
        }

    def _require_any_consent(self, purpose):
        if not any(purpose in c["consents"] for c in self.data["children"].values()):
            raise ConsentMissingError(f"没有任何监护人同意用途 {purpose}，统计暂不可用")

    def trace_event(self, event_id):
        """管理者视角：从一次处置还原完整链条。

        覆盖最初观察、升级理由、专业结论、授权签署及关闭依据。
        """
        event = self._event(event_id)
        child = self._child(event["child_id"])
        observations = [dict(o) for o in self.data["observations"]
                        if o["id"] in event["observation_ids"]]
        opinions = []
        for opinion in self.data["opinions"]:
            if opinion["event_id"] == event["id"]:
                item = dict(opinion)
                item["author"] = self._staff_ref(opinion["author_id"])
                opinions.append(item)
        medications = [dict(m) for m in self.data.get("medications", [])
                       if m["event_id"] == event["id"]]
        followups = [dict(f) for f in self.data["followups"] if f["event_id"] == event["id"]]
        improvements = [dict(i) for i in self.data["improvements"] if i["event_id"] == event["id"]]
        tasks = [dict(t) for t in self.data["pending_tasks"] if t["event_id"] == event["id"]]
        owner = self._staff_ref(event["owner_id"]) if event["owner_id"] else None
        return {
            "event": dict(event),
            "child": {"id": child["id"], "name": child["name"]},
            "owner": owner,
            "observations": observations,
            "opinions": opinions,
            "medications": medications,
            "followups": followups,
            "improvements": improvements,
            "tasks": tasks,
            "timeline": list(event["timeline"]),
        }
