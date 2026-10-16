# 通用车库门控制实施计划

日期：2026-10-15。状态：V1 + hold_open 已实现，按用户确认交付 v0.7.0；after_exit 隔离演练及硬件验收待完成，未部署用户 HA。
2026-10-16 增量：T12/T13 的 after_exit、先制动后授权和分步配置已完成隔离验证；用户随后指定 v0.7.1 GitHub 发布，见 T14。未部署 HA，型号实物验收待用户操作。T1–T11 保留 v0.7.0 阶段的计划及历史结果，不作为本轮模式限制。
目标：门证据和标准流程随集成交付，家庭策略通过许可/请求/事件对接。
架构：纯证据 + 单门单机控制器 + HA 接线，复用机器人状态和位置采样。
技术：Python asyncio、HA Options Flow/Store/实体服务、pytest 虚拟时钟。
依据：[req](2026-10-15-req-garage-control.md)、[spec](2026-10-15-spec-garage-control.md)、[设计](../docs/plans/2026-10-15-door-evidence-design.md)。

## 执行约束

- 用户确认规格/计划后才编码；每批由用户给时间盒，不创建 worktree。
- 保留旧路径对照，回放/变异在隔离路径，不触发真实设备；实物由用户操作。
- 下列是实施验收命令；实际结果和未完成项见文末，不以计划命令代替执行结果。
- 每次改动后执行 Review 查 Bug → 第一性原理分析，高风险检查点不过不进入下一层。
- 为避免重复长路径，下文模块文件名均相对 custom_components/roborock_plus/；tests/、scripts/、docs/ 路径从仓库根开始。

## T1 实施前检查（REQ-1、REQ-14、REQ-16）

- `<files>` 本套 req/spec/设计；AGENTS.md、lessons.md；__init__.py、coordinator.py、garage_guard.py、resume_logic.py、v1_task_state.py、safe_zone_store.py、services.py、models.py；旧门控自动化和相关测试。
- `<action>` 阅读当前 API、schema、存储、同类组件与状态表；列全受保护入口/新旧交接；确认真实 V1 只开 hold_open，核对已有变更并运行基线。
- `<verify>` `python -m pytest tests/test_garage_guard.py tests/test_garage_guard_integration_points.py tests/test_resume_logic.py tests/test_v1_task_state.py tests/test_system_composition.py -q`；逐项核对 spec 范围。
- `<done>` 基线错误如实报告；不跳过对照，不猜返航 pause 和多机器人绑定关系。

## T2 统一门证据（REQ-2、REQ-3、REQ-4、REQ-15）

- `<files>` 新增 door_evidence.py、tests/test_door_evidence.py、scripts/mutation_check_door_evidence.py；调整 garage_guard.py、door_timing.py 及相关旧测试。
- `<action>` 先约束来源组合/极性、真实位置/回声/计时、失联/冲突、反向失效、统一时钟的失败场景；再实现纯判定；旧 guard 包装适配，保留旧兼容基线。
- `<verify>` `python -m pytest tests/test_door_evidence.py tests/test_garage_guard.py tests/test_door_timing.py -q`；`python scripts/mutation_check_door_evidence.py`；`python scripts/mutation_check_garage_guard.py`。
- `<done>` V2–V4：估算不冒充确认、坏来源不降级、无输入不算到位，旧路径仍受测。

评审检查点 A：证据与失效方向 review，核对 SET_POSITION、端点非互补、稳定不等于心跳、时钟不混算。

## T3 前端界面预检查与原生配置（REQ-1、REQ-3、REQ-8、REQ-9、REQ-13）

