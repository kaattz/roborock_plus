# 车库反馈恢复修正（原车库控制功能 T23）

关联：[req](2026-10-15-req-garage-control.md) REQ-4/5/10/11/15；[spec](2026-10-15-spec-garage-control.md) S3/S4/S6。
用户同意修正新请求与旧计时的区分；2026-10-16 23:17:39–23:47:39（上海），30分钟。仅本地实现及隔离验证，不发布、不访问或操作真实设备，不新建worktree。

## 实施任务

- `<files>` door_evidence.py、garage_control.py；tests/test_garage_feedback_recovery.py、tests/ha_native_garage_feedback_recovery.py；原req/spec/plan、本计划和CHANGELOG。
- `<action>` 先用真实证据/控制器回放“cover不可用→恢复→新清扫请求”并观察红灯；分开当前来源校验与历史命令失效原因，新的开门命令重新等待35+10秒，旧等待不能继续；已锁存故障及人工保持仍需显式复核，reset无设备动作，活动会话无许可时复核后保持manual_hold等待明确请求，不放宽会话/启动/卸载限制。
- `<verify>` 新请求、无请求只恢复、主动等待中失联、旧计时作废、reset只复核、人工保持、当前cover/contact/position失效、冲突、restored、时钟和会话恢复拒绝；定向红绿、主机全量、离线原生HA、定向变异和独立Review。保留原contact/measured及故障对照，不调用HA服务。
- `<done>` 历史旧标记不再误拦显式新请求，45秒之前无清扫命令；恢复和reset均无自动动作，真实故障/人工保持未绕过。文档和实现一致，固定本地提交，manifest保持0.7.5。

## 诊断依据

当日12:00:13门实体不可用，12:00:35恢复；12:00:37控制器idle但计时开位仍feedback_unavailable，12:59:35请求在开门预检被拒。读取历史和真实trace已确认；本批只用隔离输入回放，不再次查询或操作设备。

## 完成记录

已完成本批代码交付：主机1030 passed，离线HA 2026.9.4原生52 passed（仅5条既有上游弃用提示），compileall通过。新增26个纯回归与6个原生场景，覆盖timed/estimated-position、两种任务模式、运行及持久化恢复的原会话，明确请求均重新等35+10=45秒；contact/measured及旧门控对照未削弱。
先红后绿：6个idle新请求/reset场景暴露历史原因误拦；独立评审发现活动暂停会话reset后下一tick重新锁死，4例先红后绿并扩展为8个原会话/持久化恢复组合。修复只调整证据的command_required、估算等待代次检查及复核后的明确请求等待，未变更存储或HA接口。
失败记录：增强暂停回放时最后的cleaning观测漏填in_cleaning=1，导致保持awaiting_start；修正测试输入后26例通过，未修改产品以绕过实际启动观测。
定向变异在内存中执行、不改产品文件：恢复旧故障传播、移除旧等待代次检查、跳过完整行程、移除活动复核等待，分别被6/2/4/8例捕获。实际未请求、恢复反馈和显式reset均无自动门/机器人动作，人工保持及真实来源错误继续拒绝。
Review查Bug → 第一性原理：当前来源可用不等于已有有效行程，故障复核不等于恢复任务授权；复用既有command/operation_id/manual_hold，无新增状态或配置。原P2已消除，最终独立评审无P1/P2；REQ-4/5/10/11/15与S3/S4/S6一致。经验已固化为回归，不重复新增lesson；未宣称现场硬件验收或整项功能全部完成。
实现与验证约11分钟，流程约6分钟（规格同步、边界评审、交付记录），30分钟内收口；仅本地提交，manifest仍0.7.5，未推送/发布/部署，未操作真实设备。


## T24 用户指定发布 v0.7.6

- `<files>` manifest.json、CHANGELOG.md、README.md、配置验收清单、本计划。
- `<action>` 合并最新origin/main，按用户指定升0.7.6；完成鲜验证及发布评审后提交、推送，经PR合并并创建正式GitHub Release；不部署HA、不操作设备。
- `<verify>` 合并后主机全量/离线原生HA/compileall，独立覆盖与计划审计、错误时序挑战；核对远端main、tag、manifest与Release一致。
- `<done>` GitHub可下载T23反馈恢复修复，真实故障/人工保持不自动解除；使用本门35+10秒作为验收示例而非通用硬件默认值，现场验收仍由用户操作。

发布前鲜验证：合并origin/main后主机1030 passed、离线原生HA 52 passed（5条既有上游弃用提示）、compileall通过。独立AI场景审计6/6有回归（非实测行/分支率）；两遍发布Review、16组开关门错误时序挑战及111项独立回归通过，无P1/P2。原本地实施完成，远端交付待PR/Release记录核实，未宣称实物验收。
