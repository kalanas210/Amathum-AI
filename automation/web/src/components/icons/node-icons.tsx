// Node icons. Generic nodes use custom monochrome glyphs (currentColor, white
// on the group-colored chip). Full-color icons — real brand logos (Simple
// Icons, CC0) and Flat Color Icons (MIT, by Icons8) — render on a white chip;
// see isBrandIcon().

import type { ComponentType, ReactNode } from "react";

export type NodeIconComponent = ComponentType<{ className?: string }>;

function S({
  className,
  children,
  filled = false,
}: {
  className?: string;
  children: ReactNode;
  filled?: boolean;
}) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill={filled ? "currentColor" : "none"}
      stroke={filled ? "none" : "currentColor"}
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

// Manual / Test trigger — a "run" play button.
const PlayIcon: NodeIconComponent = ({ className }) => (
  <S className={className} filled>
    <path d="M8 5.2v13.6a1 1 0 0 0 1.54.84l10.2-6.8a1 1 0 0 0 0-1.68L9.54 4.36A1 1 0 0 0 8 5.2Z" />
  </S>
);

// Wait — a stopwatch.
const StopwatchIcon: NodeIconComponent = ({ className }) => (
  <S className={className}>
    <circle cx="12" cy="14" r="7" />
    <path d="M12 14V9.8" />
    <path d="M9.5 2.5h5" />
    <path d="M12 4.6V2.5" />
    <path d="M18.6 8.4l1.3-1.3" />
  </S>
);

// Edit Fields (Set) — a pencil.
const PencilIcon: NodeIconComponent = ({ className }) => (
  <S className={className}>
    <path d="M14.5 5.5l4 4" />
    <path d="M4.5 19.5l1-4L15 6a2 2 0 0 1 3 3l-9.5 9.5z" />
    <path d="M4.5 19.5l4-1" />
  </S>
);

// Webhook — a broadcast / event signal.
const WebhookIcon: NodeIconComponent = ({ className }) => (
  <S className={className}>
    <circle cx="12" cy="12" r="2.2" />
    <path d="M8.6 8.6a5 5 0 0 0 0 6.8" />
    <path d="M15.4 8.6a5 5 0 0 1 0 6.8" />
    <path d="M6.1 6.1a8.5 8.5 0 0 0 0 11.8" />
    <path d="M17.9 6.1a8.5 8.5 0 0 1 0 11.8" />
  </S>
);

// Form — a clipboard with fields.
const FormIcon: NodeIconComponent = ({ className }) => (
  <S className={className}>
    <rect x="5" y="4" width="14" height="17" rx="2" />
    <path d="M9 4v-.5A1.5 1.5 0 0 1 10.5 2h3A1.5 1.5 0 0 1 15 3.5V4" />
    <path d="M8.5 11h7" />
    <path d="M8.5 15h4.5" />
  </S>
);

// Respond to Webhook — a reply arrow.
const ReplyIcon: NodeIconComponent = ({ className }) => (
  <S className={className}>
    <path d="M9 7.5L4 12l5 4.5" />
    <path d="M4 12h9.5a5.5 5.5 0 0 1 5.5 5.5V19" />
  </S>
);

// Schedule — a clock.
const ClockIcon: NodeIconComponent = ({ className }) => (
  <S className={className}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.2V12l3.2 2" />
  </S>
);

// Code — angle brackets.
const CodeIcon: NodeIconComponent = ({ className }) => (
  <S className={className}>
    <path d="M8.5 8L4.5 12l4 4" />
    <path d="M15.5 8l4 4-4 4" />
    <path d="M13.5 6l-3 12" />
  </S>
);

// Call Completed (AI voice call) — a phone handset.
const PhoneIcon: NodeIconComponent = ({ className }) => (
  <S className={className}>
    <path d="M6.8 3.5h2.7l1.4 3.6-1.8 1.4a11.5 11.5 0 0 0 4.4 4.4l1.4-1.8 3.6 1.4v2.7a2 2 0 0 1-2.2 2A16.5 16.5 0 0 1 4.8 5.7a2 2 0 0 1 2-2.2Z" />
  </S>
);

