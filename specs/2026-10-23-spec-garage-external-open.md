# 车库控制：外部开门授权与人工接管收窄 — 规格

日期：2026-10-23。状态：**待用户评审**。对应 [req](2026-10-23-req-garage-external-open.md)。
关联：[原 spec](2026-10-15-spec-garage-control.md) S6/S7、[原 req](2026-10-15-req-garage-control.md) REQ-9/REQ-10。

## 代码证据

| 阅读对象 | 位置 | 与本批相关的事实 |
| --- | --- | --- |
| 外部命令分类 | `garage_control.py:142-155` | `external_command()` 只在 `direction=='open' and phase in ('opening','return_opening')` 时返回；`stop_cover` 在「静止 + 可信全闭 + 无 pending/operations + 无 pause/closing + phase ∈ idle/outside_closed/waiting_close_permission/blocked/manual_hold」时返回；**其余一律** `fault('manual_override', manual=True)` |
| 调用者识别 | `garage_runtime.py:81,181-182,452` | `_contexts` 是运行时自己发起的 64 条 context 环形记录；`_service_event()` 用 `event.context.id in self._contexts` 过滤自身调用。外部自动化与 UI 都拿不到这个身份 |
| 门命令拦截名单 | `garage_runtime.py:455` | `open_cover/close_cover/stop_cover/set_cover_position/toggle` 五种 |
| 通行许可推导 | `door_evidence.py:44-52` | `passage_allowed` 依赖 `open.reached`；`cover_available` 为假时返回 `None`；`direction=='close'` 返回 `False` |
| 计时开门需要本控制器的命令 | `door_evidence.py:258-270` | `_estimate()`：`self._command is None or command.direction != direction` → `command_required`；否则未满 `travel+margin` → `travel_in_progress`；满后放行 |
| 已有受控关闭入口 | `garage_control.py:422-450` | `async_request_close()`：refresh → `_check` → `_preflight('close')` → `close_allowed` → `session.can_close` → `_acquire` → 开门/关门 → `_wait` → `phase='idle'` → `persist` → `_release` |
| 关闭等待的就绪判据 | `garage_control.py:277-289` | `direction=='open'` 时 `ready = passage_allowed is True`；`close` 时看 `endpoint.reached` + quality + `allow_estimated` |
| 服务注册 | `services.py:46-81` | `async_register_platform_entity_service`，`entity_domain=VACUUM_DOMAIN`，`func="async_request_garage_close"`，`supports_response=SuppressesResponse.NONE` |
| vacuum 侧实现 | `vacuum.py:283-294` | `async_request_garage_close()` → `runtime.async_request_close()`；`GarageControlError`/`GarageTaskError` 统一转 `ServiceValidationError(translation_key='garage_control_error', reason=...)` |
| 运行时包装 | `garage_runtime.py:402-406` | `async_request_close()` 先查 `_first_install`，再委托控制器，最后 `publish()` |
| 事件发布与去重 | `garage_runtime.py:419-436` | `notify(reason)` 以 `(session_id, reason)` 为键去重；边沿变化时先发 `active=False` 再发 `active=True` |
| 相位集合 | `garage_entities.py:15-20` | 15 个相位，含 `idle / outside_closed / waiting_close_permission / blocked / manual_hold` |
| 故障可见性 | `garage_entities.py:104-111` | `garage_problem` 在 `reason` 非空或 phase ∈ (blocked, manual_hold) 时为 on |
| 既有豁免回归 | `tests/test_garage_closed_stop.py` | 54 项，含「stop 不洗白既有保持」与「运动中 stop 仍锁存」 |
| 现场证据 | 本批 req 的「起因」 | 两次 `previous_phase=idle` + 无活动任务；`context_user_id=seno` 证明 UI 操作同样被拦 |

**现有代码限制**（写规格前已确认，不靠猜测）：

1. `external_command()` 没有「有无冲突对象」的概念，只有「谁发的」。
2. 没有 `request_garage_open`；`async_request_close` 的 `session.can_close` 前置在开门方向上没有对应物，
   且开门方向不能复用 `can_close`（会要求 `known_idle` 且 `dock_stable`，而开门本身就发生在需要通行的时刻）。
3. `_wait('open')` 依赖 `passage_allowed is True`，而 `passage_allowed` 依赖 `open.reached`；
   `open.reached` 在 `timed` 来源下**必须**有本控制器的 `_command` 才会进入估算。因此开门必须
   `note_command('open')` 之后才可能等到 `True`——这与 `async_request_close` 的做法一致。

---

## S11.1 外部命令分类（REQ-18）

把 `external_command()` 的判决从「调用者身份」改为「冲突对象」。

