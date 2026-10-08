# 车库门 BLE 按键单键循环

取代原先的 `automation.wu_xian_chuang_lian_dian_dong_chuang_dan_jian_xun_huan_kong_zhi_che_ku_men`
（蓝图 `my_custom/无线窗帘电动窗单键循环控制.yaml` 的"车库门"实例）。

## 为什么要换

那个蓝图是为了给**不报 position 的设备**补一个位置而做的：

| 设备 | 平台 | 原生 position |
| --- | --- | --- |
| 6 个 Zigbee 窗帘 | `mqtt` (Z2M) | Z2M 提供 |
| 3 个电动窗 | `template`（包在易微联电机外） | **没有**，靠 template 转换 |
| **车库门** | `xiaomi_home` 原生 | **有**，真实值 |

蓝图用 `total_duration` + 一个 `input_datetime` 推算"还在动"。对不报 position 的设备这是合理变通；对车库门既多余又**算错**：

- 配置的 `total_duration` 是 **40 秒**
- 实际门 **不到 1 秒**就报到位（实测 `20:53:45.086` closing → `20:53:45.832` closed）
- 蓝图监听 cover 域的**每一次** `call_service`，分不清"用户按键"和"自动化发的命令"

后果（2026-10-08 实测两次，延迟都是 9.5 秒）：

```
20:53:44.950  柜门状态机: cover.close_cover
20:53:44.952  蓝图: 当成按键 → input_datetime 推到 20:54:24 (+39.0s ≈ total_duration)
20:53:45.832  门到位（0.75s）
20:53:51.846  蓝图触发
20:53:55.377  蓝图认为"循环下一步" → 反方向 → 门被打开
```

`21:09:04` → `21:09:14` 完全重复一次。

## 现在的行为

`cover.vacuum_garage_door` 自己上报 position（实测见过 `0 / 11 / 77 / 100`，包含停在半路的中间值），所以直接读它：

| 条件 | 动作 |
| --- | --- |
| 门在动（`opening` / `closing`） | 停止 |
| 位置 ≤ 1 | 打开 |
| 位置 ≥ 2 | 关闭 |
| 位置读不到 | **什么都不做 + 通知** |

最后一行是刻意的：位置未知时不能猜。猜"关闭"是唯一可能夹到扫地机的方向；什么都不做至少保持原状。

## 验证

```bash
python scripts/garage_button_cycle/build.py          # 生成配置
python scripts/garage_button_cycle/simulate_presses.py   # 回放按键（用实测位置）
python scripts/mutation_check_button_cycle.py        # 5 个变异必须全部被捕获
```

`simulate_presses.py` 用**实测的** position 值回放，覆盖 0/1/11/40/60/77/100 以及
position 缺失、`unavailable`、`unknown` 各种情况。变异检查会验证：反接开关、
在未知位置关门、去掉停止分支、静默拒绝、响应非单击 —— 五个都必须让测试变红。

## 清理掉的残留

`cover.g20s_ultra_garage_door`（定义在 `/config/packages/window.yaml`）是早期
"没有 position 所以包一层"的产物。它只是转发到 `cover.vacuum_garage_door`，
而且 `state` 模板有 bug：位置既不是 0 也不是 100 就恒渲染 `opening`，
所以在 position=77 时它一直显示"正在打开"。零个自动化引用它，已移除。

## 辅助实体现状

`input_datetime.garage_door_moving_end_time` 和
`input_select.garage_door_last_action` 只被**已退休**的蓝图自动化使用。它们现在是
孤儿。**没有删除** —— 等确认新按键循环稳定后再决定。

## 注意：蓝图本身不能删

`my_custom/无线窗帘电动窗单键循环控制.yaml` 仍被**其他 9 个**自动化使用
（餐厅窗帘、厨房百叶帘、次卧电动窗/厚帘/纱帘、外卫百叶帘/电动窗、主卫百叶帘/电动窗）。
只退休了"车库门"这一个实例。
