# 车库门 position 回声修复 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让依赖「门已全开 / 门已全关」的所有消费者，在设备回声窗口之外读取位置，从而不再把门停在半路、不再让扫地机撞半开的门。

**Architecture:** 设备在收到任意电机命令后，立刻把 `current-position` 写成**目标值**，而真实行程需要 35 秒。集成读的就是这个属性，`is_closed` 也由它派生，所以位置和状态在整个行程窗口内都被污染。修法统一为一条规则：**命令后先等过污染窗口（45 秒），再读位置做判断**。三处消费者（`auto_unlock` 自动化、状态机的四个 `wait_template`、`garage_guard.py`）各自落地这条规则。长期改用物理限位开关，本计划先把时间方案做到可验证。

**Tech Stack:** Python 3.13 / pytest 9 / Home Assistant 自动化 YAML + JSON 构建脚本 / 变异测试

**设计文档：** [2026-10-10-garage-door-position-echo-design.md](2026-10-10-garage-door-position-echo-design.md)

---

## 前置约定

**`AGENTS.md` 的实操禁令仍然有效**：本计划中的一切改动都用单元测试、回放和变异测试验证。**不要**为了验证去发 `cover.*`、`vacuum.*`。物理验证由用户执行，计划里每处都写清了「请用户做什么、预期看到什么」。

**基线**：`python -m pytest tests -q` 当前 **354 passed**。任何一步之后它都必须保持全绿（数量只增不减）。

---

## 关于模块导入的重要约束

**消费者不要 `import` 这个新模块。** 实测：

```
$ python -c "import custom_components.roborock_plus"
ModuleNotFoundError: No module named 'roborock'        # __init__.py 依赖未安装的库
```

而测试用 `spec_from_file_location` 单独加载模块，此时**相对导入直接失败**：

```
ImportError: attempted relative import with no known parent package
```

所以 `garage_guard.py` 里写 `from .door_timing import ...` 会让 `tests/test_garage_guard.py` 整套报错。

**仓库既有约定**：可被测试单独加载的模块一律不带相对导入（`garage_guard.py` / `clean_command_watch.py` / `v1_task_state.py` / `safe_zone.py` / `v1_position_trust.py` 都是纯标准库导入）。

**因此本计划的做法是**：`door_timing.py` 作为**测量值的文档来源**，各消费者保留自己的字面量，由**测试逐文件加载比对**来保证一致（这正是仓库里 `test_garage_guard_integration_points.py` 读源码断言的既有风格）。如果实现时想「顺手改成 import」，测试会立刻变红。

---

## Task 1: 把行程时长与污染窗口写成单一常量来源

回声窗口目前散落在三处（自动化 YAML、`build.py`、`garage_guard.py`），必须有一个可测的单一来源，否则以后调参只改一处。该模块**只放常量、不含任何导入**，因此可以被任何测试用路径加载。

**Files:**
- Create: `custom_components/roborock_plus/door_timing.py`
- Test: `tests/test_door_timing.py`

**Step 1: 写失败的测试**

```python
"""The echo window is one number, shared by every consumer of door position.

Measured on 2026-10-10: a full travel takes 35 seconds (user, stopwatch), while
the device echoes the target position within 0.5 seconds of any motor command.
Anything that reads `current_position` sooner than one travel is reading the
echo, not the door.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "door_timing.py"
)
SPEC = spec_from_file_location("roborock_plus_door_timing", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_travel_is_the_measured_35_seconds() -> None:
    assert MODULE.DOOR_TRAVEL_SECONDS == 35


def test_echo_settle_exceeds_one_full_travel_with_margin() -> None:
    """A settling window shorter than a travel reads the echo, not the door."""
    assert MODULE.ECHO_SETTLE_SECONDS > MODULE.DOOR_TRAVEL_SECONDS
    assert MODULE.ECHO_SETTLE_SECONDS - MODULE.DOOR_TRAVEL_SECONDS >= 10


def test_settle_covers_a_slower_than_measured_travel() -> None:
    """Cold weather or added resistance makes the door slower, not faster."""
    assert MODULE.ECHO_SETTLE_SECONDS >= MODULE.DOOR_TRAVEL_SECONDS * 1.25
```

**Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_door_timing.py -q --no-header`
Expected: FAIL — `FileNotFoundError`（模块还不存在）

**Step 3: 写最小实现**

```python
"""How long the garage door's position reading stays untrustworthy.

The device (zhenji.wopener.zj1q) answers any motor command by writing the
*target* position into `current-position` immediately, while the door is still
at its old position. Home Assistant's cover entity reads that same property,
and derives `is_closed` from it, so both the position and the state are wrong
for the whole travel.

Measured 2026-10-10: `open_cover` reported 100 within 0.5s, `close_cover`
reported 0 within 0.8s, and a full travel takes 35 seconds (user, stopwatch).
The device reports the real position only once the motor stops.

Consumers must therefore wait out `ECHO_SETTLE_SECONDS` after issuing a command
before believing any position they read.
"""

