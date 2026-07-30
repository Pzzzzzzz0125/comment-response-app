import { useEffect, useState } from "react"
import { AlertTriangle, Check, CheckCircle2, ChevronLeft, ChevronRight, ExternalLink, FileSpreadsheet, Flag, MessageSquareMore } from "lucide-react"
import { api } from "@/lib/api"
import type { CommentRecord, SourceReference } from "@/types"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"

type WorkbookReview = {
  source_document: string
  filename: string
  status: "pending" | "confirmed" | "needs_followup"
  note: string
  city: string
  property_project: string
  review_rounds: string[]
  comment_count: number
  response_count: number
  comment_columns: string[]
  response_columns: string[]
  source: SourceReference | null
  rows: CommentRecord[]
  structural_checks: {
    can_confirm: boolean
    reason: string
    expected_comments?: number
    unresolved_signals?: number
    requires_visual?: boolean
  }
}

type WorkbookPayload = {
  items: WorkbookReview[]
  counts: { total: number; pending: number; confirmed: number; needs_followup: number }
}

export function WorkbookReviewDialog({
  open,
  onOpenChange,
  cities,
  onOpenSource,
  onChanged,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  cities: string[]
  onOpenSource: (id: string) => void
  onChanged: () => void
}) {
  const [status, setStatus] = useState("pending")
  const [city, setCity] = useState("__all__")
  const [payload, setPayload] = useState<WorkbookPayload | null>(null)
  const [index, setIndex] = useState(0)
  const [note, setNote] = useState("")
  const [saving, setSaving] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const item = payload?.items[index]

  async function load() {
    const next = await api<WorkbookPayload>(`/api/workbook-reviews?status=${status}&city=${encodeURIComponent(city === "__all__" ? "" : city)}`)
    setPayload(next)
    setIndex(0)
    setNote(next.items[0]?.note || "")
    setConfirming(false)
  }

  useEffect(() => { if (open) load() }, [open, status, city])
  useEffect(() => {
    setNote(item?.note || "")
    setConfirming(false)
  }, [item?.source_document])

  async function decide(decision: "confirmed" | "needs_followup") {
    if (!item) return
    setSaving(true)
    try {
      await api("/api/workbook-reviews", {
        method: "POST",
        body: JSON.stringify({
          source_document: item.source_document,
          decision,
          note: note.trim(),
        }),
      })
      await load()
      onChanged()
    } finally {
      setSaving(false)
      setConfirming(false)
    }
  }

  const move = (direction: number) => payload?.items.length && setIndex((current) => (current + direction + payload.items.length) % payload.items.length)
  const openReference = (reference: SourceReference | undefined | null) => reference?.source_id && onOpenSource(reference.source_id)

  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="flex max-h-[94vh] max-w-7xl flex-col overflow-hidden">
      <DialogHeader>
        <DialogTitle>Review structured workbooks</DialogTitle>
        <DialogDescription>Confirm the detected comment and response columns once per workbook. Exact source text remains immutable.</DialogDescription>
      </DialogHeader>

      <div className="flex flex-wrap items-center gap-3 border-y py-3">
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="pending">Pending</SelectItem>
            <SelectItem value="needs_followup">Needs follow-up</SelectItem>
            <SelectItem value="confirmed">Confirmed</SelectItem>
            <SelectItem value="all">All workbooks</SelectItem>
          </SelectContent>
        </Select>
        <Select value={city} onValueChange={setCity}>
          <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">All cities</SelectItem>
            {cities.map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}
          </SelectContent>
        </Select>
        <span className="ml-auto text-sm text-muted-foreground">{payload ? `${payload.counts.pending} pending · ${payload.counts.confirmed} confirmed` : "Loading…"}</span>
        <Button variant="outline" size="icon" aria-label="Previous workbook" onClick={() => move(-1)}><ChevronLeft /></Button>
        <Button variant="outline" size="icon" aria-label="Next workbook" onClick={() => move(1)}><ChevronRight /></Button>
      </div>

      {!payload ? <div className="grid gap-4 py-4 md:grid-cols-2"><Skeleton className="h-72" /><Skeleton className="h-72" /></div> : !item ? <div className="grid min-h-72 place-items-center text-center"><div><CheckCircle2 className="mx-auto size-9 text-green-700" /><h3 className="mt-3 font-semibold">No workbooks in this queue</h3><p className="text-sm text-muted-foreground">Choose another status or city.</p></div></div> : <div className="min-h-0 flex-1 overflow-y-auto pr-2">
        <section className="sticky top-0 z-10 rounded-xl border bg-card p-4 shadow-sm">
          <div className="flex flex-wrap items-start gap-3">
            <div className="grid size-10 place-items-center rounded-lg bg-teal-50 text-primary"><FileSpreadsheet /></div>
            <div className="min-w-0 flex-1">
              <h3 className="truncate font-semibold">{item.filename}</h3>
              <p className="mt-1 text-xs text-muted-foreground">{item.property_project} · {item.city} · Round {item.review_rounds.join(", ") || "unknown"}</p>
            </div>
            <Badge className={item.status === "confirmed" ? "border-green-200 bg-green-50 text-green-800" : item.status === "needs_followup" ? "border-amber-200 bg-amber-50 text-amber-900" : ""} variant="outline">{item.status.replaceAll("_", " ")}</Badge>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Badge variant="secondary">{item.comment_count} comments</Badge>
            <Badge variant="secondary">{item.response_count} responses</Badge>
            <Badge variant="outline">Comment column {item.comment_columns.join(", ") || "unknown"}</Badge>
            <Badge variant="outline">Response column {item.response_columns.join(", ") || "none"}</Badge>
            {item.source && <Button className="ml-auto" variant="outline" size="sm" onClick={() => openReference(item.source)}>Open workbook<ExternalLink /></Button>}
          </div>
        </section>

        <Alert className={`my-4 ${item.structural_checks.can_confirm ? "border-green-200 bg-green-50/60" : "border-amber-200 bg-amber-50/60"}`}>
          {item.structural_checks.can_confirm ? <CheckCircle2 className="text-green-700" /> : <AlertTriangle className="text-amber-700" />}
          <AlertTitle>{item.structural_checks.can_confirm ? "Local structure checks passed" : "Workbook cannot be batch-confirmed"}</AlertTitle>
          <AlertDescription>{item.structural_checks.can_confirm ? `All ${item.structural_checks.expected_comments ?? item.comment_count} physical comment rows are assigned exactly once, with no hidden, merged, formula, or unresolved units.` : item.structural_checks.reason}</AlertDescription>
        </Alert>

        <div className="space-y-3">
          {item.rows.map((row) => {
                  const commentSource = row.sources.find((source) => source.kind !== "external")
                  const responseSource = row.response?.sources.find(
                    (source) => source.kind !== "external",
                  )
                  const issueEvents = row.issue_thread?.events || []
                  const hasHistory = issueEvents.length > 2
            return <article className="rounded-xl border bg-card p-4" key={row.comment_id}>
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant="outline">Row {commentSource?.location?.metadata?.source_row as number || row.comment_number}</Badge>
                <span>Comment {row.comment_number}</span>
                <span>·</span>
                <span>{row.discipline}</span>
                {commentSource && <Button className="ml-auto" variant="ghost" size="sm" onClick={() => openReference(commentSource)}>Open cited cell<ExternalLink /></Button>}
              </div>
              {hasHistory ? <div className="mt-3 rounded-lg border border-teal-200 bg-teal-50/40 p-4">
                <div className="flex flex-wrap items-center gap-2"><MessageSquareMore className="size-4 text-teal-800" /><p className="text-sm font-semibold">One issue with {issueEvents.length} review events</p><Badge className="ml-auto border-amber-200 bg-amber-50 text-amber-900" variant="outline">{row.issue_thread?.status || "History detected"}</Badge></div>
                <div className="mt-3 space-y-2">
                  {issueEvents.map((event, eventIndex) => <div className={`rounded-md border p-3 ${event.event_type === "reviewer_follow_up" ? "border-amber-200 bg-amber-50/80" : event.actor_role === "company" ? "border-teal-200 bg-white" : "bg-white"}`} key={event.event_id}>
                    <div className="flex flex-wrap items-center gap-2 text-xs"><span className="font-semibold">{eventIndex + 1}. {event.label}</span>{event.actor && <span className="text-muted-foreground">{event.actor}</span>}{event.occurred_at_label && <Badge variant="outline">{event.occurred_at_label}</Badge>}{event.source && <Button className="ml-auto" variant="ghost" size="sm" onClick={() => openReference(event.source)}>Open cell<ExternalLink /></Button>}</div>
                    <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{event.text}</p>
                  </div>)}
                </div>
              </div> : <div className="mt-3 grid gap-3 md:grid-cols-2">
                <div className="rounded-lg border bg-muted/20 p-4"><p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Government comment</p><p className="mt-2 whitespace-pre-wrap text-sm leading-6">{row.display_text}</p></div>
                <div className="rounded-lg border border-teal-100 bg-teal-50/50 p-4"><div className="flex items-center justify-between gap-2"><p className="text-[11px] font-semibold uppercase tracking-wide text-teal-800">Company response</p>{responseSource && <Button variant="ghost" size="sm" onClick={() => openReference(responseSource)}>Open cell<ExternalLink /></Button>}</div><p className="mt-2 whitespace-pre-wrap text-sm leading-6">{row.response?.display_text || "No response is stored in this row."}</p></div>
              </div>}
            </article>
          })}
        </div>
        <Textarea className="mt-4" aria-label="Workbook reviewer note" value={note} onChange={(event) => setNote(event.target.value)} placeholder="Workbook review note (optional)" />
        {confirming && <Alert className="mt-4 border-green-300 bg-green-50"><Check className="text-green-700" /><AlertTitle>Confirm all {item.comment_count} rows?</AlertTitle><AlertDescription>This promotes the exact C/E cell values to human-verified searchable records. Original source text and audit history remain unchanged.</AlertDescription></Alert>}
      </div>}

      <DialogFooter className="flex-wrap border-t pt-4">
        <Button variant="outline" disabled={!item || saving} onClick={() => decide("needs_followup")}><Flag />Needs follow-up</Button>
        <div className="flex-1" />
        {confirming
          ? <><Button variant="ghost" disabled={saving} onClick={() => setConfirming(false)}>Cancel</Button><Button className="bg-green-700 hover:bg-green-800" disabled={saving} onClick={() => decide("confirmed")}><Check />Yes, confirm workbook</Button></>
          : <Button className="bg-green-700 hover:bg-green-800" disabled={!item || saving || !item.structural_checks.can_confirm || item.status === "confirmed"} onClick={() => setConfirming(true)}><Check />Confirm entire workbook</Button>}
      </DialogFooter>
    </DialogContent>
  </Dialog>
}
