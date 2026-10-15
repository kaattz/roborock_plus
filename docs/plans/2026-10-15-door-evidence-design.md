# 柜门判据通用化 — 设计

日期：2026-10-15
状态：设计已确认，待写 spec/plan
阶段：第三阶段（第一阶段柜门状态机、第二阶段定时蓝图已完成）

## 需求

用户问题（原话）：

> 柜门的门磁现在是写死吗，如果有个柜门本身自带 current_position，或者有两个门磁（全开和全关），要如何处理

用户确认的方向（原话）：

> 做成通用的，支持各种门，但是关键问题是门全开和门全闭怎么判断，如果无法判断门全开和门全闭，就要兜底，用 45 秒/95，但是 45 秒和 95% 应该做成可配置的

范围：**通用场景**（不只是本屋这扇门）。本仓库是公开发布的 HACS 集成
（`v0.6.11`，`kaattz/roborock_plus`，`quality_scale: silver`），所以"别人家的门"
是真实需求面。

## 现状（代码证据）

### 门磁根本不在集成里

`custom_components/roborock_plus/` 下 grep `contact` **零命中**。门磁只作为字面量
活在两个自动化里，源头是 `scripts/garage_state_machine/build.py:78` 的 `CONTACT`。

集成只认一个 cover 实体 + 一个位置阈值：

| 位置 | 写死了什么 |
| --- | --- |
| `garage_guard.py:15` | 阈值 `95` |
| `garage_guard.py:221` | 属性名 `"current_position"` |
| `garage_guard.py:21` | 回声窗口 `45` |
| `config_flow.py:482` | `EntitySelectorConfig(domain="cover")` —— **门磁配不进去** |
| `build.py:78` | 全关门磁 entity_id（自动化侧字面量） |

### 命令目标与证据来源被当成同一个实体

`CONF_GARAGE_DOOR_ENTITY_ID` 既是要下发 `open_cover` 的对象，也是读
`current_position` 的对象（`garage_guard.py:166-194` 里同一个 `cover_entity_id`
先被命令、再被观测）。这是"门自带 position"会变成问题的根因：一旦想用**另一个
实体**（门磁）当证据，现有 schema 表达不了。

### 信号分工是刻意不对称的

| 问题 | 谁回答 | 信号 | 靠什么建立信任 |
| --- | --- | --- | --- |
| 门开够了吗 | 集成 `garage_guard` | `current_position >= 95` | **时间**（≥45s） |
| 回基站前门开了吗 | 状态机阶段 C | 同上 | **时间**（45s settle） |
| 门关好了吗 | 状态机 / auto_unlock | 门磁 `off` | **物理触点**，不需要时间 |

### position 双向都会错（实测）

| 事件 | 时刻 | 错的方向 |
| --- | --- | --- |
| `cover → open` | 14:15:16.976 | **早 0.7 秒**（回声，门还在关着） |
| 全关触点 `→ off` | 14:21:15.373 | 物理到达 |
| `cover → closed` | 14:21:20.736 | **晚 5.4 秒**（电机停了才报真值） |

所以 position 不是"差一点的证据"，是**两个方向都会错**。这是"触点必须压过
position、而不是两者都要满足"的依据。

要求两者一致会把正常的开关门判成失败 —— `vacuum_garage_door_auto_unlock.yaml:41-45`
已经为这个理由删掉过 position 复查。

### 按"有没有 position"实测覆盖（本屋 19 个 cover）

| 类型 | `current_position` | `supported_features` | 数量 |
| --- | --- | --- | --- |
| 有位置 | ✅ 0–100 | 15 | 15 |
| 无位置 | ❌ 无该属性 | 11 | 3（电动窗开窗器） |
| 无位置 | ❌ 无该属性 | 0 | 1（电机控制器） |

`supported_features` 15 = `OPEN|CLOSE|SET_POSITION|STOP`，11 = `OPEN|CLOSE|STOP`，
0 = 都没有。**按位检测 `SET_POSITION` 就能在配置时判定"这个 cover 能不能报位置"。**

`is_closed` 不能当替代品：那 3 个开窗器 `is_closed: null`；而这扇柜门上
`is_closed` 就是 `current_position == 0`（`door_timing.py:5-8`），`lessons.md:66`
已记录"等 state 变成 `closed` 并不能绕开回声 —— 它读的是同一个值"。

