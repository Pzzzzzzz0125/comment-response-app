import { useState } from "react"
import { AlertTriangle, ArrowRight, BookOpen, Database, MessageSquareText, Sparkles } from "lucide-react"
import { api } from "@/lib/api"
import type { KnowledgeAnswer } from "@/types"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Conversation, ConversationContent, ConversationEmptyState, ConversationScrollButton } from "@/components/ai-elements/conversation"
import { Message, MessageContent, MessageResponse } from "@/components/ai-elements/message"
import { PromptInput, PromptInputBody, PromptInputFooter, PromptInputSubmit, PromptInputTextarea, PromptInputTools } from "@/components/ai-elements/prompt-input"
import { Source, Sources, SourcesContent, SourcesTrigger } from "@/components/ai-elements/sources"
import { Suggestion, Suggestions } from "@/components/ai-elements/suggestion"

type ChatMessage = { id: string; role: "user" | "assistant"; text?: string; payload?: KnowledgeAnswer }

const suggestions = [
  "How have we handled tree-protection comments?",
  "How many historical comments concern door size?",
  "Summarize drainage comments and confirmed responses.",
  "Compare fire-separation comments across projects.",
]

const sectionOrder = [
  ["historical_pattern", "Direct answer"],
  ["database_result", "Historical coverage"],
  ["data_limitation", "Limitations"],
] as const

const intentLabels: Record<string, string> = {
  aggregate_count: "Database count", topic_summary: "History summary", historical_response_summary: "Response history",
  precedent_search: "Precedent search", compare_groups: "Comparison", filter_previous_results: "Follow-up",
  explain_selected_comment: "Selected record", database_exploration: "Database analysis", unsupported_or_ambiguous: "Clarification needed",
}

function AnswerMessage({ payload, onOpenSource, onOpenResults }: { payload: KnowledgeAnswer; onOpenSource: (id: string) => void; onOpenResults: (id: string) => void }) {
  const metrics = payload.metrics || {}
  const unverified = payload.query_plan?.evidence_scope === "literal_unverified"
  const databaseScope = payload.query_plan?.operations?.includes("load_filtered_comments") || payload.intent === "aggregate_count"
  const sections = payload.answer_sections || {}
  const entries = sectionOrder.filter(([key]) => sections[key])

  return <MessageContent className="w-full rounded-xl border bg-card p-5 shadow-xs">
    <div className="mb-4 flex flex-wrap items-center justify-between gap-2 border-b pb-3">
      <div className="flex items-center gap-2"><Sparkles className="size-4 text-primary" /><span className="font-semibold">Permit History</span><span className="text-xs text-muted-foreground">{intentLabels[payload.intent || ""] || "Grounded answer"}</span></div>
      <Badge variant={unverified ? "destructive" : "secondary"}>{unverified ? "Unverified matches" : databaseScope ? "Database calculated" : "Verified evidence"}</Badge>
    </div>
    <div className="space-y-5">
      {entries.length ? entries.map(([key, label], index) => {
        const hideCleanLimitation = key === "data_limitation" && Number(metrics.missing_responses || 0) === 0 && Number(metrics.unconfirmed_responses || 0) === 0
        if (hideCleanLimitation) return null
        return <section key={key} className={index === 0 ? "answer-primary" : "answer-supporting"}>
          <h4>{label}</h4><MessageResponse>{String(sections[key]).replace(/^[^:]+:\s*/, "")}</MessageResponse>
        </section>
      }) : <MessageResponse>{payload.answer}</MessageResponse>}
      {Number(metrics.parent_comments || 0) > 0 && <section>
        <h4 className="mb-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">At a glance</h4>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
          {[["parent_comments", "Comments"], ["projects", "Projects"], ["review_rounds", "Rounds"], ["confirmed_responses", "Confirmed"], ["missing_responses", "Missing"]].map(([key, label]) => metrics[key] === undefined ? null : <div className="rounded-lg border bg-muted/35 p-3" key={key}><strong className="block text-xl text-primary">{metrics[key]}</strong><span className="text-[11px] text-muted-foreground">{label}</span></div>)}
        </div>
      </section>}
      {(payload.warnings || []).map((warning) => <Alert className="border-amber-200 bg-amber-50 text-amber-950" key={warning}><AlertTriangle /><AlertTitle>Evidence note</AlertTitle><AlertDescription>{warning}</AlertDescription></Alert>)}
      {!!payload.citations?.length && <Sources className="mb-0 text-primary">
        <SourcesTrigger count={payload.citations.length} />
        <SourcesContent className="w-full">
          {payload.citations.map((citation) => <Source href="#" title={citation.label} onClick={(event) => { event.preventDefault(); onOpenSource(citation.source_id) }} key={citation.source_id} className="rounded-md border bg-white px-3 py-2 hover:bg-muted"><BookOpen className="size-4" /><span className="font-medium">{citation.label.replace("Comment source · ", "Comment · ").replace("Response source · ", "Response · ")}</span></Source>)}
        </SourcesContent>
      </Sources>}
      {(payload.actions || []).map((action) => action.type === "show_results" ? <Button onClick={() => onOpenResults(action.result_set_id)} key={action.result_set_id}>{action.label}<ArrowRight /></Button> : null)}
    </div>
  </MessageContent>
}

