/**
 * The dashboard's only way to talk to the API.
 *
 * Relative paths on purpose: Caddy serves the dashboard at `/` and proxies
 * `/api/*` to the API, so a relative fetch is same-origin and the `eva_session`
 * cookie is sent automatically. A configured absolute base URL would add a CORS
 * surface and a second place for the origin to be wrong.
 *
 * Errors are surfaced as the API's own `{error: {code, message}}` shape rather than
 * collapsed into a string, because the screens branch on `code` (401 means "log in",
 * 404 means "this conversation is not yours or is gone") and a bare message would
 * force string matching.
 */

export class ApiFailure extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });

  if (!response.ok) {
    let code = `http_${response.status}`;
    let message = response.statusText;
    try {
      const body = await response.json();
      const error = body?.detail?.error ?? body?.error;
      if (error?.code) {
        code = error.code;
        message = error.message ?? message;
      }
    } catch {
      // A non-JSON error body (a proxy error page, say) keeps the status text.
    }
    throw new ApiFailure(response.status, code, message);
  }
  // 204 has no body; calling .json() on it throws a parse error that would be
  // reported as a failed logout even though the session cookie was cleared.
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export type HandoverBadge = {
  reason: string;
  status: string;
  notified_at: string | null;
  ambiguous: boolean;
};

export type ConversationSummary = {
  id: string;
  status: string;
  bot_id: string;
  bot_name: string;
  channel: string;
  locale: string;
  page_url: string | null;
  visitor_name: string | null;
  visitor_email: string | null;
  started_at: string;
  last_activity_at: string;
  ended_at: string | null;
  message_count: number;
  handover: HandoverBadge | null;
  csat_up: number;
  csat_down: number;
};

export type Turn = {
  id: string;
  role: string;
  content: string;
  citations: unknown[];
  confidence: number | null;
  model: string | null;
  latency_ms: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: string;
  feedback: string | null;
  feedback_comment: string | null;
};

export type ConversationDetail = {
  summary: ConversationSummary;
  transcript: string;
  turns: Turn[];
};

