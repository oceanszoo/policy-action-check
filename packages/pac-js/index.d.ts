// Types for pac.js (policy-action-check bundle reader). Results mirror the Python engine's JSON;
// see SPEC.md in the repository for every field.

export type Verdict = "invalid_code" | "violates" | "compliant_if_requirements_met" | "needs_info" | "compliant";
export type Features = Record<string, string>;
export type Notes = Record<string, string>;

export interface CodeError { token: string; message: string; did_you_mean: string | null }

export interface ParsedCode {
  features: Features;
  errors: CodeError[];
  name: string | null;
  date: string | null;
  [field: string]: unknown;
}

export interface CheckResult {
  code: string;
  verdict: Verdict;
  violated: string[];
  required_next: string[];
  open: string[];
  errors: CodeError[];
  features: Features;
  next_actions: Array<Record<string, unknown>>;
  [field: string]: unknown;
}

export interface FlowResult extends Partial<CheckResult> {
  flow?: CheckResult[];
  [field: string]: unknown;
}

export interface ManifestVerification {
  digests_ok: boolean;
  mismatched: string[];
  crlf_only: string[];
  exam: Record<string, unknown> | null;
  exam_passed: boolean;
  [field: string]: unknown;
}

export declare const SUPPORTED_MAJOR: string;
export declare const VERDICT_ORDER: Verdict[];
export declare const DEFAULT_TEXTS: Record<string, unknown>;
export declare const DEFAULT_SUBJECTIVE: string[];

export declare class BundleError extends Error {}

export declare class Space {
  constructor(generator: object, judge: object, records?: object[]);
  static fromTexts(generatorText: string, judgeText: string, recordsText?: string): Space;
  generator: Record<string, any>;
  judge: Record<string, any>;
  dims: Record<string, Record<string, any>>;
  rules: Array<Record<string, any>>;
  constants(): Features;
  defaults(): Features;
  multi(key: string): boolean;
}

export declare function parseCode(text: string, space: Space): ParsedCode;
export declare function formatCode(features: Features, space: Space, name?: string | null): string;
export declare function explainCode(features: Features, space: Space): string;
export declare function check(code: string, space: Space, date?: string | null, notes?: Notes | null): CheckResult;
export declare function checkFlow(code: string, space: Space, date?: string | null, notes?: Notes | null): FlowResult;
export declare function renderReport(result: FlowResult, space: Space): string;
export declare function nextQuestion(answers: string, space: Space, date?: string | null, notes?: Notes | null): Record<string, unknown>;
export declare function lookup(text: string, space: Space): Array<Record<string, unknown>>;
export declare function categories(space: Space): Array<Record<string, unknown>>;
export declare function fitDetail(features: Features, space: Space, date?: string | null): Record<string, unknown>;
export declare function minimalFixes(features: Features, space: Space, date?: string | null, limit?: number): Array<Record<string, unknown>>;
export declare function today(): string;
export declare function sha256Hex(text: string): Promise<string>;
export declare function verifyManifest(manifest: object, files: Record<string, string>): Promise<ManifestVerification>;
