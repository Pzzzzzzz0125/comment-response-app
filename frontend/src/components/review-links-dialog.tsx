import { useEffect, useState } from "react"
import { Check, ChevronLeft, ChevronRight, ExternalLink, Flag, Link2, X } from "lucide-react"
import { api } from "@/lib/api"
import type { CommentRecord } from "@/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"

type ReviewItem = { link_id: string; status: string; base_status: string; note: string; comment: CommentRecord }
type ReviewPayload = { items: ReviewItem[]; counts: { total: number; completed: number; suggested: number; needs_review: number; needs_followup: number } }

export function ReviewLinksDialog({ open, onOpenChange, cities, onOpenSource, onChanged }: { open: boolean; onOpenChange: (open: boolean) => void; cities: string[]; onOpenSource: (id: string) => void; onChanged: () => void }) {
  const [status, setStatus] = useState("pending")
  const [city, setCity] = useState("__all__")
  const [payload, setPayload] = useState<ReviewPayload | null>(null)
  const [index, setIndex] = useState(0)
  const [note, setNote] = useState("")
  const [saving, setSaving] = useState(false)
  const item = payload?.items[index]

  async function load() {
    const next = await api<ReviewPayload>(`/api/link-reviews?status=${status}&city=${encodeURIComponent(city === "__all__" ? "" : city)}`)
    setPayload(next); setIndex(0); setNote(next.items[0]?.note || "")
  }
  useEffect(() => { if (open) load() }, [open, status, city])
  useEffect(() => { setNote(item?.note || "") }, [item?.link_id])

  async function decide(decision: string) {
    if (!item) return
    setSaving(true)
    try {
      await api("/api/link-reviews", { method: "POST", body: JSON.stringify({ link_id: item.link_id, decision, note: note.trim() }) })
      await load(); onChanged()
    } finally { setSaving(false) }
  }
  const move = (direction: number) => payload?.items.length && setIndex((current) => (current + direction + payload.items.length) % payload.items.length)
  const sourceButtons = (sources: CommentRecord["sources"]) => <div className="mt-3 flex flex-wrap gap-2">{sources.map((source) => <Button size="sm" variant="outline" onClick={() => onOpenSource(source.source_id)} key={source.source_id}>{source.filename}<ExternalLink /></Button>)}</div>

  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="max-h-[92vh] max-w-6xl overflow-y-auto">
    <DialogHeader><DialogTitle>Review response links</DialogTitle><DialogDescription>Confirm, reject, or flag suggested comment-response relationships. Original text remains immutable.</DialogDescription></DialogHeader>
    <div className="flex flex-wrap items-center gap-3 border-y py-3"><Select value={status} onValueChange={setStatus}><SelectTrigger className="w-52"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="pending">Pending + follow-up</SelectItem><SelectItem value="suggested">Not reviewed</SelectItem><SelectItem value="needs_review">Ingestion needs review</SelectItem><SelectItem value="needs_followup">Needs follow-up</SelectItem><SelectItem value="confirmed">Confirmed</SelectItem><SelectItem value="rejected">Rejected</SelectItem><SelectItem value="all">All review links</SelectItem></SelectContent></Select><Select value={city} onValueChange={setCity}><SelectTrigger className="w-48"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="__all__">All cities</SelectItem>{cities.map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}</SelectContent></Select><span className="ml-auto text-sm text-muted-foreground">{payload ? `${payload.counts.completed} of ${payload.counts.total} completed` : "Loading…"}</span><Button variant="outline" size="icon" aria-label="Previous review link" onClick={() => move(-1)}><ChevronLeft /></Button><Button variant="outline" size="icon" aria-label="Next review link" onClick={() => move(1)}><ChevronRight /></Button></div>
    {!payload ? <div className="grid grid-cols-2 gap-4"><Skeleton className="h-80" /><Skeleton className="h-80" /></div> : !item ? <div className="grid min-h-64 place-items-center text-center"><div><Check className="mx-auto size-8 text-green-700" /><h3 className="mt-2 font-semibold">No links in this queue</h3><p className="text-sm text-muted-foreground">Choose another status or city.</p></div></div> : <>
      <div className="flex flex-wrap gap-2"><Badge>{item.status.replaceAll("_", " ")}</Badge><Badge variant="outline">{item.comment.city}</Badge><Badge variant="outline">{item.comment.property_project}</Badge><Badge variant="outline">Round {item.comment.review_round}</Badge></div>
      <div className="grid gap-4 md:grid-cols-2"><section className="rounded-xl border p-5"><h3 className="font-semibold">Government comment</h3><p className="mt-3 whitespace-pre-wrap text-sm leading-6">{item.comment.display_text}</p>{sourceButtons(item.comment.sources)}</section><section className="rounded-xl border bg-teal-50/40 p-5"><h3 className="font-semibold">Company response</h3><p className="mt-3 whitespace-pre-wrap text-sm leading-6">{item.comment.response?.display_text || "No response text is stored for this link."}</p>{sourceButtons(item.comment.response?.sources || [])}</section></div>
      <Textarea aria-label="Reviewer note" value={note} onChange={(event) => setNote(event.target.value)} placeholder="Reviewer note (optional)" />
    </>}
    <DialogFooter className="flex-wrap"><Button variant="ghost" disabled={!item || saving} onClick={() => decide("")}>Undo</Button><div className="flex-1" /><Button variant="outline" disabled={!item || saving} onClick={() => decide("needs_followup")}><Flag />Needs follow-up</Button><Button variant="destructive" disabled={!item || saving} onClick={() => decide("rejected")}><X />Reject</Button><Button className="bg-green-700 hover:bg-green-800" disabled={!item || saving} onClick={() => decide("confirmed")}><Check />Confirm</Button></DialogFooter>
  </DialogContent></Dialog>
}
