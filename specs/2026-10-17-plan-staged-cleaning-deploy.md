# 多阶段清扫 v0.7.8 发布及 HA 接线

绑定原REQ-17/S5.3/T26。用户“继续”授权发布、安装与卡片接线；随后指定0.7.8、30分钟。时间盒上海14:21–14:51，不延长、不操作实体设备。原60分钟开发盒已结束。本批沿现有checkout，不新worktree。

## 发布

- `<files>` 本批已验证的集成/蓝图/测试/文档；manifest、README、CHANGELOG。
- `<action>` 当前旧发布分支与origin/main树一致后，创建codex/staged-cleaning-v078并合并base；保留全部本次授权未提交内容。补蓝图source_url，版本0.7.8，评审和验证后提交/push/PR/合并/Release。
- `<verify>` 主机pytest、断网原生HA、蓝图结构、compileall；独立diff Review、文档同步及版本一致。PR与release指向验证过的提交；不虚构CI。
- `<done>` GitHub已发布可下载安装的v0.7.8；无未解决P1/P2。

## HA 安装和脚本

- `<files>` HACS kaattz/roborock_plus、script蓝图roborock_plus/多阶段清扫.yaml、六个garage_*_sweep_then_mop脚本。
- `<action>` 先只读核对无活动任务及当前门反馈，使用HACS安装精确版本及受支持的集成重载入口；不发vacuum/cover/button命令或reset。通过蓝图/脚本配置API安装，已有同名对象先读配置，保存回滚值；不运行脚本。
- `<verify>` 读取实际服务和阶段属性、蓝图及六个脚本；核对房间/参数/次数，服务存在不等于物理验收。安装失败时不改卡片。
- `<done>` 实际HA已加载新服务，六个脚本可查询且目标正确；保留配置回滚路径。

## 卡片

- `<files>` lovelace下#vacuum弹窗；仅主“先扫后拖”和厨房/主卧/次卧/主卫/外卫五行。
- `<action>` 主按钮调用客餐厅新脚本，五行各追加“先扫后拖”调用单房间新脚本。保留原21分区按钮、其他例程及暂停停止回充；用config_hash和JSON Patch仅改目标路径，不写.storage。
- `<verify>` 读回实际配置比对目标/旧21按钮；截图只检查呈现，不点击运行按钮。错误时以保存的原路径值回滚对应修改。
- `<done>` 六个入口正确指向六个已安装脚本；界面可见、不触发设备。

## 完成前复核

- `<files>` GitHub、HA实际配置、原req/spec及本计划。
- `<action>` Review查Bug→第一性原理：发布、脚本安装和入口绑定逐层确认，避免“只生成本地文件”被当作在线生效。记录实际失败和未完成项。
- `<verify>` 发布URL/提交、HA安装版本与新服务、六脚本和卡片读回、原入口差异；不做物理启动试验。
- `<done>` 明确在线生效范围及任何重启/人工保持限制，盒内收口。
