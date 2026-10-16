// Extracted unchanged from frontend 20260826.7 step-flow-form.ts lines 213-226.
// Test-only change: import/export wrapper, no function-body modifications.
// Apache-2.0; see LICENSE.md and README.md.
import { getHiddenFields } from "./conditions";

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

export { checkAllRequiredFields };