from __future__ import annotations

# One full travel, measured with a stopwatch by the owner on 2026-10-10.
DOOR_TRAVEL_SECONDS = 35

# A travel plus margin. The margin also absorbs a door that is slower than
# measured (cold, added resistance) -- it can be slower, never faster.
ECHO_SETTLE_SECONDS = 45
```

**Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_door_timing.py -q --no-header`
Expected: PASS（3 passed）

**Step 5: 提交**

```bash
git add custom_components/roborock_plus/door_timing.py tests/test_door_timing.py
git commit -m "feat(door): give the position echo window a single source"
```

---

## Task 2: `garage_guard.py` 不再在回声上放行扫地机

**这是最危险的一处**：guard 开门后等 `position >= 95` 才下发清扫命令，而回声让它 0.5 秒就通过 —— 扫地机会在门只开了一小半时出发。

**Files:**
- Modify: `custom_components/roborock_plus/garage_guard.py:14-15,58-88`
- Test: `tests/test_garage_guard.py`

**Step 1: 写失败的测试**

追加到 `tests/test_garage_guard.py` 末尾。注意：**用路径加载 `door_timing.py`，不要 import 包**（见上文导入约束）。

```python
def _door_timing():
    """Load the timing constants by path; the package itself cannot be imported."""
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "roborock_plus"
        / "door_timing.py"
    )
    spec = spec_from_file_location("roborock_plus_door_timing_for_guard", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_min_travel_outlasts_the_echo_window() -> None:
    """The guard must not accept a position read during the echo."""
    assert MODULE.DEFAULT_GARAGE_DOOR_MIN_TRAVEL >= _door_timing().ECHO_SETTLE_SECONDS


def test_timeout_leaves_room_for_the_settle_plus_the_wait() -> None:
    """A timeout shorter than travel + wait would fail a door that is opening fine."""
    assert MODULE.DEFAULT_GARAGE_DOOR_TIMEOUT > MODULE.DEFAULT_GARAGE_DOOR_MIN_TRAVEL


def test_elapsed_time_alone_is_not_enough() -> None:
    """Waiting is necessary but not sufficient: the position must also be high."""
    assert MODULE.door_reached_open_position(elapsed=999, position=10) is False
    assert MODULE.door_reached_open_position(elapsed=999, position=95) is True


def test_position_alone_is_not_enough() -> None:
    """A high position read too early is the echo of the command."""
    assert MODULE.door_reached_open_position(elapsed=0, position=100) is False
    assert MODULE.door_reached_open_position(elapsed=10, position=100) is False
```

**Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_garage_guard.py -q --no-header`
Expected: FAIL — `AttributeError: module has no attribute 'DEFAULT_GARAGE_DOOR_MIN_TRAVEL'`

**Step 3: 写最小实现**

改 `garage_guard.py` 顶部常量。**字面量与 `door_timing.py` 相同，但不要 import 它**（相对导入会破坏单独加载）：

```python
DEFAULT_GARAGE_DOOR_OPEN_POSITION = 95

# A position read sooner than one full travel is the device echoing the command
# back, not the door. The guard waits this long before it believes a reading.
# Kept in step with door_timing.ECHO_SETTLE_SECONDS by
# tests/test_garage_guard.py::test_min_travel_outlasts_the_echo_window.
DEFAULT_GARAGE_DOOR_MIN_TRAVEL = 45

# Min travel plus a bounded wait for a door that is genuinely slow. A timeout
# shorter than the settle would fail a door that is opening perfectly well.
DEFAULT_GARAGE_DOOR_TIMEOUT = DEFAULT_GARAGE_DOOR_MIN_TRAVEL + 45
```

新增纯函数（放在 `is_garage_door_open_enough` 之后）：

```python
def door_reached_open_position(elapsed: float, position: Any) -> bool:
    """Return whether enough time has passed *and* the position is high enough.

    Both halves are load-bearing. Position alone is the echo: the device reports
    the target the moment it is commanded. Time alone would accept a door that a
    physical obstruction stopped part-way.
    """
    if elapsed < DEFAULT_GARAGE_DOOR_MIN_TRAVEL:
        return False
    return is_garage_door_open_enough(position)
