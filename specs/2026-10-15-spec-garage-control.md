# 通用车库门控制规格

日期：2026-10-15。状态：V1 + hold_open 已实现，v0.7.0 发布范围和验收缺口见 plan；after_exit 未开放。
2026-10-16 追加：v0.7.1 补齐离库后关门及三步配置，最新要求见 S5.1/S5.2；v0.7.0 标签仍为旧范围，本轮仅 GitHub 发布，未部署 HA。
依据：[req](2026-10-15-req-garage-control.md)、[设计](../docs/plans/2026-10-15-door-evidence-design.md)。

## S1 架构与支持范围

```
原 cover / 端点 → DoorEvidence → GarageController ← RobotTaskSession / 受信位置
                                     ↑             ↓
                          家庭关门许可/请求     阶段实体、故障事件
```

`door_evidence.py` 负责纯判定，`garage_control.py` 负责单门单机调度，
`garage_runtime.py` 负责 HA 订阅/服务执行；不复制另一套 YAML 状态机。
`garage_guard.py` 保留旧入口，新 binding 只委托同一控制器，不双重等待/发命令。
复用 coordinator 状态/地图采样、危险区存储、resume_logic、启动确认监听。

v0.7.0 的生产范围是 V1 + hold_open；本轮本地实现新增 after_exit，按 S5.1 每次观测
实际暂停、门通行与原返航恢复，不以一个“支持 pause”勾选或服务回执冒充成功。
内部 transport 只接受 app_pause/app_resume，无此适配时明确 unsupported_return_interlock。
静态接口/隔离回放不替代具体型号的硬件任务保持、制动和门防夹验收。
Q7/Q10/A01 与多机共门明确拒绝；不同门可分别绑定不同 V1。

## S2 数据与配置

新 options：`garage_control_version=1`、`garage_bindings`（duid → binding）；
旧 `garage_guard_enabled`、`garage_door_entity_id` 保留。binding 字段如下。

| 字段 | 值/默认 | 校验 |
| --- | --- | --- |
| enabled / cover_entity_id | false / 必选 cover | OPEN/CLOSE 可用；同门跨绑定/条目仅一名管理者 |
| task_door_mode | hold_open / after_exit，默认 hold_open | after_exit 执行 S5.1 返航暂停/开门/恢复的实际观测互锁 |
| open_source / close_source | contact、position、timed；首次无隐式选择 | 各方向独立，不按临时可用性切换 |
| open_contact / close_contact | 各方向可选 binary_sensor | 两端点不能同实体；不按 device_class 排除合法门磁 |
| open_contact_state / close_contact_state | 配触点时必选 on/off | 不默认猜极性 |
| position_kind | measured / estimated | position 来源必选；不得由 SET_POSITION 推导 |
| open_position / close_position | 旧值建议 95 / 5 | 0 ≤ close < open ≤ 100；低开位/高关位提示实测确认 |
| open_travel_seconds / close_travel_seconds | 新绑定无通用值；旧值建议 45 | 1–600；估算必填完整行程 |
| travel_margin_seconds | 新绑定需确认 | 0–120；不把旧 45 秒再重复叠默认余量 |
| contact_settle_seconds / position_settle_seconds | 新绑定需确认 | 0–60；本屋全关 8 秒不当全开通用值 |
| operation_timeout_seconds | 新绑定需确认；旧值建议 90 | 1–1800，须大于所选来源最长证据等待；估算含行程+余量+稳定期 |
| dock_settle_seconds | 新绑定需确认，1–300 | 至少两次成功状态样本跨越窗口；重复旧样本不累计稳定期 |
| allow_estimated | false | timed/estimated position 须明确接受风险 |
| close_permission_entity | 可空，input_boolean/binary_sensor | 只读；配置后非 on 均否；空表示无家庭附加限制 |
| handover_confirmed | false | S9 交接后用户明确确认 |

可保存未完整配置的草稿，不能 enabled；现场离线可保存但不授予许可。
NaN、bool 冒充数值、极性非法、阈值倒置等显式报错，不 clamp 安全参数。
实体引用跟踪注册表重命名；删除/失联变未知，不自动选同名实体。

`DoorEvidence`：open/close 到位三值、quality（confirmed/estimated/unknown）、source、
reason、方向和 operation_id。false 是有效反馈未到位，None 是无法判断；两个方向不互为取反。
`passage_allowed` 是策略结果，不是物理事实。控制器保存 phase、session_id、operation_id、
暂停所有者和当前故障；期限使用同一 monotonic 时钟，UTC 仅审计/记录关联，不混算。

## S3 门证据规则

1. contact：按所选触点极性和稳定期确认；未到位不退 position。两个有效端点同时到位
   为 endpoint_conflict，全局撤销许可；两者均未到位可以是中间态。
