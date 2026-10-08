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

