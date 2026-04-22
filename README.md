# Roborock Plus

`Roborock Plus` is a Home Assistant custom integration derived from the upstream `roborock` integration.

## Why this exists

The stock integration does not fully preserve pause and resume behavior for every cleaning task type.

`Roborock Plus` adds:

- full-task resume routing for paused cleaning jobs
- explicit `roborock_plus.resume_task` service
- a separate custom integration domain so it can coexist with the built-in integration

## Current scope

- account setup through Home Assistant config flow
- vacuum entities and standard Roborock platforms from the upstream integration
- custom resume handling for paused global, zoned, segment, return-to-dock, and build-map tasks

## Installation

1. Copy `custom_components/roborock_plus` into your Home Assistant config directory.
2. Restart Home Assistant.
3. Add the `Roborock Plus` integration from the UI.
4. Recreate any automations against the new `roborock_plus` entities.

## Custom service

`roborock_plus.resume_task`

Use this service when you want an explicit resume action instead of relying on `vacuum.start`.

## Notes

- This project is experimental.
- It is intentionally independent from the built-in `roborock` integration.
- You can keep both integrations installed at the same time, but they will create separate entities.
