import type { Space, ManifestVerification } from "./index.js";

export declare const BUNDLE_FILES: string[];
export declare function loadBundle(dir: string): Promise<{
  space: Space;
  manifest: Record<string, unknown> | null;
  verification: ManifestVerification | null;
}>;