```

改 `async_guard_garage_open`。**注意这里有两次位置读取，语义不同，不能共用一个函数**：

- **前置检查**（原第 69 行）问的是「门现在是不是已经开着」—— 此时**我们还没发任何命令，没有回声要等**。它必须保持「只看位置」。
- **命令后的等待循环**问的是「门开好了没」—— 这里必须加时间闸门，否则回声让它在 0.5 秒放行。

所以我原先把 `elapsed` 塞进 `_is_configured_cover_open` 是错的：它会让前置检查那一行（原第 69 行）抛 `TypeError`，而且前置检查本来就不该等时间。改成拆出位置读取，两处各用各的判据：

```python
    cover_entity_id = str(options[CONF_GARAGE_DOOR_ENTITY_ID])
    # No command has been issued yet, so this reading is not an echo and can be
    # trusted on its own. Waiting here would stall every start behind 45s even
    # when the door is already open.
    if is_garage_door_open_enough(
        _configured_cover_position(hass, cover_entity_id)
    ):
        return

    await hass.services.async_call(
        "cover",
        "open_cover",
        {"entity_id": cover_entity_id},
        blocking=True,
    )

    # The command was issued now; everything the device reports until the door
    # has physically travelled is the echo of this call.
    started = time.monotonic()
    try:
        async with asyncio.timeout(DEFAULT_GARAGE_DOOR_TIMEOUT):
            while not door_reached_open_position(
                time.monotonic() - started,
                _configured_cover_position(hass, cover_entity_id),
            ):
                await asyncio.sleep(0.5)
    except TimeoutError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="garage_guard_open_timeout",
            translation_placeholders={"entity_id": cover_entity_id},
        ) from err
```

把 `_is_configured_cover_open` **改名并去掉判据**，只负责读位置（读不到仍然抛错）：

```python
def _configured_cover_position(hass: Any, cover_entity_id: str) -> Any:
    """Return the configured cover's raw position attribute.

    Deliberately does not decide whether the door is "open enough": the
    pre-command check and the post-command wait need different judgements, and
    the post-command one additionally needs elapsed time.
    """
    from homeassistant.exceptions import HomeAssistantError

    state = hass.states.get(cover_entity_id)
    if state is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="garage_guard_cover_not_found",
            translation_placeholders={"entity_id": cover_entity_id},
        )
    return state.attributes.get("current_position")