2. measured position：有效有限 0–100、来源可用且非恢复旧值，满足阈值和稳定期才确认。
   cover 的 open 不自动等于全开，SET_POSITION 仅表示支持设置位置。
3. estimated position：本次同方向命令接受后，完整行程+余量经过且满足阈值才 estimated。
   时间加目标回声不能排除卡门，不产生 confirmed。
4. timed：cover 可用、命令已接受、完整行程+余量经过且无 stop/反向/人工失效事件，
   才 estimated；没有本次有效命令不能仅凭“HA 已运行很久”估算到位。
5. 所选来源 unavailable/unknown、动作失败、冲突不降级；确认/估算随事实变化撤销。
   端点久未变化不等于陈旧，不用 last_changed 当心跳；位置复用来源有效性边界。
6. 启动不信 restored 快照；等待原提供方可用且提供有效观测。重报相同目标值、服务成功
   都不是新的物理确认；稳定期不能替代来源可信度。
7. 监听门的外部 open/close/stop/set_position 和状态；内部动作用 context/operation_id
   区分，不能把自己发的动作认成手动。纯计时观测不到的物理移动是已知限制。
8. 可信端点与位置回声不同，仍按选定端点判断并展示偏差，不要求二者同时满足。
   单端点粘死、错误标定和通路障碍仍可能失效，不承诺检测所有物理故障。
9. 区分当前来源不可用与旧命令已撤销：来源恢复后，没有有效同方向命令的估算端点仍为unknown/command_required，不授予许可，也不把历史故障冒充当前来源失联。新请求须接受新的门命令并重新等完整行程+余量；旧等待不得继续或复活。控制器已锁存的故障、人工保持和会话恢复要求不随来源恢复自动解除；显式reset只复核、不发门/机器人命令，不复用旧计时。活动会话复核通过但尚无许可时维持manual_hold，后台tick不重新锁成失联、不自动动作；只有新的明确恢复请求重新申请通行，保持原session_id。

通行许可：当前开位 confirmed，或 allow_estimated 且开位 estimated；同时 cover 可用、
无冲突/人工保持/阻塞故障。实际移动命令发出前再次检查，而非等待一结束就永久授权。

## S4 机器人适配与本次任务

`garage_task.py` 消费 coordinator.data.status、clean_summary 和受信位置，
不读取本屋 sensor.sao_di_ji_v2_timestamp_2 等用户命名实体。
会话在本次 HA 请求准备时创建，命令前记设备记录/计数基线；发送后须观测与本次请求
关联的任务开始。原会话活跃时重复 clean 请求拒绝，不排队启动第二场。
暂停、回洗、补电、装卸拖布保留会话；task_active=off 不使会话终结。

终结关联使用现有依赖的实际字段，不假设设备有尚未接入的 current_task_id：

1. 请求前成功刷新并保存 clean_summary.records、clean_count、last_clean_record 的
   begin/end 基线；记录本次发送 UTC 秒值和首次观测本次实际清扫的 UTC 秒值。
2. 候选记录 ID 必须不在基线 records 中，clean_count 必须增加；begin 须落在本次
   发送至首次实际清扫观测窗口，按设备整数秒精度比较。请求前已开始但请求后才
   延迟上报的旧记录不能归入本会话；多候选/计数跳变不猜选。
3. 候选 end >= begin 且不在未来、complete 明确为 1；finish_reason 若与成功终结
   冲突则拒绝。只有 begin/end 的旧格式不能单凭时间当完成。时钟无法对齐、字段
   缺失、分段记录无法证明整场终结时均不自动关门。
4. 候选还须稳定停靠、无待续任务和真实 charger 几何自洽；回洗/补电不能产生完整
   终结令牌。显式取消另用本会话停止请求及取消后新鲜状态确认，不把调用成功当取消
   成功。错误/未知 finish_reason 不用裸数猜语义。

无法关联/读取失败/无记录的清扫命令保持 task_end_unconfirmed，不自动关门。
实施须覆盖上一场迟到、分段回洗记录及整数秒边界。稳定停靠至少两次成功状态样本
跨越 dock_settle_seconds，处于明确停靠白名单并通过位置自洽；未知/失败重置稳定期。
危险区包含基站，区内不等同穿门或任务结束。

`garage_session_store.py` 用 HA Store，版本 1，保存 duid、session_id、已观测任务标识、
记录基线、终结/不确定状态。只存核对所需信息，不存可重放命令、期限或通行许可。
首次启用尚无 Store 是合法初始化：交接时用户确认无待续任务，且设备通过稳定停靠、
无活跃/待恢复状态和位置校验后建立 known_idle；初始化不触发门动作。
已有绑定重启只识别未结束会话；运行后文件缺失/损坏/不兼容不能当“无任务”，阻塞并报告。
沿用 safe_zone_store 的存储方式，不修改现有危险区数据。
显式 reset 可复核已持久化且实际开始过的活动会话：时钟关联有效、无取消请求、
新鲜状态为暂停或仍有活动标志的任务态，且 records/clean_count/旧记录 begin/end
均与原基线相同。复核保留 session_id，不发设备命令；基线改变或日志缺失仍阻塞。

