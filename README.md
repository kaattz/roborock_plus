# Roborock Plus

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

当前发布：**v0.7.6**，修复门反馈恢复后计时或估算开门仍被旧标记误拦的问题；新显式请求重新等待完整行程与余量，新车库控制默认关闭。[更新记录](CHANGELOG.md)

`Roborock Plus` 是一个 Home Assistant 自定义集成，基于官方内置 `roborock` 集成演化而来。

这个项目的目标很明确：

- 保留官方 `roborock` 的主体能力
- 补齐更完整的 `pause/resume` 语义
- 作为独立第三方集成存在，可与内置版并存

## 适用场景

如果你遇到下面这类问题，这个集成就是为你准备的：

- 扫地机暂停后，继续任务时会重新开始，而不是继续原任务
- 你需要在自动化里显式区分“开始任务”和“恢复任务”
- 你希望保留官方内置 `roborock`，同时安装一个增强版并行验证

## 当前增强点

目前 `Roborock Plus` 已经补上这些能力：

- 为暂停后的任务增加统一的恢复分发逻辑
- 支持全屋、划区、房间、回基站、建图等任务类型的恢复判定
- 新增显式服务 `roborock_plus.resume_task`
- 新增 `task_active` 语义实体：区分“设备正在动作”和“任务仍然存在”，暂停也算任务在
- 支持按机器人启用原生车库控制：统一门状态判断、通行互锁和标准出入库流程，默认任务期间保持门开
- 保留旧全局柜门 guard，升级不会自动迁移或接管门
- 可配置本地状态轮询间隔（1–60 秒），只刷新状态类实体
- 支持填写自建服务器地址（区域选择 `Manual`），可对接 [local_roborock_server](https://github.com/Python-roborock/local_roborock_server)

## 原生车库控制：集成管流程，自动化管家庭策略

新车库控制是 **opt-in（需要明确启用）**。HACS 更新集成本体后，标准流程就在集成内，
不需要复制柜门状态机 YAML。没有启用新绑定时，原有配置继续走旧路径。

部署前先看[一页配置、迁移和验收清单](docs/garage-control-checklist.md)，其中解释端点极性、完整行程和旧门控交接。

v0.7.1 支持 **V1 + `hold_open` / `after_exit`**，新控制默认关闭。默认 `hold_open`：HA 发起需要通行的请求，先开门、等待所选来源
允许通行，再下发机器人命令；任务期间不主动关门，回洗拖布、补电和自主继续不算任务结束。
只有确认本次任务终结、稳定停靠、基站位置自洽且所有关闭守卫通过，才会收尾关门。
危险区包含基站；最终停靠关门使用停靠守卫，不把“在危险区外”当成最终关闭的条件。

v0.7.1 补齐 **`after_exit`（离库后关门）**：出库不额外暂停，确认离开危险区后关门；
实际返航且门未允许通行时，暂停并确认 → 开门并确认 → `app_resume` 继续原返航。
中途洗拖布/补电停靠时保持开门，再次离库再次关门；最终仍按本次任务终结和稳定停靠收尾。
用户暂停或停止会撤销自动恢复授权；暂停、门位或原返航未确认时阻塞报告，不补发回充/新清扫。
活动会话关联异常不阻断返航制动：仍先确认暂停并开门，再保持暂停告警；后续记录恢复不能重新取得自动恢复授权。
v0.7.0 标签中的代码尚未提供此模式。Q7、Q10、A01 和多机器人共用一扇门不在范围；不同 V1 可以分别绑定不同门。
配置范围不代表所有 V1 机型已经做过硬件验证。隔离 Home Assistant 2026.9.4 已验证原生
加载、表单、服务和内存完整流程；实际时长、极性、停靠与任务保持行为仍由用户操作设备验收。
仓库回放不能替代实测；本批待关闭事项见[实施计划](specs/2026-10-15-plan-garage-control.md)。

独立回充按本次返航和稳定停靠收尾，不要求虚构清扫记录。导航须先观测移动，再以
两次新鲜、可信且无活动标志的 idle 样本确认停稳；区外导航结束不自动关门，
可再明确请求回充或清扫。这只确认停稳，不承诺精确命中目标坐标。

若在没有受管活动会话时观测到 App 等外部任务，旧导航/空闲记录的授权会失效，
并显示阻塞故障；之后停稳、重启或调用 reset 都不会重新授权该旧记录。
外部未知任务不能被当作已完成的本次任务，也不能靠复核服务清空上下文。

### 配置来源与标定

在集成选项首页顶部勾选“继续配置单机车库控制”，提交后选择本条目的 V1 扫地机。
首页仅保留“运行监测”（默认展开）和“地图显示”（默认折叠）两个分组，仍在同一页提交；地图项使用中文名称与说明。
“旧版兼容设置”分组及旧“柜门保护”“柜门实体”字段已移出界面；已有存储值保留，不自动停用旧保护，也不自动改动门/机器人流程或绑定。
不继续配置车库时，本页提交即保存；勾选继续时，本页改动在车库流程最后与车库设置一起保存。
运行监测数值正常预填；实际缺项或无效数字提交后，顶部中文提示会列出具体字段名，并保留已填内容。升级后的现场界面、保存和物理验收仍待用户完成。
绑定按稳定 duid 保存；控制门的 cover 与全开、全闭的判断来源分开配置。

配置分为三步：①基本设置/来源，②所选反馈，③相关时长/交接。
首屏不再展开全部参数；触点只配触点及稳定时间，真实位置只配位置与稳定时间，
计时/估算才要求对应方向完整行程与余量。已有值保留，切换来源时补齐新必填项，不猜硬件参数。

v0.7.2 为缺失辅助项预填余量10秒、触点稳定1秒、位置稳定2秒、停靠稳定5秒，超时初值至少90秒并参考已知行程；已有值不覆盖。真实行程、阈值和极性仍需用户实测。必填项有标记，提交错误同时在页顶和字段显示；例如超时21秒会明确提示须大于开门35秒加余量10秒所得的45秒。v0.7.1 标签尚不包含这项配置体验修复。

v0.7.3 已修复最后提交时出现“Unknown error occurred”的重载冲突，配置保存后只重载一次。

| 来源 | 使用条件 | 到位显示 |
| --- | --- | --- |
| `contact` | 选定端点 binary_sensor，并实测到位是 `on` 还是 `off`；全开、全闭不能使用同一个实体 | 稳定到位后为已确认 |
| `position` + `measured` | 已核实为真实物理位置反馈，实测开关阈值，满足 `0 ≤ 全闭 < 全开 ≤ 100` | 稳定到位后为已确认 |
| `position` + `estimated` | 位置可能是目标回声或软件估算，必须接受估算风险并填写完整行程 | 仅为预计到位 |
| `timed` | 无对应反馈时明确选择，填写完整行程并接受估算风险 | 仅为预计到位 |

两个方向独立：只有全闭触点时，全开可以明确选计时；没有全闭不等于已经全开。
`current_position` 字段存在、cover 支持设置位置，都不能证明位置是真实测量。
所选来源失联时判为未知，不自动改用计时。计时无法发现卡门、电机空转或通路障碍。

v0.7.6 中，`timed` 或 `position` + `estimated` 的反馈恢复后，新显式请求不再被旧失效标记误拦，但必须重新等待该方向完整行程加余量。以本门35秒行程+10秒余量=45秒为例，这不是通用硬件默认值；旧等待作废，不会因反馈恢复而复活。
已锁存的故障（fault）或人工保持（manual_hold）不会自动清除；`reset_garage_control` 只复核，不发送设备命令。活动任务复核后仍无通行许可时保持 `manual_hold`，等待用户明确请求 `resume_task`，再重新开门、重新计时，并沿用原会话 sid。

新绑定没有通用的硬件标定值。用户只需核实所选来源对应的稳定时间、估算行程/余量，
以及动作超时和停靠稳定时间；动作超时须覆盖该来源的最长证据等待。
低开位或高关位阈值尤其要验证实际通行和闭合空间。旧门的 95/5、45 秒只是旧配置建议，
不会自动套用。可保存不完整的未启用草稿；来源离线可以保存配置，但不能由此获得通行许可。

### 从旧 guard／状态机交接

1. 在没有正在清扫或待续任务时操作，确认机器人安全停靠；先保存未启用的单机草稿。
2. 核对所有会操作同一扇门的自动化，停用旧 `roborock_garage_door_statemachine` 及其
   `*_plus` 前身的主动门控。升级不会按名称自动关闭或删除任何家庭自动化。
3. 逐项处理无人关门、洗衣机占用、自动解锁和手动按键：自动关门改走
   `roborock_plus.request_garage_close`；释放离合须在可靠停稳后；保留手动控制并接受它会进入人工保持。
4. 多机使用旧全局 guard 时，逐台确认保护关系。同门仍被另一台机器人依赖时，新绑定拒绝接管；
   可将另一台绑定到它自己的门，不能静默解除它的保护。同一机器人的旧 guard 可以转发到新控制器。
5. 完成来源标定后明确勾选交接确认，再启用新绑定。单机器人也需要确认；不同时运行新旧主动状态机。

首次交接仍需两次成功状态样本跨越停靠稳定窗口，结合位置核实空闲；仅勾选确认不会直接放行。
重启后只恢复核对，不重放清扫或门动作；已运行过却丢失会话记录时会阻塞报告，不能当成首次安装。

### 留给家庭自动化的接口

| 接口 | 用途 |
| --- | --- |
| `close_permission_entity` | 可选 input_boolean / binary_sensor，只读；仅 `on` 允许关闭，其他状态拒绝。留空表示没有家庭附加限制 |
| `roborock_plus.request_garage_close` | 以 vacuum 为目标请求关闭，重新检查任务、停靠、位置和许可等守卫，无 `force` 绕过 |
| `roborock_plus.reset_garage_control` | 重新观察并复核可恢复故障及人工保持，不发送设备命令、不清除活动任务；无通行许可的活动任务仍保持人工保持 |
| `garage_open_confirmed` / `garage_closed_confirmed` | 各方向的物理确认；仅估算或无法判断时为 unknown |
| `garage_passage_allowed` / `garage_phase` / `garage_problem` | 当前通行许可、流程阶段和阻塞故障；阶段属性含会话、原因与来源质量 |
| `roborock_plus_garage_issue` 事件 | 故障激活/解除事件，家庭自动化自行选择通知渠道 |

清扫日程、房间选择、家庭占用和通知继续由自动化决定。家庭许可限制关闭，不能绕过安全守卫；
许可恢复也不清除人工保持或故障。人工操作优先，控制器不会立即抢回门，下一次明确请求仍需完整检查。
许可输入不替代硬件防夹，也不承诺能撤回已经发出的门动作。

Roborock App、厂商定时和机器人实体按键不经过 HA 命令入口，无法事前互锁。
观察到机器人已经动作也不能补回这一时机；使用这些入口前，应由用户确保通道安全。

## 旧方案（legacy）：这套东西怎么配合

以下保留旧全局 guard 与 YAML 状态机的分工和验证历史，仅适用于**尚未启用新绑定**的配置。
在旧方案中，**集成、三个自动化、一个蓝图是一套系统**；迁入原生车库控制后，不再需要状态机承担标准流程。

### 为什么必须配合

难点在于**柜门**。扫地机停在柜子里，出门前门必须开，出门后门必须关（门是共用的）。而这件事分成两半，各自都做不全：

| 谁 | 能做 | 做不到 |
| --- | --- | --- |
| **集成** | 在命令**下发前**开门并等门全开 —— 它在命令通路上，可以阻塞 | 看不到机器人**之后**的动向 |
| **自动化** | 看机器人**之后**的动向，决定何时关门、何时再开 | 做不到"命令下发前就把门开好"：等它看到机器人动了，机器人已经在动了 |

所以：**集成负责"出门前"，自动化负责"出门后"**。蓝图则是"什么时候开始"。

### 一条完整的时序

```
蓝图（到点触发）
  │  设拖地意图 → vacuum.clean_area
  ▼
集成 garage_guard ──────── 开门 → 等 position ≥ 95 → 才下发清扫命令
  │                        （门没开到位就不发命令，机器人不会被关着的门挡住）
  ▼
命令已下发
  ├─ 自动化③ 清扫没启动？ ──→ 只告警，不关门
  ▼
机器人出门中
  ├─ 自动化② 卡住 / 设备报错？ ──→ vacuum.stop + 告警（绝不自动关门）
  ▼
走出危险区
  └─ 自动化① 柜门状态机 ──→ 暂停 → 关门 → roborock_plus.resume_task（集成提供）
  ▼
清扫中（中途回基站洗拖布？门全程开着）
  ▼
回基站途中
  └─ 自动化① ──→ 开门
  ▼
任务结束 + 已停靠 + 危险区确认
  └─ 自动化① ──→ 关门
```

**三处关键分工**：

1. **开门靠集成，不靠自动化。** 开门必须在命令下发**之前**完成，只有走命令通路的集成代码能做到。这也是为什么"监控状态 → 开门"的方案必然有撞门窗口。
2. **恢复任务靠集成的 `resume_task`。** 原生 `vacuum.start` 在暂停后会重开任务而不是续上；关门流程必须暂停机器人（否则门会夹到正在移动的它），暂停后又必须能续上原任务。
3. **判断"清扫真的启动了"靠集成的 `task_active`。** 服务调用返回 200 不等于机器人动了 —— 这是实测过的坑（见下文）。

### 各部分清单

| 部分 | 位置 | 作用 |
| --- | --- | --- |
| **集成** | `custom_components/roborock_plus/` | 开门守卫、恢复语义、状态实体、失败事件 |
| **自动化①** | [automations/roborock_garage_door_statemachine.yaml](automations/roborock_garage_door_statemachine.yaml) | 出门后关门、回基站前开门、停靠后关门 |
| **自动化②** | [automations/roborock_stuck_or_error_stop.yaml](automations/roborock_stuck_or_error_stop.yaml) | 卡住或设备报错时停止并告警 |
| **自动化③** | [automations/roborock_clean_command_not_started.yaml](automations/roborock_clean_command_not_started.yaml) | 命令下发了但任务没启动时告警 |
| **蓝图** | [blueprints/扫地机分区定时清扫.yaml](blueprints/扫地机分区定时清扫.yaml) | 分区定时清扫，可选"区域内有人则跳过并重试" |

**自动化和蓝图靠人工加入 HA，不在 HACS 流程里**（HACS 没有这两类）。
细节见 [automations/README.md](automations/README.md)。

### 集成提供的接口（自动化依赖这些）

| 实体 | 作用 |
| --- | --- |
| `binary_sensor.<vacuum>_task_active` | 任务是否仍然存在（暂停也算在） |
| `binary_sensor.<vacuum>_outside_danger_zone` | 是否已离开危险区（出门信号） |
| `binary_sensor.<vacuum>_in_danger_zone` | 是否位于危险区（关门确认） |
| `binary_sensor.<vacuum>_danger_zone_configured` | 是否已配置危险区 |
| `binary_sensor.<vacuum>_stuck` | 是否应该移动却停着不动 |

| 服务 / 事件 | 作用 |
| --- | --- |
| `roborock_plus.resume_task` | 恢复已暂停的任务（区别于"重新开始"） |
| `roborock_plus.set_safe_zone` 等 | 危险区的读写 |
| 事件 `roborock_plus_vacuum_stuck` | 集成判定卡住 → 自动化② 停止并告警 |
| 事件 `roborock_plus_clean_command_not_started` | 命令没生效 → 自动化③ 告警 |

> 危险区只表示位置，不能代替 `returning_home` 判断回基站意图；否则普通清扫靠近柜门时会误触发。

## 旧柜门保护（legacy）

扫地机停在柜子里时，需要先开门才能出 dock。`Roborock Plus` 会拦截 HA 发起的清扫命令：

- 覆盖 `vacuum.start`、房间清扫、`vacuum.send_command` 和清洁例程按钮
- 调用配置的 cover，等待 `current_position >= 95` 才下发扫地机命令
- 只影响从 Home Assistant 发起的命令；Roborock App 发起的命令不经过 HA，无法拦截

关门需要配合自动化：等扫地机离开危险区后再暂停、关门、恢复任务。这个自动化就是
[automations/roborock_garage_door_statemachine.yaml](automations/roborock_garage_door_statemachine.yaml)，
它依赖下面这些由集成提供的实体：

| 实体 | 作用 |
| --- | --- |
| `binary_sensor.<vacuum>_task_active` | 任务是否仍然存在（暂停也算在） |
| `binary_sensor.<vacuum>_outside_danger_zone` | 是否已离开危险区 |
| `binary_sensor.<vacuum>_in_danger_zone` | 是否位于危险区 |
| `binary_sensor.<vacuum>_danger_zone_configured` | 是否已配置危险区 |
| `binary_sensor.<vacuum>_stuck` | 是否应该移动却停着不动 |

> 危险区只表示位置，不能代替 `returning_home` 判断回基站意图；否则普通清扫靠近柜门时会误触发。

### ⚠️ 「命令成功」不等于「机器人动了」

这是柜门保护的一个真实盲区。清扫命令是**异步执行**的：例程在服务器内部跑，HTTP 调用只要被接受就返回成功。如果例程随后失败（例如某条命令被固件拒绝），**扫地机根本不会启动，但 HA 会认为一切正常**。

后果是门被打开了、活没干、却没有任何东西察觉。而上面那套自动化全都以「机器人已经动了」为前提（触发条件是 `docked` → `cleaning`），所以它们一个都不会触发。

为此集成会**在受保护的清扫命令下发后观察一段时间**：如果始终没有任务开始，就发出事件：

```yaml
event_type: roborock_plus_clean_command_not_started
data:
  command: execute_scene          # 或 app_start / app_segment_clean …
  entity_id: vacuum.g20s_ultra
  timeout: 60
  entry_id: <config entry id>
```

配套自动化就是
[automations/roborock_clean_command_not_started.yaml](automations/roborock_clean_command_not_started.yaml)
（通知方式由你决定，本仓库用的是 `script.alert_notify`）。它做的事：

```yaml
triggers:
  - trigger: event
    event_type: roborock_plus_clean_command_not_started
actions:
  - action: script.alert_notify
    data:
      level: warning
      title: 扫地机没有启动
      message: >-
        {{ trigger.event.data.command }} 已下发，但
        {{ trigger.event.data.timeout }} 秒内扫地机没有开始任务，
        柜门可能一直开着。
```

选项里的「清扫启动确认时间」控制等待秒数，填 `0` 关闭。默认 60 秒。

> **本集成不会因为这个事件自动关门。** 命令失败并不等于扫地机没动（可能只是状态回报滞后），而柜门是共用的（兼作洗衣机柜门），贸然关门有夹机或困人的风险。事件只负责让你知道，关不关由你和自动化决定。

### ⚠️ 任务启动了，但扫地机卡住了

上一个盲区是「命令没生效」。这个是「命令生效了，但机器人动不了」——任务一直是活的，所以上面那套以
`docked` → `cleaning` 为触发条件的自动化**一个都不会触发**：

- 门没开，扫地机一直顶着门
- 清扫完回基站，**开着的门板挡住了回充路径**，一直回不去

这两种情况在 HA 里的表现完全一样：任务进行中，但没有位移。

集成通过**坐标采样**识别它：任务状态属于「应该移动」时，如果坐标在一段时间内始终没有超出容差范围，
就判定卡住。

| 实体 | 作用 |
| --- | --- |
| `binary_sensor.<vacuum>_stuck` | 是否卡住（附带 `seconds_stuck` 属性） |

```yaml
event_type: roborock_plus_vacuum_stuck
data:
  entity_id: vacuum.g20s_ultra
  x: 25728
  y: 24233
  state: returning_home      # 卡住时的状态
  seconds_stuck: 132
  entry_id: <config entry id>
```

配套自动化就是
[automations/roborock_stuck_or_error_stop.yaml](automations/roborock_stuck_or_error_stop.yaml)
（停止 + 通知）：

```yaml
triggers:
  - trigger: event
    event_type: roborock_plus_vacuum_stuck
actions:
  - action: vacuum.stop
    target:
      entity_id: "{{ trigger.event.data.entity_id }}"
  - action: script.alert_notify
    data:
      level: warning
      title: 扫地机卡住了
      message: >-
        状态 {{ trigger.event.data.state }}，在
        ({{ trigger.event.data.x }}, {{ trigger.event.data.y }})
        停留 {{ trigger.event.data.seconds_stuck }} 秒，已停止。
```

> **本集成只负责上报，不会自己停止扫地机。** 停止是一个决定，放在自动化里你才能加自己的条件、
> 也才能在 trace 里看到它为什么执行。

#### 为什么不会在洗拖布时误报

判断依据是一份**「应该移动」的状态白名单**（`cleaning`、`returning_home`、`docking`、
`going_to_wash_the_mop` 等），而不是 `binary_sensor.<vacuum>_task_active`。

后者回答的是「任务还在不在」，所以**暂停、洗拖布、回充时它依然是 `on`**。用它做判断，
每次洗拖布都会误报一次卡住。白名单只列出确定该动的状态，没列到的状态就是不判断 ——
万一将来固件加了新状态，也只是少判一次，而不会误停一台正常工作的扫地机。

清扫中途回基站充电、洗拖布都属于「合法静止」，因此被排除在判断之外。

#### 相关选项

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| 卡住检测 | 开 | 总开关 |
| 卡住判定时长 | 120 秒 | 坐标在容差内停留多久算卡住 |
| 卡住判定半径 | 200 | 坐标容差（地图单位）。静止采样本身有 1~2 单位抖动，所以需要容差 |

> 检测质量取决于采样密度：判定窗口内需要有若干次有效采样。自建服务器默认 5 秒采样，
> 120 秒窗口内有 24 次。**官方云 60 秒采样下这个功能会明显变粗**，这是限流约束带来的固有限制，
> 不是可以通过调参解决的。

### ⚠️ 地图坐标来自服务器，不是本地

这一点很重要：`map_content` / `maps` 这两个 trait 在 `python-roborock` 里被标记为
`@common.map_rpc_channel`，**强制走 MQTT 且没有本地回退**。也就是说，危险区传感器读的坐标
**始终来自服务器**，无论机器人是否在局域网内可直连。

因此轮询频率不能只看本地连接，必须看服务器是谁：

| 服务器 | 清扫中采样 | 空闲采样 | 说明 |
| --- | --- | --- | --- |
| Roborock 官方云 | 60 秒 | 300 秒 | 太快会被限流甚至封号 |
| 自建服务器 | 5 秒 | 60 秒 | 只消耗本地资源，可以放心加快 |

选项里的「危险区坐标轮询间隔」填 `0`（默认）即按服务器自动选择。手动填写时：

- **官方云有 30 秒硬下限**，填更小的值会被自动夹到 30 秒 —— 防止误填导致账号被限流
- 自建服务器无此限制，可填到 1 秒

> 坐标样本最多容忍 **1 次**读取失败就转为 `unknown`。这是失败安全的：过期的「已离开柜门」
> 绝不能被用来判断可以关门，否则机器人折返时会被门夹住。

### 为什么柜门自动化建议配自建服务器

柜门状态机等 `outside_danger_zone` 翻转的窗口是 **2 分钟**。官方云下 60 秒才采样一次，
最坏情况第一个新坐标就要等 60 秒，再叠加一次失败就越过窗口 —— 属于「能用但很紧」。
自建服务器把采样缩短到 10 秒，这个窗口就非常从容。这是切本地服务器最实际的理由。

## 对接自建服务器

在集成配置的区域下拉里选择 `Manual`，填入你的服务器地址（例如 `https://api-rr.example.com:555`），
然后按 [local_roborock_server 文档](https://python-roborock.github.io/local_roborock_server/) 填写本地登录邮箱和 6 位 PIN 作为验证码。

切换到自建服务器后，危险区坐标轮询会自动放宽到 10 秒（不需要手动改选项）。

## HACS 安装方法

### 方式一：通过 HACS 自定义仓库安装

1. 打开 HACS。
2. 进入右上角菜单 `Custom repositories`。
3. 仓库地址填写：`https://github.com/kaattz/roborock_plus`
4. 分类选择：`Integration`
5. 添加后，在 HACS 中搜索 `Roborock Plus`
6. 点击下载并重启 Home Assistant
7. 在 Home Assistant 集成页面中添加 `Roborock Plus`

### 方式二：手动安装

1. 将 `custom_components/roborock_plus` 整个目录复制到你的 Home Assistant 配置目录下
2. 重启 Home Assistant
3. 在集成页面添加 `Roborock Plus`

## 配置后注意事项

- 这是一个**独立域名**的第三方集成：`roborock_plus`
- 它不会覆盖内置 `roborock`
- 你可以同时保留内置版和 `Roborock Plus`
- 但两者会生成**不同的实体**，自动化需要迁移到新的实体 ID

## 自定义服务

### 可视化安全区编辑面板

安装集成后，侧边栏会出现 `Roborock Plus Zones` 面板。

你可以在这个面板里：

- 选择 `roborock_plus` 的 vacuum 实体
- 加载当前地图
- 生成建议框
- 在地图上拖拽矩形框选安全区
- 保存或清空安全区

> 当前这一版是第一版可用实现，重点是先把“能框、能存、能读”做通。

### `roborock_plus.resume_task`

显式恢复当前已暂停的任务。

适合这些场景：

- 你不想依赖 `vacuum.start` 的默认语义
- 你要在自动化里稳定区分“启动任务”和“恢复任务”
- 你有柜门、门禁、回充联动这类复杂流程

暂停时集成会记住“该用哪个命令恢复”，因此中途回洗拖布、回充这类状态下也能正确续上原任务。

### 危险区相关服务

| 服务 | 作用 |
| --- | --- |
| `roborock_plus.get_dock_position` | 返回基站参考坐标 |
| `roborock_plus.get_safe_zone` | 返回已保存的危险区 |
| `roborock_plus.set_safe_zone` | 保存危险区 |
| `roborock_plus.clear_safe_zone` | 清空危险区 |
| `roborock_plus.get_safe_zone_suggestion` | 按方向和尺寸给出建议框 |
| `roborock_plus.get_safe_zone_editor_context` | 返回编辑器需要的地图与坐标信息 |
| `roborock_plus.get_maps` | 返回地图与房间列表 |
| `roborock_plus.get_vacuum_current_position` | 返回扫地机当前位置 |
| `roborock_plus.set_vacuum_goto_position` | 让扫地机前往指定坐标 |

## 当前仓库结构

```text
custom_components/roborock_plus/    集成本体（HACS 分发这一部分）
automations/                        三个扫地机自动化 + 说明（人工加入 HA）
blueprints/                         分区定时清扫蓝图（从 GitHub URL 导入）
scripts/                            构建、验证、变异检查工具
tests/                              测试
docs/plans/                         设计文档
hacs.json
README.md
```

**HACS 只分发 `custom_components/roborock_plus/` 这一个目录。** 自动化和蓝图不在
HACS 的类别里（它只支持 AppDaemon / Dashboard / Integration / Python Script /
Template / Theme），所以单独维护，见 [automations/README.md](automations/README.md)。

## 兼容性说明

- Home Assistant：见 `hacs.json` 和 `manifest.json`
- 安装方式：HACS 自定义仓库 / 手动复制
- 集成类型：`integration`

## 开发

运行测试：

```bash
python -m pytest tests -q
```

测试覆盖恢复命令选择、危险区几何、柜门保护、轮询策略、状态表互斥和翻译文件完整性。

改蓝图或自动化后，提交前跑这两个：

```bash
python scripts/validate_blueprints.py    # 蓝图结构（HA 导入时才会发现的结构错误）
python scripts/mutation_check_blueprint.py   # 确认上面的检查真的能抓到问题
```

`validate_blueprints.py` 不是多余的：蓝图第一版的**每个 Jinja 模板都渲染正确**，
但被 HA 导入器拒收 —— `then:` 的缩进落在了 `if:` 的条件列表里。只渲染模板
永远发现不了这类结构错误。

## 项目状态

已完成的主线：恢复语义、危险区/柜门保护、状态轮询、卡住检测、位置可信度、
三个自动化与分区定时清扫蓝图。

如果你只是想直接替代内置集成，这个项目**不是覆盖版**，而是**并存版**。

## 已知问题

- **`last_resume_command` 在 HA 重启后丢失。** 它存在内存里，用于暂停后
  恢复原任务。若恰好在"暂停中"重启，`resume_task` 会因为没有兜底命令而报
  `resume_not_available`。集成已经尽量从设备状态推导，但暂停态本身信息不足。
- **官方云下卡住检测精度受限。** 采样 60 秒时，120 秒窗口内只有 2 个样本，
  判定会明显变粗。这是限流约束带来的固有限制，调参解决不了。
- **App 发起的清扫不受保护。** 从 Roborock App 启动的命令不经过 HA，
  `garage_guard` 拦不到，柜门不会自动打开。这是架构限制，不是 bug。
- **依赖装不上时整个集成不会加载。** `manifest.json` 声明的
  `python-roborock==5.5.1` 由 HA 在启动时从 pypi 安装；网络不通时
  `async_setup` 根本不会执行，所有 `roborock_plus.*` 服务和实体一起消失。
  2026-10-13 那晚就发生了约 3.5 小时。此时 Spook 会报自动化引用了
  「未知动作」—— 那是**掉线的症状，不是配置错误**，不要按它的建议删掉动作。
  柜门状态机已经对这种情况做了前置检查（见
  [scripts/garage_state_machine/README.md](scripts/garage_state_machine/README.md) 守卫 4）。

## 致谢

本项目基于 Home Assistant 官方 `roborock` 集成修改而来，保留其原有整体结构，并针对暂停/恢复行为做增强。
