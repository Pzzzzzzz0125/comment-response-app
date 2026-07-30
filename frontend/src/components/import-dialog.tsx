import { DatabaseZap, Terminal } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"

export function ImportDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent><DialogHeader><DialogTitle className="flex items-center gap-2"><DatabaseZap className="size-5 text-primary" />Import data</DialogTitle><DialogDescription>New documents use the verified visual-ingestion pipeline before records appear in this knowledge base.</DialogDescription></DialogHeader><Alert><Terminal /><AlertTitle>Importer remains server-side</AlertTitle><AlertDescription>Run the documented ingestion command from an authorized workstation. Browser uploads are intentionally unavailable until authentication, file-size limits, and job monitoring are configured.</AlertDescription></Alert><div className="rounded-lg bg-muted p-4 font-mono text-xs leading-6">python3 phase2/incremental_update.py --help</div></DialogContent></Dialog>
}
