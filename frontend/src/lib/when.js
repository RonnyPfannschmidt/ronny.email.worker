const DAY = { day: "2-digit", month: "short", year: "numeric" };
const TIME = { hour: "2-digit", minute: "2-digit", hour12: false };

const format = (value, options) => {
  if (!value) return "";
  const moment = new Date(/(Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`);
  return new Intl.DateTimeFormat("en-GB", options).format(moment);
};

export const day = (value) => format(value, DAY);
export const when = (value) => format(value, { ...DAY, ...TIME }).replace(",", "");
