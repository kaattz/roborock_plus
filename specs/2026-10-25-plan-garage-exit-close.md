# after_exit 离库关门修复 — 实施计划

日期：2026-10-25。状态：**待用户评审**。
对应 [req](2026-10-25-req-garage-exit-close.md) / [spec](2026-10-25-spec-garage-exit-close.md)。
版本：0.8.0 → 0.8.1（bug 修正，无新服务/参数）。
本批只做本地实现与隔离验证，**不部署 HA、不发版、不操作设备**。

## 实施前检查

- `<files>` 本批 req/spec、原 req/spec/plan、AGENTS.md、lessons.md、
  `garage_control.py`、`garage_runtime.py`、`v1_map_position.py`、`v1_position_trust.py`、
  `door_evidence.py`、`garage_task.py`、
  `tests/test_garage_external_open.py`、`tests/test_garage_closed_stop.py`、
  `tests/test_garage_runtime*.py`、`tests/ha_native_garage*.py`。
- `<action>` 核对：`refresh()` 的三层语义；`_exit_can_close()` 已有的新鲜度/可信度要求；
  `_state_event()` 的触发条件与 phase 白名单；`notify()` 的去重键；现有断言里哪些依赖
  「任务中门被关必然锁存」。
- `<verify>` 跑一次 `pytest tests -q` 取基线（应为 1253 passed）。
- `<done>` 会受影响的测试清单明确，且每条都能说明「为什么必须改」。

## T1 控制器：unknown 不改变已离区状态（REQ-26 / REQ-27）

- `<files>` `garage_control.py`
- `<action>` 按 S12.1 把合并的 `if` 拆成`不可信` / `None` / `有样本` 三层；`None` 分支只
  记录日志、不触碰 `_departure_armed`、`_exit_due`、`_position_sample`。
- `<verify>` 新增 `tests/test_garage_exit_close.py`：
  1. `False → None → True` 序列能置 `_exit_due` 并触发关门
  2. `False → True` 直连仍能关门（不回归）
  3. `None` 期间三个字段逐一断言不变
  4. `None` 不进入 `_close_after_departure`
  5. `True → None → True` 不会重复触发第二次关门
- `<done>` 五条全绿；`tests/test_garage_external_open.py` 不受影响。

## T2 不可信位置仍清状态（REQ-28）

- `<files>` `tests/test_garage_exit_close.py`
- `<action>` 断言 `position_trusted=False` 时 `_departure_armed` 与 `_exit_due` 都被清零，
  且不发出任何门命令；`robot.fresh=False` 同理。
- `<verify>` 该测试通过；把 T1 的「不可信」分支误并入 `None` 分支后必须变红。
- `<done>` 变异捕获。

## T3 人工关门分类（REQ-29 控制器侧）

- `<files>` `garage_control.py`、`garage_runtime.py`
- `<action>` `_state_event()` 按 S12.3 的三分法改写：机器人在区内 → 保持 `manual_override`；
  确认在区外 → 记录 + `notify('external_close_after_exit')`，不 `fault()`；位置未知 →
  沿用 `manual_override`。`notify()` 的去重键与 `previous_phase` 逻辑不变。
- `<verify>` 三条断言：区内关门 `reason == 'manual_override'`；区外关门 `reason is None`
  且事件里有 `external_close_after_exit`；位置未知关门 `reason == 'manual_override'`。
- `<done>` 三条全绿；下次清扫在「区外关门」后不被拒（用 `async_execute` 断言）。

## T4 原生 HA 分发（REQ-29 运行时侧）

- `<files>` `tests/ha_native_garage.py`
- `<action>` 用真实 `hass.bus` 事件模拟门状态变化，覆盖 S12.3 三分法；并补一条
  「门磁失联后恢复、未发生真实关门 → 不锁存」。
- `<verify>` 原生测试通过；把三分法退回「一律锁存」后必须变红。
- `<done>` 变异捕获；无设备动作。

## T5 回归与变异（REQ-30 / REQ-31）

- `<files>` 既有测试文件（只改必须改的断言）、`.analysis/mutate_exit_close.py`（新建）
- `<action>` 拆分依赖旧行为的断言（登记在偏差记录）；新增 5 条变异：
  ①`None` 走清零 ②`None` 走置真 ③不可信位置不清 ④人工关门全判锁存 ⑤人工关门全不锁存。
- `<verify>` `pytest tests -q` 全绿；离线原生 HA 全组通过；5 条变异逐个变红。
- `<done>` 结果写入完成记录；**未为绿灯放宽任何非必要断言**。

## T6 文档与交付（REQ-31）

