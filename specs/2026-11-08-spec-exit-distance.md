# 受控出库目标点（方向 + 距离）— 规格

日期：2026-11-08。状态：**待用户评审**。
对应 [req](2026-11-08-req-exit-distance.md)。
关联：[受控出库 spec](2026-10-25-spec-controlled-exit.md) S14.1/S14.3。

## 代码证据

| 阅读对象 | 位置 | 事实 |
| --- | --- | --- |
| 现有目标点字段 | `garage_config.py:22-24` | `CONTROLLED_EXIT_FIELDS = (x, y, timeout)`，范围 `(0,65535)` |
| 默认值 | `garage_config.py:37-39` | `DEFAULT_CONTROLLED_EXIT_X/Y = 25696/26500`，注释说明「仅表单预填」 |
| 启用时强制填全 | `garage_config.py:233-238` | 缺任一 → `missing_controlled_exit_target` |
| 到达容差 | `garage_config.py:44` | `CONTROLLED_EXIT_TOLERANCE_MM = 400` |
| 运行期读取目标点 | `garage_runtime.py:485-490` | `_controlled_exit_settings()` 直接读 `binding['controlled_exit_x']` |
| 目标点使用点 | `garage_runtime.py:520` | `app_goto_target(list(target))` |
| 方向四选一先例 | `safe_zone.py:43-86` | `suggest_safe_zone(cabinet_direction=...)`，north/south/east/west |
| 表单字段装配 | `config_flow.py:770-783` | `_garage_fields()` 按开关决定是否包含目标点字段 |
| 预填机制 | `config_flow.py:808-819` | `timing_suggestions` 只填缺失键 |
| 数字控件 | `config_flow.py:857-869` | `NumberSelector`，`unit_of_measurement` 按 key 分支 |
| 隐藏字段可编辑 | `config_flow.py:791-799` | `extra_fields` 让无效保留值仍可改 |
| 危险区存储 | `safe_zone_store.py` | 按 duid 存 `SafeZone(min_x,max_x,min_y,max_y)` |
| 充电座来源 | `v1_position_trust.py:113-152` | `resolve_position_trust` 用地图 `charger` 标记 |
| 既有测试 | `tests/test_controlled_exit_config.py` | 覆盖坐标范围/缺字段/默认值，需随本次迁移改写 |

## S15.1 数据模型（REQ-43 / REQ-50）

绑定新增两字段：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `controlled_exit_distance_mm` | int | 无（启用时必填） | 目标点**超出危险区边界**的垂直距离 |
| `controlled_exit_direction` | str | `"auto"` | `auto` / `north` / `south` / `east` / `west` |

目标点由方向 + 距离 + 几何**构造**，不存坐标：

```python
def resolve_exit_target(*, dock, zone, direction, distance_mm) -> tuple[int, int] | None:
    axis = resolve_exit_direction(dock, zone, direction)   # 自动或手动
    if axis is None:
        return None
    if axis == 'north':
        return dock.x, zone.min_y - distance_mm
    if axis == 'south':
        return dock.x, zone.max_y + distance_mm
    if axis == 'west':
        return zone.min_x - distance_mm, dock.y
    return zone.max_x + distance_mm, dock.y              # east
```

**只有主轴分量**：north/south 时 x 恒等于 `dock.x`；west/east 时 y 恒等于 `dock.y`。
这是 REQ-50 的直接体现，也是「直线驶出」原意。

坐标系是整数毫米，构造结果取 `int(round())`。

## S15.2 方向推算（REQ-44 / REQ-45）

```python
def resolve_exit_direction(dock, zone, requested) -> str | None:
    if requested in ('north', 'south', 'east', 'west'):
        return requested                                   # 手动优先
    center = ((zone.min_x + zone.max_x) / 2, (zone.min_y + zone.max_y) / 2)
    vx, vy = center[0] - dock.x, center[1] - dock.y
    if vx == 0 and vy == 0:
        return None                                        # 无法判断，拒绝
    if abs(vy) >= abs(vx):
        return 'north' if vy < 0 else 'south'
    return 'west' if vx < 0 else 'east'
```

**为什么不用「最近的边」**：本屋充电座到 +X 只有 373mm、到 +Y 只有 325mm，
而真实出库是 -Y（1316mm，最远的那条）。按最近边会推向旁边那扇门 ——
2026-10-25 蹭门事故的方向。见 req 的实测表。

**本屋预期结果**：向量 (-163, -495.5)，`|vy| >= |vx|` 且 `vy < 0` → `north`（-Y）。
复核 `28439 - 1316 - 623 = 26500`，与现有配置一致。

`requested` 不是五个合法值之一 → `GarageConfigError('invalid_controlled_exit_direction')`。

## S15.3 旧配置迁移（REQ-47 / REQ-48）

`validate_binding()` 在启用且**没有** `controlled_exit_distance_mm` 时：

```
1. 若 x/y 都在 → 反算
   axis = resolve_exit_direction(dock, zone, requested or 'auto')
   axis == north → distance = zone.min_y - y
   axis == south → distance = y - zone.max_y
   axis == west  → distance = zone.min_x - x
   axis == east  → distance = x - zone.max_x
2. distance <= 0 → GarageConfigError('controlled_exit_target_inside_zone')
3. 横向偏移检查
   axis in (north, south): lateral = |x - dock.x|
   axis in (west, east):   lateral = |y - dock.y|
   lateral > CONTROLLED_EXIT_LATERAL_WARN_MM (1000) → 记 warning，不拒绝
4. 结果写入 controlled_exit_distance_mm
```

