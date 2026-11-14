import { AlertTriangle, RotateCcw } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Catches render errors (e.g. an unexpected API shape) so one broken page shows a card instead of
 * blanking the whole SPA. A change of `resetKey` clears a shown error without remounting healthy children
 * (remounting on every URL change made filtered pages flicker and drop their state).
 */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Render error", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="grid place-items-center p-6">
        <div className="card card-pad max-w-lg">
          <p className="flex items-center gap-2 text-[15px] font-semibold text-ink">
            <AlertTriangle className="h-4 w-4 text-bad" aria-hidden /> Something went wrong on this page
          </p>
          <p className="mt-2 break-words font-mono text-xs text-ink-2">{error.message || String(error)}</p>
          <button className="btn mt-4 text-xs" onClick={() => window.location.reload()}>
            <RotateCcw className="h-3.5 w-3.5" /> Reload
          </button>
        </div>
      </div>
    );
  }
}
