"use strict";

const state = {
  city: "",
  comments: [],
  visible: [],
  cities: [],
  categories: [],
  analysis: null,
  selected: new Set(),
  activeId: null,
  similarity: new Map(),
  searchExplanations: new Map(),
  searchMatchClasses: new Map(),
  searchHasDirectMatches: false,
  searchNoResultMessage: "",
  similarityMode: false,
  searchModeLabel: "Gemini semantic ranking",
  sourceViewerRequest: 0,
  appConfig: null,
  reviewQueue: [],
  reviewQueueIndex: 0,
  reviewCounts: null,
};

const $ = (id) => document.getElementById(id);

const elements = {
  city: $("citySelect"),
  analysisSummary: $("analysisSummary"),
  analysisMethod: $("analysisMethod"),
  totalComments: $("totalComments"),
  uniqueComments: $("uniqueComments"),
  technicalComments: $("technicalComments"),
  nontechnicalComments: $("nontechnicalComments"),
  commonTopics: $("commonTopics"),
  datasetCount: $("datasetCount"),
  visibleCount: $("visibleCount"),
  historySearchForm: $("historySearchForm"),
  historySearch: $("historySearch"),
  smartSearchPrompt: $("smartSearchPrompt"),
  smartSearchButton: $("smartSearchButton"),
  searchModeNotice: $("searchModeNotice"),
  searchModeText: $("searchModeText"),
  clearSmartSearch: $("clearSmartSearchButton"),
  project: $("projectFilter"),
  discipline: $("disciplineFilter"),
  round: $("roundFilter"),
  match: $("matchFilter"),
  category: $("categoryFilter"),
  review: $("reviewFilter"),
  selectVisible: $("selectVisible"),
  selectedCount: $("selectedCount"),
  categorize: $("categorizeButton"),
  list: $("commentList"),
  emptyList: $("emptyList"),
  resetFilters: $("resetFiltersButton"),
  detailEmpty: $("detailEmpty"),
  detailContent: $("detailContent"),
  sourceViewerDialog: $("sourceViewerDialog"),
  sourceViewerTitle: $("sourceViewerTitle"),
  sourceViewerMeta: $("sourceViewerMeta"),
  sourceViewerClose: $("sourceViewerClose"),
  sourceViewerLocation: $("sourceViewerLocation"),
  sourceViewerQuote: $("sourceViewerQuote"),
  sourceViewerStatus: $("sourceViewerStatus"),
  sourceViewerDownload: $("sourceViewerDownload"),
  sourceViewerLoading: $("sourceViewerLoading"),
  adobePdfViewer: $("adobePdfViewer"),
  nativePdfViewer: $("nativePdfViewer"),
  spreadsheetViewer: $("spreadsheetViewer"),
  spreadsheetSheet: $("spreadsheetSheet"),
  spreadsheetSelection: $("spreadsheetSelection"),
  spreadsheetGrid: $("spreadsheetGrid"),
  unsupportedViewer: $("unsupportedViewer"),
  unsupportedViewerMessage: $("unsupportedViewerMessage"),
  categoryDialog: $("categoryDialog"),
  categoryForm: $("categoryForm"),
  categoryInput: $("categoryInput"),
  categorySuggestions: $("categorySuggestions"),
  categoryDialogHelp: $("categoryDialogHelp"),
  removeCategory: $("removeCategoryButton"),
  toast: $("toast"),
  responseReviewButton: $("responseReviewButton"),
  responseReviewCount: $("responseReviewCount"),
  responseReviewDialog: $("responseReviewDialog"),
  responseReviewClose: $("responseReviewClose"),
  responseReviewProgress: $("responseReviewProgress"),
  responseReviewStatus: $("responseReviewStatus"),
  responseReviewCity: $("responseReviewCity"),
  responseReviewPrevious: $("responseReviewPrevious"),
  responseReviewSkip: $("responseReviewSkip"),
  responseReviewEmpty: $("responseReviewEmpty"),
  responseReviewContent: $("responseReviewContent"),
  responseReviewMeta: $("responseReviewMeta"),
  responseReviewCommentSources: $("responseReviewCommentSources"),
  responseReviewResponseSources: $("responseReviewResponseSources"),
  responseReviewCommentText: $("responseReviewCommentText"),
  responseReviewResponseText: $("responseReviewResponseText"),
  responseReviewNote: $("responseReviewNote"),
  responseReviewUndo: $("responseReviewUndo"),
  responseReviewFollowup: $("responseReviewFollowup"),
  responseReviewReject: $("responseReviewReject"),
  responseReviewConfirm: $("responseReviewConfirm"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => elements.toast.classList.add("hidden"), 3200);
}

function option(select, value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  select.appendChild(node);
}

function populateSelect(select, values, allLabel, current = "") {
  select.replaceChildren();
  option(select, "", allLabel);
  values.forEach((value) => option(select, value, value));
  if ([...select.options].some((item) => item.value === current)) select.value = current;
}

function unique(field) {
  return [...new Set(state.comments.map((comment) => String(comment[field] || "unknown")))].sort((a, b) =>
    a.localeCompare(b, undefined, { numeric: true })
  );
}

async function loadCity(city) {
  const payload = await api(`/api/data?city=${encodeURIComponent(city)}`);
  state.comments = payload.comments;
  state.cities = payload.cities;
  state.categories = payload.categories;
  state.analysis = payload.analysis;
  state.city = city;
  state.selected.clear();
  state.activeId = null;
  state.similarity.clear();
  state.similarityMode = false;
  renderCityOptions();
  renderAnalysis();
  populateFilters();
  clearFilterValues();
  updateSearchState();
  applyFilters();
  elements.datasetCount.textContent = `${payload.stats.comments} ${city} comments · ${payload.stats.matched} with responses`;
}

