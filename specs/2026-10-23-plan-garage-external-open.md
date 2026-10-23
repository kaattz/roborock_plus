# 车库控制：外部开门授权与人工接管收窄 — 实施计划

日期：2026-10-23。状态：**待用户评审**。
对应 [req](2026-10-23-req-garage-external-open.md) / [spec](2026-10-23-spec-garage-external-open.md)。
版本：0.7.9 → 0.8.0（新增服务，按语义化版本升 minor）。
本批只做本地实现与隔离验证，**不部署 HA、不发版、不操作设备**。

## 实施前检查

- `<files>` 两版 req/spec（原 `2026-10-15-*` 与本批 `2026-10-23-*`）、AGENTS.md、lessons.md、
  `garage_control.py`、`garage_runtime.py`、`door_evidence.py`、`garage_task.py`、
  `garage_entities.py`、`services.py`、`services.yaml`、`vacuum.py`、`strings.json`、
  `translations/en.json`、`tests/test_garage_closed_stop.py`、`tests/test_garage_runtime.py`、
  `tests/ha_native_garage*.py`、`automations/garage_door_sync_folding_door.yaml`。
- `<action>` 核对：`external_command` 的现有豁免边界；`_contexts` 的过滤范围；`_wait` 对
  `passage_allowed` 的依赖；`async_request_close` 的前置链；`notify()` 的去重键；
  现有测试里哪些断言会因本批行为变更而需要「在冲突前提下重写」。
- `<verify>` 逐个函数读完并记录行号；跑一次 `pytest tests -q` 取得基线（应为 1193 passed）。
- `<done>` 已知的「会红的测试」清单明确：`test_other_external_commands_never_borrow_the_closed_stop_exemption`
  的 `open_cover`/`close_cover` 两参数必须改为「有冲突」前提；其余不得改断言。

## T1 控制器：无冲突判定（REQ-18）

- `<files>` `garage_control.py`
- `<action>` 新增只读判定 `_no_conflict_for_external(direction, *, door_moving)`，按 S11.1
  的 10 个条件逐条实现（`'move'` 直接 False）；在 `external_command()` 中**先于**
  现有 `fault()` 调用；放行时只记 `_LOGGER.info` 并返回，不触碰
  `reason/phase/operation_id/evidence`。不新增字段、不改 `fault()`。
- `<verify>` 新增 `tests/test_garage_external_open.py`：11 个参数化反例（每个条件单独置假
  → 仍 `manual_override`）+ 1 个组合正例（全真 → `reason is None` 且 `phase` 不变）；
  断言放行时 `operation_id` 不变、`evidence` 未被 `invalidate`。
- `<done>` 反例全红、正例全绿；既有 54 项 `test_garage_closed_stop.py` 不改动且全绿。

## T2 豁免不授予通行（REQ-19）

- `<files>` `tests/test_garage_external_open.py`、`tests/test_garage_runtime.py`
- `<action>` 用原生回放构造：外部开门被豁免 → 紧接着发清扫命令 → 断言门命令总数为 2
  （外部 1 次 + 清扫 1 次），且清扫那次仍等待完整行程。断言豁免前后
  `passage_allowed` 都不是 `True`（`timed` 来源下应为 `None`/`False`）。
- `<verify>` 该测试通过；把 T1 的判定改成「直接返回 True 且 `note_command`」后必须变红
  （证明它真的在守 REQ-19）。
- `<done>` 变异捕获；无设备动作。

## T3 锁存可见性（REQ-20）

- `<files>` `garage_runtime.py`、`tests/ha_native_garage.py`
- `<action>` `notify()` 的 `active=True` 分支加 `previous_phase` 字段（取自 `controller`
  在 `fault` 前记录的值；`fault()` 已计算 `previous_phase` 但未外露，改为把最近一次的
  `previous_phase` 存到控制器上供读取，不新增持久化）。事件键与去重逻辑不变。
- `<verify>` 原生测试：`fault(manual=True)` → 事件 `active=true` 且含 `previous_phase='idle'`；
  `reset()` → `active=false`；同键重复 `fault` 不重复发。
- `<done>` 三条断言通过；无新增事件类型、无新增持久化字段。

## T3b 锁存通知自动化（REQ-20a）

- `<files>` `automations/roborock_garage_issue_notify.yaml`（新建）、
  `automations/README.md`（登记 unique_id）、`tests/test_garage_issue_notify.py`（新建）
- `<action>` 按 S11.5 的契约新建自动化：`event: roborock_plus_garage_issue` 触发，
  `active=true` 时经 `script.alert_notify` 发 warning，`active=false` 静默。
  消息模板引用 `reason / phase / previous_phase / cover_entity_id / vacuum_entity_id`，
  并明确「不会自动解除、不会操作门或机器人」。
