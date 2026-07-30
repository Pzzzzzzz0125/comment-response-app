import { afterEach, describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import { SourceViewer } from "@/components/source-viewer"

afterEach(() => vi.restoreAllMocks())

describe("SourceViewer", () => {
  it("loads an opaque source id and shows evidence without navigating away", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      source_id: "source-1",
      relation: "Primary source",
      preview_url: "/api/documents/document-1/preview",
      spreadsheet_url: "/api/documents/document-1/spreadsheet",
      location: { document_id: "document-1", original_document_type: "bin", viewer_type: "unsupported", exact_quote: "Exact cited evidence remains visible." },
      document: { document_id: "document-1", filename: "Evidence.bin", original_document_type: "bin", viewer_type: "unsupported", size: 1200 },
    }), { status: 200, headers: { "Content-Type": "application/json" } })))

    render(<SourceViewer sourceId="source-1" open onOpenChange={vi.fn()} />)
    expect(await screen.findByText("Evidence.bin")).toBeInTheDocument()
    expect(screen.getByText("Exact cited evidence remains visible.")).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith("/api/sources/source-1", expect.any(Object))
  })
})
