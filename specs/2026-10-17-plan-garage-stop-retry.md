# 全闭停止误判与清扫重试修正

原功能：2026-10-15-req/spec-garage-control，REQ-4/5/10/12/14/15；原 S6 并发与人工接管。
蓝图配套绑定 docs/plans/2026-10-09-cleaning-schedule-blueprint-design.md 的重试窗口与最终告警，不新增日程策略。
用户确认：旧解锁自动化保持原样，只修集成误判和蓝图时间比较。上海 2026-10-17 12:55:52–13:25:52，30 分钟。
输入提交：f9bb0bf8a24690681502a65a144673860fe54108。仅本地正式代码和隔离测试，不发布、不修改 HA、不操作设备，不创建 worktree；版本保持现有 0.7.7。

## 实施前检查

- `<files>` 原 req/spec/plan、AGENTS.md、lessons.md；garage_control.py、garage_runtime.py、door_evidence.py、旧解锁 YAML、清扫蓝图及相关测试。
- `<action>` 核对外部服务归类、可信全闭与估算的区别、pending/phase/operation_id、实际蓝图模板，复用现有缓存和测试边界。
- `<verify>` 阅读实际监听入口、真实证据和原生 HA 测试；确认旧解锁用途与原 ID 不变。
- `<done>` 原因已明确：无条件 external stop 锁存 manual_override；deadline 渲染为字符串却与 datetime 比较。蓝图是确定性局部 bug，不重定义日程策略。

## T1 集成停止分类（REQ-4/5/10/14/15）

- `<files>` garage_control.py、garage_runtime.py；tests/test_garage_closed_stop.py、tests/ha_native_garage_closed_stop.py。
- `<action>` 先红后绿：只给可信全闭且无控制流程的真实 stop 增加无动作例外；运动、等待、未知/估算、冲突、恢复快照、既有故障/保持不放宽；toggle/position 不借用例外。不加配置、服务、调用者白名单或自动 reset。
- `<verify>` 双模式 idle/outside_closed、重复解锁、随后显式清扫完整开门先行；运动中停止取消旧等待且机器人不动；估算/失联/冲突/未稳定；原生 HA 实际 call_service 分发；保留旧自动化与原 stop 对照。
- `<done>` 正常解锁不锁死下一次清扫；真正人工停止/接管继续拦截；旧解锁 YAML 无改动。

## T2 蓝图时间比较（REQ-12/15 的现有家庭自动化配套修正）

- `<files>` blueprints/扫地机分区定时清扫.yaml；tests/ha_native_cleaning_retry.py。
- `<action>` deadline 与比较统一使用数值时间戳；保留首轮、窗口/间隔与原告警语义，不改变门控与重试策略。
- `<verify>` 离线真实 HA 渲染/Script：首次、零窗口、启动失败、忙/占用、后续成功和窗口结束最终告警；原代码必须能复现类型异常。
- `<done>` 时间类型异常不再截断重试或告警，原蓝图输入/身份不变。

## 分层 Review 与完成前漂移检查

- `<files>` 上述实现、测试、原 req/spec、本计划及 CHANGELOG。
- `<action>` 产品：不限制手动/电动门、不增加迁移入口；工程：独立检查停止分类和并发；UI：无界面改动；验证：先红后绿、旧路径对照、原生 HA、定向变异，适当全量回归。逐条回查需求及边界。
- `<verify>` 主机 pytest、离线原生 HA、蓝图校验及 compileall；Review 查 Bug → 第一性原理分析。
- `<done>` 报告实际通过/失败与未验收项；仅本地交付，真实人工保持仍需用户升级后显式复核，本批不解除。

## 完成记录

本批已完成本地实现，未提交/发布/部署。结果 SHA-256（按路径依次拼接两个门控模块和蓝图内容）：ef466e1b9451da7d91261c0c8aa240421b2b34c5affa61195869b010bbf8b8cd。manifest 仍为输入版本 0.7.7；旧解锁 YAML 无 diff，无新增服务/参数。

先红后绿：门控 6 failed/28 passed 复现正常全闭 stop 误触发 manual_override，最终新回归 54 passed；真实 HA Script 原蓝图 5 failed/2 passed 复现 datetime/str 异常，修后 7 passed。HA if 条件异常会记警告并视为 false，下一轮 variables 比较才抛错中止；不是 if 本身立即终止。

完成验证：主机全量 `python -m pytest -q` 为 1089 passed；最后完善估算输入后定向 54 passed。离线原生 HA 2026.9.4（当前 roborock 7.12.0/parser 0.1.5，网络关闭、仓库只读挂载）全组 75 passed；38 条既有弃用提示（aiohttp/backoff 5，蓝图正则转义 33），按规则不扩修。compileall、蓝图结构校验通过。

5 个仅内存变异均捕获：去除可信全闭、pending、运动、phase 判断，以及将 toggle/position 再映射成 stop，分别失败 9/1/1/10/8 项。没有改写产品文件来跑变异。原 contact/实测位置、旧 guard/自动化、人工保持、返航等待和完整 45 秒开门对照保留。

失败原因：首次原生出库测试漏提供坐标采样时间戳，导致尚未识别越区；补齐实际样本后 16 项新原生及最终 75 项全绿，未改产品规避。离线缓存首次缺当前 7.12.0 依赖，复用原 volume 只补缺项，验证阶段恢复断网。自检将估算反例改为先实际达到 estimated=True，再验证不能豁免，避免无效负时间输入造成假绿。

独立 Review 无 P1/P2；补齐 group/all/自身 context/无关门测试。第一性原理：释放离合不等于接管正在执行的门控流程；只按同步实时门证据区分，不推进任务状态或取消既有保持。req/spec/T25/蓝图原设计及用户手动、电动开门用途一致。经验由回归及原规格表达，不重复写 lesson。硬件验收及升级后显式复核仍由用户处理；更新集成不会替换 HA 已导入蓝图。本批实现验证约 9 分钟，流程约 4 分钟（规格同步、独立评审、交付记录），在 30 分钟内收口。
