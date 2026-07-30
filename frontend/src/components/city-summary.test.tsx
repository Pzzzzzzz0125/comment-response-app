import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"
import { CitySummary } from "@/components/city-summary"
import { TooltipProvider } from "@/components/ui/tooltip"

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
})
