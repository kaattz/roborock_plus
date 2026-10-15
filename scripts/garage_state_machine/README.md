# 扫地机柜门状态机

## legacy 状态与迁移入口

这里是**旧全局 guard + 家庭 YAML 状态机**的历史实现和验证材料。
新车库控制在集成选项中显式按 V1 机器人绑定，默认 `hold_open`：整个任务（包括回洗/补电）
不主动关门，标准流程随 HACS 集成本体更新，不需要运行本目录的构建或复制 JSON/YAML。
新配置不会自动接管旧门。当前真实 V1 不开放下文 B 阶段“离开后关门”的 `after_exit` 模式。

迁移步骤、来源标定和硬件边界见 [主说明](../../README.md#原生车库控制集成管流程自动化管家庭策略)，
家庭自动化交接见 [automations/README.md](../../automations/README.md#原生车库控制与旧自动化交接)。
启用前核对并停用同门的旧状态机及 `*_plus` 主动流程；多机旧 guard 的其他保护关系需先解决。
无人关门等家庭策略改用 `request_garage_close`，人工操作保留；`reset_garage_control` 只复核、不动设备。

以下内容继续保留供未迁移配置维护和仿真对照；本次没有改写或删除旧 YAML/JSON，
也不会自动把仓库内容写入正在运行的 Home Assistant。

自动化本体保存在 Home Assistant 里（`automation.sao_di_ji_ju_men_zhuang_tai_ji`，
`automations.yaml` 中 id `1791392186966`）。这个目录是它的**可复现来源和验证**，
因为 HA 里的那份不在版本控制里，改坏了没有历史可查。

## 文件

| 文件 | 作用 |
| --- | --- |
| `build.py` | 生成配置。所有规则和理由都写在里面，改逻辑改这里 |
| `state_machine.json` | 生成结果，也是提交给 HA 的那一份 |
| `replay_docked_flapping.py` | 用真实观测到的时序回放，验证守卫 |
| `fetch_deployed.py` | 从 HA 容器里读出**实际部署**的配置 |
| `verify_deployed.py` | 比对部署版与仓库版，并对部署版跑回放 |

工作流：改 `build.py` → 跑 `build.py` → 跑 `replay_docked_flapping.py` →
提交到 HA → 跑 `fetch_deployed.py` + `verify_deployed.py` 确认部署的那份行为一致。

`verify_deployed.py` 存在的原因：中间可能经过 `python_transform`、schema 归一化，
所以「仓库里的配置对」不等于「HA 里的配置对」。

## 危险区语义（最容易搞反的地方）

配置的区域**包含充电桩**，所以它是「不能关门」的危险区：

| 状态 | `outside_danger_zone` | `in_danger_zone` |
| --- | --- | --- |
| 停在基站 | `off` | `on` |
| 出门清扫 | `on` | `off` |

所以「离开」用 `outside_danger_zone` 从 `off` 变 `on` 判断，**不是** `in_danger_zone` 变 `on`
（那个在基站时就是 `on`）。

## 四个阶段

- **A 启动** —— 集成的 `garage_guard` 在下发命令前开门并等门全开。
- **B 离开** —— `outside_danger_zone` 从 `off` 变 `on` 后：暂停 → 关门 → 恢复 → **确认真的恢复了**。
- **C 返回** —— 状态变成回基站/洗拖布，**且扫地机确实不在基站上**，才开门。
- **D 停靠** —— 任务结束、已回基站、**危险区传感器确认它在区内**，才关门。

## 不能去掉的守卫

每一条都对应一次真实故障，去掉会重新引入：

### 1. `leave` 触发器的 `from: "off"`

没有它，`to: on` 会在**每一次**进入 `on` 时触发，而危险区传感器不只在扫地机离开时变 `on`：
清扫中它会因为样本过期而在 `on → unknown → on` 之间反复跳。

实测（2026-10-08 19:41）：地图读取间隔中位数 6 秒、经常 11 秒，而样本只被信任
`2 × 5 秒 = 10 秒`，所以替代品到达前样本就过期了。那次清扫里传感器跳了 4 次，
每次 `unknown → on` 都算一次新的 `to: on`，**一次离开被算成 4 次**，
机器人被暂停 3 次（19:42:00、19:42:38、19:43:21，每次都紧跟在一次恢复之后）。

加 `from: off` 后：真实离开（`off → on`）触发，抖动恢复（`unknown → on`）不触发。

### 2. B 阶段要求柜门是开着的

纵深防御。万一还有重复边漏进来，对已经关好的门不会再暂停一次机器人。

### 3. D 阶段要求传感器确认它在区内

停靠时机器人就在危险区里，`outside_danger_zone` 应该是 `off`。如果读数是过时的 `on`
（「已离开」），关下去就会夹住机器。没确认时门保持开启**并且告警**。

### 4. B 阶段的恢复必须先确认集成在、再确认真的动了

这是全篇唯一调用**集成自有服务**的步骤，所以也是唯一会因为「自动化看不见的原因」失败的步骤。

2026-10-13 21:47 → 2026-10-14 01:18，集成整整掉了约 3.5 小时：HA 装不上
`python-roborock==5.5.1`（pypi 连不上），于是 `async_setup` 根本没跑，10 个
`roborock_plus.*` 服务一个都不存在，`vacuum.g20s_ultra` 等实体全部 `unavailable`。
Spook 正是在这个窗口里报「这条自动化引用了无法识别的动作」——它说的是**一次真实的
掉线**，不是自动化写错了。（网络恢复后集成加载，Spook 自己撤回了那条报告。）

裸调用在这个窗口里帮不上忙：`continue_on_error` **不覆盖** `ServiceNotFound` ——
`homeassistant/helpers/script.py` 里 `ServiceNotFound` 是被显式重新抛出的，
因为「调用一个不存在的动作」被当作配置错误而不是运行时错误。所以唯一的办法是
**不调用**，于是有了 `unavailable` 前置检查。

另一半是「没报错」不等于「动了」：恢复后要等 vacuum 离开 `paused` 才算成功，
成功通知放在整个块**之后**，任何失败路径都够不到它。两条失败路径都有告警：

| 情况 | 行为 |
| --- | --- |
| 集成未加载（实体 `unavailable`/`unknown`） | 告警「集成未加载，任务未恢复」+ `stop` |
| 恢复命令报 `resume_not_available`（暂停中重启，`last_resume_command` 丢失） | 被 `continue_on_error` 接住，进入等待 |
| 命令发了但 60 秒内没离开 `paused` | 告警「恢复任务失败」+ `stop` |

三种情况都会**如实告警**，因为此时扫地机停在关着的柜门后面、不会自行移动，
静默等于让用户以为它在正常工作。

## 超时策略

任何等待超时都**只告警、不强行动作**。门与扫地机状态不一致时，机器人可能在门洞里，
没有安全的自动恢复方式。所有 `wait_template` 都设了 `continue_on_timeout: true`，
失败路径只发通知然后 `stop`。

## 与其它自动化的关系

- `automation.garage_door_sync_folding_door` —— 折叠门打开时开门，方向一致，不冲突。
- `automation.che_ku_men_chang_shi_jian_wu_ren_zi_dong_guan_bi` —— 无人 1 小时后关门。
- 蓝图自动化（单键循环 `event.vacuum_garage_door_click`）—— 手动按键控制。

门是**手自一体**的，用户随时可以自己开。所以「门在任务中被打开」不一定是故障，
不要试图用自动化把它强行关回去。