新增私有判定 `_no_conflict_for_external(direction, door_moving)`，**全部为真**才放行：

| 条件 | 取值来源 | 为什么需要 |
| --- | --- | --- |
| `direction in ('open', 'close', 'stop')` | 入参 | `set_cover_position` / `toggle` 归为 `'move'`，**永不**豁免（原 spec 明文） |
| `not self.reason` | 控制器 | 既有故障/人工保持不自动解除，也不被外部动作洗白 |
| `not self._closed` | 控制器 | 卸载后不接受任何判决 |
| `not self._pending` | 控制器 | 有排队中的门动作时，外部命令会与之竞争 |
| `not self._operations` | 控制器 | 有在途 operation（含安全暂停）时同理 |
| `not self._pause_session` | 控制器 | 返航暂停流程中不许插入外部动作 |
| `not self._closing_for_exit` | 控制器 | 离库关门流程中同理 |
| `self.phase in ('idle', 'outside_closed', 'waiting_close_permission')` | 控制器 | 只在这三个「无流程在跑」的相位放行；`task_open`/`returning`/`docked_intermission`/`opening`/`closing` 等一律锁存 |
| 方向不是 `stop` 时：`not door_moving` | `garage_runtime.py:479` 传入的 cover 状态 | 门正在 `opening`/`closing` 时，外部 open/close 会与物理行程冲突 |
| `self.state is not None and self.state.cover_available` | 门证据 | 反馈不可用时无法判断有没有冲突，按失败安全锁存 |
| 两个端点都无 `endpoint_conflict` / `feedback_unavailable` / `restored_snapshot` / `invalid_clock` / `invalid_configuration` | 门证据 | 与 `_preflight` 同一份拒绝集合，避免「证据本身不可信还谈无冲突」 |

放行时：
- **不**调用 `fault()`，**不**改 `phase`、`operation_id`、`reason`
- `_LOGGER.info('Garage control observed external %s with no conflict: ...')`，含 phase、
  门状态、`fresh`、`trusted`、`outside`、session 摘要
- 由 `_service_event()` 既有的 `publish()` 负责刷新实体

拒绝时：保持原 `fault('manual_override', manual=True)` 一字不改。

**为什么 `'move'` 永不豁免**：`set_cover_position` 与 `toggle` 的物理效果取决于当前门位，
没有「无冲突」这一说——同一个数值在不同起点上可能是开门也可能是关门。原 spec
（`2026-10-15-spec-garage-control.md:290`）已明文禁止借用停止豁免，本批沿用同一原则。

## S11.2 豁免不授予通行（REQ-19）

`_no_conflict_for_external` 成功时不触碰 `self.evidence`，因此：

- `_command` 仍为 `None`（或仍属于此前本控制器发出的命令）
- `timed` 来源下 `_estimate()` 继续返回 `command_required`
- `passage_allowed` 对 `timed` 来源仍为 `unknown`

**回归要求**：外部开门被豁免后，紧接的 `vacuum.clean_area` 仍会走完整开门流程
（发 `open_cover` 并等待 45 秒），不得因为「门已经开着」而跳步。这一条必须在
隔离测试里断言门命令次数为 2（外部一次 + 清扫一次）。

## S11.3 受控开门服务（REQ-21 / REQ-22）

### 服务契约

| 项 | 值 |
| --- | --- |
| 名称 | `roborock_plus.request_garage_open` |
| 目标 | `target: entity: [{integration: roborock_plus, domain: [vacuum]}]` |
| 参数 | 无（`schema=None`） |
| 响应 | `SupportsResponse.NONE`，与 `request_garage_close` 一致 |
| 失败 | `ServiceValidationError`，`translation_key='garage_control_error'`，`reason` 为稳定原因码 |

### 控制器实现 `async_request_open()`

```
refresh()
_check()                       # control_unloaded / 既有 reason / operation_cancelled
_preflight('open')             # endpoint_conflict / feedback_unavailable / restored_snapshot / invalid_*
if passage_allowed is True:  # 已经可通行：幂等短路，不发任何设备命令
    phase 保持当前值；persist()；return
if not door_observation()['cover_available']:  raise 'feedback_unavailable'
operation = _acquire()         # operation_in_progress 由 _acquire 抛
try:
    phase = 'return_opening' if self._pause_session else 'opening'
    evidence.invalidate('opening')
    await move_door('open', operation)
    _check(operation)
    evidence.note_command('open', clock(), operation)
    await _wait('open', operation)   # 满 travel+margin，或 confirmed 端点更早到达
    phase = 'idle'
    await persist()
except CancelledError:
    fault('operation_cancelled'); raise
except Exception as err:
    if not self.reason: fault(getattr(err,'reason',None) or 'command_failed')
    raise
finally:
    _release(operation)
```

