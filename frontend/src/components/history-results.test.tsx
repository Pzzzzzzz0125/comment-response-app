import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { HistoricalResults, type Filters } from "@/components/history-results"
import { commentFixture } from "@/test/fixtures"

const filters: Filters = { property_project: "", discipline: "", review_round: "", match_status: "", category: "", human_review_status: "" }

describe("HistoricalResults", () => {
  it("selects a result and centers its comment-response detail", async () => {
    const onActive = vi.fn()
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView")
    render(<HistoricalResults city="San Jose" comments={[commentFixture]} loading={false} activeId={null} onActive={onActive} filters={filters} onFilters={vi.fn()} relevance={new Map([["comment-1", "direct"]])} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    fireEvent.click(screen.getByText("Protect the existing street tree during construction."))
    expect(onActive).toHaveBeenCalledWith("comment-1")
    expect(screen.getByText("Has response")).toHaveClass("bg-green-50")
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ block: "center" })))
  })

  it("opens a source citation from the selected detail", () => {
    const openSource = vi.fn()
    render(<HistoricalResults city="San Jose" comments={[commentFixture]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={openSource} onCategoriesChanged={vi.fn()} />)
    fireEvent.click(screen.getByRole("button", { name: /Primary source: City Comments.pdf/ }))
    expect(openSource).toHaveBeenCalledWith("source-comment")
  })

  it("uses an amber status label when no response is stored", () => {
    const unmatched = { ...commentFixture, comment_id: "comment-2", response: null, match_status: "unmatched" }
    render(<HistoricalResults city="San Jose" comments={[unmatched]} loading={false} activeId="comment-2" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    expect(screen.getByText("No response")).toHaveClass("bg-amber-50")
  })

  it("keeps response and record details in normal document order", () => {
    render(<HistoricalResults city="San Jose" comments={[commentFixture]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    const detail = Array.from(document.querySelectorAll("#comment-detail-panel")).at(-1)
    const header = detail?.querySelector("header")
    const response = detail?.querySelector('[aria-label="Historical company response"]')
    const recordDetails = Array.from(detail?.querySelectorAll("button") || []).find((button) => button.textContent?.includes("Record details"))
    expect(header).not.toHaveClass("sticky")
    expect(response).toBeTruthy()
    expect(recordDetails).toBeTruthy()
    expect(response!.compareDocumentPosition(recordDetails!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it("shows repeated review activity as one chronological issue history", () => {
    const openSource = vi.fn()
    const threaded = {
      ...commentFixture,
      issue_thread: {
        thread_id: "thread-1",
        grouping_status: "explicit",
        grouping_method: "same_spreadsheet_row",
        status: "Unresolved",
        event_count: 4,
        events: [
          { event_id: "event-comment", event_type: "government_comment" as const, actor_role: "government" as const, actor: "Eric Morgan", occurred_at: "", occurred_at_label: "", label: "Government comment", text: "Label tree circumference.", review_round: "1", source: commentFixture.sources[0] },
          { event_id: "event-response-1", event_type: "applicant_response" as const, actor_role: "company" as const, actor: "Weiran Jia", occurred_at: "2026-05-25T15:05", occurred_at_label: "5/25/26 3:05 PM", label: "Applicant response", text: "Tree labels were added.", review_round: "1", source: { source_id: "source-history", relation: "Prior applicant response", filename: "Review.xlsx" } },
          { event_id: "event-reviewer", event_type: "reviewer_follow_up" as const, actor_role: "government" as const, actor: "Eric Morgan", occurred_at: "2026-06-30T15:02", occurred_at_label: "6/30/26 3:02 PM", label: "Reviewer follow-up", text: "Not addressed. Use circumference, not DBH.", review_round: "1", source: { source_id: "source-reviewer", relation: "Reviewer follow-up", filename: "Review.xlsx" } },
          { event_id: "event-current", event_type: "current_applicant_response" as const, actor_role: "company" as const, actor: "", occurred_at: "", occurred_at_label: "", label: "Current applicant response", text: "Circumference is now shown.", review_round: "1", source: commentFixture.response!.sources[0] },
        ],
      },
    }
    render(<HistoricalResults city="San Jose" comments={[threaded]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={openSource} onCategoriesChanged={vi.fn()} />)
    const history = screen.getByRole("region", { name: "Issue history" })
    expect(history).toHaveTextContent("4 events")
    expect(history).toHaveTextContent("Applicant response")
    expect(history).toHaveTextContent("Reviewer follow-up")
    expect(history).toHaveTextContent("Current applicant response")
    fireEvent.click(screen.getByRole("button", { name: /Reviewer follow-up: Review.xlsx/ }))
    expect(openSource).toHaveBeenCalledWith("source-reviewer")
  })
})