独立回充和导航使用不同会话类别 `kind=return/goto`；清扫仍为 `clean`。
持久化类别和首次移动观测 UTC，不保存可重放命令；旧版本日志缺类别时按 clean 读取。
只有已核实 known_idle，或本集成已确认结束且当前仍新鲜停稳的 goto，才能建立下一次
显式请求；未知原任务、缺日志不能由回充/导航洗成空闲。活动 clean 中的回充保留原会话。
活动 goto 收到显式回充时，在原会话内改为 return，清空旧导航的开始/停稳观测并持久化，
以本次回充发送后的新鲜返航/停靠重新确认；不把导航的开始样本当作返航成功。
return 的开始须发送后的新鲜返航/停靠观测；最终仍需两次新鲜样本跨稳定窗口、
无待续任务且基站几何自洽才终结并允许关门，不伪造清扫记录。
goto 须先观测 going_to_target，再观测后续新鲜受信 idle 且活动标志均为 0，才确认
导航已停止；不宣称精确坐标命中，结束后 known_idle=false、不可自动关门，可接受
后续显式回充或新清扫。重启 transit 仍先阻塞核对，不重放；只按对应状态/终点和
相同记录基线复核，未知或矛盾继续阻塞。
用户于 2026-10-15 确认采用统一关联失效规则：无受管活动会话时观测到外部任务，
撤销旧会话的关联有效性，而不是只设置可清除的 reason。`correlation_valid=false`
沿用现有 Store 字段持久化；can_begin、can_close、reset 和重启核对统一消费该事实。
旧导航 terminal 或 known_idle 不能在外部任务停止后重新授予权限，reset 不恢复旧关联。
统一观察入口将失效投影为 blocked/garage_problem 和既有去重故障事件；不移动设备。

## S5 标准流程与守卫

| 阶段 | 条件 | 行为 |
| --- | --- | --- |
| disabled | 新绑定未启用 | 只保留旧 guard，不启动新流程 |
| recovering | 启动/重载 | 无动作核对来源、会话和设备；不确定 blocked |
| idle | 无活跃会话且事实可判断 | 接受请求，不因进入 idle 自动关门 |
| opening | 明确请求需通行 | 发开门并等许可；超时 blocked，不发机器人移动命令 |
| awaiting_start | 原命令已发送 | 复用启动监视；观测开始才进入任务，未开始不补偿关门 |
| task_open | hold_open 活跃任务 | 保持开门意图，回洗/补电/自主恢复不关 |
| outside_closed | after_exit 的受信区外清扫 | 不额外暂停，区内→区外后关门验证；实际返航抢占关门等待 |
| pausing_return / return_opening / resuming_return | after_exit 的受管返航 | 发送暂停并等新鲜 paused；等门通行后只恢复原会话，再等实际返航继续 |
| returning | 本次真实返航且未停靠 | hold_open 应已开；异常关闭立即撤销许可/报风险，开门服从 S6 控制权 |
| docked_intermission | 任务尚未终结且停靠 | 保持开门，等待继续，charging 不是结束 |
| closing | 满足下文关闭守卫 | 重查家庭许可、发关闭、验证本方向来源 |
| waiting_close_permission | 最终收尾被家庭许可阻止 | 不关；许可恢复重查任务、停靠和人工保持 |
| blocked / manual_hold | 故障/相反命令/人工/上下文丢失 | 撤销许可，不再自动关门或恢复，需显式复核 |

启动/需要出库的恢复：开门 → 许可 → 发原请求 → 观测结果；HA 显式回充也先开门。
async_resume_task 与 async_start 选到 app_resume 的路径同样重查；不得把恢复换成
app_start/app_charge。只有自动恢复要求暂停属于控制器；用户显式 resume_task/start
恢复自己的暂停任务仍可执行，但须任务上下文有效且完整通过门互锁。
恢复调用成功不足以报告成功，必须观测原任务继续。

after_exit 关闭：真实受信区内→区外边沿、当前仍清扫、家庭许可允许，再关门并验证，
不额外暂停。返航风险优先于关门等待，先暂停并确认，再用开门反转自身未完成的关闭，
等通行后恢复；不依赖可选 STOP RPC，不靠加密官方云轮询补偿，详见 S5.1。

