/**
 * Ghostless Node.js / TypeScript SDK
 * ===================================
 *
 * Installation:
 *   npm install ghostless
 *   # or: yarn add ghostless
 *
 * Zero production dependencies — uses native fetch (Node 18+) or node-fetch.
 *
 * Quick start:
 *   import { GhostlessClient } from 'ghostless';
 *
 *   const client = new GhostlessClient({
 *     apiKey:   'sk_live_gl_your_key',
 *     tenantId: 'your-tenant-slug',
 *   });
 *
 *   const result = await client.validate({
 *     workerId:       'worker_123',
 *     taskType:       'survey',
 *     payload:        { responses: { q1: 'A', q2: 'B' } },
 *     completionTime: 45.2,
 *   });
 *
 *   console.log(result.decision);     // 'accept' | 'review' | 'reject'
 *   console.log(result.riskLevel);    // 'low' | 'medium' | 'high' | 'critical'
 *   console.log(result.explanation);  // plain-English reason
 *   console.log(result.shouldSubmit); // boolean shorthand
 */

import { createHmac, timingSafeEqual } from 'crypto';

// ─── Types ───────────────────────────────────────────────────────────────────

export type Decision   = 'accept' | 'review' | 'reject';
export type RiskLevel  = 'low' | 'medium' | 'high' | 'critical';
export type Tier       = 'bronze' | 'silver' | 'gold' | 'elite';
export type Lifecycle  = 'new' | 'learning' | 'trusted' | 'flagged' | 'banned';

export interface ClientOptions {
  apiKey:       string;
  tenantId:     string;
  baseUrl?:     string;        // default: https://api.ghostless.io
  timeout?:     number;        // ms, default: 10000
  hmacSecret?:  string;        // optional request signing
}

export interface ValidateInput {
  workerId:        string;
  taskType:        'image_label' | 'transcription' | 'survey' | 'moderation' | string;
  payload:         Record<string, unknown>;
  completionTime:  number;    // seconds
  projectId?:      string;
  difficulty?:     number;    // 0.1–10.0, default 1.0
  idempotencyKey?: string;
}

export interface ValidationWarning {
  code:     string;
  message:  string;
  severity: 'info' | 'warning' | 'error';
  field?:   string;
}

/** Raw API response (verbatim from server) */
export interface ValidationResponse {
  validation_id:    string;
  worker_id:        string;
  task_type:        string;
  quality_score:    number;
  allow_submit:     boolean;
  warnings:         ValidationWarning[];
  suggestions:      string[];
  flags:            string[];
  anomaly_score:    number;
  velocity_warning: boolean;
  shadow_banned:    boolean;
  processed_ms:     number;
}

/** Enriched result with business-friendly helpers */
export interface ValidationResult extends ValidationResponse {
  /** High-level submission decision */
  readonly decision:     Decision;
  /** Risk classification */
  readonly riskLevel:    RiskLevel;
  /** Plain-English reason for the decision */
  readonly explanation:  string;
  /** Simple bool: should you accept this submission? */
  readonly shouldSubmit: boolean;
}

export interface WorkerScoreResponse {
  worker_id:          string;
  external_id:        string;
  tier:               Tier;
  trust_score:        number;
  accuracy_7d?:       number;
  accuracy_30d?:      number;
  accuracy_all?:      number;
  total_tasks:        number;
  accepted_tasks:     number;
  streak_days:        number;
  last_calculated_at?: string;
}

export interface WorkerScore extends WorkerScoreResponse {
  readonly acceptanceRate: number | null;
  readonly trustLabel:     string;
  readonly summary:        string;
}

export interface ScoreExplanation {
  worker_id:         string;
  trust_score:       number;
  algorithm_version: string;
  credible_interval: { lower: number; upper: number; width: number };
  posterior_mean:    number;
  total_tasks:       number;
  lifecycle_stage:   Lifecycle;
  score_volatility?: number;
  components:        Record<string, number | boolean | null>;
  /** Plain-English summary (ready to display in your UI) */
  narrative:         string;
  last_updated?:     string;
  readonly confidenceBand: string;
}

