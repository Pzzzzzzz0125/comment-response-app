import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { KnowledgeChat } from "@/components/knowledge-chat"

function setDesktopWorkspace(enabled: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: enabled && query.includes("min-width: 1024px"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  setDesktopWorkspace(false)
})

function knowledgePayload(overrides: Record<string, unknown> = {}) {
  return {
    answer: "The applicant documented the requested correction.[1]",
    answer_type: "HOW_HANDLED",
    conversation_id: "conversation-focus",
    result_set_id: "results-focus",
    coverage: { comment_count: 1, issue_count: 1, project_count: 1, round_count: 1, confirmed_response_count: 1, missing_response_count: 0 },
    representative_evidence: [{
      event_id: "event-focus", comment_id: "comment-focus", claim: "A correction was documented.",
      project: "365 Nature", city: "San Jose", round: "2",
      reviewer_summary: "Reviewer requested the correction.", response_summary: "Applicant documented the correction.",
      comment_excerpt: "Reviewer requested the correction.", response_excerpt: "Applicant documented the correction.",
      primary_source_occurrence_id: "source-response", comment_source_id: "source-comment", response_source_id: "source-response",
      source_occurrences: [
        { source_id: "source-response", filename: "Response.xlsx", role: "response", label: "Response source" },
        { source_id: "source-comment", filename: "Comment.pdf", role: "comment", label: "Comment source" },
      ],
      evidence_level: 3, evidence_badge: "Specific revision cited",
    }],
    citations: [{ citation_id: "citation-stable", citation_index: 1, evidence_id: "event-focus", source_id: "source-response", primary_source_occurrence_id: "source-response", label: "Response source" }],
    retrieval: { stage: 1, coverage: { event_count: 1, project_count: 1 } },
    actions: [], query_plan: { evidence_scope: "verified" },
    ...overrides,
  }
}

