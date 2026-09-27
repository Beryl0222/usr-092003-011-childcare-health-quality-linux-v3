# 托育医育质量闭环

服务用于连接托育日常照护与专业健康支持，把日常观察、筛查、医疗判断
和跟进回访组织为一条有边界、可追溯、不丢事项的质量闭环。

## 责任边界

三类健康意见各有唯一合法来源，系统按角色强制：

| 意见 | 来源角色 | 动作 |
| --- | --- | --- |
| 建议观察 | 保育员 | 连续记录日常观察 |
| 调整饮食 | 健康管理员（须具筛查资质） | 组织筛查、提出非医疗处置 |
| 需要就医 / 用药处置 | 获授权医生 | 医疗判断，医生签署 |

- 资料使用受监护人同意的用途限制（照护、筛查、医疗、质量上报、家长通知）；
  未授权或已撤销同意的用途一律拒绝。
- 家长端只看到本人孩子的建议、通知与回访，每条意见标注来源角色与人员；
  监管端只接收脱敏质量统计，不含姓名与身份标识。

## 事件闭环

- 保育员发现异常即开启事件并连续追加观察；重复上报按
  “同儿童 + 当天 + 摘要指纹”自动合并为同一事件。
- 异常出现即设定复核时限；升级记录理由并重定时限，转介须指定对象，
  换班须把责任交接给明确接手人。
- 关闭事件须有医生专业结论或已转介依据，且无未完成健康改进项；
  改进项长期未落实会出现在逾期清单中。
- 管理者可从一次处置还原：最初观察 → 升级理由 → 专业结论与签署 →
  转介 → 关闭依据。
- 使用 `--data` 落盘后，换班或服务重启，未完成事项（逾期复核、
  逾期改进）仍可通过待办接口取回。

## 运行与验证

- 配置核对：`python3 service.py --check`
- 启动服务：`python3 service.py --port 8000 [--data data.json]`
- 运行测试：`python3 -m unittest discover -s tests -v`

## 主要接口

- `GET /health`：服务身份
- `POST /v1/children`、`/v1/staff`、`/v1/consents`：建档与同意登记
- `POST /v1/incidents/report`：异常上报（重复自动合并）
- `POST /v1/incidents/{id}/{observations|screenings|medical|advice|escalate|reviews|referral|handover|followups|improvements|close}`
- `GET /v1/parents/{guardian_id}/children/{child_id}`：家长视图
- `GET /v1/regulator/quality`：监管脱敏质量信息
- `GET /v1/incidents/{id}/trace`：管理者全链路追溯
- `GET /v1/pending`：未完成/逾期事项（换班、重启后使用）