export function KnowledgeChat({ city, filters, onOpenSource, onOpenResults }: { city: string; filters: Record<string, string>; onOpenSource: (id: string) => void; onOpenResults: (id: string) => void }) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [conversationId, setConversationId] = useState<string>()
  const [previousResultSetId, setPreviousResultSetId] = useState<string>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")

  async function ask(question: string) {
    const text = question.trim()
    if (!text || loading) return
    setMessages((current) => [...current, { id: crypto.randomUUID(), role: "user", text }])
    setLoading(true); setError("")
    try {
      const payload = await api<KnowledgeAnswer>("/api/knowledge-chat", { method: "POST", body: JSON.stringify({ conversation_id: conversationId, message: text, city_id: city, filters, previous_result_set_id: previousResultSetId }) })
      setConversationId(payload.conversation_id)
      if (payload.result_set_id) setPreviousResultSetId(payload.result_set_id)
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: "assistant", payload }])
    } catch (reason) {
      setError((reason as Error).message)
    } finally { setLoading(false) }
  }

  return <Card className="knowledge-chat overflow-hidden border-border/80 shadow-sm">
    <div className="flex items-start justify-between gap-4 border-b px-6 py-5">
      <div><div className="mb-1 flex items-center gap-2 text-sm font-semibold text-primary"><Database className="size-4" />Conversational knowledge explorer</div><h2 className="text-xl font-semibold tracking-tight">Ask Permit History</h2><p className="mt-1 text-sm text-muted-foreground">Answers stay grounded in stored records and open directly to their source evidence.</p></div>
      <Badge variant="outline" className="shrink-0">{city || "All cities"}</Badge>
    </div>
    <Conversation className="h-[460px] bg-muted/15">
      <ConversationContent className="mx-auto w-full max-w-4xl gap-5 p-6">
        {!messages.length && <ConversationEmptyState icon={<MessageSquareText className="size-8" />} title="Ask a question about permit history" description="Search precedents, compare projects, summarize common requirements, or calculate exact counts." />}
        {messages.map((message) => <Message from={message.role} key={message.id}>
          {message.role === "user" ? <MessageContent>{message.text}</MessageContent> : message.payload && <AnswerMessage payload={message.payload} onOpenSource={onOpenSource} onOpenResults={onOpenResults} />}
        </Message>)}
        {loading && <Message from="assistant"><MessageContent className="w-full space-y-3 rounded-xl border bg-card p-5"><div className="flex items-center gap-2 text-sm text-muted-foreground"><Sparkles className="size-4" />Searching verified history…</div><Skeleton className="h-4 w-5/6" /><Skeleton className="h-4 w-3/4" /><Skeleton className="h-20 w-full" /></MessageContent></Message>}
        {error && <Alert variant="destructive"><AlertTriangle /><AlertTitle>Could not answer</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
      </ConversationContent>
      <ConversationScrollButton />
    </Conversation>
    <div className="border-t bg-card px-5 py-4">
      <PromptInput onSubmit={({ text }) => ask(text)}>
        <PromptInputBody><PromptInputTextarea aria-label="Ask Permit History" className="min-h-20 text-sm" placeholder="Ask about historical comments, confirmed responses, exact counts, or comparisons…" /></PromptInputBody>
        <PromptInputFooter><PromptInputTools><span className="px-2 text-xs text-muted-foreground">Gemini-assisted · source grounded</span></PromptInputTools><PromptInputSubmit disabled={loading} status={loading ? "submitted" : "ready"} /></PromptInputFooter>
      </PromptInput>
      <Suggestions className="mt-3">{suggestions.map((item) => <Suggestion suggestion={item} onClick={ask} key={item} />)}</Suggestions>
    </div>
  </Card>
}
