import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { AlertCircle, FileSearch, Loader2, SheetIcon } from "lucide-react"
import { api } from "@/lib/api"
import type { SourceLocation, SourcePayload, SpreadsheetPayload } from "@/types"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"

declare global {
  interface Window { AdobeDC?: { View: new (options: Record<string, unknown>) => AdobeView } }
}

type AdobeApis = {
  gotoLocation: (page: number, x?: number, y?: number) => Promise<unknown>
  search: (text: string) => Promise<{ onResultsUpdate: (callback: (result: { totalResults?: number; status?: string }) => void) => void; clear?: () => Promise<unknown> }>
}
type AdobeView = {
  previewFile: (file: unknown, config: unknown) => Promise<AdobeView>
  getAPIs: () => Promise<AdobeApis>
  getAnnotationManager: () => Promise<{ addAnnotations: (annotations: unknown[]) => Promise<unknown> }>
}

function formatLocation(location: SourceLocation) {
  if (location.sheet_name) return `${location.sheet_name}${location.cell_range ? ` · ${location.cell_range}` : ""}`
  if (location.page_number) return `${location.preview_document_id ? "Preview page" : "Page"} ${location.page_number}`
  if (location.paragraph_index) return `Paragraph ${location.paragraph_index}`
  return String(location.metadata?.legacy_location || "Location not recorded")
}

function searchCandidates(location: SourceLocation) {
  let quote = String(location.exact_quote || location.normalized_quote || "").replace(/_x000D_/gi, " ").replace(/\s+/g, " ").trim()
  quote = quote.replace(/^(?:general|building|planning|fire|public works|electrical|mechanical|plumbing|structural|civil)\s+(?=(?:please|provide|show|revise|note|verify|indicate|submit|remove|add|clarify|identify|correct|when)\b)/i, "")
  if (!quote) return []
  const sentences = quote.match(/[^.!?]+[.!?]?/g)?.map((value) => value.trim()).filter(Boolean) || [quote]
  const commentNumber = String(location.metadata?.comment_number || "").trim()
  const numbered = commentNumber && /^\d+$/.test(commentNumber)
    ? sentences.flatMap((sentence) => [`${commentNumber} ${sentence}`, `${commentNumber} ${sentence.replace(/[.!?]+$/, "")}`])
    : []
  return [...new Set([...numbered, ...sentences.flatMap((sentence) => [sentence, sentence.replace(/[.!?]+$/, "")])])]
    .filter((candidate) => candidate.split(" ").length >= 4)
    .map((candidate) => candidate.slice(0, 700)).slice(0, 6)
}

function waitForAdobe(timeout = 5000) {
  if (window.AdobeDC?.View) return Promise.resolve()
  return new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("Adobe PDF Embed SDK did not load")), timeout)
    document.addEventListener("adobe_dc_view_sdk.ready", () => {
      window.clearTimeout(timer)
      resolve()
    }, { once: true })
  })
}

function waitForSearch(search: Awaited<ReturnType<AdobeApis["search"]>>, timeout = 2600) {
  return new Promise<boolean>((resolve) => {
    let settled = false
    const finish = (value: boolean) => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      resolve(value)
    }
    const timer = window.setTimeout(() => finish(false), timeout)
    search.onResultsUpdate((result) => {
      if (Number(result?.totalResults || 0) > 0) finish(true)
      else if (String(result?.status || "").toUpperCase() === "COMPLETED") finish(false)
    })
  })
}

function annotation(documentId: string, page: number, box: number[], index: number) {
  const [xMin, yMin, xMax, yMax] = box
  return {
    "@context": ["https://www.w3.org/ns/anno.jsonld", "https://comments.acrobat.com/ns/anno.jsonld"],
    type: "Annotation", id: crypto.randomUUID?.() || `evidence-${Date.now()}-${index}`,
    bodyValue: "Cited evidence", motivation: "commenting",
    target: { source: documentId, selector: { type: "AdobeAnnoSelector", subtype: "highlight", node: { index: Math.max(0, page - 1) }, boundingBox: box, quadPoints: [xMin, yMax, xMax, yMax, xMin, yMin, xMax, yMin], strokeColor: "#f2b84b", opacity: 0.4 } },
    creator: { type: "Person", name: "Permit evidence viewer" }, created: new Date().toISOString(), modified: new Date().toISOString(),
  }
}

function columnNumber(address: string) {
  const letters = /^([A-Za-z]+)(\d+)$/.exec(address)?.[1]
  return letters?.toUpperCase().split("").reduce((total, value) => total * 26 + value.charCodeAt(0) - 64, 0) || 0
}

function selectedCell(address: string, bounds?: number[]) {
  const row = Number(/^([A-Za-z]+)(\d+)$/.exec(address)?.[2] || 0)
  const column = columnNumber(address)
  return Boolean(bounds?.length === 4 && row >= bounds[0] && column >= bounds[1] && row <= bounds[2] && column <= bounds[3])
}

