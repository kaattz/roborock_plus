// Offline regression using original HA frontend 20260826.7 functions.
// Feed JSON from frontend_options_required_schema.py on stdin. Node >= 22.13.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";

const fixtures = new URL("./fixtures/homeassistant_frontend/", import.meta.url);
const source = (name) => readFileSync(new URL(name, fixtures), "utf8");
const moduleUrl = (text) =>
  "data:text/javascript;base64," + Buffer.from(stripTypeScriptTypes(text)).toString("base64");
const selectorsUrl = moduleUrl(source("get-selector-initial-value.ts"));
const initialUrl = moduleUrl(source("compute-initial-ha-form-data.ts").replace(
  '"./get-selector-initial-value"', JSON.stringify(selectorsUrl),
));
const conditionsUrl = moduleUrl(source("conditions.ts"));
const checkerUrl = moduleUrl(source("check-all-required-fields.ts").replace(
  '"./conditions"', JSON.stringify(conditionsUrl),
));
const fireEventUrl = moduleUrl(source("fire_event.ts"));
const submitUrl = moduleUrl(source("submit-step.ts")
  .replace('"./conditions"', JSON.stringify(conditionsUrl))
  .replace('"./fire_event"', JSON.stringify(fireEventUrl)));
const { computeInitialHaFormData } = await import(initialUrl);
const { checkAllRequiredFields } = await import(checkerUrl);
const { StepFlowFormProbe } = await import(submitUrl);
const scenarios = JSON.parse(readFileSync(0, "utf8").replace(/^\uFEFF/, ""));
assert.equal(scenarios.length, 2, "Need fresh and saved HA-native schemas");

const numericFields = [
  "v1_local_status_poll_interval", "v1_map_position_poll_interval",
  "clean_command_watch_timeout", "v1_stuck_window", "v1_stuck_radius",
];

test("official frontend control: false/zero stay valid and null suggestions stay blank", () => {
  const schema = [{ name: "group", type: "expandable", required: true, schema: [
    { name: "off", type: "boolean", required: true, default: false },
    { name: "zero", type: "integer", required: true, default: 0 },
    { name: "blank", required: false, selector: {number: {min: 1}},
      description: {suggested_value: null} },
  ] }];
  const initial = computeInitialHaFormData(schema);
  assert.deepEqual(initial, {group: {off: false, zero: 0}});
  assert.equal(Object.hasOwn(initial.group, "blank"), false);
  assert.equal(checkAllRequiredFields(schema, initial), true);
  delete initial.group.zero;
  assert.equal(checkAllRequiredFields(schema, initial), false);
});

for (const scenario of scenarios) {
  const schema = scenario.schema;
  const initial = computeInitialHaFormData(schema);
  const groups = Object.fromEntries(schema.filter((field) => field.type === "expandable").map((field) => [field.name, field]));

  test(`${scenario.name}: original frontend initializes every child default`, () => {
    assert.equal(checkAllRequiredFields(schema, initial), true, "Initial values must be submittable");
    for (const group of Object.values(groups)) {
      for (const field of group.schema) {
        const expected = field.description?.suggested_value ?? field.default;
        if (expected !== undefined) assert.deepEqual(initial[group.name]?.[field.name], expected, `${group.name}.${field.name}`);
      }
    }
    for (const name of numericFields) {
      const field = groups.monitoring.schema.find((item) => item.name === name);
      assert.equal(field.required, false, `${name} must reach named backend validation when cleared`);
      assert.equal(Object.hasOwn(field, "default"), false, `${name} must not refill a missing request server-side`);
      assert.equal(typeof field.description?.suggested_value, "number", `${name} keeps its initial UI value`);
    }
  });

  test(`${scenario.name}: old guard is absent from the UI`, () => {
    assert.equal(groups.legacy_compatibility, undefined);
    const names = schema.flatMap((field) => field.type === "expandable" ? field.schema.map((child) => child.name) : [field.name]);
    assert.equal(names.includes("garage_guard_enabled"), false);
    assert.equal(names.includes("garage_door_entity_id"), false);
  });

  test(`${scenario.name}: folded map keeps initialized values and false booleans`, () => {
    assert.equal(groups.map_display.expanded, false);
    assert.equal(typeof initial.map_display.show_rooms, "boolean");
    const edited = structuredClone(initial);
    edited.monitoring.v1_stuck_detection_enabled = false;
    edited.map_display.show_rooms = false;
    edited.map_display.path = false;
    assert.equal(checkAllRequiredFields(schema, edited), true);
    if (scenario.name === "saved_values") {
      for (const field of numericFields) assert.equal(initial.monitoring[field], scenario.previous_options[field], field);
      assert.equal(initial.monitoring.v1_stuck_detection_enabled, false);
      assert.equal(initial.map_display.show_background, true);
      assert.equal(initial.map_display.show_rooms, false);
      assert.equal(initial.map_display.show_walls, false);
      assert.equal(initial.map_display.path, false);
      assert.equal(initial.map_display.charger, false);
    }
  });

  for (const field of numericFields) {
    test(`${scenario.name}: clearing ${field} reaches backend validation`, async () => {
      const edited = structuredClone(initial);
      edited.monitoring[field] = undefined;
      // This is the frontend's actual preflight helper. An empty number must
      // reach the integration, which returns its own named validation error.
      assert.equal(checkAllRequiredFields(schema, edited), true);
      assert.equal(JSON.stringify(edited.monitoring).includes(`"${field}":`), false);
      assert.equal(edited.monitoring[field], undefined);
      const requests = [];
      const probe = new StepFlowFormProbe();
      probe._stepData = edited;
      probe.step = {data_schema: schema, flow_id: "memory-flow"};
      probe.hass = {localize: (key) => key};
      probe.flowConfig = {async handleFlowStep(_hass, flowId, data) {
        requests.push({flowId, data: JSON.parse(JSON.stringify(data))});
        return {type: "form"};
      }};
      await probe._submitStep();
      assert.equal(requests.length, 1, "The actual submit method must call its backend boundary");
      assert.equal(requests[0].flowId, "memory-flow");
      assert.equal(Object.hasOwn(requests[0].data.monitoring, field), false);
      assert.equal(probe._errorMsg, undefined);
    });
  }
}