- `<verify>` 结构测试断言：
  1. `id` 为 `1791600000001`，alias/description 非空；
  2. 触发器只有 `roborock_plus_garage_issue`，无其它触发源；
  3. 动作树里**不存在** `cover.` / `vacuum.` / `button.` / `automation.trigger` /
     `roborock_plus.reset_garage_control` 任何一项；
  4. 存在 `script.alert_notify` 且带 `level: warning`；
  5. 消息模板引用了五个事件字段；
  6. `active` 为假的分支不发通知。
  变异：把 `if active` 去掉 → 测试必须变红（否则解除时也会刷通知）。
- `<done>` 六条断言全绿；变异捕获；README 表格已登记，`test_it_records_every_unique_id` 通过。

## T4 控制器：受控开门（REQ-21 控制器层）

- `<files>` `garage_control.py`
- `<action>` 按 S11.3 实现 `async_request_open()`。要点：`passage_allowed is True` 时
  **幂等短路**（不发设备命令）；`_acquire/_release` 沿用；开门前 `_preflight('open')`；
  `note_command('open')` 之后才 `_wait('open')`；结束 `phase='idle'` + `persist()`；
  异常路径与 `async_request_close()` 逐行对称（含 `operation_cancelled`）。
- `<verify>` 单测：已通行 → `door_calls == []`；正常 → `['open']` 且等待 `travel+margin`；
  反馈不可用/端点冲突/恢复快照 → 对应原因码且无设备命令；`_acquire` 并发 → `operation_in_progress`。
- `<done>` 五类结果全覆盖；不改 `async_request_close` 的任何一行。

## T5 服务与运行时接线（REQ-21 / REQ-22）

- `<files>` `services.py`、`services.yaml`、`vacuum.py`、`garage_runtime.py`、
  `strings.json`、`translations/en.json`
- `<action>` 注册 `REQUEST_GARAGE_OPEN_SERVICE_NAME = "request_garage_open"`，
  `entity_domain=VACUUM_DOMAIN`、`schema=None`、`func="async_request_garage_open"`、
  `supports_response=SupportsResponse.NONE`；`GarageRuntime.async_request_open()` 对称于
  `async_request_close()`（含 `_first_install` 检查与 `publish()`）；`vacuum.py` 的异常转换
  与 `async_request_garage_close` 完全一致；`services.yaml` 与两个翻译文件同步增补。
- `<verify>` 原生测试：服务可注册、target 过滤只匹配 roborock_plus 的 vacuum、
  失败抛 `ServiceValidationError` 且 `reason` 为稳定码；翻译完整性测试（既有）覆盖新条目。
- `<done>` 服务出现在「动作」列表且中英描述齐全；不新增 reason 码。

## T6 折叠门联动改用受控通道（REQ-23）

- `<files>` `automations/garage_door_sync_folding_door.yaml`、`tests/test_folding_door_link.py`、
  `automations/README.md`
- `<action>` 动作改为「`passage_allowed` 非 on 时调 `roborock_plus.request_garage_open`」，
  删除全部 `cover.*` 调用；文件头说明改写为「由集成开门并等待行程」；README 对应条目更新。
- `<verify>` `tests/test_folding_door_link.py` 改为断言：`roborock_plus.request_garage_open`
  存在；`cover.` 前缀的任何动作都不存在；仍保留 `passage_allowed` 前置；`id` 不变。
- `<done>` 结构断言全绿；变异（把动作换回 `cover.stop_cover`）必须变红。

## T7 回归与变异（REQ-24 / REQ-25）

- `<files>` `tests/test_garage_closed_stop.py`、`tests/test_garage_control.py`、
  `tests/ha_native_garage*.py`
- `<action>` 把 `test_other_external_commands_never_borrow_the_closed_stop_exemption` 的
  `open_cover`/`close_cover` 参数改为在 `task_open`（有冲突）前提下断言仍锁存；
  新增「无冲突时 `open_cover` 被豁免」的独立正例。其余断言一律不改。
- `<verify>` `pytest tests -q` 全绿；对 T1/T4 的每条守卫做内存变异，逐个确认变红。
- `<done>` 全量通过；变异清单与结果写入本计划的完成记录；**未为了绿灯而放宽任何既有断言**
  （唯一改动是上述两个参数的前提，且已在 spec S11.6 显式记录为有意行为变更）。

## T8 文档与交付（REQ-25）

- `<files>` `README.md`、`CHANGELOG.md`、`docs/garage-control-checklist.md`、
  `specs/2026-10-15-spec-garage-control.md`（S6 增补交叉引用）、`automations/README.md`
- `<action>` README 的服务表增补 `request_garage_open`；CHANGELOG 增设 0.8.0 条目；
  清单文档的验收项增补「外部开门在无冲突时不锁存」「折叠门联动走受控通道」
  「锁存会推送告警」；原 spec S6 补一行指向本批；`automations/README.md` 的
  「四个/五个自动化」标题与表格增补新通知自动化（T3b 已登记 id，此处补说明段落）。
- `<verify>` `pytest tests -q` 仍全绿（文档断言测试存在）；人工通读一致性。
- `<done>` 文档与实际行为一致；未声称为硬件验收通过。

## 分层 Review

