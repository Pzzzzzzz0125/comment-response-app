import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { HistoricalResults, type Filters } from "@/components/history-results"
import { mergeIssueEvents } from "@/components/comment-detail"
import { commentFixture } from "@/test/fixtures"

const filters: Filters = { property_project: "", discipline: "", review_round: "", match_status: "", category: "", human_review_status: "", timeline: "" }

afterEach(cleanup)

describe("HistoricalResults", () => {
  it("merges identical same-date events and preserves every source link", () => {
    const first = { event_id: "event-1-file-a", event_type: "government_comment" as const, actor_role: "government" as const, actor: "Reviewer", occurred_at: "", occurred_at_label: "6/17/25 2:41 PM", time_label: "6/17/25", label: "Government comment", text: "Show the structural calculation.", review_round: "1", source: { source_id: "source-a", relation: "Primary source", filename: "file-a.xlsx" } }
    const second = { ...first, event_id: "event-1-file-b", source: { source_id: "source-b", relation: "Primary source", filename: "file-b.xlsx" } }
    const later = { ...first, event_id: "event-2-file-b", occurred_at_label: "6/18/25 9:00 AM", time_label: "6/18/25", text: "Show the response calculation.", source: { source_id: "source-b-2", relation: "Primary source", filename: "file-b.xlsx" } }
    const merged = mergeIssueEvents([first, second, later])
    expect(merged).toHaveLength(2)
    expect(merged[0].sources?.map((source) => source.source_id)).toEqual(["source-a", "source-b"])
    expect(merged[0].merged_event_ids).toEqual(["event-1-file-a", "event-1-file-b"])
  })

  it("merges a canonical undated event repeated by thread members", () => {
    const first = { event_id: "event-pc1", event_type: "government_comment" as const, actor_role: "government" as const, actor: "Reviewer", occurred_at: "", occurred_at_label: "", time_label: "Exact time not recorded · Round 1", label: "Government comment · PC1", text: "Provide the surveyor signature.", review_round: "1", source: { source_id: "source-file-1", relation: "Primary source", filename: "PC1.pdf" }, sources: [{ source_id: "source-file-1", relation: "Primary source", filename: "PC1.pdf" }, { source_id: "source-file-2", relation: "Also appears in", filename: "PC2.pdf" }] }
    const second = { ...first, source: first.sources[0], sources: first.sources }
    const merged = mergeIssueEvents([first, second])
    expect(merged).toHaveLength(1)
    expect(merged[0].sources?.map((source) => source.source_id)).toEqual(["source-file-1", "source-file-2"])
  })

  it("groups undated records by their PC key while keeping dated records separate", () => {
    const first = { event_id: "pc1-a", event_type: "government_comment" as const, actor_role: "government" as const, actor: "", occurred_at: "", occurred_at_label: "", time_label: "", record_label: "PC1", label: "Government comment · PC1", text: "Provide the signature.", review_round: "1", source: { source_id: "pc1-source-a", relation: "Primary source", filename: "PC1-a.pdf" } }
    const second = { ...first, event_id: "pc1-b", source: { source_id: "pc1-source-b", relation: "Also appears in", filename: "PC1-b.pdf" } }
    const pc2 = { ...first, event_id: "pc2-a", record_label: "PC2", label: "Reviewer follow-up · PC2", review_round: "2", text: "Provide the signature.", source: { source_id: "pc2-source", relation: "Primary source", filename: "PC2.pdf" } }
    const merged = mergeIssueEvents([first, second, pc2])
    expect(merged).toHaveLength(2)
    expect(merged[0].record_label).toBe("PC1")
    expect(merged[0].sources?.map((source) => source.source_id)).toEqual(["pc1-source-a", "pc1-source-b"])
    expect(merged[1].record_label).toBe("PC2")
  })

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
    fireEvent.click(screen.getByRole("button", { name: "Open original source" }))
    expect(openSource).toHaveBeenCalledWith("source-response")
  })

  it("uses an amber status label when no response is stored", () => {
    const unmatched = { ...commentFixture, comment_id: "comment-2", response: null, match_status: "unmatched" }
    render(<HistoricalResults city="San Jose" comments={[unmatched]} loading={false} activeId="comment-2" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    expect(screen.getByText("No response")).toHaveClass("bg-amber-50")
  })

  it("keeps a single-round comment and response out of issue history", () => {
    const singlePair = {
      ...commentFixture,
      issue_thread: {
        thread_id: "single-round-pair",
        grouping_status: "same_row",
        grouping_method: "same_pdf_form_row",
        status: "Responded",
        event_count: 2,
        events: [
          { event_id: "single-comment", event_type: "government_comment" as const, actor_role: "government" as const, actor: "Reviewer", occurred_at: "", occurred_at_label: "", effective_round: "1", review_round: "1", label: "Government comment", text: commentFixture.display_text },
          { event_id: "single-response", event_type: "current_applicant_response" as const, actor_role: "company" as const, actor: "", occurred_at: "", occurred_at_label: "", effective_round: "1", review_round: "1", label: "Current applicant response", text: commentFixture.response!.display_text },
        ],
      },
    }
    render(<HistoricalResults city="San Jose" comments={[singlePair]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    expect(screen.queryByText("Issue history")).not.toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Historical company response" })).toBeInTheDocument()
  })

  it("collapses records from the same issue thread into one history item", () => {
    const laterRound = {
      ...commentFixture,
      comment_id: "comment-1-later",
      review_round: "2",
      response: null,
      issue_thread: {
        thread_id: "thread-shared",
        grouping_status: "deterministic_exact",
        grouping_method: "exact_site_discipline_comment",
        status: "Unresolved",
        event_count: 1,
        events: [{
          event_id: "event-later",
          event_type: "government_comment" as const,
          actor_role: "government" as const,
          actor: "Reviewer B",
          occurred_at: "",
          occurred_at_label: "",
          time_label: "Document date · 09/24/2025",
          time_basis: "document_date",
          time_precision: "document" as const,
          source_date: "09/24/2025",
          submission: "4th submission",
          label: "Government comment",
          text: "The same issue remains open.",
          review_round: "2",
        }],
      },
    }
    const firstRound = {
      ...commentFixture,
      issue_thread: {
        thread_id: "thread-shared",
        grouping_status: "deterministic_exact",
        grouping_method: "exact_site_discipline_comment",
        status: "Unresolved",
        event_count: 1,
        events: [{
          event_id: "event-first",
          event_type: "government_comment" as const,
          actor_role: "government" as const,
          actor: "Reviewer A",
          occurred_at: "",
          occurred_at_label: "",
          time_label: "Document date · 08/11/2025",
          time_basis: "document_date",
          time_precision: "document" as const,
          source_date: "08/11/2025",
          submission: "3rd submission",
          label: "Government comment",
          text: "Protect the existing street tree during construction.",
          review_round: "1",
        }],
      },
    }
    render(<HistoricalResults city="San Jose" comments={[firstRound, laterRound]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    expect(screen.getByText("Issues")).toBeInTheDocument()
    expect(screen.getByText("Issue history · 2 records")).toBeInTheDocument()
    expect(screen.getByText("1 issue · 2 records")).toBeInTheDocument()
  })

  it("collapses records from different source threads into one recurring-issue link", () => {
    const laterRound = {
      ...commentFixture,
      comment_id: "comment-recurring-2",
      review_round: "2",
      original_text: "The same design issue remains open in PC2.",
      display_text: "The same design issue remains open in PC2.",
      issue_thread: { thread_id: "source-thread-2", grouping_status: "deterministic_exact", grouping_method: "source_document", status: "Unresolved", event_count: 1, events: [{ event_id: "event-recurring-2", event_type: "government_comment" as const, actor_role: "government" as const, actor: "Reviewer", occurred_at: "", occurred_at_label: "", time_label: "Round 2", label: "Government comment", text: "The same design issue remains open in PC2.", review_round: "2" }] },
    }
    const firstRound = {
      ...commentFixture,
      issue_thread: { thread_id: "source-thread-1", grouping_status: "deterministic_exact", grouping_method: "source_document", status: "Unresolved", event_count: 1, events: [{ event_id: "event-recurring-1", event_type: "government_comment" as const, actor_role: "government" as const, actor: "Reviewer", occurred_at: "", occurred_at_label: "", time_label: "Round 1", label: "Government comment", text: commentFixture.display_text, review_round: "1" }] },
    }
    render(<HistoricalResults city="San Jose" comments={[firstRound, laterRound]} recurringIssues={[{ issue_thread_id: "timeline-1", project_id: "project-1", site_id: "site-1", site_name: "123 Main Street", city: "San Jose", title: "Street-tree protection", common_topic: "Tree protection", discipline: "Planning", status: "open", status_reason: "No later resolution", first_round: 1, latest_round: 2, round_count: 2, event_count: 2, source_occurrence_count: 2, source_document_count: 2, company_response_count: 1, comment_ids: ["comment-1", "comment-recurring-2"], events: [] }]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    expect(screen.getByText("1 issue · 2 records")).toBeInTheDocument()
    expect(screen.getByText("Review history · 2 rounds")).toBeInTheDocument()
    expect(screen.getByText("The same design issue remains open in PC2.")).toBeInTheDocument()
  })

  it("preserves library order while showing combined comment-response counts", () => {
    const small = { ...commentFixture, comment_id: "small-issue", display_text: "Small standalone issue." }
    const largeFirst = { ...commentFixture, comment_id: "large-1", display_text: "Large recurring issue." }
    const largeSecond = { ...commentFixture, comment_id: "large-2", display_text: "Large recurring issue remains open.", review_round: "2", response: null }
    const recurring = {
      issue_thread_id: "large-timeline", project_id: "project-1", site_id: "site-1", site_name: "123 Main Street", city: "San Jose",
      title: "Large recurring issue", common_topic: "Building", discipline: "Building", status: "open", status_reason: "Still open",
      first_round: 1, latest_round: 2, round_count: 2, event_count: 4, history_event_count: 5, comment_event_count: 2, response_event_count: 3, source_occurrence_count: 4, source_document_count: 3,
      company_response_count: 3, comment_ids: ["large-1", "large-2"], events: [],
    }
    render(<HistoricalResults city="San Jose" comments={[small, largeFirst, largeSecond]} recurringIssues={[recurring]} loading={false} activeId={null} onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    const cards = Array.from(document.querySelectorAll(".result-card"))
    expect(cards).toHaveLength(2)
    expect(cards[0]).toHaveTextContent("Small standalone issue")
    expect(cards[1]).toHaveTextContent("Large recurring issue")
    expect(cards[1]).toHaveTextContent("2 comments · 3 responses")
  })

  it("filters the library to comments with a timeline", () => {
    const standalone = { ...commentFixture, comment_id: "standalone", display_text: "Standalone issue." }
    const timeline = {
      ...commentFixture,
      comment_id: "timeline-comment",
      display_text: "Timeline issue.",
      issue_thread: {
        thread_id: "timeline-thread", grouping_status: "explicit", grouping_method: "same_issue", status: "open", event_count: 2,
        events: [
          { event_id: "timeline-pc1", event_type: "government_comment" as const, actor_role: "government" as const, actor: "", occurred_at: "", occurred_at_label: "", label: "Government comment", text: "Timeline issue.", review_round: "1", effective_round: "1" },
          { event_id: "timeline-pc2", event_type: "reviewer_follow_up" as const, actor_role: "government" as const, actor: "", occurred_at: "", occurred_at_label: "", label: "Reviewer follow-up", text: "Timeline issue remains open.", review_round: "2", effective_round: "2" },
        ],
      },
    }
    render(<HistoricalResults city="San Jose" comments={[standalone, timeline]} loading={false} activeId={null} onActive={vi.fn()} filters={{ ...filters, timeline: "with_timeline" }} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    expect(document.querySelectorAll(".result-card")).toHaveLength(1)
    expect(screen.getByText("Timeline issue.")).toBeInTheDocument()
    expect(screen.queryByText("Standalone issue.")).not.toBeInTheDocument()
    expect(screen.getByText("1 issue · 1 record")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /Filters/ }))
    expect(screen.getByText("History")).toBeInTheDocument()
  })

  it("clears stale list search when a different recurring issue is opened", async () => {
    const view = render(<HistoricalResults city="San Jose" comments={[commentFixture]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} resultLabel="First timeline" onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    const input = screen.getByRole("textbox", { name: "Search historical comments" })
    fireEvent.change(input, { target: { value: "old search" } })
    expect(input).toHaveValue("old search")
    view.rerender(<HistoricalResults city="San Jose" comments={[commentFixture]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} resultLabel="Second timeline" onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    await waitFor(() => expect(input).toHaveValue(""))
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
          { event_id: "event-comment", event_type: "government_comment" as const, actor_role: "government" as const, actor: "Eric Morgan", occurred_at: "", occurred_at_label: "4/3/26 10:17 AM", time_label: "4/3/26", time_basis: "reviewer_cell", time_precision: "exact_date" as const, label: "Government comment", text: "Label tree circumference.", review_round: "1", source: commentFixture.sources[0] },
          { event_id: "event-response-1", event_type: "applicant_response" as const, actor_role: "company" as const, actor: "Weiran Jia", occurred_at: "2026-05-25T15:05", occurred_at_label: "5/25/26 3:05 PM", time_label: "5/25/26", time_basis: "discussion_header", time_precision: "exact_date" as const, label: "Applicant response", text: "Tree labels were added.", review_round: "1", source: { source_id: "source-history", relation: "Prior applicant response", filename: "Review.xlsx" } },
          { event_id: "event-reviewer", event_type: "reviewer_follow_up" as const, actor_role: "government" as const, actor: "Eric Morgan", occurred_at: "2026-06-30T15:02", occurred_at_label: "6/30/26 3:02 PM", time_label: "6/30/26", time_basis: "discussion_header", time_precision: "exact_date" as const, label: "Reviewer follow-up", text: "Not addressed. Use circumference, not DBH.", review_round: "1", source: { source_id: "source-reviewer", relation: "Reviewer follow-up", filename: "Review.xlsx" } },
          { event_id: "event-current", event_type: "current_applicant_response" as const, actor_role: "company" as const, actor: "", occurred_at: "", occurred_at_label: "", time_label: "By workbook export · 07/01/2026", time_basis: "workbook_export", time_precision: "available_by" as const, label: "Current applicant response", text: "Circumference is now shown.", review_round: "1", source: commentFixture.response!.sources[0] },
        ],
      },
    }
    render(<HistoricalResults city="San Jose" comments={[threaded]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={openSource} onCategoriesChanged={vi.fn()} />)
    const history = screen.getByRole("region", { name: "Issue history" })
    expect(history).toHaveTextContent("4 events")
    expect(history).toHaveTextContent("Applicant response")
    expect(history).toHaveTextContent("Reviewer follow-up")
    expect(history).toHaveTextContent("Current applicant response")
    expect(history).toHaveTextContent("4/3/26")
    expect(history).toHaveTextContent("By workbook export · 07/01/2026")
    fireEvent.click(screen.getByRole("button", { name: /Reviewer follow-up: Review.xlsx/ }))
    expect(openSource).toHaveBeenCalledWith("source-reviewer")
  })

  it("repairs an incomplete timeline so the major government comment is first", () => {
    const incomplete = {
      ...commentFixture,
      issue_thread: {
        thread_id: "incomplete-thread", grouping_status: "explicit", grouping_method: "same_issue", status: "open", event_count: 2,
        events: [
          { event_id: "response-first", event_type: "applicant_response" as const, actor_role: "company" as const, actor: "Applicant", occurred_at: "2025-07-01T10:00:00", occurred_at_label: "7/1/25 10:00 AM", label: "Applicant response", text: "The plans were revised.", review_round: "1", effective_round: "1" },
          { event_id: "follow-up", event_type: "reviewer_follow_up" as const, actor_role: "government" as const, actor: "Reviewer", occurred_at: "2025-07-02T10:00:00", occurred_at_label: "7/2/25 10:00 AM", label: "Reviewer follow-up", text: "The revision is incomplete.", review_round: "2", effective_round: "2" },
        ],
      },
    }
    render(<HistoricalResults city="San Jose" comments={[incomplete]} loading={false} activeId="comment-1" onActive={vi.fn()} filters={filters} onFilters={vi.fn()} relevance={new Map()} explanations={new Map()} onClearResultSet={vi.fn()} onOpenSource={vi.fn()} onCategoriesChanged={vi.fn()} />)
    const history = screen.getByRole("region", { name: "Issue history" })
    const events = Array.from(history.querySelectorAll("article"))
    expect(events).toHaveLength(3)
    expect(events[0]).toHaveTextContent("Government comment")
    expect(events[0]).toHaveTextContent(commentFixture.display_text)
    expect(events[1]).toHaveTextContent("Applicant response")
    expect(events[2]).toHaveTextContent("Reviewer follow-up")
  })
})
