# 受控出库与车库门全流程托管 — 规格

日期：2026-10-25。状态：**待用户评审**。
对应 [req](2026-10-25-req-controlled-exit.md)。
关联：[原 spec](2026-10-15-spec-garage-control.md) S5/S6、[v0.8.1 spec](2026-10-25-spec-garage-exit-close.md) S12。

## 代码证据

| 阅读对象 | 位置 | 事实 |
| --- | --- | --- |
| goto 参数是坐标 | `vacuum.py:407-408` | `await self.send(RoborockCommand.APP_GOTO_TARGET, [x, y])` |
| goto 属新任务 | `garage_runtime.py:27-29` | `app_goto_target` 在 `_NEW_TASK_COMMANDS`，与 `app_start` 同级 |
| 活动会话拒绝新任务 | `garage_control.py:473-474` | `raise GarageControlError('task_already_active')` |
| **清扫前要求门可通行** | `garage_control.py:483-484` | `if self.passage_allowed is not True: raise feedback_unavailable` ← **本批要修的前提** |
| goto 终态判定 | `garage_task.py:358-359` | `arrived = task_kind=='goto' and started and latest_quiet and state=='idle'` |
| goto 结束后可开新任务 | `garage_task.py:191-192` | `terminal_goto` 使 `can_begin` 为真 |
| 关门入口守卫 | `garage_control.py:518-527` | `async_request_close` 要求 `session.can_close` |
| `can_close` 定义 | `garage_task.py:197` | `known_idle and not active and _dock_stable` |
| 出库关门判据 | `garage_control.py:603-610` | `_exit_can_close()` 要求 `state in _CLEANING_STATES` |
| 位置顶替 | `v1_position_trust.py:147-151` | docked 且与充电座不符 → 用充电座坐标替换，`trusted=True` |
| **顶替保护** | `garage_runtime.py:151-152` | 要求充电座标记落在危险区内才信任 |
| 位置服务 | 实测 | `get_vacuum_current_position` 返回地图坐标与 `position_source` |
| 硬编码充电座 | `safe_zone.py:10-11` | `DEFAULT_DOCK_X/Y = 25500`，仅几何原点 |
| 回洗/返航流程 | `garage_control.py:692-733` | `_return_through_door()`：pause → open → resume，已实现 |
| 回洗保持开门 | `garage_control.py:924-932` | `docked_intermission` 保持通行，已实现 |
| 任务结束关门 | `garage_control.py:917-923` | `session.ended and can_close` → `async_request_close()`，已实现 |
| 遗留守卫 | `vacuum.py:481-491` | 配了新控制后 `is_garage_configured` 直接 return，旧路径是死代码 |
| 配置校验入口 | `garage_config.py:111-141` | `_validate_supplied_fields` |

**实测几何**：停靠 `(25696, 28439)`；危险区 `x 24997–26069 / y 27123–28764`；
出库方向 `-y` 需 1316 mm；右侧 `+x` 仅 373 mm。

---

## S14.1 配置（REQ-32 / REQ-37）

绑定级新增四字段，默认关闭：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `controlled_exit_enabled` | bool | `False` | 总开关 |
| `controlled_exit_x` | int | `25696` | 安全点 x |
| `controlled_exit_y` | int | `26500` | 安全点 y |
| `controlled_exit_timeout_seconds` | int | `120` | goto 到到达的超时 |

校验加在 `_validate_supplied_fields`：

```
controlled_exit_enabled 必须是 bool（沿用既有 bool 检查）
controlled_exit_x / _y 必须在 0..65535
controlled_exit_timeout_seconds 必须在 10..300
启用时三个数值必须显式存在；缺任一 -> GarageConfigError('missing_controlled_exit_target', <key>)
```

**为什么启用时必须给全**：否则「默认 x 配用户 y」这种没人验证过的组合会悄悄生效。
默认值只用于表单预填。

**为什么可配置**：任何柜体、任何充电座位置都可能复现。写死一座房子的坐标等于把私有几何塞进通用集成。

## S14.2 插入条件（REQ-33）

插入点在 `GarageRuntime.async_send`，覆盖**全部真实清扫命令**
（`app_start` / `app_segment_clean` / `app_zoned_clean` / `app_spot`），四条件全真才插入：

```
binding['controlled_exit_enabled'] is True
and name in ('app_start','app_segment_clean','app_zoned_clean','app_spot')
and not self.session.active
and self.controller.phase in ('idle','outside_closed')
and self.controller.reason is None
```

`session.active` 为真是唯一可靠的「不是全新任务」判据。回洗后继续走 `app_resume`；
但用户中断再点开始仍会来清扫命令，此时 session 可能仍 active，也必须拒绝插入。

**不满足时**：不报错、不插入，完全走 0.8.1 原路径。

## S14.3 第 2–3 步：开门与 goto（REQ-34 / REQ-36）

```
2. await self._open(operation)                  # 既有开门路径，已可通行则幂等
3. 发 app_goto_target(controlled_exit_x, controlled_exit_y)
   phase = 'controlled_exit'
   _controlled_exit_deadline = clock() + timeout
```

`async_update` 每 tick 推进：

```
若 not (robot.fresh and robot.position_trusted): 不算到达，继续等
若 hypot(pos.x - target_x, pos.y - target_y) <= 400: 到达
若 clock() >= deadline: fault('controlled_exit_failed')
```

**到达容差 400 mm**：机器人停靠精度非零，要求精确到点会永远超时；400 mm 小于机身尺寸，仍能保证在目标附近。