function renderAnalysis() {
  const analysis = state.analysis;
  if (!analysis) return;
  elements.analysisSummary.textContent = analysis.summary;
  elements.analysisMethod.textContent = analysis.method_note;
  elements.totalComments.textContent = analysis.total_comments;
  elements.uniqueComments.textContent = analysis.unique_comments;
  elements.technicalComments.textContent = analysis.technical;
  elements.nontechnicalComments.textContent = analysis.nontechnical;
  elements.commonTopics.replaceChildren();
  if (!analysis.common_topics.length) {
    const empty = document.createElement("p");
    empty.className = "topics-empty";
    empty.textContent = "No repeated topics were detected for this city.";
    elements.commonTopics.append(empty);
    return;
  }
  analysis.common_topics.forEach((topic) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "topic-card";
    const count = document.createElement("strong");
    count.textContent = `${topic.occurrences}×`;
    const copy = document.createElement("span");
    copy.textContent = topic.label;
    const meta = document.createElement("small");
    meta.textContent = `${topic.projects} project scope${topic.projects === 1 ? "" : "s"} · ${topic.rounds} review cycle${topic.rounds === 1 ? "" : "s"}`;
    button.append(count, copy, meta);
    button.addEventListener("click", () => {
      state.similarity = new Map(topic.comment_ids.map((id) => [id, 1]));
      state.similarityMode = true;
      state.searchModeLabel = "deduplicated topic group";
      elements.historySearch.value = topic.label;
      updateSearchState();
      applyFilters();
      document.querySelector(".workspace").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    elements.commonTopics.append(button);
  });
}

function renderCityOptions() {
  const current = state.city;
  elements.city.replaceChildren();
  state.cities.forEach((city) => option(elements.city, city.name, `${city.name} · ${city.count}`));
  elements.city.value = current;
}

function populateFilters() {
  const currentCategory = elements.category.value;
  populateSelect(elements.project, unique("property_project"), "All projects");
  populateSelect(elements.discipline, unique("discipline"), "All disciplines");
  populateSelect(elements.round, unique("review_round"), "All rounds");
  populateSelect(elements.category, state.categories.map((item) => item.name), "All categories", currentCategory);
  elements.categorySuggestions.replaceChildren();
  state.categories.filter((item) => item.name !== "Uncategorized").forEach((item) => {
    option(elements.categorySuggestions, item.name, item.name);
  });
}

function clearFilterValues() {
  elements.historySearch.value = "";
  [elements.project, elements.discipline, elements.round, elements.match, elements.category, elements.review]
    .forEach((select) => { select.value = ""; });
}

function applyFilters() {
  const query = elements.historySearch.value.trim().toLocaleLowerCase();
  const filters = {
    property_project: elements.project.value,
    discipline: elements.discipline.value,
    review_round: elements.round.value,
    match_status: elements.match.value,
    category: elements.category.value,
    human_review_status: elements.review.value,
  };
  let comments = state.comments.filter((comment) => {
    if (state.similarityMode && !state.similarity.has(comment.comment_id)) return false;
    if (query && !state.similarityMode) {
      const haystack = [comment.display_text, comment.property_project, comment.discipline, comment.source_filename, comment.category]
        .join(" ").toLocaleLowerCase();
      if (!haystack.includes(query)) return false;
    }
    return Object.entries(filters).every(([field, value]) => !value || String(comment[field]) === value);
  });
  if (state.similarityMode) {
    const rank = { direct: 0, related: 1, unverified: 2 };
    comments.sort((a, b) => {
      const classDifference = (rank[state.searchMatchClasses.get(a.comment_id)] ?? 3) - (rank[state.searchMatchClasses.get(b.comment_id)] ?? 3);
      return classDifference || (state.similarity.get(b.comment_id) || 0) - (state.similarity.get(a.comment_id) || 0);
    });
  }
  state.visible = comments;
  if (state.activeId && !comments.some((item) => item.comment_id === state.activeId)) state.activeId = null;
  if (!state.activeId && comments.length) state.activeId = comments[0].comment_id;
  renderList();
  renderDetail();
  updateSelectionToolbar();
  updateSearchState();
}

function badge(text, className) {
  const node = document.createElement("span");
  node.className = `badge ${className}`;
  node.textContent = text;
  return node;
}

function renderList() {
  elements.list.replaceChildren();
  elements.visibleCount.textContent = state.visible.length;
  elements.emptyList.classList.toggle("hidden", state.visible.length > 0);
  elements.list.classList.toggle("hidden", state.visible.length === 0);
  const fragment = document.createDocumentFragment();
  let previousMatchClass = "";
  state.visible.forEach((comment) => {
    if (state.similarityMode) {
      const matchClass = state.searchMatchClasses.get(comment.comment_id) || "unverified";
      if (matchClass !== previousMatchClass) {
        const heading = document.createElement("h3");
        heading.className = `search-result-group ${matchClass}`;
        heading.textContent = matchClass === "direct" ? "Direct precedents" : matchClass === "related" ? "Related precedents" : "Unverified fallback candidates";
        fragment.append(heading);
        previousMatchClass = matchClass;
      }
    }
    const card = document.createElement("div");
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    card.className = `comment-card${comment.comment_id === state.activeId ? " active" : ""}`;
    card.dataset.id = comment.comment_id;
    const activate = () => {
      state.activeId = comment.comment_id;
      renderList();
      renderDetail();
    };
    card.addEventListener("click", activate);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate();
      }
    });

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "comment-checkbox";
    checkbox.checked = state.selected.has(comment.comment_id);
    checkbox.setAttribute("aria-label", `Select comment ${comment.comment_number}`);
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      checkbox.checked ? state.selected.add(comment.comment_id) : state.selected.delete(comment.comment_id);
      updateSelectionToolbar();
    });

    const main = document.createElement("div");
    main.className = "comment-card-main";
    const topline = document.createElement("div");
    topline.className = "card-topline";
    const discipline = document.createElement("span");
    discipline.className = "discipline";
    discipline.textContent = comment.discipline;
    topline.append(discipline);
    topline.append(badge(comment.comment_type === "technical" ? "Technical" : "Non-technical", comment.comment_type));
    topline.append(badge(comment.match_status === "matched" ? "Has response" : "No response", comment.match_status));
    if (comment.category !== "Uncategorized") topline.append(badge(comment.category, "category"));
    if (state.similarityMode) topline.append(badge(`${Math.min(100, Math.round((state.similarity.get(comment.comment_id) || 0) * 100))}%`, "similarity-score"));

    const text = document.createElement("p");
    text.className = "card-text";
    text.textContent = comment.display_text;
    const meta = document.createElement("div");
    meta.className = "card-meta";
    [comment.property_project, `Round ${comment.review_round}`, `${comment.source_filename} · ${comment.source_location}`].forEach((value) => {
      const item = document.createElement("span");
      item.textContent = value;
      item.title = value;
      meta.append(item);
    });
    main.append(topline, text, meta);
    const explanation = state.searchExplanations.get(comment.comment_id);
    if (state.similarityMode && explanation) {
      const why = document.createElement("p");
      why.className = "search-match-reason";
      why.textContent = `Why it matched: ${explanation.reason}`;
      main.append(why);
      if (explanation.important_difference) {
        const difference = document.createElement("p");
        difference.className = "search-match-difference";
        difference.textContent = `Important difference: ${explanation.important_difference}`;
        main.append(difference);
      }
    }
    card.append(checkbox, main);
    fragment.append(card);
  });
  elements.list.append(fragment);
}

