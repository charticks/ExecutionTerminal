import { IndicesPanel } from "@/components/IndicesPanel";
import { OptionChainPanel } from "@/components/OptionChainPanel";
import { PositionGridPanel } from "@/screens/Positions";

/** Primary trading workspace — indices, positions, and option chain together,
 *  usable without switching tabs. */
export function Home() {
  return (
    <div className="home">
      <IndicesPanel />
      <div className="pos-layout">
        <div className="pos-left">
          <PositionGridPanel />
        </div>
        <OptionChainPanel />
      </div>
    </div>
  );
}