最终关闭：本次任务终结、稳定停靠且真实 charger 几何自洽、无待恢复请求、无人工保持、
家庭许可为真且 cover 可用。无任务的家庭 request_garage_close 使用另一合法前提：
会话为已核实 known_idle（首次交接核实或已完成会话），重新通过同样的稳定停靠、
无待恢复、几何/家庭/门守卫；不要求虚构一次清扫。会话未知/丢失不能当 known_idle。
这些场景机器人在危险区内正常，不套区外关闭守卫。
按 configured 来源得到 confirmed 或 accepted estimated 后报告对应结果；
estimated 不使确认关位实体变真，不循环重发 close 碰运气。

### S5.1 离库后关门补齐（2026-10-16 最新用户要求，REQ-7/REQ-8）

用户要求恢复旧状态机的产品流程；这是原 feature 的规格修正。本轮增量已实现并通过隔离验证，用户硬件验收仍待完成。

| 时点 | 目标行为 |
| --- | --- |
| HA 清扫开始 | 先开门并确认通行，再发原清扫请求 |
| 清扫离开危险区 | 不额外暂停；只在真实受信区内→区外边沿、当前仍清扫、家庭许可允许时关门并验证；关门中实际返航优先抢占 |
| 返回基站洗拖布或补电/最终回库 | 门尚未允许通行时，确认实际返航且未停靠，发送 app_pause 并等发送后的新鲜 paused；开门并确认通行，复核同一会话和暂停所有权，再发一次 app_resume，等待新鲜返航观测；不以 app_charge/app_start 代替恢复 |
| 中途停靠及自主继续 | 门保持开；本次任务仍活跃，不因 charging/task_active=off 收尾。再次清扫并确认离开危险区后重复离库关门 |
| 最终清扫完成回库 | 先确保返航开门；沿用本次任务终结、稳定停靠、真实基站几何和家庭许可等最终关闭守卫 |

旧实现证据：scripts/garage_state_machine/build.py 的 PHASE_LEAVE 为暂停→关门→恢复；
PHASE_RETURN 直接开门，没有暂停机器人；PHASE_PARK 额外核对清扫结束。
旧离库触发是实际区内→区外边沿，没有仅首次离库的限制；信号 unknown→区外恢复不算新离库。

最新用户要求覆盖上述旧代码：出库不暂停、返航必须暂停后开门再恢复，不直接沿用旧流程。
pause/resume 回执不是观测；暂停后 flags 清零也不能单独当成取消，须保存原会话和记录基线，
等待发送后的新鲜 paused，开门期间持续维持暂停，恢复前重查上下文、通行和所有权。
用户确认先制动、后授权：活动会话的新鲜实际返航先暂停并确认，再开门；初始记录无效或关联已失效不能阻断制动，只能撤销自动开门和恢复授权。初始授权失败须保留，后续记录恢复也不能洗白；暂停确认后保持暂停并告警，不自动开门或恢复。
内部暂停/恢复使用白名单原 transport，不能递归走用户暂停入口；用户 pause/stop、人工门控、
卸载或上下文变化撤销旧代次和暂停所有权，不能自动续上用户的暂停。失败明确阻塞，不回充代偿。
历史“V1 非清扫暂停必取消”没有原始轨迹支持；已确认旧代码把返航恢复错误映射为 app_charge，
当前代码与本地 APK 将 app_resume 和 app_charge 分开，不能混淆命令意图和固件能力。
必须测试慢门、暂停不生效、未观察到原返航恢复、记录变更、靠门返航和关门中返航。
仍须由用户进行型号实际暂停/保持/恢复验收；不能宣称采样与制动延迟为零或所有 V1 已硬件验证。
人工/故障抢占、反馈失效、重启不重放与暂停所有权沿用 S6，不引入真实设备实操。

### S5.1.1 观测恢复与故障制动（2026-10-20）

2026-10-20 S5.1修正（用户已批准，REQ-4/7/8/10/12）：
- 观测沉默与肯定矛盾分开。未知不产生离库边沿、不授权新关门；fresh/trusted区外、原会话、
  家庭许可及代次均重新通过才可重试同次中断的关闭。fresh返航始终优先抢占。
- 关门未到位而缺样：STOP可用则停止并等重采样，原动作超时固定不续期；无STOP则open反向
  中止自己的close。确认打开且观测恢复后保留task_open，本次不重关，等待未来真实离库边沿。
  可信全闭已到位可接受闭门事实，缺样继续由outside_closed的有限观察等待处理。
- 门已闭时缺样：不发新动作，首次缺样以operation_timeout_seconds设截止；恢复完整观测后
  清等待，持续失败报observation_timeout。实际进入危险区、取消或血缘变化仍失败安全。