function metadataChip(text) {
  const chip = document.createElement("span");
  chip.className = "metadata-chip";
  chip.textContent = text;
  return chip;
}

function sourceLink(source) {
  if (source.kind === "external") {
    const link = document.createElement("a");
    link.className = "source-link";
    link.target = "_blank";
    link.rel = "noopener";
    link.href = source.url;
    link.textContent = `${source.relation}: ${source.filename}`;
    return link;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "source-link source-viewer-trigger";
  const legacyLocation = source.location?.metadata?.legacy_location || "";
  button.textContent = `${source.relation}: ${source.filename}${legacyLocation ? ` · ${legacyLocation}` : ""}`;
  button.addEventListener("click", () => SourceViewer.open(source.source_id));
  return button;
}

function sourceLinks(sources) {
  const container = document.createElement("div");
  container.className = "source-links";
  (sources || []).forEach((source) => container.append(sourceLink(source)));
  return container;
}

async function loadReviewSummary() {
  const payload = await api("/api/link-reviews?status=all&summary=1");
  state.reviewCounts = payload.counts;
  const remaining = payload.counts.suggested + payload.counts.needs_review + payload.counts.needs_followup;
  elements.responseReviewCount.textContent = remaining;
  elements.responseReviewButton.title = `${remaining} links still need attention; ${payload.counts.completed} completed`;
}

function populateReviewCities() {
  const selected = elements.responseReviewCity.value;
  elements.responseReviewCity.replaceChildren(new Option("All cities", ""));
  state.cities.forEach((city) => elements.responseReviewCity.append(new Option(city.name, city.name)));
  elements.responseReviewCity.value = selected;
}

async function loadReviewQueue(preferredLinkId = "") {
  const status = elements.responseReviewStatus.value;
  const city = elements.responseReviewCity.value;
  const payload = await api(`/api/link-reviews?status=${encodeURIComponent(status)}&city=${encodeURIComponent(city)}`);
  state.reviewQueue = payload.items;
  state.reviewCounts = payload.counts;
  const preferredIndex = preferredLinkId ? state.reviewQueue.findIndex((item) => item.link_id === preferredLinkId) : -1;
  state.reviewQueueIndex = preferredIndex >= 0 ? preferredIndex : Math.min(state.reviewQueueIndex, Math.max(0, state.reviewQueue.length - 1));
  renderReviewQueue();
  await loadReviewSummary();
}

function renderReviewQueue() {
  const item = state.reviewQueue[state.reviewQueueIndex];
  const counts = state.reviewCounts || { total: 0, completed: 0, suggested: 0, needs_review: 0, needs_followup: 0 };
  const remaining = counts.suggested + counts.needs_review + counts.needs_followup;
  elements.responseReviewProgress.textContent = `${counts.completed} of ${counts.total} completed · ${remaining} need attention${state.reviewQueue.length ? ` · showing ${state.reviewQueueIndex + 1} of ${state.reviewQueue.length}` : ""}`;
  elements.responseReviewEmpty.classList.toggle("hidden", Boolean(item));
  elements.responseReviewContent.classList.toggle("hidden", !item);
  elements.responseReviewPrevious.disabled = !item || state.reviewQueue.length < 2;
  elements.responseReviewSkip.disabled = !item || state.reviewQueue.length < 2;
  elements.responseReviewFollowup.disabled = !item;
  elements.responseReviewReject.disabled = !item;
  elements.responseReviewConfirm.disabled = !item;
  elements.responseReviewUndo.disabled = !item;
  if (!item) return;

  const comment = item.comment;
  const response = comment.response;
  elements.responseReviewMeta.replaceChildren(
    metadataChip(item.status.replaceAll("_", " ")),
    metadataChip(comment.city), metadataChip(comment.property_project),
    metadataChip(`Round ${comment.review_round}`), metadataChip(comment.discipline),
    metadataChip(`Comment ${comment.comment_number || "—"}`),
  );
  elements.responseReviewCommentText.textContent = comment.display_text;
  elements.responseReviewResponseText.textContent = response?.display_text || "No response text is stored for this link.";
  elements.responseReviewCommentSources.replaceChildren(sourceLinks(comment.sources));
  elements.responseReviewResponseSources.replaceChildren(sourceLinks(response?.sources || []));
  elements.responseReviewNote.value = item.note || "";
  elements.responseReviewUndo.disabled = item.base_status === item.status && !item.note;
}

function moveReviewQueue(offset) {
  if (!state.reviewQueue.length) return;
  state.reviewQueueIndex = (state.reviewQueueIndex + offset + state.reviewQueue.length) % state.reviewQueue.length;
  renderReviewQueue();
}

async function saveLinkReview(decision) {
  const item = state.reviewQueue[state.reviewQueueIndex];
  if (!item) return;
  const buttons = [elements.responseReviewUndo, elements.responseReviewFollowup, elements.responseReviewReject, elements.responseReviewConfirm];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    await api("/api/link-reviews", {
      method: "POST",
      body: JSON.stringify({ link_id: item.link_id, decision, note: elements.responseReviewNote.value.trim() }),
    });
    showToast(decision ? `Link marked ${decision.replaceAll("_", " ")}.` : "Review decision removed.");
    await loadReviewQueue(decision ? "" : item.link_id);
  } catch (error) {
    showToast(error.message);
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function openResponseReview() {
  populateReviewCities();
  elements.responseReviewStatus.value = "pending";
  elements.responseReviewCity.value = "";
  state.reviewQueueIndex = 0;
  if (!elements.responseReviewDialog.open) elements.responseReviewDialog.showModal();
  try {
    await loadReviewQueue();
  } catch (error) {
    showToast(error.message);
  }
}

function resetSourceViewer() {
  elements.sourceViewerLoading.classList.remove("hidden");
  elements.adobePdfViewer.classList.add("hidden");
  elements.nativePdfViewer.classList.add("hidden");
  elements.spreadsheetViewer.classList.add("hidden");
  elements.unsupportedViewer.classList.add("hidden");
  elements.adobePdfViewer.replaceChildren();
  elements.nativePdfViewer.src = "about:blank";
  elements.spreadsheetGrid.replaceChildren();
  elements.sourceViewerStatus.textContent = "";
}

function formatSourceLocation(location) {
  if (location.sheet_name) {
    return `${location.sheet_name}${location.cell_range ? ` · ${location.cell_range}` : ""}`;
  }
  if (location.page_number) return `${location.preview_document_id ? "Preview page" : "Page"} ${location.page_number}`;
  if (location.paragraph_index) return `Paragraph ${location.paragraph_index}${location.preview_document_id ? " · page mapping unavailable" : ""}`;
  return location.metadata?.legacy_location || "Location not recorded";
}

function waitForAdobeSdk(timeoutMs = 5000) {
  if (window.AdobeDC?.View) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("Adobe PDF Embed SDK did not load")), timeoutMs);
    document.addEventListener("adobe_dc_view_sdk.ready", () => {
      window.clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

function pdfSearchCandidates(location) {
  let quote = String(location.exact_quote || location.normalized_quote || "")
    .replace(/_x000D_/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!quote) return [];

  // Extracted spreadsheet rows often prefix the PDF text with a discipline
  // such as "General". That label is not part of the visible PDF sentence.
  quote = quote.replace(
    /^(?:general|building|planning|fire|public works|electrical|mechanical|plumbing|structural|civil)\s+(?=(?:please|provide|show|revise|note|verify|indicate|submit|remove|add|clarify|identify|correct|when)\b)/i,
    "",
  );

  const sentences = quote.match(/[^.!?]+[.!?]?/g)?.map((value) => value.trim()).filter(Boolean) || [quote];
  const candidates = [];
  const add = (value) => {
    const candidate = String(value || "").replace(/\s+/g, " ").trim();
    if (candidate.split(" ").length >= 4 && !candidates.includes(candidate)) candidates.push(candidate.slice(0, 700));
  };
  sentences.slice(0, 3).forEach((sentence) => {
    add(sentence);
    add(sentence.replace(/[.!?]+$/, ""));
  });
  return candidates.slice(0, 6);
}

function waitForPdfSearch(searchObject, timeoutMs = 2600) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      resolve(value);
    };
    const timer = window.setTimeout(() => finish(null), timeoutMs);
    try {
      searchObject.onResultsUpdate((result) => {
        const total = Number(result?.totalResults || 0);
        if (total > 0) finish(result);
        else if (String(result?.status || "").toUpperCase() === "COMPLETED") finish(null);
      });
    } catch (_error) {
      finish(null);
    }
  });
}

