# 扫地机分区定时清扫蓝图 — 设计

日期：2026-10-09
状态：待评审
阶段：第二阶段（第一阶段柜门状态机已完成并部署）

## 需求

用户当前靠手动（或 Roborock App 的例程）启动清扫。目标是把日常清扫自动化：

| 任务 | 频率 | 区域 |
| --- | --- | --- |
| 公区 | 每天 | 客厅 + 餐厅 + 玄关 + 厨房（待定） |
| 午饭后餐厨 | 每天午饭后 | 厨房 + 餐厅 |
| 卧室 | 每周固定几天 | 主卧 + 次卧 + 主卫 + 外卫 |

## 已确认的决策

| 项 | 决策 | 理由 |
| --- | --- | --- |
| 实现形式 | **自动化蓝图** | 3+ 个任务共用同一套逻辑；复制三份几乎相同的自动化不可维护 |
| 时间在哪配 | **蓝图自带 `time` 触发器** | 每个任务自包含（一个自动化里看全时间+区域+模式），不额外建 schedule 助手 |
| 任务内容来源 | **HA 指定房间**（`vacuum.clean_area`） | 全透明、可版本管理；设备例程虽可用但定义在本地服务器快照里 |
| 扫地/拖地 | **每个任务自己选** | 饭后只扫、卧室拖地等需求不同 |
| 有人在家 | **只看本任务房间的传感器；未配传感器则到点就扫** | 由蓝图输入决定，不做成两种模式 |
| 每隔几天 | **每周固定几天**（`condition: time` + `weekday`） | 纯原生，不需要持久化助手 |
| 失败重试 | **窗口内重试**（新增需求，见下） | 触发时可能因有人/机器人忙碌/门打不开而无法执行 |
| 第一版范围 | 做得通用，以后随便加实例 | |

## ⚠️ 实施前必须解决的两个阻塞

### 阻塞 1：`vacuum.clean_area` 需要先配"区域映射"

HA 的 `clean_area` 不直接把房间号给设备，它读实体注册表里的映射：

```python
area_mapping = entity.registry_entry.options.get("vacuum", {}).get("area_mapping")
if area_mapping is None:
    raise ServiceValidationError("area_mapping_not_configured")
```

**当前状态：`vacuum.g20s_ultra` 没有 `area_mapping`。** 所以 `clean_area` 现在调用必然失败。

**解决**：需要在 UI 里配一次 —— 设备页面 → 扫地机 → 齿轮 → 区域映射，把 HA 区域对应到设备的 segment 号。**这是用户操作，助手不做。**

设备自报的房间（`roborock_plus.get_maps` 实测）：

```
1 主卧   2 主卫   3 客厅   4 外卫   5 厨房   6 次卧   7 餐厅
```

### 阻塞 2：⚠️ 现有脚本的"拖地模式"很可能是错的

仓库外存在 `script.set_mop_mode_and_clean_target_segment_2`（设置拖地模式并清扫指定房间），它的下拉框是：

```yaml
mop_mode: 303=扫地, 301=拖地, 300=扫拖
```

**但 `SET_MOP_MODE` 接受的是「拖地路线」（`CleanRoutes`），不是「清扫类型」：**

```python
class CleanRoutes(RoborockModeEnum):
    STANDARD   = ("standard",  300)   # 标准路线
    DEEP       = ("deep",      301)   # 深度路线
    DEEP_PLUS  = ("deep_plus", 303)   # 深度+路线
    FAST       = ("fast",      304)
    SMART_MODE = ("smart_mode", 306)
    CUSTOMIZED = ("custom",    302)
```

也就是说 **300/301/303 全是"拖地路线"，没有一个是"扫地"**。那个脚本的三个选项名与实际语义不符。

设备实际暴露的两个相关实体：

