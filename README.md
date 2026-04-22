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

## 项目状态

当前项目仍在演进中，优先目标是先把 `resume` 语义做正确，再逐步补测试和文档。

如果你只是想直接替代内置集成，这个项目**不是覆盖版**，而是**并存版**。

## 致谢

本项目基于 Home Assistant 官方 `roborock` 集成修改而来，保留其原有整体结构，并针对暂停/恢复行为做增强。