async function searchPdfEvidence(apis, location) {
  for (const candidate of pdfSearchCandidates(location)) {
    try {
      const searchObject = await apis.search(candidate);
      const result = await waitForPdfSearch(searchObject);
      if (result) return { candidate, result };
      await Promise.resolve(searchObject?.clear?.()).catch(() => {});
    } catch (_error) {
      // A shorter candidate may still match PDFs with unusual line breaks.
    }
  }
  return null;
}

function highlightAnnotation(documentId, pageNumber, box, index) {
  const [xMin, yMin, xMax, yMax] = box;
  const timestamp = new Date().toISOString();
  return {
    "@context": ["https://www.w3.org/ns/anno.jsonld", "https://comments.acrobat.com/ns/anno.jsonld"],
    type: "Annotation",
    id: window.crypto?.randomUUID?.() || `evidence-${Date.now()}-${index}`,
    bodyValue: "Cited evidence",
    motivation: "commenting",
    target: {
      source: documentId,
      selector: {
        type: "AdobeAnnoSelector",
        subtype: "highlight",
        node: { index: Math.max(0, pageNumber - 1) },
        boundingBox: box,
        quadPoints: [xMin, yMax, xMax, yMax, xMin, yMin, xMax, yMin],
        strokeColor: "#f0b429",
        opacity: 0.35,
      },
    },
    creator: { type: "Person", name: "Permit evidence viewer" },
    created: timestamp,
    modified: timestamp,
  };
}

