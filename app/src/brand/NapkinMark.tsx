// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import type { CSSProperties } from "react";

/**
 * Napkin Studio OS product mark — a folded-square "napkin" glyph: a rounded
 * canvas with one corner turned up. Reads as "a sketch on a napkin becomes an
 * app" and as a window/surface. Drawn in the shared indigo→teal duotone so it
 * sits in the same family as the CLAN format mark ({@link ClanMark}).
 *
 * <NapkinMark />              → product mark
 * <NapkinLogo />             → mark + "Napkin Studio OS" wordmark lockup
 */

const GRAD_ID = "napkin-duo";

export function NapkinMark({
  size = 32,
  style,
  title = "Napkin Studio OS",
}: {
  size?: number;
  style?: CSSProperties;
  title?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      style={style}
      role="img"
      aria-label={title}
    >
      <title>{title}</title>
      <defs>
        <linearGradient id={GRAD_ID} x1="0" y1="0" x2="100" y2="100" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#6366f1" />
          <stop offset="1" stopColor="#2dd4cf" />
        </linearGradient>
      </defs>
      {/* Napkin body with the bottom-right corner folded away. */}
      <path
        d="M22 14h56c4.4 0 8 3.6 8 8v40L62 86H22c-4.4 0-8-3.6-8-8V22c0-4.4 3.6-8 8-8z"
        fill={`url(#${GRAD_ID})`}
      />
      {/* The turned-up fold. */}
      <path d="M86 62L62 86V70c0-4.4 3.6-8 8-8h16z" fill="#0f1117" fillOpacity="0.55" />
      <path d="M86 62L62 86V70c0-4.4 3.6-8 8-8h16z" fill="none" stroke="#2dd4cf" strokeWidth="2.4" strokeLinejoin="round" />
    </svg>
  );
}

/** Mark + "Napkin Studio OS" wordmark. Wordmark uses Space Grotesk. */
export function NapkinLogo({
  size = 20,
  color = "#eceefb",
  style,
  compact = false,
}: {
  size?: number;
  color?: string;
  style?: CSSProperties;
  /** Render just "Napkin" instead of the full "Napkin Studio OS". */
  compact?: boolean;
}) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: size * 0.45, ...style }}>
      <NapkinMark size={size * 1.4} />
      <span
        style={{
          fontFamily: '"Space Grotesk", system-ui, sans-serif',
          fontWeight: 600,
          fontSize: size,
          letterSpacing: "0.01em",
          color,
          lineHeight: 1,
        }}
      >
        Napkin{compact ? "" : " "}
        {!compact && (
          <span style={{ fontWeight: 500, color: "#9aa3c7" }}>Studio </span>
        )}
        {!compact && <span style={{ color: "#2dd4cf" }}>OS</span>}
      </span>
    </span>
  );
}
