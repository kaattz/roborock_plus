# Roborock Plus

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

当前版本：**v0.12.1**

基于官方内置 `roborock` 集成演化的 Home Assistant 自定义集成。除了保留原有能力、补齐暂停恢复语义之外，主要解决一件事：
扫地机停在一扇电动门后面，出门清扫时门要开，回来时门要开，其余时间门要关，而任何一步出错都不能让它撞门。

- 可与内置 `roborock` 并存
- 暂停后继续原任务，而不是重新开始
- 原生支持「扫地机在门内，靠一扇 `cover` 出入」的部署
- 可对接自建服务器 [python-roborock/local_roborock_server](https://github.com/Python-roborock/local_roborock_server)

---

## 目录

1. [功能](#功能)
2. [和电动门配合](#和电动门配合)
3. [安装](#安装)
4. [连接方式](#连接方式)
5. [配置参数](#配置参数)
6. [实体](#实体)
7. [服务](#服务)
8. [配套蓝图](#配套蓝图)
9. [故障排查](#故障排查)
10. [已知限制](#已知限制)

---

## 功能

### 暂停与恢复

官方集成没有区分任务类型。暂停后点继续，有时会重新开始而不是接着扫。这个集成记录了暂停时的任务类型（全屋、划区、房间、回基站、建图），`vacuum.start` 会恢复原任务。也可以直接调用 `roborock_plus.resume_task`。

### `task_active`

`vacuum.*.state` 表达不了「任务还在不在」。暂停时它是 `paused`，但任务其实没结束；中途回充时它是 `charging`，但机器人还会再出来。

`binary_sensor.<robot>_task_active` 补上这个语义：

| 情况 | `vacuum.state` | `task_active` |
| --- | --- | --- |
| 正在清扫 | `cleaning` | `on` |
| 清扫中暂停 | `paused` | `on` |
| 中途回充 | `charging` | `on` |
| 任务结束回基站 | `returning` | `on` |
| 已结束 | `docked` / `charging` | `off` |

判断清扫是否真的在跑，用这个，不要看服务调用有没有报错。命令被服务器接受，不代表机器人动了。

### 电动门协同

见下一节。

### 危险区

在扫地机地图上标一个矩形，代表门开关会扫到的空间。集成据此判断机器人是否在区内，并给出可视化编辑面板和出库点推算。

### 卡困检测

`binary_sensor.<robot>_stuck`，依据位置长时间不动、任务仍在进行等信号判断。

### 地图与坐标

地图图片实体（图层可配），以及查询机器人位置、充电座位置的服务。

---

## 和电动门配合

### 场景

充电座在门内侧。机器人要出门清扫，门得开；扫完回来，门得开；其余时间门关着。这扇门在 HA 里是一个 `cover`。

难点不在开和关，而在：

- 门「正在开」和「已经全开」是两回事，机器人不能在门没开到位时出发
- 机器人出门后门要及时关，否则敞着几小时
- 机器人回程时门要开，否则它撞门
- 回程中它会暂停，而暂停的机器人看起来像「任务结束了」
- 任何一步没确认就不能继续

### 两种模式

配置项 `task_door_mode`：

| 模式 | 行为 | 适用 |
| --- | --- | --- |
| `hold_open` | 任务期间门一直开着，任务结束才关 | 门在室内，敞着无妨 |
| `after_exit` | 出门后立即关门；回程时暂停 → 开门 → 恢复 → 进站 → 关门 | 门临街或需要随手关 |

`after_exit` 是主要验证的模式，下面的时序都指它。

### 完整时序

```
1. 清扫命令进来
2. 开门 → 等确认全开
3. 受控出库：app_goto_target 到出库点
4. 等机器人报告 trip 结束，并确认在危险区外
5. 关门 → 等确认全闭
6. 下发清扫命令 → 等 task_active = on
7. 清扫中：机器人出区就关门；中途回充或洗抹布时保持开门
8. 任务结束，机器人回程
9. 回程：暂停 → 等确认 → 开门 → 等确认全开 → 恢复
10. 机器人进站 → 关门
```

### 门的反馈来源

`open_source` / `close_source` 三选一：

| 类型 | 说明 | 可靠性 |
| --- | --- | --- |
| `contact` | 门磁 `binary_sensor` | 最好，物理确认 |
| `position` | `cover` 的 `current_position` | 次之，可能不准 |
| `timed` | 按行程时间估算 | 最差，前两者都不可用时才用 |

极性必须自己填。同样表示「全开」，有的门磁报 `on`，有的报 `off`。`open_contact_state` / `close_contact_state` 就是填「这个状态代表到位」。填反的后果是集成把开着的门当成关着。

### 出库方向

`controlled_exit_direction: auto` 取「充电座到危险区中心」的主轴，不取最近的边。

实测几何：

| 边 | 充电座到该边的距离 |
| --- | --- |
| -X | 699 mm |
| +X | 373 mm |
| -Y | 1316 mm（真实出库方向） |
| +Y | 325 mm |

按最近的边会推出 +X 或 +Y，把机器人推向旁边那扇门。可以手动指定方向。

### 相关实体

| 实体 | 用途 |
| --- | --- |
| `binary_sensor.<robot>_garage_fully_open_confirmed` | 门确认全开 |
| `binary_sensor.<robot>_garage_fully_closed_confirmed` | 门确认全闭 |
| `binary_sensor.<robot>_garage_control_problem` | 有故障 |
| `sensor.<robot>_garage_control_phase` | 当前阶段 |
| `binary_sensor.<robot>_in_danger_zone` | 在危险区内 |
| `binary_sensor.<robot>_outside_danger_zone` | 在危险区外 |

排查问题先看 `garage_control_phase` 和 `garage_control_problem` 的 `reason` 属性。

> 实体 ID 里带有 garage 字样，是历史命名，未随文档改动。

---

## 安装

### HACS

1. HACS → 自定义存储库 → 添加 `https://github.com/kaattz/roborock_plus`，类别选「集成」
2. 搜索 `Roborock Plus` 安装
3. 重启 Home Assistant
4. 设置 → 设备与服务 → 添加集成 → Roborock Plus

### 手动

```bash
cd /config
git clone https://github.com/kaattz/roborock_plus.git tmp
cp -r tmp/custom_components/roborock_plus custom_components/
rm -rf tmp
```

重启 Home Assistant。

蓝图不随 HACS 更新，需单独下载，见[配套蓝图](#配套蓝图)。

### 与内置 roborock 并存

可以。两者 domain 不同，实体不冲突。但若两套同时登录同一账号，云端请求会翻倍。

---

## 连接方式

### 云端

安装时选区域（`auto` / `us` / `eu` / `ru` / `cn`），用 Roborock App 的邮箱登录，填收到的验证码。

### 自建服务器

配置时区域选 **Manual**，填服务器地址。配合 [local_roborock_server](https://github.com/Python-roborock/local_roborock_server)：

| 方面 | 云端 | 自建 |
| --- | --- | --- |
| 状态延迟 | 数秒到十几秒 | 亚秒级 |
| 位置采样 | 稀疏，约 30 秒一次丢包 | 密集 |
| 门控可靠性 | 时序判断容易落在采样间隙 | 明显更稳 |
| 隐私 | 数据经厂商服务器 | 局域网内 |

门控更依赖采样密度。判断「是否已出区」「是否已停止」都看状态采样；云端每 30 秒左右有一段约 1.2 秒的丢包，正好落在关键判断上时集成会保守处理（不关门、继续等），代价是延后，不是误动作。

本地状态轮询间隔可配，1–60 秒，默认 1 秒。

---

## 配置参数

配置分两层。

### 集成选项

设置 → 设备与服务 → Roborock Plus → 配置：

| 参数 | 说明 |
| --- | --- |
| 地图可绘制元素 | 路径、充电座、禁区、障碍物、虚拟墙等图层开关 |
| 本地状态轮询间隔 | 1–60 秒，默认 1 |

### 门绑定

选一台机器人后分三步填写。

#### 第一步：门与来源

| 参数 | 键 | 说明 |
| --- | --- | --- |
| 启用门控 | `enabled` | 总开关，默认关 |
| 受控门 cover 实体 | `cover_entity_id` | 那扇电动门 |
| 任务期间门的模式 | `task_door_mode` | `hold_open` 或 `after_exit` |
| 全开判断来源 | `open_source` | `contact` / `position` / `timed` |
| 全闭判断来源 | `close_source` | 同上 |
| 家庭关闭许可 | `close_permission_entity` | 可选；关掉它集成就不会关门 |

#### 第二步：来源反馈

仅当选了 `contact` 或 `position` 时显示。

| 参数 | 键 | 说明 |
| --- | --- | --- |
| 全开门磁 | `open_contact` | 代表门已全开的 `binary_sensor` |
| 全闭门磁 | `close_contact` | 代表门已全闭的 `binary_sensor` |
| 全开到位值 | `open_contact_state` | 该门磁在全开时报 `on` 还是 `off` |
| 全闭到位值 | `close_contact_state` | 同上 |
| 位置反馈类型 | `position_kind` | `measured` 或 `estimated` |
| 全开位置阈值 | `open_position` | 0–100 |
| 全闭位置阈值 | `close_position` | 0–100 |

#### 第三步：时间与安全确认

| 参数 | 键 | 范围 | 说明 |
| --- | --- | --- | --- |
| 开门行程时间 | `open_travel_seconds` | 1–600 | 仅 `timed` 模式使用 |
| 关门行程时间 | `close_travel_seconds` | 1–600 | 同上 |
| 行程余量 | `travel_margin_seconds` | 0–120 | 加在行程时间上 |
| 门磁稳定时间 | `contact_settle_seconds` | 0–60 | 门磁是锁存开关，通常填 0 |
| 位置稳定时间 | `position_settle_seconds` | 0–60 | 位置读数需持续多久才认 |
| 门动作超时 | `operation_timeout_seconds` | 1–1800 | 一次开门或关门最多等多久 |
| 停靠稳定时间 | `dock_settle_seconds` | 1–300 | 判断已停靠需稳定多久 |
| 接受估算位置的风险 | `allow_estimated` | 布尔 | 用 `estimated` 时必须勾 |
| 确认门控权已交接 | `handover_confirmed` | 布尔 | 从旧自动化接管时必须勾 |
| 清扫前受控出库 | `controlled_exit_enabled` | 布尔 | 见下 |
| 出库到达超时 | `controlled_exit_timeout_seconds` | 10–300 | 默认 120 |
| 出库方向 | `controlled_exit_direction` | 五选一 | `auto` / `north` / `south` / `east` / `west` |
| 出库距离 | `controlled_exit_distance_mm` | 1–65535 | 超出危险区边界多少毫米，默认 623 |

### 出库距离的含义

存的是从危险区边界往外多少毫米，不是从充电座算。安全规则约束的是机器人离门多远，也就是离区界多远；「从充电座前进多少」是同一趟行程的另一种说法，界面换算显示，不存储。

填 100 就是走到区外 100mm 处。到达判断以机器人报告 trip 结束为准，坐标只做事后校核（属性 `controlled_exit_arrival_offset_mm` 记录实际差多少）。这样填多少就走多少，不会被容差吃掉。

---

## 实体

以 `vacuum.g20s_ultra` 为例，前缀 `g20s_ultra`。

### 主要实体

| 实体 | 说明 |
| --- | --- |
| `vacuum.<robot>` | 扫地机主体 |
| `binary_sensor.<robot>_task_active` | 任务是否还在进行（暂停也算） |
| `binary_sensor.<robot>_in_cleaning` | 是否在清扫 |
| `binary_sensor.<robot>_stuck` | 是否卡住 |
| `binary_sensor.<robot>_in_danger_zone` | 在危险区内 |
| `binary_sensor.<robot>_outside_danger_zone` | 在危险区外 |
| `binary_sensor.<robot>_garage_fully_open_confirmed` | 门确认全开 |
| `binary_sensor.<robot>_garage_fully_closed_confirmed` | 门确认全闭 |
| `binary_sensor.<robot>_garage_control_problem` | 门控故障 |
| `sensor.<robot>_garage_control_phase` | 门控阶段 |
| `image.<robot>_map` | 地图 |
| `select.*` / `switch.*` / `number.*` / `button.*` | 设置与动作 |

### 阶段取值

| 值 | 含义 |
| --- | --- |
| `idle` | 空闲，门不归集成管 |
| `opening` | 正在开门 |
| `outside` | 机器人在门外 |
| `controlled_exit` | 受控出库中 |
| `closing` | 正在关门 |
| `closed_outside` | 机器人在外，门已关 |
| `pausing_return` | 回程中，正在暂停 |
| `resuming_return` | 门已开，正在恢复回程 |
| `blocked` | 被故障挡住，看 `reason` |

---

## 服务

| 服务 | 说明 |
| --- | --- |
| `roborock_plus.resume_task` | 继续被暂停的任务 |
| `roborock_plus.get_maps` | 地图列表 |
| `roborock_plus.get_vacuum_current_position` | 机器人当前坐标 |
| `roborock_plus.get_dock_position` | 充电座坐标 |
| `roborock_plus.set_vacuum_goto_position` | 指定 goto 目标 |
| `roborock_plus.get_safe_zone` | 读取危险区 |
| `roborock_plus.get_safe_zone_suggestion` | 推算建议危险区 |
| `roborock_plus.set_safe_zone` | 写入危险区 |
| `roborock_plus.clear_safe_zone` | 清除危险区 |
| `roborock_plus.request_garage_open` | 请求开门 |
| `roborock_plus.request_garage_close` | 请求关门 |
| `roborock_plus.reset_garage_control` | 重置门控状态（清 `blocked`） |
| `roborock_plus.start_garage_cleaning_stage` | 多阶段清扫的段启动 |

---

## 配套蓝图

蓝图不随 HACS 更新，需要单独导入。Home Assistant 的蓝图界面可以直接从 URL 导入：

1. 设置 → 自动化与场景 → 蓝图
2. 右下角「导入蓝图」
3. 粘贴下面的地址，确认

扫地机分区定时清扫：

```
https://raw.githubusercontent.com/kaattz/roborock_plus/main/blueprints/%E6%89%AB%E5%9C%B0%E6%9C%BA%E5%88%86%E5%8C%BA%E5%AE%9A%E6%97%B6%E6%B8%85%E6%89%AB.yaml
```

多阶段清扫：

```
https://raw.githubusercontent.com/kaattz/roborock_plus/main/blueprints/%E5%A4%9A%E9%98%B6%E6%AE%B5%E6%B8%85%E6%89%AB.yaml
```

导入后 HA 会把它存在 `blueprints/automation/` 下的一个子目录里，子目录名取自导入地址的
GitHub 账号（`kaattz`）。这个名字不影响使用，蓝图按文件名区分。界面上能直接看到。

地址用的是 `raw.githubusercontent.com`，不是 GitHub 的文件页面。文件页面的 HTML 不是蓝图，导入会失败。

若 HA 无法访问外网，改成手动放置：把这两个文件放进 `blueprints/automation/` 下的任意子目录，
再重载蓝图。放好后的相对路径要写进自动化的 `use_blueprint.path`。

- [`blueprints/扫地机分区定时清扫.yaml`](blueprints/扫地机分区定时清扫.yaml)
- [`blueprints/多阶段清扫.yaml`](blueprints/多阶段清扫.yaml)

### 扫地机分区定时清扫

按房间清扫，支持有人跳过、门磁检查、失败重试、全程通知。

#### 参数

| 参数 | 键 | 默认 | 说明 |
| --- | --- | --- | --- |
| 扫地机 | `vacuum_entity` | — | 只选这一个，其余实体从设备自动推导 |
| 何时清扫 | `my_trigger` | — | 自定义触发器 |
| 区域 | `areas` | — | HA 区域，可多选 |
| 意图 | `cleaning_mode` | 扫拖一体 | 仅扫地 / 扫拖一体 / 仅拖地 |
| 拖地强度 | `mop_intensity` | `medium` | 仅扫地时忽略，强制 `off` |
| 拖地模式 | `mop_route` | `standard` | 走几遍的路线 |
| 人体传感器 | `presence_sensors` | `[]` | 留空＝不检查 |
| 门磁 | `door_sensors` | `[]` | 留空＝不检查；填了就必须都开着才扫 |
| 门开状态 | `door_open_state` | `on` | 门磁开门时报什么；反极性填 `off` |
| 重试窗口 | `retry_window_minutes` | 30 | 人体和门磁共用；0＝只试一次 |
| 重试间隔 | `retry_interval_minutes` | 5 | 窗口 ÷ 间隔 = 尝试次数 |
| 信息发送 | `notify_script` | `script.alert_notify` | 脚本需接收 `title` / `message` / `level` |
| 开始时通知 | `notify_on_start` | `true` | |
| 跳过时通知 | `notify_on_skip` | `true` | 建议开着，否则点了没反应看不出原因 |

#### 通知

| 时机 | level | 标题 |
| --- | --- | --- |
| 配置错误 | warning | 找不到设备实体 |
| 开始 | info | 已开始清扫 |
| 跳过 | info | 本轮跳过（附原因） |
| 完成 | info | 已完成（附用时） |
| 中断 | critical | 清扫中断（离线或报错） |
| 超时 | warning | 清扫超时（8 小时） |
| 异常结束 | warning | 清扫异常结束（任务结束 120 秒后仍没回基站） |
| 未执行 | warning | 定时清扫未执行（整轮从未启动过清扫） |

「已完成」与「异常结束」的判据是**任务结束后再等最多 120 秒**看机器人有没有回基站，
不是在任务结束的那一刻采一次状态：实测 `task_active` 会比 `vacuum` 早 4～11 秒变 off，
采在那个窗口里会把正常结束报成异常。「未执行」看的是本轮有没有真的启动过清扫
（`clean_started`），不是「此刻任务在不在跑」——后者在每次正常扫完之后也成立。

#### 示例

```yaml
- id: '1794200000001'
  alias: 主卧打扫任务
  use_blueprint:
    path: kaattz/扫地机分区定时清扫.yaml
    input:
      vacuum_entity: vacuum.your_robot
      areas: [master_bedroom]
      cleaning_mode: 仅扫地
      presence_sensors: []
      door_sensors: [binary_sensor.master_bedroom_door]
      door_open_state: 'on'
      my_trigger:
      - trigger: time
        at: '10:00:00'
        weekday: [tue, thu, sat]
```

#### 通知脚本接口

蓝图调用时传三个参数：

```yaml
data:
  level: info | warning | critical
  title: 标题
  message: 正文
```

自己写一个接收这三个参数的脚本即可：

```yaml
alert_notify:
  alias: alert_notify
  mode: parallel
  fields:
    title: {required: true, selector: {text: {}}}
    message: {required: true, selector: {text: {}}}
    level:
      default: info
      selector:
        select: {options: [info, warning, critical]}
  sequence:
  - action: persistent_notification.create
    data:
      title: '{{ title }}'
      message: '{{ message }}'
  - action: notify.mobile_app_your_phone
    data:
      title: '{{ title }}'
      message: '{{ message }}'
```

### 多阶段清扫

`blueprints/多阶段清扫.yaml`

把一个清扫拆成多段（例如先扫后拖），每段经过同一套门互锁，确认本段成功后才启动下一段。人工中断、失败或超时都会停止后续。

| 参数 | 说明 |
| --- | --- |
| 扫地机 | |
| 分段传感器 | 区分阶段的实体 |
| 清扫阶段列表 | 每段的目标与模式 |
| 阶段超时 | 每段最多等多久 |

### 这套东西怎么配合

| 谁 | 管什么 |
| --- | --- |
| 集成 | 门和机器人的时序、互锁、每一步的确认。不管什么时候该扫 |
| 蓝图 | 什么时候扫、扫哪个房间、有人要不要跳过、失败怎么重试、通知发给谁 |
| 自动化 | 家庭策略：周几几点、什么事件触发、谁收到消息 |

集成负责不出事故，蓝图负责按你的意愿做事。在自动化里自己写开门关门就绕过了互锁。

---

## 故障排查

先看两个：

```yaml
sensor.<robot>_garage_control_phase        # 当前阶段
binary_sensor.<robot>_garage_control_problem   # 有无故障，reason 属性说明原因
```

### 常见 reason

| reason | 含义 | 处理 |
| --- | --- | --- |
| `duplicate_binding` | 门被另一个已失效的集成实例占着 | 重启 Home Assistant |
| `pause_not_observed` | 暂停命令发了但状态没确认 | 检查网络或采样；机器人可能停着，需人工恢复 |
| `open_not_confirmed` | 门没在超时内确认全开 | 检查门磁极性、行程时间、门是否卡住 |
| `close_not_confirmed` | 门没确认全闭 | 同上 |
| `controlled_exit_failed` | 受控出库没在超时内完成 | 检查出库点是否可达 |
| `estimated_risk_not_accepted` | 用了估算位置但没勾风险确认 | 回配置勾上，或改用 `contact` |
| `handover_required` | 从旧自动化接管但没确认交接 | 回配置勾上 |

### 点了没反应

按顺序查：

1. `binary_sensor.<robot>_task_active` 是否为 `on`——已经有任务在跑，`mode: single` 会丢弃新请求
2. `sensor.<robot>_garage_control_phase` 是否为 `blocked`——看 `reason`
3. 自动化的 `current` 属性是否大于 0——蓝图可能在重试循环里（有人、门没开、机器人忙）
4. 有没有收到「本轮跳过」通知——蓝图会写明原因

### 门开了但机器人不走

- 检查 `binary_sensor.<robot>_garage_fully_open_confirmed` 是否为 `on`
- 状态是 `unknown` 说明门磁读不到：未接线、极性填反，或实体不可用

---

## 已知限制

- 只支持 V1 协议。Q7、Q10、A01 等新协议型号不在范围
- 一扇门只能绑一台机器人；多台共用一扇门不支持，但可以分别绑不同的门
- 危险区是矩形，不支持多边形
- 地图坐标来自服务器，不是本地计算；自建服务器时更可靠
- 受控出库依赖 `app_goto_target`，机器人必须在基站上
- 蓝图在重试窗口内条件满足就自动开始；条件始终不满足时只发一条失败通知

---

## 致谢

基于 Home Assistant 官方 `roborock` 集成演化。地图解析使用 `vacuum-map-parser-roborock`，设备通信使用 `python-roborock`。
