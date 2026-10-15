import { createHash, createPublicKey, verify } from "node:crypto";
import { createReadStream } from "node:fs";

function decode(value, size) {
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(value || "")) throw new Error("Invalid signature encoding.");
  const bytes = Buffer.from(value, "base64");
  if (bytes.length !== size || bytes.toString("base64") !== value) throw new Error("Invalid signature size.");
  return bytes;
}

export function updaterPublicKey(text) {
  let lines = text.trim().split(/\r?\n/);
  if (lines.length === 1 && lines[0].length !== 56) {
    lines = Buffer.from(text.trim(), "base64").toString("utf8").trim().split(/\r?\n/);
  }
  const key = decode(lines.at(-1), 42);
  if (key.subarray(0, 2).toString() !== "Ed") throw new Error("Invalid public key algorithm.");
  return key;
}

function signatureParts(text, publicKey) {
  let lines = text.trim().split(/\r?\n/);
  if (lines.length === 1) lines = Buffer.from(text.trim(), "base64").toString("utf8").trim().split(/\r?\n/);
  if (lines.length !== 4 || !lines[0].startsWith("untrusted comment: ") ||
      !lines[2].startsWith("trusted comment: ")) throw new Error("Invalid signature envelope.");
  const packet = decode(lines[1], 74);
  const algorithm = packet.subarray(0, 2).toString();
  if (!["Ed", "ED"].includes(algorithm)) throw new Error("Invalid signature algorithm.");
  if (!packet.subarray(2, 10).equals(publicKey.subarray(2, 10))) throw new Error("Wrong product updater key.");
  return { algorithm, signature: packet.subarray(10), comment: lines[2].slice(17),
    global: decode(lines[3], 64) };
}

function verifyParts(data, parts, key) {
  const publicKey = createPublicKey({ key: Buffer.concat([
    Buffer.from("302a300506032b6570032100", "hex"), key.subarray(10),
  ]), format: "der", type: "spki" });
  if (!verify(null, data, publicKey, parts.signature)) throw new Error("Updater artifact signature failed.");
  if (!verify(null, Buffer.concat([parts.signature, Buffer.from(parts.comment)]), publicKey, parts.global)) {
    throw new Error("Updater trusted comment signature failed.");
  }
  return { keyId: Buffer.from(key.subarray(2, 10)).reverse().toString("hex").toUpperCase(),
    algorithm: parts.algorithm };
}

export function verifyUpdaterBytes(bytes, signature, publicKey) {
  const key = updaterPublicKey(publicKey);
  const parts = signatureParts(signature, key);
  return verifyParts(parts.algorithm === "ED" ? createHash("blake2b512").update(bytes).digest() : bytes, parts, key);
}

export async function verifyUpdaterFile(file, signature, publicKey) {
  const key = updaterPublicKey(publicKey);
  const parts = signatureParts(signature, key);
  // Native update packages use prehashed signatures. Avoid loading GiB packages into memory.
  if (parts.algorithm !== "ED") throw new Error("Native archive requires a prehashed updater signature.");
  const hash = createHash("blake2b512");
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return verifyParts(hash.digest(), parts, key);
}
