import { CheckCircle2, ChevronDown, ExternalLink, FileText, Link2Off, MessageSquareMore, ShieldAlert } from "lucide-react"
import type { CommentRecord, IssueEvent, SourceReference, TextBlock } from "@/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"

function SourceButtons({ sources, onOpenSource }: { sources: SourceReference[]; onOpenSource: (id: string) => void }) {
  return <div className="flex min-w-0 flex-wrap gap-2">{(sources || []).map((source) => source.kind === "external" && source.url ? <Button asChild size="sm" variant="outline" className="h-auto max-w-full justify-start whitespace-normal py-2 text-left leading-5" key={source.url}><a href={source.url} target="_blank" rel="noreferrer"><span className="min-w-0 break-words">{source.relation}</span><ExternalLink className="shrink-0" /></a></Button> : <Button size="sm" variant="outline" className="h-auto max-w-full min-w-0 justify-start whitespace-normal py-2 text-left leading-5" onClick={() => onOpenSource(source.source_id)} key={source.source_id}><FileText className="shrink-0" /><span className="min-w-0 break-words">{source.relation}: {source.filename}</span></Button>)}</div>
}

function OrganizedText({ blocks, fallback }: { blocks?: TextBlock[]; fallback: string }) {
  if (!blocks?.length) return <p className="break-words whitespace-pre-wrap text-[15px] leading-7">{fallback}</p>
  return <div className="space-y-4">{blocks.map((block, index) => <section key={`${block.title}-${index}`}>{block.title && <h4 className="mb-1.5 text-sm font-semibold">{block.title}</h4>}{block.kind === "list" && block.items ? <ul className="list-disc space-y-1.5 pl-5 text-[15px] leading-7">{block.items.map((item) => <li className="break-words" key={item}>{item}</li>)}</ul> : <p className="break-words whitespace-pre-wrap text-[15px] leading-7">{block.text || fallback}</p>}</section>)}</div>
}

function EvidencePanel({ tone, title, status, text, blocks, sources, onOpenSource }: { tone: "comment" | "response"; title: string; status: React.ReactNode; text: string; blocks?: TextBlock[]; sources: SourceReference[]; onOpenSource: (id: string) => void }) {
  return <section aria-label={title} className={`evidence-panel ${tone} min-w-0 rounded-xl border p-4 shadow-xs sm:p-5 xl:p-6`}>
    <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{tone === "comment" ? "Government record" : "Company record"}</p><h3 className="mt-1 text-lg font-semibold">{title}</h3></div><div className="shrink-0">{status}</div></div>
    <div className={`mt-4 min-w-0 rounded-lg border p-4 sm:p-5 ${tone === "response" ? "border-teal-200/80 bg-white" : "bg-white"}`}><OrganizedText blocks={blocks} fallback={text} /></div>
    <div className="mt-4 min-w-0"><p className="mb-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">Source evidence</p><SourceButtons sources={sources} onOpenSource={onOpenSource} /></div>
  </section>
}

function IssueHistory({ events, rounds, status, onOpenSource }: { events: IssueEvent[]; rounds: string[]; status: React.ReactNode; onOpenSource: (id: string) => void }) {
  return <section aria-label="Issue history" className="rounded-xl border bg-card p-4 shadow-xs sm:p-5 xl:p-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-xs font-semibold tracking-wide text-primary uppercase">Single design issue</p><h3 className="mt-1 text-lg font-semibold">Comment and response history</h3><p className="mt-1 text-sm text-muted-foreground">Shown in chronological order so each attempted resolution and reviewer follow-up stays together.</p></div>
      <div className="flex flex-wrap gap-2"><Badge variant="secondary">{events.length} events</Badge>{rounds.length > 1 && <Badge variant="outline">{rounds.length} review rounds</Badge>}</div>
    </div>
    <div className="relative mt-5 space-y-3 before:absolute before:top-4 before:bottom-4 before:left-[11px] before:w-px before:bg-border">
      {events.map((event, index) => {
        const government = event.actor_role === "government"
        const current = event.event_type === "current_applicant_response"
        const tone = event.event_type === "reviewer_follow_up"
          ? "border-amber-200 bg-amber-50/70"
          : government
            ? "border-slate-200 bg-white"
            : "border-teal-200 bg-teal-50/60"
        return <article className="relative pl-9" key={event.event_id}>
          <span className={`absolute top-4 left-1.5 z-10 size-3 rounded-full border-2 border-white ${event.event_type === "reviewer_follow_up" ? "bg-amber-600" : government ? "bg-slate-600" : "bg-teal-700"}`} />
          <div className={`rounded-lg border p-4 ${tone}`}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold">{event.label}</span>
              {event.actor && <span className="text-xs text-muted-foreground">{event.actor}</span>}
              {event.occurred_at_label && <Badge variant="outline">{event.occurred_at_label}</Badge>}
              {event.review_round && <Badge variant="outline">Round {event.review_round}</Badge>}
              {current && <div className="ml-auto">{status}</div>}
            </div>
            <p className="mt-3 break-words whitespace-pre-wrap text-[15px] leading-7">{event.text}</p>
            {event.source && <div className="mt-3"><SourceButtons sources={[event.source]} onOpenSource={onOpenSource} /></div>}
          </div>
          {index === 0 && <span className="sr-only">Issue begins</span>}
        </article>
      })}
    </div>
  </section>
}

