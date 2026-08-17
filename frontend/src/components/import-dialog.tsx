import { useEffect, useMemo, useRef, useState } from "react"
import { AlertTriangle, CheckCircle2, DatabaseZap, FolderOpen, Loader2, RefreshCw, UploadCloud } from "lucide-react"
import { api } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"

type Job = {
  job_id: string
  mode: "inventory" | "prescan" | "ingest"
  site_label: string
  status: string
  stage: string
  message: string
  log_tail?: string[]
}
type IngestionState = {
  enabled: boolean
  dependencies: { ghostscript: boolean; tesseract: boolean; libreoffice: boolean; gemini_key: boolean }
  active_job: Job | null
  jobs: Job[]
}
type SelectedUpload = { file: File; relativePath: string }
type UploadSession = {
  upload_id: string
  project_name: string
  file_count: number
  size_bytes: number
  files: { file_id: string; relative_path: string; size: number }[]
}

const SUPPORTED_EXTENSIONS = new Set(["pdf", "doc", "docx", "xls", "xlsx", "csv"])

function sizeLabel(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function extension(path: string) {
  return path.split(".").pop()?.toLocaleLowerCase() || ""
}

export function ImportDialog({ open, onOpenChange, onCompleted }: { open: boolean; onOpenChange: (open: boolean) => void; onCompleted?: () => void }) {
  const pickerRef = useRef<HTMLInputElement | null>(null)
  const reportedJob = useRef("")
  const [state, setState] = useState<IngestionState | null>(null)
  const [projectName, setProjectName] = useState("")
  const [selectedFiles, setSelectedFiles] = useState<SelectedUpload[]>([])
  const [ignoredCount, setIgnoredCount] = useState(0)
  const [uploading, setUploading] = useState(false)
  const [uploadedCount, setUploadedCount] = useState(0)
  const [uploadStage, setUploadStage] = useState("")
  const [error, setError] = useState("")

  const latest = state?.jobs[0]
  const shownJob = state?.active_job || latest
  const selectedSize = useMemo(() => selectedFiles.reduce((total, item) => total + item.file.size, 0), [selectedFiles])
  const extensionCounts = useMemo(() => selectedFiles.reduce<Record<string, number>>((counts, item) => {
    const label = extension(item.relativePath).toUpperCase()
    counts[label] = (counts[label] || 0) + 1
    return counts
  }, {}), [selectedFiles])

  async function refresh() {
    try {
      setState(await api<IngestionState>("/api/ingestion"))
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  useEffect(() => { if (open) refresh() }, [open])
  useEffect(() => {
    if (!open || !state?.active_job) return
    const timer = window.setInterval(refresh, 2000)
    return () => window.clearInterval(timer)
  }, [open, state?.active_job?.job_id])
  useEffect(() => {
    if (latest?.status !== "completed" || latest.mode !== "ingest" || reportedJob.current === latest.job_id) return
    reportedJob.current = latest.job_id
    onCompleted?.()
  }, [latest?.job_id, latest?.status, latest?.mode, onCompleted])

  function chooseFolder(files: FileList | null) {
    setError("")
    setUploadedCount(0)
    setUploadStage("")
    const picked = Array.from(files || [])
    if (!picked.length) return
    const firstPath = picked[0].webkitRelativePath || picked[0].name
    const root = firstPath.includes("/") ? firstPath.split("/")[0] : picked[0].name.replace(/\.[^.]+$/, "")
    const supported: SelectedUpload[] = []
    let ignored = 0
    for (const file of picked) {
      const browserPath = file.webkitRelativePath || file.name
      const parts = browserPath.split("/").filter(Boolean)
      const relativePath = parts.length > 1 ? parts.slice(1).join("/") : file.name
      if (!SUPPORTED_EXTENSIONS.has(extension(relativePath))) {
        ignored += 1
        continue
      }
      supported.push({ file, relativePath })
    }
    setProjectName(root)
    setSelectedFiles(supported)
    setIgnoredCount(ignored)
    if (!supported.length) setError("This folder contains no supported PDF, Word, Excel, or CSV files.")
  }

  async function uploadBinary(uploadId: string, fileId: string, file: File) {
    const response = await fetch(`/api/ingestion/uploads/${uploadId}/files/${fileId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    })
    const payload = await response.json().catch(() => ({})) as { error?: string }
    if (!response.ok) throw new Error(payload.error || `File upload failed (${response.status})`)
  }

  async function uploadAndIngest() {
    if (!selectedFiles.length || !projectName || uploading || state?.active_job) return
    setUploading(true)
    setError("")
    setUploadedCount(0)
    try {
      setUploadStage("Preparing secure upload")
      const session = await api<UploadSession>("/api/ingestion/uploads", {
        method: "POST",
        body: JSON.stringify({
          project_name: projectName,
          files: selectedFiles.map((item) => ({ relative_path: item.relativePath, size: item.file.size })),
        }),
      })
      const localFiles = new Map(selectedFiles.map((item) => [item.relativePath, item.file]))
      for (const [index, remote] of session.files.entries()) {
        const file = localFiles.get(remote.relative_path)
        if (!file) throw new Error(`The selected folder changed before ${remote.relative_path} was uploaded`)
        setUploadStage(`Uploading ${index + 1} of ${session.files.length}: ${remote.relative_path}`)
        await uploadBinary(session.upload_id, remote.file_id, file)
        setUploadedCount(index + 1)
      }
      setUploadStage("Starting the complete ingestion pipeline")
      const next = await api<IngestionState>(`/api/ingestion/uploads/${session.upload_id}/complete`, {
        method: "POST",
        body: "{}",
      })
      setState(next)
      setUploadStage("Upload complete. Ingestion is running in the background.")
    } catch (reason) {
      setError((reason as Error).message)
      setUploadStage("")
    } finally {
      setUploading(false)
    }
  }

  const canStart = Boolean(
    state?.enabled && state.dependencies.gemini_key && !state.active_job && selectedFiles.length && !uploading,
  )

  return <Dialog open={open} onOpenChange={(next) => { if (!uploading) onOpenChange(next) }}>
    <DialogContent className="flex max-h-[90vh] max-w-3xl flex-col overflow-hidden">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2"><DatabaseZap className="size-5 text-primary" />Import project data</DialogTitle>
        <DialogDescription>Choose one complete project folder, then run the full accuracy-first ingestion with one button.</DialogDescription>
      </DialogHeader>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-2">
        {error && <Alert variant="destructive"><AlertTriangle /><AlertTitle>Import could not continue</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
        {!state ? <div className="grid min-h-56 place-items-center"><Loader2 className="size-7 animate-spin text-primary" /></div> : <>
          {!state.enabled && <Alert variant="destructive"><AlertTriangle /><AlertTitle>Management entrance disabled</AlertTitle><AlertDescription>Run the app in local-maintenance mode before importing project data.</AlertDescription></Alert>}
          {!state.dependencies.gemini_key && <Alert variant="destructive"><AlertTriangle /><AlertTitle>Gemini is not configured</AlertTitle><AlertDescription>Add the same Gemini API key used by Smart Search, then restart the server.</AlertDescription></Alert>}

          <input
            ref={(node) => {
              pickerRef.current = node
              node?.setAttribute("webkitdirectory", "")
              node?.setAttribute("directory", "")
            }}
            className="hidden"
            type="file"
            multiple
            aria-label="Choose project folder"
            onChange={(event) => chooseFolder(event.target.files)}
          />
          <button
            type="button"
            className="flex w-full items-center gap-4 rounded-xl border border-dashed border-primary/40 bg-primary/[0.035] p-5 text-left transition-colors hover:bg-primary/[0.07]"
            onClick={() => pickerRef.current?.click()}
            disabled={uploading}
          >
            <span className="grid size-11 shrink-0 place-items-center rounded-full bg-primary/10 text-primary"><FolderOpen className="size-5" /></span>
            <span className="min-w-0 flex-1">
              <span className="block font-semibold">{projectName || "Choose a complete project folder"}</span>
              <span className="mt-1 block text-sm text-muted-foreground">Original filenames and nested folders are preserved. PDF, Word, Excel, and CSV are supported.</span>
            </span>
            <span className="rounded-md border bg-background px-3 py-1.5 text-sm font-medium">Browse</span>
          </button>

          {!!selectedFiles.length && <div className="rounded-xl border bg-card p-4">
            <div className="flex flex-wrap items-center gap-2">
              <p className="mr-auto font-semibold">Ready to import</p>
              <Badge variant="secondary">{selectedFiles.length} supported file{selectedFiles.length === 1 ? "" : "s"}</Badge>
              <Badge variant="secondary">{sizeLabel(selectedSize)}</Badge>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">{Object.entries(extensionCounts).map(([name, count]) => <Badge key={name} variant="outline">{name} {count}</Badge>)}</div>
            {ignoredCount > 0 && <p className="mt-3 text-xs text-muted-foreground">{ignoredCount} unsupported file{ignoredCount === 1 ? " was" : "s were"} ignored.</p>}
          </div>}

          <Alert><UploadCloud /><AlertTitle>One action runs the complete pipeline</AlertTitle><AlertDescription>After upload, the server automatically inventories files, prescans for relevant evidence, extracts and verifies records, deduplicates canonical events, rebuilds timelines, and refreshes source links and search metadata. Gemini processing can take several minutes and may incur API cost; existing hashes and checkpoints are reused.</AlertDescription></Alert>

          {(uploading || uploadStage) && <div className="rounded-xl border bg-muted/40 p-4">
            <div className="flex items-center gap-3">{uploading ? <Loader2 className="size-5 animate-spin text-primary" /> : <CheckCircle2 className="size-5 text-green-700" />}<div><p className="font-medium">{uploadStage}</p>{uploading && <p className="mt-1 text-xs text-muted-foreground">{uploadedCount} of {selectedFiles.length} files uploaded</p>}</div></div>
          </div>}

          {shownJob && <div className="rounded-xl border bg-card p-4">
            <div className="flex flex-wrap items-center gap-2"><p className="font-semibold">Latest ingestion</p><Badge className={shownJob.status === "completed" ? "bg-green-700" : shownJob.status === "failed" ? "bg-red-700" : ""}>{shownJob.status}</Badge><Badge variant="outline">{shownJob.stage}</Badge><span className="ml-auto text-xs text-muted-foreground">{shownJob.site_label}</span></div>
            <p className="mt-2 text-sm text-muted-foreground">{shownJob.message}</p>
            {!!shownJob.log_tail?.length && <pre className="mt-3 max-h-48 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] leading-5 text-slate-100">{shownJob.log_tail.join("\n")}</pre>}
            {shownJob.status === "completed" && <div className="mt-3 flex items-center gap-2 text-sm text-green-700"><CheckCircle2 className="size-4" />Dataset and derived indexes are ready.</div>}
          </div>}
        </>}
      </div>
      <DialogFooter>
        <Button variant="outline" disabled={uploading} onClick={() => onOpenChange(false)}>Close</Button>
        <Button variant="outline" disabled={uploading} onClick={refresh}><RefreshCw />Refresh status</Button>
        <Button disabled={!canStart} onClick={uploadAndIngest}>{uploading || state?.active_job ? <Loader2 className="animate-spin" /> : <UploadCloud />}{state?.active_job ? "Ingestion running" : uploading ? "Uploading" : "Upload and ingest"}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}