// ---- Full-color icons (rendered on a white chip) ----

// HTTP Request — a colored globe (Flat Color Icons).
const GlobeIcon: NodeIconComponent = ({ className }) => (
  <svg className={className} viewBox="0 0 48 48" aria-hidden="true">
    <path fill="#7CB342" d="M24 4C13 4 4 13 4 24s9 20 20 20s20-9 20-20S35 4 24 4" />
    <path
      fill="#0277BD"
      d="M45 24c0 11.7-9.5 21-21 21S3 35.7 3 24S12.3 3 24 3s21 9.3 21 21m-21.2 9.7c0-.4-.2-.6-.6-.8c-1.3-.4-2.5-.4-3.6-1.5c-.2-.4-.2-.8-.4-1.3c-.4-.4-1.5-.6-2.1-.8h-4.2c-.6-.2-1.1-1.1-1.5-1.7c0-.2 0-.6-.4-.6c-.4-.2-.8.2-1.3 0c-.2-.2-.2-.4-.2-.6c0-.6.4-1.3.8-1.7c.6-.4 1.3.2 1.9.2c.2 0 .2 0 .4.2c.6.2.8 1 .8 1.7v.4c0 .2.2.2.4.2c.2-1.1.2-2.1.4-3.2c0-1.3 1.3-2.5 2.3-2.9c.4-.2.6.2 1.1 0c1.3-.4 4.4-1.7 3.8-3.4c-.4-1.5-1.7-2.9-3.4-2.7c-.4.2-.6.4-1 .6c-.6.4-1.9 1.7-2.5 1.7c-1.1-.2-1.1-1.7-.8-2.3c.2-.8 2.1-3.6 3.4-3.1l.8.8c.4.2 1.1.2 1.7.2c.2 0 .4 0 .6-.2s.2-.2.2-.4c0-.6-.6-1.3-1-1.7s-1.1-.8-1.7-1.1c-2.1-.6-5.5.2-7.1 1.7s-2.9 4-3.8 6.1c-.4 1.3-.8 2.9-1 4.4c-.2 1-.4 1.9.2 2.9c.6 1.3 1.9 2.5 3.2 3.4c.8.6 2.5.6 3.4 1.7c.6.8.4 1.9.4 2.9c0 1.3.8 2.3 1.3 3.4c.2.6.4 1.5.6 2.1c0 .2.2 1.5.2 1.7c1.3.6 2.3 1.3 3.8 1.7c.2 0 1-1.3 1-1.5c.6-.6 1.1-1.5 1.7-1.9c.4-.2.8-.4 1.3-.8c.4-.4.6-1.3.8-1.9c.1-.5.3-1.3.1-1.9m.4-19.4c.2 0 .4-.2.8-.4c.6-.4 1.3-1.1 1.9-1.5s1.3-1.1 1.7-1.5c.6-.4 1.1-1.3 1.3-1.9c.2-.4.8-1.3.6-1.9c-.2-.4-1.3-.6-1.7-.8c-1.7-.4-3.1-.6-4.8-.6c-.6 0-1.5.2-1.7.8c-.2 1.1.6.8 1.5 1.1c0 0 .2 1.7.2 1.9c.2 1-.4 1.7-.4 2.7c0 .6 0 1.7.4 2.1zM41.8 29c.2-.4.2-1.1.4-1.5c.2-1 .2-2.1.2-3.1c0-2.1-.2-4.2-.8-6.1c-.4-.6-.6-1.3-.8-1.9c-.4-1.1-1-2.1-1.9-2.9c-.8-1.1-1.9-4-3.8-3.1c-.6.2-1 1-1.5 1.5c-.4.6-.8 1.3-1.3 1.9c-.2.2-.4.6-.2.8c0 .2.2.2.4.2c.4.2.6.2 1 .4c.2 0 .4.2.2.4c0 0 0 .2-.2.2c-1 1.1-2.1 1.9-3.1 2.9c-.2.2-.4.6-.4.8s.2.2.2.4s-.2.2-.4.4c-.4.2-.8.4-1.1.6c-.2.4 0 1.1-.2 1.5c-.2 1.1-.8 1.9-1.3 2.9c-.4.6-.6 1.3-1 1.9c0 .8-.2 1.5.2 2.1c1 1.5 2.9.6 4.4 1.3c.4.2.8.2 1.1.6c.6.6.6 1.7.8 2.3c.2.8.4 1.7.8 2.5c.2 1 .6 2.1.8 2.9c1.9-1.5 3.6-3.1 4.8-5.2c1.5-1.3 2.1-3 2.7-4.7"
    />
  </svg>
);

