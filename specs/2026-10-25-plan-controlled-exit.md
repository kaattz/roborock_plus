# 受控出库与车库门全流程托管 — 实施计划

日期：2026-10-25。状态：**待用户评审**。
对应 [req](2026-10-25-req-controlled-exit.md) / [spec](2026-10-25-spec-controlled-exit.md)。
版本目标：**0.9.0**（新增可选配置与新阶段，默认关闭）。
本批只做本地实现与隔离验证，**不部署 HA、不发版、不操作设备**。

## 实施前检查

- `<files>` 本批 req/spec、原 req/spec/plan、AGENTS.md、lessons.md、
  `garage_control.py`、`garage_runtime.py`、`garage_task.py`、`garage_config.py`、
  `config_flow.py`、`const.py`、`vacuum.py`、`safe_zone.py`、
  `tests/test_garage_after_exit.py`、`tests/test_garage_exit_close.py`、
  `tests/test_garage_external_close.py`、`tests/test_garage_runtime.py`。
- `<action>` 核对：`_NEW_TASK_COMMANDS` 全部成员；`async_execute` 的分支顺序与所有
  `passage_allowed` 检查点；`async_update` 推进点；`_update_after_exit()` 短路位置；
  `fault()`/`notify()` 去重键；`_validate_supplied_fields` 风格；
  **grep 所有 phase 比较处**，确认新值 `controlled_exit` 有明确归属。
- `<verify>` `pytest tests -q` 取基线（应为 1276 passed）。
- `<done>` 基线记录 + 「新 phase 影响清单」+ 「`passage_allowed` 全部检查点清单」。

## T1 配置：开关与目标点（REQ-32 / REQ-37）

- `<files>` `const.py`、`garage_config.py`、`config_flow.py`、`strings.json`、
  `translations/en.json`、`translations/zh-Hans.json`
- `<action>` 新增 4 字段与默认值；`_validate_supplied_fields` 加范围校验；
  启用缺字段报 `missing_controlled_exit_target`；Options Flow 在 garage 分组加控件。
- `<verify>` `tests/test_controlled_exit_config.py`：默认关闭；非法 bool/坐标/超时拒绝；
  启用缺坐标拒绝；合法配置保存；关闭时返回值与 0.8.1 等价。
- `<done>` 全绿；关闭路径无新字段副作用。

## T2 插入条件（REQ-33）

- `<files>` `garage_runtime.py`
- `<action>` `async_send` 前加 `_should_controlled_exit(name)`，覆盖四种清扫命令，
  按 S14.2 四条件；不满足完全走原路径。
- `<verify>` `tests/test_controlled_exit_trigger.py`：开关关闭 / `session.active` /
  phase 不符 / `reason` 非空 各自不触发；四条件全真触发；
  `app_segment_clean`、`app_zoned_clean`、`app_spot` 均被覆盖；
  不触发路径不产生 goto 命令。
- `<done>` 七条断言全绿。

## T3 第 2–3 步：开门、goto、坐标到达（REQ-34 / REQ-36）

- `<files>` `garage_control.py`、`garage_runtime.py`
- `<action>` 新增 `phase='controlled_exit'`、`_controlled_exit_target`、
  `_controlled_exit_deadline`、`_controlled_exit_arrived()`；
  `async_update` 推进到达/超时；失败 `fault('controlled_exit_failed')` 且不发清扫命令。
- `<verify>` `tests/test_controlled_exit_sequence.py`：
  1. 门已可通行不重复开门
  2. goto 发出后 phase == `controlled_exit`
  3. 坐标在容差内判定到达
  4. 坐标在容差外不判定到达
  5. 超时 → fault 且无清扫命令
  6. 位置不可信不判定到达
  7. goto 参数是 `[x, y]` 且等于配置坐标
- `<done>` 七条全绿。

## T4 第 4 步关门 + 出库中禁止关门（REQ-35 / REQ-38）

- `<files>` `garage_control.py`
- `<action>` `_close_after_controlled_exit()` 按 S14.4 判据；
  `_update_after_exit()` 首行按 S14.5 短路；
  关门失败 → `fault('controlled_exit_close_failed')` + 不发清扫命令 + 发告警。
- `<verify>` `tests/test_controlled_exit_close.py`：
  1. 到达后发出 `close_cover`
  2. 关门成功后继续清扫命令
  3. 关门失败 → 无清扫命令
  4. 关门失败 → `garage_issue` 且 `reason == 'controlled_exit_close_failed'`
  5. 出库中 `_exit_due` 为真也不关门
  6. 离开 `controlled_exit` 后关门判定恢复
  7. 家庭许可 `off` → 不关门且不清扫
- `<done>` 七条全绿；变异「关门失败仍清扫」必须变红。

## T5 第 5 步门判据按位置分支（REQ-39 / REQ-40）

- `<files>` `garage_control.py`
- `<action>` 按 S14.4 把 `async_execute` 的 `passage_allowed` 要求改成位置分支；
  `robot_outside` 为假时完全走原逻辑。
- `<verify>` `tests/test_controlled_exit_passage.py`：
  1. 机器人在区外 → 门关着也能发清扫命令
  2. 机器人在区内 → 仍要求通行（原行为）
  3. 机器人停靠（`docked`）→ 不算 `robot_outside`
  4. 位置不可信 → 不算 `robot_outside`
  5. `outside_danger_zone` 为 `None` → 不算 `robot_outside`
  6. 顶替坐标未被信任时 → 不算 `robot_outside`（REQ-40）
  7. `resume` 命令同样适用
