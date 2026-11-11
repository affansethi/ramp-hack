// Copies the Vite build into app/static, which FastAPI serves. Node, not
// rm/cp, so the build works the same on Windows and Linux. index.html is
// written with LF endings: a Windows checkout has a CRLF web/index.html, and
// CI fails when the committed app/static differs from a Linux build.
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = resolve(here, "..", "dist");
const target = resolve(here, "..", "..", "app", "static");

rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
cpSync(dist, target, { recursive: true });

const index = join(target, "index.html");
writeFileSync(index, readFileSync(index, "utf8").replace(/\r\n/g, "\n"));
