import { useEffect, useMemo, useState } from "react"
import { ChevronDown, Filter, Search, Sparkles, X } from "lucide-react"
import { api } from "@/lib/api"
import type { CommentRecord, RecurringIssue, SearchResult } from "@/types"
import { CommentDetail } from "@/components/comment-detail"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"

export type Filters = { property_project: string; discipline: string; review_round: string; match_status: string; category: string; human_review_status: string; timeline: string }

const emptyFilters: Filters = { property_project: "", discipline: "", review_round: "", match_status: "", category: "", human_review_status: "", timeline: "" }
const valueOrAll = (value: string) => value || "__all__"
const fromSelect = (value: string) => value === "__all__" ? "" : value
const threadKey = (comment: CommentRecord) => comment.issue_thread?.thread_id || comment.comment_id

function representativeForThread(rows: CommentRecord[]) {
  return [...rows].sort((left, right) => {
    const responseDifference = Number(Boolean(right.response)) - Number(Boolean(left.response))
    if (responseDifference) return responseDifference
    const historyDifference = (right.issue_thread?.event_count || 0) - (left.issue_thread?.event_count || 0)
    if (historyDifference) return historyDifference
    return String(right.source_filename).localeCompare(String(left.source_filename))
  })[0]
}

function showsIssueHistory(rows: CommentRecord[], recurringIssue?: RecurringIssue) {
  if (recurringIssue) return true
  const rounds = new Set(rows.map((row) => row.review_round).filter(Boolean))
  const eventRounds = new Set(rows.flatMap((row) => row.issue_thread?.events || []).map((event) => event.effective_round || event.review_round).filter(Boolean))
  const hasFollowUp = rows.some((row) => (row.issue_thread?.events || []).some((event) => event.event_type === "reviewer_follow_up" || event.event_type === "discussion_note"))
  return rounds.size > 1 || eventRounds.size > 1 || hasFollowUp
}

function issueRecordCounts(rows: CommentRecord[], recurringIssue?: RecurringIssue) {
  const commentCount = recurringIssue?.comment_event_count
    ?? (rows.flatMap((row) => row.issue_thread?.events || []).filter((event) => event.actor_role !== "company" && event.event_type !== "applicant_response" && event.event_type !== "current_applicant_response").length || rows.length)
  const responseIds = new Set(rows.map((row) => row.response?.response_id).filter(Boolean))
  const responseCount = recurringIssue?.response_event_count ?? recurringIssue?.company_response_count ?? responseIds.size
  return { commentCount, responseCount, total: commentCount + responseCount }
}

function FilterSelect({ label, value, values, all, valueLabels = {}, onChange }: { label: string; value: string; values: string[]; all: string; valueLabels?: Record<string, string>; onChange: (value: string) => void }) {
  return <div className="space-y-1.5"><Label className="text-xs text-muted-foreground">{label}</Label><Select value={valueOrAll(value)} onValueChange={(next) => onChange(fromSelect(next))}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="__all__">{all}</SelectItem>{values.map((item) => <SelectItem value={item} key={item}>{valueLabels[item] || item}</SelectItem>)}</SelectContent></Select></div>
}