| 层 | 关注点 | 检查点 |
| --- | --- | --- |
| 产品 | 不缩小原安全边界（任务中/门在动仍锁存）；不引入自动解除；通知自动化只读不自动 reset | T1 完成后、T3b 后、T6 前 |
| 工程 | `_acquire/_release` 配对；`_wait` 依赖链；异常路径不漏 `fault`；无新持久化 | T4 完成后 |
| 安全（独立于工程） | 「无冲突」十个条件缺一不可；`'move'` 永不豁免；豁免后不授予通行 | T2、T7 完成后 |
| 验证 | 变异全捕获；未放宽既有断言 | T7 完成后 |

## 完成前漂移检查

逐条回查：

1. REQ-18 的十个条件是否与 S11.1 表格一一对应，没有多也没有少。
2. REQ-19 是否真的做到「豁免不产生 `_command`」——检查放行分支不触碰 `evidence`。
3. REQ-20 是否只加字段、没换事件类型、没改去重键。
3b. REQ-20a 的通知自动化是否真的只读：无 `cover.*`/`vacuum.*`/`button.*`/`reset`/
   `automation.trigger`，且 `active=false` 不通知。
4. REQ-21/22 的原因码是否全部落在原 REQ-12 的集合内。
5. REQ-23 的自动化是否已无任何 `cover.*` 调用，且 `id` 未变。
6. REQ-24 的既有断言是否只在 spec 明示的两处被改写。
7. 是否有任何未写入 spec 的新行为（尤其是新的相位或新的豁免方向）。
8. 是否误称「已部署」或「硬件已验收」。
9. 新增自动化是否已在 `automations/README.md` 登记 unique_id（否则
   `test_it_records_every_unique_id` 会红）。

## 完成记录

本批已完成本地实现与隔离验证，**未部署 HA、未发版、未操作设备**。
交付组合 SHA-256（按上面六个路径顺序拼接）：
`4ca34d6e9339e85f8bd4dce4470f0dc1ca019b0846dbb8a86598523dc502d0bf`

### 实施结果

| 任务 | 结果 |
| --- | --- |
| T1 | `_no_conflict_for_external` 落地，10 个条件逐条实现；31 项控制器测试 |
| T2 | 豁免后门命令次数断言通过；变异「让豁免授予通行」被捕获 |
| T3 | `fault()` 记录 `previous_phase`，事件新增该字段；35 项 runtime 测试通过 |
| T3b | `roborock_garage_issue_notify.yaml` 新建，9 项只读断言 |
| T4 | `async_request_open` 落地，12 项测试 |
| T5 | 服务注册 + runtime + vacuum 接线，5 项服务测试；三份翻译齐全 |
| T6 | 折叠门自动化改调受控通道，9 项结构断言 |
| T7 | 变异见下 |
| T8 | README/CHANGELOG/清单/原 spec/automations README 已同步；版本升至 0.8.0 |

### 验证

- 主机全量 `pytest tests -q`：**1253 passed**
- 离线原生 HA 2026.10.1（`tests/ha_native_*.py`，14 个文件）：**156 passed**，
  38 条既有上游弃用提示（aiohttp/backoff/jinja2），按既有规则记录不扩修
- 控制器变异：**13/13 捕获**
- 自动化变异：**4/4 捕获**
- `compileall` 与蓝图结构校验见下

### 与计划的偏差（需要记录）

1. **删除了一条计划外的守卫**。原实现里 `async_request_open` 有一个显式的
   `cover_available` 检查，但它不可达——`refresh()` 已把可用性折进端点证据，
   `_preflight` 用同一个 `feedback_unavailable` 拒绝。按「测不到的守卫会腐烂」取舍，
   删掉而不是留一条无法被变异捕获的代码；这一条已写进 `mutate_external_open.py`
   的文件头说明。这是**减少**代码，不是放宽行为。
2. **测试前提修正**（不是放宽断言）：`test_garage_closed_stop.py` 里
   `test_other_external_commands_never_borrow_the_closed_stop_exemption` 的
   `open_cover`/`close_cover` 两个参数按 spec S11.6 的预告改成了「有冲突前提」，
   并新增等强度的「任务中仍锁存」反例。其余断言一字未改。
3. **`test_garage_external_open.py` 的两处初版测试是我写错**：
   - 「已有保持不被清除」原本断言 `reason` 不变，但既有实现本就会把旧 reason
     重写为 `manual_override`；改为断言「仍被锁存、仍拒绝清扫、无设备动作」，
     与原 `closed_stop` 测试同强度。
   - 「开门方向 while opening」按既有 v0.7.x 行为是同向忽略，不是锁存；
     拆成同向忽略 + 反向仍锁存两条。

### 未验收项

- **实物验收全部待用户完成**：清单新增的三条（空闲外部开门不锁存、任务中仍锁存、
  折叠门走受控通道）都需要真实门与机器人。
- 未部署到 HA，未发 HACS 版本。升级后需要用户重新导入两条自动化
  （`garage_door_sync_folding_door`、`roborock_garage_issue_notify`）。
- `roborock_garage_issue_notify` 依赖 `script.alert_notify` 存在；本屋已有。