export interface WebhookEvent<T = Record<string, unknown>> {
  event:           string;
  tenant_id:       string;
  data:            T;
  timestamp:       number;
  attempt:         number;
  idempotency_key: string;
}

export interface GhostlessError extends Error {
  statusCode: number;
  response?:  unknown;
}

// ─── Error classes ────────────────────────────────────────────────────────────

function createError(message: string, statusCode: number, response?: unknown): GhostlessError {
  const err = new Error(message) as GhostlessError;
  err.statusCode = statusCode;
  err.response   = response;
  err.name = statusCode === 401 ? 'AuthenticationError'
           : statusCode === 404 ? 'NotFoundError'
           : statusCode === 429 ? 'RateLimitError'
           : statusCode >= 500  ? 'ServerError'
           : 'GhostlessError';
  return err;
}

// ─── Enrichers ────────────────────────────────────────────────────────────────

function enrichValidation(raw: ValidationResponse): ValidationResult {
  const decision: Decision = !raw.allow_submit ? 'reject'
    : raw.anomaly_score > 0.6 || raw.velocity_warning || raw.flags.length >= 2 ? 'review'
    : 'accept';

  const riskLevel: RiskLevel =
    raw.anomaly_score >= 0.75 || ['SPEED_FLAG', 'STRAIGHT_LINE'].some(f => raw.flags.includes(f)) ? 'critical'
    : raw.anomaly_score >= 0.5 || raw.velocity_warning || raw.flags.length >= 2 ? 'high'
    : raw.anomaly_score >= 0.25 || raw.flags.length >= 1 ? 'medium'
    : 'low';

  let explanation: string;
  if (raw.shadow_banned) {
    explanation = 'This worker is under review. Submissions are accepted but earnings are paused.';
  } else if (!raw.allow_submit) {
    const errors = raw.warnings.filter(w => w.severity === 'error');
    explanation  = errors.length > 0
      ? `Submission blocked: ${errors[0].message}`
      : 'Submission quality below threshold. Please review and resubmit.';
  } else if (decision === 'review') {
    const parts: string[] = [];
    if (raw.velocity_warning)   parts.push('high submission rate');
    if (raw.anomaly_score > 0.5) parts.push('anomalous patterns detected');
    if (raw.flags.length)        parts.push(`quality flags: ${raw.flags.slice(0, 2).join(', ')}`);
    explanation = `Submission flagged for review due to: ${parts.join('; ')}.`;
  } else {
    explanation = 'Submission looks good. Quality within expected parameters.';
  }

  return {
    ...raw,
    decision,
    riskLevel,
    explanation,
    shouldSubmit: decision === 'accept' || decision === 'review',
  };
}

function enrichScore(raw: WorkerScoreResponse): WorkerScore {
  const acceptanceRate = raw.total_tasks > 0
    ? Math.round(raw.accepted_tasks / raw.total_tasks * 1000) / 1000
    : null;

  const trustLabel =
    raw.trust_score >= 90 ? 'Elite'
    : raw.trust_score >= 75 ? 'Trusted'
    : raw.trust_score >= 60 ? 'Reliable'
    : raw.trust_score >= 40 ? 'Building'
    : 'New';

  const summary =
    `Worker ${raw.external_id} — ${trustLabel} (${raw.trust_score.toFixed(1)}/100) | ` +
    `Tier: ${raw.tier.charAt(0).toUpperCase() + raw.tier.slice(1)} | ` +
    `Tasks: ${raw.total_tasks} | ` +
    `Acceptance: ${acceptanceRate !== null ? (acceptanceRate * 100).toFixed(0) + '%' : 'N/A'} | ` +
    `Streak: ${raw.streak_days}d`;

  return { ...raw, acceptanceRate, trustLabel, summary };
}

function enrichExplanation(raw: Record<string, unknown>): ScoreExplanation {
  const ci = (raw.credible_interval ?? {}) as ScoreExplanation['credible_interval'];
  const ciWidth = ci.width ?? 1;
  const confidenceBand =
    ciWidth < 0.15 ? 'high confidence'
    : ciWidth < 0.30 ? 'moderate confidence'
    : 'low confidence (more data needed)';

  return { ...(raw as unknown as ScoreExplanation), confidenceBand };
}