```

并在文件顶部加 `import time`。

**Step 3b: 补一个测试，钉住前置检查不等时间**

上面的 `TypeError` 不会有任何现有测试发现它（原测试只测纯函数）。同时要防止另一个错误改法：把时间闸门也加到前置检查上，那样门已经开着时每次启动还要白等 45 秒。

追加到 `tests/test_garage_guard.py`：

```python
def _guard_body() -> str:
    """Return the body of async_guard_garage_open, up to the next top-level def."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    body = source.split("async def async_guard_garage_open", 1)[1]
    return body.split("\ndef ", 1)[0]


def test_precheck_reads_position_without_waiting() -> None:
    """An already-open door must not be stalled behind the settle window.

    The pre-command check runs before any command is issued, so its reading is
    not an echo. If the elapsed-time gate were applied here, every clean start
    with the door already open would idle for 45 seconds.
    """
    precheck = _guard_body().split("await hass.services.async_call", 1)[0]

    assert "door_reached_open_position" not in precheck, (
        "the pre-command check must not apply the elapsed-time gate: no command "
        "has been issued yet, so there is no echo to outlast"
    )
    assert "elapsed" not in precheck and "monotonic" not in precheck, (
        "the pre-command check must not measure time"
    )
    assert "position" in precheck, (
        "the pre-command check must still read the door position"
    )


def test_postcommand_wait_applies_the_time_gate() -> None:
    """The wait after the command must require elapsed time as well as position."""
    post = _guard_body().split("await hass.services.async_call", 1)[1]

    assert "door_reached_open_position" in post, (
        "the post-command wait must use the elapsed-time gate, or the device's "
        "echo of open_cover satisfies it in half a second"
    )
    assert "monotonic()" in post, "the wait must measure real elapsed time"
```

**Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_garage_guard.py tests/test_garage_guard_integration_points.py -q --no-header`
Expected: PASS

再跑全量：`python -m pytest tests -q --no-header`
Expected: 363 passed（357 + 本任务新增 6）

**Step 4b: 确认改名没有漏掉调用点**

Run: `grep -rn "_is_configured_cover_open" custom_components tests scripts`
Expected: 无输出（旧名字已彻底消失）。若有残留，说明有调用点没改到。

**Step 5: 提交**

```bash
git add custom_components/roborock_plus/garage_guard.py tests/test_garage_guard.py
git commit -m "fix(guard): wait out the position echo before releasing the robot"
```

---

## Task 3: 状态机的三处 `wait_template` 越过污染窗口

阶段 B/D 用 `< 5` 判断门关了、C 用 `>= 95` 判断门开了。回声让它们 0.5 秒就通过，**防夹保护等于不存在**。

**已核对的真实结构**（`state_machine.json`，`actions[0].choose[i].sequence`）：

| 分支 | 位置 | 判据 |
| --- | --- | --- |
| 0（离开/关门） | `sequence[4]`，**顶层** | `< 5` |
| 1（返回/开门） | `sequence[1]`，**顶层** | `>= 95` |
| 2（停靠/关门） | `sequence[5].then[1]`，**嵌套在 `if/then` 里** | `< 5` |

**注意分支 2 是嵌套的** —— 只扫顶层会漏掉它，那样测试就形同虚设。下面的测试用**递归查找**，并额外断言「找到的门等待数量 == 3」，防止将来结构变化导致漏扫。

另外 `branch 0 sequence[1]` 那个 `wait_template`（等 `paused`）**不是门等待**，不要给它加 delay。

**Files:**
- Modify: `scripts/garage_state_machine/build.py:60-65,242-380`
- Test: `tests/test_garage_state_machine.py`

**Step 1: 写失败的测试**

追加到 `tests/test_garage_state_machine.py` 末尾：

```python
def _delay_seconds(delay: object) -> int:
    """Read a HA delay, which may be 'HH:MM:SS' or {seconds: N}."""
    if isinstance(delay, dict):
        return int(delay["seconds"])
    hours, minutes, seconds = (int(part) for part in str(delay).split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _echo_settle_seconds() -> int:
    """Load the shared timing constant by path.

    `custom_components.roborock_plus` cannot be imported here: its
    `__init__.py` pulls in the `roborock` library, which is not installed in the
    test environment. Loading the module file directly avoids that.
    """
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "roborock_plus"
        / "door_timing.py"
    )
    spec = importlib.util.spec_from_file_location("door_timing_for_state_machine", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ECHO_SETTLE_SECONDS


def _door_waits() -> list[tuple[str, dict, object]]:
    """Return (path, wait_step, preceding_step) for every door-position wait.

    Recursive, because the parking branch keeps its door wait inside `if/then`.
    `preceding_step` is the sibling immediately before the wait in the same
    list, which is where the settle delay must be inserted.

    Verified against the current config: finds 3 waits
    (`choose[0].sequence[4]`, `choose[1].sequence[1]`,
    `choose[2].sequence[5].then[1]`), none of which has a delay yet.
    """
    config = _config()
    found: list[tuple[str, dict, object]] = []

    def visit_list(items: list, path: str) -> None:
        for index, item in enumerate(items):
            child = f"{path}[{index}]"
            if (
                isinstance(item, dict)
                and "wait_template" in item
                and "current_position" in item["wait_template"]
            ):
                found.append((child, item, items[index - 1] if index else None))
            visit(item, child)

    def visit(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, list):
                    visit_list(value, f"{path}.{key}")
                elif isinstance(value, dict):
                    visit(value, f"{path}.{key}")
        elif isinstance(node, list):
            visit_list(node, path)

    visit(config["actions"][0]["choose"], "choose")
    return found


class TestDoorWaitsOutlastTheEcho:
    """A wait that is satisfied by the command echo proves nothing.

    The device writes the target position into `current-position` within half a
    second of any motor command, while the door is still at the old position. A
    wait keyed only on position therefore passes instantly, and the anti-crush
    waits in the closing phases never actually hold.
    """

    def test_all_three_door_waits_are_found(self) -> None:
        """Guard the scan itself: a structural change must not silently skip one."""
        paths = [path for path, _, _ in _door_waits()]
        assert len(paths) == 3, (
            f"expected 3 door-position waits, found {len(paths)}: {paths}. "
            "If the automation legitimately changed, update this count -- but "
            "check you are not just failing to see a nested wait."
        )

    def test_the_nested_parking_wait_is_included(self) -> None:
        """The regression this test exists for: branch 2's wait is inside if/then."""
        paths = [path for path, _, _ in _door_waits()]
        assert any("branch2" in path for path in paths), (
            "the parking branch's door wait is nested in if/then and was not "
            f"found by the scan; found only {paths}"
        )

    def test_every_door_wait_has_a_settle_delay_before_it(self) -> None:
        for path, _, previous in _door_waits():
            assert previous is not None, f"{path} has no preceding step"
            assert "delay" in previous, (
                f"{path} reads current_position with no settle delay in front of "
                "it, so the device's echo of the command satisfies it"
            )

    def test_the_settle_delay_is_at_least_the_echo_window(self) -> None:
        settle = _echo_settle_seconds()
        for path, _, previous in _door_waits():
            seconds = _delay_seconds(previous["delay"])
            assert seconds >= settle, (
                f"{path} waits only {seconds}s before reading position; the echo "
                f"window is {settle}s"
            )

    def test_the_paused_wait_is_not_given_a_door_delay(self) -> None:
        """The `paused` wait is about the vacuum, not the door."""
        for _, step, _ in _door_waits():
            assert "is_state" not in step["wait_template"], (
                "a vacuum-state wait was misidentified as a door wait"
            )
