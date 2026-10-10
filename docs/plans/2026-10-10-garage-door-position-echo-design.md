# 车库门 position 回声 —— 设计与止血

日期：2026-10-10
状态：已实施（代码与仓库内自动化就绪；HA 侧需人工粘贴，物理验证待用户执行）
相关：[2026-10-08 柜门状态机设计](2026-10-08-garage-door-state-machine-design.md)
实施计划：[2026-10-10 修复计划](2026-10-10-garage-door-position-echo-fix-plan.md)

## 症状

用户报告「车库门的 position 好像不对」，并补充四条独立观察：

| 观察 | 来源 |
| --- | --- |
| 米家 App 点「开」「关」按钮，开合度立刻跳 100% / 0% | 用户在米家 App 亲见 |
| 米家 App 拖 position 滑块，门**会开到**指定位置 | 用户亲见 |
| 手动操作（手自一体）时 position 是准的 | 用户亲见 |
| 自动化依赖「门完全打开」，而这个状态不准 | 用户报告的实际影响 |

历史上 recorder 里出现过一批「幽灵值」：`4 / 11 / 13 / 18 / 22 / 31 / 38 / 40 / 50 / 52 / 63 / 72 / 77 / 79 / 86 / 95`。
它们长期被当成设备故障或读数噪声。

## 根因

**不是设备坏了，也不是集成 bug，而是「用会被回声污染的信号判断门是否到位」。**

三个环节，缺一不可：

1. **设备回声。** 下发任意电机命令后，设备立刻把 `current-position` 写成**目标值**，而门物理上还在原地。实测：
   - `open_cover` → 0.5 秒内报 100
   - `set_cover_position(40)` → 约 1 秒内报 40
   - `close_cover` → 0.8 秒内报 0
   - 而**真实行程 35 秒**（用户秒表计时）
2. **触发器被回声骗了。** `automation.vacuum_garage_door_auto_unlock` 的条件是 `current_position below 1 for 1s`。回声让它以为门已经关好。
3. **5 秒后发 PAUSE。** 此时门才走了约 1/5 行程，被 `cover.stop_cover` 硬生生按停。

**结果是门每次自动关闭都被自己的自动化停在半路。** 那些「幽灵值」全是**真实位置**——门确实停在那里。

### A/B 对照（决定性证据）

唯一变量是 `auto_unlock` 的启用状态：

| 轮次 | 时刻 | `auto_unlock` | 关门命令 | 回声 | 最终读数 |
| --- | --- | --- | --- | --- | --- |
| A | 14:58 | **OFF** | 14:58:37 | closed/0 | `closed` / **0**，稳定 9 分钟 |
| B | 15:09 | **ON** | 15:09:22.464 | 15:09:23.233 closed/0 | `open` / **77** |

B 轮完整因果链（时间戳逐条吻合）：

```
15:09:22.464  close_cover 下发
15:09:23.233  HA 报 closed / 0          ← 回声，门物理上还在 100% 处
15:09:24.240  auto_unlock 触发           ← 被回声骗了
15:09:29.24   cover.stop_cover 发出      ← 门只走了约 7 秒
15:09:32.741  设备上报真实位置 77         ← 被按停在 77%
```

第二轮独立复现（14:49，同样 `auto_unlock` ON）：

```
14:49:43.900  closed / 0                 ← 回声
14:49:44.903  auto_unlock 触发（trace）
14:49:50.038  stop_cover 执行完（trace）
14:49:53.412  open / 38                  ← 真实位置
```

### 协议层事实：设备两个属性都有

MIoT 官方 spec（`urn:miot-spec-v2:device:window-opener:0000A043:zhenji-zj1q:1`）：

```
service siid=2  Window Opener
    prop piid=6 : target-position    ← 目标位置
    prop piid=7 : current-position   ← 当前位置（独立存在）
```

集成读的是 `current-position`（`cover.py:277`），那条「没有 current-position 就假定等于 target」的回退分支（`cover.py:268-276`）**未被触发**。

**所以是固件在收到命令时把 target 的值写进了 current-position。** 这一点 HA 侧改不了，只能绕开它的污染窗口（见下方「什么时候读数才是真的」）。