- fresh明确返航且通行未确认时先制动，与会话/故障/人工保持分开。既有reason/保持、取消、
  未知会话只允许一次pause及新鲜paused确认，绝不open/resume；失败不按每帧重发。
  用户暂停、卸载和旧代次仍抢占。正常路径的初始授权也必须在paused后、open前复核。
- 门RPC等待不能挡住新鲜返航的制动检查；HA普通调度串行时另设只制动检查，不并发普通任务推进。
  未结束的旧门RPC只撤权、制动，不自动开门/恢复；旧操作收尾不能释放新制动所有权。
  统一跟踪全部未结束操作；安全暂停完成不代表旧门请求已退出，在全部旧请求退出前拒绝
  reset和新门动作（operation_in_progress），保持原故障，退出后仅允许用户显式重置。
  STOP、反向开门、重关均受原关闭截止约束；等待期限绑定原会话，到期后恢复也不能洗白超时。
- 可信全闭且静止、无待执行门动作的stop只释放离合；即使原有blocked/manual_hold，也原样
  保留reason/phase/operation，不新增人工接管。真实运动中stop仍撤销授权。
- 日志记录故障前phase、具体守卫、状态样本年龄、地图采样时间及fresh/trusted/outside和会话信息；
  不新增配置或持久化结构。家庭通知仍消费原事件，不由本批修改HA自动化。

### S5.2 配置减负（2026-10-16，REQ-13）

将原单页展开参数拆成基本/来源、所选反馈、相关时长与确认三步，保持 HA 原生表单。
只显示选中 contact/position/timed 的参数；未使用来源字段可以不提供，不新造硬件默认值。
已保存配置合并保留；所有已提供值仍校验范围，切换来源后缺失相关参数须明确提示，
动作超时须覆盖本次所选证据最长等待，估算接受/交接确认和单门所有权守卫继续生效。

2026-10-16 配置体验反馈：只在原生表单为缺失辅助项预填建议：行程余量10秒、触点稳定1秒、位置稳定2秒、停靠稳定5秒；动作超时取90秒与当前选中来源最长已知等待+30秒的较大值，向上取整。只看选中来源，未知/非法输入不作为测量依据，最终仍严格校验。真实开/关行程、位置阈值及触点极性不预填；估算接受和交接仍默认false。已有值（含0、非法值）不覆盖，不在运行时给缺项补默认。
第二、三步所选来源的必填参数标为Required，首屏关闭状态仍可保存不完整草稿。业务错误同时放base顶部提示及原字段，纠正前不保存绑定，错误回显保留用户值；超时提示解释须大于行程、余量及适用稳定时间之和。
上述辅助建议和提交反馈修复随v0.7.2发布；不自动改变已有配置或启用绑定，型号实物验收仍由用户完成。
配置保存验证须经过真实 HA OptionsFlowManager 的完成阶段并检查 options 更新及重载次数；由既有配置更新监听器统一负责重载，避免与 OptionsFlowWithReload 自动重载机制冲突。
v0.7.4 首屏整理：保持 drawables 同一页一次提交，页首保留新车库入口；运行监测展开，地图显示及旧版兼容设置折叠。分组仅改变表单嵌套，保存时恢复原 options/drawables 结构，保留旧平铺提交兼容；错误回显保留用户输入，分组键不落盘。中文地图、当前轮询值与原安全提示保留，不新增配置步骤。
后续用户反馈修正：移除旧版兼容组及两个字段，但不自动删除或停用已有旧配置，原所有权校验继续生效。section不再提供空对象默认值，避免HA前端跳过子字段初始化。五个监测数值仍为业务必填，在标签标明；用suggested_value预填而非schema默认值，避免提交缺项被旧值补回。数值selector保留number显示、将原始输入交给步骤校验，避免HA manager预校验抢先报泛化错误；表单允许空值到达后端，由集成严格校验缺项、整数及范围并在顶部列出具体字段名，保留全部输入、不能保存非法配置。校验错误不改变门或机器人流程。

## S5.3 HA 多阶段清扫编排（REQ-17）

HA script blueprint 按用户明确给定的阶段顺序编排；不执行服务器整套多阶段 routine。
新增一个受保护单阶段启动服务 start_garage_cleaning_stage，目标为本集成 V1 vacuum；
stage 为明确的清扫命令/参数与非移动设置列表，previous_receipt 为上一段成功回执（首段省略）。
只允许 app_start/app_segment_clean/app_zoned_clean 和明确列出的风机、水量、拖地模式、重复次数设置；
所有参数先校验，设置失败即停止，不静默丢参数，不透传未知或任意动作。
stage 仅含 command、params（可省略为 null）、settings；params 必须为有限 JSON 数据。
settings 必须是无重复的 command/params 对象列表；每段显式包含 set_custom_mode、
set_water_box_custom_mode、set_mop_mode，参数均为单个非布尔整数的列表。
app_start 另需 set_clean_repeat_times，参数为 {repeat: 正整数}；分区/划区次数保留在原 params 中。
目前仅支持这四种设置；原例程若含 set_mop_template_id 等其他设置，须报告迁移缺口，不丢弃或猜值。
服务复用现有 runtime/controller 开门与任务关联，原子捕获本次创建的 session_id，返回
{runtime_id, session_id, interruption_revision}。不能通过调用后任意读取当前 sid 冒认其他任务。
下一段携上一回执，在每个外部等待后及真正发送前复核 epoch/sid/中断代次及成功结果；
并发启动不得覆盖正在发起的阶段，旧回执不能恢复 reset、替换任务或重载前的队列。

