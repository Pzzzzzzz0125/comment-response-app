import { CheckCircle2, ChevronDown, FileText, Link2Off, ShieldAlert, X } from "lucide-react"
import type { TextBlock } from "@/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"

export type CanonicalEvidenceSource = {
  sourceId: string
  filename?: string
  label?: string
  relation?: string
  role?: string
  primary?: boolean
}

export type CanonicalEvidenceSection = {
  kind: "comment" | "response" | "followup"
  title: string
  text: string
  blocks?: TextBlock[]
  sources?: CanonicalEvidenceSource[]
}

export type CanonicalEvidenceRecord = {
  title: string
  issueLabel?: string
  city?: string
  round?: string
  statusLabel: string
  statusTone: "confirmed" | "response" | "missing" | "unverified"
  sections: CanonicalEvidenceSection[]
  sources: CanonicalEvidenceSource[]
  primarySourceId?: string | null
}

function sourceIdentity(source: CanonicalEvidenceSource) {
  return source.sourceId || `${source.filename || ""}|${source.relation || ""}`.toLocaleLowerCase()
}

export function uniqueCanonicalSources(sources: CanonicalEvidenceSource[]) {
  const seen = new Set<string>()
  return (sources || []).filter((source) => {
    const key = sourceIdentity(source)
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function readableText(value: string) {
  return String(value || "")
    .replace(/_x000[dD]_|_x000[aA]_|\*x000[dD]_\*?/g, "\n")
    .replace(/&nbsp;/gi, " ")
    .trim()
}

function EvidenceText({ text, blocks }: { text: string; blocks?: TextBlock[] }) {
  if (blocks?.length) return <div className="space-y-4">{blocks.map((block, index) => <section key={`${block.title || block.kind}-${index}`}>{block.title && <h4 className="mb-1.5 text-sm font-semibold">{block.title}</h4>}{block.kind === "list" && block.items ? <ul className="list-disc space-y-1.5 pl-5 text-[15px] leading-7">{block.items.map((item) => <li className="break-words" key={item}>{item}</li>)}</ul> : <EvidenceText text={block.text || text} />}</section>)}</div>
  const paragraphs = readableText(text).split(/\n{2,}/).map((item) => item.trim()).filter(Boolean)
  return <div className="space-y-3">{paragraphs.map((paragraph, index) => <p className="break-words whitespace-pre-wrap text-[15px] leading-7" key={`${paragraph.slice(0, 24)}-${index}`}>{paragraph}</p>)}</div>
}

export function EvidenceStatusBadge({ label, tone }: { label: string; tone: CanonicalEvidenceRecord["statusTone"] }) {
  if (tone === "confirmed") return <Badge className="border-green-200 bg-green-50 text-green-800" variant="outline"><CheckCircle2 />{label}</Badge>
  if (tone === "missing") return <Badge className="border-amber-200 bg-amber-50 text-amber-900" variant="outline"><Link2Off />{label}</Badge>
  if (tone === "unverified") return <Badge variant="destructive"><ShieldAlert />{label}</Badge>
  return <Badge className="border-teal-200 bg-teal-50 text-teal-900" variant="outline">{label}</Badge>
}

function sectionTone(kind: CanonicalEvidenceSection["kind"]) {
  if (kind === "response") return "border-teal-200 bg-teal-50/60"
  if (kind === "followup") return "border-amber-200 bg-amber-50/70"
  return "border-slate-200 bg-white"
}

function sourceLabel(source: CanonicalEvidenceSource) {
  return source.filename || source.label || source.relation || source.sourceId
}

export function CanonicalEvidenceDetail({ record, onOpenSource, onClose, navigation, showHeader = true }: {
  record: CanonicalEvidenceRecord
  onOpenSource: (sourceId: string) => void
  onClose?: () => void
  navigation?: React.ReactNode
  showHeader?: boolean
}) {
  const sources = uniqueCanonicalSources(record.sources)
  const primarySourceId = record.primarySourceId || sources.find((source) => source.primary)?.sourceId || sources[0]?.sourceId

  return <div className="canonical-evidence-detail min-h-0 bg-slate-100/70">
    {showHeader && <header className="border-b border-teal-100 bg-teal-50 p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0"><p className="text-xs font-semibold tracking-wide text-primary uppercase">Historical evidence</p><h2 className="mt-1 break-words text-xl font-semibold">{record.title}</h2>{record.issueLabel && <p className="mt-1 line-clamp-2 text-sm font-medium text-slate-700">{record.issueLabel}</p>}</div>
        <div className="flex shrink-0 items-center gap-1">{navigation}{onClose && <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close evidence panel"><X /></Button>}</div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">{record.city && <span>{record.city}</span>}{record.city && record.round && <span>·</span>}{record.round && <span>Round {record.round}</span>}<EvidenceStatusBadge label={record.statusLabel} tone={record.statusTone} /></div>
    </header>}

    <div className="space-y-4 p-3 sm:p-4 xl:p-5">
      {record.sections.map((section, index) => <section aria-label={section.title} className={`min-w-0 rounded-xl border p-4 shadow-xs sm:p-5 ${sectionTone(section.kind)}`} key={`${section.kind}-${index}`}>
        <p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{section.kind === "comment" ? "Government record" : section.kind === "response" ? "Company record" : "Later review"}</p>
        <h3 className="mt-1 text-lg font-semibold">{section.title}</h3>
        <div className="mt-4 min-w-0 rounded-lg border bg-white p-4 sm:p-5"><EvidenceText text={section.text} blocks={section.blocks} /></div>
      </section>)}

      {!record.sections.some((section) => section.kind === "response") && <section className="rounded-xl border border-amber-200 bg-amber-50/70 p-6 text-center"><Link2Off className="mx-auto size-7 text-amber-700" /><h3 className="mt-2 font-semibold">No confirmed response</h3><p className="mt-1 text-sm text-muted-foreground">The selected evidence contains no confirmed applicant response.</p></section>}

      {sources.length > 0 && <section className="rounded-xl border bg-card p-4 sm:p-5" aria-label="Source evidence">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Source evidence</p><p className="mt-1 text-sm text-muted-foreground">{sources.length} unique {sources.length === 1 ? "source occurrence" : "source occurrences"}</p></div>{primarySourceId && <Button onClick={() => onOpenSource(primarySourceId)}><FileText />Open original source</Button>}</div>
        {sources.length > 1 && <Collapsible className="mt-3 rounded-lg border bg-muted/20 px-3 py-2"><CollapsibleTrigger className="flex w-full items-center justify-between gap-2 text-left text-sm font-medium"><span>{sources.length} sources</span><ChevronDown className="size-4" /></CollapsibleTrigger><CollapsibleContent className="space-y-2 pt-3">{sources.map((source) => <Button variant="outline" className="h-auto w-full justify-start whitespace-normal py-2 text-left" onClick={() => onOpenSource(source.sourceId)} key={source.sourceId}><FileText className="shrink-0" /><span className="min-w-0"><span className="block text-xs font-semibold text-muted-foreground">{source.sourceId === primarySourceId ? "Primary" : source.label || source.relation || "Also appears in"}</span><span className="block break-words">{sourceLabel(source)}</span></span></Button>)}</CollapsibleContent></Collapsible>}
      </section>}
    </div>
  </div>
}