async function applyPdfEvidence(adobeViewer, source, viewerDocumentId) {
  const location = source.location;
  const page = location.page_number || 1;
  const boxes = location.pdf_bounding_boxes || [];
  const apis = await adobeViewer.getAPIs();
  const firstBox = boxes[0];
  await apis.gotoLocation(page, firstBox?.[0] || 0, firstBox?.[3] || 0).catch(() => {});
  if (boxes.length) {
    try {
      const manager = await adobeViewer.getAnnotationManager();
      await manager.addAnnotations(boxes.map((box, index) => highlightAnnotation(viewerDocumentId, page, box, index)));
      elements.sourceViewerStatus.textContent = `Opened page ${page} and highlighted ${boxes.length} stored evidence area${boxes.length === 1 ? "" : "s"}.`;
      return;
    } catch (_error) {
      // Use exact-text search if the viewer cannot import the coordinate annotation.
    }
  }
  const match = await searchPdfEvidence(apis, location);
  if (match) {
    const shortMatch = match.candidate.length > 72 ? `${match.candidate.slice(0, 69)}…` : match.candidate;
    elements.sourceViewerStatus.textContent = `Opened page ${page} and highlighted “${shortMatch}”.`;
    return;
  }
  elements.sourceViewerStatus.textContent = `Opened page ${page}, but this PDF's text layer did not contain the extracted evidence. The full evidence remains visible on the left.`;
}

function showNativePdfFallback(source, reason = "") {
  const page = source.location.page_number || 1;
  elements.sourceViewerLoading.classList.add("hidden");
  elements.adobePdfViewer.classList.add("hidden");
  elements.nativePdfViewer.classList.remove("hidden");
  elements.nativePdfViewer.src = `${source.preview_url}#page=${page}`;
  elements.sourceViewerStatus.textContent = `Opened page ${page} in the browser PDF viewer${reason ? ` (${reason})` : ""}. Evidence text remains visible beside the document.`;
}

async function renderPdfSource(source) {
  const document = source.document;
  if (document.viewer_type === "pdf_preview" && document.preview_status !== "ready") {
    showUnsupportedSource(document.preview_error || "A PDF preview has not been generated for this document.");
    return;
  }
  try {
    state.appConfig ||= await api("/api/config");
    if (!state.appConfig.adobe_pdf_embed_client_id) throw new Error("Adobe client ID is not configured");
    await waitForAdobeSdk();
    elements.sourceViewerLoading.classList.add("hidden");
    elements.adobePdfViewer.classList.remove("hidden");
    const viewerDocumentId = source.location.preview_document_id || document.document_id;
    const adobeView = new window.AdobeDC.View({
      clientId: state.appConfig.adobe_pdf_embed_client_id,
      divId: "adobePdfViewer",
    });
    const previewPromise = adobeView.previewFile({
      content: { location: { url: source.preview_url } },
      metaData: { fileName: document.filename, id: viewerDocumentId, hasReadOnlyAccess: true },
    }, {
      embedMode: "FULL_WINDOW",
      showDownloadPDF: false,
      showPrintPDF: false,
      showAnnotationTools: false,
      showSaveButton: false,
      enableSearchAPIs: true,
      enableAnnotationAPIs: Boolean(source.location.pdf_bounding_boxes?.length),
      includePDFAnnotations: false,
    });
    const adobeViewer = await previewPromise;
    await applyPdfEvidence(adobeViewer, source, viewerDocumentId);
  } catch (error) {
    showNativePdfFallback(source, error.message);
  }
}

function cellInsideSelection(address, bounds) {
  if (!bounds || bounds.length !== 4) return false;
  const match = /^([A-Za-z]+)(\d+)$/.exec(address);
  if (!match) return false;
  const column = match[1].toUpperCase().split("").reduce((total, character) => total * 26 + character.charCodeAt(0) - 64, 0);
  const row = Number(match[2]);
  return row >= bounds[0] && column >= bounds[1] && row <= bounds[2] && column <= bounds[3];
}

function renderSpreadsheetGrid(payload) {
  elements.spreadsheetGrid.replaceChildren();
  const table = document.createElement("table");
  table.className = "sheet-table";
  const head = document.createElement("thead");
  const headerRow = document.createElement("tr");
  const corner = document.createElement("th");
  corner.className = "row-heading";
  headerRow.append(corner);
  payload.columns.forEach((column) => {
    const cell = document.createElement("th");
    cell.textContent = column;
    headerRow.append(cell);
  });
  head.append(headerRow);
  const body = document.createElement("tbody");
  const selectedCells = [];
  payload.rows.forEach((row) => {
    const node = document.createElement("tr");
    const rowHeading = document.createElement("th");
    rowHeading.className = "row-heading";
    rowHeading.textContent = row.row_number;
    node.append(rowHeading);
    const cells = new Map(row.cells.map((cell) => [cell.column, cell]));
    payload.columns.forEach((column) => {
      const data = cells.get(column);
      const cell = document.createElement("td");
      const address = data?.address || `${column}${row.row_number}`;
      const value = data?.value ?? "";
      const cited = cellInsideSelection(address, payload.selection_bounds);
      cell.dataset.address = address;
      cell.textContent = value;
      cell.title = `${address}: ${value}`;
      if (cited) {
        cell.classList.add("cited-cell");
        node.classList.add("cited-row");
        selectedCells.push({ cell, address, value: String(value) });
      }
      node.append(cell);
    });
    body.append(node);
  });
  table.append(head, body);
  elements.spreadsheetGrid.append(table);
  const primary = selectedCells.find(({ value }) => value.trim()) || selectedCells[0];
  if (primary) {
    primary.cell.classList.add("cited-cell-primary");
  }
  window.setTimeout(() => primary?.cell.scrollIntoView({ block: "center", inline: "center" }), 20);
}

