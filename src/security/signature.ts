import crypto from "node:crypto";

export interface SignatureInput {
  method: string;
  path: string;
  clientId: string;
  timestamp: string;
  nonce: string;
  fileSha256: string;
  fileSizeBytes: number;
  schemaVersion: string;
}

export function buildCanonicalString(input: SignatureInput): string {
  return [
    input.method,
    input.path,
    input.clientId,
    input.timestamp,
    input.nonce,
    input.fileSha256,
    String(input.fileSizeBytes),
    input.schemaVersion
  ].join("\n");
}

export function hmacSha256Hex(secret: string, canonicalString: string): string {
  return crypto
    .createHmac("sha256", secret)
    .update(canonicalString)
    .digest("hex");
}

export function timingSafeEqualHex(expectedHex: string, providedHex: string): boolean {
  if (!/^[0-9a-f]+$/i.test(expectedHex) || !/^[0-9a-f]+$/i.test(providedHex)) {
    return false;
  }

  if (expectedHex.length !== providedHex.length || expectedHex.length % 2 !== 0) {
    return false;
  }

  const expected = Buffer.from(expectedHex, "hex");
  const provided = Buffer.from(providedHex, "hex");
  if (expected.length !== provided.length) {
    return false;
  }

  return crypto.timingSafeEqual(expected, provided);
}

export function verifySignature(
  secret: string,
  canonicalString: string,
  providedSignatureHex: string
): boolean {
  const expected = hmacSha256Hex(secret, canonicalString);
  return timingSafeEqualHex(expected, providedSignatureHex.toLowerCase());
}

export function sha256Hex(payload: Buffer): string {
  return crypto.createHash("sha256").update(payload).digest("hex");
}

export function hashInviteCode(inviteCode: string): string {
  return crypto.createHash("sha256").update(inviteCode).digest("hex");
}

export function randomToken(prefix: string, bytes: number): string {
  return `${prefix}${crypto.randomBytes(bytes).toString("hex")}`;
}