- `<files>` config_flow.py、strings.json、translations/en.json、translations/zh-Hans.json；新增 garage_config.py、tests/test_garage_config.py；更新 tests/test_options_ui.py。
- `<ui-rules>` 原生 Options Flow；无 frontend-architect 文件；安全字段双语、显式选来源，原轮询/绘图不丢失。
- `<reference>` 编码前重读 config_flow.py 的 async_step_drawables、tests/test_options_ui.py、safe_zone_store.py 的 duid 模式。
- `<reuse>` HA EntitySelector/NumberSelector、现有 options 入口及危险区面板，不新增网页壳。
- `<action>` 完成前端预检查后实现 duid 绑定、来源/极性/估算确认、家庭许可、能力检查与交接草稿；跨字段严格校验，同门跨条目拒绝，门字段不落入 DRAWABLES。
- `<verify>` `python -m pytest tests/test_garage_config.py tests/test_options_ui.py -q`；三份翻译校验；隔离 HA 中 Options Flow 保存/重入。
- `<done>` V1/V3/V8/V9/V13：现有 V1 after_exit 拒绝，默认不接管；单机/多机/非法输入有清楚反馈。

## T4 会话与流程核心（REQ-4、REQ-6、REQ-7、REQ-8、REQ-10、REQ-11）

- `<files>` 新增 garage_task.py、garage_control.py、garage_session_store.py；新增 tests/test_garage_task.py、tests/test_garage_control.py、tests/test_garage_session_store.py。
- `<action>` 先测试本次任务关联、回洗/补电、终结/停靠分离、重复事件/请求和用户暂停；实现状态迁移、可取消等待、代次及最小会话存储，覆盖首次 known_idle/运行后会话丢失、实际 records/complete 关联、显式恢复与自动恢复的区别；独立回充/导航按 S4 分离会话类别，不用无清扫记录当永久清扫会话，不洗白未知任务；生产只开 hold_open，隔离适配器验证 after_exit 的能力拒绝及互锁契约。
- `<verify>` `python -m pytest tests/test_garage_task.py tests/test_garage_control.py tests/test_garage_session_store.py -q`；虚拟时钟下断言真实控制器命令轨迹。
- `<done>` V4/V6–V8/V10/V11：中途不收尾、旧记录不结束新任务、旧等待不复活、重启不重放，恢复只属于控制器暂停的原任务。

评审检查点 B：产品/工程 review 任务关联、返航能力、最终停靠几何，不用 task_active=off/最近结束时间代替终结。

## T5 接通所有受保护入口（REQ-5、REQ-6、REQ-7、REQ-12）

- `<files>` vacuum.py、button.py、garage_guard.py、clean_command_watch.py、coordinator.py；新增 tests/test_garage_command_paths.py；更新 tests/test_garage_guard_integration_points.py、tests/test_resume_context_integration_points.py、tests/test_clean_command_watch_wiring.py。
- `<action>` 原清扫/例程、恢复、回充、导航和已知 raw 移动统一委托；新旧配置分流；执行前重查；分类只读/未知命令；复用启动监视，恢复须观测成功。
- `<verify>` `python -m pytest tests/test_garage_command_paths.py tests/test_garage_guard_integration_points.py tests/test_resume_context_integration_points.py tests/test_clean_command_watch.py tests/test_clean_command_watch_wiring.py tests/test_resume_logic.py -q`。
- `<done>` V5–V7/V12：服务记录器逐入口证明许可先于原命令；不重复发任务；用户暂停不被续上，未启动/未恢复明确报告。

## T6 HA 生命周期、家庭接口和诊断（REQ-9、REQ-10、REQ-11、REQ-12）

