# after_exit 离库关门：位置缺样与人工关门 — 规格

日期：2026-10-25。状态：**待用户评审**。对应 [req](2026-10-25-req-garage-exit-close.md)。
关联：[原 spec](2026-10-15-spec-garage-control.md) S5.1/S6、[v0.8.0 spec](2026-10-23-spec-garage-external-open.md) S11.1。

## 代码证据

| 阅读对象 | 位置 | 与本批相关的事实 |
| --- | --- | --- |
| 离区武装/触发 | `garage_control.py:110-123` | `outside is None` 与「位置不可信」写在同一个 `if` 里，都执行 `_departure_armed=False; _exit_due=False` |
| 触发源 | `garage_runtime.py:122-164` | `outside_danger_zone` 来自 `resolve_vacuum_position` + 危险区几何；`position_sample_id` 是地图采样 token |
| 关门前提 | `garage_control.py:603-610` | `_exit_can_close()` **已经**要求 `robot.fresh and position_trusted and outside_danger_zone is True`，且 `state in _CLEANING_STATES and not docked` |
| 关门流程 | `garage_control.py:781-800` | `_close_after_departure()` 先 `refresh()`，再查 `_exit_can_close()` 与 `close_allowed()`，然后 `_preflight('close')` |
| 新鲜度判定 | `v1_map_position.py:149-174` | 任务进行中要求 `now - position_time <= max_age`；`max_age = active_interval × 2` |
| 官方云间隔上限 | `v1_map_position.py:39-43` | 官方云 60 秒活动间隔、最小 30 秒；本地 5 秒 |
| 本地判定 | `server_url.py:34-53` | `OFFICIAL_CLOUD_HOSTS = ("roborock.com",)`；`api-rr.homeaxis.xyz` 不含该后缀 → `is_official_cloud=False` |
| 人工关门分类 | `garage_runtime.py:494-510` | 门状态变化后若 `session.active` 且 phase 属于 task_open/returning/docked_intermission，`passage_allowed is not True` 就 `fault('manual_override', manual=True)` |
| 端点证据 | `door_evidence.py:44-59` | `passage_allowed`：`cover_available` 假 → None；`direction=='close'` → False；`open.reached` 为 None → None |
| 现场数据 | 本批 req 的「起因」 | 本地 MQTT 每秒约 2 次 `map_response`；`unknown` 每约 30 秒出现一次、持续 1.0–1.3 秒 |

**实测补充**（2026-10-25 16:09，addon 日志统计）：24 次 `map_response` / 25 次 `rpc_request` 全部走本地 MQTT，相邻间隔 0–2.4 秒。**数据源不是瓶颈**，`unknown` 来自 `max_age` 判定或单次读取失败，与本地/云端无关。

---

## S12.1 区分「没采到」与「不可信」（REQ-26 / REQ-27）

把当前合并的 `if` 拆成三层，各自语义独立：

```python
if not (self.robot.fresh and self.robot.position_trusted):
    # 位置不可信：旧值、跨区、基站不一致。安全方向是立刻撤销武装。
    self._departure_armed = False
    self._exit_due = False
elif outside is None:
    # 这一次没采到。既不是"还在区内"，也不是"已经出区"。
    # 保持原判，不推进不回收 —— 下一帧有效样本再决定。
    pass
elif token is not None and token != self._position_sample:
    self._position_sample = token
    if outside is False:
        self._departure_armed = True
        self._exit_due = False
    elif self._departure_armed:
        self._exit_due = True
        self._departure_armed = False
```

**为什么 `None` 不能清零**：`armed` 表达的是「这台机器人这一次出库过吗」。一次采样缺失不改变这个历史事实。清零会让「出库」和「回库」在丢包时无法区分，实测中 `unknown` 每 30 秒必现一次，等于每次清扫都必然丢掉关门机会。

**为什么 `None` 也不能置真**：把丢包当成「在区内」（`armed=True`）会让下一次无关的 `outside=True`（例如机器人本就在区内但坐标漂移）触发关门——那是在机器人可能还在柜子里时关门。

**为什么 `_exit_due` 也要保持**：`_exit_due` 是「已经该关了」的一次性标记。`unknown` 到达时若已置位，说明关门条件曾经成立；清掉它等于因为一次丢包放弃一次本该执行的关门。真正的安全网在 `_exit_can_close()`，它在关门那一刻重新要求 `outside is True`。

