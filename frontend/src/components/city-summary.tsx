import { useMemo, useState } from "react"
import { ArrowRight, BarChart3, Building2, CircleAlert, CircleCheck, FileStack, Fingerprint, History, Layers3, Wrench } from "lucide-react"
import type { CityAnalysis, RecurringIssue } from "@/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

const stats = [
  { key: "total_comments", label: "Total comments", icon: FileStack, tone: "teal" },
  { key: "unique_comments", label: "Unique comments", icon: Fingerprint, tone: "slate" },
  { key: "technical", label: "Technical", icon: Wrench, tone: "blue" },
  { key: "nontechnical", label: "Non-technical", icon: Layers3, tone: "amber" },
  { key: "projects", label: "Projects", icon: Building2, tone: "violet" },
  { key: "review_cycles", label: "Review rounds", icon: BarChart3, tone: "stone" },
] as const

type CommonTopic = CityAnalysis["common_topics"][number]

function recurringStatus(issue: RecurringIssue) {
  if (issue.status === "resolved") return { label: "Resolved", className: "border-green-200 bg-green-50 text-green-800", icon: CircleCheck }
  if (issue.status === "open") return { label: "Still open", className: "border-amber-200 bg-amber-50 text-amber-900", icon: CircleAlert }
  return { label: "Resolution unknown", className: "border-slate-200 bg-slate-50 text-slate-700", icon: History }
}

function recurringHistoryEventCount(issue: RecurringIssue) {
  const stored = Number(issue.history_event_count)
  if (Number.isFinite(stored)) return stored
  return Number(issue.event_count || 0)
}