- `<files>` 新增 garage_runtime.py、tests/test_garage_runtime.py、tests/test_garage_entities.py、tests/test_garage_services.py；更新 __init__.py、coordinator.py、binary_sensor.py、sensor.py、services.py、services.yaml、strings.json、translations/*、icons.json。
- `<action>` 安装可注销监听/绑定占用；新增确认/许可/阶段/故障实体、受保护关闭/只复核服务；接家庭许可，处理内部 context、人工接管、启动已有故障、unload/reload 与会话恢复。
- `<verify>` `python -m pytest tests/test_garage_runtime.py tests/test_garage_entities.py tests/test_garage_services.py tests/test_garage_session_store.py tests/test_options_ui.py -q`；隔离 HA 验证 schema、监听取消、事件次数。
- `<done>` V9–V12：不硬编码家庭实体，unknown 不当 on；许可恢复重查；卸载无动作，既有 unique_id 不变。

评审检查点 C：跨模块接线、持久化和外部输入 review；不以源码子串出现当作接通证据。

## T7 兼容迁移与使用说明（REQ-1、REQ-3、REQ-14、REQ-16）

- `<files>` README.md、automations/README.md、scripts/garage_state_machine/README.md；新增 tests/test_garage_migration.py；保留旧 YAML/JSON 和 tests/test_system_composition.py 对照。
- `<action>` 写单机/多机迁移、来源标定、显式交接、旧主动状态机停用、家庭关门改受保护请求；说明 App/厂商定时/物理边界；不自动改线上配置/原 id；文档与最终行为一致。
- `<verify>` `python -m pytest tests/test_garage_migration.py tests/test_system_composition.py tests/test_garage_guard.py -q`；旧配置/新草稿/新启用三组对照，含新绑定与另一台机器人的 legacy guard 争用同门；复核核心安装不依赖家庭 YAML。
- `<done>` V1/V3/V14/V16：升级不接管，旧路径可继续，交接不双控，不依赖 HACS 更新自动化。

## T8 回放、分层 Review 与完成前漂移检查（REQ-1–REQ-16）

- `<files>` 新增 tests/test_garage_workflow_replay.py、scripts/mutation_check_garage_control.py；本套 req/spec/plan 和本次实现文件。
- `<action>` 真控制器回放原对照与新时序：回洗/补电、自主恢复、启动失败、冲突、人工/并发、许可撤销、各阶段重启；定向变异不碰线上；分层 review 后逐条回查需求。用户实物验收前不宣称已硬件验证。
- `<verify>` `python -m pytest tests -q`；`python scripts/mutation_check_door_evidence.py`；`python scripts/mutation_check_garage_control.py`；`python scripts/mutation_check_garage_guard.py`。失败明确报告，不减少覆盖过关。
- `<done>` V1–V16 均有真实实现证据；无遗漏/未记录行为；未验证机型不进支持列表；用户验收后再 archive，不自动发布。

## 分层 Review

| 层 | 检查内容 | 检查点 |
| --- | --- | --- |
| 产品 | 默认模式、估算诚实性、家庭职责和支持范围 | B、T8 |
| 工程 | 来源、代次、会话关联、生命周期、不伤任务的恢复 | A/B/C |
| UI | 原生配置可达、极性文案、风险确认、双语与原选项保存 | T3、T8 |
| 验证 | 真代码驱动、旧路径对照、变异捕获、不调用真实设备 | A/B/C、T8 |

## 完成前漂移检查

按 req 追踪表核对 REQ-1–REQ-16 → spec → T1–T8 → V1–V16；
确认真实 V1 after_exit 未偷开、估算不冒充确认、旧自动化没被自动删改、重启不重放、
全入口无绕过、家庭规则未写进核心。实际实现和测试按下面记录核对；
未完成验收保留待执行，不降低覆盖，不 archive 为全部完成。

## 本批实施与验收记录

### T9 统一旧会话授权失效（REQ-6、REQ-11）

- `<files>` garage_task.py、tests/test_garage_transit.py、scripts/mutation_check_garage_control.py；本套 spec/plan 和 README。
- `<action>` 无受管活动会话时发现外部任务，统一撤销并持久化旧会话关联；所有授权入口共享该失效事实，不继续添加 reset 特例。
- `<verify>` 外部任务→停稳→reset、Store 往返及新意图回归先红后绿；全量 pytest、定向变异和隔离 HA 原生检查。
- `<done>` 旧导航/空闲记录不被复活，失效跨重启保留；正常受管清扫、导航、回充和用户暂停仍通过，不触发真实设备。

工作分支 `codex/garage-control`，沿用当前工作区；未创建 worktree。

| 需求 | 实现与主要验证 |
| --- | --- |
| REQ-1/8/14 | garage_config 的 duid/门所有权、显式交接、旧路径对照；真实 V1 仅 hold_open，after_exit 明确拒绝 |
| REQ-2/3/4 | door_evidence 的来源/极性/估算/冲突/失联；test_door_evidence 和定向变异 |
| REQ-5 | vacuum.send、例程按钮统一委托；test_garage_command_paths；garage_commands 明确分类固定依赖命令 |
| REQ-6/7 | garage_task/control 的本次记录关联、洗拖布/补电、稳定停靠；真实实现 workflow_replay |
| REQ-9/10 | 家庭许可、受保护关门、人工优先、操作代次、卸载取消；control/runtime/native HA 测试 |
| REQ-11 | Store 严格校验、初始化标记先行、restore 屏障、显式核对原会话；store/runtime_safety/workflow_replay |
| REQ-12 | 推送阶段/确认/许可/故障实体、去重事件、未启动/未恢复诊断；entities/services/native HA |
| REQ-13 | 原生 Options Flow、三份翻译、字段分离、原选项保留；options_ui/config/native HA |
| REQ-15/16 | 内存回放、临时副本变异、Linux HA 原生导入/表单/服务/流程；README 和迁移说明 |

Review 已修复：洗拖布准备误报启动超时、恢复误认充电为成功、冲突前先发门命令、
Store 恢复前被 tick 覆盖、初始化标记写入次序、物理/组关门识别、状态 RPC 采样时点、
会话重启后永远不能复核及真实 resume 入口上下文、HA 同步监听误入线程池、
实体注册表重命名和删除后同名重建保护、慢地图消费导致清扫记录关联窗口被扩大，
独立回充/导航误当清扫永久占用会话，
以及 legacy guard 的 epoch/monotonic 混算。旧 95/45/90 判据保留为兼容对照；
新 binding 使用独立证据模型，未把旧用户自动迁移进新判据。
独立 Review 后检查失败方向：不自动恢复未知会话、不把估算变确认、不用旧记录收尾。

待关闭事项（保留原验收要求）：

- T4：合格隔离适配器的 after_exit 完整互锁演练尚未实现；生产仍拒绝，不算该项通过。
- 各硬件的行程、极性、防夹和完整物理流程由用户验收；本批未触发真实设备。

本批最终主机全量 `python -m pytest tests -q`：829 passed；原生 HA：14 passed。
临时副本变异：证据 12/12、控制器/会话 17/17、旧 guard 8/8；均先跑原始对照。
`compileall` 通过。HA 原生检查有 5 条上游 HTTP/backoff 弃用提示，按规则记录并跳过。
这些结果不代表上面的待关闭事项已通过；没有删除旧对照或降低覆盖。

本批使用用户授权的 60 分钟时间盒，约 50 分钟收口到规则确认点；
实现/修补/测试约 40 分钟，流程约 10 分钟，主要是文档回查、独立 Review、隔离 HA 环境准备。

原生检查使用 Linux、Python 3.14.2、Home Assistant 2026.9.4、python-roborock 5.5.1、
vacuum-map-parser-roborock 0.1.4 和 pytest，运行 `python -m pytest tests/ha_native_garage.py -q`。
覆盖真实 HA 导入、原生表单/服务、事件线程、Registry 引用和内存清扫/回充/导航闭环；
未加载账号/连接设备，也未声称完成浏览器显示或硬件验收。
静态检查另有 9 项非阻断的导入排序/未用导入/风格提示，按 AGENTS 记一行跳过；不声称 lint 全绿。

## 续批：统一关联失效

用户确认继续，并再次明确不触发真实设备；使用上批剩余约 10 分钟。
根因是 ended/reason 同时表达历史完成与当前授权；现在统一以持久化 correlation_valid
维护旧会话关联，时钟异常和外部未知任务均撤销该事实。can_begin/can_close/reset/restore
共用这一失效事实，控制器统一投影故障，不增加 reset 特例，不改变 Store schema。
7 个授权/重启回归先失败后通过，另补故障可见与去重回归；保留正常清扫/回充/导航对照。
Review → 第一性原理：复用一个持久化关联位，不再增加可相互矛盾的终点授权条件。
续批全量 pytest：838 passed；隔离原生 HA：16 passed（含持久化/重启后旧授权仍被拒绝、故障实体可见）。
compileall 通过；原生环境仅原有 5 条上游弃用提示。
扩展变异首次 18/19：统一故障检查遮住了“后台 tick 前新请求”的竞争场景；补真实时序回归，保留全部变异复跑，最终 19/19 全部捕获。原证据 12/12 与旧 guard 8/8 的被测实现本批未改。
本次约 9 分钟：实现与验证约 7 分钟，流程约 2 分钟，主要用于规格同步、独立评审和结果回查；未触发真实设备、未部署。

## T10 整理候选交付（REQ-13、REQ-14、REQ-16）

- `<files>` docs/garage-control-checklist.md、README.md、本计划；当前 feature 实现及测试。
- `<action>` 补一页配置/迁移/验收清单，解释标定与仓库旧门控，保留现场待验收状态；以本地 Git 提交固定候选代码，不推送/部署/发布。
- `<verify>` 回查 spec/原生字段/旧自动化名称，检查文档链接；重新运行全量 pytest，提交后确认候选提交包含清单与实现。
- `<done>` 用户可按一个提交定位完整候选版本，清单明确谁操作设备、什么结果才算通过，无虚构现场测量/验收结果。

交付复核：清单 42 行，引用路径存在；全量 pytest 838 passed。Review 核对家庭许可仅限制关闭、最终停靠几何与旧自动化来源；保留一页清单，不重复整套规格。候选版本以包含本页的本地提交固定，未推送/部署/发布，现场验收仍待用户完成。

## T11 发布前修正与 v0.7.0 交付（REQ-5、REQ-6、REQ-10、REQ-14）

- `<files>` garage_control.py、garage_runtime.py、garage_task.py、tests/test_garage_runtime_safety.py、scripts/mutation_check_garage_control.py；manifest.json、本套文档及 CHANGELOG.md。
- `<action>` 修复用户暂停/停止未撤销等待启动、活动导航转显式回充未收尾两项实现偏离；清扫回充保留原关联。版本按用户确认升级 v0.7.0，推送、合并并发布 GitHub Release，不部署 HA。
- `<verify>` 两项回归先红后绿；全量 pytest、原生 HA 隔离检查、控制变异；独立评审修正；核对远端合并、标签、manifest 与 Release。
- `<done>` 旧启动不在用户停止/暂停后补发；导航显式回充按独立返航终点收尾，清扫不提前结束；发布明确 V1 + hold_open、默认关闭与现场验收未完成。

发布前验证：全量 pytest 849 passed；原生 HA 2026.9.4 隔离检查 16 passed（仅 5 条上游弃用提示）；运行时安全回归 26 passed；compileall 通过。控制变异最终 22/22 全部捕获，保留既有对照；中途 20/21 暴露变异目标因新分支错位，已拆分新任务/导航回充保存点并复跑。未量测覆盖率百分比，最低 HA 2026.8.0 未逐版验证。
分层复审：两个具体实现遗漏均先红后绿，定向独立复审无新增 P1/P2；文档 24 个相对链接有效。漂移检查仍仅交付 V1 + hold_open，不把 after_exit 演练和物理验收标记完成。发布目标为 [v0.7.0](https://github.com/kaattz/roborock_plus/releases/tag/v0.7.0)，推送、合并、标签与 manifest 的远端一致性在发布后核对；不部署用户 HA。

## T12 补齐旧离库关门流程（2026-10-16，REQ-4、REQ-6、REQ-7、REQ-8、REQ-10）

- `<files>` 原 req/spec/设计；garage_config.py、garage_control.py、garage_runtime.py、garage_task.py、config_flow.py 及对应翻译；tests/test_garage_after_exit.py、tests/test_garage_after_exit_review.py、tests/test_garage_after_exit_runtime.py、tests/ha_native_garage_after_exit_runtime.py、tests/ha_native_garage.py 及原 guard/会话/回放对照，控制变异脚本。
- `<action>` 已复核旧 PHASE_LEAVE/RETURN/PARK；按用户随后明确的 S5.1 实现出库无额外暂停、受信区外关门；返航必须暂停确认→开门确认→app_resume 原返航。补控制器代次/暂停所有权、返航抢占和原生配置；不只删除 unsupported 校验，不复制本屋常量。
- `<verify>` 新场景先红后绿：首次/再次离库、回洗/补电停靠及自主继续、真正结束、重复/失联样本、用户暂停、恢复失败、慢门/靠门返航和关门中返航；保留旧自动化及 hold_open 对照，运行相关全量/原生 HA/变异，所有 I/O 均隔离。
- `<done>` 实际命令轨迹符合 S5.1，原任务不因返航 pause/app_charge 被破坏，未验证能力不能在 UI 中冒充可用；同一控制器处理两种模式，且不触发真实设备。

时间盒：用户确认 60 分钟，开始 2026-10-16 00:35:19（上海），01:35:19 截止；后续要求合入同盒，不延长。内部代码/隔离验证授权有效，不部署、不触发真实设备。历史 v0.7.0 结果不改写为本轮成果。

## T13 原生配置三步减负（REQ-13）

- `<files>` config_flow.py、garage_config.py、strings.json 与 en/zh-Hans；tests/test_options_ui.py、tests/test_garage_config.py、原生 HA 表单测试。
- `<action>` 首屏仅基本来源选择；随后只展示所选反馈和对应时长，沿用原生选择器；旧值合并保留，未选来源不要求无用字段，不猜门行程/极性。
- `<verify>` 真实 Options 方法与 schema 保存/重入/切换来源、未启用草稿、已提供非法值及缺失必填的回归先红后绿；Linux 原生 HA 验证。
- `<done>` 不再首屏铺全部参数，反馈不相关项不可见；所有选中来源的安全校验及旧配置兼容仍完整。

根因与修正：返航开始前已有取消或其它任务记录时，旧校验先报错而跳过 app_pause。用户确认换路线后，先确认新鲜返航并制动，观察暂停后开门；初始及持续原任务关联仅授权自动恢复，初始失败不可被后续恢复洗白。
6 个制动/授权场景先红后绿，覆盖坏记录、记录缺失/格式异常、后续恢复及关联时钟失效。最终主机全量 949 passed，Linux 原生 HA 2026.9.4 共 21 passed，compileall 通过；原生仅 5 条既有上游弃用提示。独立聚焦复审 111 passed，无新增 P1/P2。
HA 只读核对：两条分区任务使用同一蓝图，开门由旧 garage_guard，离库关门由旧状态机；未触发设备、未修改远端配置。
Review → 第一性原理：制动保护与恢复授权职责分开，复用原会话基线，不增加自动回充代偿或 STOP 依赖。漂移核对 REQ-8/REQ-13、S5.1/S5.2 与本地实现一致；原生表单覆盖 contact/timed 和重入/切换，位置来源由主机测试覆盖，未声称原生全组合或最低 HA 2026.8 逐版验证。
保留缺口：型号物理暂停/保持/恢复、门行程/极性/防夹及整场实物验收仍由用户完成，未 archive 为全部验收完成。仓库未提供 scripts/check-lessons.mjs，未执行该脚本，不为此扩大改动。
安全变异：原 34 项 34/34 捕获，新增“初始关联失败被后续好记录洗白”单项 1/1 捕获，合计 35/35；两轮均先跑原始对照，保留全部旧项，无覆盖降级。文档本地链接 26/26 有效，计划 202 行左右，未超过 300 行。
本地候选以包含本段的提交固定，manifest 仍为已发布的 0.7.0，本轮改动标为未发布；不推送、不部署、不操作设备。本批实现/验证约 33 分钟，流程约 7 分钟（规格同步、独立评审、HA 配置核对），另等待路线确认约 12 分钟，在原 60 分钟时间盒内收口。

## T14 用户指定发布 v0.7.1（2026-10-16）

- `<files>` manifest.json、CHANGELOG.md、README.md、交接清单、本 feature 文档。
- `<action>` 按用户明确指定升级 0.7.1，合并最新 origin/main，复核并推送分支，经 PR 合并到 main 后发布 v0.7.1；不部署 HA、不启用绑定。
- `<verify>` 合并后重跑全量 pytest，独立发布复审；核对 GitHub main、tag、manifest 和 Release；保留 T12 的原生/变异结果及实物验收缺口。
- `<done>` GitHub 可下载包含离库流程与配置减负的 v0.7.1，版本和说明一致，新控制默认关闭，未操作设备。发布结果以 GitHub Release 及 PR 记录为准。

## T15 辅助默认值与提交错误可见（REQ-13，2026-10-16）

- `<files>` garage_config.py、config_flow.py、strings/en/zh-Hans；tests/test_garage_form_defaults.py、tests/test_garage_form_feedback.py、tests/test_garage_form_review.py、tests/ha_native_garage_form.py及原配置/表单对照；原req/spec/设计/清单/CHANGELOG。
- `<action>` 仅在UI给缺失辅助项预填S5.2建议值，不覆盖旧值或猜测行程；二/三步标必填，业务错误同时在顶部与字段显示，保留用户输入和原校验。沿用三步，不新增页面或持久化结构。
- `<verify>` 缺dock/21秒超时/交接失败顶部提示先红后绿；默认值、未用来源、保留0/非法值/草稿、真实行程和确认框边界；主机全量与离线原生HA schema/保存测试，独立Review后核对REQ-13漂移。
- `<done>` 新配置通常只手填真实行程并确认交接；不合法提交有可见中文提示，可纠正后保存；不操作真实设备，不发布或部署。

时间盒：用户确认30分钟，起点2026-10-16 01:57:32（上海），02:27:32截止。此批仅本地修复/隔离验证。v0.7.1旧翻译显示此前已由用户强刷确认解决，不把缓存问题写成新代码缺陷。

T15结果：默认值及顶部错误7个场景先红后绿，超时数字提示单项先红后绿；独立Review补双向计时单侧未知2个先红后绿。最终主机全量965 passed；离线原生HA 2026.9.4共22 passed，覆盖Required/schema自动填值、35+10与21的顶部/字段提示、中文占位符和纠正后保存；compileall通过。原生仅5条既有上游弃用提示。无浏览器或实物验收声明，无新增P1/P2。
Review → 第一性原理：辅助建议仅作用UI、业务规则严格拒绝缺项；同一端点等待公式用于校验/建议/错误数值，未知另一方向不抹掉已知较大等待。旧值及源选择保留，草稿可存，真实行程和确认仍由用户提供；REQ-13/S5.2/T15一致，不改Store或门/机器人状态流。
本批约14分钟：实现与验证约10分钟，流程约4分钟（复现、规格同步、独立评审），在30分钟内提前收口。以包含本段的本地提交固定，未推送、未发布、未部署，未触发设备；v0.7.1已发布标签保持原样。

## T16 用户指定发布 v0.7.2（2026-10-16）

- `<files>` manifest.json、CHANGELOG.md、README.md、清单、本feature文档。
- `<action>` 按用户指定升级0.7.2，合并最新origin/main；提交推送分支，经PR合并后发布GitHub Release，不部署HA或操作设备。
- `<verify>` 合并后全量pytest与离线原生HA表单检查、独立发布Review；核对远端main、tag、manifest和正式Release一致。
- `<done>` GitHub的v0.7.2可下载T15修复，发布说明与配置行为相符，默认关闭、实物验收边界保留；发布结果以PR/Release记录为准。

## T17 配置完成保存的重载冲突（REQ-13，2026-10-16）

- `<files>` config_flow.py、tests/test_options_ui.py、tests/ha_native_options_finish.py；原spec、本计划、CHANGELOG。
- `<action>` 修复 OptionsFlowWithReload 与既有配置更新监听器冲突；改用普通 OptionsFlow，由原监听器统一重载，不改变配置数据、安全校验或设备状态流。
- `<verify>` 真实 HA 2026.9.4 OptionsFlowManager 完成保存先红后绿，覆盖轮询/绘图/车库及未启用草稿；断言 options 保存且变更只重载一次、无变更及仅初始化元数据不重载；主机全量、离线原生测试、独立 Review。
- `<done>` 本地回归不再抛出 update listeners/OptionsFlowWithReload 冲突，保留原配置更新语义；不部署、不发布、不操作设备，用户现场保存仍待升级后确认。

T17结果：只读现场日志定位 HA 拒绝 OptionsFlowWithReload 与 update_listeners 并用。新增3条真实完成流程回归全部先复现同一异常，再修复为通过；检查选项与旧绑定保留、变更1次重载、同值0次及初始化元数据仅保存不重载。主机965 passed，离线原生HA 2026.9.4共25 passed（仅5条既有上游弃用提示），compileall通过；独立Review无新增P1/P2。
Review → 第一性原理：只保留原监听器这一处重载职责，不捕获或隐藏HA异常，不改变程序化配置更新、安全校验或设备状态流；REQ-13/S5.2一致。此前原生测试只验证到create_entry返回值，现补实际finish及listener边界；可用回归表达，不另堆lesson。lessons/by-topic目录缺失，相关关键词检索未执行；不扩大修复范围。
本批实现与验证约3分钟，流程约4分钟（只读日志定位、测试边界复核、文档及独立评审）。本地提交固定修复，未发布、未部署，未触发设备，现场保存仍待升级后由用户确认。

## T18 用户指定发布 v0.7.3（2026-10-16）

- `<files>` manifest.json、CHANGELOG.md、README.md、交接清单、本feature文档。
- `<action>` 按用户指定升级0.7.3，合并最新origin/main；提交推送分支，经PR合并后发布GitHub Release，不部署HA、不操作设备。
- `<verify>` 合并后主机全量和离线原生HA完成保存回归、独立覆盖及发布Review；核对远端main、tag、manifest和正式Release一致。
- `<done>` GitHub可下载包含T17提交修复的v0.7.3，发布说明与实际范围一致；默认关闭及现场验收边界保留，现场保存待用户升级后确认。

发布前验证：合并origin/main后主机965 passed，离线原生HA 2026.9.4共25 passed（5条既有上游弃用提示），compileall通过。独立覆盖审计确认5个变更行为场景均有真实manager/listener回归，此为场景审计而非实测行/分支覆盖率；独立发布Review无P1/P2，T17已完成，T18推送/合并/发布以远端记录为准。无新增产品行为或未记录范围漂移，现场保存及物理验收仍待用户完成。

## T19 首屏原生分组与旧入口收纳（REQ-13、REQ-14，2026-10-16）

- `<files>` config_flow.py、strings/en/zh-Hans；tests/test_options_ui.py、tests/test_options_sections.py、原生HA选项回归；本套req/spec/plan、原设计及CHANGELOG。
- `<action>` 保持同页提交，新车库入口置顶；监测展开，地图和旧版兼容折叠；原生section输入还原原options结构，保留旧平铺提交、旧保护和值，不自动迁移；补齐中文地图文案及错误回显。
- `<verify>` 分组/折叠及中文资源先红后绿；旧值/0/False、无分组键落盘、冲突回显、普通保存/新车库跳转；真实HA schema序列化及OptionsFlowManager完成保存、单次重载；主机全量、原生隔离与独立Review，回查REQ-13/14漂移。
- `<done>` 用户可分清新旧门控并在同页提交，旧保护仍可配置，已有监测/地图/车库选项不丢失，无生产mock、不操作设备、不发布。

时间盒：用户确认30分钟，起点2026-10-16 09:22:32（上海），09:52:32截止；仅本地实现/隔离验证。用户已同意上轮建议，本次为原feature质量补充，不新开feature。

T19结果：主机5条分组/保存/回显回归、原生HA3条section/schema/finish回归先红后绿；补组内翻译路径回归，主机全量971 passed，离线原生HA 2026.9.4共28 passed（仅5条既有上游弃用提示），compileall通过。原生序列化确认expandable与折叠状态；普通/车库/草稿完成保存、旧平铺提交、旧值、0/False、冲突拒绝和单次重载均保留。独立Review无P1/P2；无浏览器现场或实物验收声明。
Review → 第一性原理：用原生section保留同页提交，只在边界还原原格式；不迁移配置、不自动停用旧保护、不改变门/机器人流程。字段中文按官方frontend分组路径补齐，测试覆盖分组内翻译查找；REQ-13/14、S5.2及原设计一致。可测试表达的分组/回显经验保留在回归中，不另堆lesson。
本批实现与验证约5分钟，流程约3分钟（原生API核对、原计划补充、独立评审），在30分钟内提前收口；本地提交固定，manifest保持0.7.3，未推送、未发布、未部署、未触发设备。
