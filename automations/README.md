# 仓库里的自动化与蓝图

## 三个扫地机自动化

| 文件 | HA unique_id | entity_id |
| --- | --- | --- |
| `roborock_garage_door_statemachine.yaml` | `1791392186966` | `automation.roborock_garage_door_statemachine` |
| `roborock_stuck_or_error_stop.yaml` | `1791390713215` | `automation.roborock_stuck_or_error_stop` |
| `roborock_clean_command_not_started.yaml` | `1791372324572` | `automation.roborock_clean_command_not_started` |

**这两个目录是给人看的版本管理,不是自动同步源。**

HA 那边**人工添加、人工更新** —— 在 UI 里粘贴本文件的 YAML 内容即可。
没有同步脚本,也不需要。

> **`id` 不能改。** 它是 HA 的 unique_id,改了等于换一个自动化:历史断掉、
> 实体注册表的身份丢失。2026-10-09 改成英文名时只改了 alias 和 entity_id,
> `id` 原样保留。

## 为什么不做自动同步

一开始写了双向同步脚本,后来去掉了:

- **HACS 覆盖不到这两类。** 它只支持 [AppDaemon / Dashboard / Integration /
  Python Script / Template / Theme](https://hacs.xyz/docs/publish/start/),
  **没有 blueprint 和 automation**。所以集成那套 HACS 更新流程用不上。
- **剩下的同步要么靠 SSH,要么靠 git 集成**,两者都要在 HA 上多养一套机制,
  而收益只是省几次复制粘贴 —— 三个自动化的改动频率很低。
- **同步脚本本身有风险**:`automations.yaml` 里有 109 个自动化,只有 3 个是这里的,
  写错一次会波及无关的自动化。

**结论:人工维护,仓库只负责版本记录和 review。**

## 蓝图

`blueprints/扫地机分区定时清扫.yaml` —— 分区定时清扫,支持按星期、
扫地/拖地意图、区域内有人则跳过并重试。

**蓝图走 GitHub URL 导入**,这是 HA 原生支持的路径,不需要任何凭据:

```
设置 → 自动化 → 蓝图 → 导入蓝图
https://raw.githubusercontent.com/kaattz/roborock_plus/main/blueprints/%E6%89%AB%E5%9C%B0%E6%9C%BA%E5%88%86%E5%8C%BA%E5%AE%9A%E6%97%B6%E6%B8%85%E6%89%AB.yaml
```

导入后落在 `/config/blueprints/automation/kaattz/`。

**两种 URL 都行** —— 中文原样和百分号编码都验证过,HA 都能解析。

### 改蓝图的三步

```
1. 改仓库文件,commit,push
2. 重新导入,必须带 overwrite=true
3. reload 自动化 —— 已建的实例会跟着更新,不需要重建
```

第 2 步:重新导入同一个路径会报 `RESOURCE_ALREADY_EXISTS`,要显式覆盖:

```
ha_manage_blueprints(action="import", url=..., overwrite=True)
```

或者 UI 里 `⋮ → Re-import blueprint`。

第 3 步:**更新会自动传播到已创建的自动化。** 这一点我一开始写反了 ——
以为蓝图渲染成独立副本。实际不是:自动化里存的是

```yaml
use_blueprint:
  path: my_custom/智能起夜模式.yaml
  input: { ... }
```

**只有路径和输入值,没有展开的逻辑。** 所以 HA 每次加载都重新渲染,
蓝图一改,所有实例跟着变。官方文档也确认:*"The new changes will appear
to your existing automations as well."*

反过来的代价:**不兼容的改动能改坏已运行的自动化**。文档专门警告了这点。
所以改蓝图时不要改输入的名字或含义 —— 改了会让已有实例的参数失配。

**顺序不能反:先 push,再从 URL 导入。** 反过来的话 HA 装到的是上一次
推送的版本 —— 这一点踩过一次:改了文件名却先导入了,结果 HA 里同时存在
新旧两份。

### 为什么文件名用中文

**HA 用文件名作为蓝图的显示名**,不看 YAML 里的 `name:` 字段。所以

- 文件名 `扫地机分区定时清扫.yaml` → UI 里显示「扫地机分区定时清扫」

一开始为了避开中文路径的编码问题用了 ASCII 名,代价是 UI 里显示
`roborock_cleaning_schedule` —— 一眼看不出是什么。**而且现在是从 GitHub URL
导入,不经过手工传输,当初担心的编码问题根本不存在**,所以换回中文。

改动蓝图后**重新导入一次**才会生效(HA 不会自己跟着 GitHub 更新)。

设计说明见
[2026-10-09 清扫调度蓝图设计](../docs/plans/2026-10-09-cleaning-schedule-blueprint-design.md)。

## 提交前的检查

```powershell
python scripts/validate_blueprints.py     # 蓝图结构与 YAML
python -m pytest tests -q                 # 全量测试
```

`validate_blueprints.py` 检查的是 HA 导入时的真实要求:YAML 可解析、
`!input` 有声明、`if/then` 缩进关系正确、`repeat` 有结束条件等。
**这不是多余的** —— 蓝图第一版所有 Jinja 模板都渲染正确,却被 HA 拒收,
原因是 `then:` 缩进在 `if:` 的条件列表里。只渲染模板发现不了这种结构错误。

`scripts/mutation_check_blueprint.py` 会把那个 bug 重新种回去,
确认检查能抓到 —— 否则"检查通过"没有意义。

## 凭据

仓库是**公开的**。任何需要 SSH 的脚本都从环境变量读密码:

```powershell
$env:ROBOROCK_HA_PASSWORD = '...'
```

**不要把密码写进文件。** 早期版本有三个脚本硬编码了它并推送上去;
那些值仍在 git 历史里,已通过**轮换密码**作废(比重写历史更彻底 ——
重写收不回已经公开过的值)。

`tests/test_no_committed_secrets.py` 会扫描整个仓库,阻止再出现。

剩下的 SSH 脚本(`garage_state_machine/fetch_deployed.py`、
`garage_button_cycle/verify_deployed.py`、`validate_map_block_against_library.py`)
只用于**读回 HA 里实际运行的配置并与仓库构建比对** —— 这个比对有价值,
因为"本地构建正确"和"HA 在运行正确的东西"是两个不同的断言。
它们不推送任何东西。