**但请注意措辞**：回声本身是个并存的次要现象，**不是**用户所遇「position 不准」的根因。真正的根因是 `auto_unlock` 在回声上触发、把行进中的门按停（见「根因」一节）。早期排查把它当成根因是错的，第 238 行有记录。

### 关键推论：state 也被污染

`cover.py:308-311`：

```python
@property
def is_closed(self) -> Optional[bool]:
    if self.current_cover_position is not None:
        return self.current_cover_position == 0
```

而 `_position_changed_handler`（`cover.py:214-218`）在 `current_position` 变化时清掉 `_prop_pos_opening` / `_prop_pos_closing`。

所以回声一到：方向标志被清 → `is_closed` 为真 → **`state` 立刻变 `closed`**，门却还在全开位置。

**「等 `state` 变成 `closed`」不是解药，它读的是同一个被污染的值。**

### 什么时候读数才是真的

设备在**电机停止后**才报真实位置（正常走完停在目标值，中途被停则报中途位置）。

- 正常走完 → 电机停在目标 → 报目标值 = 真值 ✓
- 中途被 PAUSE → 电机停在中途 → 报中途位置 ✓（这正是观测到 77/38 的时机）

**行程进行中没有任何可观测的「还在动」信号**：position 卡在回声值不变，`state` 已经变成 `closed`。所以只能靠**时间**越过污染窗口。

## 影响面

依赖 `cover.vacuum_garage_door` 的 6 条自动化 + 1 处集成代码：

| 位置 | 用的信号 | 污染后果 | 状态 |
| --- | --- | --- | --- |
| `automation.vacuum_garage_door_auto_unlock` | position < 1 for 1s | **门被按停在半路**（本次根因） | ✅ 已修 |
| `automation.roborock_garage_door_statemachine` 阶段 B/D | `wait_template` position < 5 | 瞬间放行，可能夹机 | ✅ 已修 |
| 同上 阶段 C | `wait_template` position ≥ 95 | 瞬间放行，防撞保护形同虚设 | ✅ 已修 |
| `custom_components/roborock_plus/garage_guard.py` | `position ≥ 95` | 开门后立刻放行扫地机，门可能只开了一小半 | ✅ 已修 |
| `automation.garage_door_sync_folding_door` | 无（只发 open） | 无 | — 无需改 |
| `automation.che_ku_men_chang_shi_jian_wu_ren_zi_dong_guan_bi` | `state: open` | 状态被污染 → 可能**漏关**（失败方向安全） | ⚠️ 未修，风险低 |
| `automation.che_ku_men_dan_jian_xun_huan_ble_an_jian` | `state: opening/closing` + position | **按键反向**（失败方向危险） | ⚠️ **未修，见下** |

**最危险的是 `garage_guard.py`**：它开门后等 `position >= 95` 才下发清扫命令，而回声让它瞬间通过——扫地机会在门只开了一小半时就出发。

### ⚠️ 尚未修复：BLE 按键单键循环

`automation.che_ku_men_dan_jian_xun_huan_ble_an_jian`（当前 `off`，处于潜伏状态）的分支顺序是：

```
门在动（opening/closing） → stop
position ≤ 1（已关）      → open
position ≥ 2（未关到底）  → close
```

**两个前提都被回声破坏了：**

1. 这台设备在 MIoT spec 里**没有 status 属性**，所以 `opening`/`closing` 来自集成自己下的「乐观标志」，而
   `_position_changed_handler` 在 `current_position` 变化时**会把它清掉** —— 而回声在 ~0.5 秒内就会改变它。
   实测 7 天历史：`opening`/`closing` 平均只持续 **0.79 秒**（最大 1.71 秒，n=20），其余 ~35 秒行程里实体报的是**回声后的终态**。
2. 于是「停止」分支**实际上永远不可达**，按键会落到「反向」分支：

| 按键时机 | 实体上报 | 自动化实际执行 | 用户意图 |
| --- | --- | --- | --- |
| 关门途中 | `closed` / 0 | `open_cover` | 停住 |
| 开门途中 | `open` / 100 | `close_cover` | 停住 |

**失败方向是危险的**：开门途中被按键会下发 `close_cover`，若此时扫地机在门口，这是唯一可能撞到它的方向。目前该自动化是 `off`，所以是潜伏的而非活跃的。

**为什么本设计不顺手改**：修它需要「门还在动」的可靠判据，而这台设备给不出。可选路径各有代价：

