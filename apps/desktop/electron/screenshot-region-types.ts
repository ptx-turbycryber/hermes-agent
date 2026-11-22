/** A region capture the composer's attach menu asks for on the user's click. */
export type CaptureRegionResult =
  | { ok: true; height: number; png: Uint8Array; width: number }
  | { ok: false; reason: 'busy' | 'cancelled' | 'failed' | 'unavailable' }

export interface CaptureRegionApi {
  /** Opens the drag-to-select overlay on the display holding the cursor. */
  captureRegion: (options?: { hint?: string }) => Promise<CaptureRegionResult>
}