// IF — a colored flow chart that splits into branches (Flat Color Icons).
const FlowChartIcon: NodeIconComponent = ({ className }) => (
  <svg className={className} viewBox="0 0 48 48" aria-hidden="true">
    <path fill="#CFD8DC" d="M35 36h4V22H26v-9h-4v9H9v14h4V26h9v10h4V26h9z" />
    <path fill="#3F51B5" d="M17 6h14v10H17z" />
    <path fill="#00BCD4" d="M32 32h10v10H32zM6 32h10v10H6zm13 0h10v10H19z" />
  </svg>
);

// ---- Real brand logos (Simple Icons, CC0). Rendered on a white chip. ----

// Google Sheets — brand green; the table cells are negative space, so they show
// the white chip through as the grid.
const GoogleSheetsIcon: NodeIconComponent = ({ className }) => (
  <svg className={className} viewBox="0 0 24 24" fill="#0F9D58" aria-hidden="true">
    <path d="M11.318 12.545H7.91v-1.909h3.41v1.91zM14.728 0v6h6l-6-6zm1.363 10.636h-3.41v1.91h3.41v-1.91zm0 3.273h-3.41v1.91h3.41v-1.91zM20.727 6.5v15.864c0 .904-.732 1.636-1.636 1.636H4.909a1.636 1.636 0 0 1-1.636-1.636V1.636C3.273.732 4.005 0 4.909 0h9.318v6.5h6.5zm-3.273 2.773H6.545v7.909h10.91v-7.91zm-6.136 4.636H7.91v1.91h3.41v-1.91z" />
  </svg>
);

// OpenAI — official logomark.
const OpenAIIcon: NodeIconComponent = ({ className }) => (
  <svg className={className} viewBox="0 0 24 24" fill="#0d0d0d" aria-hidden="true">
    <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" />
  </svg>
);

// Fallback.
const BoxIcon: NodeIconComponent = ({ className }) => (
  <S className={className}>
    <rect x="4.5" y="4.5" width="15" height="15" rx="3" />
  </S>
);

/** Keyed by the catalog `icon` name so the node catalog stays the source. */
export const NODE_ICONS: Record<string, NodeIconComponent> = {
  "mouse-pointer-click": PlayIcon,
  globe: GlobeIcon,
  timer: StopwatchIcon,
  "git-branch": FlowChartIcon,
  pencil: PencilIcon,
  webhook: WebhookIcon,
  "clipboard-list": FormIcon,
  reply: ReplyIcon,
  clock: ClockIcon,
  "phone-call": PhoneIcon,
  "file-spreadsheet": GoogleSheetsIcon,
  terminal: CodeIcon,
  sparkles: OpenAIIcon,
  box: BoxIcon,
};

/** Full-color icons / logos carry their own colors and render on a white chip. */
const BRAND_ICON_NAMES = new Set([
  "file-spreadsheet",
  "sparkles",
  "globe",
  "git-branch",
]);

export function isBrandIcon(name: string): boolean {
  return BRAND_ICON_NAMES.has(name);
}

export function nodeIconFor(name: string): NodeIconComponent {
  return NODE_ICONS[name] ?? BoxIcon;
}
