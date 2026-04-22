# Safe Zone Backend Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add backend safe-zone storage, suggestion services, and state entities for `roborock_plus`.

**Architecture:** Use a pure helper module for zone math, a storage helper for persistence, and expose the feature through vacuum entity services plus binary sensors. Restrict the first implementation to V1 vacuums.

**Tech Stack:** Home Assistant storage, entity services, binary sensors, pytest.

---

### Task 1: Add pure safe-zone logic

**Files:**
- Create: `custom_components/roborock_plus/safe_zone.py`
- Test: `tests/test_safe_zone_logic.py`

**Step 1: Write failing tests for suggestion math and containment**

**Step 2: Run tests and confirm they fail**

Run: `python -m pytest C:\Code\roborock_plus\tests\test_safe_zone_logic.py -q`

**Step 3: Implement minimal pure functions**

**Step 4: Re-run tests**

**Step 5: Commit**

### Task 2: Add storage and entity services

**Files:**
- Create: `custom_components/roborock_plus/safe_zone_store.py`
- Modify: `custom_components/roborock_plus/services.py`
- Modify: `custom_components/roborock_plus/services.yaml`
- Modify: `custom_components/roborock_plus/vacuum.py`
- Modify: `custom_components/roborock_plus/strings.json`

**Step 1: Add failing tests for store read/write if practical**

**Step 2: Implement store and services**

**Step 3: Add vacuum entity methods**

**Step 4: Verify JSON/YAML parse and python compile**

**Step 5: Commit**

### Task 3: Add safe-zone binary sensors

**Files:**
- Modify: `custom_components/roborock_plus/binary_sensor.py`

**Step 1: Add `has_safe_zone` and `in_safe_zone` entities for V1 vacuums**

**Step 2: Use last known vacuum position from map content**

**Step 3: Verify module compile**

**Step 4: Commit**
