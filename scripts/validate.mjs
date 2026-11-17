#!/usr/bin/env node
/**
 * Repository hygiene, secret-leak, and Kubernetes Kustomize validator.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FORBIDDEN_DIRS = [".artifacts", ".runtime", ".worktrees", "backup", "graphify-out"];
const KUSTOMIZE_DIRS = ["kubernetes"];

const PRIVATE_KEY_RE = new RegExp(
  "-----BEGIN " + "(?:RSA|OPENSSH|EC|DSA) PRIVATE KEY-----"
);
const AGE_KEY_RE = new RegExp("AGE-SECRET-" + "KEY-1[0-9A-Z]+");
const ENV_NAME_RE = /^\.env(\..+)?$/;

const errors = [];

for (const dirName of FORBIDDEN_DIRS) {
  if (fs.existsSync(path.join(REPO_ROOT, dirName))) {
    errors.push(`Forbidden scratch/clutter directory present: ${dirName}`);
  }
}

function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === ".git" || entry.name === "node_modules") continue;
    const full = path.join(dir, entry.name);
    const rel = path.relative(REPO_ROOT, full).replace(/\\/g, "/");
    if (entry.isDirectory()) {
      walk(full);
      continue;
    }
    if (ENV_NAME_RE.test(entry.name) && !entry.name.endsWith(".env.example")) {
      errors.push(`Plaintext .env file not allowed in repository: ${rel}`);
    }
    if (/\.(woff2|png|jpg|sqlite|db)$/i.test(entry.name)) continue;
    const text = fs.readFileSync(full, "utf8");
    if (PRIVATE_KEY_RE.test(text) || AGE_KEY_RE.test(text)) {
      errors.push(`Raw private key material detected in: ${rel}`);
    }
    if (entry.name !== ".sops.yaml" && /\.sops\.ya?ml$/i.test(entry.name) && !entry.name.endsWith(".example")) {
      if (!/^\s*sops:/m.test(text) || !text.includes("ENC[AES256_GCM")) {
        errors.push(`SOPS file missing sops metadata or ENC payload: ${rel}`);
      }
    }
  }
}

walk(REPO_ROOT);

for (const kdir of KUSTOMIZE_DIRS) {
  const full = path.join(REPO_ROOT, kdir);
  if (!fs.existsSync(path.join(full, "kustomization.yaml"))) {
    errors.push(`Missing kustomization.yaml in ${kdir}`);
    continue;
  }
  try {
    const rendered = execFileSync("kubectl", ["kustomize", full], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    if (!rendered.trim()) {
      errors.push(`Empty kustomize render for ${kdir}`);
    } else {
      console.log(`OK kustomize: ${kdir} (${rendered.split("\n").length} lines)`);
    }
  } catch (err) {
    const stderr = err.stderr ? String(err.stderr).trim() : err.message;
    errors.push(`kubectl kustomize failed for ${kdir}: ${stderr}`);
  }
}

if (errors.length > 0) {
  console.error("\nValidation FAILED:");
  for (const msg of errors) console.error(` - ${msg}`);
  process.exit(1);
}

console.log("\nAll homelab-mediaserver validations passed.");
