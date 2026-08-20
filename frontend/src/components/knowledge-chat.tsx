import { useEffect, useMemo, useState } from "react"
import { AlertTriangle, ArrowLeft, ArrowRight, BookOpen, ChevronLeft, ChevronRight, Database, Expand, MessageSquareText, Minimize2, Sparkles } from "lucide-react"
import { api } from "@/lib/api"
import type { Citation, GuidedAction, KnowledgeAnswer, KnowledgeSourceOccurrence } from "@/types"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable"
import { Skeleton } from "@/components/ui/skeleton"
import { Conversation, ConversationContent, ConversationEmptyState, ConversationScrollButton } from "@/components/ai-elements/conversation"
import { Message, MessageContent, MessageResponse } from "@/components/ai-elements/message"
import { PromptInput, PromptInputBody, PromptInputFooter, PromptInputSubmit, PromptInputTextarea, PromptInputTools } from "@/components/ai-elements/prompt-input"
import { Suggestion, Suggestions } from "@/components/ai-elements/suggestion"
import { CanonicalEvidenceDetail, type CanonicalEvidenceRecord } from "@/components/canonical-evidence-detail"
import { ScrollArea } from "@/components/ui/scroll-area"

type ChatMessage = { id: string; role: "user" | "assistant"; text?: string; payload?: KnowledgeAnswer }
type EvidenceItem = NonNullable<KnowledgeAnswer["evidence"]>[number]
type EvidenceSelection = {
  messageId: string
  evidenceId: string
  sourceId: string
  citationIndex: number
  project?: string
  city?: string
  round?: string
  eventType?: string
  item: EvidenceItem
}

const suggestions = [
  "How have we handled tree-protection comments?",
  "How many historical comments concern door size?",
  "Summarize drainage comments and confirmed responses.",
  "Compare fire-separation comments across projects.",
]

const evidenceLevelLabels: Record<number, string> = {
  1: "Comment only", 2: "Response stated", 3: "Concrete revision cited", 4: "Later review confirmed",
}

function useDesktopWorkspace() {
  const [desktop, setDesktop] = useState(() => window.matchMedia("(min-width: 1024px)").matches)
  useEffect(() => {
    const query = window.matchMedia("(min-width: 1024px)")
    const update = () => setDesktop(query.matches)
    update()
    query.addEventListener("change", update)
    return () => query.removeEventListener("change", update)
  }, [])
  return desktop
}

function evidenceSources(item: EvidenceItem): KnowledgeSourceOccurrence[] {
  const structured = item.source_occurrences?.filter((source) => source.source_id) || []
  if (structured.length) return structured.filter((source, index, items) => items.findIndex((candidate) => candidate.source_id === source.source_id) === index)
  const known = [
    item.response_source_id && { source_id: item.response_source_id, role: "response", label: "Response source" },
    item.comment_source_id && { source_id: item.comment_source_id, role: "comment", label: "Comment source" },
    item.later_review_source_id && { source_id: item.later_review_source_id, role: "later_review", label: "Later review source" },
    ...(item.source_ids || []).map((sourceId) => ({ source_id: sourceId, label: "Additional source" })),
  ].filter(Boolean) as KnowledgeSourceOccurrence[]
  return known.filter((source, index, items) => items.findIndex((candidate) => candidate.source_id === source.source_id) === index)
}

function primarySourceId(item: EvidenceItem) {
  return item.primary_source_occurrence_id || item.response_source_id || item.comment_source_id || item.later_review_source_id || evidenceSources(item)[0]?.source_id || null
}

function citationSelection(messageId: string, item: EvidenceItem, index: number, sourceId?: string): EvidenceSelection | null {
  const selectedSourceId = sourceId || primarySourceId(item)
  if (!selectedSourceId) return null
  return {
    messageId,
    evidenceId: item.event_id || `${messageId}-evidence-${index}`,
    sourceId: selectedSourceId,
    citationIndex: index + 1,
    project: item.project,
    city: item.city,
    round: item.round,
    eventType: item.response_summary ? "Applicant response evidence" : "Government comment evidence",
    item,
  }
}