export function HistoricalResults({ city, comments, loading, activeId, onActive, filters, onFilters, relevance, explanations, resultLabel, onClearResultSet, onOpenSource, onCategoriesChanged, recurringIssues = [] }: {
  city: string; comments: CommentRecord[]; loading: boolean; activeId: string | null; onActive: (id: string) => void; filters: Filters; onFilters: (filters: Filters) => void
  relevance: Map<string, string>; explanations: Map<string, { reason: string; important_difference: string }>; resultLabel?: string; onClearResultSet: () => void
  onOpenSource: (id: string) => void; onCategoriesChanged: () => void; recurringIssues?: RecurringIssue[]
}) {
  const [query, setQuery] = useState("")
  const [smartResults, setSmartResults] = useState<SearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  // Summary links replace the result scope.  Do not carry a previous keyword
  // or Gemini ranking into the new scope; stale client-side search state was
  // making a recurring-issue click show a mixture of the selected timeline
  // and records from the prior list.
  useEffect(() => {
    setQuery("")
    setSmartResults(null)
  }, [comments, resultLabel])
  // A recurring issue is the library-level unit.  Its events may have been
  // extracted from several files (and may have different source thread ids),
  // so group by the timeline first and only fall back to the legacy thread key.
  const recurringByCommentId = useMemo(() => {
    const map = new Map<string, RecurringIssue>()
    recurringIssues.forEach((issue) => issue.comment_ids.forEach((commentId) => map.set(commentId, issue)))
    return map
  }, [recurringIssues])
  const groupKey = (comment: CommentRecord) => recurringByCommentId.get(comment.comment_id)?.issue_thread_id || threadKey(comment)
  const groupIssue = (comment: CommentRecord) => recurringByCommentId.get(comment.comment_id)
  const activeComment = comments.find((item) => item.comment_id === activeId) || null
  const activeGroupId = activeComment ? groupKey(activeComment) : undefined
  const activeThreadMembers = activeGroupId
    ? comments.filter((item) => groupKey(item) === activeGroupId)
    : activeComment ? [activeComment] : []

  const unique = (key: keyof CommentRecord) => [...new Set(comments.map((row) => String(row[key] || "unknown")))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
  const smartIds = useMemo(() => smartResults ? new Set(smartResults.map((item) => item.comment_id)) : null, [smartResults])
  const smartById = useMemo(() => new Map((smartResults || []).map((item) => [item.comment_id, item])), [smartResults])
  const visible = useMemo(() => comments.filter((comment) => {
    if (smartIds && !smartIds.has(comment.comment_id)) return false
    if (query && !smartIds) {
      const haystack = [comment.display_text, comment.property_project, comment.discipline, comment.source_filename, comment.category].join(" ").toLocaleLowerCase()
      if (!haystack.includes(query.toLocaleLowerCase())) return false
    }
    return Object.entries(filters).every(([key, value]) => key === "timeline" || !value || String(comment[key as keyof CommentRecord]) === value)
  }).sort((a, b) => {
    if (!smartResults) return 0
    const rank = new Map(smartResults.map((item, index) => [item.comment_id, index]))
    return (rank.get(a.comment_id) || 0) - (rank.get(b.comment_id) || 0)
  }), [comments, filters, query, smartIds, smartResults])
  const visibleGroups = useMemo(() => {
    const grouped = new Map<string, CommentRecord[]>()
    visible.forEach((comment) => {
      const key = groupKey(comment)
      grouped.set(key, [...(grouped.get(key) || []), comment])
    })
    return [...grouped.entries()].map(([key, rows]) => {
      const recurringIssue = groupIssue(rows[0])
      const hasHistory = showsIssueHistory(rows, recurringIssue)
      return { key, rows, representative: representativeForThread(rows), recurringIssue, hasHistory, counts: issueRecordCounts(rows, recurringIssue) }
    }).filter((group) => (
      filters.timeline !== "with_timeline" || group.hasHistory
    ) && (
      filters.timeline !== "without_timeline" || !group.hasHistory
    ))
  }, [visible, recurringByCommentId, filters.timeline])
  const displayedComments = visibleGroups.flatMap((group) => group.rows)
  const groupedIssues = visibleGroups.some((group) => group.hasHistory || group.rows.length > 1)

  const showSmartPrompt = Boolean(query && !smartIds && visible.length === 0)
  async function smartSearch() {
    if (!query.trim()) return
    setSearching(true)
    try {
      const payload = await api<{ results: SearchResult[] }>("/api/search", { method: "POST", body: JSON.stringify({ city, query, limit: 10, discipline: filters.discipline, category: filters.category }) })
      setSmartResults(payload.results)
    } finally { setSearching(false) }
  }
  function activateComment(commentId: string) {
    onActive(commentId)
    window.setTimeout(() => {
      document.getElementById("comment-detail-panel")?.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" })
    }, 0)
  }

  return <section aria-label="Historical results" className="history-workspace grid min-h-[760px] grid-cols-1 overflow-hidden rounded-xl border bg-card shadow-sm lg:grid-cols-[minmax(380px,42%)_minmax(0,1fr)]">
    <div className="history-list-column flex min-h-0 flex-col border-r bg-stone-50/80">
      <div className="border-b bg-white/90 p-5">
        <div className="flex items-center justify-between"><div><p className="text-xs font-semibold tracking-wide text-primary uppercase">Historical records</p><h2 className="mt-1 text-xl font-semibold">{groupedIssues ? "Issues" : "Comments"}</h2></div><Badge variant="secondary">{groupedIssues ? `${visibleGroups.length} issue${visibleGroups.length === 1 ? "" : "s"} · ${displayedComments.length} record${displayedComments.length === 1 ? "" : "s"}` : displayedComments.length}</Badge></div>
        <div className="relative mt-4"><Search className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" /><Input aria-label="Search historical comments" value={query} onChange={(event) => { setQuery(event.target.value); setSmartResults(null) }} className="pl-9" placeholder="Keyword, code, sheet, or question…" /></div>
        {showSmartPrompt && <Alert className="mt-3 border-teal-200 bg-teal-50"><Sparkles /><AlertTitle>No keyword match</AlertTitle><AlertDescription className="mt-1 flex items-center justify-between gap-3"><span>Search by meaning with Gemini.</span><Button size="sm" onClick={smartSearch} disabled={searching}>{searching ? "Searching…" : "Smart Search"}</Button></AlertDescription></Alert>}
        {smartIds && <div className="mt-3 flex items-center justify-between rounded-md bg-muted px-3 py-2 text-xs"><span>{smartResults?.length || 0} Gemini-ranked records</span><Button variant="ghost" size="sm" onClick={() => setSmartResults(null)}><X />Clear</Button></div>}
        {resultLabel && <div className="mt-3 flex items-center justify-between rounded-md border border-teal-200 bg-teal-50 px-3 py-2 text-xs"><span className="line-clamp-1">Evidence for “{resultLabel}”</span><Button variant="ghost" size="sm" onClick={onClearResultSet}>All comments</Button></div>}
        <Collapsible className="mt-4"><CollapsibleTrigger asChild><Button variant="outline" size="sm"><Filter />Filters<ChevronDown /></Button></CollapsibleTrigger><CollapsibleContent className="mt-3 grid grid-cols-2 gap-3">
          <FilterSelect label="Project / address" value={filters.property_project} values={unique("property_project")} all="All projects" onChange={(value) => onFilters({ ...filters, property_project: value })} />
          <FilterSelect label="Discipline" value={filters.discipline} values={unique("discipline")} all="All disciplines" onChange={(value) => onFilters({ ...filters, discipline: value })} />
          <FilterSelect label="Round" value={filters.review_round} values={unique("review_round")} all="All rounds" onChange={(value) => onFilters({ ...filters, review_round: value })} />
          <FilterSelect label="Response" value={filters.match_status} values={["matched", "unmatched"]} all="Any response status" onChange={(value) => onFilters({ ...filters, match_status: value })} />
          <FilterSelect label="Review state" value={filters.human_review_status} values={["confirmed", "pending", "not_required"]} all="All review states" onChange={(value) => onFilters({ ...filters, human_review_status: value })} />
          <FilterSelect label="History" value={filters.timeline} values={["with_timeline", "without_timeline"]} valueLabels={{ with_timeline: "With timeline", without_timeline: "Without timeline" }} all="Any history" onChange={(value) => onFilters({ ...filters, timeline: value })} />
          <Button variant="ghost" size="sm" className="col-span-2 justify-self-start" onClick={() => onFilters(emptyFilters)}>Reset filters</Button>
        </CollapsibleContent></Collapsible>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        {loading ? <div className="space-y-3 p-4">{[1,2,3,4].map((item) => <Skeleton className="h-32" key={item} />)}</div> : visibleGroups.length ? <div className="space-y-2 p-3">{visibleGroups.map(({ key, rows, representative: comment, recurringIssue, hasHistory, counts }) => {
          const smart = smartById.get(comment.comment_id)
          const matchClass = smart?.match_class || relevance.get(comment.comment_id)
          const why = smart ? { reason: smart.reason || "Related permit requirement and requested action.", important_difference: smart.important_difference || "" } : explanations.get(comment.comment_id)
          const memberCount = rows.length
          const isActive = key === activeGroupId
          return <Card role="button" tabIndex={0} aria-pressed={isActive} className={`result-card cursor-pointer p-4 transition-colors hover:border-primary/40 ${isActive ? "border-primary bg-primary/[.035]" : ""}`} onClick={() => activateComment(comment.comment_id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activateComment(comment.comment_id) } }} key={key}>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-1.5"><span className="text-xs font-semibold tracking-wide text-foreground uppercase">{comment.discipline}</span>{comment.comment_label && <Badge className="border-slate-200 bg-slate-50 text-slate-600" variant="outline">{comment.comment_label}</Badge>}{matchClass && <Badge variant={matchClass === "unverified" ? "destructive" : "secondary"}>{matchClass === "direct" ? "Direct precedent" : matchClass === "related" ? "Related" : "Unverified"}</Badge>}<Badge variant="outline" className={comment.response ? "border-green-200 bg-green-50 text-green-800" : "border-amber-200 bg-amber-50 text-amber-900"}>{comment.response ? "Has response" : "No response"}</Badge>{hasHistory && <Badge className="border-teal-200 bg-teal-50 text-teal-900" variant="outline">{recurringIssue ? `Review history · ${recurringIssue.round_count} rounds` : `Issue history · ${Math.max(memberCount, new Set(rows.map((row) => row.review_round).filter(Boolean)).size)} records`}</Badge>}<Badge variant="secondary">{counts.commentCount} comments · {counts.responseCount} responses</Badge></div>
              <p className="mt-2 line-clamp-3 text-sm leading-6">{comment.display_text}</p>
              <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground"><span>{comment.property_project}</span><span>{comment.city}</span><span>Round {comment.review_round}</span></div>
              {why && <Collapsible onClick={(event) => event.stopPropagation()} className="mt-2"><CollapsibleTrigger className="flex items-center gap-1 text-xs font-medium text-primary">Why it matched <ChevronDown className="size-3" /></CollapsibleTrigger><CollapsibleContent className="mt-2 rounded-md bg-muted/60 p-3 text-xs leading-5"><p>{why.reason}</p>{why.important_difference && <p className="mt-1 text-amber-800"><strong>Important difference:</strong> {why.important_difference}</p>}</CollapsibleContent></Collapsible>}
            </div>
          </Card>
        })}</div> : <div className="grid min-h-64 place-items-center p-8 text-center"><div><Search className="mx-auto size-8 text-muted-foreground" /><h3 className="mt-3 font-semibold">No comments found</h3><p className="mt-1 text-sm text-muted-foreground">Adjust the filters or try a broader search.</p></div></div>}
      </ScrollArea>
    </div>
    <CommentDetail comment={activeComment} threadMembers={activeThreadMembers} onOpenSource={onOpenSource} />
  </section>
}
