# Roborock Plus Translation Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Roborock Plus entity names display as clearly as the built-in Roborock integration by restoring the missing translation resource chain.

**Architecture:** Keep all entity definitions unchanged. Add the translation files Home Assistant expects for a custom integration, using the existing `strings.json` content as the source of truth, then verify that the frontend resolves `translation_key` names instead of falling back to generic device-class labels or device names.

**Tech Stack:** Home Assistant custom integration JSON translations, Python entity metadata, frontend entity translation loading.

---

### Task 1: Confirm the translation file layout Home Assistant expects for custom integrations

**Files:**
- Inspect: `C:/Code/core-upstream/homeassistant/helpers/translation.py`
- Inspect: `C:/Code/core-upstream/homeassistant/helpers/entity_platform.py`
- Inspect: `C:/Code/roborock_plus/custom_components/roborock_plus/strings.json`

**Step 1: Find the translation loading path**

Run: inspect Home Assistant translation helper code and verify whether custom integrations need `translations/<lang>.json` in addition to `strings.json`.

**Step 2: Identify the minimum language files needed**

Decide whether to add only `en.json` and `zh-Hans.json` or more.

**Step 3: Verify no entity-code change is needed**

Check that the current entity descriptions already define the right `translation_key` values.

### Task 2: Add translation resource files for Roborock Plus

**Files:**
- Create: `C:/Code/roborock_plus/custom_components/roborock_plus/translations/en.json`
- Create: `C:/Code/roborock_plus/custom_components/roborock_plus/translations/zh-Hans.json`
- Source: `C:/Code/roborock_plus/custom_components/roborock_plus/strings.json`

**Step 1: Copy the entity/service/exception naming structure needed by HA**

Add the generated translation JSON files based on the existing keys already present in `strings.json`.

**Step 2: Ensure Chinese names match the built-in Roborock integration where applicable**

Use the same visible names for common vacuum entities like current room, cleaning area, cleaning time, errors, attached mop, attached water box, and remaining consumable time.

**Step 3: Keep custom-only keys intact**

Preserve `roborock_plus` specific keys such as `resume_task`, safe-zone sensors, and safe-zone services.

### Task 3: Verify the translation resources are structurally valid

**Files:**
- Verify: `C:/Code/roborock_plus/custom_components/roborock_plus/translations/en.json`
- Verify: `C:/Code/roborock_plus/custom_components/roborock_plus/translations/zh-Hans.json`

**Step 1: Parse the translation JSON files**

Run a JSON parse check so broken syntax cannot ship.

**Step 2: Re-run existing test coverage**

Run: `python -m pytest C:/Code/roborock_plus/tests/test_resume_logic.py C:/Code/roborock_plus/tests/test_safe_zone_logic.py -q`

**Step 3: Summarize the exact HA-side validation still needed**

List the manual check: reload integration or restart HA, then verify entity labels in Chinese no longer fall back to generic names.
