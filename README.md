# Roborock Plus

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

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
- 新增柜门保护：HA 发起的清扫命令会先打开柜门并等门完全打开
- 可配置本地状态轮询间隔（1–60 秒），只刷新状态类实体
- 支持填写自建服务器地址（区域选择 `Manual`），可对接 [local_roborock_server](https://github.com/Python-roborock/local_roborock_server)

## 柜门保护

扫地机停在柜子里时，需要先开门才能出 dock。`Roborock Plus` 会拦截 HA 发起的清扫命令：

- 覆盖 `vacuum.start`、房间清扫、`vacuum.send_command` 和清洁例程按钮
- 调用配置的 cover，等待 `current_position >= 95` 才下发扫地机命令
- 只影响从 Home Assistant 发起的命令；Roborock App 发起的命令不经过 HA，无法拦截

关门需要配合自动化：等扫地机离开危险区后再暂停、关门、恢复任务。相关状态由集成提供：

| 实体 | 作用 |
| --- | --- |
| `binary_sensor.<vacuum>_task_active` | 任务是否仍然存在（暂停也算在） |
| `binary_sensor.<vacuum>_clear_of_garage` | 是否已离开危险区 |
| `binary_sensor.<vacuum>_in_safe_zone` | 是否位于危险区 |
| `binary_sensor.<vacuum>_safe_zone_configured` | 是否已配置危险区 |

> 危险区只表示位置，不能代替 `returning_home` 判断回基站意图；否则普通清扫靠近柜门时会误触发。

## 对接自建服务器

在集成配置的区域下拉里选择 `Manual`，填入你的服务器地址（例如 `https://api-rr.example.com:555`），
然后按 [local_roborock_server 文档](https://python-roborock.github.io/local_roborock_server/) 填写本地登录邮箱和 6 位 PIN 作为验证码。

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
custom_components/roborock_plus/
hacs.json
README.md
```

这个结构符合 HACS 对自定义集成仓库的基本要求。

## 兼容性说明

- Home Assistant：见 `hacs.json` 和 `manifest.json`
- 安装方式：HACS 自定义仓库 / 手动复制
- 集成类型：`integration`

## 开发

运行测试：

```bash
python -m pytest tests -q
```

测试覆盖恢复命令选择、危险区几何、柜门保护、轮询策略和翻译文件完整性。

## 项目状态

当前项目仍在演进中。已经完成的主线是恢复语义、危险区/柜门保护和状态轮询；
接下来会继续补文档和自动化示例。

如果你只是想直接替代内置集成，这个项目**不是覆盖版**，而是**并存版**。

## 致谢

本项目基于 Home Assistant 官方 `roborock` 集成修改而来，保留其原有整体结构，并针对暂停/恢复行为做增强。