现有 garage_phase 增加 stage_runtime_id/stage_session_id/stage_interruption_revision、
stage_result（running/succeeded/aborted）、stage_reason。属性是稳定事实，不靠易漏事件。
succeeded 必须属于同一已开始 clean 会话，匹配新成功清扫记录、未取消、关联有效、稳定停靠，
且控制器无故障及 pending；充电、task_active off、ended、服务回执都不能单独当成功。
用户 HA pause/stop、可观测外部暂停/错误、人工接管、reset、卸载使中断事实锁存；恢复不洗白。
内部返航 pause 以控制器拥有的当前会话/操作识别，不依赖尚未及时发布的 phase 实体；不算用户中断。
App 上两次采样之间完整发生的暂停再恢复不可保证发现，不夸大可观测边界。

脚本 mode single，先校验全部阶段；每次调用保存响应，等待同回执的明确结果/异常，有限超时；
异常、人工中断、换会话、缺字段或超时显式失败，不 continue_on_error、不重试、不持久化续跑索引。
脚本取消/HA 重启不启动下一段，不发设备补偿动作。原按钮/卡片只有取得真实阶段定义，或用户明确
指定替代目标且设备参数已核对，并完成隔离验证后才可切换；不捏造与原例程等价。
本批用户指定客厅与餐厅都先扫再拖，其余默认；按只读地图0的房间3/7和当前均衡吸力、中等水量、
标准路线形成两段各一次的本地交接配置。此机型使用滑动水量映射，中等为235；只扫水量200、
只拖风机105。配置未部署；换地图/重分房间需重新核对编号，其他原例程仍未取得完整定义。
用户后续要求为厨房、主卧、次卧、主卫、外卫分别生成同样的先扫后拖脚本；
沿用已核对地图0的房间5/1/6/2/4与上述参数，每房间两段各一次，仅补本地配置和隔离验证。
用户随后授权0.7.8发布和HA接线，并指出七房间已有扫地/拖地/扫拖按钮：复用原21分区动作的
受保护入口，不新增按钮、不将扫拖改成先扫后拖。六脚本配置安装且新服务加载后，仅卡片主
“先扫后拖”以script.turn_on异步调用客餐厅脚本；五个单房间脚本准备但不新增卡片入口。
保留其他例程、暂停、停止、回充；新服务须重启HA加载，未加载前不切换主入口。

## S6 并发、人工与生命周期

每绑定一个控制器/操作锁；锁保护短状态迁移，不能持锁等整段行程。
命令与等待带 generation/operation_id；返航、故障、人工可中断旧等待，旧协程醒来
不得再关门/恢复。相同开门请求可合并，但机器人任务不能重复下发；失败显式返回。

家庭许可只读，配置后仅 on 允许；只限制关闭，不阻止明确通行开门，也不绕过安全守卫。
关门中撤销许可：取消后续流程；门支持可靠停止则停止，不支持就报告动作无法撤回，
不假称已停。人体防夹依赖硬件，不由这个许可输入保证。