function stableCitationSelection(messageId: string, citation: Citation, item?: EvidenceItem): EvidenceSelection | null {
  const sourceId = citation.primary_source_occurrence_id || citation.source_id
  if (!sourceId) return null
  const citationIndex = citation.citation_index || 1
  return {
    messageId,
    evidenceId: citation.evidence_id || item?.event_id || citation.citation_id || `${messageId}-citation-${citationIndex}`,
    sourceId,
    citationIndex,
    project: item?.project,
    city: item?.city,
    round: item?.round,
    eventType: citation.role === "response" || item?.response_summary ? "Applicant response evidence" : citation.role === "later_review" ? "Later reviewer evidence" : "Government comment evidence",
    item: item || { event_id: citation.evidence_id, claim: citation.label, source_occurrences: [{ source_id: sourceId, label: citation.label, role: citation.role }] },
  }
}

function evidenceForCitation(citation: Citation, evidence: EvidenceItem[]) {
  if (citation.evidence_id) {
    const exact = evidence.find((candidate) => candidate.event_id === citation.evidence_id || candidate.comment_id === citation.evidence_id)
    if (exact) return exact
  }
  const sourceId = citation.primary_source_occurrence_id || citation.source_id
  return evidence.find((candidate) => evidenceSources(candidate).some((source) => source.source_id === sourceId))
    || (evidence.length === 1 ? evidence[0] : undefined)
}

function canonicalEvidenceRecord(selection: EvidenceSelection): CanonicalEvidenceRecord {
  const item = selection.item
  const sources = evidenceSources(item).map((source) => ({
    sourceId: source.source_id,
    filename: source.filename,
    label: source.label,
    relation: source.relation,
    role: source.role,
    primary: source.source_id === selection.sourceId,
  }))
  const level = item.evidence_level || 1
  const statusLabel = item.evidence_badge || evidenceLevelLabels[level] || "Historical evidence"
  const statusTone: CanonicalEvidenceRecord["statusTone"] = level >= 4
    ? "confirmed"
    : item.response_excerpt || item.response_summary
      ? "response"
      : level <= 1
        ? "missing"
        : "unverified"
  return {
    title: item.project || item.city || "Historical project",
    issueLabel: item.issue_label || item.topic_label || item.claim || item.summary,
    city: item.city,
    round: item.round,
    statusLabel,
    statusTone,
    sections: [
      ...(item.comment_excerpt || item.reviewer_summary ? [{ kind: "comment" as const, title: "Reviewer comment", text: item.comment_excerpt || item.reviewer_summary || "" }] : []),
      ...(item.response_excerpt || item.response_summary ? [{ kind: "response" as const, title: "Applicant response", text: item.response_excerpt || item.response_summary || "" }] : []),
      ...(item.later_review_excerpt ? [{ kind: "followup" as const, title: "Later reviewer follow-up", text: item.later_review_excerpt }] : []),
    ],
    sources,
    primarySourceId: selection.sourceId || primarySourceId(item),
  }
}