async function loadSpreadsheet(source, requestedSheet = "") {
  const location = source.location;
  const citedSheet = location.sheet_name || "";
  const sheet = requestedSheet || citedSheet;
  const row = location.metadata?.source_row;
  const selection = requestedSheet && requestedSheet !== citedSheet ? "" : (location.cell_range || (row ? `A${row}:XFD${row}` : ""));
  const params = new URLSearchParams({ sheet, range: selection, page_size: "100" });
  const payload = await api(`${source.spreadsheet_url}?${params}`);
  elements.sourceViewerLoading.classList.add("hidden");
  elements.spreadsheetViewer.classList.remove("hidden");
  elements.spreadsheetSheet.replaceChildren();
  payload.sheet_names.forEach((name) => option(elements.spreadsheetSheet, name, name));
  elements.spreadsheetSheet.value = payload.sheet_name;
  elements.spreadsheetSelection.textContent = payload.selection ? `Cited range ${payload.selection}` : `Rows ${payload.start_row}–${payload.start_row + payload.page_size - 1}`;
  renderSpreadsheetGrid(payload);
  elements.sourceViewerStatus.textContent = payload.selection ? `Opened ${payload.sheet_name} and highlighted ${payload.selection}.` : `Opened ${payload.sheet_name}.`;
  elements.spreadsheetSheet.onchange = () => loadSpreadsheet(source, elements.spreadsheetSheet.value).catch((error) => showUnsupportedSource(error.message));
}

function showUnsupportedSource(message) {
  elements.sourceViewerLoading.classList.add("hidden");
  elements.adobePdfViewer.classList.add("hidden");
  elements.nativePdfViewer.classList.add("hidden");
  elements.spreadsheetViewer.classList.add("hidden");
  elements.unsupportedViewer.classList.remove("hidden");
  elements.unsupportedViewerMessage.textContent = message || "This format does not have an in-app preview. The extracted evidence and file metadata are still available.";
  elements.sourceViewerStatus.textContent = "Use Download original only if you need the source file itself.";
}

async function openSourceViewer(sourceId) {
  const request = ++state.sourceViewerRequest;
  resetSourceViewer();
  elements.sourceViewerTitle.textContent = "Loading source…";
  elements.sourceViewerMeta.textContent = "";
  elements.sourceViewerLocation.textContent = "—";
  elements.sourceViewerQuote.textContent = "Loading extracted evidence…";
  elements.sourceViewerDownload.classList.add("hidden");
  if (!elements.sourceViewerDialog.open) elements.sourceViewerDialog.showModal();
  try {
    const source = await api(`/api/sources/${encodeURIComponent(sourceId)}`);
    if (request !== state.sourceViewerRequest) return;
    const document = source.document;
    elements.sourceViewerTitle.textContent = document.filename;
    elements.sourceViewerMeta.textContent = `${source.relation} · ${document.original_document_type.toUpperCase()} · ${(document.size / 1024 / 1024).toFixed(1)} MB`;
    elements.sourceViewerLocation.textContent = formatSourceLocation(source.location);
    elements.sourceViewerQuote.textContent = source.location.exact_quote || "No extracted evidence text is available for this referenced file.";
    elements.sourceViewerDownload.href = source.original_download_url;
    elements.sourceViewerDownload.classList.remove("hidden");
    if (["pdf", "pdf_preview"].includes(source.location.viewer_type)) {
      await renderPdfSource(source);
    } else if (source.location.viewer_type === "spreadsheet") {
      await loadSpreadsheet(source);
    } else {
      showUnsupportedSource("This file type is not supported by an in-app viewer.");
    }
  } catch (error) {
    showUnsupportedSource(error.message);
  }
}

const SourceViewer = Object.freeze({
  open: openSourceViewer,
  renderPdf: renderPdfSource,
  renderSpreadsheet: loadSpreadsheet,
  renderUnsupported: showUnsupportedSource,
});

function evidenceItem(label, value) {
  const node = document.createElement("div");
  node.className = "evidence-item";
  const title = document.createElement("span");
  title.textContent = label;
  const content = document.createElement("strong");
  content.textContent = value || "—";
  node.append(title, content);
  return node;
}

function organizedText(blocks, fallback) {
  const container = document.createElement("div");
  container.className = "organized-text";
  const usable = Array.isArray(blocks) ? blocks : [];
  if (!usable.length) {
    const paragraph = document.createElement("p");
    paragraph.className = "full-text";
    paragraph.textContent = fallback;
    container.append(paragraph);
    return container;
  }
  usable.forEach((block) => {
    const section = document.createElement("section");
    if (block.title) {
      const title = document.createElement("h4");
      title.textContent = block.title;
      section.append(title);
    }
    if (block.kind === "list" && Array.isArray(block.items)) {
      const list = document.createElement("ul");
      block.items.forEach((item) => {
        const row = document.createElement("li");
        row.textContent = item;
        list.append(row);
      });
      section.append(list);
    } else {
      const paragraph = document.createElement("p");
      paragraph.textContent = block.text || fallback;
      section.append(paragraph);
    }
    container.append(section);
  });
  return container;
}