**横向偏移只警告不拒绝**，理由：迁移的是既有可运行配置，拒绝会把用户挡在升级之外。
但必须记日志，因为新模型无法表达横向偏移，精度会丢。

**旧字段保留**（REQ-48）：迁移后 `controlled_exit_x` / `controlled_exit_y`
**原样留在 binding 里**，但运行期不再读取（`_controlled_exit_settings()` 改读 distance）。
诊断实体 `garage_phase` 的 `extra_state_attributes` 增加：

```
controlled_exit_direction     解析后的实际方向（auto 也解析成具体值）
controlled_exit_distance_mm   生效距离
controlled_exit_target        构造出的 (x, y)
controlled_exit_legacy_xy     若存在旧字段，原样回显
```

这样排查时能直接看到「我以为设的是 26500，实际算出来是多少」。

## S15.4 界面（REQ-46）

`garage_timing` 步骤，`controlled_exit_enabled` 为真时显示：

```
出库方向   [自动（当前：北 / -Y）▼]        SelectSelector
出库距离   [ 623 ] mm 超出危险区边界        NumberSelector (1..65535, step 1)
           └ 相当于从充电座前进 1939 mm     描述文字，非输入
```

**反算说明**是表单描述（`description_placeholders`），每次渲染时按当前
危险区、充电座、方向、距离重新计算。公式：

```
前进距离 = 充电座到该方向边界的距离 + 出库距离
```

**充电座从哪来**：`coordinator.properties_api.map_content.map_data.charger`。
读不到时（地图未加载）→ 说明文字显示「充电座未知，无法换算」，但**不阻止保存**
（保存需要的是距离，不需要充电座）。这一点必须写进实现，否则地图未加载时表单会崩。

**方向下拉的显示值**：`auto` 选项的文字要带解析结果，例如
`自动（当前：北 / -Y）`，让用户不必先保存就知道会往哪走。

## S15.5 校验（REQ-49）

启用时：

```
controlled_exit_direction 必须是 auto/north/south/east/west
controlled_exit_distance_mm 必须是 1..65535 的整数
按方向解析出 axis 后：
    distance_mm > 0（已由范围保证）
    构造出的目标点必须严格在危险区外（复算 point_in_danger_zone）
    充电座必须在危险区内（否则方向推算与出区语义都不成立）
```

**为什么要复算「点在区外」**：方向与危险区可能被配成互相矛盾的状态
（例如手动选 north 但充电座贴着 min_y）。构造公式本身保证出区，
但方向选错时目标点会落在别处 —— 复算是最后一道闸。

**充电座不在区内** → `GarageConfigError('exit_direction_needs_dock_in_zone')`。
这是新增约束，理由：出库语义是「从区内走到区外」，充电座不在区内说明
危险区配错了，此时任何距离推算都无意义。

## S15.6 到达容差（REQ-51）

`CONTROLLED_EXIT_TOLERANCE_MM: 400 → 200`。

| 口径 | 400mm | 200mm |
| --- | --- | --- |
| 目标点离区界 623mm，最坏余量 | 223mm | **423mm** |
| 定位偏大时的超时风险 | 低 | 略高 |

风险由 v0.11.6 加的 `trip_finished()` 兜底：定位偏差导致坐标判不上时，
「goto 结束且已出区」仍能放行。所以收紧容差不会把系统变脆。

## S15.7 不回归（REQ-52）

必须保持绿：

- `tests/test_controlled_exit_runtime.py`（10 条，v0.11.6 新增）
- `tests/test_controlled_exit_config.py`（随迁移改写）
- `tests/test_controlled_exit_options.py`（表单字段集合）
- `tests/test_options_ui.py`（`_page_keys` 断言，需同步）
- `mutation_check_controlled_exit.py` 8/8

**开关关闭时一行代码都不该执行**：`resolve_exit_target` 不应被调用。

## 需求追踪

| 需求 | spec | plan | 验证 |
| --- | --- | --- | --- |
| REQ-43 | S15.1 | T2 | `resolve_exit_target` 单元测试 |
| REQ-44 | S15.2 | T2 | 本屋算出 north；退化几何返回 None |
| REQ-45 | S15.2 | T2 | 手动值优先于 auto |
| REQ-46 | S15.4 | T4 | 表单渲染 + 反算文案 + 充电座缺失降级 |
| REQ-47 | S15.3 | T3 | 25696/26500 → 623；四方向反算矩阵 |
| REQ-48 | S15.3 | T3 | 迁移后 x/y 仍在；诊断属性存在 |
| REQ-49 | S15.5 | T2 | 拒绝矩阵：区内/方向非法/充电座在区外 |
| REQ-50 | S15.1 | T2 | 构造点主轴分量等于充电座 |
| REQ-51 | S15.6 | T5 | 常量 200 + 到达判定不变 |
| REQ-52 | S15.7 | T6 | 全量 + 变异 |
