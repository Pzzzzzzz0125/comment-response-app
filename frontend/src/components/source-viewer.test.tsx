import { afterEach, describe, expect, it, vi } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import { pdfBoxesByPage, pdfFocusForBoxes, pdfZoomForFilename, SourceViewer, spreadsheetContextRange } from "@/components/source-viewer"

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

  it("waits for the PDF viewer, opens the cited page at normal size, and adds coordinate highlights", async () => {
    const gotoLocation = vi.fn().mockResolvedValue(undefined)
    const addAnnotations = vi.fn().mockResolvedValue(undefined)
    const getAnnotations = vi.fn().mockImplementation(({ annotationIds }: { annotationIds?: string[] }) => Promise.resolve((annotationIds || []).map((id) => ({ id }))))
    const selectAnnotation = vi.fn().mockResolvedValue(undefined)
    const previewFile = vi.fn()
    const search = vi.fn().mockResolvedValue({ onResultsUpdate: (callback: (result: { totalResults?: number }) => void) => callback({ totalResults: 1 }) })
    let readyCallback: ((event: { type?: string }) => void) | undefined
    const viewer = {
      getAPIs: vi.fn().mockResolvedValue({
        gotoLocation,
        getCurrentPage: vi.fn().mockResolvedValue(5),
        search,
      }),
      getAnnotationManager: vi.fn().mockResolvedValue({ addAnnotations, getAnnotations, selectAnnotation }),
    }
    class MockAdobeView {
      static Enum = { CallbackType: { EVENT_LISTENER: "event" }, Events: { PDF_VIEWER_READY: "PDF_VIEWER_READY" } }
      registerCallback(_type: unknown, callback: (event: { type?: string }) => void) { readyCallback = callback }
      async previewFile(file: unknown, config: unknown) {
        previewFile(file, config)
        queueMicrotask(() => readyCallback?.({ type: "PDF_VIEWER_READY" }))
        return viewer
      }
    }
    Object.defineProperty(window, "AdobeDC", { configurable: true, writable: true, value: { View: MockAdobeView } })
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      const body = url === "/api/config"
        ? { adobe_pdf_embed_client_id: "localhost-client" }
        : {
            source_id: "source-pdf",
            relation: "Primary source",
            preview_url: "/api/documents/document-pdf/preview",
            spreadsheet_url: "/api/documents/document-pdf/spreadsheet",
            location: {
              document_id: "document-pdf", original_document_type: "pdf", viewer_type: "pdf", page_number: 5,
              exact_quote: "Please provide the stud bolt weld.", pdf_bounding_boxes: [[100, 200, 300, 240], [100, 175, 250, 198]],
            },
            document: { document_id: "document-pdf", filename: "Review.pdf", original_document_type: "pdf", viewer_type: "pdf", size: 1200 },
          }
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }))
    }))

    render(<SourceViewer sourceId="source-pdf" open onOpenChange={vi.fn()} />)
    expect(await screen.findByText("Review.pdf")).toBeInTheDocument()
    expect(await screen.findByText(/Opened page 5 at normal fit-to-width size and highlighted/)).toBeInTheDocument()
    expect(addAnnotations).toHaveBeenCalledTimes(1)
    expect(selectAnnotation).toHaveBeenCalledTimes(1)
    expect(search).toHaveBeenCalled()
    expect(previewFile).toHaveBeenCalledWith(
      expect.objectContaining({ metaData: expect.not.objectContaining({ hasReadOnlyAccess: true }) }),
      expect.objectContaining({ annotationUIConfig: expect.objectContaining({ showToolbar: false, showToolsOnTextSelection: false }) }),
    )
    await waitFor(() => expect(gotoLocation).toHaveBeenCalledWith(5, 200, 207.5))
  })
})

describe("PDF source targeting", () => {
  it("keeps continuation-page boxes instead of dropping them", () => {
    const grouped = pdfBoxesByPage({
      document_id: "document-pdf", original_document_type: "pdf", viewer_type: "pdf", page_number: 3,
      pdf_bounding_boxes: [[10, 20, 30, 40]],
      pdf_bounding_boxes_by_page: { "3": [[10, 20, 30, 40]], "4": [[50, 60, 70, 80]] },
    })
    expect([...grouped.keys()]).toEqual([3, 4])
    expect(grouped.get(4)).toEqual([[50, 60, 70, 80]])
  })

  it("centers the viewer over all stored evidence lines", () => {
    expect(pdfFocusForBoxes([[100, 200, 300, 240], [100, 175, 250, 198]])).toEqual({ x: 200, y: 207.5 })
  })

  it("zooms only the three approved Menlo Park PC2 files", () => {
    expect(pdfZoomForFilename("PC2- 2311 Warner Range Ave Plans-Reviewed-Corrections-Required-to civil.pdf")).toBe(175)
    expect(pdfZoomForFilename("PC2- 2311 Warner Range Ave Plans-Reviewed-Corrections-Required.pdf")).toBe(175)
    expect(pdfZoomForFilename("PC2- 2311 Warner Range Ave Plans-Reviewed-Corrections-Required-structure.pdf")).toBe(175)
    expect(pdfZoomForFilename("PC2- 2311 Warner Range Ave Structural Calculation.pdf")).toBeNull()
    expect(pdfZoomForFilename("Review.pdf")).toBeNull()
  })
})

describe("spreadsheet source targeting", () => {
  it("requests bounded same-row context while keeping the cited cell range separate", () => {
    expect(spreadsheetContextRange("E3")).toBe("A3:Z3")
    expect(spreadsheetContextRange("E3:F4")).toBe("A3:Z4")
    expect(spreadsheetContextRange(undefined, 12)).toBe("A12:Z12")
  })
})