export function CommentDetail({ comment, threadMembers = [], onOpenSource }: { comment: CommentRecord | null; threadMembers?: CommentRecord[]; onOpenSource: (id: string) => void }) {
  if (!comment) return <div id="comment-detail-panel" className="comment-detail-shell grid min-h-[520px] place-items-center bg-muted/10 p-8 text-center"><div><FileText className="mx-auto size-9 text-muted-foreground" /><h3 className="mt-3 font-semibold">Select a historical comment</h3><p className="mt-1 max-w-sm text-sm text-muted-foreground">Choose a result to compare the government comment with the stored company response.</p></div></div>
  const confirmed = comment.response && (comment.link.review_status === "confirmed" || comment.response.human_review_status === "confirmed")
  const responseStatus = !comment.response ? <Badge className="border-amber-200 bg-amber-50 text-amber-900" variant="outline"><Link2Off />Missing response</Badge> : confirmed ? <Badge className="border-green-200 bg-green-50 text-green-800" variant="outline"><CheckCircle2 />Confirmed</Badge> : <Badge variant="destructive"><ShieldAlert />Unverified</Badge>
  const members = (threadMembers.length ? threadMembers : [comment]).slice().sort((left, right) => String(left.review_round).localeCompare(String(right.review_round), undefined, { numeric: true }))
  const eventIds = new Set<string>()
  const issueEvents = members.flatMap((member) => member.issue_thread?.events || []).filter((event) => {
    if (eventIds.has(event.event_id)) return false
    eventIds.add(event.event_id)
    return true
  })
  const issueRounds = [...new Set(members.map((member) => member.review_round).filter(Boolean))]
  const hasHistory = issueEvents.length > 2

  return <div id="comment-detail-panel" className="comment-detail-shell min-h-0 bg-slate-100/70" tabIndex={-1}>
    <header className="border-b border-teal-100 bg-teal-50 p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold tracking-wide text-primary uppercase">Comment-response detail</p><h2 className="mt-1 text-xl font-semibold">{comment.discipline} comment {comment.comment_number || ""}</h2></div><div className="flex flex-wrap gap-2">{hasHistory && <Badge className="border-teal-200 bg-teal-50 text-teal-900" variant="outline"><MessageSquareMore />Issue history</Badge>}<Badge variant="outline">{comment.category}</Badge></div></div>
      <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground"><span>{comment.property_project}</span><span>·</span><span>{comment.city}</span><span>·</span><span>Review round {comment.review_round}</span><span>·</span><span>{comment.reviewer || "Reviewer not recorded"}</span></div>
    </header>
    <div className="space-y-4 p-3 sm:p-4 xl:p-5">
      {hasHistory ? <IssueHistory events={issueEvents} rounds={issueRounds} status={responseStatus} onOpenSource={onOpenSource} /> : <>
        <EvidencePanel tone="comment" title="Government comment" status={<Badge variant="secondary">Original record</Badge>} text={comment.display_text} blocks={comment.display_blocks} sources={comment.sources} onOpenSource={onOpenSource} />
        {comment.response ? <EvidencePanel tone="response" title="Historical company response" status={responseStatus} text={comment.response.display_text} blocks={comment.response.display_blocks} sources={comment.response.sources} onOpenSource={onOpenSource} /> : <section className="grid min-h-64 place-items-center rounded-xl border border-amber-200 bg-amber-50/70 p-8 text-center"><div><Link2Off className="mx-auto size-8 text-amber-700" /><h3 className="mt-3 font-semibold">No response recorded</h3><p className="mt-1 max-w-sm text-sm text-muted-foreground">This comment is valid historical evidence, but its source contains no stored company response.</p><div className="mt-3">{responseStatus}</div></div></section>}
      </>}
      <Collapsible className="rounded-xl border bg-card px-4 py-3 sm:px-5"><CollapsibleTrigger className="flex w-full items-center justify-between gap-2 text-left text-sm font-medium text-muted-foreground"><span>Record details</span><ChevronDown className="size-4 shrink-0" /></CollapsibleTrigger><CollapsibleContent className="grid gap-3 pt-4 text-xs text-muted-foreground sm:grid-cols-2 xl:grid-cols-4"><div className="min-w-0"><strong className="block text-foreground">Comment source</strong><span className="break-words">{comment.source_filename}</span></div><div><strong className="block text-foreground">Location</strong>{comment.source_location}</div><div><strong className="block text-foreground">Extraction</strong><span className="break-words">{comment.extraction_method || "Not recorded"}</span></div><div><strong className="block text-foreground">Confidence</strong>{comment.extraction_confidence ? `${Math.round(Number(comment.extraction_confidence) * 100)}%` : "Not recorded"}</div></CollapsibleContent></Collapsible>
    </div>
  </div>
}