```

**Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_garage_state_machine.py -q --no-header`
Expected: FAIL — `test_every_door_wait_has_a_settle_delay_before_it` 报「no settle delay in front of it」

**Step 3: 写最小实现**

在 `build.py` 里加常量（与 `DOOR_OPEN` / `DOOR_CLOSED` 并列，第 60-65 行附近）：

```python
# The device echoes the commanded position immediately, so a wait keyed on
# position alone is satisfied by the echo. Every door wait therefore sits behind
# a settle delay longer than one full travel.
DOOR_SETTLE = "00:00:45"

# Settle plus room for a genuinely slow door: the old 60s budget now has to
# cover the settle as well.
DOOR_WAIT_TIMEOUT = "00:01:30"
```

加一个构造函数（放在 `wait_for` 之后）。**必须返回两个独立的步骤** —— HA 的每个动作字典只能有一个动作键，`{"delay": ..., "wait_template": ...}` 是无效配置（已核对：现有 `state_machine.json` 每个步骤字典恰好一个动作键）：

```python
def door_wait(template: str) -> list[dict]:
    """Wait for a door position, past the window where the reading is an echo.

    The delay is not cosmetic. Without it the device's immediate echo of the
    command satisfies the wait, so the anti-crush guards in phases B and D never
    hold and the door can be moved while the robot is in the doorway.

    Returns a *list* because HA allows one action key per step: the delay and the
    wait must be separate list items. Splice it in with `*door_wait(...)`.
    """
    return [{"delay": DOOR_SETTLE}, wait_for(template, DOOR_WAIT_TIMEOUT)]
```

把三处替换掉（注意用 `*` 展开，`sequence` 列表里要展开成两个元素）：

- 分支 0（离开/关门），`sequence[4]`：`wait_for(DOOR_CLOSED, "00:01:00"),` → `*door_wait(DOOR_CLOSED),`
- 分支 1（返回/开门），`sequence[1]`：`wait_for("... >= 95", "00:01:00"),` → `*door_wait("... >= 95"),`
- 分支 2（停靠/关门），`sequence[5]["then"][1]`：`wait_for(DOOR_CLOSED, "00:01:00"),` → `*door_wait(DOOR_CLOSED),`

分支 2 的替换在 `then` 列表内部，改动时要保持 `if/then/else` 的缩进结构。

分支 0/2 的告警文案里「60 秒内」要跟着改成「90 秒内」，否则告警会撒谎：

- 分支 0：`"但柜门未能在 60 秒内完全关闭"` → `"但柜门未能在 90 秒内完全关闭"`
- 分支 2：同类文案一并检查

重建配置：

```bash
python scripts/garage_state_machine/build.py
```

**Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_garage_state_machine.py -q --no-header`
Expected: PASS

再跑全量：`python -m pytest tests -q --no-header`

**Step 5: 提交**

```bash
git add scripts/garage_state_machine/build.py scripts/garage_state_machine/state_machine.json tests/test_garage_state_machine.py
git commit -m "fix(state-machine): let the door waits outlast the position echo"
```

---

## Task 4: 给状态机的回声修复加变异检查

测试通过不代表它测得动。必须证明「把 settle delay 拿掉」会让测试变红。

**注意变异脚本的工作方式**：它读 `state_machine.json`、用 **dict 变更函数**改结构、写出、跑 `tests/test_garage_state_machine.py`、再还原。**不是**做源文本替换（那是 `mutation_check_garage_guard.py` 的风格）。新变异要写成 `def _xxx(config: dict) -> None`。

**Files:**
- Modify: `scripts/mutation_check_garage_state_machine.py:136-167`（新增辅助函数）与 `:25-71`（`MUTATIONS` 列表）

**Step 1: 加辅助函数**

放在 `_drop_delay` 之后：

```python
def _shrink_door_settle(config: dict, seconds: int) -> None:
    """Shorten every settle delay to `seconds`, restoring the echo window.

    Before 2026-10-10 the door waits had no delay at all, so the device's
    immediate echo of the command satisfied them. Five seconds is the delay the
    auto-unlock used while it was parking the door part-way open; it is inside
    the echo window just as surely as zero is.
    """
    for node in _walk(config):
        if "delay" in node and isinstance(node["delay"], str):
            parts = [int(p) for p in node["delay"].split(":")]
            total = parts[0] * 3600 + parts[1] * 60 + parts[2]
            if total > seconds:
                node["delay"] = f"00:00:{seconds:02d}"


