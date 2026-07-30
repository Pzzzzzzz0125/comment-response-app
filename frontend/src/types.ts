export type SourceLocation = {
  document_id: string
  original_document_type: string
  viewer_type: "pdf" | "pdf_preview" | "spreadsheet" | "unsupported"
  page_number?: number
  pdf_bounding_boxes?: number[][]
  exact_quote?: string
  normalized_quote?: string
  sheet_name?: string
  cell_range?: string
  paragraph_index?: number
  preview_document_id?: string
  metadata?: Record<string, unknown>
}

export type SourceReference = {
  source_id: string
  kind?: string
  relation: string
  filename: string
  url?: string
  location?: SourceLocation
}

export type ResponseRecord = {
  response_id: string
  original_text: string
  display_text: string
  display_blocks?: TextBlock[]
  source_filename: string
  sources: SourceReference[]
  source_location: string
  human_review_status: string
}

export type IssueEvent = {
  event_id: string
  event_type: "government_comment" | "applicant_response" | "reviewer_follow_up" | "discussion_note" | "current_applicant_response"
  actor_role: "government" | "company" | "unknown"
  actor: string
  occurred_at: string
  occurred_at_label: string
  label: string
  text: string
  review_round: string
  source?: SourceReference | null
}

export type IssueThread = {
  thread_id: string
  grouping_status: string
  grouping_method: string
  status: string
  event_count: number
  events: IssueEvent[]
}

export type TextBlock = {
  kind?: "paragraph" | "list"
  title?: string
  text?: string
  items?: string[]
}

export type CommentRecord = {
  comment_id: string
  source_file_id?: string
  canonical_document_id?: string
  canonical_comment_id?: string
  occurrence_type?: string
  city: string
  property_project: string
  review_round: string
  discipline: string
  comment_type: string
  reviewer: string
  comment_number: string
  original_text: string
  display_text: string
  display_blocks?: TextBlock[]
  source_filename: string
  sources: SourceReference[]
  source_location: string
  extraction_method: string
  extraction_confidence: number | string
  match_status: string
  human_review_status: string
  category: string
  response: ResponseRecord | null
  issue_thread?: IssueThread
  link: {
    link_id: string
    match_confidence: number | string
    matching_method: string
    review_status: string
  }
}

export type CityData = {
  comments: CommentRecord[]
  cities: { name: string; count: number }[]
  categories: { name: string; count: number }[]
  analysis: CityAnalysis | null
  stats: { comments: number; matched: number }
}

export type CityAnalysis = {
  summary: string
  total_comments: number
  unique_comments: number
  topic_count: number
  common_topic_count?: number
  technical: number
  nontechnical: number
  projects: number
  review_cycles: number
  common_topics: {
    label: string
    occurrences: number
    independent_source_documents?: number
    physical_duplicate_files_excluded?: number
    projects: number
    rounds: number
    cities?: number
    comment_ids: string[]
  }[]
  method_note: string
}

export type Citation = { source_id: string; label: string }

export type KnowledgeAnswer = {
  answer: string
  intent?: string
  conversation_id?: string
  result_set_id?: string
  answer_sections?: Record<string, string>
  metrics?: Record<string, number>
  citations?: Citation[]
  warnings?: string[]
  actions?: { type: string; label: string; result_set_id: string }[]
  query_plan?: { evidence_scope?: string; operations?: string[] }
}

export type SourcePayload = {
  source_id: string
  relation: string
  preview_url: string
  spreadsheet_url: string
  location: SourceLocation
  document: {
    document_id: string
    filename: string
    original_document_type: string
    viewer_type: string
    size: number
    preview_status?: string
    preview_error?: string
  }
}

export type SpreadsheetPayload = {
  sheet_names: string[]
  sheet_name: string
  selection: string
  selection_bounds?: number[]
  columns: string[]
  rows: { row_number: number; cells: { column: string; address: string; value: string | number }[] }[]
  start_row: number
  page_size: number
}

export type SearchResult = {
  comment_id: string
  score: number
  reason?: string
  important_difference?: string
  match_class?: "direct" | "related" | "unverified"
}
