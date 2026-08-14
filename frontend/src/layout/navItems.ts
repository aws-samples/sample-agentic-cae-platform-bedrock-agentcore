export interface NavItem {
  /** Client-side route path for this panel. */
  to: string
  /** Human-readable tab label. */
  label: string
}

/**
 * The platform panels, surfaced as tabs in the app shell. Order here determines
 * the tab order in the navigation bar.
 *
 * The Workspace tab is the single-screen home: it composes the conversation
 * sidebar (Sessions), the chat, and the drawing upload/viewer, so those three
 * no longer need standalone tabs. Results Dashboard and 3D Viewer remain
 * separate because they are distinct downstream views.
 */
// Labels follow the automotive CAE (Computer-Aided Engineering) workflow:
//   Pre-Processing  → model/data intake, drawing extraction, constraint prep
//   Design Exploration → NSGA-II multi-objective optimization / solver study
//   Post-Processing → 3D result visualization & design review
export const NAV_ITEMS: NavItem[] = [
  { to: '/workspace', label: 'Pre-Processing' },
  { to: '/results', label: 'Design Exploration' },
  { to: '/viewer', label: 'Post-Processing' },
]
