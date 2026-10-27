# 车库门控制：按位置驱动，不判断任务归属

日期：2026-10-27。状态：**实施中**。类型：**重写**（替换 garage_control / garage_task / garage_session_store / garage_stage）。
版本目标：0.10.0。

## 为什么重写

现状：2434 行、14 个 phase、14 个原因码、6 处 `correlation_valid` 检查。为的是回答一个
**本屋不会发生的问题**——「机器人现在扫的是不是我在 App 里批准的那次任务」。

用户确认：**永远不会有 App 发起的清扫，所有清扫都从 HA 进入集成。**

代价是反复锁存：2026-10-26、10-27 三次故障，机器人停在充电座、门关着、什么都没发生，
系统却报 `session_recovery_required` 并要求人工「解除」，而 reset 自己也解不开。

## 唯一的目标

```
机器人要出去  → 门必须已经开着
机器人出去了  → 门关上
机器人要回来  → 门打开
机器人进去了  → 门关上
```

## 两条不同的触发方式

| 方向 | 触发 | 理由 |
| --- | --- | --- |
| **开门（出库）** | **主动**：发清扫命令前先开门并确认可通行，然后才发命令 | 门必须赶在机器人动之前让路。等观察到 `cleaning` 再开已经太晚——2026-10-23 之前的实测证明，那时机器人已移动，中途 pause 会让它在窄口原地打转 |
| **关门（出库后）** | 观察：确认机器人已在危险区外 | 关门必须确认安全，可以等 |
| **开门（回库）** | 观察 `returning` → 先 pause → 开门 → resume | pause 是安全的：机器人停下等，不会乱走 |
| **关门（最终）** | 任务结束且稳定停靠 | |

## 状态机（7 个 phase）

```
idle              无任务，门关着
opening           正在开门（主动或回库前）
outside           门开，机器人可通行（任务中或刚出库）
closing           正在关门
closed_outside    机器人已出区，门已关，任务继续
pausing_return    返航中，正在暂停机器人以开门
resuming_return   门已开，正在恢复同一返航
blocked           门反馈不可用，暂停动作等恢复
```

`blocked` 是**暂时的**：反馈恢复后自动回到正常判断，**不需要人工解除**，也不锁存。

## 直接删除

| 删除 | 理由 |
| --- | --- |
| `garage_task.py`（495 行） | 整个是任务归属：session_id、correlation、baseline、cancel |
| `garage_session_store.py` | 持久化任务记录，已无意义 |
| `garage_stage.py`（98 行） | 阶段回执建立在 session 上 |
| `roborock_plus.start_garage_cleaning_stage` | 同上 |
| 9 个 reason code | 只留：`feedback_unavailable`、`close_not_allowed`、`operation_in_progress`、`unsupported_*` |
| 「人工保持」/ `reset_garage_control` | 没有可保持的东西 |

## 保留

- `door_evidence.py`：门端点证据（open/close confirmed、estimated、unknown）
- `garage_commands.py`：命令分类
- `garage_config.py`：配置与校验
- 危险区几何、位置可信度判定
- `controlled_exit`（受控出库）——它是「主动开门」的一种，符合本设计

## 分阶段脚本的出路

6 个 `garage_*_sweep_then_mop` 脚本用 `stage_sensor`（phase 传感器）判断阶段完成。新设计保留
phase 语义：

- 任务进行中 → `outside` / `closed_outside`
- 任务结束 → `idle`

脚本只需判断「phase 是否回到 idle」即可串行，不再需要阶段回执。

## 失败处理原则（关键）

**任何异常都不产生需要人工解除的锁存状态。** 三种情况：

1. **门反馈不可用** → `blocked`，不发命令，等下一次采样自动恢复
2. **位置不可信** → 不基于它做决定，等下一次采样
3. **门命令失败** → 记录日志、保持当前 phase，不锁存

用户选 A：反馈不可信就跳过这次动作，等恢复。

## 非目标

- 不判断任务归属
- 不处理 App 发起的清扫（本屋不会发生）
- 不自动重试开门
- 不做多机器人共享一扇门的仲裁

## 需求追踪

| 需求 | 验证 |
| --- | --- |
| 出库主动开门 | 发清扫命令前，门已 `open_confirmed` |
| 出库后关门需确认区外 | 位置不在区外时不关门 |
| 回库 pause/open/resume | 顺序断言 + pause 未确认不 resume |
| 反馈不可用不锁存 | 恢复后自动继续，无需 reset |
| 无人工解除 | `reset_garage_control` 不再是必要入口 |
| 分阶段脚本可用 | phase 回到 idle 即可判断完成 |
