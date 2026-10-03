type BoundRun = {
  id: string;
  asset_id: string;
  configuration?: {
    source?: { asset_id?: string };
    input_integrity?: { input_fingerprint: string; manifest: { asset_id: string } };
    result_binding_version?: string;
    correction_version?: string;
    correction_fingerprint?: string;
  };
  result?: {
    asset_id?: string;
    run_id?: string;
    input_fingerprint?: string;
    correction_fingerprint?: string;
  } | null;
};

export function validateKenRuns<T extends BoundRun>(assetId: string, runs: T[]): T[] {
  if (!Array.isArray(runs)) throw new Error("Invalid KEN history");
  for (const run of runs) {
    const config = run.configuration;
    const integrity = config?.input_integrity;
    if (
      run.asset_id !== assetId ||
      !run.id ||
      (config?.source?.asset_id && config.source.asset_id !== assetId) ||
      (integrity && integrity.manifest.asset_id !== assetId)
    )
      throw new Error("Video identity mismatch");
    const result = run.result;
    if (
      result &&
      config?.correction_version === "ken-corrections-v1" &&
      (!config.correction_fingerprint ||
        result.correction_fingerprint !== config.correction_fingerprint)
    )
      throw new Error("Correction identity mismatch");
    if (
      result &&
      (config?.result_binding_version ||
        result.asset_id ||
        result.run_id ||
        result.input_fingerprint) &&
      (result.asset_id !== assetId ||
        result.run_id !== run.id ||
        result.input_fingerprint !== integrity?.input_fingerprint)
    )
      throw new Error("Result identity mismatch");
  }
  return runs;
}
