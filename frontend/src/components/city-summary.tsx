import { ArrowRight, BarChart3, Building2, FileStack, Fingerprint, Layers3, Wrench } from "lucide-react"
import type { CityAnalysis } from "@/types"
import { Badge } from "@/components/ui/badge"
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

export function CitySummary({ city, analysis, onOpenTopic }: { city: string; analysis: CityAnalysis; onOpenTopic?: (topic: CommonTopic) => void }) {
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
    </CardContent>
  </Card>
}