- 加一个「上次命令时间」辅助实体 → 与「不留第二个更滞后的真相来源」的既有决定冲突（`scripts/garage_button_cycle/README.md` 记录过退休 `input_datetime` 的理由）
- 改用物理限位开关 → 与门磁方案同一件事，等硬件
- 接受「按键只做开/关切换，不支持中途停止」→ 语义降级，需要用户确认

**在用户决定之前，保持它 `off`**，并已记入遗留风险表。

## 设计

### 常量

| 常量 | 值 | 依据 |
| --- | --- | --- |
| `DOOR_TRAVEL_SECONDS` | **35** | 用户秒表计时（单次，见「遗留风险」） |
| `ECHO_SETTLE_SECONDS` | **45** | 35 + 10 秒余量 |

所有等待都基于「命令后至少经过 `ECHO_SETTLE_SECONDS`，读数才可信」。

### 1. `auto_unlock`：保留意图，修正时机

用户确认这条自动化的目的是**关到底后释放离合，让人能手动开门**。意图合理，实现错了。

```yaml
# 现在：门还在走就发 PAUSE
trigger: position < 1 for 1s
  → delay 5s
  → stop_cover                          # ← 把门按停在半路

# 改为：
trigger: position < 1 for 1s
  → delay 45s                            # 越过污染窗口，等行程真正走完
  → 校验：position < 5 且两次采样一致
     是 → stop_cover                    # PAUSE 打在静止的门上 = 松劲
     否 → 不动作 + 通知                  # 门没关好，不乱发命令
```

**关键点**：PAUSE 打在已经静止的门上才是「释放离合」；打在行进中的门上是「按停」。同一个命令，时机不同，效果相反。

### 2. 状态机四个 `wait_template`

```yaml
# 现在：回声瞬间放行
- action: cover.open_cover
- wait_template: "{{ pos >= 95 }}"       # 0.5 秒就通过
  timeout: "00:01:00"

# 改为：
- action: cover.open_cover
- delay: "00:00:45"                       # 先越过污染窗口
- wait_template: "{{ pos >= 95 }}"        # 此时才是真值
  timeout: "00:01:30"
```

超时值相应放宽：原 60 秒在「先等 45 秒」之后只剩 15 秒余量，改为 **90 秒**（45 秒 settle + 45 秒观察）。

超时行为不变：**只告警，不强行动作**（门与扫地机状态不一致时没有安全的自动恢复方式）。

### 3. `garage_guard.py`

`DEFAULT_GARAGE_DOOR_OPEN_POSITION = 95` 的判据本身没错，错在**读得太早**。

新增最小行程约束：`open_cover` 之后，**同时**满足

- 已过去 ≥ `DEFAULT_GARAGE_DOOR_MIN_TRAVEL`（45 秒）
- `current_position ≥ 95`

才算门开好了。轮询间隔保持 0.5 秒，总超时从 60 秒提到 90 秒。

### 4. 长期方案：物理限位传感器（用户已选）

时间方案依赖「35 秒」这个常量，而它无法覆盖**门卡住但电机仍在转**的情况：此时 position 停在回声值（目标值），我们会误判「已到位」。

彻底的解法是物理触点，用户已选定**米家门磁**（与现有小方 `isa.magnet.dw2hl` 同款，走 `xiaomi_home`）：

- 全开位一个触点、全关位一个触点
- 关键词义：**`unavailable` 必须当作「不确定 → 禁止移动」**

最后一条不是理论担忧——现有小方传感器历史里会周期性掉线：

```
23:54:11 unavailable → 23:54:14 on → 23:54:34 off
18:07:03 unavailable → 18:07:06 on → 18:07:24 off
```

BLE 掉线若被当成「门没开」，反而制造危险。

**本设计先落地时间方案止血；门磁到位后，把「position ≥ 95」替换为「触点闭合」，时间常量降级为兜底。**

## 不做什么

- **不删 `auto_unlock`。** 它的意图（释放离合）合理，只是时机错了。
- **不用 `state: closed` 判断到位。** 实测它同样被回声污染（见上）。
- **不指望固件改掉回声。** 米家 App 显示同样的行为，是设备侧设计。回声只能靠时间绕开，且这只是次要现象 —— 真正要修的是消费它的自动化（本设计第 1 节）。
- **不缩短行程等待来「优化体验」。** 少等几秒换来的是一扇可能只开了一半的门。