function AnswerMessage({ messageId, payload, expanded, selectedEvidenceId, onOpenEvidence, onOpenResults, onGuidedAction }: {
  messageId: string
  payload: KnowledgeAnswer
  expanded: boolean
  selectedEvidenceId?: string
  onOpenEvidence: (selection: EvidenceSelection) => void
  onOpenResults: (id: string) => void
  onGuidedAction: (action: GuidedAction) => void
}) {
  const metrics = payload.metrics || {}
  const unverified = payload.query_plan?.evidence_scope === "literal_unverified"
  const conversational = payload.answer_type === "GENERAL_CONVERSATION" || payload.intent === "general_conversation"
  const grounded = conversational
    || payload.validation_status === "validated"
    || payload.validation_status === "not_required"
    || (!payload.validation_status && !unverified)
  const answer = payload.answer || payload.direct_answer?.join("\n\n") || "I couldn't find a verified answer in the selected history."
  const evidence = payload.representative_evidence || payload.evidence || []
  const coverage = payload.coverage
  const showEvidence = grounded && evidence.length > 0
  const commentCount = coverage?.comment_count ?? Number(metrics.parent_comments || 0)
  const issueCount = coverage?.issue_count ?? Number(metrics.canonical_issues || 0)
  const projectCount = coverage?.project_count ?? Number(metrics.projects || 0)
  const roundCount = coverage?.round_count ?? Number(metrics.review_rounds || 0)
  const responseCount = coverage?.confirmed_response_count ?? Number(metrics.confirmed_responses || 0)
  const missingCount = coverage?.missing_response_count ?? Number(metrics.missing_responses || 0)
  const structuredCitations = (payload.citations || []).filter((citation) => citation.source_id || citation.primary_source_occurrence_id)
  const citations = structuredCitations.length
    ? structuredCitations.map((citation) => {
        const item = evidenceForCitation(citation, evidence)
        return stableCitationSelection(messageId, citation, item)
      }).filter(Boolean) as EvidenceSelection[]
    : evidence.map((item, index) => citationSelection(messageId, item, index)).filter(Boolean) as EvidenceSelection[]
  const citationsByIndex = new Map(citations.map((citation) => [citation.citationIndex, citation]))
  const citedAnswer = answer.replace(/\[(\d+)\](?!\()/g, (marker, rawIndex: string) => {
    const index = Number(rawIndex)
    if (!citationsByIndex.has(index)) return marker
    return `[[${index}]](#citation-${index})`
  })

  function openCitation(citation: EvidenceSelection) {
    onOpenEvidence(citation)
  }

  function handleAnswerClick(event: React.MouseEvent<HTMLElement>) {
    const target = event.target as HTMLElement
    const link = target.closest<HTMLAnchorElement>('a[href^="#citation-"]')
    if (!link) return
    const index = Number(link.getAttribute("href")?.replace("#citation-", ""))
    const citation = citationsByIndex.get(index)
    if (!citation) return
    event.preventDefault()
    openCitation(citation)
  }

  function citationIndexFor(item: EvidenceItem, fallbackIndex: number) {
    const itemSources = new Set(evidenceSources(item).map((source) => source.source_id))
    const citation = structuredCitations.find((candidate) =>
      (candidate.evidence_id && candidate.evidence_id === item.event_id)
      || itemSources.has(candidate.primary_source_occurrence_id || candidate.source_id),
    )
    return citation?.citation_index || fallbackIndex + 1
  }

  function open(item: EvidenceItem, index: number, sourceId?: string) {
    const selection = citationSelection(messageId, item, index, sourceId)
    if (!selection) return
    onOpenEvidence(selection)
  }

  return <MessageContent className="w-full rounded-xl border bg-card p-5 shadow-xs">
    <div className="mb-4 flex flex-wrap items-center justify-between gap-2 border-b pb-3">
      <div className="flex items-center gap-2"><Sparkles className="size-4 text-primary" /><span className="font-semibold">Permit History assistant</span></div>
      {!grounded && <Badge variant="destructive">No validated evidence</Badge>}
    </div>
    <div className="space-y-5">
      <section className="answer-primary prose prose-sm max-w-none leading-7 dark:prose-invert [&_a[href^='#citation-']]:rounded [&_a[href^='#citation-']]:border [&_a[href^='#citation-']]:border-primary/25 [&_a[href^='#citation-']]:bg-primary/5 [&_a[href^='#citation-']]:px-1 [&_a[href^='#citation-']]:font-semibold [&_a[href^='#citation-']]:text-primary [&_a[href^='#citation-']]:no-underline hover:[&_a[href^='#citation-']]:bg-primary/10" onClick={handleAnswerClick}>
        <MessageResponse>{citedAnswer}</MessageResponse>
      </section>
      {payload.answer_type === "TIMELINE" && issueCount > 0
        ? <p className="text-xs text-muted-foreground">
            Based on <strong>{issueCount}</strong> recurring {issueCount === 1 ? "issue" : "issues"} · <strong>{projectCount}</strong> {projectCount === 1 ? "project" : "projects"} · <strong>{roundCount}</strong> review {roundCount === 1 ? "round" : "rounds"}.
          </p>
        : commentCount > 0 && <p className="text-xs text-muted-foreground">
            Based on <strong>{commentCount}</strong> relevant {commentCount === 1 ? "comment" : "comments"} · <strong>{projectCount}</strong> {projectCount === 1 ? "project" : "projects"} · <strong>{roundCount}</strong> review {roundCount === 1 ? "round" : "rounds"}.
            {responseCount > 0 && <> <strong>{responseCount}</strong> confirmed {responseCount === 1 ? "response" : "responses"} for {responseCount} {responseCount === 1 ? "comment" : "comments"}.</>}
            {missingCount > 0 && responseCount > 0 && <> {missingCount} without a confirmed response.</>}
          </p>}
      {showEvidence && <section className="space-y-3 border-t pt-4">
        <div className="flex items-center justify-between gap-3"><h4 className="text-sm font-semibold text-primary">Supporting sources</h4><span className="text-xs text-muted-foreground">{evidence.length} {evidence.length === 1 ? "record" : "records"}</span></div>
        <div className={expanded ? "grid gap-3 xl:grid-cols-2" : "grid gap-3 sm:grid-cols-2"}>
          {evidence.map((item, index) => {
            const sources = evidenceSources(item)
            const primaryId = primarySourceId(item)
            const displayCitationIndex = citationIndexFor(item, index)
            const evidenceId = item.event_id || `${messageId}-evidence-${index}`
            const selected = selectedEvidenceId === evidenceId
            return <article aria-current={selected ? "true" : undefined} className={`rounded-lg border p-3 transition-colors ${selected ? "border-primary bg-primary/5 ring-1 ring-primary/20" : "bg-muted/15"}`} id={`evidence-${item.event_id || index}`} key={`${item.event_id || item.comment_source_id || "evidence"}-${index}`}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0"><p className="text-sm font-medium text-foreground"><span className="mr-1 text-primary">[{displayCitationIndex}]</span>{item.project || item.city || "Historical project"}</p>{(item.issue_label || item.topic_label) && <p className="mt-1 line-clamp-2 text-xs font-medium text-foreground/80">{item.issue_label || item.topic_label}</p>}<p className="mt-0.5 text-xs text-muted-foreground">{[item.city, item.round ? `Round ${item.round}` : ""].filter(Boolean).join(" · ")}</p></div>
                <Badge variant="outline" className="text-[11px]">{item.evidence_badge || evidenceLevelLabels[item.evidence_level || 1] || "Historical evidence"}</Badge>
              </div>
              {item.reviewer_summary && <p className="mt-3 line-clamp-3 text-sm leading-5"><span className="font-medium">Reviewer: </span>{item.reviewer_summary}</p>}
              {item.response_summary && <p className="mt-2 line-clamp-3 text-sm leading-5 text-muted-foreground"><span aria-hidden="true">→ </span><span className="font-medium">Response: </span>{item.response_summary}</p>}
              <div className="mt-3 flex flex-wrap items-center gap-2 border-t pt-2"><Button variant="outline" size="sm" className="h-8" onClick={() => open(item, index, primaryId || undefined)}><BookOpen />View evidence</Button>{sources.length > 1 && <Badge variant="secondary">{sources.length} sources</Badge>}</div>
            </article>
          })}
        </div>
      </section>}
      {!!payload.limitations?.length && <section className="space-y-1 border-t pt-3 text-xs leading-5 text-muted-foreground">{payload.limitations.map((limitation) => <p key={limitation}>{limitation}</p>)}</section>}
      {!conversational && <details className="rounded-lg border bg-muted/15 px-3 py-2 text-sm">
        <summary className="cursor-pointer font-medium text-muted-foreground">Retrieval diagnostics</summary>
        <div className="mt-3 space-y-3 text-xs text-muted-foreground">
          {payload.retrieval?.stage ? <p>Stage {payload.retrieval.stage}{payload.retrieval.coverage?.event_count !== undefined ? ` · ${payload.retrieval.coverage.event_count} validated events` : ""}{payload.retrieval.coverage?.project_count !== undefined ? ` · ${payload.retrieval.coverage.project_count} projects` : ""}</p> : null}
          {payload.retrieval?.fallback_reason && <p>{payload.retrieval.fallback_reason}</p>}
          {(payload.warnings || []).map((warning) => <p key={warning}>{warning}</p>)}
        </div>
      </details>}
      {conversational && !!payload.suggested_followups?.length && <section className="space-y-2 border-t pt-3"><h4 className="text-xs font-semibold tracking-wide text-primary uppercase">You could also ask</h4><div className="flex flex-wrap gap-2">{payload.suggested_followups.map((question) => <Button variant="outline" size="sm" onClick={() => onGuidedAction({ type: "general_followup", label: question, result_set_id: "" })} key={question}>{question}<ArrowRight /></Button>)}</div></section>}
      {!!payload.actions?.some((action) => action.type !== "show_results") && <section className="space-y-2 border-t pt-3"><h4 className="text-xs font-semibold tracking-wide text-primary uppercase">Explore next</h4><div className="flex flex-wrap gap-2">{(payload.actions || []).filter((action) => action.type !== "show_results").map((action) => <Button variant="outline" size="sm" onClick={() => onGuidedAction(action)} key={`${action.type}-${action.label}`} aria-label={action.label}>{action.label}<ArrowRight /></Button>)}</div></section>}
      {(payload.actions || []).filter((action) => action.type === "show_results").map((action) => <Button onClick={() => onOpenResults(action.result_set_id)} key={`results-${action.result_set_id}`}>{action.label}<ArrowRight /></Button>)}
    </div>
  </MessageContent>
}

export function KnowledgeChat({ city, filters, onOpenSource, onOpenResults, sourceViewerOpen = false }: {
  city: string
  filters: Record<string, string>
  onOpenSource: (id: string) => void
  onOpenResults: (id: string) => void
  sourceViewerOpen?: boolean
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [conversationId, setConversationId] = useState<string>()
  const [previousResultSetId, setPreviousResultSetId] = useState<string>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  const [expanded, setExpanded] = useState(false)
  const [draft, setDraft] = useState("")
  const [selectedEvidence, setSelectedEvidence] = useState<EvidenceSelection | null>(null)
  const desktopWorkspace = useDesktopWorkspace()

  const navigableEvidence = useMemo(() => messages.flatMap((message) => {
    if (!message.payload) return []
    const evidence = message.payload.representative_evidence || message.payload.evidence || []
    const structuredCitations = (message.payload.citations || []).filter((citation) => citation.source_id || citation.primary_source_occurrence_id)
    if (structuredCitations.length) {
      return structuredCitations.map((citation) => {
        const item = evidenceForCitation(citation, evidence)
        return stableCitationSelection(message.id, citation, item)
      }).filter(Boolean) as EvidenceSelection[]
    }
    return evidence.map((item, index) => citationSelection(message.id, item, index)).filter(Boolean) as EvidenceSelection[]
  }), [messages])
  const canonicalNavigableEvidence = useMemo(() => navigableEvidence.filter((item, index, all) => all.findIndex((candidate) => candidate.messageId === item.messageId && candidate.evidenceId === item.evidenceId) === index), [navigableEvidence])
  const selectedIndex = selectedEvidence ? canonicalNavigableEvidence.findIndex((item) => item.messageId === selectedEvidence.messageId && item.evidenceId === selectedEvidence.evidenceId) : -1

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape" || !expanded || sourceViewerOpen) return
      event.preventDefault()
      if (selectedEvidence) setSelectedEvidence(null)
      else setExpanded(false)
    }
    document.addEventListener("keydown", handleKeyDown)
    return () => document.removeEventListener("keydown", handleKeyDown)
  }, [expanded, selectedEvidence, sourceViewerOpen])

  async function ask(question: string, guidedAction?: GuidedAction, startFresh = false) {
    const text = question.trim()
    if (!text || loading) return
    setDraft("")
    setMessages((current) => [...current, { id: crypto.randomUUID(), role: "user", text }])
    setLoading(true); setError("")
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 120_000)
    try {
      const payload = await api<KnowledgeAnswer>("/api/knowledge-chat", { method: "POST", signal: controller.signal, body: JSON.stringify({ conversation_id: conversationId, message: text, city_id: city, filters, previous_result_set_id: startFresh ? undefined : (guidedAction?.result_set_id || previousResultSetId), guided_action: guidedAction || undefined }) })
      setConversationId(payload.conversation_id)
      if (payload.result_set_id) setPreviousResultSetId(payload.result_set_id)
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: "assistant", payload }])
    } catch (reason) {
      setError((reason as Error).name === "AbortError" ? "Evidence verification exceeded two minutes. No unverified candidates were used as an answer; please retry when Gemini recovers." : (reason as Error).message)
    } finally { window.clearTimeout(timeout); setLoading(false) }
  }

  function navigateEvidence(direction: -1 | 1) {
    if (!canonicalNavigableEvidence.length) return
    const base = selectedIndex >= 0 ? selectedIndex : 0
    const next = (base + direction + canonicalNavigableEvidence.length) % canonicalNavigableEvidence.length
    setSelectedEvidence(canonicalNavigableEvidence[next])
  }

  function selectEvidence(selection: EvidenceSelection) {
    setSelectedEvidence(selection)
    setExpanded(true)
  }

  const evidenceNavigation = selectedEvidence ? <div className="flex items-center gap-1"><Button variant="ghost" size="icon-sm" onClick={() => navigateEvidence(-1)} aria-label="Previous evidence"><ChevronLeft /></Button><span className="whitespace-nowrap text-xs text-muted-foreground">Evidence {Math.max(selectedIndex + 1, 1)} of {canonicalNavigableEvidence.length}</span><Button variant="ghost" size="icon-sm" onClick={() => navigateEvidence(1)} aria-label="Next evidence"><ChevronRight /></Button></div> : null
  const selectedRecord = selectedEvidence ? canonicalEvidenceRecord(selectedEvidence) : null

  const chatHeader = <div className="flex items-start justify-between gap-4 border-b px-6 py-4">
    <div><div className="mb-1 flex items-center gap-2 text-sm font-semibold text-primary"><Database className="size-4" />Conversational knowledge explorer</div><h2 className="text-xl font-semibold tracking-tight">Ask Permit History</h2><p className="mt-1 text-sm text-muted-foreground">Ask a general question or explore verified permit history with exact source evidence.</p></div>
    <div className="flex shrink-0 items-center gap-2"><Badge variant="outline" className="hidden sm:inline-flex">{city || "All cities"}</Badge>{expanded ? <Button variant="outline" size="sm" onClick={() => { setSelectedEvidence(null); setExpanded(false) }} aria-label="Collapse AI workspace"><Minimize2 />Collapse</Button> : <Button variant="outline" size="sm" onClick={() => setExpanded(true)} aria-label="Expand AI workspace"><Expand />Focus mode</Button>}</div>
  </div>

  const chatBody = <>
    <Conversation className={expanded ? "min-h-0 flex-1 bg-muted/15" : "h-[460px] bg-muted/15"}>
      <ConversationContent className="mx-auto w-full max-w-[1080px] gap-5 p-6">
        {!messages.length && <ConversationEmptyState icon={<MessageSquareText className="size-8" />} title="Ask Permit History" description="Start with a general permit question, search precedents, compare projects, or calculate exact counts." />}
        {messages.map((message) => <Message from={message.role} key={message.id}>{message.role === "user" ? <MessageContent>{message.text}</MessageContent> : message.payload && <AnswerMessage messageId={message.id} payload={message.payload} expanded={expanded} selectedEvidenceId={selectedEvidence?.messageId === message.id ? selectedEvidence.evidenceId : undefined} onOpenEvidence={selectEvidence} onOpenResults={onOpenResults} onGuidedAction={(action) => ask(String(action.parameters?.query || action.label), action.type === "general_followup" ? undefined : action, action.type === "general_followup")} />}</Message>)}
        {loading && <Message from="assistant"><MessageContent className="w-full space-y-3 rounded-xl border bg-card p-5"><div className="flex items-center gap-2 text-sm text-muted-foreground"><Sparkles className="size-4" />Thinking…</div><Skeleton className="h-4 w-5/6" /><Skeleton className="h-4 w-3/4" /><Skeleton className="h-20 w-full" /></MessageContent></Message>}
        {error && <Alert variant="destructive"><AlertTriangle /><AlertTitle>Could not answer</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
      </ConversationContent><ConversationScrollButton />
    </Conversation>
    <div className="border-t bg-card px-5 py-4">
      <PromptInput onSubmit={({ text }) => ask(text)}>
        <PromptInputBody><PromptInputTextarea value={draft} onChange={(event) => setDraft(event.target.value)} aria-label="Ask Permit History" className={expanded ? "min-h-16 text-sm" : "min-h-20 text-sm"} placeholder="Ask about permit history or a general permit question…" /></PromptInputBody>
        <PromptInputFooter><PromptInputTools><span className="px-2 text-xs text-muted-foreground">General help · verified sources when history is used</span></PromptInputTools><PromptInputSubmit disabled={loading} status={loading ? "submitted" : "ready"} /></PromptInputFooter>
      </PromptInput>
      {!messages.length && <Suggestions className="mt-3">{suggestions.map((item) => <Suggestion suggestion={item} onClick={ask} key={item} />)}</Suggestions>}
    </div>
  </>

  const chatPane = <div className="flex size-full min-h-0 flex-col overflow-hidden">{chatHeader}{chatBody}</div>

  return <>
    {!expanded && <Card className="knowledge-chat overflow-hidden border-border/80 shadow-sm">{chatPane}</Card>}
    {/* Radix modal dialogs must not compete for focus. While the app-level
        source viewer is open, temporarily remove this fullscreen dialog but
        retain its conversation/evidence state. Closing the viewer restores
        the user to the exact same AI workspace. */}
    <Dialog open={expanded && !sourceViewerOpen} onOpenChange={(open) => {
      if (sourceViewerOpen) return
      setExpanded(open)
      if (!open) setSelectedEvidence(null)
    }}>
      <DialogContent className="ai-workspace flex h-[92vh] max-h-[92vh] w-[92vw] max-w-[92vw] flex-col gap-0 overflow-hidden p-0" onEscapeKeyDown={(event) => { event.preventDefault(); if (selectedEvidence) setSelectedEvidence(null); else setExpanded(false) }}>
        <DialogHeader className="sr-only"><DialogTitle>Permit History AI research workspace</DialogTitle><DialogDescription>Conversation and Library-style historical evidence in one workspace.</DialogDescription></DialogHeader>
        {selectedEvidence && desktopWorkspace ? <ResizablePanelGroup direction="horizontal" className="min-h-0 flex-1">
          <ResizablePanel defaultSize={60} minSize={40}><div className="size-full overflow-hidden">{chatPane}</div></ResizablePanel>
          <ResizableHandle withHandle />
          <ResizablePanel defaultSize={40} minSize={30}><ScrollArea className="size-full">{selectedRecord && <CanonicalEvidenceDetail record={selectedRecord} onOpenSource={onOpenSource} onClose={() => setSelectedEvidence(null)} navigation={evidenceNavigation} />}</ScrollArea></ResizablePanel>
        </ResizablePanelGroup> : !selectedEvidence ? chatPane : null}
        {selectedEvidence && !desktopWorkspace && <div className="flex min-h-0 flex-1 flex-col"><div className="border-b p-3"><Button variant="ghost" size="sm" onClick={() => setSelectedEvidence(null)}><ArrowLeft />Back to answer</Button></div><ScrollArea className="min-h-0 flex-1">{selectedRecord && <CanonicalEvidenceDetail record={selectedRecord} onOpenSource={onOpenSource} navigation={evidenceNavigation} />}</ScrollArea></div>}
      </DialogContent>
    </Dialog>
  </>
}
