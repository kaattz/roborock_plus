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

判据分两步。

**「门在不在动」看时间，不看 cover 状态。** 第一版用 `opening`/`closing`，那在这台设备上不成立：它没有 status 属性，那两个状态是集成自己下的乐观标志，而设备在 ~0.5 秒内把命令的目标值回声进 `current_position`，触发 `_position_changed_handler` 把标志清掉。实测 7 天：`opening`/`closing` 平均只持续 **0.79 秒**（最大 1.71，n=20），而真实行程 ~34 秒。

后果是「门在动 → 停止」这一支**永远不可达**，行进中按键落到反向分支：

| 按键时机 | 实体上报 | 实际执行 | 用户意图 |
| --- | --- | --- | --- |
| 关门途中 | `closed` / 0 | `open_cover` | 停住 |
| **开门途中** | `open` / 100 | **`close_cover`** | 停住 |

第二行是危险的：扫地机在门口时，关门是唯一可能撞到它的方向。本自动化一直是 `off`，所以从未发生。

现在用「距上次命令的时间」代替：一条 `call_service` 触发器（cover 域）在命令到达时写 `input_datetime.garage_door_moving_end_time`：

```
open_cover / close_cover → moving_end = 现在 + 45 秒
stop_cover               → moving_end = 现在（行程结束）
```

**「门开着还是关着」看门磁。** `binary_sensor.vacuum_garage_door_contact_state` 是 2026-10-14 装在全关位的物理触点，和 `current_position` 不同，它不会被回声污染，而且比 cover 早约 5 秒报出真实状态（实测 14:21:15.373 门磁 `off`，14:21:20.736 cover 才 `closed`）。

| 条件 | 动作 |
| --- | --- |
| `moving_end` 读不到 | **什么都不做 + 通知** |
| 门磁读不到 | **什么都不做 + 通知** |
| 还在窗口内 | 停止 |
| 门磁 `off`（在关闭位） | 打开 |
| 门磁 `on`（未关闭） | 关闭 |

前两行是刻意的：判断不了就不猜。猜「关闭」是唯一可能夹到扫地机的方向。

### 窗口为什么取 45 秒

偏大是安全的：门停下后立刻按会被当成「还在动」而发 `stop_cover`，那在**静止**的门上就是松开离合 —— 正是 `vacuum_garage_door_auto_unlock` 刻意做的事。偏小是危险的：行程末尾按键会反向。

所以取 `ECHO_SETTLE_SECONDS`（一次行程 + 10 秒余量），与「位置读数多久不可信」共用同一个常量 —— 两者描述的是同一个区间：电机可能还在转多久。

### mode 必须是 queued

本自动化自己发的 `cover.open_cover` 会触发记录窗口的那条 `call_service` 触发器。`single` 会把这次重入当重复丢掉，窗口就记不上 —— 于是行程中按键又变回反向。**这是 `queued` 唯一的理由。**

### `initial_state: false`

每次 HA 启动都保持 `off`。开启它等于让一次配置重载就能让物理按键动真门 —— 那是用户的决定，不该由 reload 替做。

## 踩过的坑：模板在条件里抛异常会静默失败

行进判据最初写的是：

```jinja
{{ now() < as_datetime(travel_end) }}
```

`as_datetime()` 对不带时区的字符串返回 **naive** datetime，而 `now()` 是 **aware** 的 —— 比较抛：

```
TypeError: can't compare offset-naive and offset-aware datetimes
```

条件里抛异常会**中止整个运行**，所以 stop 分支永远不会走到，按键会失效 —— 而修复看起来还在。

**这个 bug 是本项目所有自动检查都漏掉的**：398 个测试通过、12 个变异全捕获、仿真里我自己重写的 Python 版逻辑也是对的（因为副本写对了）。**是最后拿去 HA 的模板引擎里跑真实模板才暴露的。**

修法：改用 `input_datetime` 自带的数值 `timestamp` 属性，根本不做日期解析。

```jinja
{{ now().timestamp() < (state_attr('input_datetime.garage_door_moving_end_time', 'timestamp') | float(0)) }}
```

**教训**：仿真里**重写**逻辑等于测试副本，不是测试产品。现在 `simulate_presses.py` 会遍历配置里**每一条**模板并真实渲染，任何一个抛异常都算失败。

## 验证

```bash
python scripts/garage_button_cycle/build.py             # 生成配置
python scripts/garage_button_cycle/simulate_presses.py  # 回放按键 + 渲染全部模板
python scripts/mutation_check_button_cycle.py           # 12 个变异必须全部被捕获
python -m pytest tests/test_garage_button_cycle.py -q   # 23 项结构断言
```

仿真覆盖：行进中按键（开门/关门两个方向）、窗口边界前后 1 秒、门磁两态、停在中途、各种 `unknown`/`unavailable`、双击、以及记录分支对「别的窗帘」「非 cover 服务」「列表 target」的处理。

变异检查里最重要的是两条，都是**真实缺陷的原样复现**：

- `THE SHIPPED BUG` —— 用 `opening`/`closing` 判断在动
- `THE MISSED BUG` —— 拿 naive datetime 和 aware 比

## 清理掉的残留

`cover.g20s_ultra_garage_door`（定义在 `/config/packages/window.yaml`）是早期
"没有 position 所以包一层"的产物。它只是转发到 `cover.vacuum_garage_door`，
而且 `state` 模板有 bug：位置既不是 0 也不是 100 就恒渲染 `opening`，
所以在 position=77 时它一直显示"正在打开"。零个自动化引用它，已移除。

## 辅助实体现状

| 实体 | 现在 | 说明 |
| --- | --- | --- |
| `input_datetime.garage_door_moving_end_time` | **在用** | 记录「电机可能还在转」到什么时候。是 YAML 定义（`configuration.yaml`），`editable: false` |
| `input_select.garage_door_last_action` | **仍未使用** | 蓝图用它判断往哪个方向反向；门磁直接回答了这个问题，它只会变成第二个更滞后的真相来源 |

`garage_door_moving_end_time` 曾经是蓝图留下的孤儿，2026-10-14 复用而不是新建 —— 它本来就叫「车库门移动结束时间」，语义完全吻合，新建一个只会多一个要维护的实体。

**它的当前值不参与判断的有效性**：读不到（`unknown`/`unavailable`）时按「判断不了」处理，拒绝动作并告警。所以 HA 重启后它被恢复成旧值也不会误判 —— 旧值在过去，读出来就是「不在动」。

## 注意：蓝图本身不能删

`my_custom/无线窗帘电动窗单键循环控制.yaml` 仍被**其他 9 个**自动化使用
（餐厅窗帘、厨房百叶帘、次卧电动窗/厚帘/纱帘、外卫百叶帘/电动窗、主卫百叶帘/电动窗）。
只退休了"车库门"这一个实例。
