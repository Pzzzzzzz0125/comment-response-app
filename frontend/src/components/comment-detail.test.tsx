import { describe, expect, it } from "vitest"
import { mergeIssueEvents } from "@/components/comment-detail"
import type { IssueEvent } from "@/types"

describe("comment detail source links", () => {
  it("keeps one link per source document even when locator source ids differ", () => {
    const event = {
      event_id: "E-1",
      event_type: "government_comment",
      actor_role: "government",
      actor: "Reviewer",
      occurred_at: "",
      occurred_at_label: "",
      label: "Government comment",
      text: "Provide the stud bolt weld.",
      review_round: "1",
      sources: [
        { source_id: "S-1", relation: "Primary source", filename: "review.pdf", location: { document_id: "D-1", original_document_type: "pdf", viewer_type: "pdf" } },
        { source_id: "S-2", relation: "Also appears in", filename: "review.pdf", location: { document_id: "D-1", original_document_type: "pdf", viewer_type: "pdf" } },
      ],
    } as IssueEvent
    const [merged] = mergeIssueEvents([event])
    expect(merged.sources).toHaveLength(1)
    expect(merged.sources?.[0].source_id).toBe("S-1")
  })

  it("merges same-date near-duplicate requirements across copied round labels and unions metadata", () => {
    const base = {
      event_type: "government_comment",
      actor_role: "government",
      actor: "Reviewer",
      occurred_at: "",
      occurred_at_label: "",
      time_label: "03/16/2026",
      label: "Government comment",
      text: "Provide the driveway connection.",
    } as const
    const merged = mergeIssueEvents([
      { ...base, event_id: "pc2", review_round: "2", record_label: "Markup · V2-C2 48", submission: "4th submission", source: { source_id: "s1", relation: "Primary source", filename: "a.xlsx" } },
      { ...base, event_id: "pc3", review_round: "3", record_label: "Markup · V2-C2 48", submission: "5th submission", source: { source_id: "s2", relation: "Also appears in", filename: "b.xlsx" } },
    ] as IssueEvent[])
    expect(merged).toHaveLength(1)
    expect(merged[0].submissions).toEqual(["4th submission", "5th submission"])
    expect(merged[0].record_labels).toEqual(["Markup · V2-C2 48"])
    expect(merged[0].sources?.map((source) => source.source_id)).toEqual(["s1", "s2"])
  })

  it("keeps materially different dated text as separate events", () => {
    const base = { event_type: "government_comment", actor_role: "government", actor: "Reviewer", occurred_at: "", occurred_at_label: "", time_label: "03/16/2026", label: "Government comment", review_round: "2" } as const
    const merged = mergeIssueEvents([
      { ...base, event_id: "a", text: "Revise 12 inches to 6 inches." },
      { ...base, event_id: "b", text: "Revise 6 inches to 12 inches." },
    ] as IssueEvent[])
    expect(merged).toHaveLength(2)
  })
})
