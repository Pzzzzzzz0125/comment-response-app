import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { AlertCircle, FileSearch, Loader2, RefreshCw, SheetIcon, X } from "lucide-react"
import { api } from "@/lib/api"
import type { SourceLocation, SourcePayload, SpreadsheetPayload } from "@/types"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"

declare global {
  interface Window { AdobeDC?: { View: AdobeViewConstructor } }
}

type AdobeZoomApis = {
  getZoomLimits?: () => Promise<{ minZoom?: number; maxZoom?: number }>
  setZoomLevel: (zoom: number) => Promise<unknown>
}
type AdobeApis = {
  gotoLocation: (page: number, x?: number, y?: number) => Promise<unknown>
  getCurrentPage?: () => Promise<number>
  getZoomAPIs?: () => AdobeZoomApis
  search: (text: string) => Promise<{ onResultsUpdate: (callback: (result: { totalResults?: number; status?: string }) => void) => void; clear?: () => Promise<unknown> }>
}
type AdobeAnnotation = {
  id: string
  [key: string]: unknown
}
type AdobeAnnotationManager = {
  addAnnotations: (annotations: AdobeAnnotation[]) => Promise<unknown>
  getAnnotations?: (filter: { annotationIds?: string[]; pageRange?: { startPage: number; endPage: number } }) => Promise<AdobeAnnotation[]>
  selectAnnotation?: (annotationId: string) => Promise<unknown>
}
type AdobeViewer = {
  getAPIs: () => Promise<AdobeApis>
  getAnnotationManager: () => Promise<AdobeAnnotationManager>
}
type AdobeView = {
  previewFile: (file: unknown, config: unknown) => Promise<AdobeViewer>
  registerCallback?: (type: unknown, callback: (event: { type?: string }) => void, options: Record<string, unknown>) => void
}
type AdobeViewConstructor = {
  new (options: Record<string, unknown>): AdobeView
  Enum?: {
    CallbackType?: { EVENT_LISTENER?: unknown }
    Events?: { PDF_VIEWER_READY?: unknown }
  }
}

const TARGETED_PDF_ZOOM_PERCENT = 175
const TARGETED_PDF_FILENAMES = new Set([
  "PC2- 2311 Warner Range Ave Plans-Reviewed-Corrections-Required-to civil.pdf",
  "PC2- 2311 Warner Range Ave Plans-Reviewed-Corrections-Required.pdf",
  "PC2- 2311 Warner Range Ave Plans-Reviewed-Corrections-Required-structure.pdf",
].map(normalizePdfFilename))