外部相反命令/stop/人工移动使当前操作失效并进入 manual_hold，不能立即抢回。
2026-10-17 修正：仅真实 stop_cover 在可信反馈确认全闭、门未报告运动、控制器处于
idle/outside_closed/waiting_close_permission 且无 pending、既有故障/人工保持或卸载时，
视为不干扰后续电动操作的释放离合，不新增人工保持、不发送设备命令、不增加通行许可。
只按实时事实分类，不按自动化名称或调用者白名单；toggle/set_cover_position 不得借用停止豁免。
2026-10-23 修正（v0.8.0，详见 [本批 spec](2026-10-23-spec-garage-external-open.md) S11.1）：
外部 open/close 在「无冲突」时也只记录不锁存 —— 控制器 phase 属于
idle/outside_closed/waiting_close_permission、无活动任务、无 pending、无在途 operation、
无 pause/closing 流程、门未报告运动、两端点证据无反馈失联/端点冲突/恢复快照时。
其余一切情形保持原中止/人工保持语义。该豁免**不授予通行**：不产生本控制器的命令，
`timed` 来源下 passage_allowed 仍为 unknown，下一次清扫仍须完整开门一次。
同步新增 `roborock_plus.request_garage_open` 作为受控开门入口（不查关闭许可、
不查 session.can_close），并给 garage_issue 事件补 `previous_phase`。
2026-10-25 修正（v0.8.1，详见 [本批 spec](2026-10-25-spec-garage-exit-close.md) S12.1/S12.3）：
离库判定区分「这次没采到」与「位置不可信」——前者不改变 `_departure_armed` /
`_exit_due`，后者仍立即撤销；同时「机器人已确认在危险区外时外部关门」由
`manual_override` 改为记录并发布 `external_close_after_exit`（区内或位置未知仍锁存）。
`MAP_POSITION_MAX_AGE_INTERVALS` 与 `_exit_can_close()` 均未放宽。
开关门、启动/返航等待、未知/估算全闭或来源失效仍保留原中止/人工保持语义，既有保持不自动解除。
旧关门后解锁自动化保持原触发和 stop_cover，不新增服务或配置参数。
本屋推窗器关门后必须收到停止命令释放离合，随后才能手动和电动开门；上述判断只影响
控制器是否锁存人工保持，不能拦截、替换或省略旧自动化发给推窗器的停止动作。
外部同向开门可加入观察但不重置行程计时；无法证明执行者时不由状态猜原因。
人工保持高于自动返航开门；识别为外部操作或无法归因的异常关闭都先撤销许可并立即
发风险事件，不凭“返航了”抢回门。仅无人工保持、事实有效且有明确通行请求时，
或能力合格的 after_exit 控制器自身互锁流程，才可自动发开门。
来源恢复/许可恢复不自动解除故障、续发移动或关门。

setup 注册监听/绑定占用，unload/reload 注销并取消全部等待/定时器，不能留下延迟动作。
重启清空估算与期限，读会话后只核对；不重放启动、暂停、恢复、关门。
reset_garage_control 只解除可解除的故障/人工保持并重新观察，不触发设备；
不能抹掉活动会话，下一次显式请求再过完整守卫。

## S7 实体与服务契约

新 unique_id 按稳定 duid 派生、归属目标机器人；既有 unique_id 不变。

| 接口 | 语义 |
| --- | --- |
| binary_sensor garage_open_confirmed / garage_closed_confirmed | 对应 confirmed 才 on；有效未到位 off；仅 estimated 或无法判断 unknown |
| binary_sensor garage_passage_allowed | 当前许可；无法判断 unknown，不得当作 on |
| sensor garage_phase | S5 阶段；属性 session_id、operation_id、open/close_quality、source、reason、模式 |
| binary_sensor garage_problem | 阻塞故障及 reason，供原生通知自动化消费 |
| roborock_plus.request_garage_close | 目标 vacuum；无 force；相同关闭守卫；不满足以 ServiceValidationError 返回原因 |
| roborock_plus.reset_garage_control | 目标 vacuum；仅复核/解除可解除阻塞，不动设备，不授权通行 |
| roborock_plus_garage_issue 事件 | entry_id、duid、vacuum_entity_id、cover_entity_id、session_id、phase、reason、severity、active |

事件按 duid+session+reason 激活边沿去重，解除发 active=false；启动已有故障也发布。
无会话明确 session_id=null。估算有持续可见属性/启用提示，不循环刷告警。
不绑定 script.alert_notify 或通知渠道；现有 resume_task 等服务目标/schema 不破坏，
增加互锁拒绝必须明确报错，不静默成功。

## S8 错误处理

最少原因：feedback_unavailable、endpoint_conflict、unverified_position、open_timeout、
close_timeout、start_not_observed、resume_not_observed、task_end_unconfirmed、
position_untrusted、manual_override、duplicate_binding、unsupported_return_interlock、
session_recovery_required、close_not_allowed。保留具体服务异常。
新流程下 raw 命令用显式移动/非移动分类；未知命令拒绝 unsupported_guarded_command，
不能默认透传绕过；已知只读/设置命令维持行为。
故障不补偿关闭、不盲目 pause/stop/resume；“不再关门”不等于门已开，未知如实显示。
门控错误只阻塞关联流程，不拖垮其他机器人/非门控实体，不吞错假装成功。

## S9 原生界面与升级交接