## S12.2 可信度语义原样保留（REQ-28）

`position_trusted` 为假时仍走第一条分支，行为与现状**完全一致**。这条分支对应 `v1_position_trust.py` 的三类拒绝：与基站位置不一致、距离突变、状态不允许。它们表达的是「这个坐标不能用来做安全判断」，与「这一次没读到」是两件事。

`MAP_POSITION_MAX_AGE_INTERVALS = 2` **不动**。原文明确：容忍两次连续缺样会让过期读数存活到机器人已回到区内之后仍被相信「安全」，这是唯一不能失败开放的方向。

## S12.3 人工关门的三种分类（REQ-29）

`_state_event()` 当前把「门失去开放端点」一律判为 `manual_override`。改为按冲突对象分类：

```
门状态变化（open_contact / close_contact / cover）
  ├─ phase 不属于 task_open/returning/docked_intermission → 不处理（已有逻辑）
  ├─ refresh() 后 passage_allowed is True                → 不处理（门仍可通行）
  └─ 否则按机器人位置分类：
        ├─ robot.fresh 且 position_trusted 且 outside_danger_zone is False
        │     → fault('manual_override', manual=True)       机器人还在区内，可能出不去
        ├─ robot.fresh 且 position_trusted 且 outside_danger_zone is True
        │     → 记录 INFO + 发 garage_issue(active=true, reason='external_close_after_exit')
        │       不锁存；不额外发 close_cover（门已经关了）
        └─ 位置不新鲜 / 不可信 / unknown
              → 沿用原 manual_override（失败安全：不知道机器人在哪，就不能假设它安全）
```

**新增原因码** `external_close_after_exit`：这是「用户做了集成本来也要做的事」，不是故障。它会经 `notify()` 发 `active=true` 事件，因此 v0.8.0 的通知自动化会照常告警——用户能看见「门被外部关了，我没再动作」，且下次清扫不被拒。

**为何不直接复用 `manual_override` 但不锁存**：原因码是给人看的。`manual_override` 在日志和实体里已有固定含义「有人抢了门」，这里的情况正相反——人做的是这台机器本来就打算做的动作。

**为何不省略事件**：`garage_issue` 是唯一的可见性通道。静默记录会让「门被外部关了」这件事在 UI 上完全消失，用户无法判断清扫是否会因此失败。

**边界**：这条只处理 `phase` 已在任务态的情形。任务开始前的空闲关门仍走 v0.8.0 的 `_no_conflict_for_external`（不锁存、不产生事件），两者不重叠。

## S12.4 不回归（REQ-30）

必须保持绿：

- `tests/test_garage_external_open.py`（31 项）：无冲突外部开关仍不锁存
- `tests/test_garage_closed_stop.py`（56 项）：可信全闭 stop 豁免、运动中 stop 锁存、既有保持不洗白
- `tests/test_garage_request_open.py`（14 项）：受控开门
- `tests/test_garage_runtime*.py`：事件去重与 `previous_phase`
- `tests/ha_native_garage*.py`：原生分发

**会受影响的既有测试**：`test_garage_runtime.py` 与 `ha_native_garage.py` 里断言「任务中门被关 → manual_override」的用例。它们必须拆成「机器人在区内 → 锁存」与「已在区外 → 不锁存但发事件」两条。这是**有意的行为变更**，在 plan 的偏差记录里登记。

## S12.5 验证（REQ-31）

| 层 | 覆盖 |
| --- | --- |
| 控制器回放 | `outside` 在 `False → None → True` 与 `False → True → None` 两种顺序下都能关门；`None` 期间 `armed`/`exit_due` 不变 |
| 不可信位置 | `position_trusted=False` 仍清状态且不关门 |
| 运行时事件 | 三种人工关门分类各自的事件/锁存断言 |
| 原生 HA | `call_service` 真实分发下，缺样恢复后仍发出 `close_cover` |
| 变异 | ①`None` 走清零 ②`None` 走置真 ③不可信位置不清 ④人工关门全判锁存 ⑤人工关门全不锁存 —— 逐个必须变红 |
