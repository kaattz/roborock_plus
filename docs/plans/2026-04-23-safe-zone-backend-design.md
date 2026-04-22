# Roborock Plus Safe Zone Backend Design

## Goal

为 `roborock_plus` 增加安全区后端能力，让自动化不再手写 `SAFE_MIN_X/MAX_X/MIN_Y/MAX_Y`，并为后续可视化框选面板提供稳定接口。

## Scope

本阶段只做后端，不做前端编辑面板。

## Chosen Design

采用“固定 dock 参考点 + 手动参数 + 持久化安全区”的方案。

| Component | Responsibility |
| --- | --- |
| `safe_zone.py` | 安全区数据结构、建议框算法、点位判定 |
| `safe_zone_store.py` | 使用 Home Assistant storage 保存每台机器人安全区 |
| `services.py` | 注册安全区相关服务 |
| `vacuum.py` | 为 V1 真空实现安全区服务方法 |
| `binary_sensor.py` | 暴露 `has_safe_zone` 和 `in_safe_zone` |

## Key Simplification

当前集成的 `set_vacuum_goto_position` 文案明确说明：

- 坐标以基站为参考
- `x=25500, y=25500` 为基站位置

因此第一阶段后端直接将 dock 坐标视为固定值 `25500,25500`，不依赖地图图层解析。

## Service API

| Service | Purpose |
| --- | --- |
| `roborock_plus.get_dock_position` | 返回基站坐标 |
| `roborock_plus.get_safe_zone_suggestion` | 根据方向和尺寸返回建议框 |
| `roborock_plus.set_safe_zone` | 保存确认后的安全区 |
| `roborock_plus.get_safe_zone` | 返回当前安全区 |
| `roborock_plus.clear_safe_zone` | 清空安全区 |

## Safety Zone Inputs

| Field | Meaning |
| --- | --- |
| `cabinet_direction` | 机器人从基站驶出的方向 |
| `safe_distance_front` | 基站前方安全距离 |
| `safe_half_width` | 通道半宽 |
| `close_margin` | 保守余量 |

## Stored Shape

```json
{
  "duid": "device-id",
  "min_x": 24000,
  "max_x": 27000,
  "min_y": 25000,
  "max_y": 28000,
  "updated_at": "2026-04-23T00:00:00+00:00"
}
```

## State Entities

| Entity | Purpose |
| --- | --- |
| `binary_sensor.<vacuum>_has_safe_zone` | 是否已配置安全区 |
| `binary_sensor.<vacuum>_in_safe_zone` | 当前机器人位置是否在安全区内 |

## Non-goals

- 不做前端编辑器
- 不做地图图像识别
- 不做 A01/Q7/Q10 的安全区逻辑