- `<files>` `CHANGELOG.md`、`README.md`（after_exit 描述）、`docs/garage-control-checklist.md`、
  `manifest.json`（0.8.1）、原 spec S5.1 交叉引用
- `<action>` CHANGELOG 增设 0.8.1；README 说明「unknown 不清武装」；清单增补验收项
  「出区后自动关门」「清扫中手动关门不锁死下次」。
- `<verify>` 全量测试仍绿；人工通读。
- `<done>` 文档与行为一致；未声称为硬件验收通过。

## 分层 Review

| 层 | 关注点 | 检查点 |
| --- | --- | --- |
| 安全（最高优先） | `None` 不得推进关门；不可信位置不得放宽；`_exit_can_close()` 仍是最后闸门 | T1 后立即 Review，未过不得继续 |
| 产品 | 人工关门三分法是否符合「有冲突才夺权」原则 | T3 后 |
| 工程 | `refresh()` 三层无重复赋值；`notify()` 键不冲突；无新持久化 | T3 后 |
| 验证 | 5 条变异全捕获；未放宽既有断言 | T5 后 |

## 完成前漂移检查

1. S12.1 的三层顺序是否与实现一致（不可信优先于 None）。
2. `None` 分支是否真的一个字段都没改（含 `_position_sample`）。
3. `_exit_can_close()` 是否原样未动。
4. `MAP_POSITION_MAX_AGE_INTERVALS` 是否仍为 2。
5. 新增原因码是否只出现在 S12.3 允许的位置。
6. 既有断言是否只在偏差记录列出的那些处被改。
7. 是否有未写入 spec 的新行为。
8. 是否误称已部署或硬件已验收。

## 完成记录

本批已完成本地实现与隔离验证，**未部署 HA、未发版、未操作设备**。
交付组合 SHA-256（`garage_control.py` + `garage_runtime.py` 顺序拼接）：
`eda9a0860b763b99151d567c98adf6b8acb0dd66ae33452a7f2de5320387ff79`

### 实施结果

| 任务 | 结果 |
| --- | --- |
| T1 | `refresh()` 拆出 `_observe_departure()`，三层语义各自独立；`unknown` 分支零赋值 |
| T2 | 不可信位置仍走第一条分支；12 项 `test_garage_exit_close.py` |
| T3 | `_classify_external_close()` 三分法；9 项 `test_garage_external_close.py` |
| T4 | 原生 HA 三分法测试；21 项 `ha_native_garage.py` |
| T5 | 变异 6/6；另复跑 13/13 与 4/4 确认无回归 |
| T6 | manifest 0.8.1、CHANGELOG、README、清单、原 spec S5.1 交叉引用已同步 |

### 验证

- 主机全量：**1276 passed**
- 离线原生 HA 2026.10.1（14 个 `ha_native_*.py`）：**157 passed**（38 条既有上游弃用提示）
- 变异：离库 6/6、外部开门 13/13、自动化 4/4 全捕获
- `compileall` 通过

### 与计划的偏差（需记录）

1. **改了一条既有测试的断言**：`tests/test_garage_after_exit.py::test_unknown_position_and_manual_pause_cannot_create_departure`
   原断言「inside → unknown → outside 不应关门」——这正是本次要修的 bug 的行为。
   改为两条：`test_an_unknown_sample_alone_cannot_create_a_departure`（未武装时 unknown
   不能造出出库）与 `test_a_missed_sample_does_not_lose_a_real_departure`（armed 后
   unknown 不得丢失真实出库）。另把手动暂停部分拆成独立的
   `test_manual_pause_cannot_create_departure`，语义不变。这是**修正断言到正确语义**，
   不是放宽。
2. **`_observe_departure()` 拆成独立方法**：原逻辑内联在 `refresh()` 里，无法单独测试
   三层分支。拆分不改行为，只提高可测性。
3. **新增原因码 `external_close_after_exit`**：spec S12.3 已说明；它走 `notify()` 的
   既有 `(session_id, reason)` 去重，因此 v0.8.0 的通知自动化会照常告警。
4. **测试初版有两处我自己写错**（不是代码问题）：把 `refresh()` 当成会推进 token、
   以及误判 `start()` 之后 `armed` 的初值。已按真实时序修正测试，未改产品代码。

### 未验收项

- **实物验收全部待用户完成**，清单新增两条（出区自动关门、清扫中手动关门不锁死下次）。
- 未部署 HA，未发 HACS 版本。
- 2026-10-25 那次卡死现场的锁存（`phase=blocked / reason=feedback_unavailable`）
  仍需一次 `reset_garage_control` 才能解除；本批只保证以后不再发生同类锁存。