export function CitySummary({ city, analysis, onOpenTopic, onOpenRecurringIssue }: { city: string; analysis: CityAnalysis; onOpenTopic?: (topic: CommonTopic) => void; onOpenRecurringIssue?: (issue: RecurringIssue) => void }) {
  const [showAllRecurring, setShowAllRecurring] = useState(false)
  const recurringIssues = useMemo(() => [...(analysis.recurring_issues || [])].sort((left, right) => (
    recurringHistoryEventCount(right) - recurringHistoryEventCount(left)
    || right.round_count - left.round_count
    || right.company_response_count - left.company_response_count
    || right.source_document_count - left.source_document_count
    || left.title.localeCompare(right.title)
  )), [analysis.recurring_issues])
  const recurringStats = analysis.recurring_issue_stats
  const visibleRecurringIssues = showAllRecurring ? recurringIssues : recurringIssues.slice(0, 6)
  return <Card className="city-summary overflow-hidden border-border/90 shadow-sm">
    <CardHeader className="border-b bg-teal-50/70 pb-5">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold tracking-wide text-primary uppercase">City summary</p><CardTitle className="mt-1 text-2xl">{city} permit history</CardTitle><p className="mt-2 max-w-4xl text-sm leading-6 text-muted-foreground">{analysis.summary}</p></div><Badge variant="outline" className="bg-white">{analysis.topic_count} identified topics</Badge></div>
    </CardHeader>
    <CardContent className="space-y-5 p-5 sm:p-6">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {stats.map(({ key, label, icon: Icon, tone }) => <div className={`summary-stat ${tone}`} key={key}><div className="flex items-center justify-between"><span>{label}</span><Icon className="size-4" /></div><strong>{analysis[key]}</strong></div>)}
      </div>
      <section>
        <div className="mb-3 flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">Common comment topics</h3><Tooltip><TooltipTrigger asChild><span className="text-xs text-muted-foreground underline decoration-dotted underline-offset-4">How topics are grouped</span></TooltipTrigger><TooltipContent className="max-w-sm">{analysis.method_note}</TooltipContent></Tooltip></div>
        {analysis.common_topics.length ? <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">{analysis.common_topics.map((topic) => <button type="button" aria-label={topic.independent_source_documents == null ? `View ${topic.occurrences} comments for ${topic.label}` : `View ${topic.occurrences} comments from ${topic.independent_source_documents} independent source documents for ${topic.label}`} className="group rounded-lg border bg-white p-3 text-left transition-colors hover:border-primary/40 hover:bg-teal-50/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => onOpenTopic?.(topic)} key={`${topic.label}-${topic.occurrences}`}><p className="line-clamp-2 text-sm font-medium leading-5">{topic.label}</p><div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground"><span>{topic.occurrences} comments</span><span>{topic.independent_source_documents ?? topic.occurrences} independent documents</span><span>{topic.projects} projects</span><span>{topic.rounds} rounds</span>{(topic.physical_duplicate_files_excluded ?? 0) > 0 && <span>{topic.physical_duplicate_files_excluded} duplicate files excluded</span>}</div><span className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-primary">View comments <ArrowRight className="size-3 transition-transform group-hover:translate-x-0.5" /></span></button>)}</div> : <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">No repeated topic group was identified for this city.</p>}
      </section>
      <section className="border-t pt-5" aria-label="Recurring issues">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><div className="flex items-center gap-2"><History className="size-4 text-primary" /><h3 className="text-sm font-semibold">Recurring issues</h3></div><p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">Issue histories beyond a single comment or one comment-response pair. Repeated source files stay attached to one event and do not increase the count.</p></div>
          {recurringStats && <Badge variant="outline" className="bg-white">{recurringStats.total} issue timelines</Badge>}
        </div>
        {recurringStats && <div className="mt-4 grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
          <div className="summary-stat teal"><span>Recurring issues</span><strong>{recurringStats.total}</strong></div>
          <div className="summary-stat amber"><span>Still open</span><strong>{recurringStats.open}</strong></div>
          <div className="summary-stat blue"><span>Resolved</span><strong>{recurringStats.resolved}</strong></div>
          <div className="summary-stat slate"><span>Unknown</span><strong>{recurringStats.unknown}</strong></div>
          <div className="summary-stat stone"><span>Avg. rounds to resolution</span><strong>{recurringStats.average_rounds_to_resolution ?? "—"}</strong></div>
          <div className="summary-stat violet"><span>Longest running</span><strong>{recurringStats.longest_running_rounds ? `${recurringStats.longest_running_rounds} rounds` : "—"}</strong></div>
        </div>}
        {visibleRecurringIssues.length ? <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3">{visibleRecurringIssues.map((issue) => {
          const status = recurringStatus(issue)
          const StatusIcon = status.icon
          return <button type="button" key={issue.issue_thread_id} aria-label={`View timeline for ${issue.title}`} className="group rounded-lg border bg-white p-4 text-left transition-colors hover:border-primary/40 hover:bg-teal-50/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => onOpenRecurringIssue?.(issue)}>
            <div className="flex items-start justify-between gap-2"><p className="line-clamp-2 text-sm font-semibold leading-5">{issue.title}</p><span className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${status.className}`}><StatusIcon className="size-3" />{status.label}</span></div>
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground"><span>{issue.first_round === issue.latest_round ? `Round ${issue.first_round}` : `PC${issue.first_round} → PC${issue.latest_round}`}</span><span>{issue.round_count} {issue.round_count === 1 ? "round" : "rounds"}</span><span>{issue.company_response_count} {issue.company_response_count === 1 ? "response" : "responses"}</span><span>{issue.source_document_count} source docs</span></div>
            <p className="mt-2 line-clamp-3 text-xs leading-5 text-slate-600"><strong className="font-semibold text-slate-700">{issue.status === "open" ? "Why it persisted: " : "History note: "}</strong>{issue.persistence_explanation || issue.status_reason}</p>
            <div className="mt-2 flex items-center justify-between gap-2"><span className="line-clamp-1 text-[11px] text-muted-foreground">{issue.common_topic} · {issue.site_name || city}</span><span className="inline-flex shrink-0 items-center gap-1 text-xs font-medium text-primary">View timeline <ArrowRight className="size-3 transition-transform group-hover:translate-x-0.5" /></span></div>
          </button>
        })}</div> : <p className="mt-4 rounded-lg border border-dashed p-4 text-sm text-muted-foreground">No multi-round issue timeline was identified for this city.</p>}
        {recurringIssues.length > 6 && <Button variant="ghost" size="sm" className="mt-3" onClick={() => setShowAllRecurring((value) => !value)}>{showAllRecurring ? "Show fewer" : `Show all ${recurringIssues.length} timelines`}</Button>}
      </section>
    </CardContent>
  </Card>
}