### 当前"无 position"配置的后果

顺 `vacuum.py:385-397` 走一遍：

1. `is_garage_guard_ready` 通过（只校验 enabled + 实体非空，`garage_guard.py:72-77`）
2. 前置检查 `trusts(now, None, ...)` → `is_garage_door_open_enough(None)` 恒 `False`
3. **`cover.open_cover` 真的发出去了** —— 门被打开
4. 循环里位置永远 `None` → 90 秒后 `TimeoutError` → 抛 `HomeAssistantError`
5. 异常向上传播，`await self.send(command)` **不执行** —— 扫地机不动

净效果：**门开了、活没干、扫地机没动、门一直开着。** `_watch_clean_command` 在
`send()` 之后才调用，也永不执行，所以连"命令下发但没启动"事件都不发。

失败方向安全，但物理后果不安全 —— 柜门是共用的（兼作洗衣机柜门，`README.md:168`）。

### 常量散在四处

`45` 存在于 `garage_guard.py:21`、`door_timing.py:40`、`build.py:93`、
`automations/roborock_garage_door_statemachine.yaml:152`；`95` 存在于 guard、
`build.py:228`、YAML 三处；`90` 两处。`tests/test_garage_guard.py:61` 写了一条
测试盯着前两者相等 —— 有测试盯着重复，说明重复是已知的。

**根因是"判断"被放在集成和用户自动化两处各写一遍。**

## 已确认的决策

| 项 | 决策 |
| --- | --- |
| 目标范围 | 通用（支持各种门），不只本屋 |
| 命令目标 | 仍必须是 `cover`（暂不做 script/switch 动作抽象） |
| 优先方向 | 先解决"证据"，动作抽象以后再说 |
| 判据来源 | 三级阶梯：触点 → position → 时间兜底 |
| 常量 | 45s / 95% 改为可配置，默认值逐位兼容今天的行为 |
| 证据逻辑落点 | 见"未决问题" |

### 为什么不做动作抽象（script/switch）

`tests/test_options_ui.py:43` 有一条负向断言：

```python
assert "CONF_GARAGE_OPEN_SCRIPT_ENTITY_ID" not in config_flow
```

对照 `docs/plans/2026-04-27-garage-guard-design.md:7`：早期设计是"调用配置的
script 来开门"，后来改成直接 `cover.open_cover`。

**这个项目已经试过"用 script 抽象开门动作"并把它删掉了。** 将来若要做，需要先
解释为什么当年删掉。

## 设计

### 核心：三级证据阶梯（每个方向各自判定）

"全开"和"全闭"各跑一次同样的判定，互不依赖：

```
第 1 级  物理触点    配了且读得到 → 触点说了算，不需要越过回声窗口
第 2 级  设备位置    读得到      → position 判据 + 必须越过回声窗口
第 3 级  时间兜底    都读不到    → 盲等，且必须告警（这是在猜）
```

**为什么触点压过 position，而不是"两个都满足"**：见上文实测，position 双向都会
错。要求两者一致会把正常的开关门判成失败。

**为什么第 2 级必须越过回声窗口**：设备（zhenji.wopener.zj1q）对任何电机命令都
在 0.5–0.8 秒内把**目标位置**回写进 `current_position`，而门还没动
（`door_timing.py:1-18`）。开门侧的回声已修；**关门侧同样需要** —— `close_cover`
一发，设备立刻回声 0，"门已关好"会骗到后续动作。实测两次把门停在 77 和 38
（`vacuum_garage_door_auto_unlock.yaml:6-12`）。

### 判定表

| 配置 | 判定"全开" | 判定"全闭" |
| --- | --- | --- |
| 两个触点都有 | 全开触点 = 期望值，持续 `contact_settle` | 全关触点 = 期望值，持续 `contact_settle` |
| 只有全开触点 | 全开触点 = 期望值 | position ≤ `close_position` 且 ≥ `min_travel`，或时间兜底 |
| 只有全关触点 | position ≥ `open_position` 且 ≥ `min_travel` | 全关触点 = 期望值 |
| 只有触点、无 position | 触点 | 触点 |
| 只有 position | position ≥ `open_position` 且 ≥ `min_travel` | position ≤ `close_position` 且 ≥ `min_travel` |
| 都没有 | 时间兜底 + 告警 | 时间兜底 + 告警 |