| 实体 | 中文名 | 选项 |
| --- | --- | --- |
| `select.sao_di_ji_v2_2` | 扫地机 拖地模式 | standard / deep / deep_plus / fast / smart_mode / custom |
| `select.sao_di_ji_v2` | 扫地机 拖地强度 | off / slight / low / medium / moderate / high / extreme |

**在 V1 协议上，"扫地 vs 拖地"最可能由 `拖地强度 = off` 表达**（水量关 = 干扫），而 `拖地模式` 只控制走几遍。但这**需要实测确认**（见"未决问题"）。

`cleaning_mode`（仅扫地/仅拖地/扫拖一体）只存在于 B01/Q7 设备的 `B01_SELECT_DESCRIPTIONS`，**不在本设备的 V1 描述表里**，所以此设备没有那个实体。

**这个必须在写蓝图前查清**，否则蓝图会提供三个选项、其中两个名字是错的，而用户会以为自己在选"扫地"实际选的是"深度拖地路线"。

## 设计

### 蓝图输入

| 输入 | 选择器 | 必填 | 说明 |
| --- | --- | --- | --- |
| `vacuum_entity` | entity（vacuum 域） | ✅ | 目标扫地机 |
| `start_time` | time | ✅ | 每天/每周几的启动时间 |
| `weekdays` | select（多选 mon–sun） | ✅ | 哪几天执行；全选即每天 |
| `areas` | area（多选） | ✅ | 要清扫的 HA 区域 |
| `mop_intensity` | select | ✅ | 拖地强度；选 `off` 即干扫 |
| `mop_route` | select | ⬜ | 拖地路线；默认 `standard` |
| `presence_sensors` | entity（多选，binary_sensor） | ⬜ | 本任务房间的人体传感器；**留空=不检查** |
| `retry_window_minutes` | number（0–120，默认 30） | ⬜ | 重试窗口；0 = 不重试 |
| `retry_interval_minutes` | number（1–30，默认 5） | ⬜ | 重试间隔 |
| `notify_script` | entity（script） | ✅ | 告警脚本，默认 `script.alert_notify` |

### 触发

```yaml
triggers:
  - trigger: time
    at: !input start_time
conditions:
  - condition: time
    weekday: !input weekdays
```

`time` 触发器只能表达"每天几点"；"每周固定几天"由 `condition: time` 的 `weekday` 承担。这是原生组合，不需要额外助手。

### 执行流程

```
1. 前置检查（每次尝试都重新检查）
   ├─ 人体传感器（若已配置）有任意一个 on？  → 本轮跳过，等待重试
   └─ 扫地机不在 docked/charging？          → 本轮跳过，等待重试
                    │
                    ├─ 两种都通过 ↓
                    │
2. 设置拖地强度 / 拖地路线
3. vacuum.clean_area(areas)
4. 等待确认（最多 90 秒）
   ├─ binary_sensor.<vacuum>_task_active → on   ✅ 成功，结束
   └─ 收到 roborock_plus_clean_command_not_started 事件  ❌ 失败，进入重试
                    │
5. 若在重试窗口内 → 等 retry_interval → 回到步骤 1
   若超出窗口     → 告警（说明最后一次跳过的原因），结束
```

### 为什么用事件而不是只看服务返回

`lessons.md` 记录过：**"HTTP 返回 200" 不等于"机器人动了"**。例程/命令在服务器内部异步执行，服务调用成功只代表被接受。

集成已经为此提供了权威信号 —— `clean_command_watch.py`：

```
EVENT_CLEAN_COMMAND_NOT_STARTED = "roborock_plus_clean_command_not_started"
DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT = 60
```

命令发出 60 秒后若任务仍未启动，集成主动 fire 这个事件。**蓝图直接等这个事件**，而不是自己轮询状态，比自建检测更可靠，也不重复实现。

### 重试的设计要点

**"失败"要区分可重试与不可重试**：

