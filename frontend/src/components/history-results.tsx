import { useMemo, useState } from "react"
import { ChevronDown, Filter, Search, Sparkles, Tags, X } from "lucide-react"
import { api } from "@/lib/api"
import type { CommentRecord, SearchResult } from "@/types"
import { CommentDetail } from "@/components/comment-detail"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"

export type Filters = { property_project: string; discipline: string; review_round: string; match_status: string; category: string; human_review_status: string }

const emptyFilters: Filters = { property_project: "", discipline: "", review_round: "", match_status: "", category: "", human_review_status: "" }
const valueOrAll = (value: string) => value || "__all__"
const fromSelect = (value: string) => value === "__all__" ? "" : value

function FilterSelect({ label, value, values, all, onChange }: { label: string; value: string; values: string[]; all: string; onChange: (value: string) => void }) {
  return <div className="space-y-1.5"><Label className="text-xs text-muted-foreground">{label}</Label><Select value={valueOrAll(value)} onValueChange={(next) => onChange(fromSelect(next))}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="__all__">{all}</SelectItem>{values.map((item) => <SelectItem value={item} key={item}>{item}</SelectItem>)}</SelectContent></Select></div>
}

export function HistoricalResults({ city, comments, loading, activeId, onActive, filters, onFilters, relevance, explanations, resultLabel, onClearResultSet, onOpenSource, onCategoriesChanged }: {
  city: string; comments: CommentRecord[]; loading: boolean; activeId: string | null; onActive: (id: string) => void; filters: Filters; onFilters: (filters: Filters) => void
  relevance: Map<string, string>; explanations: Map<string, { reason: string; important_difference: string }>; resultLabel?: string; onClearResultSet: () => void
  onOpenSource: (id: string) => void; onCategoriesChanged: () => void
}) {
  const [query, setQuery] = useState("")
  const [smartResults, setSmartResults] = useState<SearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [categoryOpen, setCategoryOpen] = useState(false)
  const [category, setCategory] = useState("")
  const threadCounts = useMemo(() => {
    const counts = new Map<string, number>()
    comments.forEach((comment) => {
      const threadId = comment.issue_thread?.thread_id
      if (threadId) counts.set(threadId, (counts.get(threadId) || 0) + 1)
    })
    return counts
  }, [comments])
  const activeComment = comments.find((item) => item.comment_id === activeId) || null
  const activeThreadId = activeComment?.issue_thread?.thread_id
  const activeThreadMembers = activeThreadId
    ? comments.filter((item) => item.issue_thread?.thread_id === activeThreadId)
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
    return Object.entries(filters).every(([key, value]) => !value || String(comment[key as keyof CommentRecord]) === value)
  }).sort((a, b) => {
    if (!smartResults) return 0
    const rank = new Map(smartResults.map((item, index) => [item.comment_id, index]))
    return (rank.get(a.comment_id) || 0) - (rank.get(b.comment_id) || 0)
  }), [comments, filters, query, smartIds, smartResults])

  const showSmartPrompt = Boolean(query && !smartIds && visible.length === 0)
  async function smartSearch() {
    if (!query.trim()) return
    setSearching(true)
    try {
      const payload = await api<{ results: SearchResult[] }>("/api/search", { method: "POST", body: JSON.stringify({ city, query, limit: 10, discipline: filters.discipline, category: filters.category }) })
      setSmartResults(payload.results)
    } finally { setSearching(false) }
  }
  async function saveCategory(value: string) {
    if (!selected.size) return
    await api("/api/categories", { method: "POST", body: JSON.stringify({ comment_ids: [...selected], category: value }) })
    setSelected(new Set()); setCategoryOpen(false); setCategory(""); onCategoriesChanged()
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
        <div className="flex items-center justify-between"><div><p className="text-xs font-semibold tracking-wide text-primary uppercase">Historical records</p><h2 className="mt-1 text-xl font-semibold">Comments</h2></div><Badge variant="secondary">{visible.length}</Badge></div>
        <div className="relative mt-4"><Search className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" /><Input aria-label="Search historical comments" value={query} onChange={(event) => { setQuery(event.target.value); setSmartResults(null) }} className="pl-9" placeholder="Keyword, code, sheet, or question…" /></div>
        {showSmartPrompt && <Alert className="mt-3 border-teal-200 bg-teal-50"><Sparkles /><AlertTitle>No keyword match</AlertTitle><AlertDescription className="mt-1 flex items-center justify-between gap-3"><span>Search by meaning with Gemini.</span><Button size="sm" onClick={smartSearch} disabled={searching}>{searching ? "Searching…" : "Smart Search"}</Button></AlertDescription></Alert>}
        {smartIds && <div className="mt-3 flex items-center justify-between rounded-md bg-muted px-3 py-2 text-xs"><span>{smartResults?.length || 0} Gemini-ranked records</span><Button variant="ghost" size="sm" onClick={() => setSmartResults(null)}><X />Clear</Button></div>}
        {resultLabel && <div className="mt-3 flex items-center justify-between rounded-md border border-teal-200 bg-teal-50 px-3 py-2 text-xs"><span className="line-clamp-1">Evidence for “{resultLabel}”</span><Button variant="ghost" size="sm" onClick={onClearResultSet}>All comments</Button></div>}
        <Collapsible className="mt-4"><CollapsibleTrigger asChild><Button variant="outline" size="sm"><Filter />Filters<ChevronDown /></Button></CollapsibleTrigger><CollapsibleContent className="mt-3 grid grid-cols-2 gap-3">
          <FilterSelect label="Project / address" value={filters.property_project} values={unique("property_project")} all="All projects" onChange={(value) => onFilters({ ...filters, property_project: value })} />
          <FilterSelect label="Discipline" value={filters.discipline} values={unique("discipline")} all="All disciplines" onChange={(value) => onFilters({ ...filters, discipline: value })} />
          <FilterSelect label="Round" value={filters.review_round} values={unique("review_round")} all="All rounds" onChange={(value) => onFilters({ ...filters, review_round: value })} />
          <FilterSelect label="Response" value={filters.match_status} values={["matched", "unmatched"]} all="Any response status" onChange={(value) => onFilters({ ...filters, match_status: value })} />
          <FilterSelect label="Category" value={filters.category} values={unique("category")} all="All categories" onChange={(value) => onFilters({ ...filters, category: value })} />
          <FilterSelect label="Review state" value={filters.human_review_status} values={["confirmed", "pending", "not_required"]} all="All review states" onChange={(value) => onFilters({ ...filters, human_review_status: value })} />
          <Button variant="ghost" size="sm" className="col-span-2 justify-self-start" onClick={() => onFilters(emptyFilters)}>Reset filters</Button>
        </CollapsibleContent></Collapsible>
      </div>
      <div className="flex items-center gap-3 border-b bg-stone-100/80 px-5 py-2.5 text-xs text-muted-foreground"><Checkbox aria-label="Select visible comments" checked={visible.length > 0 && visible.every((item) => selected.has(item.comment_id))} onCheckedChange={(checked) => setSelected(checked ? new Set(visible.map((item) => item.comment_id)) : new Set())} /><span>{selected.size ? `${selected.size} selected` : "Select visible"}</span>{selected.size > 0 && <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setCategoryOpen(true)}><Tags />Categorize</Button>}</div>
      <ScrollArea className="min-h-0 flex-1">
        {loading ? <div className="space-y-3 p-4">{[1,2,3,4].map((item) => <Skeleton className="h-32" key={item} />)}</div> : visible.length ? <div className="space-y-2 p-3">{visible.map((comment) => {
          const smart = smartById.get(comment.comment_id)
          const matchClass = smart?.match_class || relevance.get(comment.comment_id)
          const why = smart ? { reason: smart.reason || "Related permit requirement and requested action.", important_difference: smart.important_difference || "" } : explanations.get(comment.comment_id)
          return <Card role="button" tabIndex={0} aria-pressed={comment.comment_id === activeId} className={`result-card cursor-pointer p-4 transition-colors hover:border-primary/40 ${comment.comment_id === activeId ? "border-primary bg-primary/[.035]" : ""}`} onClick={() => activateComment(comment.comment_id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activateComment(comment.comment_id) } }} key={comment.comment_id}>
            <div className="flex items-start gap-3"><Checkbox aria-label={`Select comment ${comment.comment_number}`} checked={selected.has(comment.comment_id)} onClick={(event) => event.stopPropagation()} onCheckedChange={(checked) => setSelected((current) => { const next = new Set(current); checked ? next.add(comment.comment_id) : next.delete(comment.comment_id); return next })} /><div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5"><span className="text-xs font-semibold tracking-wide text-foreground uppercase">{comment.discipline}</span>{matchClass && <Badge variant={matchClass === "unverified" ? "destructive" : "secondary"}>{matchClass === "direct" ? "Direct precedent" : matchClass === "related" ? "Related" : "Unverified"}</Badge>}<Badge variant="outline" className={comment.response ? "border-green-200 bg-green-50 text-green-800" : "border-amber-200 bg-amber-50 text-amber-900"}>{comment.response ? "Has response" : "No response"}</Badge>{((comment.issue_thread?.event_count || 0) > 2 || (threadCounts.get(comment.issue_thread?.thread_id || "") || 0) > 1) && <Badge className="border-teal-200 bg-teal-50 text-teal-900" variant="outline">Issue history</Badge>}</div>
              <p className="mt-2 line-clamp-3 text-sm leading-6">{comment.display_text}</p>
              <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground"><span>{comment.property_project}</span><span>{comment.city}</span><span>Round {comment.review_round}</span></div>
              {why && <Collapsible onClick={(event) => event.stopPropagation()} className="mt-2"><CollapsibleTrigger className="flex items-center gap-1 text-xs font-medium text-primary">Why it matched <ChevronDown className="size-3" /></CollapsibleTrigger><CollapsibleContent className="mt-2 rounded-md bg-muted/60 p-3 text-xs leading-5"><p>{why.reason}</p>{why.important_difference && <p className="mt-1 text-amber-800"><strong>Important difference:</strong> {why.important_difference}</p>}</CollapsibleContent></Collapsible>}
            </div></div>
          </Card>
        })}</div> : <div className="grid min-h-64 place-items-center p-8 text-center"><div><Search className="mx-auto size-8 text-muted-foreground" /><h3 className="mt-3 font-semibold">No comments found</h3><p className="mt-1 text-sm text-muted-foreground">Adjust the filters or try a broader search.</p></div></div>}
      </ScrollArea>
    </div>
    <CommentDetail comment={activeComment} threadMembers={activeThreadMembers} onOpenSource={onOpenSource} />
    <Dialog open={categoryOpen} onOpenChange={setCategoryOpen}><DialogContent><DialogHeader><DialogTitle>Assign a category</DialogTitle><DialogDescription>Organize {selected.size} selected comment{selected.size === 1 ? "" : "s"}. Original records remain unchanged.</DialogDescription></DialogHeader><div className="space-y-2"><Label htmlFor="category">Category name</Label><Input id="category" value={category} onChange={(event) => setCategory(event.target.value)} placeholder="e.g. Fire separation" /></div><DialogFooter><Button variant="outline" onClick={() => saveCategory("")}>Remove category</Button><Button disabled={!category.trim()} onClick={() => saveCategory(category.trim())}>Apply</Button></DialogFooter></DialogContent></Dialog>
  </section>
}