## 验证方法

按 `AGENTS.md`，实际动作由用户执行，助手负责仿真与回放。

1. **时序回放** —— **未做，且 `replay_docked_flapping.py` 不覆盖本 bug。**
   该脚本回放的是 2026-10-08 的「停靠中抖动」时序（01:26 装拖布、19:41 抖动），
   **没有**回声模型、没有 35 秒行程、没有 `stop_cover`，也不涉及 `auto_unlock`。
   它全 PASS 是真实的，但那是上一个 bug 的回归网。本次修复的回归网是：
   - `tests/test_door_timing.py`（常量与上下界）
   - `tests/test_garage_guard.py`（虚拟时钟驱动真实协程：回声不能放行扫地机、已开的门不空等、卡住的门超时）
   - `tests/test_auto_unlock_waits_for_travel.py`（45 秒延时、**顺序**在 stop 之前、复查守卫）
   - `tests/test_garage_state_machine.py`（三处门等待都有 settle、预算 90 秒、递归扫到嵌套那处）
   - `tests/test_system_composition.py`（粘贴用 YAML 与生成物的 actions/triggers/mode 一致）

   给状态机补一个真正带回声模型与 35 秒行程的回放，仍是**未完成事项**。
2. **变异测试**：✅ 已做。`mutation_check_garage_state_machine.py` 12/12、
   `mutation_check_garage_guard.py` 6/6（含「把 elapsed 换成常量」这个原本会逃逸的变异）。
3. **用户实测**（预期结果写清楚）：
   - `auto_unlock` 开启 + 关到底 → 门应停在 **0**，不再出现中途值
   - 门在行进中被手动停 → 应出现真实中途值，且 `auto_unlock` **不**再补一脚
4. **秒表复核**：请用户再计 2～3 次行程，取最大值更新 `DOOR_TRAVEL_SECONDS`。

## 已落地的改动（2026-10-10）

| 改动 | 位置 |
| --- | --- |
| 行程/回声常量单一来源 | `custom_components/roborock_plus/door_timing.py` |
| guard 加行程闸门（前置检查不等、命令后等待等） | `custom_components/roborock_plus/garage_guard.py` |
| 状态机三处门等待各加 45 秒 settle，超时放宽到 90 秒 | `scripts/garage_state_machine/build.py` |
| `auto_unlock` 等过回声窗口 + 复查后才发 PAUSE | `automations/vacuum_garage_door_auto_unlock.yaml` |
| 防漂移：粘贴用 YAML 必须与生成物 actions 一致 | `tests/test_system_composition.py` |
| 教训 | `lessons.md` |

**尚未完成（依赖用户）**：HA 里的自动化是人工粘贴的，所以线上仍是旧的。见实施计划末尾的「部署」与「物理验证」两节。

## 遗留风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 35 秒是**单次**秒表测量 | 可能偏小；冷天/阻力大时行程更长 | 留 10 秒余量；门磁到位后不再依赖它 |
| 门卡住但电机仍转 | position 停在回声值 → 误判到位 | **只有物理触点能解**（本设计的已知边界） |
| 时间方案的固有代价 | 每次开关门多等 45 秒 | 安全优先，用户已确认取此方向 |
| `stop_cover` 是否真能「释放离合」 | 未经验证：PAUSE 打在静止电机上是否有效果未知 | 修复后请用户手动试一次门能否轻松推开 |

## 过程记录

本次排查中，助手在用户明确授权下实操了车库门（用户原话：「你自己开启和关闭车库门，看下 position」），这与 `AGENTS.md`「绝对不要主动触发实体设备」的默认约定相反——**授权优先，但需要记录**。

两次把门留在不确定状态（15:09 停在 77%、14:49 停在 38%），且**无法从 HA 判断门是否物理关闭**——这正是本问题的本质。后续实操应在用户能目视确认的前提下进行。

### 一次被推翻的中间结论

排查中期曾根据「命令回声」判定**根因在设备固件、HA 侧无法修复**。这个结论是错的：回声真实存在，但不是用户所遇问题的根因。**只有做了 A/B 对照（关掉 `auto_unlock` 后门即正常关闭）才定位到真正的因果。** 教训：并存现象不等于根因，隔离变量才能定因果。