function SpreadsheetViewer({ source, onStatus }: { source: SourcePayload; onStatus: (value: string) => void }) {
  const [sheet, setSheet] = useState(source.location.sheet_name || "")
  const [data, setData] = useState<SpreadsheetPayload | null>(null)
  const [error, setError] = useState("")
  const primaryRef = useRef<HTMLTableCellElement>(null)

  useEffect(() => {
    let active = true
    const citedSheet = source.location.sheet_name || ""
    const row = source.location.metadata?.source_row
    const range = sheet && sheet !== citedSheet ? "" : (source.location.cell_range || (row ? `A${row}:XFD${row}` : ""))
    const params = new URLSearchParams({ sheet, range, page_size: "100" })
    api<SpreadsheetPayload>(`${source.spreadsheet_url}?${params}`).then((payload) => {
      if (!active) return
      setData(payload); setSheet(payload.sheet_name); setError("")
      onStatus(payload.selection ? `Opened ${payload.sheet_name} and highlighted ${payload.selection}.` : `Opened ${payload.sheet_name}.`)
      window.setTimeout(() => primaryRef.current?.scrollIntoView({ block: "center", inline: "center" }), 30)
    }).catch((reason) => active && setError(reason.message))
    return () => { active = false }
  }, [onStatus, sheet, source])

  if (error) return <Alert variant="destructive"><AlertCircle /><AlertTitle>Spreadsheet unavailable</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>
  if (!data) return <div className="space-y-3 p-6"><Skeleton className="h-10 w-72" /><Skeleton className="h-[560px] w-full" /></div>
  let foundPrimary = false
  return <div className="flex h-full min-h-0 flex-col bg-white">
    <div className="flex items-center gap-3 border-b px-4 py-3">
      <SheetIcon className="size-4 text-primary" />
      <Select value={sheet} onValueChange={setSheet}>
        <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
        <SelectContent>{data.sheet_names.map((name) => <SelectItem value={name} key={name}>{name}</SelectItem>)}</SelectContent>
      </Select>
      {data.selection && <Badge variant="outline">Cited range {data.selection}</Badge>}
    </div>
    <div className="spreadsheet-scroll min-h-0 flex-1 overflow-auto">
      <table className="spreadsheet-table">
        <thead><tr><th className="row-head" />{data.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
        <tbody>{data.rows.map((row) => <tr key={row.row_number}>
          <th className="row-head">{row.row_number}</th>
          {data.columns.map((column) => {
            const cell = row.cells.find((item) => item.column === column)
            const address = cell?.address || `${column}${row.row_number}`
            const cited = selectedCell(address, data.selection_bounds)
            const primary = cited && !foundPrimary && String(cell?.value ?? "").trim().length > 0
            if (primary) foundPrimary = true
            return <td ref={primary ? primaryRef : undefined} className={cited ? `cited-cell${primary ? " primary" : ""}` : ""} title={`${address}: ${cell?.value ?? ""}`} key={address}>{cell?.value ?? ""}</td>
          })}
        </tr>)}</tbody>
      </table>
    </div>
  </div>
}

function PdfViewer({ source, onStatus }: { source: SourcePayload; onStatus: (value: string) => void }) {
  const [native, setNative] = useState(false)
  const [loading, setLoading] = useState(true)
  const divId = useMemo(() => `adobe-pdf-${source.source_id.replace(/[^a-z0-9]/gi, "")}`, [source.source_id])

  useEffect(() => {
    let active = true
    async function render() {
      try {
        const config = await api<{ adobe_pdf_embed_client_id: string }>("/api/config")
        if (!config.adobe_pdf_embed_client_id) throw new Error("Adobe client ID is not configured")
        await waitForAdobe()
        if (!active || !window.AdobeDC?.View) return
        const documentId = source.location.preview_document_id || source.document.document_id
        const view = new window.AdobeDC.View({ clientId: config.adobe_pdf_embed_client_id, divId })
        const viewer = await view.previewFile({ content: { location: { url: source.preview_url } }, metaData: { fileName: source.document.filename, id: documentId, hasReadOnlyAccess: true } }, {
          embedMode: "FULL_WINDOW", showDownloadPDF: false, showPrintPDF: false, showAnnotationTools: false, showSaveButton: false,
          enableSearchAPIs: true, enableAnnotationAPIs: Boolean(source.location.pdf_bounding_boxes?.length), includePDFAnnotations: false,
        })
        const page = source.location.page_number || 1
        const boxes = source.location.pdf_bounding_boxes || []
        const apis = await viewer.getAPIs()
        await apis.gotoLocation(page, boxes[0]?.[0] || 0, boxes[0]?.[3] || 0).catch(() => undefined)
        if (boxes.length) {
          try {
            const manager = await viewer.getAnnotationManager()
            await manager.addAnnotations(boxes.map((box, index) => annotation(documentId, page, box, index)))
            onStatus(`Opened page ${page} and highlighted ${boxes.length} stored evidence area${boxes.length === 1 ? "" : "s"}.`)
            setLoading(false); return
          } catch { /* exact-text fallback below */ }
        }
        for (const candidate of searchCandidates(source.location)) {
          try {
            const search = await apis.search(candidate)
            if (await waitForSearch(search)) {
              onStatus(`Opened page ${page} and highlighted the cited sentence.`); setLoading(false); return
            }
            await search.clear?.().catch(() => undefined)
          } catch { /* try a shorter sentence */ }
        }
        onStatus(`Opened page ${page}. The extracted evidence remains fully visible beside the document.`)
        setLoading(false)
      } catch (reason) {
        if (!active) return
        setNative(true); setLoading(false)
        onStatus(`Opened page ${source.location.page_number || 1} in the browser PDF viewer (${(reason as Error).message}).`)
      }
    }
    render()
    return () => { active = false }
  }, [divId, onStatus, source])

  if (native) return <iframe className="size-full border-0" src={`${source.preview_url}#page=${source.location.page_number || 1}`} title="PDF source preview" />
  return <div className="relative size-full"><div id={divId} className="size-full" />{loading && <div className="absolute inset-0 grid place-items-center bg-white"><Loader2 className="size-7 animate-spin text-primary" /></div>}</div>
}

export function SourceViewer({ sourceId, open, onOpenChange }: { sourceId: string | null; open: boolean; onOpenChange: (open: boolean) => void }) {
  const [source, setSource] = useState<SourcePayload | null>(null)
  const [error, setError] = useState("")
  const [status, setStatus] = useState("")
  const stableStatus = useCallback((value: string) => setStatus(value), [])

  useEffect(() => {
    if (!open || !sourceId) return
    setSource(null); setError(""); setStatus("")
    api<SourcePayload>(`/api/sources/${encodeURIComponent(sourceId)}`).then(setSource).catch((reason) => setError(reason.message))
  }, [open, sourceId])

  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="source-dialog flex h-[94vh] max-h-[94vh] w-[98vw] max-w-[98vw] flex-col gap-0 overflow-hidden p-0">
      <DialogHeader className="border-b px-6 py-4">
        <div className="flex items-start justify-between gap-4 pr-8">
          <div><DialogTitle>{source?.document.filename || "Source evidence"}</DialogTitle><DialogDescription>{source ? `${source.relation} · ${source.document.original_document_type.toUpperCase()} · ${(source.document.size / 1024 / 1024).toFixed(1)} MB` : "Loading authorized document…"}</DialogDescription></div>
          {source && <Badge variant="outline">{formatLocation(source.location)}</Badge>}
        </div>
      </DialogHeader>
      {error ? <div className="p-6"><Alert variant="destructive"><AlertCircle /><AlertTitle>Unable to open source</AlertTitle><AlertDescription>{error}</AlertDescription></Alert></div> : !source ? <div className="grid flex-1 grid-cols-[320px_1fr] gap-4 p-5"><Skeleton /><Skeleton /></div> :
        <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(280px,22%)_1fr]">
          <aside className="overflow-y-auto border-b bg-muted/35 p-5 lg:border-r lg:border-b-0">
            <p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Citation</p>
            <p className="mt-1 font-semibold">{formatLocation(source.location)}</p>
            <p className="mt-6 text-xs font-semibold tracking-wide text-muted-foreground uppercase">Extracted evidence</p>
            <blockquote className="mt-2 whitespace-pre-wrap rounded-lg border bg-white p-4 text-sm leading-7 shadow-xs">{source.location.exact_quote || "No extracted evidence text is available for this referenced file."}</blockquote>
            {status && <Alert className="mt-5"><FileSearch /><AlertTitle>Viewer status</AlertTitle><AlertDescription>{status}</AlertDescription></Alert>}
          </aside>
          <section className="min-h-[65vh] overflow-hidden bg-muted/20">
            {(["pdf", "pdf_preview"].includes(source.location.viewer_type) && !(source.document.viewer_type === "pdf_preview" && source.document.preview_status !== "ready")) && <PdfViewer source={source} onStatus={stableStatus} />}
            {source.location.viewer_type === "spreadsheet" && <SpreadsheetViewer source={source} onStatus={stableStatus} />}
            {(source.location.viewer_type === "unsupported" || (source.document.viewer_type === "pdf_preview" && source.document.preview_status !== "ready")) && <div className="grid h-full place-items-center p-8"><Alert className="max-w-lg"><AlertCircle /><AlertTitle>Preview unavailable</AlertTitle><AlertDescription>{source.document.preview_error || "This format does not have an in-app preview. File metadata and extracted evidence remain available."}</AlertDescription></Alert></div>}
          </section>
        </div>}
    </DialogContent>
  </Dialog>
}
