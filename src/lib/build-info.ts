export type BuildInfo = {
  commit: string;
  environment: string;
  repository: string;
};

export async function fetchBuildInfo(): Promise<BuildInfo | null> {
  const response = await fetch("/build-info.json");

  return response.ok ? ((await response.json()) as BuildInfo) : null;
}

export function isDevelopmentBuild(identity: BuildInfo | null): boolean {
  return identity?.environment === "development";
}
