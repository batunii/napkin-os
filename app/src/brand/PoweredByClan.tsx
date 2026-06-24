// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import type { CSSProperties } from "react";
import { ClanMark } from "../components/ClanMark";

/**
 * Small "powered by CLAN" lockup. Napkin Studio OS is the product; CLAN is the
 * open file format underneath. This surfaces the format provenance wherever it
 * matters (launcher footer, install prompt, lineage panel).
 */
export function PoweredByClan({ style }: { style?: CSSProperties }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontSize: 11,
        color: "var(--muted)",
        letterSpacing: "0.04em",
        ...style,
      }}
    >
      powered by <ClanMark size={13} tone="mono" style={{ color: "var(--muted)" }} />
      <span style={{ fontWeight: 600 }}>CLAN</span>
    </span>
  );
}
