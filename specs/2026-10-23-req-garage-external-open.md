# 车库控制：外部开门授权与人工接管收窄 — 需求

日期：2026-10-23。状态：**待用户评审**。类型：已有功能反馈 + 规格遗漏（绑定原
[车库控制需求](2026-10-15-req-garage-control.md) REQ-10 / REQ-9）。
关联：[原 spec](2026-10-15-spec-garage-control.md) S6 / S7、[原 req](2026-10-15-req-garage-control.md)、
本批 [spec](2026-10-23-spec-garage-external-open.md)、[plan](2026-10-23-plan-garage-external-open.md)。

## 起因（实测）

两次真实的清扫被同一机制卡死，日志里都是 `previous_phase=idle` + 无活动任务：

```
2026-10-22 16:08:38  folding_door_contact_state off→on
                     automation.garage_door_sync_folding_door → cover.open_cover
                     WARNING Garage control held for manual handling:
                       reason=manual_override previous_phase=idle phase=manual_hold
2026-10-23 12:48:26  公区打扫 → vacuum.clean_area
                     → Garage control rejected the operation: manual_override
```

```
2026-10-23 14:00:24  cover.open_cover（context_user_id=seno，人在 UI 上操作）
                     → 同上锁存
2026-10-23 14:47:40  公区打扫第 1 次 → manual_override 拒绝
                     14:54 / 15:00 / 15:07 / 15:13 四次重试均被拒
                     15:20:11 蓝图发出「30 分钟内未能启动清扫」告警
```

判决原文（`garage_control.py:142-155`）：

```python
def external_command(self, direction, *, door_moving=False):
    if direction == 'open' and self.phase in ('opening', 'return_opening'):
        return                      # 只有"我自己正在开门"才放过
    ...
    self.fault('manual_override', manual=True)
```

判据是「**谁发的**」，不是「**有没有冲突对象**」。`_contexts` 只记录运行时自己发起的
调用，外部自动化与 UI 操作都拿不到这个身份，`open/close/stop/set_position/toggle`
五个服务全在拦截名单。

## 现有行为的三处后果

| # | 后果 | 代码证据 |
| --- | --- | --- |
| 1 | 无任务、无待执行操作、门静止时，任何外部门命令照样锁存 | `garage_control.py:155` 无条件 `fault(..., manual=True)` |
| 2 | 没有受控开门通道，外部自动化只能直接发 `cover.open_cover` | `services.py` 只有 `request_garage_close`，无 open 对称物 |
| 3 | 直接开门后 `passage_allowed` 仍为 `unknown` | `door_evidence.py:264-268`：`open_source=timed` 且无本控制器命令 → `command_required` |

后果 3 意味着：即便绕过了后果 1，外部开门也不会被集成承认为「可通行」，下一次清扫
仍会重新开门并再等一次 45 秒，门的电机被无谓驱动两次。

## 用户故事

- 作为家庭成员，我打开柜门/折叠门时，车库门应当由电机打开到可通行位，而不是只
  松开离合让我手拉。
- 作为扫地机主人，我在没有清扫任务时动过门，不应该导致下一小时的定时清扫被拒绝，
  更不应该在拒绝时毫无提示。
- 作为维护者，我要能解释清楚「什么情况下集成会和站在门口的人抢门」——那个集合应当
  由冲突对象定义，而不是由调用者身份定义。

## 功能需求

