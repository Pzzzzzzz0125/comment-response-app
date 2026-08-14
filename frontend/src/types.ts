export type SourceLocation = {
  document_id: string
  original_document_type: string
  viewer_type: "pdf" | "pdf_preview" | "spreadsheet" | "unsupported"
  page_number?: number
  pdf_bounding_boxes?: number[][]
  pdf_bounding_boxes_by_page?: Record<string, number[][]>
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

export type DisplayStructureBlock = {
  type: "paragraph" | "list_item" | "heading" | string
  start: number
  end: number
  label?: string
}

export type TextReconstruction = {
  version?: string
  method?: string
  source_unit_ids?: string[]
  verified?: boolean
  verification_version?: string
  uncertain?: boolean
  raw_text_source?: string
}

export type ResponseRecord = {
  response_id: string
  original_text: string
  text_raw?: string
  text_reconstructed?: string
  normalized_identity_text_v2?: string
  normalized_search_text_v2?: string
  display_structure?: DisplayStructureBlock[]
  source_unit_ids?: string[]
  reconstruction?: TextReconstruction
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
  time_label?: string
  time_basis?: string
  time_precision?: "exact" | "exact_date" | "available_by" | "document" | "round_only" | "unknown"
  source_date?: string
  embedded_date?: string
  embedded_date_note?: string
  submission?: string
  submissions?: string[]
  record_label?: string
  record_labels?: string[]
  label: string
  text: string
  review_round: string
  effective_round?: string
  observed_in_document_round?: string
  source?: SourceReference | null
  sources?: SourceReference[]
  merged_event_ids?: string[]
  date_variants?: string[]
  text_reconstructed?: string
  display_structure?: DisplayStructureBlock[]
  source_unit_ids?: string[]
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
  city_id?: string
  site_id?: string
  site_name?: string
  project_id?: string
  project_alias?: string
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
  comment_label?: string
  original_text: string
  text_raw?: string
  text_reconstructed?: string
  normalized_identity_text_v2?: string
  normalized_search_text_v2?: string
  display_structure?: DisplayStructureBlock[]
  source_unit_ids?: string[]
  reconstruction?: TextReconstruction
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
  recurring_issues?: RecurringIssue[]
  recurring_issue_stats?: {
    total: number
    open: number
    resolved: number
    unknown: number
    average_rounds_to_resolution: number | null
    longest_running_rounds: number
    longest_running_issue_id: string
    longest_running_title: string
  }
  method_note: string
}

export type RecurringIssueEvent = {
  event_id: string
  effective_round: string
  event_type: string
  comment_text: string
  response_text: string
  source_occurrence_count: number
  source_document_count: number
  source_comment_ids: string[]
  relationship_to_previous: string
}

export type RecurringIssue = {
  issue_thread_id: string
  project_id: string
  site_id: string
  site_name: string
  city: string
  title: string
  common_topic: string
  discipline: string
  status: "open" | "resolved" | "unknown" | string
  status_reason: string
  persistence_explanation?: string
  first_round: number
  latest_round: number
  round_count: number
  event_count: number
  history_event_count?: number
  comment_event_count?: number
  response_event_count?: number
  source_occurrence_count: number
  source_document_count: number
  company_response_count: number
  comment_ids: string[]
  events: RecurringIssueEvent[]
}

export type Citation = {
  citation_id?: string
  citation_index?: number
  evidence_id?: string
  comment_id?: string
  role?: "comment" | "response" | "later_review" | string
  source_id: string
  primary_source_occurrence_id?: string
  source_occurrence_ids?: string[]
  label: string
}

export type KnowledgeSourceOccurrence = {
  source_id: string
  filename?: string
  relation?: string
  role?: "comment" | "response" | "later_review" | string
  label?: string
}

export type GuidedAction = {
  type: string
  label: string
  result_set_id: string
  parameters?: Record<string, string | boolean | number>
}

export type KnowledgeAnswer = {
  answer: string
  answer_type?: "COUNT" | "FACT_LOOKUP" | "HISTORY_SUMMARY" | "HOW_HANDLED" | "COMPARISON" | "EXAMPLE_SEARCH" | "TIMELINE" | "PRACTICAL_LESSONS" | "FOLLOW_UP"
  direct_answer?: string[]
  intent?: string
  conversation_id?: string
  result_set_id?: string
  answer_sections?: Record<string, string>
  key_patterns?: KnowledgePattern[]
  patterns?: KnowledgePattern[]
  differences?: { title: string; text: string; supporting_event_ids?: string[] }[]
  takeaway?: { text: string; type: "historical_inference" } | null
  limitations?: string[]
  suggested_followups?: string[]
  evidence?: {
    event_id?: string
    comment_id?: string
    issue_id?: string
    project_id?: string
    claim: string
    project?: string
    city?: string
    round?: string
    issue_label?: string
    topic_label?: string
    summary?: string
    reviewer_summary?: string
    response_summary?: string
    comment_excerpt?: string
    response_excerpt?: string
    later_review_excerpt?: string
    comment_source_id?: string | null
    response_source_id?: string | null
    later_review_source_id?: string | null
    source_ids?: string[]
    primary_source_occurrence_id?: string | null
    source_occurrence_ids?: string[]
    source_occurrences?: KnowledgeSourceOccurrence[]
    evidence_level?: number
    evidence_badge?: string
    evidence_level_reason?: string
  }[]
  representative_evidence?: KnowledgeAnswer["evidence"]
  coverage?: {
    comments: number
    projects: number
    review_rounds: number
    confirmed_responses: number
    comment_count?: number
    issue_count?: number
    project_count?: number
    round_count?: number
    confirmed_response_count?: number
    missing_response_count?: number
  }
  uncertainty?: string
  metrics?: Record<string, number>
  citations?: Citation[]
  warnings?: string[]
  actions?: GuidedAction[]
  query_plan?: {
    raw_query?: string
    mode?: string
    subject?: string
    intent?: string
    operations?: string[]
    primary_topics?: string[]
    objects?: string[]
    response_requirements?: {
      confirmed_responses_required?: boolean
      comparison_required?: boolean
      timeline_required?: boolean
    }
    scope?: {
      city_ids?: string[]
      site_ids?: string[]
      project_ids?: string[]
      review_rounds?: string[]
      date_range?: { from?: string; to?: string }
    }
    evidence_scope?: string
  }
  retrieval?: {
    stage?: number
    coverage?: Record<string, number>
    candidate_coverage?: Record<string, number>
    matched_tags?: Record<string, string[]>
    suggested_tags?: { event_id?: string; suggested_tag?: string; status?: string }[]
    fallback_reason?: string
  }
  validation_status?: "validated" | "not_required" | "unverified" | "insufficient_comparison" | "no_validated_evidence"
  validation_summary?: { relevant_comments: number; relevant_projects: number; excluded_off_topic: number }
  excluded_records?: { comment_id: string; project?: string; city?: string; record_topic?: string; exclude_reason?: string; supporting_excerpt?: string }[]
}

export type KnowledgePattern = {
  title: string
  explanation: string
  historical_action?: string
  support_level?: "single_record" | "single_project" | "multiple_records" | "cross_project"
  supporting_event_ids?: string[]
  supporting_project_ids?: string[]
  evidence_ids?: string[]
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
  context_range?: string
  context_bounds?: number[]
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