**两个触点都配时多一条规则**：两端**同时**声称到位 = 物理上不可能 → 判 `unknown`，
**不二选一**，告警。这是触点自身卡死（粘在"到位"）的唯一解 —— 单个触点做不到
交叉验证。

集成已有 `None` 表示未知的先例（`binary_sensor.py:366-368`）。

### 触点极性：必须显式问，不能猜

实测语义（`build.py:65-77`）：磁铁**吸合 → `off`**，分离 → `on`。

- 装在**关闭位**的触点：门关到底 → `off`
- 装在**全开位**的触点：门全开时磁铁吸合 → **同样是 `off`**

所以两个触点"各自到位时都报 `off`"。**绝对不能**把配置项设计成"门开时 on" ——
那会让用户把"还关着"配成"已全开"，直接放行扫地机。这类 bug 不会自己暴露，只会撞门。

**正确问法**：**"门到了这个位置时，这个传感器报什么？`on` / `off`"**

配 `on`/`off` 两个字面量；`unavailable`/`unknown` 不是可选项，不能填。

### 配置项

按仓库既有风格（`v1_stuck_detection.py:149-169` 的 `resolve_stuck_options`：
**坏值 clamp / 回默认，不抛异常**，理由是"a typo cannot silently disable the
detector"；`_as_int` 显式把 `bool` 当垃圾）。

| 配置键 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `garage_door_min_travel` | **45** | 5–600 | 回声窗口；即"电机该停了" |
| `garage_door_open_position` | **95** | 1–100 | 全开阈值 |
| `garage_door_close_position` | **5** | 0–99 | 全闭阈值；仓库先例见下 |
| `garage_door_open_contact` | 空 | — | `binary_sensor`；留空 = 用位置 |
| `garage_door_open_contact_state` | `off` | `on`/`off` | **极性** |
| `garage_door_close_contact` | 空 | — | 同上 |
| `garage_door_close_contact_state` | `off` | `on`/`off` | **极性** |
| `garage_door_contact_settle` | **8** | 0–60 | 磁铁提前吸合 + 抖动 |

**默认值逐位兼容今天的行为**：不配任何触点 + 四项默认值 = 现在的 `≥95 且 ≥45s`。
这是这个方案风险低的主要原因。

`close_position` 默认 5 有仓库先例：`vacuum_garage_door_auto_unlock.yaml:59` 记录
"以前用 `position < 5`（允许 5% 未关）"，`docs/plans/2026-10-10-garage-door-position-echo-design.md:115`
也记过状态机阶段 B/D 曾用 `position < 5`。

`contact_settle` 默认 8 取自 `build.py:86` 的 `CONTACT_SETTLE = {"seconds": 8}`
（在全关位实测：磁铁吸合后还要等，因为两个调用方都期望"门已静止"）。

### 未测数字（不可凭印象填）

**全开位触点的磁铁提前量没人测过。** `contact_settle` 的 8 秒只在全关位实测过。

这仓库为"凭印象估时长"付过代价：`docs/plans/2026-10-10-garage-door-position-echo-fix-plan.md:898`
记录按用户"十几秒"的说法设计 `delay: 25`，实测 **35 秒**，门走到 71% 就被发 PAUSE，
bug 原样复现。

所以：**默认沿用全关位的实测值，UI 文案必须写明"全开位需自行实测"**，
不暗示某个默认值在全开位是对的。

### 护栏

**clamp 不够。** `open_position` 填 40 是范围合法但危险的 —— 等于告诉集成
"门开一半就算全开"。这类"合法但把安全边界关掉"的值应该**接受 + 明确告警**
（repair 或通知），而不是静默生效。

同样，第 3 级时间兜底**必须告警**：它是"猜"不是"观测"。现在那条静默路径
（门开了、机器不动、门一直开着、只留一个日志错）证明**安全失败 ≠ 用户知情**。

### 配置校验（新增，堵住现状的坑）

`is_garage_guard_ready` 现在只校验实体非空（`garage_guard.py:72-77`），
应在保存时校验：

