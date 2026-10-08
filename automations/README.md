# 仓库里的自动化与蓝图

这个仓库是**真源**;Home Assistant 是它们运行的地方。两份东西:

| 目录 | 内容 | 同步方向 |
| --- | --- | --- |
| `automations/` | 三个扫地机自动化 | 双向,各有脚本 |
| `blueprints/` | 分区定时清扫蓝图 | 复制到 HA 的 blueprints 目录 |

## 为什么不是 HACS 同步

HACS 只支持这几类:[AppDaemon / Dashboard / Integration / Python Script / Template / Theme](https://hacs.xyz/docs/publish/start/)。
**没有 blueprint,也没有 automation。** 所以集成那套 HACS 更新流程覆盖不到这两类,
必须单独同步。

## 三个自动化

| 文件 | HA unique_id | entity_id |
| --- | --- | --- |
| `roborock_garage_door_statemachine.yaml` | `1791392186966` | `automation.roborock_garage_door_statemachine` |
| `roborock_stuck_or_error_stop.yaml` | `1791390713215` | `automation.roborock_stuck_or_error_stop` |
| `roborock_clean_command_not_started.yaml` | `1791372324572` | `automation.roborock_clean_command_not_started` |

**匹配永远按 `id`,不按 alias 或 entity_id。** `id` 是自动化唯一的稳定身份;
alias 和 entity_id 都可以改而自动化还是同一个。

> **`id` 不能改。** 它是 HA 的 unique_id,改了等于换一个自动化:历史断掉、
> 实体注册表的身份丢失。2026-10-09 把这三个改成英文名时,只改了 alias 和
> entity_id,`id` 原样保留。

## 同步

两个脚本**故意分开**。合成一个"读+写"的脚本会在同一次运行里接受自己的输出,
真正的漂移就永远看不出来。

**HA → 仓库**(发现漂移):

```powershell
$env:ROBOROCK_HA_PASSWORD = '...'
python scripts/fetch_automations.py           # 只检查,报告漂移
python scripts/fetch_automations.py --write   # 接受 HA 的改动
```

**仓库 → HA**(下发改动):

```powershell
$env:ROBOROCK_HA_PASSWORD = '...'
python scripts/sync_automations.py            # 默认 dry run
python scripts/sync_automations.py --apply    # 写入 + 校验
```

写完要 reload 才生效:

```
ha_reload_core(target="automations")
```

### 为什么凭据走环境变量

**这个仓库是公开的**,而 SSH 密码曾被硬编码进三个脚本并推送上去:

```
scripts/garage_button_cycle/verify_deployed.py
scripts/garage_state_machine/fetch_deployed.py
scripts/validate_map_block_against_library.py
```

那三个仍在已推送的历史里。新脚本一律从 `ROBOROCK_HA_PASSWORD` 读,
`tests/test_no_committed_secrets.py` 会扫描全仓库,阻止再扩散。

**待办**:轮换 HA 的 SSH 密码,或重写历史。改工作区删不掉历史里的值。

## 写入 automation.yaml 的安全措施

`automations.yaml` 里有 **109 个自动化,只有 3 个属于本仓库**。所以
`sync_automations.py` 在写入前做四道检查:

1. **往返校验** —— 先确认 pyyaml 的 dump 与输入等价。不等价就拒绝写入,
   因为那意味着 dump 有损,写下去可能破坏无关的自动化。
2. **目标唯一** —— 每个 `id` 必须恰好出现一次。重复 id 会让"改哪个"变得含糊。
3. **备份** —— 写入前复制一份带时间戳的 `automations.yaml.bak-sync-*`。
4. **写后校验** —— 重新读取,逐字段比对,并确认条目总数没变。
   任何不符就**自动还原备份**。

只替换这三个条目,其余 106 个原样重新 dump,不重建。

### 为什么载荷走文件而不是命令行

约 15 KB 的 JSON 放进 argv 会超出命令行长度并被**静默截断** —— 第一次实现就
踩了这个坑。现在写到 `/config/_sync.json` 再读。

## 蓝图

`blueprints/扫地机分区定时清扫.yaml` —— 分区定时清扫,支持按星期、
扫地/拖地意图、区域内有人则跳过并重试。部署时复制到
`/config/blueprints/automation/kaattz/`。

蓝图内容与设计说明见
[2026-10-09 清扫调度蓝图设计](../docs/plans/2026-10-09-cleaning-schedule-blueprint-design.md)。