// ─── Main Client ──────────────────────────────────────────────────────────────

export class GhostlessClient {
  private readonly apiKey:      string;
  private readonly tenantId:    string;
  private readonly baseUrl:     string;
  private readonly timeout:     number;
  private readonly hmacSecret?: string;

  constructor(options: ClientOptions) {
    this.apiKey     = options.apiKey;
    this.tenantId   = options.tenantId;
    this.baseUrl    = (options.baseUrl ?? 'https://api.ghostless.io').replace(/\/$/, '');
    this.timeout    = options.timeout ?? 10_000;
    this.hmacSecret = options.hmacSecret;
  }

  // ── Internal ───────────────────────────────────────────────────────────────

  private url(path: string): string {
    return `${this.baseUrl}/v1/${path.replace(/^\//, '')}`;
  }

  private headers(body?: string, extra?: Record<string, string>): Record<string, string> {
    const headers: Record<string, string> = {
      'X-API-Key':    this.apiKey,
      'X-Tenant-ID':  this.tenantId,
      'Content-Type': 'application/json',
      'User-Agent':   'ghostless-node-sdk/6.0.0',
      ...extra,
    };
    if (this.hmacSecret && body) {
      const ts    = Math.floor(Date.now() / 1000);
      const bodyHash = createHmac('sha256', Buffer.from(body)).digest('hex');
      const sig   = createHmac('sha256', this.hmacSecret)
        .update(`${ts}.${bodyHash}`)
        .digest('hex');
      headers['X-Signature-SHA256'] = `sha256=${sig}`;
      headers['X-Timestamp'] = String(ts);
    }
    return headers;
  }

  private async request<T>(method: string, path: string, body?: unknown, extraHeaders?: Record<string, string>): Promise<T> {
    const bodyStr  = body ? JSON.stringify(body) : undefined;
    const controller = new AbortController();
    const timer    = setTimeout(() => controller.abort(), this.timeout);

    try {
      const resp = await fetch(this.url(path), {
        method,
        headers: this.headers(bodyStr, extraHeaders),
        body:    bodyStr,
        signal:  controller.signal,
      });

      clearTimeout(timer);
      const data = await resp.json();

      if (!resp.ok) {
        throw createError(
          data?.detail?.message ?? data?.error ?? `HTTP ${resp.status}`,
          resp.status,
          data,
        );
      }
      return data as T;

    } catch (err: unknown) {
      clearTimeout(timer);
      if ((err as Error).name === 'AbortError') {
        throw createError(`Request timed out after ${this.timeout}ms`, 408);
      }
      throw err;
    }
  }

  // ── Validation ─────────────────────────────────────────────────────────────

  /**
   * Pre-validate a task submission before accepting it on your platform.
   *
   * @example
   * const result = await client.validate({
   *   workerId:       'user_456',
   *   taskType:       'image_label',
   *   payload:        { labels: ['cat', 'dog'] },
   *   completionTime: 23.5,
   * });
   *
   * if (result.shouldSubmit) {
   *   await yourPlatform.acceptTask(taskId);
   *   showWorker(result.explanation);  // "Submission looks good."
   * } else {
   *   showWorker(result.explanation);  // "Submission blocked: ..."
   *   showWorker(result.suggestions);  // actionable feedback
   * }
   */
  async validate(input: ValidateInput): Promise<ValidationResult> {
    const { idempotencyKey, ...rest } = input;
    const body = {
      worker_id:        rest.workerId,
      task_type:        rest.taskType,
      payload:          rest.payload,
      completion_time:  rest.completionTime,
      project_id:       rest.projectId,
      difficulty:       rest.difficulty ?? 1.0,
    };
    const extra: Record<string, string> = {};
    if (idempotencyKey) extra['Idempotency-Key'] = idempotencyKey;

    const raw = await this.request<ValidationResponse>('POST', '/validate/task', body, extra);
    return enrichValidation(raw);
  }

  // ── Scores ─────────────────────────────────────────────────────────────────