function normalizePdfFilename(value: string) {
  return value.normalize("NFKC")
    .replace(/[‐‑‒–—]/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase()
}

export function pdfZoomForFilename(filename: string) {
  return TARGETED_PDF_FILENAMES.has(normalizePdfFilename(filename))
    ? TARGETED_PDF_ZOOM_PERCENT
    : null
}

function formatLocation(location: SourceLocation) {
  if (location.sheet_name) return `${location.sheet_name}${location.cell_range ? ` · ${location.cell_range}` : ""}`
  if (location.page_number) return `${location.preview_document_id ? "Preview page" : "Page"} ${location.page_number}`
  if (location.paragraph_index) return `Paragraph ${location.paragraph_index}`
  return String(location.metadata?.legacy_location || "Location not recorded")
}

function searchCandidates(location: SourceLocation) {
  const normalize = (value: string) => value
    .replace(/_x000D_/gi, " ")
    .replace(/\*x000d\*/gi, " ")
    .replace(/&quot;|&#34;/gi, '"')
    .replace(/&apos;|&#39;/gi, "'")
    .replace(/[“”″]/g, '"')
    .replace(/[‘’]/g, "'")
    .replace(/[‐‑‒–—]/g, "-")
    .replace(/[\u00a0\u200b]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
  const raw = normalize(String(location.exact_quote || location.normalized_quote || ""))
  if (!raw) return []
  const withoutPrefix = raw
    .replace(/^\s*(?:\([A-Z]\)\s*)?(?:PC\s*\d+\s*[:\-]\s*)/i, "")
    .replace(/^(?:general|building|planning|fire|public works|electrical|mechanical|plumbing|structural|civil)\s+(?=(?:please|provide|show|revise|note|verify|indicate|submit|remove|add|clarify|identify|correct|when)\b)/i, "")
    .trim()
  const sentences = withoutPrefix.match(/[^.!?]+[.!?]?/g)?.map((value) => value.trim()).filter(Boolean) || [withoutPrefix]
  const commentNumber = String(location.metadata?.comment_number || "").trim()
  const numbered = commentNumber && /^\d+$/.test(commentNumber)
    ? sentences.flatMap((sentence) => [`${commentNumber} ${sentence}`, `${commentNumber} ${sentence.replace(/[.!?]+$/, "")}`])
    : []
  const punctuationFree = (value: string) => value
    .replace(/[“”″]/g, '"')
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim()
  const compact = punctuationFree(withoutPrefix)
  const short = withoutPrefix.match(/^(?:please\s+)?(?:revise|provide|show|add|remove|correct|verify|indicate)\b[^.!?]{0,180}/i)?.[0]?.trim() || ""
  const variants = [
    raw,
    withoutPrefix,
    withoutPrefix.replace(/[.!?]+$/, ""),
    ...sentences.flatMap((sentence) => [sentence, sentence.replace(/[.!?]+$/, "")]),
    short,
    punctuationFree(short),
    ...numbered,
    compact,
  ]
  return [...new Set(variants.map(normalize))]
    .filter((candidate) => candidate.split(" ").length >= 4)
    .map((candidate) => candidate.slice(0, 700))
    .slice(0, 10)
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

function delay(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds))
}

function waitForViewerReady(view: AdobeView, timeout = 12000) {
  return new Promise<boolean>((resolve) => {
    let settled = false
    const finish = (ready: boolean) => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      resolve(ready)
    }
    const timer = window.setTimeout(() => finish(false), timeout)
    const adobeView = window.AdobeDC?.View
    const callbackType = adobeView?.Enum?.CallbackType?.EVENT_LISTENER
    const readyEvent = adobeView?.Enum?.Events?.PDF_VIEWER_READY || "PDF_VIEWER_READY"
    if (!view.registerCallback || !callbackType) {
      window.clearTimeout(timer)
      resolve(false)
      return
    }
    try {
      view.registerCallback(callbackType, (event) => {
        if (String(event?.type || "") === String(readyEvent)) finish(true)
      }, { enableFilePreviewEvents: true, listenOn: [readyEvent] })
    } catch {
      finish(false)
    }
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

async function highlightPdfText(apis: AdobeApis, location: SourceLocation) {
  for (const candidate of searchCandidates(location)) {
    try {
      const search = await apis.search(candidate)
      if (await waitForSearch(search)) return true
      await search.clear?.().catch(() => undefined)
    } catch { /* try the next conservative text variant */ }
  }
  return false
}

function annotation(documentId: string, page: number, box: number[], index: number): AdobeAnnotation {
  const [xMin, yMin, xMax, yMax] = box
  return {
    "@context": ["https://www.w3.org/ns/anno.jsonld", "https://comments.acrobat.com/ns/anno.jsonld"],
    type: "Annotation", id: crypto.randomUUID?.() || `evidence-${Date.now()}-${index}`,
    bodyValue: "Cited evidence", motivation: "commenting",
    target: { source: documentId, selector: { type: "AdobeAnnoSelector", subtype: "highlight", node: { index: Math.max(0, page - 1) }, boundingBox: box, quadPoints: [xMin, yMax, xMax, yMax, xMin, yMin, xMax, yMin], strokeColor: "#ffd43b", opacity: 0.62 } },
    creator: { type: "Person", name: "Permit evidence viewer" }, created: new Date().toISOString(), modified: new Date().toISOString(),
  }
}

function validPdfBoxes(boxes: number[][] | undefined) {
  return (boxes || []).filter((box) => box.length === 4
    && box.every(Number.isFinite)
    && box[2] > box[0]
    && box[3] > box[1])
}

export function pdfBoxesByPage(location: SourceLocation) {
  const grouped = new Map<number, number[][]>()
  for (const [page, boxes] of Object.entries(location.pdf_bounding_boxes_by_page || {})) {
    const pageNumber = Number(page)
    const valid = validPdfBoxes(boxes)
    if (Number.isInteger(pageNumber) && pageNumber > 0 && valid.length) grouped.set(pageNumber, valid)
  }
  const fallbackPage = Number(location.page_number || 1)
  const fallback = validPdfBoxes(location.pdf_bounding_boxes)
  if (!grouped.size && fallback.length) grouped.set(fallbackPage, fallback)
  else if (fallback.length && !grouped.has(fallbackPage)) grouped.set(fallbackPage, fallback)
  return grouped
}

export function pdfFocusForBoxes(boxes: number[][]) {
  if (!boxes.length) return null
  const xMin = Math.min(...boxes.map((box) => box[0]))
  const yMin = Math.min(...boxes.map((box) => box[1]))
  const xMax = Math.max(...boxes.map((box) => box[2]))
  const yMax = Math.max(...boxes.map((box) => box[3]))
  return { x: (xMin + xMax) / 2, y: (yMin + yMax) / 2 }
}

async function zoomPdf(apis: AdobeApis, requestedZoom: number) {
  const zoomApis = apis.getZoomAPIs?.()
  if (!zoomApis) return null
  let limits: { minZoom?: number; maxZoom?: number } | undefined
  try { limits = await zoomApis.getZoomLimits?.() } catch { /* use the requested zoom */ }
  const minimum = Number.isFinite(limits?.minZoom) ? Number(limits?.minZoom) : requestedZoom
  const maximum = Number.isFinite(limits?.maxZoom) ? Number(limits?.maxZoom) : requestedZoom
  const zoom = Math.max(minimum, Math.min(maximum, requestedZoom))
  await zoomApis.setZoomLevel(zoom)
  return zoom
}

async function locatePdf(apis: AdobeApis, page: number, focus?: { x: number; y: number } | null, zoom?: number | null) {
  await apis.gotoLocation(page)
  if (zoom) await zoomPdf(apis, zoom).catch(() => null)
  if (focus) await apis.gotoLocation(page, focus.x, focus.y)
  if (apis.getCurrentPage && await apis.getCurrentPage().catch(() => page) !== page) {
    await delay(180)
    await apis.gotoLocation(page, focus?.x, focus?.y)
  }
}

function columnNumber(address: string) {
  const letters = /^([A-Za-z]+)(\d+)$/.exec(address)?.[1]
  return letters?.toUpperCase().split("").reduce((total, value) => total * 26 + value.charCodeAt(0) - 64, 0) || 0
}

function columnLetters(number: number) {
  let value = ""
  let current = Math.max(1, number)
  while (current > 0) {
    const remainder = (current - 1) % 26
    value = String.fromCharCode(65 + remainder) + value
    current = Math.floor((current - 1) / 26)
  }
  return value
}

/**
 * Keep the cited cell as the selection, but ask the API for a bounded row
 * context so the viewer can show the other fields from the same workbook row.
 * The old XFD fallback made the request look like a single-cell citation and
 * could also create an unnecessarily wide table.
 */
export function spreadsheetContextRange(cellRange?: string, sourceRow?: unknown) {
  const parsed = /^\s*([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?\s*$/.exec(cellRange || "")
  const row = Number(sourceRow)
  if (!parsed && !Number.isInteger(row)) return ""
  const startRow = parsed ? Math.min(Number(parsed[2]), Number(parsed[4] || parsed[2])) : row
  const endRow = parsed ? Math.max(Number(parsed[2]), Number(parsed[4] || parsed[2])) : row
  const endColumn = parsed
    ? Math.min(52, Math.max(26, columnNumber(`${parsed[3] || parsed[1]}1`) + 8))
    : 26
  return `A${startRow}:${columnLetters(endColumn)}${endRow}`
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
    const sameSheet = !sheet || !citedSheet || sheet === citedSheet
    const range = sameSheet ? (source.location.cell_range || (row ? `A${row}` : "")) : ""
    const contextRange = sameSheet ? spreadsheetContextRange(range, row) : ""
    const params = new URLSearchParams({ sheet, range, page_size: "100" })
    if (contextRange) params.set("context_range", contextRange)
    api<SpreadsheetPayload>(`${source.spreadsheet_url}?${params}`).then((payload) => {
      if (!active) return
      setData(payload); setSheet(payload.sheet_name); setError("")
      onStatus(payload.selection
        ? `Opened ${payload.sheet_name}, highlighted ${payload.selection}, and showed the other cells in the cited row.`
        : `Opened ${payload.sheet_name}.`)
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
      {data.context_range && <Badge variant="secondary">Same-row context {data.context_range}</Badge>}
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
            const inContext = Boolean(data.context_bounds?.length === 4
              && row.row_number >= data.context_bounds[0]
              && row.row_number <= data.context_bounds[2])
            const primary = cited && !foundPrimary && String(cell?.value ?? "").trim().length > 0
            if (primary) foundPrimary = true
            const className = [inContext ? "context-cell" : "", cited ? `cited-cell${primary ? " primary" : ""}` : ""].filter(Boolean).join(" ")
            return <td ref={primary ? primaryRef : undefined} className={className} title={`${address}: ${cell?.value ?? ""}`} key={address}>{cell?.value ?? ""}</td>
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
        const readyPromise = waitForViewerReady(view)
        const viewer = await view.previewFile({ content: { location: { url: source.preview_url } }, metaData: { fileName: source.document.filename, id: documentId } }, {
          embedMode: "FULL_WINDOW", showDownloadPDF: false, showPrintPDF: false, showAnnotationTools: false, showSaveButton: false,
          defaultViewMode: "FIT_WIDTH", showZoomControl: true,
          enableFormFilling: false, enableSearchAPIs: true, enableAnnotationAPIs: true, includePDFAnnotations: false,
          annotationUIConfig: { showToolbar: false, showCommentsPanel: false, showToolsOnTextSelection: false },
        })
        const pageBoxes = pdfBoxesByPage(source.location)
        const page = source.location.page_number || [...pageBoxes.keys()][0] || 1
        const boxes = pageBoxes.get(page) || []
        const targetedZoom = pdfZoomForFilename(source.document.filename)
        const ready = await readyPromise
        // Older SDK builds do not emit PDF_VIEWER_READY. previewFile can still
        // resolve before navigation and annotation APIs are usable, so allow a
        // short render grace period in that compatibility path.
        if (!ready) await delay(700)
        if (!active) return
        const apis = await viewer.getAPIs()
        let navigationError = ""
        try {
          await locatePdf(apis, page, pdfFocusForBoxes(boxes), targetedZoom)
        } catch (reason) {
          navigationError = (reason as Error).message || "page navigation failed"
        }
        // Stored geometry is tied to the cited page and is therefore both more
        // precise and less ambiguous than document-wide text search.
        if (boxes.length) {
          try {
            const manager = await viewer.getAnnotationManager()
            const annotations = [...pageBoxes.entries()].flatMap(([pageNumber, pageBoxesForPage]) =>
              pageBoxesForPage.map((box, index) => annotation(documentId, pageNumber, box, index)),
            )
            await manager.addAnnotations(annotations)
            await manager.selectAnnotation?.(annotations[0].id)
            const textHighlighted = await highlightPdfText(apis, source.location)
            await locatePdf(apis, page, pdfFocusForBoxes(boxes), targetedZoom)
            onStatus(`Opened page ${page}${targetedZoom ? ` at ${targetedZoom}%` : " at normal fit-to-width size"} and highlighted the cited evidence on ${pageBoxes.size} page${pageBoxes.size === 1 ? "" : "s"}${textHighlighted ? " using its stored coordinates and exact text" : " using its stored coordinates"}.`)
            setLoading(false); return
          } catch (reason) {
            navigationError ||= (reason as Error).message || "coordinate highlight failed"
          }
        }
        // Fall back to exact-text search only when reliable coordinates are not
        // available or the Adobe annotation API rejected them.
        if (await highlightPdfText(apis, source.location)) {
          await locatePdf(apis, page, null, targetedZoom).catch(() => undefined)
          onStatus(`Opened page ${page} and highlighted the cited text using exact-text search.`); setLoading(false); return
        }
        onStatus(`Opened page ${page}, but no reliable in-document highlight was available${navigationError ? ` (${navigationError})` : ""}. The extracted evidence remains visible beside the document.`)
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

  const nativeZoom = pdfZoomForFilename(source.document.filename)
  if (native) return <iframe className="size-full border-0" src={`${source.preview_url}#page=${source.location.page_number || 1}${nativeZoom ? `&zoom=${nativeZoom}` : ""}`} title="PDF source preview" />
  return <div className="relative size-full"><div id={divId} className="size-full" />{loading && <div className="absolute inset-0 grid place-items-center bg-white"><Loader2 className="size-7 animate-spin text-primary" /></div>}</div>
}

type SourceViewerContentProps = {
  sourceId: string | null
  open: boolean
  embedded?: boolean
  onClose?: () => void
  headerContext?: { title?: string; subtitle?: string; eventType?: string }
  toolbar?: React.ReactNode
}

export function SourceViewerContent({ sourceId, open, embedded = false, onClose, headerContext, toolbar }: SourceViewerContentProps) {
  const [source, setSource] = useState<SourcePayload | null>(null)
  const [error, setError] = useState("")
  const [status, setStatus] = useState("")
  const [reloadKey, setReloadKey] = useState(0)
  const stableStatus = useCallback((value: string) => setStatus(value), [])

  useEffect(() => {
    if (!open || !sourceId) return
    setSource(null); setError(""); setStatus("")
    api<SourcePayload>(`/api/sources/${encodeURIComponent(sourceId)}`).then(setSource).catch((reason) => setError(reason.message))
  }, [open, sourceId, reloadKey])

  return <div className="flex size-full min-h-0 flex-col overflow-hidden bg-background">
      <div className="border-b px-4 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate font-semibold">{headerContext?.title || source?.document.filename || "Source evidence"}</p>
            <p className="mt-0.5 truncate text-xs text-muted-foreground">{headerContext?.subtitle || (source ? `${source.relation} · ${source.document.original_document_type.toUpperCase()} · ${(source.document.size / 1024 / 1024).toFixed(1)} MB` : "Loading authorized document…")}</p>
            {headerContext?.eventType && <p className="mt-1 text-xs font-medium text-primary">{headerContext.eventType}</p>}
          </div>
          <div className="flex shrink-0 items-center gap-2">{toolbar}{source && <Badge variant="outline" className="hidden max-w-52 truncate sm:inline-flex">{formatLocation(source.location)}</Badge>}{embedded && onClose && <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close source panel"><X /></Button>}</div>
        </div>
      </div>
      {error ? <div className="p-6"><Alert variant="destructive"><AlertCircle /><AlertTitle>Unable to open source</AlertTitle><AlertDescription>{error}</AlertDescription><Button className="mt-4" variant="outline" size="sm" onClick={() => setReloadKey((value) => value + 1)}><RefreshCw />Retry</Button></Alert></div> : !source ? <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 p-5 lg:grid-cols-[minmax(220px,28%)_1fr]"><Skeleton /><Skeleton /></div> :
        <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(280px,22%)_1fr]">
          <aside className="overflow-y-auto border-b bg-muted/35 p-5 lg:border-r lg:border-b-0">
            <p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Citation</p>
            <p className="mt-1 font-semibold">{formatLocation(source.location)}</p>
            <p className="mt-6 text-xs font-semibold tracking-wide text-muted-foreground uppercase">Extracted evidence</p>
            <blockquote className="mt-2 whitespace-pre-wrap rounded-lg border bg-white p-4 text-sm leading-7 shadow-xs">{source.location.exact_quote || "No extracted evidence text is available for this referenced file."}</blockquote>
            {status && <Alert className="mt-5"><FileSearch /><AlertTitle>Viewer status</AlertTitle><AlertDescription>{status}</AlertDescription></Alert>}
          </aside>
          <section className={embedded ? "min-h-[55vh] overflow-hidden bg-muted/20 lg:min-h-0" : "min-h-[65vh] overflow-hidden bg-muted/20"}>
            {(["pdf", "pdf_preview"].includes(source.location.viewer_type) && !(source.document.viewer_type === "pdf_preview" && source.document.preview_status !== "ready")) && <PdfViewer source={source} onStatus={stableStatus} />}
            {source.location.viewer_type === "spreadsheet" && <SpreadsheetViewer source={source} onStatus={stableStatus} />}
            {(source.location.viewer_type === "unsupported" || (source.document.viewer_type === "pdf_preview" && source.document.preview_status !== "ready")) && <div className="grid h-full place-items-center p-8"><Alert className="max-w-lg"><AlertCircle /><AlertTitle>Preview unavailable</AlertTitle><AlertDescription>{source.document.preview_error || "This format does not have an in-app preview. File metadata and extracted evidence remain available."}</AlertDescription></Alert></div>}
          </section>
        </div>}
  </div>
}

export function SourceViewer({ sourceId, open, onOpenChange }: { sourceId: string | null; open: boolean; onOpenChange: (open: boolean) => void }) {
  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="source-dialog flex h-[94vh] max-h-[94vh] w-[98vw] max-w-[98vw] flex-col gap-0 overflow-hidden p-0">
      <DialogHeader className="sr-only"><DialogTitle>Source evidence</DialogTitle><DialogDescription>Original authorized document and stored evidence locator.</DialogDescription></DialogHeader>
      <SourceViewerContent sourceId={sourceId} open={open} />
    </DialogContent>
  </Dialog>
}
