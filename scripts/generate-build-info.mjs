import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const version = readFileSync(resolve("VERSION"), "utf8").trim();
const environment = (process.env.PV_ENVIRONMENT || "development").toLowerCase();
const commit = (process.env.PV_COMMIT || "unknown").trim().toLowerCase();
const repository = (process.env.PV_REPOSITORY || "unknown").trim();

if (!/^\d+\.\d+\.\d+$/.test(version)) {
  throw new Error("VERSION must contain a MAJOR.MINOR.PATCH release version");
}
if (!new Set(["development", "production", "test"]).has(environment)) {
  throw new Error("PV_ENVIRONMENT must be development, production, or test");
}
if (
  !/^[0-9a-f]{7,64}$/.test(commit) &&
  !(environment !== "production" && ["unknown", "development", "test"].includes(commit))
) {
  throw new Error("PV_COMMIT must be an immutable Git commit identifier in production");
}
if (repository !== "unknown" && !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
  throw new Error("PV_REPOSITORY must be an owner/repository identifier or unknown");
}

writeFileSync(
  resolve("public/build-info.json"),
  `${JSON.stringify({ version, commit, environment, repository }, null, 2)}\n`,
);