describe("KnowledgeChat", () => {
  it("renders ordinary conversation without permit-evidence warnings or diagnostics", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      answer: "Hi! I can answer general questions or help you explore verified permit history.",
      answer_type: "GENERAL_CONVERSATION",
      intent: "general_conversation",
      conversation_id: "conversation-general",
      result_set_id: null,
      validation_status: "not_applicable",
      metrics: {}, coverage: {}, citations: [], evidence: [], representative_evidence: [],
      retrieval: { stage: 0, coverage: {} }, actions: [], suggested_followups: [],
    }), { status: 200, headers: { "Content-Type": "application/json" } })))
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={vi.fn()} onOpenResults={vi.fn()} />)

    const textbox = screen.getByRole("textbox", { name: "Ask Permit History" })
    fireEvent.change(textbox, { target: { value: "Hello" } })
    fireEvent.submit(textbox.closest("form")!)

    expect(await screen.findByText(/I can answer general questions/)).toBeInTheDocument()
    expect(screen.queryByText("No validated evidence")).not.toBeInTheDocument()
    expect(screen.queryByText("Retrieval diagnostics")).not.toBeInTheDocument()
    expect(screen.queryByText("Supporting sources")).not.toBeInTheDocument()
  })

  it("starts a fresh city search from a general-conversation suggestion", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(knowledgePayload({
        answer: "An earlier evidence answer.[1]",
        conversation_id: "conversation-scope",
        result_set_id: "results-old",
      })), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        answer: "Hi! What would you like to explore?",
        answer_type: "GENERAL_CONVERSATION",
        intent: "general_conversation",
        conversation_id: "conversation-scope",
        result_set_id: null,
        validation_status: "not_applicable",
        metrics: {}, coverage: {}, citations: [], evidence: [], representative_evidence: [],
        retrieval: { stage: 0, coverage: {} }, actions: [],
        suggested_followups: ["How have we handled tree-related comments?"],
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(knowledgePayload({
        answer: "Tree comments were handled through plan revisions.[1]",
        conversation_id: "conversation-scope",
        result_set_id: "results-tree",
      })), { status: 200, headers: { "Content-Type": "application/json" } }))
    vi.stubGlobal("fetch", fetchMock)
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={vi.fn()} onOpenResults={vi.fn()} />)

    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    await screen.findByText(/earlier evidence answer/i)

    const textbox = screen.getByRole("textbox", { name: "Ask Permit History" })
    fireEvent.change(textbox, { target: { value: "Hi" } })
    fireEvent.submit(textbox.closest("form")!)
    await screen.findByText(/What would you like to explore/)

    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-related comments?" }))
    await screen.findByText(/Tree comments were handled/)

    const request = fetchMock.mock.calls[2][1] as RequestInit
    const body = JSON.parse(String(request.body))
    expect(body.city_id).toBe("San Jose")
    expect(body).not.toHaveProperty("previous_result_set_id")
    expect(body.guided_action).toBeUndefined()
  })

  it("leads with a coherent answer and reveals evidence/diagnostics on demand", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      answer: "Across the validated records, applicants documented tree-protection measures on the plans.[1]",
      answer_type: "HOW_HANDLED",
      direct_answer: ["Across the validated records, applicants documented tree-protection measures on the plans.[1]"],
      intent: "historical_response_summary",
      conversation_id: "conversation-1",
      result_set_id: "results-1",
      answer_sections: { database_result: "The search screened a bounded San Jose candidate set." },
      metrics: { parent_comments: 2, projects: 1, review_rounds: 1, confirmed_responses: 2, missing_responses: 0 },
      coverage: { comment_count: 2, issue_count: 2, project_count: 1, round_count: 1, confirmed_response_count: 2, missing_response_count: 0 },
      patterns: [{
        title: "Document the protection measures",
        explanation: "In this project, the response made the proposed tree controls reviewable.",
        historical_action: "The applicant added tree-protection notes to Sheet L1.",
        support_level: "single_record",
        supporting_event_ids: ["event-1"],
        supporting_project_ids: ["project-1"],
        evidence_ids: ["event-1"],
      }],
      differences: [{ title: "Where the records differed", text: "This project used a named plan revision.", supporting_event_ids: ["event-1"] }],
      takeaway: { type: "historical_inference", text: "The history suggests that naming the revised sheet makes the response easier to verify." },
      representative_evidence: [{
        event_id: "event-1",
        project_id: "project-1",
        claim: "Tree-protection notes were added before resubmittal.",
        project: "100 Main St",
        city: "San Jose",
        round: "1",
        issue_label: "Construction tree protection measures",
        reviewer_summary: "Reviewer requested construction tree protection.",
        response_summary: "Applicant identified the protection notes on Sheet L1.",
        comment_excerpt: "Provide tree-protection fencing and root-zone measures during construction.",
        response_excerpt: "Tree-protection notes were added to Sheet L1.",
        comment_source_id: "source-1",
        response_source_id: "source-2",
        evidence_level: 3,
        evidence_badge: "Specific revision cited",
        evidence_level_reason: "The confirmed response names the revised sheet.",
      }],
      retrieval: { stage: 1, coverage: { event_count: 2, project_count: 1 } },
      citations: [{ citation_id: "citation-1", citation_index: 1, evidence_id: "event-1", source_id: "source-2", primary_source_occurrence_id: "source-2", role: "response", label: "Response source · Response Letter.pdf · page 3" }],
      actions: [{ type: "show_results", label: "Show 2 supporting records", result_set_id: "results-1" }],
      query_plan: { evidence_scope: "verified" },
    }), { status: 200, headers: { "Content-Type": "application/json" } })))
    const openSource = vi.fn()
    const openResults = vi.fn()
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={openSource} onOpenResults={openResults} />)

    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    expect(await screen.findByText(/Across the validated records/)).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "[1]" })).toBeInTheDocument()
    expect(screen.getByText("Supporting sources")).toBeInTheDocument()
    expect(screen.getByText("Construction tree protection measures")).toBeInTheDocument()
    expect(screen.queryByText("What the history shows")).not.toBeInTheDocument()
    expect(screen.queryByText(/Document the protection measures/)).not.toBeInTheDocument()
    expect(screen.queryByText("Where the records differed")).not.toBeInTheDocument()
    expect(screen.queryByText("What this history suggests")).not.toBeInTheDocument()
    expect(screen.queryByText(/Provide tree-protection fencing/)).not.toBeInTheDocument()
    expect(screen.getByText(/Stage 1/)).not.toBeVisible()

    fireEvent.click(screen.getByText("View evidence"))
    expect(screen.getByText(/Provide tree-protection fencing/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Open original source" }))
    expect(openSource).toHaveBeenCalledWith("source-2")

    fireEvent.click(screen.getByRole("button", { name: "Back to answer" }))
    fireEvent.click(screen.getByText("Retrieval diagnostics"))
    expect(screen.getByText(/Stage 1 · 2 validated events · 1 projects/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /Show 2 supporting records/ }))
    expect(openResults).toHaveBeenCalledWith("results-1")
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/knowledge-chat", expect.objectContaining({ method: "POST" })))
  })

  it("renders a count as a compact analytical answer with representative evidence", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      answer: "San Jose has 12 validated setback comments. One representative project shows a plan correction.[1]",
      answer_type: "COUNT",
      direct_answer: ["San Jose has 12 validated setback comments."],
      conversation_id: "conversation-count",
      result_set_id: "results-count",
      coverage: { comment_count: 12, issue_count: 9, project_count: 4, round_count: 6, confirmed_response_count: 7, missing_response_count: 5 },
      representative_evidence: [{ event_id: "event-count", claim: "A representative correction.", project: "Count Project", city: "San Jose", issue_label: "Setback dimension correction", primary_source_occurrence_id: "source-count", comment_source_id: "source-count", source_occurrences: [{ source_id: "source-count", role: "comment", label: "Comment source" }] }],
      citations: [{ citation_id: "citation-count", citation_index: 1, evidence_id: "event-count", source_id: "source-count", primary_source_occurrence_id: "source-count", label: "Comment source" }],
      actions: [],
    }), { status: 200, headers: { "Content-Type": "application/json" } })))
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={vi.fn()} onOpenResults={vi.fn()} />)
    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    expect(await screen.findByText(/San Jose has 12 validated setback comments/)).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "[1]" })).toBeInTheDocument()
    expect(screen.getByText((_, node) => node?.tagName === "P" && /12 relevant comments/.test(node.textContent || "") && /4 projects/.test(node.textContent || "") && /6 review rounds/.test(node.textContent || "") && /7 comments/.test(node.textContent || ""))).toBeInTheDocument()
    expect(screen.getByText("Supporting sources")).toBeInTheDocument()
    expect(screen.getByText("Setback dimension correction")).toBeInTheDocument()
  })

  it("treats not-required validation as grounded when backend evidence is authoritative", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(knowledgePayload({
      validation_status: "not_required",
    })), { status: 200, headers: { "Content-Type": "application/json" } })))
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={vi.fn()} onOpenResults={vi.fn()} />)
    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    expect(await screen.findByText(/applicant documented the requested correction/i)).toBeInTheDocument()
    expect(screen.queryByText("No validated evidence")).not.toBeInTheDocument()
    expect(screen.getByText("Supporting sources")).toBeInTheDocument()
  })

  it("turns an evidence-aware explore-next action into a contextual chat turn", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        answer: "I found recurring driveway issues.",
        intent: "topic_summary",
        conversation_id: "conversation-2",
        result_set_id: "results-2",
        metrics: { parent_comments: 4, projects: 2, review_rounds: 2, confirmed_responses: 1, missing_responses: 3 },
        actions: [{ type: "timeline_analysis", label: "See what repeated across review rounds", result_set_id: "results-2", parameters: { result_set_id: "results-2" } }],
        query_plan: { evidence_scope: "verified" },
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        answer: "The issue continued into the next round.",
        intent: "topic_summary",
        conversation_id: "conversation-2",
        result_set_id: "results-3",
        metrics: { parent_comments: 2, projects: 1, review_rounds: 2, confirmed_responses: 0, missing_responses: 2 },
        actions: [],
        query_plan: { evidence_scope: "verified" },
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
    vi.stubGlobal("fetch", fetchMock)
    render(<KnowledgeChat city="Menlo Park" filters={{}} onOpenSource={vi.fn()} onOpenResults={vi.fn()} />)
    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    const action = await screen.findByRole("button", { name: "See what repeated across review rounds" })
    fireEvent.click(action)
    await screen.findByText("The issue continued into the next round.")
    const secondRequest = fetchMock.mock.calls[1][1] as RequestInit
    const body = JSON.parse(String(secondRequest.body))
    expect(body.guided_action.type).toBe("timeline_analysis")
    expect(body.guided_action.result_set_id).toBe("results-2")
    expect(body.previous_result_set_id).toBe("results-2")
  })

  it("submits a model follow-up with its evidence-aware query and reuse decision", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        answer: "Tree comments were handled through plan revisions.[1]",
        intent: "historical_response_summary",
        conversation_id: "conversation-model-followup",
        result_set_id: "results-tree",
        metrics: { parent_comments: 2, projects: 1, review_rounds: 2, confirmed_responses: 2, missing_responses: 0 },
        actions: [{
          type: "model_followup",
          label: "Why did this issue continue?",
          result_set_id: "results-tree",
          parameters: {
            result_set_id: "results-tree",
            query: "Why did the tree-protection issue continue across rounds?",
            reuse_current_evidence: true,
          },
        }],
        query_plan: { evidence_scope: "verified" },
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        answer: "The reviewer reissued the requirement after the first response.",
        intent: "timeline_analysis",
        conversation_id: "conversation-model-followup",
        result_set_id: "results-tree-followup",
        metrics: { parent_comments: 2, projects: 1, review_rounds: 2, confirmed_responses: 2, missing_responses: 0 },
        actions: [],
        query_plan: { evidence_scope: "verified" },
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
    vi.stubGlobal("fetch", fetchMock)
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={vi.fn()} onOpenResults={vi.fn()} />)

    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    fireEvent.click(await screen.findByRole("button", { name: "Why did this issue continue?" }))
    await screen.findByText("The reviewer reissued the requirement after the first response.")

    const request = fetchMock.mock.calls[1][1] as RequestInit
    const body = JSON.parse(String(request.body))
    expect(body.message).toBe("Why did the tree-protection issue continue across rounds?")
    expect(body.previous_result_set_id).toBe("results-tree")
    expect(body.guided_action.type).toBe("model_followup")
    expect(body.guided_action.parameters.reuse_current_evidence).toBe(true)
  })

  it("can explicitly broaden only the current city's history and warns that it may take longer", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        answer: "I found a narrow tag-based result.", conversation_id: "conversation-broader", result_set_id: "results-narrow",
        actions: [{ type: "broaden_scope", label: "Search broader San Jose history (may take longer)", result_set_id: "results-narrow", parameters: { result_set_id: "results-narrow", scope: "city", may_take_longer: true } }],
        query_plan: { evidence_scope: "verified" }, retrieval: { stage: 1 },
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        answer: "I searched the broader San Jose history.", conversation_id: "conversation-broader", result_set_id: "results-wide", actions: [],
        query_plan: { evidence_scope: "verified" }, retrieval: { stage: 3 },
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
    vi.stubGlobal("fetch", fetchMock)
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={vi.fn()} onOpenResults={vi.fn()} />)

    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    fireEvent.click(await screen.findByRole("button", { name: /Search broader San Jose history \(may take longer\)/ }))
    await screen.findByText("I searched the broader San Jose history.")
    const body = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body))
    expect(body.guided_action.type).toBe("broaden_scope")
    expect(body.city_id).toBe("San Jose")
  })

  it("preserves the same answer, session context, and draft across focus and compact modes", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(knowledgePayload()), { status: 200, headers: { "Content-Type": "application/json" } }))
    vi.stubGlobal("fetch", fetchMock)
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={vi.fn()} onOpenResults={vi.fn()} />)

    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    await screen.findByText(/applicant documented the requested correction/i)
    fireEvent.change(screen.getByRole("textbox", { name: "Ask Permit History" }), { target: { value: "Keep this follow-up draft" } })
    fireEvent.click(screen.getByRole("button", { name: "Expand AI workspace" }))

    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Ask Permit History" })).toHaveValue("Keep this follow-up draft")
    expect(screen.getByText(/applicant documented the requested correction/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Collapse AI workspace" }))
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Ask Permit History" })).toHaveValue("Keep this follow-up draft")
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("opens stable citations in the Library-style evidence panel without loading the original source", async () => {
    setDesktopWorkspace(true)
    const payload = knowledgePayload({
      answer: "The response documents one correction.[1] A second project documents another.[2]",
      representative_evidence: [
        ...(knowledgePayload().representative_evidence as object[]),
        {
          event_id: "event-second", comment_id: "comment-second", claim: "A second correction was documented.",
          project: "701 Clover", city: "San Jose", round: "3", reviewer_summary: "A second correction was requested.",
          primary_source_occurrence_id: "source-second", comment_source_id: "source-second",
          source_occurrences: [{ source_id: "source-second", filename: "Second.bin", role: "comment" }],
          evidence_level: 1, evidence_badge: "Comment only",
        },
      ],
      citations: [
        ...(knowledgePayload().citations as object[]),
        { citation_id: "citation-second", citation_index: 2, evidence_id: "event-second", source_id: "source-second", primary_source_occurrence_id: "source-second", label: "Second source" },
      ],
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } }))
    vi.stubGlobal("fetch", fetchMock)
    const openSource = vi.fn()
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={openSource} onOpenResults={vi.fn()} />)

    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    await screen.findByRole("link", { name: "[1]" })
    fireEvent.click(screen.getByRole("button", { name: "Expand AI workspace" }))
    fireEvent.click(screen.getByRole("link", { name: "[1]" }))
    expect((await screen.findAllByText("Reviewer requested the correction.")).length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText("View evidence")[0].closest("article")).toHaveAttribute("aria-current", "true")
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(screen.getByRole("button", { name: "Previous evidence" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Next evidence" })).toBeInTheDocument()
    expect(screen.getByText("Evidence 1 of 2")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("link", { name: "[2]" }))
    expect((await screen.findAllByText("A second correction was requested.")).length).toBeGreaterThanOrEqual(1)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/response documents one correction/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Open original source" }))
    expect(openSource).toHaveBeenCalledWith("source-second")
    expect(screen.getByRole("button", { name: "Close evidence panel" })).toBeInTheDocument()
  })

  it("renders one canonical evidence card with unique multiple-source choices", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(knowledgePayload()), { status: 200, headers: { "Content-Type": "application/json" } })))
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={vi.fn()} onOpenResults={vi.fn()} />)
    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    expect(await screen.findByText("Supporting sources")).toBeInTheDocument()
    expect(screen.getAllByText("365 Nature")).toHaveLength(1)
    fireEvent.click(screen.getByText("View evidence"))
    fireEvent.click(screen.getByRole("button", { name: "2 sources" }))
    expect(screen.getByText("Response.xlsx")).toBeInTheDocument()
    expect(screen.getByText("Comment.pdf")).toBeInTheDocument()
    expect(screen.getAllByText("Primary")).toHaveLength(1)
  })

  it("does not fetch an original source until the user explicitly requests it", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(knowledgePayload()), { status: 200, headers: { "Content-Type": "application/json" } }))
    vi.stubGlobal("fetch", fetchMock)
    const openSource = vi.fn()
    render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={openSource} onOpenResults={vi.fn()} />)
    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    await screen.findByRole("link", { name: "[1]" })
    fireEvent.click(screen.getByRole("button", { name: "Expand AI workspace" }))
    fireEvent.click(screen.getByRole("link", { name: "[1]" }))
    expect(await screen.findByText("Reviewer requested the correction.")).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole("button", { name: "Open original source" }))
    expect(openSource).toHaveBeenCalledWith("source-response")
    fireEvent.click(screen.getByRole("button", { name: "Back to answer" }))
    expect(screen.getByText(/applicant documented the requested correction/i)).toBeInTheDocument()
  })

  it("hands fullscreen focus to the source viewer and restores the same evidence workspace", async () => {
    setDesktopWorkspace(true)
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(knowledgePayload()), { status: 200, headers: { "Content-Type": "application/json" } })))
    const openSource = vi.fn()
    const view = render(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={openSource} onOpenResults={vi.fn()} sourceViewerOpen={false} />)

    fireEvent.click(screen.getByRole("button", { name: "How have we handled tree-protection comments?" }))
    await screen.findByRole("link", { name: "[1]" })
    fireEvent.click(screen.getByRole("button", { name: "Expand AI workspace" }))
    fireEvent.click(screen.getByRole("link", { name: "[1]" }))
    expect(await screen.findByRole("button", { name: "Open original source" })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Open original source" }))
    expect(openSource).toHaveBeenCalledWith("source-response")

    view.rerender(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={openSource} onOpenResults={vi.fn()} sourceViewerOpen />)
    expect(screen.queryByRole("dialog", { name: "Permit History AI research workspace" })).not.toBeInTheDocument()

    view.rerender(<KnowledgeChat city="San Jose" filters={{}} onOpenSource={openSource} onOpenResults={vi.fn()} sourceViewerOpen={false} />)
    expect(screen.getByRole("dialog", { name: "Permit History AI research workspace" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Close evidence panel" })).toBeInTheDocument()
    expect(screen.getByText("Evidence 1 of 1")).toBeInTheDocument()
  })
})
