# 受控出库目标点（方向 + 距离）— 实施计划

日期：2026-11-08。
对应 [req](2026-11-08-req-exit-distance.md) / [spec](2026-11-08-spec-exit-distance.md)。
版本目标：0.12.0。

## T1 实施前检查

<files>
- specs/2026-11-08-req-exit-distance.md
- specs/2026-11-08-spec-exit-distance.md
- specs/2026-10-25-spec-controlled-exit.md
- custom_components/roborock_plus/garage_config.py
- custom_components/roborock_plus/garage_runtime.py
- custom_components/roborock_plus/config_flow.py
- custom_components/roborock_plus/safe_zone.py
- tests/test_controlled_exit_config.py
- tests/test_controlled_exit_options.py
- tests/test_options_ui.py
- tests/test_controlled_exit_runtime.py

<action>
- 通读 req/spec，确认 S15.1–S15.7 与 REQ-43..REQ-52 一一对应。
- 读 `safe_zone.py` 的 `suggest_safe_zone`，确认方向枚举与命名可直接复用。
- 读 `config_flow.py` 的 `_garage_form` / `field` / `choice`，确认新字段的接入点。
- 读 `_garage_values()` 与草稿暂存，确认新字段是否会被原样保留。
- 记录：`_garage_error_form()` 会把未知字段路由到 `garage_timing`，新字段要加进
  `_garage_fields('garage_timing')`，否则报错时字段不可见、无法修正。

<verify>
- 能指出每个 REQ 对应的 spec 章节与将改的文件行号。
- 列出 `_garage_fields` / `_garage_form` / `NUMERIC_RANGES` 三处必须同步的位置。

<done>
- 上面三处位置确认完毕，且没有发现 spec 未覆盖的行为。
</done>

## T2 纯逻辑：方向与目标点构造

<files>
- custom_components/roborock_plus/garage_config.py
- tests/test_exit_geometry.py（新建）

<action>
- 新增 `resolve_exit_direction(dock, zone, requested)`：
  - 手动值直接返回；
  - 否则用「危险区中心 − 充电座」主轴，`|vy| >= |vx|` 取南北，否则东西；
  - 中心与充电座重合返回 `None`；非法 requested 抛 `GarageConfigError`。
- 新增 `resolve_exit_target(dock, zone, direction, distance_mm)`：
  - north → `(dock.x, zone.min_y - distance)`
  - south → `(dock.x, zone.max_y + distance)`
  - west  → `(zone.min_x - distance, dock.y)`
  - east  → `(zone.max_x + distance, dock.y)`
  - 返回 `int` 四舍五入。
- 新增常量 `CONTROLLED_EXIT_LATERAL_WARN_MM = 1000`。
- 新增 `EXIT_DIRECTIONS = ('auto','north','south','east','west')` 与
  `_CONTROLLED_EXIT_DIRECTIONS`（无 auto）。

<verify>
```
python -m pytest tests/test_exit_geometry.py -q
```
覆盖：本屋几何算出 north；四方向构造点；主轴分量等于充电座；
中心重合返回 None；非法方向抛错。

<done>
- 用本屋实测值断言：`resolve_exit_target(dock, zone, 'north', 623) == (25696, 26500)`。
- 该断言失败即说明方向规则实现错了，不得进入 T3。

## T3 配置校验与迁移

<files>
- custom_components/roborock_plus/garage_config.py
- tests/test_controlled_exit_config.py

<action>
- `CONTROLLED_EXIT_FIELDS` 增加 `controlled_exit_distance_mm`、`controlled_exit_direction`。
- `NUMERIC_RANGES` 增加 `controlled_exit_distance_mm: (1, 65535)`。
- `validate_binding()` 启用分支改为：
  1. 若缺 `controlled_exit_distance_mm` 且存在 x/y → 反算（S15.3 四方向公式）；
  2. `distance <= 0` → `controlled_exit_target_inside_zone`；
  3. 横向偏移 > 1000 → `_LOGGER.warning`，不拒绝；
  4. 解析 axis，复算目标点必须严格在区外，否则 `controlled_exit_target_inside_zone`；
  5. 充电座不在区内 → `exit_direction_needs_dock_in_zone`；
  6. `direction` 非法 → `invalid_controlled_exit_direction`。
- 旧 `controlled_exit_x/y` **保留在返回值里**（REQ-48），不删除。

<verify>
```
python -m pytest tests/test_controlled_exit_config.py -q
```
覆盖：25696/26500 → 623；四个方向各自反算；横向偏移警告不拒绝；
区内拒绝；充电座在区外拒绝；非法方向拒绝；旧字段仍在。