function renderDetail() {
  const comment = state.comments.find((item) => item.comment_id === state.activeId);
  elements.detailEmpty.classList.toggle("hidden", Boolean(comment));
  elements.detailContent.classList.toggle("hidden", !comment);
  elements.detailContent.replaceChildren();
  if (!comment) return;

  const inner = document.createElement("div");
  inner.className = "detail-content-inner";
  const kicker = document.createElement("div");
  kicker.className = "detail-kicker";
  const left = document.createElement("div");
  left.append(badge(comment.match_status === "matched" ? "Matched precedent" : "Unmatched comment", comment.match_status));
  left.append(document.createTextNode(" "));
  left.append(badge(comment.human_review_status.replaceAll("_", " "), comment.human_review_status));
  const categoryButton = document.createElement("button");
  categoryButton.type = "button";
  categoryButton.className = "text-button";
  categoryButton.textContent = `Category: ${comment.category}`;
  categoryButton.addEventListener("click", () => openCategoryDialog([comment.comment_id]));
  kicker.append(left, categoryButton);

  const title = document.createElement("h2");
  title.className = "detail-title";
  title.textContent = `${comment.discipline} comment ${comment.comment_number || ""}`.trim();
  const meta = document.createElement("div");
  meta.className = "detail-meta";
  [comment.city, comment.property_project, `Review round ${comment.review_round}`, comment.reviewer ? `Reviewer: ${comment.reviewer}` : "Reviewer not recorded"]
    .forEach((value) => meta.append(metadataChip(value)));

  const commentBlock = document.createElement("section");
  commentBlock.className = "content-block";
  const commentHeading = document.createElement("div");
  commentHeading.className = "block-heading";
  const commentTitle = document.createElement("h3");
  commentTitle.textContent = "Government comment";
  commentHeading.append(commentTitle, sourceLinks(comment.sources));
  const commentText = organizedText(comment.display_blocks, comment.display_text);
  commentBlock.append(commentHeading, commentText);

  inner.append(kicker, title, meta, commentBlock);

  if (comment.response) {
    const responseBlock = document.createElement("section");
    responseBlock.className = "content-block response-block";
    const responseHeading = document.createElement("div");
    responseHeading.className = "block-heading";
    const responseTitle = document.createElement("h3");
    responseTitle.textContent = "Historical company response";
    responseHeading.append(responseTitle, sourceLinks(comment.response.sources));
    const responseText = organizedText(comment.response.display_blocks, comment.response.display_text);
    responseBlock.append(responseHeading, responseText);
    inner.append(responseBlock);
  } else {
    const empty = document.createElement("section");
    empty.className = "no-response";
    const heading = document.createElement("h3");
    heading.textContent = "No response recorded";
    const copy = document.createElement("p");
    copy.textContent = "This comment remains useful evidence, but the selected historical source contains no company response.";
    empty.append(heading, copy);
    inner.append(empty);
  }

  const evidence = document.createElement("section");
  evidence.className = "source-evidence";
  const evidenceTitle = document.createElement("h3");
  evidenceTitle.textContent = "Source evidence";
  const grid = document.createElement("div");
  grid.className = "evidence-grid";
  grid.append(
    evidenceItem("Comment source", comment.source_filename),
    evidenceItem("Original location", comment.source_location),
    evidenceItem("Extraction", `${comment.extraction_method} · ${Math.round(Number(comment.extraction_confidence) * 100)}%`),
  );
  evidence.append(evidenceTitle, grid);
  inner.append(evidence);
  elements.detailContent.append(inner);
}

function updateSelectionToolbar() {
  const count = state.selected.size;
  elements.selectedCount.textContent = `${count} selected`;
  elements.categorize.disabled = count === 0;
  const visibleIds = state.visible.map((item) => item.comment_id);
  const selectedVisible = visibleIds.filter((id) => state.selected.has(id)).length;
  elements.selectVisible.checked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
  elements.selectVisible.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;
}

async function findSimilar() {
  const query = elements.historySearch.value.trim();
  if (!query) {
    showToast("Enter a keyword or a full comment first.");
    elements.historySearch.focus();
    return;
  }
  elements.smartSearchButton.disabled = true;
  elements.smartSearchButton.textContent = "Searching…";
  try {
    const payload = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        city: state.city,
        query,
        limit: 10,
        discipline: elements.discipline.value,
        category: elements.category.value,
      }),
    });
    state.similarity = new Map(payload.results.map((item) => [item.comment_id, item.score]));
    state.searchExplanations = new Map(payload.results.map((item) => [item.comment_id, {
      reason: item.reason || "Related permit requirement and requested action.",
      important_difference: item.important_difference || "",
    }]));
    state.searchMatchClasses = new Map(payload.results.map((item) => [item.comment_id, item.match_class || "unverified"]));
    state.searchHasDirectMatches = Boolean(payload.has_direct_matches);
    state.searchNoResultMessage = payload.no_result_message || "";
    state.similarityMode = true;
    state.searchModeLabel = payload.engine_label || "Gemini semantic ranking";
    updateSearchState();
    applyFilters();
    if (!payload.results.length) showToast(state.searchNoResultMessage || "No sufficiently relevant historical precedent was found.");
  } catch (error) {
    showToast(error.message);
  } finally {
    elements.smartSearchButton.disabled = false;
    elements.smartSearchButton.textContent = "Try Gemini Smart Search";
  }
}

function clearSimilarity() {
  state.similarity.clear();
  state.searchExplanations.clear();
  state.searchMatchClasses.clear();
  state.searchHasDirectMatches = false;
  state.searchNoResultMessage = "";
  state.similarityMode = false;
  applyFilters();
}

