import type Database from "better-sqlite3";
import type { MultipartFile } from "@fastify/multipart";
import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { randomUUID } from "node:crypto";
import { z } from "zod";

import type { AppConfig } from "../config";
import type { ClientRecord } from "../db";
import type { DiscordForwarder } from "../discord/forward";
import {
  cleanupExpiredNonces,
  hasSeenNonce,
  isTimestampWithinSkew,
  persistNonce
} from "../security/nonces";
import {
  buildCanonicalString,
  sha256Hex,
  verifySignature
} from "../security/signature";
import type {
  ApiErrorResponse,
  UploadErrorCode,
  UploadMetadata,
  UploadSuccessResponse
} from "../types/api";

interface UploadRouteDeps {
  db: Database.Database;
  config: AppConfig;
  discordForwarder: DiscordForwarder;
}

interface ParsedUpload {
  metadata: UploadMetadata;
  csvBuffer: Buffer;
  verifiedFileSha256: string;
}

const UploadHeadersSchema = z.object({
  "x-scamscreener-client-id": z.string().min(1),
  "x-scamscreener-timestamp": z.string().regex(/^\d+$/),
  "x-scamscreener-nonce": z.string().uuid(),
  "x-scamscreener-signature": z.string().regex(/^[a-fA-F0-9]{64}$/),
  "x-scamscreener-signature-version": z.literal("v1")
});

const UploadMetadataSchema = z.object({
  schemaVersion: z.literal("1"),
  modVersion: z.string().min(1),
  aiModelVersion: z.string().min(1),
  playerName: z.string().min(1),
  playerUuid: z.string().min(1),
  clientTimestamp: z.string().min(1),
  fileSha256: z
    .string()
    .regex(/^[a-fA-F0-9]{64}$/)
    .transform((value) => value.toLowerCase()),
  fileSizeBytes: z.coerce.number().int().positive()
});

function uploadError(
  errorCode: UploadErrorCode,
  requestId: string
): ApiErrorResponse {
  return { ok: false, errorCode, requestId };
}

async function drainFile(part: MultipartFile): Promise<void> {
  for await (const _ of part.file) {
    // Intentionally draining.
  }
}

async function readMultipartPayload(
  request: FastifyRequest,
  maxUploadBytes: number
): Promise<ParsedUpload> {
  let metadataRaw: string | undefined;
  let csvBuffer: Buffer | undefined;
  let csvBytes = 0;
  const csvChunks: Buffer[] = [];

  const parts = request.parts();
  for await (const part of parts) {
    if (part.type === "field") {
      if (part.fieldname === "metadata") {
        metadataRaw = typeof part.value === "string" ? part.value : String(part.value);
      }
      continue;
    }

    const filePart = part as MultipartFile;
    if (filePart.fieldname !== "training_file") {
      await drainFile(filePart);
      continue;
    }

    for await (const chunk of filePart.file) {
      const asBuffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      csvBytes += asBuffer.length;
      if (csvBytes > maxUploadBytes) {
        throw new Error("FILE_TOO_LARGE");
      }
      csvChunks.push(asBuffer);
    }

    csvBuffer = Buffer.concat(csvChunks);
  }

  if (!metadataRaw || !csvBuffer) {
    throw new Error("PAYLOAD_INVALID");
  }

  let parsedMetadataJson: unknown;
  try {
    parsedMetadataJson = JSON.parse(metadataRaw);
  } catch {
    throw new Error("PAYLOAD_INVALID");
  }

  const metadataValidation = UploadMetadataSchema.safeParse(parsedMetadataJson);
  if (!metadataValidation.success) {
    throw new Error("PAYLOAD_INVALID");
  }

  if (csvBuffer.length > maxUploadBytes) {
    throw new Error("FILE_TOO_LARGE");
  }

  const metadata = metadataValidation.data;
  if (metadata.fileSizeBytes !== csvBuffer.length) {
    throw new Error("PAYLOAD_INVALID");
  }

  const verifiedFileSha256 = sha256Hex(csvBuffer);
  if (verifiedFileSha256 !== metadata.fileSha256) {
    throw new Error("PAYLOAD_INVALID");
  }

  return { metadata, csvBuffer, verifiedFileSha256 };
}

function writeAuditRow(
  deps: UploadRouteDeps,
  requestId: string,
  clientId: string | null,
  status: string,
  ip: string,
  errorCode?: UploadErrorCode
): void {
  deps.db
    .prepare(
      `
        INSERT INTO upload_audit (request_id, client_id, status, error_code, ip, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
      `
    )
    .run(
      requestId,
      clientId,
      status,
      errorCode ?? null,
      ip,
      new Date().toISOString()
    );
}

function sendError(
  deps: UploadRouteDeps,
  reply: FastifyReply,
  requestId: string,
  ip: string,
  statusCode: number,
  errorCode: UploadErrorCode,
  clientId: string | null
) {
  writeAuditRow(deps, requestId, clientId, "REJECTED", ip, errorCode);
  return reply.code(statusCode).send(uploadError(errorCode, requestId));
}

