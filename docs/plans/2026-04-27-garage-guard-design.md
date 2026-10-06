# Garage Guard Design

Goal: make Home Assistant initiated Roborock clean commands wait for the cabinet door before the robot starts moving.

Scope for this step:
- Guard HA initiated start-like commands only.
- Open the configured cover by calling the configured script before the Roborock command is sent.
- Wait until `cover.current_position >= 95` before sending the Roborock command.
- Do not close the door in this step. Closing depends on the robot leaving the configured danger zone and needs a separate state machine.
- Do not try to intercept Roborock app initiated commands because those do not pass through Home Assistant services.

Files:
- `custom_components/roborock_plus/garage_guard.py`: guard config and wait logic.
- `custom_components/roborock_plus/vacuum.py`: wrap V1 start-like commands.
- `custom_components/roborock_plus/button.py`: wrap routine buttons.
- `custom_components/roborock_plus/config_flow.py`: options UI.
- translations/strings: labels and errors.
- tests: pure guard tests and options UI checks.
