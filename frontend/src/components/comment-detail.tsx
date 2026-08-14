import { CheckCircle2, ChevronDown, Clock3, ExternalLink, FileText, Link2Off, MessageSquareMore, ShieldAlert } from "lucide-react"
import type { CommentRecord, IssueEvent, SourceReference, TextBlock } from "@/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { CanonicalEvidenceDetail, type CanonicalEvidenceRecord } from "@/components/canonical-evidence-detail"

function sourceIdentity(source: SourceReference) {
  if (source.kind === "external" && source.url) return `url:${source.url}`
  return source.location?.document_id ? `document:${source.location.document_id}` : `filename:${source.filename.trim().toLocaleLowerCase()}`
}

function uniqueSources(sources: SourceReference[]) {
  const seen = new Set<string>()
  return (sources || []).filter((source) => {
    const key = sourceIdentity(source)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function SourceButtons({ sources, onOpenSource }: { sources: SourceReference[]; onOpenSource: (id: string) => void }) {
  return <div className="flex min-w-0 flex-wrap gap-2">{uniqueSources(sources).map((source) => source.kind === "external" && source.url ? <Button asChild size="sm" variant="outline" className="h-auto max-w-full justify-start whitespace-normal py-2 text-left leading-5" key={source.url}><a href={source.url} target="_blank" rel="noreferrer"><span className="min-w-0 break-words">{source.relation}</span><ExternalLink className="shrink-0" /></a></Button> : <Button size="sm" variant="outline" className="h-auto max-w-full min-w-0 justify-start whitespace-normal py-2 text-left leading-5" onClick={() => onOpenSource(source.source_id)} key={source.source_id}><FileText className="shrink-0" /><span className="min-w-0 break-words">{source.relation}: {source.filename}</span></Button>)}</div>
}

function ReadableText({ text, className = "" }: { text: string; className?: string }) {
  const paragraphs = String(text || "").split(/\n{2,}/).map((item) => item.trim()).filter(Boolean)
  if (!paragraphs.length) return null
  return <div className={`space-y-3 ${className}`}>{paragraphs.map((paragraph, index) => <p className="break-words whitespace-pre-wrap text-[15px] leading-7" key={`${paragraph.slice(0, 24)}-${index}`}>{paragraph}</p>)}</div>
}

function eventDateKey(event: IssueEvent) {
  const value = [event.occurred_at, event.occurred_at_label, event.time_label, event.source_date, event.embedded_date].filter(Boolean).join(" ")
  const iso = value.match(/\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b/)
  const slash = value.match(/\b(\d{1,2})\/(\d{1,2})\/(\d{2,4})\b/)
  if (iso) return `${iso[1]}-${String(Number(iso[2])).padStart(2, "0")}-${String(Number(iso[3])).padStart(2, "0")}`
  if (slash) return `${Number(slash[3]) < 100 ? 2000 + Number(slash[3]) : slash[3]}-${String(Number(slash[1])).padStart(2, "0")}-${String(Number(slash[2])).padStart(2, "0")}`
  return ""
}

function eventRoundKey(event: IssueEvent) {
  const value = String(event.effective_round || event.review_round || "").trim()
  if (/^\d+$/.test(value)) return value
  const marker = String(event.record_label || "").match(/^PC\s*-?\s*(\d+)$/i)
  return marker ? marker[1] : ""
}

function eventSources(event: IssueEvent) {
  const candidates = event.sources?.length ? event.sources : event.source ? [event.source] : []
  return uniqueSources(candidates)
}

function eventRecordLabels(event: IssueEvent) {
  return [...new Set([...(event.record_labels || []), event.record_label].filter((value): value is string => Boolean(value && value.trim())))]
}

function timelineTokens(value: string) {
  return normalizedTimelineText(value).match(/[a-z0-9]+(?:['-][a-z0-9]+)*/g) || []
}

function timelineParameters(value: string) {
  // Preserve order: “12 inches to 6 inches” and “6 inches to 12 inches”
  // are different instructions even though they contain the same numbers.
  return normalizedTimelineText(value).match(/\b\d+(?:\.\d+)?\s*(?:inch|inches|in|ft|feet|foot|mm|cm|%|hour|hours|minute|minutes|ga|ul|cbc|asce)\b|\b\d+(?:\.\d+)?\b/g) || []
}

function timelineNegations(value: string) {
  return normalizedTimelineText(value).match(/\b(?:not|no|never|without|except|cannot|can't|must not|shall not|remain open)\b/g) || []
}

function similarTimelineText(left: string, right: string) {
  const a = normalizedTimelineText(left)
  const b = normalizedTimelineText(right)
  if (!a || !b) return false
  if (a === b) return true
  // Measurements, code references, and negation are meaningful differences;
  // never collapse two events when those tokens disagree.
  if (JSON.stringify(timelineParameters(a)) !== JSON.stringify(timelineParameters(b))) return false
  if (JSON.stringify(timelineNegations(a)) !== JSON.stringify(timelineNegations(b))) return false
  const aTokens = timelineTokens(a)
  const bTokens = timelineTokens(b)
  if (!aTokens.length || !bTokens.length) return false
  const aSet = new Set(aTokens)
  const bSet = new Set(bTokens)
  const shared = [...aSet].filter((token) => bSet.has(token)).length
  const union = new Set([...aSet, ...bSet]).size
  const containment = shared / Math.min(aSet.size, bSet.size)
  const jaccard = shared / union
  const lengthRatio = Math.min(a.length, b.length) / Math.max(a.length, b.length)
  return containment >= 0.96 && jaccard >= 0.9 && lengthRatio >= 0.84
}

function eventRoleFamily(event: IssueEvent) {
  const role = String(event.actor_role || "").trim().toLocaleLowerCase()
  const type = String(event.event_type || "").trim().toLocaleLowerCase()
  if (role === "company" || role === "applicant" || type === "applicant_response" || type === "current_applicant_response") return "company"
  if (role === "government" || role === "reviewer" || type === "government_comment" || type === "reviewer_follow_up" || type === "discussion_note") return "government"
  return role || type || "unknown"
}

function eventTypeRank(event: IssueEvent) {
  return ({ government_comment: 0, applicant_response: 0, reviewer_follow_up: 1, current_applicant_response: 1, discussion_note: 2 } as Record<string, number>)[event.event_type] ?? 3
}

export function mergeIssueEvents(events: IssueEvent[]) {
  const merged: IssueEvent[] = []
  const byIdentity = new Map<string, number>()
  for (const event of events) {
    const textKey = normalizedTimelineText(event.text || "")
    const undatedRoundKey = event.record_label
      || (event.effective_round ? `PC${event.effective_round}` : "PCx")
    const roundKey = eventRoundKey(event)
    const dateKey = eventDateKey(event)
    // A dated event is identified by role + date + substantive text.  The
    // same visible row may be copied into PC2/PC3 containers with different
    // round labels; those labels are metadata, not separate events.
    const identity = `${eventRoleFamily(event)}|${dateKey ? `date:${dateKey}` : `round:${roundKey || undatedRoundKey}`}|${textKey}`
    let index = byIdentity.get(identity)
    if (index === undefined && dateKey) {
      const candidateIndex = merged.findIndex((candidate) => eventRoleFamily(candidate) === eventRoleFamily(event)
        && eventDateKey(candidate) === dateKey
        && similarTimelineText(candidate.text || "", event.text || ""))
      if (candidateIndex >= 0) {
        index = candidateIndex
        byIdentity.set(identity, candidateIndex)
      }
    }
    if (index === undefined) {
      const sources = eventSources(event)
      const submissions = [...new Set([...(event.submissions || []), event.submission].filter((value): value is string => Boolean(value)))]
      merged.push({ ...event, source: sources[0] || event.source, sources, submissions, record_labels: eventRecordLabels(event), date_variants: [dateKey].filter(Boolean), merged_event_ids: [event.event_id] })
      byIdentity.set(identity, merged.length - 1)
      continue
    }
    const current = merged[index]
    const sources = [...eventSources(current), ...eventSources(event)].filter((source, sourceIndex, all) => {
      const key = sourceIdentity(source)
      return all.findIndex((candidate) => sourceIdentity(candidate) === key) === sourceIndex
    })
    const preferred = eventTypeRank(event) < eventTypeRank(current) ? event : current
    merged[index] = {
      ...current,
      // Prefer the event with an exact timestamp or a non-empty actor while
      // keeping the first event's stable display order.
      actor: current.actor || event.actor,
      occurred_at: current.occurred_at || event.occurred_at,
      occurred_at_label: current.occurred_at_label || event.occurred_at_label,
      time_label: current.time_label || event.time_label,
      event_type: preferred.event_type,
      actor_role: preferred.actor_role,
      label: preferred.label,
      record_label: current.record_label || event.record_label,
      record_labels: [...new Set([...eventRecordLabels(current), ...eventRecordLabels(event)])],
      date_variants: [...new Set([...(current.date_variants || []), ...(event.date_variants || []), eventDateKey(current), eventDateKey(event)].filter(Boolean))],
      sources,
      source: sources[0] || current.source || event.source,
      submissions: [...new Set([...(current.submissions || []), ...(event.submissions || []), current.submission, event.submission].filter((value): value is string => Boolean(value)))],
      merged_event_ids: [...(current.merged_event_ids || [current.event_id]), event.event_id],
    }
  }
  return merged
}

function OrganizedText({ blocks, fallback }: { blocks?: TextBlock[]; fallback: string }) {
  if (!blocks?.length) return <ReadableText text={fallback} />
  return <div className="space-y-4">{blocks.map((block, index) => <section key={`${block.title}-${index}`}>{block.title && <h4 className="mb-1.5 text-sm font-semibold">{block.title}</h4>}{block.kind === "list" && block.items ? <ul className="list-disc space-y-1.5 pl-5 text-[15px] leading-7">{block.items.map((item) => <li className="break-words" key={item}>{item}</li>)}</ul> : <ReadableText text={block.text || fallback} />}</section>)}</div>
}

function EvidencePanel({ tone, title, label, status, text, blocks, sources, onOpenSource }: { tone: "comment" | "response"; title: string; label?: string; status: React.ReactNode; text: string; blocks?: TextBlock[]; sources: SourceReference[]; onOpenSource: (id: string) => void }) {
  return <section aria-label={title} className={`evidence-panel ${tone} min-w-0 rounded-xl border p-4 shadow-xs sm:p-5 xl:p-6`}>
    <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{tone === "comment" ? "Government record" : "Company record"}</p><div className="mt-1 flex flex-wrap items-center gap-2"><h3 className="text-lg font-semibold">{title}</h3>{label && <Badge className="border-slate-200 bg-slate-50 text-slate-600" variant="outline">{label}</Badge>}</div></div><div className="shrink-0">{status}</div></div>
    <div className={`mt-4 min-w-0 rounded-lg border p-4 sm:p-5 ${tone === "response" ? "border-teal-200/80 bg-white" : "bg-white"}`}><OrganizedText blocks={blocks} fallback={text} /></div>
    <div className="mt-4 min-w-0"><p className="mb-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">Source evidence</p><SourceButtons sources={sources} onOpenSource={onOpenSource} /></div>
  </section>
}

function IssueHistory({ events, rounds, status, onOpenSource }: { events: IssueEvent[]; rounds: string[]; status: React.ReactNode; onOpenSource: (id: string) => void }) {
  return <section aria-label="Issue history" className="rounded-xl border bg-card p-4 shadow-xs sm:p-5 xl:p-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-xs font-semibold tracking-wide text-primary uppercase">Single design issue</p><h3 className="mt-1 text-lg font-semibold">Comment and response history</h3><p className="mt-1 text-sm text-muted-foreground">Starts with the main government comment, then lists responses and follow-ups by their earliest known date.</p></div>
      <div className="flex flex-wrap gap-2"><Badge variant="secondary">{events.length} events</Badge>{rounds.length > 1 && <Badge variant="outline">{rounds.length} review rounds</Badge>}</div>
    </div>
    <div className="relative mt-5 space-y-3 before:absolute before:top-4 before:bottom-4 before:left-[11px] before:w-px before:bg-border">
      {events.map((event, index) => {
        const government = event.actor_role === "government"
        const current = event.event_type === "current_applicant_response"
        const sources = eventSources(event)
        const tone = event.event_type === "reviewer_follow_up"
          ? "border-amber-200 bg-amber-50/70"
          : government
            ? "border-slate-200 bg-white"
            : "border-teal-200 bg-teal-50/60"
        const dateKey = eventDateKey(event)
        const timeLabel = dateKey
          ? (event.time_label || event.occurred_at_label?.split(" ")[0] || dateKey)
          : (event.record_label || (event.effective_round ? `PC${event.effective_round}` : "PCx"))
        const renderKey = [
          event.event_id,
          event.event_type,
          event.effective_round || event.review_round,
          event.occurred_at || event.occurred_at_label || event.time_label,
          index,
        ].join("|")
        return <article className="relative pl-9" key={renderKey}>
          <span className={`absolute top-4 left-1.5 z-10 size-3 rounded-full border-2 border-white ${event.event_type === "reviewer_follow_up" ? "bg-amber-600" : government ? "bg-slate-600" : "bg-teal-700"}`} />
          <div className={`rounded-lg border p-4 ${tone}`}>
            <div className="flex flex-wrap items-center gap-2">
              <Badge className={event.event_type === "reviewer_follow_up" ? "border-amber-300 bg-amber-100 text-amber-900" : event.actor_role === "company" ? "border-teal-200 bg-teal-100 text-teal-900" : "border-slate-300 bg-white text-slate-700"} variant="outline">{event.label}</Badge>
              {eventRecordLabels(event).map((recordLabel) => <Badge className="border-slate-200 bg-slate-50 text-slate-600" variant="outline" key={recordLabel}>{recordLabel}</Badge>)}
              {(event.actor || timeLabel) && <span className="text-xs text-muted-foreground">{[event.actor, timeLabel].filter(Boolean).join(" · ")}</span>}
              {eventRoundKey(event) && <Badge variant="outline">PC{eventRoundKey(event)}</Badge>}
              {event.review_round && <Badge variant="outline">Round {event.review_round}</Badge>}
              {(event.submissions?.length ? event.submissions : event.submission ? [event.submission] : []).map((submission) => <Badge variant="outline" key={submission}>{submission}</Badge>)}
              {sources.length > 1 && <Badge className="border-teal-200 bg-teal-50 text-teal-900" variant="outline">{sources.length} source files</Badge>}
              {current && <div className="ml-auto">{status}</div>}
            </div>
            <ReadableText text={event.text} className="mt-3" />
            {sources.length > 0 && <div className="mt-3"><SourceButtons sources={sources} onOpenSource={onOpenSource} /></div>}
          </div>
          {index === 0 && <span className="sr-only">Issue begins</span>}
        </article>
      })}
    </div>
  </section>
}

function issueEventTime(event: IssueEvent) {
  const explicit = Date.parse(event.occurred_at || "")
  if (Number.isFinite(explicit)) return explicit
  const match = `${event.time_label || ""} ${event.source_date || ""}`.match(/(\d{1,2})\/(\d{1,2})\/(\d{2,4})/)
  if (!match) {
    const marker = event.record_label || (event.effective_round ? `PC${event.effective_round}` : "")
    const round = marker.match(/\d+/)
    return 10 ** 15 + (round ? Number(round[0]) : 10 ** 6)
  }
  const year = Number(match[3]) < 100 ? 2000 + Number(match[3]) : Number(match[3])
  return Date.UTC(year, Number(match[1]) - 1, Number(match[2]))
}

function issueEventRound(event: IssueEvent) {
  const value = event.effective_round || event.review_round || event.record_label || ""
  const match = String(value).match(/\d+/)
  return match ? Number(match[0]) : Number.MAX_SAFE_INTEGER
}

function normalizedTimelineText(value: string) {
  let text = String(value || "").normalize("NFKC").replace(/_x000D_|_x000A_|\*x000[dD]_\*?/g, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase()
  text = text.replace(/^\s*(?:markup|comment)\s+.*?\bv\s*\d+\s*[-/]?\s*c\s*\d+\s+\d+(?:\.\d+)?\s*/i, "")
  text = text.replace(/^\s*\(?[a-z]\)?(?:[.:]|\s+)\s*/, "")
  text = text.replace(/^\s*\(?[a-z]\)?\s*pc\s*\d+\s*[-:]\s*/i, "")
  text = text.replace(/^\s*pc\s*\d+\s*[-:]\s*/i, "")
  text = text.replace(/^\s*\d+\s*[.)]\s+/, "")
  return text
}

function stripRepeatedPrimaryComment(responseText: string, primaryText: string) {
  const words = String(primaryText || "").trim().split(/\s+/).slice(0, 8)
  if (words.length < 4) return responseText
  const escaped = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
  const match = new RegExp(escaped.join("\\s+"), "i").exec(responseText)
  if (!match || match.index <= 0) return responseText
  const prefix = responseText.slice(0, match.index).trim().replace(/[,:;-]+$/, "").trim()
  return prefix || responseText
}

function completeIssueEvents(events: IssueEvent[], members: CommentRecord[]) {
  if (!members.length) return events
  const earliestRound = Math.min(...members.map((member) => {
    const match = String(member.review_round).match(/\d+/)
    return match ? Number(match[0]) : Number.MAX_SAFE_INTEGER
  }))
  const earliestMembers = members.filter((member) => {
    const match = String(member.review_round).match(/\d+/)
    return (match ? Number(match[0]) : Number.MAX_SAFE_INTEGER) === earliestRound
  })
  // Prefer the complete source-row requirement when several records from the
  // first round represent the same issue.
  const majorComment = [...earliestMembers].sort((left, right) => right.display_text.length - left.display_text.length)[0] || members[0]
  const majorText = normalizedTimelineText(majorComment.display_text)
  const majorRound = String(majorComment.review_round || "")
  const governmentCandidates = events.map((event, index) => ({ event, index })).filter(({ event }) => event.event_type === "government_comment")
  const earliestGovernmentRound = governmentCandidates.length
    ? Math.min(...governmentCandidates.map(({ event }) => issueEventRound(event)))
    : Number.MAX_SAFE_INTEGER
  const primaryGovernment = governmentCandidates.filter(({ event }) => issueEventRound(event) === earliestGovernmentRound).sort((left, right) => right.event.text.length - left.event.text.length)[0]
  const matchingIndex = primaryGovernment?.index ?? events.findIndex((event) => (
    event.actor_role !== "company"
    && event.event_type !== "applicant_response"
    && event.event_type !== "current_applicant_response"
    && normalizedTimelineText(event.text) === majorText
    && String(event.effective_round || event.review_round || "") === majorRound
  ))
  const primarySource = majorComment.sources?.[0] || null
  const primaryEvent: IssueEvent = matchingIndex >= 0 ? {
    ...events[matchingIndex],
    event_id: `${events[matchingIndex].event_id}-primary-comment`,
    event_type: "government_comment",
    actor_role: "government",
    actor: events[matchingIndex].actor || majorComment.reviewer,
    label: "Government comment",
    record_label: events[matchingIndex].record_label || majorComment.comment_label,
    source: events[matchingIndex].source || primarySource,
    sources: events[matchingIndex].sources?.length ? events[matchingIndex].sources : majorComment.sources,
  } : {
    event_id: `${majorComment.comment_id}-primary-comment`,
    event_type: "government_comment",
    actor_role: "government",
    actor: majorComment.reviewer,
    occurred_at: "",
    occurred_at_label: "",
    time_label: `Round ${majorRound}`,
    label: "Government comment",
    record_label: majorComment.comment_label,
    text: majorComment.display_text,
    review_round: majorRound,
    effective_round: majorRound,
    source: primarySource,
    sources: majorComment.sources,
  }
  const remainder = (matchingIndex >= 0
    ? events.filter((_event, index) => index !== matchingIndex)
    : events).map((event) => (
      event.actor_role === "company"
      || event.event_type === "applicant_response"
      || event.event_type === "current_applicant_response"
    ) ? { ...event, text: stripRepeatedPrimaryComment(event.text, primaryEvent.text) } : event)
  return [primaryEvent, ...remainder].sort((left, right) => {
    const leftPrimary = left.event_id.endsWith("-primary-comment")
    const rightPrimary = right.event_id.endsWith("-primary-comment")
    if (leftPrimary !== rightPrimary) return leftPrimary ? -1 : 1
    const timeDifference = issueEventTime(left) - issueEventTime(right)
    if (timeDifference) return timeDifference
    const roundDifference = issueEventRound(left) - issueEventRound(right)
    if (roundDifference) return roundDifference
    const leftGovernment = left.event_type === "government_comment"
    const rightGovernment = right.event_type === "government_comment"
    return leftGovernment === rightGovernment ? 0 : leftGovernment ? -1 : 1
  })
}

export function CommentDetail({ comment, threadMembers = [], onOpenSource }: { comment: CommentRecord | null; threadMembers?: CommentRecord[]; onOpenSource: (id: string) => void }) {
  if (!comment) return <div id="comment-detail-panel" className="comment-detail-shell grid min-h-[520px] place-items-center bg-muted/10 p-8 text-center"><div><FileText className="mx-auto size-9 text-muted-foreground" /><h3 className="mt-3 font-semibold">Select a historical comment</h3><p className="mt-1 max-w-sm text-sm text-muted-foreground">Choose a result to compare the government comment with the stored company response.</p></div></div>
  const confirmed = comment.response && (comment.link.review_status === "confirmed" || comment.response.human_review_status === "confirmed")
  const responseStatus = !comment.response ? <Badge className="border-amber-200 bg-amber-50 text-amber-900" variant="outline"><Link2Off />Missing response</Badge> : confirmed ? <Badge className="border-green-200 bg-green-50 text-green-800" variant="outline"><CheckCircle2 />Confirmed</Badge> : <Badge variant="destructive"><ShieldAlert />Unverified</Badge>
  const members = (threadMembers.length ? threadMembers : [comment]).slice().sort((left, right) => String(left.review_round).localeCompare(String(right.review_round), undefined, { numeric: true }))
  const mergedIssueEvents = mergeIssueEvents(members.flatMap((member) => member.issue_thread?.events || []))
  const memberRounds = new Set(members.map((member) => member.review_round).filter(Boolean))
  const rawEventRounds = new Set(mergedIssueEvents.map((event) => event.effective_round || event.review_round).filter(Boolean))
  const hasReviewerFollowUp = mergedIssueEvents.some((event) => event.event_type === "reviewer_follow_up" || event.event_type === "discussion_note")
  // Decide whether this is a history before filling a missing primary event;
  // otherwise an ordinary one-round comment/response pair would become a
  // synthetic timeline merely because its metadata uses a different label.
  const hasHistory = memberRounds.size > 1 || rawEventRounds.size > 1 || hasReviewerFollowUp
  const issueEvents = hasHistory ? completeIssueEvents(mergedIssueEvents, members) : mergedIssueEvents
  const issueRounds = [...new Set([
    ...members.map((member) => member.review_round),
    ...issueEvents.map((event) => event.effective_round || event.review_round),
  ].filter(Boolean))].sort((left, right) => String(left).localeCompare(String(right), undefined, { numeric: true }))
  const canonicalRecord: CanonicalEvidenceRecord = {
    title: comment.property_project,
    issueLabel: [comment.discipline, comment.comment_number ? `comment ${comment.comment_number}` : ""].filter(Boolean).join(" "),
    city: comment.city,
    round: comment.review_round,
    statusLabel: !comment.response ? "No confirmed response" : confirmed ? "Confirmed" : "Unverified",
    statusTone: !comment.response ? "missing" : confirmed ? "confirmed" : "unverified",
    sections: [
      { kind: "comment", title: "Government comment", text: comment.display_text, blocks: comment.display_blocks },
      ...(comment.response ? [{ kind: "response" as const, title: "Historical company response", text: comment.response.display_text, blocks: comment.response.display_blocks }] : []),
    ],
    sources: [
      ...(comment.sources || []).map((source, index) => ({ sourceId: source.source_id, filename: source.filename, label: source.relation, relation: source.relation, primary: !comment.response && index === 0 })),
      ...(comment.response?.sources || []).map((source, index) => ({ sourceId: source.source_id, filename: source.filename, label: source.relation, relation: source.relation, primary: index === 0 })),
    ],
    primarySourceId: comment.response?.sources?.[0]?.source_id || comment.sources?.[0]?.source_id,
  }

  return <div id="comment-detail-panel" className="comment-detail-shell min-h-0 bg-slate-100/70" tabIndex={-1}>
    <header className="border-b border-teal-100 bg-teal-50 p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold tracking-wide text-primary uppercase">Comment-response detail</p><h2 className="mt-1 text-xl font-semibold">{comment.discipline} comment {comment.comment_number || ""}</h2></div><div className="flex flex-wrap gap-2">{hasHistory && <Badge className="border-teal-200 bg-teal-50 text-teal-900" variant="outline"><MessageSquareMore />Issue history</Badge>}<Badge variant="outline">{comment.category}</Badge></div></div>
      <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground"><span>{comment.property_project}</span><span>·</span><span>{comment.city}</span><span>·</span><span>Review round {comment.review_round}</span><span>·</span><span>{comment.reviewer || "Reviewer not recorded"}</span></div>
    </header>
    <div className="space-y-4 p-3 sm:p-4 xl:p-5">
      {hasHistory ? <IssueHistory events={issueEvents} rounds={issueRounds} status={responseStatus} onOpenSource={onOpenSource} /> : <CanonicalEvidenceDetail record={canonicalRecord} onOpenSource={onOpenSource} showHeader={false} />}
      <Collapsible className="rounded-xl border bg-card px-4 py-3 sm:px-5"><CollapsibleTrigger className="flex w-full items-center justify-between gap-2 text-left text-sm font-medium text-muted-foreground"><span>Record details</span><ChevronDown className="size-4 shrink-0" /></CollapsibleTrigger><CollapsibleContent className="grid gap-3 pt-4 text-xs text-muted-foreground sm:grid-cols-2 xl:grid-cols-4"><div className="min-w-0"><strong className="block text-foreground">Comment source</strong><span className="break-words">{comment.source_filename}</span></div><div><strong className="block text-foreground">Location</strong>{comment.source_location}</div><div><strong className="block text-foreground">Extraction</strong><span className="break-words">{comment.extraction_method || "Not recorded"}</span></div><div><strong className="block text-foreground">Confidence</strong>{comment.extraction_confidence ? `${Math.round(Number(comment.extraction_confidence) * 100)}%` : "Not recorded"}</div></CollapsibleContent></Collapsible>
    </div>
  </div>
}