  /** Get a worker's current trust score with helper properties. */
  async getScore(workerId: string): Promise<WorkerScore> {
    const raw = await this.request<WorkerScoreResponse>('GET', `/workers/${workerId}/score`);
    return enrichScore(raw);
  }

  /**
   * Get full Bayesian score explanation with confidence intervals.
   *
   * The `narrative` field is a plain-English summary ready to display
   * in admin dashboards or worker profiles.
   */
  async getExplanation(workerId: string): Promise<ScoreExplanation> {
    const raw = await this.request<Record<string, unknown>>('GET', `/scores/${workerId}/explain`);
    return enrichExplanation(raw);
  }

  /** Get score change history for a worker. */
  async getScoreHistory(workerId: string, limit = 50): Promise<Record<string, unknown>[]> {
    const data = await this.request<{ history: Record<string, unknown>[] }>(
      'GET', `/scores/${workerId}/history?limit=${limit}`
    );
    return data.history;
  }

  /** Trigger an immediate score recalculation. */
  async triggerRecalc(workerId: string): Promise<{ queued: boolean; message: string }> {
    return this.request('POST', `/workers/${workerId}/score/recalc`);
  }

  /** Get top workers leaderboard. */
  async getLeaderboard(limit = 50, tier?: Tier): Promise<WorkerScore[]> {
    let path = `/workers/leaderboard?limit=${limit}`;
    if (tier) path += `&tier=${tier}`;
    const rows = await this.request<WorkerScoreResponse[]>('GET', path);
    return rows.map(enrichScore);
  }

  // ── Webhooks ───────────────────────────────────────────────────────────────

  /**
   * Configure webhook delivery for your tenant.
   *
   * @example
   * await client.configureWebhooks({
   *   url:    'https://yourapp.com/hooks/ghostless',
   *   events: ['task.validated', 'worker.promoted', 'fraud.detected'],
   *   secret: 'your-webhook-signing-secret',
   * });
   */
  async configureWebhooks(config: {
    url:     string;
    events:  string[];
    secret?: string;
  }): Promise<Record<string, unknown>> {
    return this.request('PUT', '/tenants/webhook', {
      webhook_url:    config.url,
      webhook_events: config.events,
      webhook_secret: config.secret,
    });
  }

  /**
   * Verify a webhook signature from Ghostless.
   * Use this in your webhook handler to confirm the request is authentic.
   *
   * @example
   * // Express.js
   * app.post('/hooks/ghostless', express.raw({ type: '*\/*' }), (req, res) => {
   *   const valid = GhostlessClient.verifyWebhook(
   *     req.body,                              // raw Buffer
   *     req.headers['x-ghostless-signature'], // "sha256=..."
   *     process.env.GHOSTLESS_WEBHOOK_SECRET,
   *   );
   *   if (!valid) return res.status(401).send('Invalid signature');
   *   const event = JSON.parse(req.body.toString()) as WebhookEvent;
   *   // handle event.event ('task.validated', etc.)
   *   res.send('ok');
   * });
   */
  static verifyWebhook(
    payload:   Buffer | string,
    signature: string,
    secret:    string,
  ): boolean {
    if (!signature.startsWith('sha256=')) return false;
    try {
      const expected = 'sha256=' + createHmac('sha256', secret)
        .update(payload)
        .digest('hex');
      const a = Buffer.from(signature);
      const b = Buffer.from(expected);
      if (a.length !== b.length) return false;
      return timingSafeEqual(a, b);
    } catch {
      return false;
    }
  }

  // ── Feature Flags ──────────────────────────────────────────────────────────

  async getFlags(): Promise<{ flag_name: string; enabled: boolean; rollout_pct: number }[]> {
    return this.request('GET', '/admin/flags');
  }

  async setFlag(flagName: string, enabled: boolean, rolloutPct = 100): Promise<void> {
    await this.request('POST', '/admin/flags', {
      flag_name:   flagName,
      enabled,
      rollout_pct: rolloutPct,
    });
  }
}

// Re-export verifyWebhook as a standalone function for convenience
export const verifyWebhook = GhostlessClient.verifyWebhook;

export default GhostlessClient;
