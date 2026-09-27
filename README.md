# 托育医育质量闭环

服务用于连接托育日常照护与专业健康支持，把“建议观察 / 调整饮食 / 就医建议”
三种说法的**出处、签署人、跟进责任**固定下来，避免医育结合便利变成责任不清。

## 职责边界

| 角色 | 可以做 | 不能做 |
| --- | --- | --- |
| 保育员 | 记录日常观察、提出“建议观察” | 组织筛查、调整饮食、任何医疗判断 |
| 健康管理员（须持筛查资质） | 组织筛查、提出“调整饮食”、复核与转介跟进 | 出具医疗结论或用药处置 |
| 医生（须获医疗授权） | 医疗判断、用药处置（均须本人签署） | — |

医疗判断与用药处置**只能由获授权医生签署**；没有签名的医疗操作会被拒绝。

## 闭环规则

- **连续记录**：日常观察按儿童持续累积；异常观察自动开启事件，同儿童同症状的
  重复上报合并为同一事件，不重复生成待办。
- **复核时限**：异常开启时默认 24 小时复核期限，升级时记录升级理由、转介对象
  （健康管理员 / 医生 / 医院）和交接责任人。
- **待办不丢失**：复核、转介、回访、健康改进统一进待办台账；换班交接把交班人
  名下未完成事项整体移交；服务重启后从数据文件恢复，未完成事项不会消失。
- **关闭有据**：复核与转介待办完成后才能关闭；进入医疗阶段的事件只能由授权
  医生关闭，关闭须写明依据。
- **长期未落实可发现**：健康改进事项长期未落实（默认 30 天）会在管理端列出。
- **同意用途限制**：数据使用分为日常照护、筛查、医疗处置、家长告知、脱敏质量
  统计五类用途；监护人未同意的用途会被阻止（家长告知未同意则不生成通知）。
- **三类视图**：
  - 家长端：仅本人孩子的建议（含出具人角色）、通知、回访；
  - 监管端：只接收脱敏质量统计，不含任何儿童或员工身份信息；
  - 管理端：从一次处置还原最初观察 → 升级理由 → 专业结论与授权签署 → 关闭依据
    的完整时间线，并可查看待办台账与长期未落实改进。

## 运行

```bash
python3 service.py --check          # 配置自检
python3 service.py --port 8000      # 启动服务（默认数据文件 careloop-data.json）
python3 -m unittest discover -s tests -v
```

启动后访问 `GET /health` 做运行巡检。数据通过 `--data` 或环境变量
`CARELOOP_DATA` 指定落盘文件。

## 主要接口

除健康检查与视图外均为 `POST`，请求体为 JSON。

- `POST /staff`、`POST /children`、`POST /consents/grant`、`POST /consents/revoke`
- `POST /observations`（`abnormal=true` 开启/合并异常事件）
- `POST /screenings`（仅限持资质健康管理员）
- `POST /opinions`（`kind`: `observe` / `diet` / `medical`，医疗意见须签名）
- `POST /escalations`（升级：理由、`refer_to`、`handover_to`、复核时限）
- `POST /medical-conclusions`、`POST /medications`（仅授权医生，须签名）
- `POST /events/claim`、`POST /reviews/complete`、`POST /referrals/complete`、
  `POST /events/close`
- `POST /followups`、`POST /followups/complete`
- `POST /improvements`、`POST /improvements/complete`
- `POST /handovers`（换班交接）

视图：

- `GET /parent/child?guardian_id=…&child_id=…`
- `GET /regulator/quality`
- `GET /manager/event?event_id=…`（事件全链条还原）
- `GET /manager/pending[&owner_id=…]`
- `GET /manager/improvements/overdue[&min_days=30]`
- `GET /journal?child_id=…`（连续日常观察）

越权返回 403、缺字段或参数错误返回 400、对象不存在返回 404、状态冲突返回 409，
响应体形如 `{"error": "…"}`。