- cover 的 `supported_features` 含 `SET_POSITION`；不含且**未配任何触点** → 拒绝保存
  并说明"这个 cover 不报位置，请配一个触点或换成会报位置的 cover"
- 两个触点配成同一个实体 → 拒绝（等价于"两端恒相等"，会让两个方向都失效）
- 极性值非 `on`/`off` → 回默认，不抛错

### selector 细节

**别按 `device_class="door"` 过滤。** 触点常见的 device_class 有
`door` / `window` / `garage_door` / `opening`；本屋这扇门的 cover 自己报的就是
`window`，而门磁报 `door`。只放 `door` 会把合法的 `window`/`opening` 触点挡在外面。

建议 `EntitySelectorConfig(domain="binary_sensor")` 不加 device_class 过滤。

参考先例：`docs/plans/2026-10-09-cleaning-schedule-blueprint-design.md:223-241`
记录过选择器过滤不干净的教训，最终结论是"实际用法：用搜索框"。

### 无需迁移

options 是自由 dict，新键用 `.get(默认)` 读即可。
`VERSION = 1 / MINOR_VERSION = 2`（`config_flow.py:109`）管的是 config entry 的
`data`，与 options 无关。**加可选配置项不需要迁移** —— 这是本方案风险低的主因。

### 翻译成本是硬约束

`tests/test_options_ui.py:57-66` 已立规矩：**安全相关的 option 必须在每个
translation 文件里都有 `data` 和 `data_description`**，且用
`TRANSLATIONS_DIR.glob("*.json")` 遍历 —— 加文件就自动纳入。

现有三份：`strings.json`、`translations/en.json`、`translations/zh-Hans.json`。

**后果：配置项数量是有成本的，每个约 3 处文案 + 测试。** 这是不选"可重复证据列表"
（S2）的第二个理由 —— 列表项的文案没法静态写全。

### 推荐配置的形状

| | 形状 | 覆盖 | 代价 |
| --- | --- | --- | --- |
| **S1（选这个）** | 两个可选槽位 + 极性 | 1~2 个门磁 + 可选 position 的全部组合 | 旋转门/多段门表达不了 |
| S2 | 可重复证据列表 | 任意组合 | options flow 列表嵌套难做难校验；翻译成本高 |
| S3 | 只认一个 `binary_sensor`，语义靠模板 | 理论上任意 | 集成放弃校验，退回"配错了只报错"的坑 |

选 S1：覆盖绝大多数真实硬件，HA selector 原生支持，**每一项都能校验**，
失败方向可枚举。

### 语义前提：问的是"全开位"

配置项的语义是**"门全开位"**，不是"够用即可的位置"。

两者不同：触点若装在半路（"开一半就够扫地机过"），它**不蕴含"门停了"**，
此时 hold 时间仍然必要；而装在极限位则蕴含门已停。

**只支持"全开位"语义**，并在文案里写死这个前提 —— 否则集成无法判断该不该省时间，
只能永远保守等待，白拿不到延迟收益。

## 失败方向（必须可枚举）

沿用已踩出的规矩（`vacuum_garage_door_auto_unlock.yaml:47-54`：门磁 `unavailable`
时判"不确定"、只告警不动作）：

| 情况 | 判定 | 动作 |
| --- | --- | --- |
| 触点 = 期望值，且持续够久 | 到位 | 放行 |
| 触点 ≠ 期望值 | 未到位 | 等，超时告警 |
| 触点 `unavailable`/`unknown` | 不知道 | **不放行 + 告警** |
| 两端同时声称到位 | 矛盾 | **判 unknown，不二选一 + 告警** |
| 未配触点 | 退回位置判据 | 今天的行为 |

## 零代码捷径（写进文档，先不改代码）

两个触点的用户**今天**就能达到目的：做一个 Template Cover，
`position_template` 由触点合成（全开触点 → 100，全关触点 → 0），
`open_cover`/`close_cover` 转发真实电机，配给现有 `garage_door_entity_id`。

- ✅ 立刻消除回声：模板读的是物理触点，不是设备自报值
- ✅ 卡住的门也判得出（到不了全开触点 → 到不了 100）
- ❌ **拿不到延迟收益**：guard 无从知道这个信号是物理的，仍白等 45 秒
- ❌ 每户自己写 jinja，集成看不见、无法校验

