# after_exit 离库关门：位置缺样与人工关门的正确分类

日期：2026-10-25。状态：**待用户评审**。类型：**bug 修正**（A）+ **规格遗漏**（B）。
绑定原 [车库控制需求](2026-10-15-req-garage-control.md) REQ-8 / REQ-10。
关联：[原 spec](2026-10-15-spec-garage-control.md) S5.1 / S6、本批 [spec](2026-10-25-spec-garage-exit-close.md)、[plan](2026-10-25-plan-garage-exit-close.md)。

## 起因（2026-10-25 实测）

after_exit 模式下机器人离库，**集成一次 `close_cover` 都没发过**，门是用户手动关的。

```
15:54:52.8  open_cover（集成）→ 门开
15:55:00.3  全关门磁 on
15:55:37.9  passage_allowed on
15:55:41.8  机器人开始清扫；outside_danger_zone → on   ← 已出区，应当关门
15:55:48    outside_danger_zone → unknown（+6.6s）      ← 只持续 4.9 秒
15:56:12    outside_danger_zone → on
   ... 此后 16 次 on→unknown→on 循环，unknown 每次 1.0–1.3 秒
15:58:36.3  集成 fault: feedback_unavailable（previous_phase=task_open）
15:58:39.9  门 closed —— 无任何服务调用，用户手动关闭
```

Logbook 在 15:55:41–15:58:39 区间**没有一条 `close_cover`**。

## 缺陷 A：`unknown` 清空了「已离区」的武装状态

`garage_control.py` `refresh()`：

```python
if not (self.robot.fresh and self.robot.position_trusted) or outside is None:
    self._departure_armed = False      # ← unknown 也走这里
    self._exit_due = False
elif token is not None and token != self._position_sample:
    self._position_sample = token
    if outside is False:
        self._departure_armed = True
    elif self._departure_armed:
        self._exit_due = True          # ← 只有这里才开门关门流程
        self._departure_armed = False
```

关门需要 `_departure_armed` 曾在区内为真、且下一次读到明确 `outside=True`。但 **`outside is None`（这一次没采到）被当成「不可信」，顺手把 `armed` 清零**。

实测这条路径必然踩中：`unknown` 每 30 秒出现一次、持续约 1.2 秒，而 `refresh()` 每秒执行。窗口一旦落在出区的那一刻之后，`armed` 就永久丢失——后续 `on` 再回来时 `elif self._departure_armed` 已为假，`_exit_due` 永远不会置位。

**语义错误**：`None` 是「这一次没采到」，不是「回到区内」，也不是「位置不可信」。三个状态被折叠成两个。

## 缺陷 B：人工关门被当成设备故障

`garage_runtime.py` `_state_event()`：

```python
if self.session.active and self.controller.phase in ('task_open','returning','docked_intermission'):
    self.controller.refresh()
    if self.controller.passage_allowed is not True:
        self.controller.fault('manual_override', manual=True)
```

用户手动关门 → `passage_allowed` 变 False（门关了当然不能通行）→ 判为 `manual_override` 并锁存。2026-10-25 15:58:36 那次报的却是 `feedback_unavailable`，因为门磁变化那一刻端点证据先失联了。

两种情况的结果一样：**任务还在进行时用户关了一次门，下一次清扫就被拒**。这与 v0.8.0 刚确立的原则相矛盾——「人工操作若与任何流程无冲突，只记录、不夺权」。

但这里**有冲突**：任务正在跑，门被关上，机器人可能回不来。所以不能简单豁免。正确分类应当区分：

| 情形 | 应判为 | 理由 |
|---|---|---|
| 任务中，门被外部关上，且机器人仍在区内 | `manual_override` + 保持 | 机器人可能被关在里面出不去 |
| 任务中，门被外部关上，且机器人已确认在区外 | **记录 + 事件**，不锁存 | 这正是用户想要的收尾；集成本来也要关门 |
| 门磁短暂失联后恢复，未发生真实关门 | 不锁存（B 的直接 bug） | 一次采样缺失不是人工接管 |

## 用户故事

- 作为家庭成员，机器人出去清扫后，车库门应当**在它离开威胁区后自动关上**，而不是一直敞着等我手动关。
- 作为家庭成员，我在清扫过程中手动关门（例如风大、有小孩），不应该导致下一小时的定时清扫被拒。
- 作为维护者，我要能区分「采样失败一次」和「位置不可信到不能用来关门」——两者现在的处理方式一样，这是错的。

## 功能需求

| 编号 | 需求 | 验收标准 |
| --- | --- | --- |
| REQ-26 | `unknown` 不改变已离区状态 | `outside is None` 时 `_departure_armed` / `_exit_due` **保持原值**，既不推进也不回退。只有 `robot.fresh` 为假或 `position_trusted` 为假时才清零（安全方向） |
| REQ-27 | 关门仍要求明确证据 | `_exit_due` 只在读到明确 `outside=True` 且 `armed` 为真时置位；`unknown` 不得触发关门，也不得把已置位的 `_exit_due` 清掉 |
| REQ-28 | 位置可信度语义不变 | `position_trusted=False`（旧值、跨区、基站不一致）仍立即 `armed=False` 并清 `_exit_due`——这是原 REQ-4 的安全边界，不得放宽 |
| REQ-29 | 人工关门按冲突分类 | 任务中外部关门：机器人在区内 → `manual_override` 保持；已在区外 → 只记录 + 事件，不锁存。门磁失联后恢复且未发生真实关门 → 不锁存 |
| REQ-30 | 不回归既有语义 | 可信全闭 stop 豁免、无冲突外部开关记录、运动中 stop 锁存、既有保持不自动解除——全部保持，并有回归证明 |
| REQ-31 | 验证不触发设备 | 全部隔离回放 + 变异；实物验收由用户执行 |

## 明确非目标

- 不改变 `MAP_POSITION_MAX_AGE_INTERVALS = 2`（容忍两次连续缺样会让过期位置被当成可用，原文已说明这是「必须失败安全」的唯一方向）。
- 不把 `unknown` 当作「在区内」（那会让 `armed` 立刻置真，等于把一次丢包当成回库）。
- 不调整本地服务器或采样参数——本次实测本地 MQTT 每秒约 2 次 `map_response`，采样能力充足，问题在状态机不在数据源。
- 不做「门关了就自动重启任务」这类补偿动作。

## 界面约束

无界面改动。不改 Options Flow、实体、服务。

## 需求追踪

| 需求 | spec | plan | 验证 |
| --- | --- | --- | --- |
| REQ-26 | S12.1 | T1 | unknown 期间 armed/exit_due 逐一断言不变 |
| REQ-27 | S12.1 | T1/T2 | unknown 不触发关门；恢复后仍能关门 |
| REQ-28 | S12.2 | T2 | 不可信位置仍清状态 |
| REQ-29 | S12.3 | T3/T4 | 三种情形各自断言 |
| REQ-30 | S12.4 | T5 | 全量回归 + 变异 |
| REQ-31 | S12.5 | T1–T6 | 无设备动作 |