function updateSearchState() {
  const query = elements.historySearch.value.trim();
  const showPrompt = Boolean(query) && !state.similarityMode && state.visible.length === 0;
  elements.smartSearchPrompt.classList.toggle("hidden", !showPrompt);
  elements.searchModeNotice.classList.toggle("hidden", !state.similarityMode);
  if (state.similarityMode) {
    const directNote = state.searchHasDirectMatches ? "" : " No direct precedent was verified.";
    elements.searchModeText.textContent = `${state.similarity.size} ${state.city} comments ranked by ${state.searchModeLabel}.${directNote}`;
  }
}

function openCategoryDialog(ids) {
  state.categoryTarget = ids;
  elements.categoryInput.value = "";
  elements.categoryDialogHelp.textContent = `Apply a category to ${ids.length} comment${ids.length === 1 ? "" : "s"}.`;
  elements.categoryDialog.showModal();
  window.setTimeout(() => elements.categoryInput.focus(), 30);
}

async function saveCategory(category) {
  const ids = state.categoryTarget || [...state.selected];
  if (!ids.length) return;
  try {
    const payload = await api("/api/categories", {
      method: "POST",
      body: JSON.stringify({ comment_ids: ids, category }),
    });
    ids.forEach((id) => {
      const comment = state.comments.find((item) => item.comment_id === id);
      if (comment) comment.category = category || "Uncategorized";
    });
    const categoryPayload = await api(`/api/categories?city=${encodeURIComponent(state.city)}`);
    state.categories = categoryPayload.categories;
    populateFilters();
    elements.categoryDialog.close();
    applyFilters();
    showToast(category ? `Applied “${category}” to ${payload.updated} comment${payload.updated === 1 ? "" : "s"}.` : `Removed category from ${payload.updated} comment${payload.updated === 1 ? "" : "s"}.`);
  } catch (error) {
    showToast(error.message);
  }
}

function bindEvents() {
  elements.city.addEventListener("change", () => loadCity(elements.city.value).catch((error) => showToast(error.message)));
  elements.sourceViewerClose.addEventListener("click", () => elements.sourceViewerDialog.close());
  elements.sourceViewerDialog.addEventListener("close", () => {
    state.sourceViewerRequest += 1;
    elements.nativePdfViewer.src = "about:blank";
    elements.adobePdfViewer.replaceChildren();
  });
  elements.responseReviewButton.addEventListener("click", openResponseReview);
  elements.responseReviewClose.addEventListener("click", () => elements.responseReviewDialog.close());
  elements.responseReviewStatus.addEventListener("change", () => { state.reviewQueueIndex = 0; loadReviewQueue().catch((error) => showToast(error.message)); });
  elements.responseReviewCity.addEventListener("change", () => { state.reviewQueueIndex = 0; loadReviewQueue().catch((error) => showToast(error.message)); });
  elements.responseReviewPrevious.addEventListener("click", () => moveReviewQueue(-1));
  elements.responseReviewSkip.addEventListener("click", () => moveReviewQueue(1));
  elements.responseReviewConfirm.addEventListener("click", () => saveLinkReview("confirmed"));
  elements.responseReviewReject.addEventListener("click", () => saveLinkReview("rejected"));
  elements.responseReviewFollowup.addEventListener("click", () => saveLinkReview("needs_followup"));
  elements.responseReviewUndo.addEventListener("click", () => saveLinkReview(""));
  document.addEventListener("keydown", (event) => {
    if (!elements.responseReviewDialog.open || elements.sourceViewerDialog.open || event.target.matches("input, textarea, select")) return;
    const key = event.key.toLowerCase();
    if (key === "c") saveLinkReview("confirmed");
    if (key === "r") saveLinkReview("rejected");
    if (key === "f") saveLinkReview("needs_followup");
    if (key === "s") moveReviewQueue(1);
  });
  elements.historySearchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.similarityMode) {
      state.similarity.clear();
      state.similarityMode = false;
    }
    applyFilters();
  });
  elements.historySearch.addEventListener("input", () => {
    if (state.similarityMode) {
      state.similarity.clear();
      state.similarityMode = false;
    }
    applyFilters();
  });
  elements.smartSearchButton.addEventListener("click", findSimilar);
  elements.clearSmartSearch.addEventListener("click", clearSimilarity);
  [elements.project, elements.discipline, elements.round, elements.match, elements.category, elements.review]
    .forEach((element) => element.addEventListener("change", applyFilters));
  elements.selectVisible.addEventListener("change", () => {
    state.visible.forEach((comment) => {
      elements.selectVisible.checked ? state.selected.add(comment.comment_id) : state.selected.delete(comment.comment_id);
    });
    renderList();
    updateSelectionToolbar();
  });
  elements.categorize.addEventListener("click", () => openCategoryDialog([...state.selected]));
  elements.resetFilters.addEventListener("click", () => {
    clearFilterValues();
    clearSimilarity();
  });
  elements.categoryForm.addEventListener("submit", (event) => {
    if (event.submitter?.value !== "save") return;
    event.preventDefault();
    const category = elements.categoryInput.value.trim();
    if (!category) {
      showToast("Enter a category name or choose Remove category.");
      return;
    }
    saveCategory(category);
  });
  elements.removeCategory.addEventListener("click", () => saveCategory(""));
}

async function initialize() {
  bindEvents();
  const payload = await api("/api/data");
  const preferred = payload.cities.find((city) => city.name === "San Jose")?.name || payload.cities[0]?.name;
  if (!preferred) throw new Error("The dataset contains no cities.");
  state.cities = payload.cities;
  await loadCity(preferred);
  await loadReviewSummary();
}

initialize().catch((error) => {
  elements.datasetCount.textContent = "Dataset unavailable";
  showToast(error.message);
});
