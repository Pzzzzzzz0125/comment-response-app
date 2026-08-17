import { afterEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { ImportDialog } from "@/components/import-dialog"

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("ImportDialog", () => {
  it("uploads a chosen folder and starts the whole pipeline with one button", async () => {
    const ready = {
      enabled: true,
      dependencies: { ghostscript: true, tesseract: true, libreoffice: false, gemini_key: true },
      active_job: null,
      jobs: [],
    }
    const completed = {
      ...ready,
      jobs: [{ job_id: "ing-1", mode: "ingest", site_label: "100 Main Street", status: "completed", stage: "complete", message: "Completed successfully" }],
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === "/api/ingestion" && !init?.method) return new Response(JSON.stringify(ready), { status: 200 })
      if (path === "/api/ingestion/uploads") return new Response(JSON.stringify({
        upload_id: "upl-1234567890abcdef",
        project_name: "100 Main Street",
        file_count: 1,
        size_bytes: 9,
        files: [{ file_id: "file-00001", relative_path: "review.pdf", size: 9 }],
      }), { status: 201 })
      if (path.includes("/files/file-00001")) return new Response(JSON.stringify({ uploaded_files: 1, total_files: 1 }), { status: 200 })
      if (path.endsWith("/complete")) return new Response(JSON.stringify(completed), { status: 202 })
      return new Response(JSON.stringify({ error: "unexpected request" }), { status: 404 })
    })
    vi.stubGlobal("fetch", fetchMock)

    render(<ImportDialog open onOpenChange={vi.fn()} />)

    await screen.findByText("Choose a complete project folder")
    const file = new File(["%PDF-new"], "review.pdf", { type: "application/pdf" })
    Object.defineProperty(file, "webkitRelativePath", { value: "100 Main Street/review.pdf" })
    fireEvent.change(screen.getByLabelText("Choose project folder"), { target: { files: [file] } })

    expect(await screen.findByText("100 Main Street")).toBeInTheDocument()
    expect(screen.getByText("1 supported file")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Upload and ingest" }))

    await waitFor(() => expect(screen.getByText("Dataset and derived indexes are ready.")).toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledWith("/api/ingestion/uploads", expect.objectContaining({ method: "POST" }))
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/ingestion/uploads/upl-1234567890abcdef/files/file-00001",
      expect.objectContaining({ method: "PUT", body: file }),
    )
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/ingestion/uploads/upl-1234567890abcdef/complete",
      expect.objectContaining({ method: "POST" }),
    )
  })
})