export type ConversationList = {
  items: ConversationSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type AnalyticsSummary = {
  window: {
    from: string;
    to: string;
    days: number;
    timezone: string;
    basis: string;
  };
  counts: {
    conversations: number;
    handed_over_conversations: number;
    visitor_messages: number;
    assistant_messages: number;
    answered_turns: number;
  };
  resolution_proxies: {
    answered_turn_rate: number | null;
    handover_rate: number | null;
    definitions: Record<string, string>;
  };
  latency_ms: {
    p50: number | null;
    p95: number | null;
    samples: number;
    definition: string;
  };
  tokens: { prompt: number; completion: number; total: number };
  csat: {
    up: number;
    down: number;
    votes: number;
    positive_pct: number | null;
  };
  top_unanswered_questions: { question: string; occurrences: number }[];
  definitions: Record<string, string>;
};

export function listConversations(params: {
  status?: string;
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
}): Promise<ConversationList> {
  const query = new URLSearchParams();
  if (params.status) query.set("status", params.status);
  if (params.from) query.set("from", params.from);
  if (params.to) query.set("to", params.to);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const suffix = query.toString();
  return request<ConversationList>(`/api/v1/conversations${suffix ? `?${suffix}` : ""}`);
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/api/v1/conversations/${encodeURIComponent(id)}`);
}

export function analyticsSummary(days = 30): Promise<AnalyticsSummary> {
  return request<AnalyticsSummary>(`/api/v1/analytics/summary?days=${days}`);
}

export function exportUrl(id: string, format: "txt" | "json"): string {
  return `/api/v1/conversations/${encodeURIComponent(id)}/export?format=${format}`;
}

export type Session = {
  user_id: string;
  tenant_id: string;
  email: string;
  role: string;
};

export function login(email: string, password: string): Promise<Session> {
  return request<Session>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function register(
  email: string,
  password: string,
  tenantName: string,
): Promise<Session> {
  return request<Session>("/api/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, tenant_name: tenantName }),
  });
}

export function currentSession(): Promise<Session> {
  return request<Session>("/api/v1/auth/me");
}

export function logout(): Promise<void> {
  return request<void>("/api/v1/auth/logout", { method: "POST" });
}

/* ---------------------------------------------------------------------------
 * Bot configuration (ADMN-03)
 * ------------------------------------------------------------------------- */

export type BotConfig = {
  id: string;
  public_id: string;
  name: string;
  welcome_message: string | null;
  confidence_threshold: number;
  powered_by_visible: boolean;
  appearance: Record<string, string>;
  suggestions: string[];
  dynamic_suggestions: boolean;
  appearance_defaults: Record<string, string>;
  active_knowledge_version_id: string | null;
};

export type BotPatch = {
  name?: string;
  welcome_message?: string;
  confidence_threshold?: number;
  powered_by_visible?: boolean;
  appearance?: Record<string, string>;
  suggestions?: string[];
  dynamic_suggestions?: boolean;
};

export function getBot(botId: string): Promise<BotConfig> {
  return request<BotConfig>(`/api/v1/bots/${encodeURIComponent(botId)}`);
}

export function updateBot(botId: string, patch: BotPatch): Promise<BotConfig> {
  return request<BotConfig>(`/api/v1/bots/${encodeURIComponent(botId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function listBots(): Promise<BotConfig[]> {
  return request<BotConfig[]>("/api/v1/bots");
}

export type EmbedSnippet = {
  public_id: string;
  snippet: string;
  script_url: string;
};

export function embedSnippet(botId: string): Promise<EmbedSnippet> {
  return request<EmbedSnippet>(
    `/api/v1/bots/${encodeURIComponent(botId)}/embed-snippet`,
  );
}

export type KnowledgeDocument = {
  id: string;
  title: string;
  uri: string | null;
  version_id: string | null;
  version_no: number | null;
  status: string | null;
  error: string | null;
  chunk_count: number;
};

export type KnowledgeUpload = {
  document_id: string;
  version_id: string;
  chunks: number;
  status: string;
};

export function listDocuments(): Promise<KnowledgeDocument[]> {
  return request<KnowledgeDocument[]>("/api/v1/knowledge/documents");
}

export function uploadDocument(
  filename: string,
  file: Blob,
): Promise<KnowledgeUpload> {
  // Raw bytes with the name in the query string, not multipart: the endpoint reads
  // the request stream directly because `python-multipart` is not an approved
  // dependency. Passing the File as the body sends exactly what it sent before.
  return request<KnowledgeUpload>(
    `/api/v1/knowledge/uploads?filename=${encodeURIComponent(filename)}`,
    { method: "POST", body: file },
  );
}

export function createFaq(question: string, answer: string): Promise<KnowledgeUpload> {
  return request<KnowledgeUpload>("/api/v1/knowledge/faqs", {
    method: "POST",
    body: JSON.stringify({ question, answer }),
  });
}

export function createCrawl(url: string, schedule: string): Promise<{ source_id: string; status: string }> {
  return request("/api/v1/knowledge/crawls", {
    method: "POST",
    body: JSON.stringify({ url, schedule }),
  });
}

export type PreviewHit = {
  text: string;
  score: number;
  section: string | null;
  document: string | null;
  uri: string | null;
};

export type PreviewResult = {
  hits: PreviewHit[];
  version_ids: string[];
  declined: boolean;
  reason: string | null;
  best_score: number;
  threshold: number;
  reranked: boolean;
};

export function publishVersion(
  botId: string,
  versionId: string,
): Promise<{ active_knowledge_version_id: string }> {
  return request("/api/v1/knowledge/publish", {
    method: "POST",
    body: JSON.stringify({ bot_id: botId, version_id: versionId }),
  });
}

export function previewAnswer(
  question: string,
  versionIds?: string[],
): Promise<PreviewResult> {
  return request<PreviewResult>("/api/v1/knowledge/preview", {
    method: "POST",
    body: JSON.stringify({
      question,
      ...(versionIds ? { version_ids: versionIds } : {}),
    }),
  });
}

export type Profile = {
  user_id: string;
  email: string;
  role: string;
  tenant_id: string;
  tenant_name: string;
  created_at: string;
};

export type HandoverSettings = {
  email: string | null;
  keywords: string[];
  notify_on_low_confidence: boolean;
  notify_on_negative_feedback: boolean;
};

export type TenantSettings = { handover: HandoverSettings };

export type Credential = { provider: string; last4: string; created_at: string };

export type WebhookSummary = { id: string; url: string; events: string[]; active: boolean };

export type AuditEntry = {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string | null;
  actor_user_id: string | null;
  created_at: string;
  ip: string | null;
  detail: Record<string, unknown>;
};

export function getProfile(): Promise<Profile> {
  return request<Profile>("/api/v1/settings/profile");
}

export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  return request<void>("/api/v1/settings/password", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}

export function getTenantSettings(): Promise<TenantSettings> {
  return request<TenantSettings>("/api/v1/settings/tenant");
}

export function updateTenantSettings(patch: {
  handover: Partial<HandoverSettings>;
}): Promise<TenantSettings> {
  return request<TenantSettings>("/api/v1/settings/tenant", {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function listCredentials(): Promise<Credential[]> {
  return request<Credential[]>("/api/v1/settings/credentials");
}

/** Providers the API accepts a key for. Mirrors `security.vault.ALLOWED_PROVIDERS`;
 *  an unlisted name is rejected by the endpoint, so offering it would only fail. */
export const CREDENTIAL_PROVIDERS = ["openai", "anthropic", "deepseek", "ollama_cloud"] as const;

/** The response carries only the provider and last four characters — there is no
 *  field a key could come back in, which is why the screen never needs to hide one. */
export function setCredential(provider: string, apiKey: string): Promise<Credential> {
  return request<Credential>("/api/v1/settings/credentials", {
    method: "POST",
    body: JSON.stringify({ provider, api_key: apiKey }),
  });
}

export function deleteCredential(provider: string): Promise<void> {
  return request<void>(`/api/v1/settings/credentials/${provider}`, { method: "DELETE" });
}

export function listWebhooks(): Promise<WebhookSummary[]> {
  return request<WebhookSummary[]>("/api/v1/webhooks");
}

/**
 * The only response that ever carries a webhook's signing secret. It is returned once,
 * at creation, and cannot be read back — so the screen must show it then or the owner
 * has an endpoint they cannot verify.
 */
export type CreatedWebhook = WebhookSummary & { secret: string };

export function createWebhook(url: string, events: string[]): Promise<CreatedWebhook> {
  return request<CreatedWebhook>("/api/v1/webhooks", {
    method: "POST",
    body: JSON.stringify({ url, events }),
  });
}

export function deleteWebhook(id: string): Promise<void> {
  return request<void>(`/api/v1/webhooks/${id}`, { method: "DELETE" });
}

export type WebhookTestResult = { ok: boolean; status: number | null; detail: string };

export function testWebhook(id: string): Promise<WebhookTestResult> {
  return request<WebhookTestResult>(`/api/v1/webhooks/${id}/test`, { method: "POST" });
}

/** The events the API can deliver. Kept beside the calls rather than hardcoded in the
 *  screen so an added event is one edit, not a screen that silently omits it. */
export const WEBHOOK_EVENTS = ["conversation.created", "handover.created"] as const;

export function listAuditEntries(limit = 50): Promise<AuditEntry[]> {
  return request<AuditEntry[]>(`/api/v1/audit-logs?limit=${limit}`);
}