与 `async_request_close()` 的差异（有意为之，逐条说明）：

| 关闭 | 开门 | 理由 |
| --- | --- | --- |
| `close_allowed()` 前置 | **不查** | 家庭许可只限制关闭（原 REQ-9），开门不受其约束 |
| `session.can_close` 前置 | **不查** | `can_close` 要求「known_idle + dock 稳定」，而开门最常发生在「要出门」的时刻；改成不依赖会话，由 `_acquire()` 与 phase 判据共同保证不与任务流程并发 |
| `phase='idle'` | `phase='idle'` | 开门属于「无任务的人工通行」，结束后回到 idle；不伪造 `task_open` |

### 运行时与实体

- `garage_runtime.py` 新增 `async_request_open()`，与 `async_request_close()` 对称：
  先查 `_first_install`（→ `session_recovery_required`），委托控制器，`publish()`。
- `vacuum.py` 新增 `async_request_garage_open()`，异常转换与 `async_request_garage_close` 完全一致。
- `services.py` 注册 `REQUEST_GARAGE_OPEN_SERVICE_NAME`。
- `services.yaml` 增补条目，描述说明「无 force、走同一套守卫、不影响家庭关闭许可」。
- `strings.json` + `translations/en.json` 增补服务名/描述，与关闭条目风格一致。

### 拒绝原因（沿用既有码，不新增）

`control_unloaded`、`operation_in_progress`、`feedback_unavailable`、`endpoint_conflict`、
`restored_snapshot`、`invalid_configuration`、`invalid_clock`、`open_timeout`、
`operation_cancelled`、`command_failed`、`manual_override`（既有锁存先于本次请求）、
`session_recovery_required`。

**不新增原因码**：本批没有新的失败语义，全部复用既有集合（原 REQ-12 的「最少原因」不变）。

## S11.4 折叠门联动自动化（REQ-23）

`automation.garage_door_sync_folding_door`（unique_id `1746705574425`）保持：

- 触发器不变：`binary_sensor.folding_door_contact_state` `off → on`
- 目的不变：折叠门打开 → 车库门到通行位
- `mode: single` 不变

动作从 `cover.stop_cover`（当前版本）改为：

```yaml
- action: roborock_plus.request_garage_open
  target:
    entity_id: vacuum.g20s_ultra
```

失败分支用 `continue_on_error: false`（默认）+ 一段 `if` 无法读到响应，因此改为
「先置 `response_variable`」不可行（`SupportsResponse.NONE`）。**采用的做法**：

```yaml
- block:
    - action: roborock_plus.request_garage_open
      target: { entity_id: vacuum.g20s_ultra }
  # 失败会抛 ServiceValidationError，自动化标记为 error
```

并在同一条自动化里保留「门已确认可通行则跳过」的前置 `if`，避免每次折叠门打开都
重复驱动电机：

```yaml
- if:
    - condition: state
      entity_id: binary_sensor.ke_ting_sao_di_ji_garage_passage_allowed
      state: 'on'
  then: []          # 已经可通行，不发命令
  else:
    - action: roborock_plus.request_garage_open
      target: { entity_id: vacuum.g20s_ultra }
```

**不做**：不在自动化里重试。重试属于蓝图层（`retry_window`），本自动化是「人到门口
就该开」的一次性语义；失败让用户在通知里看到。

**告警**：沿用既有模式，新增一条 `script.alert_notify` 分支需要能读到失败原因，
但服务不支持返回响应。因此改为监听集成已发布的 `roborock_plus_garage_issue` 事件
（REQ-20 保证其存在），由**用户自行**决定是否建通知自动化——本批不替用户创建，
与 `automations/README.md` 的既有约定一致。

## S11.5 锁存可见性与通知落脚点（REQ-20 / REQ-20a）

现有 `GarageRuntime.notify(reason)` 已在 `_last_issue` 边沿变化时发
`roborock_plus_garage_issue`。本批**不新增事件机制**，做三件事：

1. 在 `notify()` 的 `active=True` 分支补 `previous_phase` 字段（当前只有 `phase`），
   便于消费方区分「本来就在跑流程时被接管」与「空闲时被接管」。
   `fault()` 已计算 `previous_phase` 但未外露；改为把最近一次的 `previous_phase`
   存到控制器（`self.previous_phase`，运行时不持久化），供 `notify()` 读取。
2. 回归测试断言：`fault(..., manual=True)` → 事件 `active=true`；`reset()` 成功 →
   `active=false`；同一 `(session_id, reason)` 重复 `fault` 不重复发。
3. **新增随附自动化 `automations/roborock_garage_issue_notify.yaml`**（REQ-20a）。

