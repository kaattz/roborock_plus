# 给 AI 助手的操作约定

## 绝对不要主动触发实体设备

**只读、仿真、改代码可以；实操不行。**

具体禁止：

- 不发 `vacuum.start` / `vacuum.pause` / `vacuum.return_to_base` / `vacuum.clean_spot` / `vacuum.clean_area`
- 不按 `button.*`（含 `button.vacuum_post_meal_cleaning2` 这类例程按钮）
- 不调 `cover.*` 去开关柜门、车库门、窗帘、窗户
- 不改 `input_boolean` / `input_select` / `input_datetime` 来旁路验证
- 不用 `automation.trigger` 去跑一个会动设备的自动化

**为什么**：这些是用户家里的真实设备。扫地机出动会扫到人、撞到东西、耗电、发噪音；
柜门和窗户关系到安全。这些代价不该由助手替用户决定。用户本人做实际验证，
助手负责把仿真做扎实、把「需要实测什么、预期看到什么」讲清楚。

**替代做法**：

| 想验证的东西 | 用这个 |
| --- | --- |
| 状态机在真实时序下的行为 | 把历史 trace / 实体 history 当输入做回放 |
| 守卫是否真的拦得住 | 变异测试（改坏配置，测试必须变红） |
| 边界情况 | 构造时序数据，比实操覆盖更广且可重复 |
| 集成的服务返回值 | 只读服务可以调（如 `get_vacuum_current_position`、`get_maps`） |

只读的服务调用、`ha_get_state`、`ha_get_history`、`ha_get_automation_traces`、
`ha_get_logs` 都不受此限制。

## 判断依据

如果某条命令会让**物理世界发生变化**（轮子转、门移动、灯亮），那它就属于实操。
拿不准的时候，先问用户。

## 发服务调用前的机械检查

上面那条"拿不准就问"不够硬 —— 2026-10-08 我在核实 `clean_spot` 是否走 guard 时，
把只读核实和实操混在同一个工具批次里，**误发了一条 `vacuum.clean_spot`**。
核实那件事只需要读代码，根本不该发命令。

所以加一条可以机械执行的规则：**任何 `ha_call_service` 之前，先说出目标域，并对照下表。**

| 域 | 能不能调 |
| --- | --- |
| `vacuum` | ❌ 一律不调（`start` / `pause` / `stop` / `return_to_base` / `clean_spot` / `clean_area` / `send_command` 全部禁止） |
| `cover` | ❌ 不调 |
| `button` / `input_button` | ❌ 不按 |
| `automation.trigger` | ❌ 不调（会跑动设备的自动化；`turn_on`/`turn_off` 只改启用状态，可以） |
| `roborock_plus.*` 里带 `get_` / `clear_` / `set_safe_zone` | ✅ 只读或改配置，可以 |
| `homeassistant.*` | ⚠️ 看目标：`update_entity` 只读可以，`turn_on/off/toggle` 按目标域判断 |

**"我要验证 X 会不会动设备" 不是调用它的理由。** 验证代码路径用读代码、读 trace、跑仿真；
只有用户明确要求才实操。

**批次里混放也有风险**：只读核实和一个危险的写调用放在同一个 `invoke` 块里时，
注意力在"核实"上，写调用就被顺手带出去了。**危险的调用单独发，发之前单独确认一次。**