| 编号 | 需求 | 验收标准 |
| --- | --- | --- |
| REQ-18 | 外部命令按冲突对象分类 | 仅当**同时**满足：控制器 `phase` 在 `idle` / `outside_closed` / `waiting_close_permission` 之一、无活动任务、无 pending、无在途 operation、无既有 `reason`、门可用且未被报告为运动时，外部 `open`/`close` 只记录并发布事件，**不锁存**。其余一切情形（任务中、门在动、有待执行操作、反馈不可信、既有故障/保持）保持原锁存语义 |
| REQ-19 | 收窄不授予通行 | 被豁免的外部开门**不得**产生 `passage_allowed=True`。`command_required` 语义不变：没有本控制器的命令就没有计时起点，不做行程估算 |
| REQ-20 | 锁存必须可见 | 每次进入 `manual_hold` / `blocked` 都要发布 `roborock_plus_garage_issue`（`active=true`），解除时 `active=false`；去重与既有实现一致。事件须带 `previous_phase`，使消费方能区分「空闲时被接管」与「流程中被接管」 |
| REQ-20a | 锁存必须有通知落脚点 | 仓库随附一条原生自动化：订阅 `roborock_plus_garage_issue`，仅在 `active=true` 时经 `script.alert_notify` 发 warning（含 reason、phase、previous_phase、门与机器人状态）；`active=false` 只发 info 或静默。该自动化不得自行 reset、不得驱动门或机器人 |
| REQ-21 | 受控开门服务 | 新增 `roborock_plus.request_garage_open`，目标 vacuum，无 force。语义对称于 `request_garage_close`：重查全部守卫 → 门已确认可通行则直接返回 → 否则开门 → 等待完整行程+余量 → 记录命令（`passage_allowed` 因此可变为 true）→ `persist` |
| REQ-22 | 开门守卫与关闭对称 | 家庭许可只限制关闭，不阻止明确开门；无活动任务时也允许明确开门（与 REQ-9 的 idle 关门对称）；既有 `manual_hold`/`fault` 仍阻止，需先 `reset_garage_control`；反馈不可用/端点冲突/恢复快照同样拒绝 |
| REQ-23 | 折叠门联动改用受控通道 | `automation.garage_door_sync_folding_door` 的触发与目的不变（折叠门打开 → 车库门开到通行位），动作改调 `roborock_plus.request_garage_open`；不得再直接调用任何 `cover.*`；失败时告警并说明原因 |
| REQ-24 | 不回归既有安全语义 | 运动中 stop、任务中外部相反命令、`toggle`/`set_position` 不借用任何豁免、既有保持不自动解除、`reset` 不发设备命令——全部保持原判，并有回归证明 |
| REQ-25 | 验证不触发设备 | 全部用隔离 HA 原生测试与控制器回放；变异检查覆盖每条新守卫；实物验收由用户执行 |

## 通知自动化的边界（REQ-20a）

用户 2026-10-23 明确要求把「锁存必须通知」纳入本批。设计边界：

- **只读**：`reset_garage_control` 不由这条自动化调用。解除仍须用户显式决定 —— 自动解除
  会奖励「在任务中动门」，正是本批要避免的行为。
- **只报**：不调用任何 `cover.*`、`vacuum.*`、`button.*`。
- **可停用**：与其它随附自动化一样，用户可随时停用；仓库只提供版本记录，不自动同步。
- **不重复**：依赖集成的 `(session_id, reason)` 边沿去重，不在自动化和集成两层重复去重。
- **不猜原因**：文案直接引用事件字段，不做归因推断。

## 明确非目标

- 不做「跨调用者白名单」或按自动化名称放行。原 spec 已否决，本批不变。
- 不做新的持久化结构。豁免判定只用运行时事实。
- 不把 `manual_hold` 改成自动解除（A2 方案已评估并放弃：需要新状态、定时器和持久化，
  且奖励「在任务中动门」这一要避免的行为）。
- 不把「电机开门」推广到任意 cover；只对已启用原生车库控制的绑定生效。
- 不改家庭许可的语义（只限制关闭）。
- 不自动部署到 HA，不发 HACS 发布。

## 界面约束

- 无新增界面。`request_garage_open` 与既有服务一样出现在「开发者工具 → 动作」列表，
  名称/描述走 `strings.json` + `translations/en.json`，中英一致。
- 不改 Options Flow 的步骤、字段或分组。
- 仓库无 `.agents/rules/frontend-architect.md`，本批无前端改动，不适用。

## 需求追踪

| 需求 | spec | plan | 验证 |
| --- | --- | --- | --- |
| REQ-18 | S11.1 | T1/T2 | 五类豁免条件各自单独反例 + 组合正例 |
| REQ-19 | S11.2 | T2 | 豁免后 `passage_allowed` 仍非 true；下次清扫仍先开门 |
| REQ-20 | S11.5 | T3 | 边沿去重、解除事件、既有去重不重复 |
| REQ-20a | S11.5 | T3b | 自动化结构：只读、无 reset、无 cover/vacuum 调用、含 alert_notify |
| REQ-21 | S11.3 | T4/T5 | 已通行短路、全行程等待、失败原因、persist |
| REQ-22 | S11.3 | T5 | 许可/保持/反馈/冲突/快照各守卫反例 |
| REQ-23 | S11.4 | T6 | 自动化 YAML 结构断言 + 变更为受控调用 |
| REQ-24 | S11.6 | T7 | 原 54 项 closed_stop + 全量回归 + 变异 |
| REQ-25 | S11.7 | T1–T8 | 无设备动作；变异全捕获；实物待用户 |