<done>
- 现有绑定 `{x:25696, y:26500, timeout:120}` 迁移后 `distance_mm == 623`
  且 `resolve_exit_target(...) == (25696, 26500)`，行为零变化。

## T4 界面

<files>
- custom_components/roborock_plus/config_flow.py
- custom_components/roborock_plus/strings.json
- custom_components/roborock_plus/translations/zh-Hans.json
- custom_components/roborock_plus/translations/en.json
- tests/test_controlled_exit_options.py
- tests/test_options_ui.py

<action>
- `_garage_fields('garage_timing')` 的目标点字段从 x/y 换成
  `controlled_exit_direction` + `controlled_exit_distance_mm`（保留 timeout）。
- 新增 `choice(list(EXIT_DIRECTIONS), "garage_exit_direction")` 选择器。
- 新增 `NumberSelector`：`controlled_exit_distance_mm`，`unit_of_measurement="mm"`。
- 反算说明放进 `description_placeholders`：`dock_travel`，
  值 = 充电座到边界距离 + 距离；充电座缺失时填「未知」。
- 方向下拉的 `auto` 文案带解析结果（`自动（当前：北 / -Y）`）。
- 三份翻译文件同步新增 key。`strings.json` 与 `translations/*` 必须一致
  （`tests/test_translation_files.py` 会把关）。

<verify>
```
python -m pytest tests/test_controlled_exit_options.py tests/test_options_ui.py \
                 tests/test_translation_files.py -q
```

<done>
- 开关关闭时两个新字段都不出现；打开时出现且预填 623 / auto。
- 充电座读不到时表单仍能渲染和保存（不抛异常）。

## T5 运行期读取与容差

<files>
- custom_components/roborock_plus/garage_runtime.py
- custom_components/roborock_plus/garage_config.py
- tests/test_controlled_exit_runtime.py

<action>
- `_controlled_exit_settings()` 改为调用 `resolve_exit_target()`，
  用「充电座 + 危险区 + direction + distance」构造 target；构造失败抛
  `GarageControlError('exit_target_unavailable')`。
- `CONTROLLED_EXIT_TOLERANCE_MM: 400 → 200`。
- 诊断实体（`garage_entities.py`）`extra_state_attributes` 增加
  `controlled_exit_direction` / `controlled_exit_distance_mm` /
  `controlled_exit_target` / `controlled_exit_legacy_xy`。

<verify>
```
python -m pytest tests/test_controlled_exit_runtime.py tests/test_garage_entities.py -q
```

<done>
- 10 条既有行为测试全绿（不含旧 x/y 假设）。
- 新增断言：目标点由距离构造；诊断属性齐全。

## T6 回归与变异

<files>
- scripts/mutation_check_controlled_exit.py
- tests/test_exit_geometry.py
- CHANGELOG.md
- custom_components/roborock_plus/manifest.json

<action>
- 变异新增至少四条：
  1. 方向用「最近的边」推算（本屋会错成 +X/+Y）→ 必须变红；
  2. auto 方向忽略手动覆盖 → 必须变红；
  3. 迁移时把横向偏移当误差丢掉却不记警告 → 必须变红；
  4. 容差改回 400 → 必须变红（或明确记录为等效变异并说明理由）。
- CHANGELOG 写 v0.12.0，manifest 版本同步。
- 全量测试 + 全部变异脚本。

<verify>
```
python -m pytest -q
python scripts/mutation_check_controlled_exit.py
```

<done>
- 全量通过；受控出库变异全捕获；其余既有变异脚本无新增失败。

## 完成前漂移检查

- [ ] REQ-43..REQ-52 每条都有对应实现与测试，无遗漏。
- [ ] 没有在 spec 之外新增字段、状态或界面元素。
- [ ] `controlled_exit_x/y` 只保留为诊断，**运行期无任何读取**（grep 确认）。
- [ ] 开关关闭路径零执行。
- [ ] 本屋实测值 623 / north / (25696, 26500) 三者互相自洽。
- [ ] 旧绑定升级后行为零变化，有测试证明。

## 分层 Review

- **产品 review**：界面是否真的降低了心智负担；「前进 1939mm」是否比 26500 更易懂。
- **工程 review**：方向推算与构造是否单点可测；迁移是否幂等（多次验证不重复反算）。
- **UI review**：字段只在开关打开时出现；充电座缺失时降级不崩。
- **验证 review**：变异是否覆盖「最近的边」这个真实陷阱；是否有等效变异未说明。
