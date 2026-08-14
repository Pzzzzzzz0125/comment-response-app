import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { CitySummary } from "@/components/city-summary"
import { TooltipProvider } from "@/components/ui/tooltip"

afterEach(cleanup)

describe("CitySummary", () => {
  it("shows city counts and deduplicated common topics", () => {
    const onOpenTopic = vi.fn()
    const topic = { label: "Provide tree protection notes.", occurrences: 4, projects: 2, rounds: 3, comment_ids: ["1", "2", "3", "4"] }
    render(<TooltipProvider><CitySummary city="San Jose" analysis={{
      summary: "San Jose historical review summary.",
      total_comments: 175,
      unique_comments: 162,
      topic_count: 140,
      technical: 131,
      nontechnical: 44,
      projects: 5,
      review_cycles: 8,
      common_topics: [topic],
      method_note: "Deterministic topic grouping.",
    }} onOpenTopic={onOpenTopic} /></TooltipProvider>)
    expect(screen.getByText("San Jose permit history")).toBeInTheDocument()
    expect(screen.getByText("175")).toBeInTheDocument()
    expect(screen.getByText("Provide tree protection notes.")).toBeInTheDocument()
    expect(screen.getByText("4 comments")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "View 4 comments for Provide tree protection notes." }))
    expect(onOpenTopic).toHaveBeenCalledWith(topic)
  })

  it("keeps recurring issue timelines separate from common topics and opens the issue", () => {
    const onOpenRecurringIssue = vi.fn()
    const issue = {
      issue_thread_id: "MP-TL-001",
      project_id: "project-1",
      site_id: "site-1",
      site_name: "2311 Warner Range Ave",
      city: "Menlo Park",
      title: "ADU/main-dwelling fire-rated wall assembly",
      common_topic: "Fire Separation",
      discipline: "Building",
      status: "open" as const,
      status_reason: "Later review remains.",
      persistence_explanation: "The earlier response did not identify the revised detail clearly.",
      first_round: 1,
      latest_round: 3,
      round_count: 3,
      event_count: 3,
      source_occurrence_count: 4,
      source_document_count: 2,
      company_response_count: 2,
      comment_ids: ["1"],
      events: [],
    }
    render(<TooltipProvider><CitySummary city="Menlo Park" analysis={{
      summary: "Menlo Park historical review summary.",
      total_comments: 639,
      unique_comments: 400,
      topic_count: 80,
      technical: 500,
      nontechnical: 139,
      projects: 1,
      review_cycles: 3,
      common_topics: [],
      recurring_issues: [issue],
      recurring_issue_stats: { total: 1, open: 1, resolved: 0, unknown: 0, average_rounds_to_resolution: null, longest_running_rounds: 3, longest_running_issue_id: issue.issue_thread_id, longest_running_title: issue.title },
      method_note: "Common topics are broad aspects.",
    }} onOpenRecurringIssue={onOpenRecurringIssue} /></TooltipProvider>)
    expect(screen.getAllByText("Recurring issues").length).toBeGreaterThan(0)
    expect(screen.getByText("ADU/main-dwelling fire-rated wall assembly")).toBeInTheDocument()
    expect(screen.getByText("PC1 → PC3")).toBeInTheDocument()
    expect(screen.getByText(/The earlier response did not identify/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "View timeline for ADU/main-dwelling fire-rated wall assembly" }))
    expect(onOpenRecurringIssue).toHaveBeenCalledWith(issue)
  })

  it("sorts recurring issues by canonical history events without showing an ambiguous entry count", () => {
    const issue = (id: string, title: string, rounds: number, events: number, responses: number) => ({
      issue_thread_id: id, project_id: "project-1", site_id: "site-1", site_name: "123 Main", city: "San Jose",
      title, common_topic: "Building", discipline: "Building", status: "unknown", status_reason: "No final resolution.",
      first_round: 1, latest_round: rounds, round_count: rounds, event_count: events, history_event_count: events, comment_event_count: events - responses, response_event_count: responses, source_occurrence_count: events,
      source_document_count: 2, company_response_count: responses, comment_ids: [id], events: [],
    })
    const moreRounds = issue("long", "More rounds but fewer entries", 4, 4, 0)
    const moreEntries = issue("large", "More history events", 2, 7, 3)
    render(<TooltipProvider><CitySummary city="San Jose" analysis={{
      summary: "Summary.", total_comments: 10, unique_comments: 8, topic_count: 3, technical: 8, nontechnical: 2,
      projects: 1, review_cycles: 4, common_topics: [], recurring_issues: [moreRounds, moreEntries],
      recurring_issue_stats: { total: 2, open: 0, resolved: 0, unknown: 2, average_rounds_to_resolution: null, longest_running_rounds: 4, longest_running_issue_id: "long", longest_running_title: moreRounds.title },
      method_note: "Method.",
    }} /></TooltipProvider>)
    const cards = screen.getAllByRole("button", { name: /View timeline for/ })
    expect(cards[0]).toHaveAccessibleName("View timeline for More history events")
    expect(cards[0]).toHaveTextContent("3 responses")
    expect(cards[0]).not.toHaveTextContent("total entries")
    expect(cards[1]).toHaveAccessibleName("View timeline for More rounds but fewer entries")
  })
})
