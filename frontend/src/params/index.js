import { defineParams } from "@sveltejs/kit/params";

export const params = defineParams({
  int: (param) => (/^\d+$/.test(param) ? Number(param) : undefined),
});
