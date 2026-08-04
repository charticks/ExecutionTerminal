export function Placeholder({ title }: { title: string }) {
  return (
    <section className="panel" style={{ gridColumn: "1 / 3" }}>
      <div className="phead">
        <h3>{title}</h3>
        <span className="tag">Coming in a later phase</span>
      </div>
      <div className="pbody">
        <div className="empty">
          {title} lands in a later phase of the Charticks rebuild.
        </div>
      </div>
    </section>
  );
}