| 情况 | 可重试？ | 说明 |
| --- | --- | --- |
| 本任务房间有人 | ✅ | 人走了就能扫 —— 这正是"半小时内重试"的主要场景 |
| 扫地机正忙（在清扫/回充） | ✅ | 上一个任务结束了就能开始 |
| 柜门打不开 | ✅ | 集成的 `garage_guard` 会在命令前开门；门卡住是暂时的 |
| 命令发出但任务没启动 | ✅ | 等 `..._not_started` 事件后重试 |
| **任务已成功启动** | ❌ | **立即停止重试**，避免重复下发 |
| 超出重试窗口 | ❌ | 告警，说明最后一次跳过的原因 |

**必须避免重试造成的重复清扫。** 判定"成功"的唯一依据是 `task_active` 变 `on` 或收到 `not_started` 事件；后者之前不能重试。

**为什么要等待而不是立即重试**：立刻重试会在同一个阻塞条件下反复下发命令。等待 `retry_interval` 让状态有机会变化，也避免把设备命令打成风暴。

### 蓝图结构选择：自动化蓝图（不是脚本蓝图）

`domain: automation`，因为需要 `time` 触发器。脚本蓝图不能自带触发器。

### 告警：沿用现有脚本

复用 `script.alert_notify`（字段 `title` / `message` / `level`，level 支持 info/warning/critical）。与柜门状态机保持一致，用户已熟悉。

需要告警的情况：
- 跳过全部重试后仍无法执行 → `warning`，说明原因
- 参数配置错误（如区域未映射）→ `warning`

**不告警**的情况：
- 正常运行（避免每天三条无意义通知）

## 非目标

- **不做 App 启动的支持。** 从 Roborock App 发起的清扫不经过本蓝图；集成的 `garage_guard` 已保证门先开。
- **不改柜门状态机。** 本蓝图只负责"何时开始清扫"，门由 `garage_guard`（开门）和柜门状态机（关门）各自负责，互不耦合。
- **不做"当前是否在清扫"的互斥锁。** 前置检查读 `vacuum` 实体状态即可，不引入新的 helper。
- **不做每任务独立的开关。** 第一版靠"启用/禁用该自动化"控制。
- **不碰 `configuration.yaml` 里的旧 helper。** 见"遗留"。

## 未决问题（实施前需实测确认）

1. **`拖地强度 = off` 是否等于"仅扫地"？** 决定蓝图怎么表达"扫地/拖地/扫拖"。需要实际下发一次并观察行为（用户实测）。
2. **`app_segment_clean` 的参数形状。** 集成内部发 `[{"segments": [3]}]`，而已有脚本发 `[3]`。走 `clean_area` 则不涉及（HA 会调 `async_clean_segments`），但要确认集成那条路是通的。
3. **重试窗口内任务已成功的判定是否够快。** 集成默认 watch 超时 60 秒，蓝图的等待需要 ≥90 秒才能覆盖。

## 遗留（与本蓝图无关，但已确认）

`configuration.yaml` 里有 5 个疑似残留的 YAML 定义 helper，**API 删不掉**（因为它们不是 UI 辅助元素）：

```
207 行  input_datetime: garage_door_moving_end_time
278 行  input_boolean:  vacuum_departing_guard
280 行  input_boolean:  vacuum_returning_guard
282 行  input_boolean:  vacuum_garage_flow_lock      ← 归属未确认，可能是别的自动化在用
425 行  input_select:   garage_door_last_action
```

删除必须编辑 `configuration.yaml` 并 reload。**`vacuum_garage_flow_lock` 名字不像已退休蓝图的东西，在确认归属前不动任何一个。**

## 待用户确认

1. 上表"未决问题 1"需要实测：你希望**先跑一次实验确认"拖地强度 off = 干扫"**，还是**蓝图先只提供拖地路线、不提供扫地/拖地选择**（更保守，第一版不猜）？
2. 公区具体包含哪些房间？（客厅/餐厅/玄关/厨房/电梯间/设备阳台 里选）
3. 三个任务各自的时间点？

确认后进入实施计划（`docs/plans/*-plan-*.md`），再动代码。