export async function registerUploadRoute(
  app: FastifyInstance,
  deps: UploadRouteDeps
): Promise<void> {
  app.post(
    "/api/v1/training-uploads",
    {
      config: {
        rateLimit: {
          max: deps.config.rateLimitPerClientPerMinute,
          timeWindow: "1 minute",
          keyGenerator: (request: FastifyRequest) =>
            String(request.headers["x-scamscreener-client-id"] ?? request.ip)
        }
      }
    },
    async (request, reply) => {
      const requestId = `req-${randomUUID()}`;
      const ip = request.ip;
      let clientIdForAudit: string | null = null;

      const parsedHeaders = UploadHeadersSchema.safeParse(request.headers);
      if (!parsedHeaders.success) {
        return sendError(
          deps,
          reply,
          requestId,
          ip,
          401,
          "AUTH_FAILED",
          clientIdForAudit
        );
      }

      const headers = parsedHeaders.data;
      clientIdForAudit = headers["x-scamscreener-client-id"];
      const timestampSeconds = Number(headers["x-scamscreener-timestamp"]);

      if (
        !isTimestampWithinSkew(
          timestampSeconds,
          Math.floor(Date.now() / 1000),
          deps.config.maxClockSkewSeconds
        )
      ) {
        return sendError(
          deps,
          reply,
          requestId,
          ip,
          401,
          "AUTH_FAILED",
          clientIdForAudit
        );
      }

      const client = deps.db
        .prepare(
          `
            SELECT client_id, client_secret, install_id, active, created_at, revoked_at
            FROM clients
            WHERE client_id = ?
            LIMIT 1
          `
        )
        .get(clientIdForAudit) as ClientRecord | undefined;

      if (!client || client.active !== 1) {
        return sendError(
          deps,
          reply,
          requestId,
          ip,
          401,
          "AUTH_FAILED",
          clientIdForAudit
        );
      }

      const nonce = headers["x-scamscreener-nonce"];
      if (hasSeenNonce(deps.db, clientIdForAudit, nonce)) {
        return sendError(
          deps,
          reply,
          requestId,
          ip,
          409,
          "NONCE_REPLAY",
          clientIdForAudit
        );
      }

      if (!request.isMultipart()) {
        return sendError(
          deps,
          reply,
          requestId,
          ip,
          422,
          "PAYLOAD_INVALID",
          clientIdForAudit
        );
      }

      let parsedUpload: ParsedUpload;
      try {
        parsedUpload = await readMultipartPayload(request, deps.config.maxUploadBytes);
      } catch (error) {
        const message = error instanceof Error ? error.message : "PAYLOAD_INVALID";
        const fastifyCode =
          typeof error === "object" &&
          error !== null &&
          "code" in error &&
          typeof (error as { code?: unknown }).code === "string"
            ? (error as { code: string }).code
            : undefined;

        if (message === "FILE_TOO_LARGE" || fastifyCode === "FST_REQ_FILE_TOO_LARGE") {
          return sendError(
            deps,
            reply,
            requestId,
            ip,
            413,
            "FILE_TOO_LARGE",
            clientIdForAudit
          );
        }

        return sendError(
          deps,
          reply,
          requestId,
          ip,
          422,
          "PAYLOAD_INVALID",
          clientIdForAudit
        );
      }

      const canonical = buildCanonicalString({
        method: "POST",
        path: "/api/v1/training-uploads",
        clientId: clientIdForAudit,
        timestamp: headers["x-scamscreener-timestamp"],
        nonce,
        fileSha256: parsedUpload.metadata.fileSha256,
        fileSizeBytes: parsedUpload.metadata.fileSizeBytes,
        schemaVersion: parsedUpload.metadata.schemaVersion
      });

      const providedSignature = headers["x-scamscreener-signature"].toLowerCase();
      const isValidSignature = verifySignature(
        client.client_secret,
        canonical,
        providedSignature
      );
      if (!isValidSignature) {
        return sendError(
          deps,
          reply,
          requestId,
          ip,
          401,
          "AUTH_FAILED",
          clientIdForAudit
        );
      }

      const now = new Date();
      const nowIso = now.toISOString();
      const expiresAtIso = new Date(
        now.getTime() + deps.config.nonceTtlSeconds * 1000
      ).toISOString();

      try {
        persistNonce(deps.db, clientIdForAudit, nonce, nowIso, expiresAtIso);
      } catch {
        return sendError(
          deps,
          reply,
          requestId,
          ip,
          409,
          "NONCE_REPLAY",
          clientIdForAudit
        );
      }

      try {
        cleanupExpiredNonces(deps.db);
      } catch (error) {
        request.log.warn({ err: error, requestId }, "Failed nonce cleanup");
      }

      let discordMessageId: string | null = null;
      try {
        const forwarded = await deps.discordForwarder(deps.config.discordWebhookUrl, {
          requestId,
          metadata: parsedUpload.metadata,
          csvData: parsedUpload.csvBuffer,
          verifiedFileSha256: parsedUpload.verifiedFileSha256
        });
        discordMessageId = forwarded.messageId;
      } catch {
        return sendError(
          deps,
          reply,
          requestId,
          ip,
          502,
          "DISCORD_FORWARD_FAILED",
          clientIdForAudit
        );
      }

      writeAuditRow(deps, requestId, clientIdForAudit, "FORWARDED", ip);

      const response: UploadSuccessResponse = {
        ok: true,
        requestId,
        discordMessageId: discordMessageId ?? "",
        verifiedFileSha256: parsedUpload.verifiedFileSha256
      };

      return reply.code(200).send(response);
    }
  );
}
