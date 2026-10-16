// Extracted unchanged from frontend 20260826.7 step-flow-form.ts lines 211-296.
// Test-only change: EventTarget class and import/export wrapper. Method body unchanged.
// Apache-2.0; see LICENSE.md and README.md.
import { getHiddenFields } from "./conditions";
import { fireEvent } from "./fire_event";

export class StepFlowFormProbe extends EventTarget {
private async _submitStep(): Promise<void> {
    const stepData = this._stepData || {};

    const checkAllRequiredFields = (
      schema: readonly HaFormSchema[],
      data: Record<string, any>
    ) => {
      const hidden = getHiddenFields(schema, data);
      return schema.every(
        (field) =>
          hidden.has(field.name) ||
          ((!field.required || !["", undefined].includes(data[field.name])) &&
            (field.type !== "expandable" ||
              (!field.required && data[field.name] === undefined) ||
              checkAllRequiredFields(field.schema, data[field.name])))
      );
    };

    const allRequiredInfoFilledIn =
      stepData === undefined
        ? // If no data filled in, just check that any field is required
          this.step.data_schema.find((field) => field.required) === undefined
        : // If data is filled in, make sure all required fields are
          checkAllRequiredFields(this.step.data_schema, stepData);

    if (!allRequiredInfoFilledIn) {
      this._errorMsg = this.hass.localize(
        "ui.panel.config.integrations.config_flow.not_all_required_fields"
      );
      return;
    }

    this._loading = true;
    this._errorMsg = undefined;
    this._submitErrors = undefined;

    const flowId = this.step.flow_id;

    const hiddenFields = getHiddenFields(this.step.data_schema, stepData);

    const toSendData: Record<string, unknown> = {};
    Object.keys(stepData).forEach((key) => {
      if (hiddenFields.has(key)) {
        // Hidden fields are not part of the submitted config
        return;
      }
      const value = stepData[key];
      const isEmpty = [undefined, ""].includes(value);
      const field = this.step.data_schema?.find((f) => f.name === key);
      const selector = (field as HaFormSelector)?.selector ?? {};
      const read_only = (
        Object.values(selector)[0] as { read_only?: boolean } | null | undefined
      )?.read_only;
      if (!isEmpty && !read_only) {
        toSendData[key] = value;
      }
    });

    try {
      const step = await this.flowConfig.handleFlowStep(
        this.hass,
        this.step.flow_id,
        toSendData
      );

      // make sure we're still showing the same step as when we
      // fired off request.
      if (!this.step || flowId !== this.step.flow_id) {
        return;
      }

      this._previewErrors = undefined;
      fireEvent(this, "flow-update", {
        step,
      });
    } catch (err: any) {
      if (err && err.body) {
        if (err.body.message) {
          this._errorMsg = err.body.message;
        }
        if (err.body.errors) {
          this._submitErrors = err.body.errors;
        }
        if (!err.body.message && !err.body.errors) {
          this._errorMsg = "Unknown error occurred";
        }
      } else {
        this._errorMsg = "Unknown error occurred";
      }
    } finally {
      this._loading = false;
    }
  }
}
