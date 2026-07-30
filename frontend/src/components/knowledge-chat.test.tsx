import { afterEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { KnowledgeChat } from "@/components/knowledge-chat"

afterEach(() => vi.restoreAllMocks())

describe("KnowledgeChat", () => {
  it("submits a suggested question and exposes citations and supporting records", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      answer: "Tree protection measures were documented on the landscape plans.",
      intent: "historical_response_summary",
      conversation_id: "conversation-1",
      result_set_id: "results-1",
      answer_sections: { historical_pattern: "Historical pattern: Tree protection notes were added before resubmittal." },
      metrics: { parent_comments: 2, projects: 1, review_rounds: 1, confirmed_responses: 2, missing_responses: 0 },
      citations: [{ source_id: "source-1", label: "Response source · Response Letter.pdf · page 3" }],
      actions: [{ type: "show_results", label: "Show 2 supporting records", result_set_id: "results-1" }],
      query_plan: { evidence_scope: "verified" },
    }), { status: 200, headers: { "Content-Type": "application/json" } })))
    const openSource = vi.fn()
    const openResults = vi.fn()
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={openSource} onOpenResults={openResults} />)

    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    expect(await screen.findByText("Tree protection notes were added before resubmittal.")).toBeInTheDocument()
    fireEvent.click(screen.getByText("Used 1 sources"))
    fireEvent.click(screen.getByText(/Response · Response Letter.pdf/))
    expect(openSource).toHaveBeenCalledWith("source-1")
    fireEvent.click(screen.getByRole("button", { name: /Show 2 supporting records/ }))
    expect(openResults).toHaveBeenCalledWith("results-1")
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/knowledge-chat", expect.objectContaining({ method: "POST" })))
  })
})
