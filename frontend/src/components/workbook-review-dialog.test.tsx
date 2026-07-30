import { afterEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { WorkbookReviewDialog } from "@/components/workbook-review-dialog"
import { commentFixture } from "@/test/fixtures"

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("WorkbookReviewDialog", () => {
  it("shows cell evidence, opens the source, and requires confirmation", async () => {
    const openSource = vi.fn()
    const onChanged = vi.fn()
    const fetchMock = vi.fn().mockImplementation((_path: string, options?: RequestInit) => {
      if (options?.method === "POST") {
        return Promise.resolve(new Response(JSON.stringify({
          source_document: "comments&response/site/review.xlsx",
          decision: "confirmed",
          updated: 1,
        }), { status: 200, headers: { "Content-Type": "application/json" } }))
      }
      return Promise.resolve(new Response(JSON.stringify({
        items: [{
          source_document: "comments&response/site/review.xlsx",
          filename: "review.xlsx",
          status: "pending",
          note: "",
          city: "San Jose",
          property_project: "100 Main St",
          review_rounds: ["1"],
          comment_count: 1,
          response_count: 1,
          comment_columns: ["C"],
          response_columns: ["E"],
          source: commentFixture.sources[0],
          rows: [{
            ...commentFixture,
            issue_thread: {
              thread_id: "thread-1",
              grouping_status: "explicit",
              grouping_method: "same_spreadsheet_row",
              status: "Unresolved",
              event_count: 3,
              events: [
                { event_id: "comment", event_type: "government_comment", actor_role: "government", actor: "", occurred_at: "", occurred_at_label: "", label: "Government comment", text: commentFixture.display_text, review_round: "1", source: commentFixture.sources[0] },
                { event_id: "reviewer", event_type: "reviewer_follow_up", actor_role: "government", actor: "City reviewer", occurred_at: "2026-06-30T15:02", occurred_at_label: "6/30/26 3:02 PM", label: "Reviewer follow-up", text: "Not addressed.", review_round: "1", source: { source_id: "source-discussion", relation: "Reviewer follow-up", filename: "review.xlsx" } },
                { event_id: "response", event_type: "current_applicant_response", actor_role: "company", actor: "", occurred_at: "", occurred_at_label: "", label: "Current applicant response", text: commentFixture.response!.display_text, review_round: "1", source: commentFixture.response!.sources[0] },
              ],
            },
          }],
          structural_checks: {
            can_confirm: true,
            reason: "",
            expected_comments: 1,
            unresolved_signals: 0,
          },
        }],
        counts: { total: 1, pending: 1, confirmed: 0, needs_followup: 0 },
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
    })
    vi.stubGlobal("fetch", fetchMock)

    render(<WorkbookReviewDialog open onOpenChange={vi.fn()} cities={["San Jose"]} onOpenSource={openSource} onChanged={onChanged} />)

    expect(await screen.findByText("review.xlsx")).toBeInTheDocument()
    expect(screen.getByText("Comment column C")).toBeInTheDocument()
    expect(screen.getByText("Response column E")).toBeInTheDocument()
    expect(screen.getByText("One issue with 3 review events")).toBeInTheDocument()
    expect(screen.getByText("Not addressed.")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /Open cited cell/ }))
    expect(openSource).toHaveBeenCalledWith("source-comment")

    fireEvent.click(screen.getByRole("button", { name: "Confirm entire workbook" }))
    expect(screen.getByText("Confirm all 1 rows?")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Yes, confirm workbook" }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/workbook-reviews",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"decision":"confirmed"'),
      }),
    ))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })
})