这正好量化了集成层该补什么。

## 仍然解不掉的边界

- **门口被别的东西挡住**：触点只证明"门扇到了全开位"，不证明**通路**通畅
  （箱子、人）。这是另一个传感器的事。
- **时间兜底测不出"门卡住"**：门被挡住时看不到任何反馈，时间一到就放行扫地机。
  这正是 `docs/plans/2026-10-10-garage-door-position-echo-design.md:284` 记的已知
  边界。**配了全开触点才能关掉这条。**
- **单个触点卡死**：粘在"到位"位置会永久放行。只有第二个触点能交叉验证。

## 已确认可关闭的边界

`docs/plans/2026-10-10-garage-door-position-echo-design.md:318-320` 记的
"门卡住但电机还在转，position 停在回声值 → 误判到位，**只有物理触点能解**，
而全开位装不了门磁" —— **通用化后这条对配了全开触点的用户自动关闭**：
卡住的门永远到不了全开触点。

对仍只有 position 的用户，这条边界保留。

## 验证方式

不靠"我觉得对"，靠**把 bug 放回去、测试必须变红**。
仓库已有 9 个这类脚本（`scripts/mutation_check_*.py`），其中 4 个与柜门相关：
`mutation_check_garage_guard.py`、`mutation_check_garage_state_machine.py`、
`mutation_check_auto_unlock.py`、`mutation_check_button_cycle.py`。

新的证据层应加一个 `mutation_check_door_evidence.py`，至少覆盖下面四条。

新的证据层至少要能抓住这几个变异：

1. 交换极性 → 红
2. 把 `unavailable` 当"全开" → 红
3. 端点矛盾时取其中一个 → 红
4. 只配一个触点却省掉 hold 时间 → 红

### 需要同步调整的既有测试

- `tests/test_door_timing.py:34` 把 `DOOR_TRAVEL_SECONDS == 34` **钉死**
  （"Pinned rather than bounded so that a change is always accompanied by a new
  measurement"）
- `tests/test_garage_guard.py:61` 钉住 guard 的 45 ≥ echo 窗口

一旦引入"每扇门自己填行程"，这两条的语义要从"常量相等"改成"配置项不低于下限"。

## 未决问题

### 1. 证据逻辑放哪一层

| 选项 | 效果 | 代价 |
| --- | --- | --- |
| 只进 `garage_guard` | 别人的门磁能消掉回声窗口和 45 秒 | **关门判据仍要自己写**（自动化不随 HACS 分发） |
| 同时暴露成实体 | 开+关两侧都能用，常量归一 | 改动面从 3 文件扩到 6+，本屋三个自动化要同步改 |

**关键约束**：`hacs.json` 是 `content_in_root: false` + `domains: ["roborock_plus"]`，
所以 **HACS 只装 `custom_components/`，`automations/` 和 `scripts/` 不会被安装。**

| 改哪里 | 本户受益 | 装了 HACS 的别人受益 |
| --- | --- | --- |
| 集成（`garage_guard.py` / `config_flow.py`） | ✅ | ✅ |
| 三个自动化 + `build.py` | ✅ | ❌ 拿不到 |

所以"把判据收进集成并暴露成实体"对通用场景**不是可选项** —— 只有集成里的东西
能被别人拿到。同理，45 秒散在自动化 YAML 和 `build.py` 里那两份**只影响本户**。

需动实体注册表（`binary_sensor.py:169-207` 的 setup）；而 `binary_sensor.py:312-318`
记录了改 unique_id 的代价（会让 HA 当新实体、孤立旧实体、切断历史），所以新实体
应新增而非改名。

### 2. 护栏用 repair 还是仅文案提示

`open_position` 填 40 这类"合法但危险"的值：HA repair issue，还是仅翻译文案提示？
属实现层要定。

## 非目标

- **不做动作抽象**（script/switch 当门）—— 见"为什么不做动作抽象"
- **不改三个自动化的现有行为**（除非未决问题 1 选"暴露成实体"）
- **不解决 App 发起的清扫**：Roborock App 命令不经过 HA，`garage_guard` 拦不到
  （`README.md:411` 已记为架构限制）
- **不做"门口有障碍物"检测**
- **不改 `binary_sensor.py` 既有实体的 unique_id**