- `<done>` 七条全绿；变异「无条件跳过门判据」必须变红（会造成停靠时放行）。

## T6 可恢复与原因码（REQ-41）

- `<files>` `garage_control.py`、`strings.json`、`translations/*.json`
- `<action>` 两个新原因码与翻译；确认 `reset_garage_control` 可清除。
- `<verify>` `tests/test_controlled_exit_recovery.py`：两种失败都可 reset 清除；
  无永久锁存；reset 后正常清扫可通过守卫。
- `<done>` 三条全绿。

## T7 回归与变异（REQ-42）

- `<files>` 既有测试、`.analysis/mutate_controlled_exit.py`（新建）
- `<action>` 变异至少覆盖：
  ①开关关闭仍插入 ②忽略 `session.active` ③到达判定改固定等待
  ④去掉出库中关门短路 ⑤关门失败仍发清扫命令 ⑥坐标缺失回落默认值
  ⑦到达容差无穷大（永不失败） ⑧无条件跳过门判据 ⑨顶替坐标被当作区外
- `<verify>` `pytest tests -q` 全绿；离线原生 HA 全组通过；9 条变异逐个变红。
- `<done>` 结果写入完成记录；未放宽任何既有断言。

## T8 文档与交付

- `<files>` `CHANGELOG.md`、`README.md`、`docs/garage-control-checklist.md`、
  `manifest.json`（0.9.0）、原 spec 交叉引用
- `<action>` 标注**实验性、默认关闭、不保证消除蹭门**；记录旧方案（状态变化触发）
  为何失败的实测结论；清单增加实测步骤。
- `<verify>` 全量测试仍绿；人工通读。
- `<done>` 文档与行为一致，不含「已解决蹭门」这类未经验证的断言。

## 分层 Review

| 层 | 关注点 | 检查点 |
| --- | --- | --- |
| 安全（最高） | 出库中绝不关门；关门失败绝不清扫；门判据分支不得在停靠时放行 | T4、T5 后各一次，未过不得继续 |
| 产品 | 失败路径可解释；实验性质表达清楚 | T6 后 |
| 工程 | 新 phase 被所有判定正确处理；是否需要持久化 | T3 后 |
| 验证 | 9 条变异全捕获；默认关闭零影响被证明 | T7 后 |

## 完成前漂移检查

1. grep 全部 phase 比较处，`controlled_exit` 是否都有明确归属。
2. grep 全部 `passage_allowed` 检查点，确认只有目标那处被分支化。
3. 关闭开关时是否真的零路径执行（变异证明）。
4. 关门失败时是否真的没有清扫命令。
5. 坐标缺失时是否拒绝启用而非回落默认。
6. 停靠状态下 `robot_outside` 是否恒为假。
7. 既有断言是否一处未改。
8. 文档是否误称「已解决蹭门」。
9. 是否误称已部署或硬件已验收。

## 完成记录

本批已完成本地实现与隔离验证，**未部署 HA、未发版、未操作设备**。

### 实施结果

| 任务 | 结果 |
| --- | --- |
| T1 | 4 个配置字段 + 校验 + 默认值；15 项 `test_controlled_exit_config.py` |
| T2 | `_should_controlled_exit()` 覆盖 4 种清扫命令；20 项 `test_controlled_exit_trigger.py` |
| T3 | `async_run_controlled_exit()` + `_wait_controlled_exit()` + `_controlled_exit_arrived()`；11 项 `test_controlled_exit_sequence.py` |
| T4 | `_close_after_controlled_exit()` + `_update_after_exit` 短路 |
| T5 | `_robot_is_outside()` 分支两处 `passage_allowed` 检查 |
| T6 | phase 标签三语；`controlled_exit_failed` / `controlled_exit_close_failed` 原因码经既有 `fault()` 路径 |
| T7 | 变异 **9/9 捕获**；复跑 6/6 与 13/13 无回归 |
| T8 | manifest 0.9.0、CHANGELOG、清单 4 条验收项已同步 |

### 验证

- 主机全量：**1322 passed**
- 离线原生 HA 2026.10.1（14 个 `ha_native_*.py`）：**157 passed**
- 变异：受控出库 9/9、离库 6/6、外部开门 13/13 全捕获
- `compileall` 通过；三份 JSON 解析通过

### 与计划的偏差

1. **`send_robot` 签名扩展**：控制器原本只发 `app_pause`/`app_resume`（单参数），
   受控出库需要带坐标的 `app_goto_target`。`GarageRuntime.async_control_command`
   改为 `(command, params=None)`，并**拒绝其它命令**，避免控制器获得通用命令通道。
2. **`_robot_is_outside()` 用 `getattr` 读取**：既有测试的机器人替身没有
   `position_trusted` 属性，直接属性访问会让它们报 `AttributeError`。改用防御式
   读取后，缺该属性的替身自然落到「不在区外」，行为与改动前一致。
3. **未删除遗留守卫**：`garage_guard.py` 的旧路径在配置新车库控制后是死代码，
   但删除会影响 `button.py`、`config_flow.py`、迁移逻辑与 12 个测试文件。
   按用户指示留待独立批次（例程迁移同批处理）。

### 未验收项

- **实物验收全部待用户完成**，清单新增 4 条（含「`goto` 自身是否也会打转」这一关键观察）。
- 未部署 HA，未发版。
- **不保证消除蹭门**：`goto` 只给终点不给路径。若实测无效，本功能应关闭并另寻方案。
