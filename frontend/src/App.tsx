import { Loader2 } from "lucide-react";
import { lazy, Suspense, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Layout } from "./components/Layout";
import { MetaProvider } from "./lib/meta";
import { ThemeProvider } from "./lib/theme";
import Overview from "./pages/Overview";

// The Architecture page inlines eight large SVGs: load it only when it is opened.
const Architecture = lazy(() => import("./pages/Architecture"));
// DSS pages need trained scores and are opened less often: keep them out of the first load too.
const ApplicationDetails = lazy(() => import("./pages/ApplicationDetails"));
const GradePolicy = lazy(() => import("./pages/GradePolicy"));
const Playground = lazy(() => import("./pages/Playground"));
const Explorer = lazy(() => import("./pages/Explorer"));
const Analytics = lazy(() => import("./pages/Analytics"));

function Splash({ error }: { error?: string }) {
  return (
    <div className="grid min-h-full place-items-center p-6">
      <div className="card card-pad max-w-md text-center">
        {error ? (
          <>
            <p className="text-[15px] font-semibold text-ink">Cannot reach the API</p>
            <p className="mt-2 text-sm text-ink-2">{error}</p>
            <p className="mt-3 text-xs text-ink-3">Start it with <code>hcdw serve</code> (and build the warehouse first with <code>hcdw etl</code>).</p>
          </>
        ) : (
          <p className="flex items-center gap-2 text-sm text-ink-2"><Loader2 className="h-4 w-4 animate-spin" /> Connecting to the warehouse…</p>
        )}
      </div>
    </div>
  );
}

/**
 * Page-level boundary: a crash stays on its page and clears when the user navigates, including a new
 * query on the same page (/applications-details?id=...), without remounting a healthy page.
 */
function PageBoundary({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation();
  // Remount only when the page changes; a query change (cohort, ?dim=, ?id=) just clears a shown error.
  return <ErrorBoundary key={pathname} resetKey={search}>{children}</ErrorBoundary>;
}

/** Segments now live on Overview: /segments?dim=X → /?dim=X. */
function SegmentsRedirect() {
  const { search } = useLocation();
  return <Navigate to={`/${search}`} replace />;
}

/** Old /assessment links keep working, including their ?id= query. */
function AssessmentRedirect() {
  const { search } = useLocation();
  return <Navigate to={`/applications-details${search}`} replace />;
}

export default function App() {
  return (
    <ErrorBoundary>
    <ThemeProvider>
      <MetaProvider fallback={(err) => <Splash error={err} />}>
          <BrowserRouter>
            <Layout>
              <PageBoundary>
              <Suspense fallback={<div className="h-96 animate-pulse rounded-xl bg-raised/50" />}>
                <Routes>
                  <Route path="/" element={<Overview />} />
                  <Route path="/explore" element={<Explorer />} />
                  <Route path="/analytics" element={<Analytics />} />
                  <Route path="/olap" element={<Navigate to="/analytics" replace />} />
                  <Route path="/applications-details" element={<ApplicationDetails />} />
                  <Route path="/playground" element={<Playground />} />
                  <Route path="/grades" element={<GradePolicy />} />
                  <Route path="/architecture" element={<Architecture />} />
                  {/* Old addresses keep working. */}
                  <Route path="/segments" element={<SegmentsRedirect />} />
                  <Route path="/portfolio" element={<Navigate to="/" replace />} />
                  <Route path="/credit-history" element={<Navigate to="/?dim=credit_history_status" replace />} />
                  <Route path="/drilldown" element={<Navigate to="/analytics" replace />} />
                  <Route path="/assessment" element={<AssessmentRedirect />} />
                  <Route path="/simulator" element={<Navigate to="/applications-details" replace />} />
                  <Route path="/policy" element={<Navigate to="/playground" replace />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
              </Suspense>
              </PageBoundary>
            </Layout>
          </BrowserRouter>
      </MetaProvider>
    </ThemeProvider>
    </ErrorBoundary>
  );
}