### 新增自动化契约

| 项 | 值 |
| --- | --- |
| 文件 | `automations/roborock_garage_issue_notify.yaml` |
| unique_id | `1791600000001`（新分配，写进 `automations/README.md` 表格） |
| 触发器 | `event: roborock_plus_garage_issue` |
| 条件 | 无（`active=false` 的分支在动作内处理） |
| 动作 | `if trigger.event.data.active` → `script.alert_notify(level=warning, title=柜门控制已接管, message=<见下>)`；`else` → 静默（不通知，避免「已解除」噪音） |
| 禁止 | 任何 `cover.*`、`vacuum.*`、`button.*`、`roborock_plus.reset_garage_control`、`automation.trigger` |

消息模板直接引用事件字段，不做归因：

```
柜门控制已暂停自动流程：{{ trigger.event.data.reason }}
阶段：{{ trigger.event.data.phase }}（此前 {{ trigger.event.data.previous_phase }}）
门：{{ trigger.event.data.cover_entity_id }}
机器人：{{ trigger.event.data.vacuum_entity_id }}

这会阻止后续清扫命令，直到你显式解除（roborock_plus.reset_garage_control）。
本通知不会自动解除，也不会操作门或机器人。
```

**为什么 `active=false` 不通知**：解除是用户自己做的动作（`reset_garage_control`），
再发一条只会制造噪音。这与 `automations/README.md` 既有「不刷告警」的取向一致。

**为什么不是「只在 manual_hold 时通知」**：`blocked`（非人工）同样会让清扫命令被拒
（`feedback_unavailable`、`endpoint_conflict` 等），也必须能看到。两者都发。

**边界**：`session_id` 为 `None` 时（无活动会话）用 `(None, reason)` 作键，与现有一致。

## S11.6 回归与不回归（REQ-24）

必须保持为绿（不得为了让新功能通过而修改断言）：

- `tests/test_garage_closed_stop.py` 全 54 项，特别是：
  - `test_stop_during_a_control_flow_still_holds_even_if_contact_has_not_released`（11 个相位）
  - `test_other_external_commands_never_borrow_the_closed_stop_exemption`（`toggle`/`set_position`/`open`/`close`）
  - `test_explicit_stop_does_not_convert_existing_manual_hold_to_idle`
- `tests/test_garage_runtime.py`、`tests/ha_native_garage*.py` 全量

**注意**：`test_other_external_commands_never_borrow_the_closed_stop_exemption` 里有
`open_cover` 与 `close_cover` 两个参数。本批会让它们在「无冲突」时被豁免，因此该测试
**必须**在「有冲突」的前提下重写（例如把 controller 置于 `task_open`），否则会与新
REQ-18 冲突。这是**有意的行为变更**，需在 plan 里显式记录，并保留等价强度的反例。

## S11.7 验证策略（REQ-25）

分层，全部离线：

| 层 | 工具 | 覆盖 |
| --- | --- | --- |
| 纯控制器 | `tests/test_garage_control.py` 风格的回放 | `_no_conflict_for_external` 的 11 个条件，每个条件单独置假，断言仍锁存 |
| 原生 HA | `tests/ha_native_garage_*.py` | 真实 `call_service` 分发、真实实体状态、`context` 区分 |
| 服务契约 | `tests/ha_native_staged_service_registration.py` 风格 | `request_garage_open` 注册、target 过滤、失败原因 |
| 自动化结构 | `tests/test_folding_door_link.py` | 断言调用的是 `roborock_plus.request_garage_open`，且不含任何 `cover.*` |
| 变异 | 内存内替换实现 | 去掉每个守卫后测试必须变红 |

**不做**：任何会移动门或机器人实体的调用。实物验收由用户执行，步骤写进 plan 的完成记录。

## 需求追踪

| 需求 | 章节 | plan 任务 | 验证 |
| --- | --- | --- | --- |
| REQ-18 | S11.1 | T1, T2 | 11 条件矩阵 + 组合正例 |
| REQ-19 | S11.2 | T2 | 豁免后門命令次数=2 |
| REQ-20 | S11.5 | T3 | 边沿去重、`previous_phase`、解除 |
| REQ-20a | S11.5 | T3b | 自动化存在、结构断言、只读 |
| REQ-21 | S11.3 | T4, T5 | 短路、等待、persist、失败码 |
| REQ-22 | S11.3 | T5 | 许可不拦、锁存拦、反馈/冲突/快照拦 |
| REQ-23 | S11.4 | T6 | YAML 结构断言 |
| REQ-24 | S11.6 | T7 | 原 54 项 + 全量 |
| REQ-25 | S11.7 | T1–T8 | 变异全捕获 |