**为什么用坐标**：目标点已在危险区外 747 mm；「刚出区」正是可能还在蹭门的位置。坐标给的是「到了我说的地方」。
`goto` 自身结束还要 `state=='idle'`（`garage_task.py:358`），本批额外要求坐标容差，两者都由 `async_update` 观测。

**失败**：`fault('controlled_exit_failed')`，不发清扫命令，发 `garage_issue`。不自动重试。

## S14.4 第 4–5 步：关门与发清扫命令（REQ-38 / REQ-39 / REQ-40）

### 第 4 步：到点关门

新增 `_close_after_controlled_exit()`，**不复用** `async_request_close()`（要求 `session.can_close`
= 稳定停靠）也**不复用** `_close_after_departure()`（`_exit_can_close()` 要求 `_CLEANING_STATES`）。

自己的判据，全真才发关门命令：

```
robot.fresh and robot.position_trusted
and outside_danger_zone is True
and state.cover_available
and {state.open.reason, state.close.reason} 与 BLOCKING_ENDPOINT_REASONS 无交集
and close_allowed()
and not self._pending and not self._operations
```

与 `after_exit` 的前提本质相同（机器人确实在区外、反馈可信、无并发），差别只在触发时机。

**失败**：`fault('controlled_exit_close_failed')`，**不发清扫命令**，发 `garage_issue`（REQ-38，用户明确要求）。

### 第 5 步：发清扫命令时跳过门判据

**本批最核心的修正（REQ-39）**。`async_execute()` 现在无条件要求通行：

```python
if self.passage_allowed is not True:
    raise GarageControlError('feedback_unavailable')
```

改为按位置分支：

```python
robot_outside = (
    self.robot.fresh
    and self.robot.position_trusted
    and self.robot.outside_danger_zone is True
    and not self.robot.docked
)
if not robot_outside:
    await self._open(operation)
    self.refresh()
    self._check(operation)
    if self.passage_allowed is not True:
        raise GarageControlError('feedback_unavailable')
# robot_outside 为真：机器人已在柜门外，门关着是对的，不需要通行
```

**为什么安全（REQ-40）**：`robot_outside` 依赖 `position_trusted` 与 `outside_danger_zone`，
而后者在 `garage_runtime.py:151-152` 已要求充电座标记落在危险区内才信任顶替坐标。
本屋充电座 `(25500, 25500)` 不在区内，顶替值不会被采信 —— 停靠时不可能误判「已在区外」。

若某配置的充电座恰在危险区内，该守卫会让 `outside_danger_zone` 保持未知，
`robot_outside` 为假，退回原逻辑（要求开门）。**失败方向是保守的。**

**适用范围**：这条分支对所有 `new_task` 与 `resume` 命令生效——机器人已在门外时，
任何清扫/恢复命令都不需要先开门。这正是第 6.6 步「机器人自主出库后继续清扫」所需的语义。

## S14.5 出库期间禁止关门（REQ-35）

`_update_after_exit()` 最前面短路：

```python
if self.phase == 'controlled_exit':
    # The robot is driving out of the cabinet; closing now would trap it.
    return True
```

**唯一改动 `after_exit` 核心路径的地方**，用户明确接受的取舍：门多开约 20–40 秒。

**为什么短路 phase 而不是推迟 `_exit_due`**：`_exit_due` 是待办标记，推迟它还要记住
「为什么推迟、何时恢复」，多两个状态就多两处可错。

## S14.6 关于遗留守卫（本批不动）

`vacuum.py:481-491` 的 `async_guard_garage_open` 在配置了新车库控制后直接 `return`，
是死代码。`garage_guard.py` 的 `DoorEchoWindow`、45 秒等待同样不在生效路径上。

**本批不删除**，理由：删除是独立的重构决策，会扩大本批风险面；且无法确认是否
有其他部署仍走旧配置。若后续确认单用户，可另开批次清理。

## S14.7 可恢复（REQ-41）

两个新原因码：
- `controlled_exit_failed`（goto 或到达失败）
- `controlled_exit_close_failed`（到点关门失败）

均经既有 `fault()` 路径，`reset_garage_control` 按既有逻辑复核后可清除。无永久锁存。

## S14.8 不回归（REQ-42）

必须保持绿：

- `tests/test_garage_exit_close.py`（12）
- `tests/test_garage_external_close.py`（9）
- `tests/test_garage_external_open.py`（31）
- `tests/test_garage_closed_stop.py`（56）
- `tests/test_garage_after_exit.py`（26）

特别验证第 6.1–6.7、8、9 步（回洗 pause/open/resume、`docked_intermission` 保持开、
任务结束关门）不因 S14.4 的分支改动而改变。

**默认关闭时，本批代码路径一行都不应执行**，用变异证明。

## 需求追踪

| 需求 | spec | plan | 验证 |
| --- | --- | --- | --- |
| REQ-32 | S14.1 | T1/T7 | 默认关闭零影响 |
| REQ-33 | S14.2 | T2 | 四条件拒绝矩阵 |
| REQ-34 | S14.3/S14.4 | T3/T4 | 五步顺序与短路 |
| REQ-35 | S14.5 | T4 | 出库中不关门 |
| REQ-36 | S14.3 | T3 | 坐标到达/超时 |
| REQ-37 | S14.1 | T1 | 配置校验 |
| REQ-38 | S14.4 | T4 | 关门失败不发命令 + 告警 |
| REQ-39 | S14.4 | T5 | 位置分支门判据 |
| REQ-40 | S14.4 | T5 | 顶替坐标不放行 |
| REQ-41 | S14.7 | T6 | reset 可恢复 |
| REQ-42 | S14.8 | T7 | 回归 + 变异 |