先选本条目 V1，再编辑 binding；来源按选择校验，旧轮询/绘图仍共同可达，门字段不进 DRAWABLES。
所有安全字段/错误均有 strings.json、en、zh-Hans 的 data/data_description 或对应错误文案。
无 frontend-architect 文件，不新增网页壳，沿用危险区编辑器。
旧配置升级不自动建 enabled binding、不触发设备；单机也需确认，多机显式选 duid；
hold_open 默认，估算须接受。完整启用前核对旧状态机及 *_plus 前身的主动门控并停用。
家庭自动解锁/BLE按键/无人关门按用途处理：自动关门走 request_garage_close，
释放离合只在可靠停稳后，手动操作保留并说明边界。不按名字猜并自动改删配置。
handover_confirmed 前不接管。所有新旧入口都查询同一门所有权注册表：同 duid 的旧
guard 可转发，其他机器人的旧全局 guard 不能继续操作已被接管的门，须显式拒绝并报告。
多机旧配置交接前必须逐台处理保护关系；未解决时拒绝接管，不静默解除其他机器人的保护。
所有权在配置校验和运行时都检查，不能只在界面挡住第二个新 binding。
旧 JSON/YAML 保留为 legacy 对照，文档标注交接要求；不依赖 HACS 同步家庭自动化。

## S10 验证策略

req 的 V1–V16 在 plan 落到文件；实际命令、结果和未完成项在 plan 记录。
测试注入时钟、状态、服务记录器，驱动真实证据/控制器/HA 接线；回放只提供输入，
不重写产品逻辑。保留旧 guard 兼容基线，对照命令轨迹；新 hold_open 少关门是显式模式差异，
不要求复刻旧流程的不安全动作。
覆盖来源组合/极性、回声、失联/冲突、计时反向失效、端点释放、启动已有状态、全入口、
未启动/未恢复、用户暂停、回洗/补电/自主恢复、上一任务污染/记录延迟、陈旧几何、
并发/人工、许可撤销、各阶段重启/卸载、旧迁移和原生 UI/双语。
变异至少移除开门等待/发送前重查、失联改放行、估算改确认、task_active=off 当结束、
跳过家庭许可、app_resume 换 app_charge、旧 generation 继续关门；每项须捕获。
所有仿真/变异在隔离路径；不改线上状态，不调用实体服务。硬件时长、极性、任务保持
及防夹验收由用户操作；未验收组合不标为支持。

## 代码证据

| 已读文件 | 可复用事实/当前限制 |
| --- | --- |
| garage_guard.py / door_timing.py | 95/45/90 与回声等待；旧 helper 混用 epoch/monotonic，需统一时钟 |
| vacuum.py / button.py | V1 清扫/例程有 guard，恢复/回充需接入；其他协议独立类 |
| coordinator.py / __init__.py | 每条目多协调器，门选项却全局一份；复用共享采样和生命周期 |
| resume_logic.py / v1_task_state.py | app_resume 选择可复用；task_active 不等于任务终结，pause 能力需证实 |
| sensor.py / models.py | clean_summary.last_clean_record.begin/end 与清扫数据可关联会话，不需本屋 sensor ID |
| python-roborock v5.5.1 上游模型/trait | CleanRecord 有 begin/end/complete/finish_reason，summary 有 records/clean_count；旧列表格式可缺完成标志，不假设当前任务 ID |
| binary_sensor.py / safe_zone_store.py / v1_position_trust.py | 保留身份、duid 存储和受信位置；不用默认坐标代替 charger |
| config_flow.py / tests/test_options_ui.py | 原生表单、旧单页保存/翻译守卫；门字段需分离落盘 |
| automations/roborock_garage_door_statemachine.yaml | 离开暂停/返回等待/最近结束时间是迁移对象，不是通用接口 |
| automations/README.md / 2026-10-08 状态机设计 | 旧流程不随 HACS 安装；返航不暂停是旧策略，不能据此断言 APP_PAUSE 本身必取消 |

HA 依据：[Cover 属性与能力](https://developers.home-assistant.io/docs/core/entity/cover/)、
[Template 乐观模式限制](https://www.home-assistant.io/integrations/template/#cover-optimistic-mode)。
固定依赖依据：[v5.5.1 V1 数据模型](https://github.com/Python-roborock/python-roborock/blob/v5.5.1/roborock/data/v1/v1_containers.py)、
[清扫记录 trait](https://github.com/Python-roborock/python-roborock/blob/v5.5.1/roborock/devices/traits/v1/clean_summary.py)。

## 需求追踪

REQ-1→S1/S2/S9；REQ-2→S2/S3；REQ-3→S2/S3/S7；REQ-4→S3/S6；REQ-5→S4/S6；
REQ-6→S4/S5；REQ-7→S5；REQ-8→S1/S5；REQ-9→S6/S7；REQ-10→S5/S6；
REQ-11→S4/S6；REQ-12→S7/S8；REQ-13→S2/S9；REQ-14→S9；REQ-15→S10；REQ-16→S1/S9。
逐项实施任务与 V1–V16 的完整映射见 req 需求追踪表，plan 沿用相同编号。
