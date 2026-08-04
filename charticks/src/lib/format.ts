export const money = (n: number) =>
  (n < 0 ? "-" : "+") + "₹" + Math.abs(Math.round(n)).toLocaleString("en-IN");

export const price = (n: number) => n.toFixed(2);
export const pct = (n: number) => (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
