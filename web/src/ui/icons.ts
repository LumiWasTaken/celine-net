/* Icon sizes for lucide's `size` prop (it needs a number, not a CSS variable).
   Mirrors --icon-xs/sm/md/lg in styles/tokens.css. Stroke width is set once in
   base.css (`svg.lucide`), so call sites never pass strokeWidth. */
export const ICON = { xs: 12, sm: 14, md: 16, lg: 20 } as const
export type IconSize = keyof typeof ICON
