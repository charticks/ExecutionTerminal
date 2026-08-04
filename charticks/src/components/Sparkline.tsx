export function Sparkline({ data, up }: { data: number[]; up: boolean }) {
  if (data.length < 2) return <svg className="spark" viewBox="0 0 100 34" preserveAspectRatio="none" />;
  const mn = Math.min(...data);
  const mx = Math.max(...data);
  const r = mx - mn || 1;
  const pts = data
    .map((v, i) => `${(i / (data.length - 1)) * 100},${34 - ((v - mn) / r) * 30 - 2}`)
    .join(" ");
  const col = up ? "var(--up)" : "var(--down)";
  return (
    <svg className="spark" viewBox="0 0 100 34" preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke={col} strokeWidth={1.5} />
      <polygon points={`0,34 ${pts} 100,34`} fill={col} opacity={0.12} />
    </svg>
  );
}
