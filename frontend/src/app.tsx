import { lazy, Suspense, useEffect, useState } from "react"
import { DatabaseZap, FileSpreadsheet, LibraryBig, Link2, MapPin, RefreshCw } from "lucide-react"
import { api } from "@/lib/api"
import type { CityAnalysis, CityData, CommentRecord, RecurringIssue } from "@/types"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { KnowledgeChat } from "@/components/knowledge-chat"
import { CitySummary } from "@/components/city-summary"
import { HistoricalResults, type Filters } from "@/components/history-results"

const SourceViewer = lazy(() => import("@/components/source-viewer").then((module) => ({ default: module.SourceViewer })))
const ImportDialog = lazy(() => import("@/components/import-dialog").then((module) => ({ default: module.ImportDialog })))
const ReviewLinksDialog = lazy(() => import("@/components/review-links-dialog").then((module) => ({ default: module.ReviewLinksDialog })))
const WorkbookReviewDialog = lazy(() => import("@/components/workbook-review-dialog").then((module) => ({ default: module.WorkbookReviewDialog })))

const emptyFilters: Filters = { property_project: "", discipline: "", review_round: "", match_status: "", category: "", human_review_status: "", timeline: "" }

export function App() {
  const [data, setData] = useState<CityData | null>(null)
  const [comments, setComments] = useState<CommentRecord[]>([])
  const [city, setCity] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [activeId, setActiveId] = useState<string | null>(null)
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [sourceId, setSourceId] = useState<string | null>(null)
  const [sourceOpen, setSourceOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [reviewOpen, setReviewOpen] = useState(false)
  const [reviewCount, setReviewCount] = useState<number | null>(null)
  const [workbookReviewOpen, setWorkbookReviewOpen] = useState(false)
  const [workbookReviewCount, setWorkbookReviewCount] = useState<number | null>(null)
  const [resultLabel, setResultLabel] = useState("")
  const [relevance, setRelevance] = useState(new Map<string, string>())
  const [explanations, setExplanations] = useState(new Map<string, { reason: string; important_difference: string }>())

  async function loadReviewCount() {
    const payload = await api<{ counts: { suggested: number; needs_review: number; needs_followup: number } }>("/api/link-reviews?status=all&summary=1")
    setReviewCount(payload.counts.suggested + payload.counts.needs_review + payload.counts.needs_followup)
  }

  async function loadWorkbookReviewCount() {
    const payload = await api<{ counts: { pending: number; needs_followup: number } }>("/api/workbook-reviews?status=all&summary=1")
    setWorkbookReviewCount(payload.counts.pending + payload.counts.needs_followup)
  }

  async function loadCity(nextCity = "") {
    setLoading(true); setError("")
    try {
      const payload = await api<CityData>(`/api/data${nextCity ? `?city=${encodeURIComponent(nextCity)}` : ""}`)
      const preferred = nextCity || payload.cities.find((item) => item.name === "San Jose")?.name || payload.cities[0]?.name || ""
      if (!nextCity && preferred) return await loadCity(preferred)
      setData(payload); setComments(payload.comments); setCity(preferred); setActiveId(payload.comments[0]?.comment_id || null)
      setFilters(emptyFilters); setResultLabel(""); setRelevance(new Map()); setExplanations(new Map())
    } catch (reason) { setError((reason as Error).message) } finally { setLoading(false) }
  }

  useEffect(() => {
    loadCity()
    loadReviewCount().catch(() => setReviewCount(0))
    loadWorkbookReviewCount().catch(() => setWorkbookReviewCount(0))
  }, [])

  function openSource(id: string) { setSourceId(id); setSourceOpen(true) }
  function openCommonTopic(topic: CityAnalysis["common_topics"][number]) {
    if (!data) return
    const commentIds = new Set(topic.comment_ids)
    const topicComments = data.comments.filter((comment) => commentIds.has(comment.comment_id))
    setComments(topicComments)
    setResultLabel(topic.label)
    setActiveId(topicComments[0]?.comment_id || null)
    setFilters(emptyFilters)
    setRelevance(new Map())
    setExplanations(new Map())
    window.setTimeout(() => document.getElementById("historical-results")?.scrollIntoView({ behavior: "smooth", block: "start" }), 30)
  }
  function openRecurringIssue(issue: RecurringIssue) {
    if (!data) return
    const ids = new Set(issue.comment_ids)
    const issueComments = data.comments.filter((comment) => ids.has(comment.comment_id))
    setComments(issueComments)
    setResultLabel(`Recurring issue: ${issue.title}`)
    setActiveId(issueComments.find((comment) => comment.response)?.comment_id || issueComments[0]?.comment_id || null)
    setFilters(emptyFilters)
    setRelevance(new Map())
    setExplanations(new Map())
    window.setTimeout(() => document.getElementById("historical-results")?.scrollIntoView({ behavior: "smooth", block: "start" }), 30)
  }
  async function openResultSet(resultSetId: string) {
    setLoading(true)
    try {
      const payload = await api<{ comments: CommentRecord[]; result_set: { query: string; match_classes: Record<string, string>; explanations?: Record<string, { reason: string; important_difference: string }> } }>(`/api/result-sets/${encodeURIComponent(resultSetId)}/comments`)
      setComments(payload.comments); setResultLabel(payload.result_set.query); setRelevance(new Map(Object.entries(payload.result_set.match_classes || {}))); setExplanations(new Map(Object.entries(payload.result_set.explanations || {}))); setActiveId(payload.comments[0]?.comment_id || null); setFilters(emptyFilters)
      window.setTimeout(() => document.getElementById("historical-results")?.scrollIntoView({ behavior: "smooth", block: "start" }), 30)
    } catch (reason) { setError((reason as Error).message) } finally { setLoading(false) }
  }
  return <div className="min-h-screen bg-background text-foreground">
    <header className="sticky top-0 z-30 border-b bg-background/95 backdrop-blur">
      <div className="mx-auto flex h-16 max-w-[1680px] items-center gap-4 px-4 sm:px-6">
        <div className="flex min-w-0 items-center gap-3"><div className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground"><LibraryBig className="size-5" /></div><div className="hidden sm:block"><p className="truncate font-semibold leading-tight">Permit Precedents</p><p className="text-xs text-muted-foreground">Comment Response App</p></div></div>
        <div className="mx-auto flex items-center gap-2"><MapPin className="hidden size-4 text-muted-foreground sm:block" /><Select value={city} onValueChange={loadCity} disabled={!data}><SelectTrigger aria-label="Choose city" className="w-[190px] bg-card"><SelectValue placeholder="Select city" /></SelectTrigger><SelectContent>{data?.cities.map((item) => <SelectItem value={item.name} key={item.name}>{item.name} · {item.count}</SelectItem>)}</SelectContent></Select></div>
        <Tooltip><TooltipTrigger asChild><Button variant="outline" size="sm" onClick={() => setReviewOpen(true)}><Link2 /><span className="hidden md:inline">Review links</span>{reviewCount !== null && <Badge variant="secondary">{reviewCount}</Badge>}</Button></TooltipTrigger><TooltipContent>Audit suggested comment-response links</TooltipContent></Tooltip>
        {workbookReviewCount !== null && workbookReviewCount > 0 && (
          <Tooltip><TooltipTrigger asChild><Button variant="outline" size="sm" onClick={() => setWorkbookReviewOpen(true)}><FileSpreadsheet /><span className="hidden lg:inline">Review workbooks</span><Badge className="border-amber-200 bg-amber-50 text-amber-900" variant="secondary">{workbookReviewCount}</Badge></Button></TooltipTrigger><TooltipContent>Verify structured spreadsheet imports without Gemini</TooltipContent></Tooltip>
        )}
        <Button size="sm" onClick={() => setImportOpen(true)}><DatabaseZap /><span className="hidden md:inline">Import data</span></Button>
      </div>
    </header>
    <main className="mx-auto max-w-[1680px] space-y-7 px-4 py-6 sm:px-6">
      {error && <Alert variant="destructive"><RefreshCw /><AlertTitle>Application error</AlertTitle><AlertDescription className="flex items-center justify-between gap-3"><span>{error}</span><Button variant="outline" size="sm" onClick={() => loadCity(city)}>Retry</Button></AlertDescription></Alert>}
      {loading && !data ? <div className="space-y-5"><Skeleton className="h-[620px] rounded-xl" /><Skeleton className="h-[700px] rounded-xl" /></div> : <>
        {data?.analysis && <CitySummary city={city} analysis={data.analysis} onOpenTopic={openCommonTopic} onOpenRecurringIssue={openRecurringIssue} />}
        {/* Chat has its own city-level evidence scope. Library filters are a
            browsing concern and must not silently narrow an unrelated chat
            question. Evidence-aware follow-ups remain scoped by result set. */}
        <KnowledgeChat city={city} filters={{}} onOpenSource={openSource} onOpenResults={openResultSet} sourceViewerOpen={sourceOpen} />
        <div id="historical-results"><HistoricalResults city={city} comments={comments} loading={loading} activeId={activeId} onActive={setActiveId} filters={filters} onFilters={setFilters} relevance={relevance} explanations={explanations} recurringIssues={data?.analysis?.recurring_issues || []} resultLabel={resultLabel} onClearResultSet={() => loadCity(city)} onOpenSource={openSource} onCategoriesChanged={() => loadCity(city)} /></div>
      </>}
    </main>
    <Suspense fallback={null}>
      {sourceOpen && <SourceViewer sourceId={sourceId} open={sourceOpen} onOpenChange={setSourceOpen} />}
      {importOpen && <ImportDialog open={importOpen} onOpenChange={setImportOpen} onCompleted={() => { loadCity(city); loadWorkbookReviewCount(); loadReviewCount() }} />}
      {reviewOpen && <ReviewLinksDialog open={reviewOpen} onOpenChange={setReviewOpen} cities={data?.cities.map((item) => item.name) || []} onOpenSource={openSource} onChanged={() => { loadReviewCount(); loadCity(city) }} />}
      {workbookReviewOpen && <WorkbookReviewDialog open={workbookReviewOpen} onOpenChange={setWorkbookReviewOpen} cities={data?.cities.map((item) => item.name) || []} onOpenSource={openSource} onChanged={() => { loadWorkbookReviewCount(); loadReviewCount(); loadCity(city) }} />}
    </Suspense>
  </div>
}