def _drop_door_wait_delays(config: dict) -> None:
    """Remove the delay that sits in front of each door position wait.

    Targets only the delays immediately preceding a `wait_template`, so the
    unrelated 8-second settle before the parking re-check survives and this
    mutation tests exactly one thing.
    """
    for branch in config["actions"][0]["choose"]:
        sequence = branch["sequence"]
        keep = []
        for index, step in enumerate(sequence):
            if (
                "delay" in step
                and index + 1 < len(sequence)
                and "wait_template" in sequence[index + 1]
                and "current_position" in json.dumps(sequence[index + 1], ensure_ascii=False)
            ):
                continue
            keep.append(step)
        branch["sequence"] = keep
```

**Step 2: 加进 `MUTATIONS`**

```python
    (
        # The defect fixed on 2026-10-10: a door wait satisfied by the command
        # echo, so the anti-crush guards in phases B and D never held.
        "drop the settle delay in front of the door waits",
        lambda c: _drop_door_wait_delays(c),
    ),
    (
        "shrink the door settle back inside the echo window",
        lambda c: _shrink_door_settle(c, 5),
    ),
```

**Step 3: 跑变异检查**

Run: `python scripts/mutation_check_garage_state_machine.py`
Expected: 全部 `CAUGHT`，`N/N mutations caught`，退出码 0

**Step 4: 确认锚点没落空**

变异脚本在 `SETUP FAILED` 时会计入 escaped。若新变异报 `SETUP FAILED`，说明锚点没匹配上 —— 检查 `state_machine.json` 里 delay 的实际形状（`_shrink_door_settle` 只处理 `"HH:MM:SS"` 字符串形式）。

**Step 5: 提交**

```bash
git add scripts/mutation_check_garage_state_machine.py
git commit -m "test(state-machine): prove the echo guard is load-bearing"
```

---

## Task 5: 修 `auto_unlock` 自动化（保留释放离合的意图）

**这是本次用户所遇问题的直接根因。** 它的意图合理（关到底后释放离合，让人能手动开），但 `delay: 5` 让 PAUSE 打在行进中的门上，把门按停在 77 / 38 这样的中途位置。

**Files:**
- Create: `automations/vacuum_garage_door_auto_unlock.yaml`
- Test: `tests/test_auto_unlock_waits_for_travel.py`

**Step 1: 写失败的测试**

```python
"""The auto-unlock must pause a *stopped* door, not a moving one.

Purpose (confirmed by the owner): release the clutch once the door is fully
shut, so it can be opened by hand. `cover.stop_cover` achieves that on a door
that has finished travelling. Issued while the door is still moving it does the
opposite -- it parks the door part-way open.

Measured 2026-10-10: the trigger fires on the device's echo (position 0 within
0.8s of the close command), then a 5-second delay, then stop_cover lands about a
fifth of the way through a 35-second travel. The door came to rest at 77 and at
38 in two separate runs.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
AUTOMATION = REPO / "automations" / "vacuum_garage_door_auto_unlock.yaml"
TIMING = REPO / "custom_components" / "roborock_plus" / "door_timing.py"


def _config() -> dict:
    return yaml.safe_load(AUTOMATION.read_text(encoding="utf-8"))


def _echo_settle() -> int:
    """Load the timing constant by path; the package cannot be imported here."""
    spec = importlib.util.spec_from_file_location("door_timing_for_auto_unlock", TIMING)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ECHO_SETTLE_SECONDS


def _first_delay() -> int:
    for action in _config()["actions"]:
        if "delay" in action:
            delay = action["delay"]
            if isinstance(delay, dict):
                return int(delay["seconds"])
            hours, minutes, seconds = (int(p) for p in str(delay).split(":"))
            return hours * 3600 + minutes * 60 + seconds
    raise AssertionError("no delay in the automation")


def test_unique_id_is_preserved() -> None:
    """`id` is the HA unique_id; changing it orphans the entity's history."""
    assert _config()["id"] == "1788771256556"


def test_pause_happens_after_a_full_travel() -> None:
    assert _first_delay() >= _echo_settle(), (
        "stop_cover must land after the door has stopped moving; a short delay "
        "issues PAUSE mid-travel and parks the door part-way open"
    )


