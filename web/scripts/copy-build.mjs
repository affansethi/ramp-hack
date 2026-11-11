// Copies the Vite build into app/static, which FastAPI serves. Node, not
// rm/cp, so the build works the same on Windows and Linux.
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = resolve(here, "..", "dist");
const target = resolve(here, "..", "..", "app", "static");

rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
cpSync(dist, target, { recursive: true });