def test_stop_is_guarded_by_a_position_recheck() -> None:
    """The door may not have shut; do not fire PAUSE at an unknown position.

    Structural rather than string-search: the recheck must be the `if` of the
    step that stops the cover, and the stop must live in its `then` branch. A
    substring scan for "condition" would also pass if the recheck sat in the
    `else`, or in some unrelated step.
    """
    actions = _config()["actions"]
    stops = [
        step
        for step in actions
        if "cover.stop_cover" in json.dumps(step.get("then", []), ensure_ascii=False)
    ]
    assert stops, "stop_cover must be inside a then branch, not an unconditional step"

    step = stops[0]
    assert step.get("if"), "the stop must be conditional"
    conditions = json.dumps(step["if"], ensure_ascii=False)
    assert "current_position" in conditions, (
        "the condition guarding the stop must be a fresh position recheck"
    )
    assert "below" in conditions, (
        "the recheck must require the door to be shut, not merely positioned"
    )


def test_the_stop_is_not_unconditional() -> None:
    """A bare stop_cover action would fire even when the door never shut."""
    for step in _config()["actions"]:
        assert step.get("action") != "cover.stop_cover", (
            "stop_cover must sit behind the recheck; an unconditional stop is "
            "the original defect in a new place"
        )
```

**Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_auto_unlock_waits_for_travel.py -q --no-header`
Expected: FAIL — `FileNotFoundError`（`automations/vacuum_garage_door_auto_unlock.yaml` 还不存在）

**Step 3: 写自动化**

`automations/vacuum_garage_door_auto_unlock.yaml`：

```yaml
# 车库门完全关闭后自动解锁
#
# 目的（用户确认）：门关到底之后释放电机离合，让门能手动推开（门是手自一体）。
#
# stop_cover 在**静止**的门上执行才是「松劲」。在门还在走的时候执行，效果相反 ——
# 它把门停在半路。2026-10-10 实测两次，门分别停在 77 和 38：
#
#   15:09:22.464  close_cover 下发
#   15:09:23.233  HA 报 closed / 0        ← 设备回声，门物理上还在 100% 处
#   15:09:24.240  本自动化触发             ← 被回声骗了
#   15:09:29.24   stop_cover 发出          ← 门只走了约 7 秒
#   15:09:32.741  设备上报真实位置 77      ← 被按停
#
# 真实行程 35 秒（用户秒表计时），所以触发后必须等过回声窗口再发 PAUSE，
# 并且用一次新鲜的位置复查确认门真的关好了。复查不通过就不发命令：
# 门没关好时发 PAUSE 没有意义，只会制造一个新的中途位置。
#
# id 不能改。它是 HA 的 unique_id，改了等于换一个自动化：历史断掉、
# 实体注册表的身份丢失。
id: "1788771256556"
alias: vacuum_garage_door_auto_unlock
description: 车库门完全关闭后自动解锁
triggers:
  - trigger: numeric_state
    entity_id: cover.vacuum_garage_door
    attribute: current_position
    below: 1
    for:
      seconds: 1
conditions: []
actions:
  # 越过回声窗口：35 秒行程 + 10 秒余量。
  - delay:
      seconds: 45
  # 复查。此时读到的是真实位置，不再是命令回声。
  - if:
      - condition: numeric_state
        entity_id: cover.vacuum_garage_door
        attribute: current_position
        below: 5
    then:
      - action: cover.stop_cover
        target:
          entity_id: cover.vacuum_garage_door
    else:
      - action: script.alert_notify
        data:
          level: warning
          title: 车库门解锁：门未关好
          # 用引号包住整条消息，避免折叠标量在中文标点后插入多余空格。
          message: "门在 45 秒后仍未完全关闭（position={{ state_attr('cover.vacuum_garage_door', 'current_position') }}），因此没有发送停止命令 —— 对没关好的门发 PAUSE 只会把它再停在一个新的中途位置。请检查门是否被卡住。"
mode: restart
```

**注意消息用双引号包成单行。** 我实测过折叠标量（`>-`）：YAML 会把折行处替换成空格，中文标点后面就会多出空格（`）， 因此`、`一个 新的`）。单行加引号没有这个问题。

**Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_auto_unlock_waits_for_travel.py -q --no-header`
Expected: PASS

**Step 5: 全量测试 + 提交**

Run: `python -m pytest tests -q --no-header`
Expected: 357 passed 以上（基线 354 + 本任务新增）

```bash
git add automations/vacuum_garage_door_auto_unlock.yaml tests/test_auto_unlock_waits_for_travel.py
git commit -m "fix(automation): pause the door after it stops, not during travel"
```

---

## Task 6: 记录教训

**Files:**
- Modify: `lessons.md`

**Step 1: 追加教训**

```markdown
- 2026-10-10: **并存现象不等于根因。** 车库门 position 不准，我一度根据「命令后 0.5 秒就报 100」判定根因在设备固件、HA 侧无法修复。回声确实存在（设备把目标值写进 current-position），但它不是用户所遇问题的根因 —— 真正的因果是 `vacuum_garage_door_auto_unlock` 在回声上触发、5 秒后发 stop_cover，把还在行进的门按停在半路。**只有做 A/B 对照（关掉那条自动化后门即正常关闭）才定位到真正的因果。** 看到两个现象同时存在时，要隔离变量，不要挑一个看起来最"底层"的当根因。
- 2026-10-10: **回声污染状态，不只是位置。** 集成的 `is_closed` 就是 `current_position == 0`，而 `_position_changed_handler` 在位置变化时清掉方向标志。所以「等 state 变成 closed」并不能绕开回声 —— 它读的是同一个值。实测 15:09:23.233 HA 报 `closed` 时，门物理上还在全开位置。**要绕开被污染的信号，必须用时间（等过污染窗口），不能换一个同样派生于它的信号。**
- 2026-10-10: **真实行程需要实测。** 我按用户「十几秒」的说法设计 `delay: 25`，用户用秒表一测是 **35 秒** —— 这个参数下门走到 71% 就会被发 PAUSE，bug 原样复现。凭印象估的时长会直接变成一个失效的安全边界；**关键时序常量必须实测，并且留余量**（门可能更慢，不可能更快）。
- 2026-10-10: **同一个命令，时机不同，效果相反。** `cover.stop_cover` 打在静止的门上是「释放离合」（这正是 auto_unlock 想要的效果），打在行进中的门上是「按停」。所以「这个命令对不对」不能脱离「什么时候发」单独判断。
```

**Step 2: 提交**

```bash
git add lessons.md
git commit -m "docs(lessons): the echo was a symptom, not the cause"
```

---

## Task 7: 更新设计文档状态并做最终核对

**Files:**
- Modify: `docs/plans/2026-10-10-garage-door-position-echo-design.md:4`（状态行）

**Step 1: 更新状态**

把 `状态：待实施` 改成 `状态：已实施（代码与自动化就绪；HA 侧需人工粘贴，物理验证待用户执行）`

**Step 2: 全量核对**

```bash
python -m pytest tests -q --no-header
python scripts/mutation_check_garage_state_machine.py
python scripts/mutation_check_garage_guard.py
python scripts/validate_blueprints.py
```

Expected：全部通过，变异检查全部 `CAUGHT`

**Step 3: 提交**

```bash
git add docs/plans/2026-10-10-garage-door-position-echo-design.md
git commit -m "docs: mark the echo design implemented"
```

---

## 部署（人工，不由助手执行）

按 `automations/README.md`：HA 里的自动化是**人工粘贴**的，没有同步脚本。

用户需要在 HA UI 里：

1. 把 `automations/vacuum_garage_door_auto_unlock.yaml` 的内容粘贴覆盖 `automation.vacuum_garage_door_auto_unlock`
   - **保留 `id: "1788771256556"`**
   - 粘贴前该自动化当前是**关闭**状态（本次排查中关掉的），粘贴后需要重新启用
2. 把 `scripts/garage_state_machine/state_machine.json` 的内容粘贴覆盖 `automation.roborock_garage_door_statemachine`
3. 通过 HACS 更新 `roborock_plus` 集成（含 `garage_guard.py` 与新增的 `door_timing.py`）

## 物理验证（由用户执行，助手不实操）

| # | 操作 | 预期 |
| --- | --- | --- |
| 1 | 确认 `auto_unlock` 已启用，然后关门到底 | 门停在 **0**，不再出现 77 / 38 这类中途值 |
| 2 | 关门后等 1 分钟，再手动推门 | 门能轻松推开（证明 PAUSE 确实释放了离合） |
| 3 | 开门到一半时手动停住 | 出现真实中途值；`auto_unlock` **不**补发 PAUSE |
| 4 | 用秒表再计 2～3 次完整行程，取**最大值** | 若 > 35 秒，更新 `DOOR_TRAVEL_SECONDS` 与相关常量 |

第 4 项很重要：35 秒是**单次**测量，冷天或阻力增大时会更长。

## 已知边界（本计划不解决）

**门卡住但电机仍在转时，position 会停在回声值（目标值），时间方案会误判「已到位」。** 只有物理限位开关能覆盖这种情况。用户已选定米家门磁（与现有小方 `isa.magnet.dw2hl` 同款，走 `xiaomi_home`）：

- 全开位、全关位各一个触点
- **`unavailable` 必须当作「不确定 → 禁止移动」** —— 现有小方传感器历史里会周期性掉线（`23:54:11 unavailable → 23:54:14 on → 23:54:34 off`），BLE 掉线若被当成「门没开」反而制造危险

门磁到位后，把「position ≥ 95」替换为「触点闭合」，本计划的时间常量降级为兜底。
